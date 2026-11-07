"""离线回放与评估。

用法：
    python -m rebench.replay synth --out data/synth --n 24      # 生成带真值的合成样本
    python -m rebench.replay eval  --data data/synth            # 评估并出报告
    python -m rebench.replay record --out data/real --n 20      # 真机采集（需 SDK）

每个样本是一个 .npz：depth / color / K / T / truth(JSON 字符串)。
把真机采集的样本按同样格式存下来，就能用同一套 eval 出准确率——这是关键：
现场改一个阈值，不用上真机，跑一次 eval 就知道变好还是变坏。
"""

import argparse
import json
import os
import sys
from typing import Dict, List

import numpy as np

from .perception import SceneModel, Slot, observe_slots

SLOTS = [
    Slot("tray", cx=0.0, cy=0.0, half_x=0.045, half_y=0.045),
    Slot("box_A", cx=0.16, cy=0.0, half_x=0.045, half_y=0.045),
    Slot("box_B", cx=-0.16, cy=0.0, half_x=0.045, half_y=0.045),
]
COLORS = {"red": (198, 62, 58), "blue": (58, 92, 190), "green": (72, 158, 92)}
GREY = (150, 150, 150)


def _paint(depth, color, K, cx, cy, half, rgb, z=0.555, table=0.60):
    h, w = depth.shape
    fx, fy = K[0, 0], K[1, 1]
    u0 = int(round(w / 2 + (cx - half) * fx / table))
    u1 = int(round(w / 2 + (cx + half) * fx / table))
    v0 = int(round(h / 2 + (cy - half) * fy / table))
    v1 = int(round(h / 2 + (cy + half) * fy / table))
    depth[v0:v1, u0:u1] = z
    color[v0:v1, u0:u1] = rgb


def synth_sample(seed: int, w=320, h=240, f=380.0):
    """按固定剧本生成样本，保证覆盖：正常/偏移/空/遮挡/未知色。"""
    rng = np.random.default_rng(seed)
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
    T = np.eye(4)
    depth = np.full((h, w), 0.60)
    color = np.full((h, w, 3), 235, dtype=np.uint8)
    truth: Dict[str, dict] = {}

    case = seed % 5
    for i, s in enumerate(SLOTS):
        truth[s.name] = {"found": False, "color": None, "dx": 0.0, "dy": 0.0, "reason": "empty"}

    # 正常：tray 放红块，位置准确
    if case == 0:
        _paint(depth, color, K, 0.0, 0.0, 0.02, COLORS["red"])
        truth["tray"] = {"found": True, "color": "red", "dx": 0.0, "dy": 0.0, "reason": "ok"}
    # 偏移：蓝块在 box_A 里偏 15mm
    elif case == 1:
        _paint(depth, color, K, 0.16 + 0.015, 0.0, 0.02, COLORS["blue"])
        truth["box_A"] = {"found": True, "color": "blue", "dx": 15.0, "dy": 0.0, "reason": "ok"}
    # 遮挡：box_B 区域深度挖空
    elif case == 2:
        _paint(depth, color, K, -0.16, 0.0, 0.02, COLORS["green"])
        cu = int(w / 2 + (-0.16) * f / 0.60)
        cv = int(h / 2)
        depth[cv - 55:cv + 55, cu - 55:cu + 55] = 0.0
        truth["box_B"] = {"found": False, "color": None, "dx": 0.0, "dy": 0.0, "reason": "occluded"}
    # 未知色：灰色物体，能被检测到但颜色应为 None
    elif case == 3:
        _paint(depth, color, K, 0.0, 0.0, 0.02, GREY)
        truth["tray"] = {"found": True, "color": None, "dx": 0.0, "dy": 0.0, "reason": "ok"}
    # 噪声：只在 box_A 放绿块，叠加深度噪声
    else:
        _paint(depth, color, K, 0.16, 0.008, 0.02, COLORS["green"])
        depth += rng.normal(0, 0.0008, depth.shape)
        truth["box_A"] = {"found": True, "color": "green", "dx": 0.0, "dy": 8.0, "reason": "ok"}

    return {
        "depth": depth.astype(np.float32),
        "color": color,
        "K": K,
        "T": T,
        "truth": json.dumps(truth, ensure_ascii=False),
    }


def cmd_synth(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    for i in range(args.n):
        s = synth_sample(i)
        np.savez_compressed(os.path.join(args.out, f"sample_{i:03d}.npz"), **s)
    print(f"已生成 {args.n} 个合成样本 → {args.out}")
    return 0


def cmd_record(args) -> int:
    """真机采集骨架。枚举名与返回格式必须现场核对。"""
    try:
        from galbot_sdk.g1 import GalbotRobot, SensorType
    except ImportError:
        print("未找到 galbot_sdk。真机采集需要先安装 SDK；无真机请用 synth。")
        return 2

    os.makedirs(args.out, exist_ok=True)
    robot = GalbotRobot()
    if not robot.init():
        print("robot.init() 失败")
        return 2
    import time
    time.sleep(3)

    for i in range(args.n):
        input(f"[{i+1}/{args.n}] 摆好场景后回车采集…")
        depth = robot.get_depth_data(SensorType.LEFT_ARM_CAMERA)
        color = robot.get_rgb_data(SensorType.LEFT_ARM_CAMERA)
        K = robot.get_camera_intrinsic(SensorType.LEFT_ARM_CAMERA)
        T = robot.get_sensor_extrinsic(SensorType.LEFT_ARM_CAMERA)
        if depth.max() > 20:
            depth = depth / 1000.0
        truth = json.loads(input("真值 JSON（如 {\"tray\":{\"found\":true,\"color\":\"red\",\"dx\":0,\"dy\":0}}）："))
        np.savez_compressed(
            os.path.join(args.out, f"real_{i:03d}.npz"),
            depth=depth.astype(np.float32), color=color, K=K, T=T,
            truth=json.dumps(truth, ensure_ascii=False),
        )
    print("采集完成 →", args.out)
    return 0


def cmd_eval(args) -> int:
    files = sorted(f for f in os.listdir(args.data) if f.endswith(".npz"))
    if not files:
        print("目录里没有 .npz 样本")
        return 2

    model = SceneModel(table_z=args.table_z) if args.table_z else SceneModel()
    rows: List[dict] = []
    conf = {"found": {"found": 0, "empty": 0, "occluded": 0},
            "empty": {"found": 0, "empty": 0, "occluded": 0},
            "occluded": {"found": 0, "empty": 0, "occluded": 0}}
    dx_err, color_ok, color_tot = [], 0, 0

    for fn in files:
        d = np.load(os.path.join(args.data, fn), allow_pickle=False)
        truth = json.loads(str(d["truth"]))
        res = observe_slots(d["depth"], d["color"], d["K"], d["T"], SLOTS, model)
        for slot, t in truth.items():
            r = res.get(slot, {})
            got_found = bool(r.get("found"))
            reason = "found" if got_found else r.get("reason", "empty")
            want = "found" if t["found"] else t.get("reason", "empty")
            conf[want][reason if reason in conf[want] else "empty"] += 1

            if t["found"] and got_found:
                dx_err.append(abs(r["dx_mm"] - t["dx"]))
                color_tot += 1
                color_ok += int(r.get("color") == t["color"])
            rows.append({"file": fn, "slot": slot, "want": want, "got": reason,
                         "want_color": t["color"], "got_color": r.get("color")})

    total = sum(sum(v.values()) for v in conf.values())
    correct = conf["found"]["found"] + conf["empty"]["empty"] + conf["occluded"]["occluded"]
    acc = correct / total if total else 0.0
    cacc = color_ok / color_tot if color_tot else 0.0
    mae = float(np.mean(dx_err)) if dx_err else 0.0
    mx = float(np.max(dx_err)) if dx_err else 0.0

    print(f"\n样本 {len(files)} 个，槽位判定 {total} 次")
    print(f"  有无判定准确率 : {acc*100:5.1f}%   ({correct}/{total})")
    print(f"  颜色判定准确率 : {cacc*100:5.1f}%   ({color_ok}/{color_tot})")
    print(f"  偏移 MAE / MAX : {mae:5.2f} mm / {mx:5.2f} mm")
    print("\n  混淆矩阵（行=真值，列=预测）")
    print(f"  {'':10s}{'found':>8s}{'empty':>8s}{'occluded':>10s}")
    for k, v in conf.items():
        print(f"  {k:10s}{v['found']:>8d}{v['empty']:>8d}{v['occluded']:>10d}")

    bad = [r for r in rows if r["want"] != r["got"] or
           (r["want"] == "found" and r["want_color"] != r["got_color"])]
    if bad:
        print(f"\n  异常样本 {len(bad)} 条：")
        for r in bad[:10]:
            print(f"    {r['file']} {r['slot']}: 期望 {r['want']}/{r['want_color']} → 实际 {r['got']}/{r['got_color']}")

    report = os.path.join(args.data, "report.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("# 视觉判定评估报告\n\n")
        f.write(f"- 样本数：{len(files)}\n- 槽位判定次数：{total}\n")
        f.write(f"- 有无判定准确率：{acc*100:.1f}%\n")
        f.write(f"- 颜色判定准确率：{cacc*100:.1f}%\n")
        f.write(f"- 偏移 MAE：{mae:.2f} mm，最大：{mx:.2f} mm\n\n")
        f.write("## 混淆矩阵\n\n| 真值\\预测 | found | empty | occluded |\n|---|---|---|---|\n")
        for k, v in conf.items():
            f.write(f"| {k} | {v['found']} | {v['empty']} | {v['occluded']} |\n")
        f.write("\n## 异常明细\n\n| 文件 | 槽位 | 期望 | 实际 |\n|---|---|---|---|\n")
        for r in bad:
            f.write(f"| {r['file']} | {r['slot']} | {r['want']}/{r['want_color']} | {r['got']}/{r['got_color']} |\n")
    print(f"\n报告已写入：{report}")
    return 0 if acc >= args.min_acc else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="视觉模块离线回放与评估")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("synth")
    p1.add_argument("--out", required=True)
    p1.add_argument("--n", type=int, default=20)
    p1.set_defaults(func=cmd_synth)

    p2 = sub.add_parser("record")
    p2.add_argument("--out", required=True)
    p2.add_argument("--n", type=int, default=20)
    p2.set_defaults(func=cmd_record)

    p3 = sub.add_parser("eval")
    p3.add_argument("--data", required=True)
    p3.add_argument("--table-z", type=float, default=0.0)
    p3.add_argument("--min-acc", type=float, default=0.9)
    p3.set_defaults(func=cmd_eval)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
