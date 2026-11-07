"""低位近距抓取：降到近处 -> 深度精定位 -> 一次抓取。

为什么这样做：
  矮积木在 40cm 外深度分辨不出来（D405 在 0.4m 处噪声 5-8mm，2cm 的积木信噪比不够）。
  把手降到桌面上方约 7cm，相机距离约 25cm，深度噪声降到 1-2mm，2cm 的积木立刻能分出高程。

关键技巧：两次探测求一致
  夹爪自己的手指也在视野里，会被当成"凸起"。判别方法很简单：
  把手臂抬高 9cm 再探一次 —— 真物体高度不变，夹爪会跟着升高。
  两次都能对上（位置差 <4cm、高度差 <2cm）的才认。

用法：
    python3 tools/lowprobe_grasp.py                 # 默认只读识别
    python3 tools/lowprobe_grasp.py --x .78 --y .12 # 指定大致位置（默认只读）
    python3 tools/lowprobe_grasp.py --dry           # 只定位不抓

安全约束（血泪）：
  - 横移一律在高空 1.06m 做，绝不低空横移（会把积木扫飞）
  - 每步先用 ikpath 预演整条路径：最低指尖 >= 桌面+2cm、横甩 <= 8cm、单关节 <= 45°
  - 每步执行后核对 err_mm <= 40，超了就停（防跳构型分支甩臂）
  - 指尖绝不进入桌面以上 1.2cm
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "robot_exec.py"
HOST = "galbot@192.168.167.46"
SOCK = "/tmp/kitt-console-live.sock"

TOOL_LEN = 0.189          # 交接估计值，需重新核对工具标定
HIGH = 1.06               # 高空横移高度
HOVER = 0.965             # 近距探测高度（指尖离桌面约 6.5cm）
PROBE_UP = 0.09           # 二次探测抬高量（用来识别夹爪自身）
MIN_TIP_CLEAR = 0.012     # 指尖离桌面的最小安全间隙
MAX_DELTA = 45.0          # 单步允许的最大关节变化
MAX_DEV_CM = 8.0         # 路径允许的最大横甩
MAX_ERR = 40.0            # 单步允许的最大到位误差(mm)
REACH = (0.58, 0.87, -0.06, 0.30)   # 可达区 x_min,x_max,y_min,y_max

_ID = [0]


from exec_transport import connect, call, close


def ascii_grid(g):
    """5mm 高程图 -> 字符画，每字符约 1cm。"""
    chars = " .:-=+*#%@"
    out = []
    for r in g[::2]:
        s = ""
        for v in r[::2]:
            s += " " if v <= 1 else chars[min(len(chars) - 1, 1 + int(v / 6))]
        out.append(s)
    return "\n".join(out)


def move_cart(p, x, y, z, speed=0.10, tag="", table_z=0.71):
    """带路径预演的移动：安全才下发，事后核对误差。"""
    dt, ik = call(p, {"cmd": "ikpath", "x": x, "y": y, "z": z, "steps": 12})
    if not ik or not ik.get("ok"):
        return False, {"why": "逆解失败 %s" % ((ik or {}).get("status"))}
    if ik["max_delta_deg"] > MAX_DELTA:
        return False, {"why": "单关节变化 %.1f° > %.0f°" % (ik["max_delta_deg"], MAX_DELTA)}
    if ik["min_tip_z"] < table_z + MIN_TIP_CLEAR:
        return False, {"why": "路径最低指尖 %.3f 离桌太近（桌面 %.3f）" % (ik["min_tip_z"], table_z)}
    if ik["max_dev_cm"] > MAX_DEV_CM:
        return False, {"why": "路径横甩 %.1f cm" % ik["max_dev_cm"]}
    dt, r = call(p, {"cmd": "execute_plan", "plan_id": ik["plan_id"],
                     "speed": speed, "table_z": table_z})
    if not r or not r.get("ok"):
        return False, {"why": "执行失败 %s" % json.dumps({k: v for k, v in (r or {}).items()
                                                      if k in ("status", "ik", "error")}, ensure_ascii=False)}
    err = r.get("err_mm")
    if err is None or err > MAX_ERR:
        return False, {"why": "到位误差无效或超限：%s mm" % err, "mount": r.get("mount")}
    print("    %s -> (%.3f, %.3f, %.3f)  误差 %s mm  关节 %.0f°  最低指尖 %.3f  %.0f ms"
          % (tag or "移动", r["mount"][0], r["mount"][1], r["mount"][2],
             err, ik["max_delta_deg"], ik["min_tip_z"], dt))
    return True, r


def probe(p, radius, label, frames=6):
    dt, pr = call(p, {"cmd": "probe", "frames": frames, "radius": radius, "peaks": 6})
    if not pr or not pr.get("ok"):
        print("   ✗ %s：%s" % (label, (pr or {}).get("error")))
        if pr:
            print("      桌面 %s 点数 %s 最高 %s cm 有效 %s%%"
                  % (pr.get("table_z"), pr.get("n"), pr.get("h_max_cm"), pr.get("valid_pct")))
        return None
    print("   %s %.0f ms  桌面 %.4f  凸起 %d 个  深度有效 %.0f%%"
          % (label, dt, pr["table_z"], len(pr.get("peaks") or []), pr["valid_pct"]))
    for pk in pr.get("peaks") or []:
        print("      (%.3f, %.3f)  高 %.1f cm  偏轴 %.1f cm  点数 %d"
              % (pk["xy"][0], pk["xy"][1], pk["h_cm"], pk["dist_axis_cm"], pk["n"]))
    return pr


def in_reach(x, y):
    return REACH[0] <= x <= REACH[1] and REACH[2] <= y <= REACH[3]


def stable_peaks(lo, hi):
    """两次探测求一致：真物体位置和高度都不随手臂升降而变，夹爪会跟着升高。

    用 abs_top（绝对高度，纯深度）判断，不用相对桌面的高度——
    桌面平面估计本身会有 1cm 级漂移，用它做差分会误杀真物体。
    位置取近距离那次（更准）。
    """
    out = []
    for a in (lo.get("peaks") or []):
        best, bd = None, 9.9
        for b in (hi.get("peaks") or []):
            d = ((a["xy"][0] - b["xy"][0]) ** 2 + (a["xy"][1] - b["xy"][1]) ** 2) ** 0.5
            if d < bd:
                best, bd = b, d
        if best is None or bd > 0.045:
            continue
        if abs(a.get("abs_top", 0) - best.get("abs_top", 0)) > 0.02:
            continue
        out.append({"xy": [a["xy"][0], a["xy"][1]],
                    "h_cm": a["h_cm"], "top_z": a["top_z"],
                    "abs_top": a.get("abs_top"), "n": a["n"]})
    return out


def merge_peaks(pks, gap=0.06, dh=1.5):
    """把挨得近、高度相当的两个峰并成一个物体（一块积木常被拆成两个峰）。"""
    groups = []
    for pk in sorted(pks, key=lambda q: -q["n"]):
        for g in groups:
            if abs(pk["h_cm"] - g["h_cm"]) < dh and \
               ((pk["xy"][0] - g["xy"][0]) ** 2 + (pk["xy"][1] - g["xy"][1]) ** 2) ** 0.5 < gap:
                w0, w1 = g["n"], pk["n"]
                g["xy"] = [(g["xy"][0] * w0 + pk["xy"][0] * w1) / (w0 + w1),
                           (g["xy"][1] * w0 + pk["xy"][1] * w1) / (w0 + w1)]
                g["n"] = w0 + w1
                g["h_cm"] = max(g["h_cm"], pk["h_cm"])
                g["top_z"] = max(g["top_z"], pk["top_z"])
                g["members"] = g.get("members", 1) + 1
                break
        else:
            groups.append({"xy": list(pk["xy"]), "h_cm": pk["h_cm"],
                           "top_z": pk["top_z"], "n": pk["n"], "members": 1})
    return groups


def head_rough(p):
    dt, r = call(p, {"cmd": "overview", "table_z": 0.71})
    if not r or not r.get("ok"):
        return []
    out = [c for c in (r.get("cands") or []) if in_reach(c["xy"][0], c["xy"][1])]
    print("  头部候选 %d 个（可达区 %d 个）%.0f ms" % (len(r.get("cands") or []), len(out), dt))
    for c in out[:4]:
        print("     xy=(%.3f, %.3f) 尺寸 %s cm" % (c["xy"][0], c["xy"][1], c["size_cm"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--x", type=float, default=None)
    ap.add_argument("--y", type=float, default=None)
    ap.add_argument("--radius", type=float, default=0.14)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true", help="仅当前视角识别，不移动")
    mode.add_argument("--execute", action="store_true", help="现场确认后低速分步执行")
    ap.add_argument("--min-h", type=float, default=2.5, help="认定为可抓物体的最小高度(cm)")
    args = ap.parse_args()

    p, boot = connect(allow_motion=args.execute)
    import atexit
    atexit.register(close, p)
    print("BOOT %s" % json.dumps({k: boot.get(k) for k in ("boot_ms", "depth_cam")}))
    if not boot.get("depth_cam"):
        print("✗ 深度相机不可用（观察台 8410 可能占着相机）")
        return 1

    st = call(p, {"cmd": "state"})[1]
    print("当前腕部 %s  夹爪 %.3f m" % (st["mount"], st["grip"]["width"]))
    if not args.execute:
        pr = probe(p, args.radius, "当前视角，只读")
        print("只读完成：未移动手臂、夹爪或底盘；单帧凸起尚未确认为积木。")
        return 0 if pr else 2
    health = call(p, {"cmd": "collision", "explicit": True})[1]
    if not health or not health.get("ok") or health.get("collisions"):
        print("碰撞服务未就绪或当前有碰撞，停止；不打开夹爪、不移动。", health)
        return 4
    if st["grip"]["width"] < 0.09:
        opened = call(p, {"cmd": "grip", "w": 0.105, "v": 0.06, "eff": 3, "blocking": False, "settle": 1.2})[1]
        if not opened or not opened.get("ok"):
            print("打开夹爪失败，停止。")
            return 1

    # ---------- 1) 决定先探哪一片 ----------
    hints = []
    if args.x is not None and args.y is not None:
        hints.append((args.x, args.y))
        print("使用指定位置 (%.3f, %.3f)" % (args.x, args.y))
    else:
        hints += [(c["xy"][0], c["xy"][1]) for c in head_rough(p)[:2]]
    hints += [(0.74, 0.08), (0.80, 0.14)]
    uniq = []
    for hx, hy in hints:
        if all(abs(hx - ux) > 0.06 or abs(hy - uy) > 0.06 for ux, uy in uniq):
            uniq.append((hx, hy))
    hints = uniq[:3]

    # ---------- 2) 逐站：高空对准 -> 降下 -> 探 -> 抬高 9cm -> 再探 ----------
    target = None
    best_grid = None
    all_stable = []
    for attempt in range(3):
        for cx, cy in hints:
            print("\n-- 探测站 (%.3f, %.3f) --" % (cx, cy))
            ok, info = move_cart(p, cx, cy, HIGH, speed=0.12, tag="① 高空对准")
            if not ok:
                print("   ✗ %s" % info.get("why")); continue
            ok, info = move_cart(p, cx, cy, HOVER, speed=0.06, tag="② 垂直下降")
            if not ok:
                print("   ✗ %s" % info.get("why")); continue
            lo = probe(p, args.radius, "近距离探测")
            if lo is None:
                continue
            best_grid = lo.get("grid")

            ok, info = move_cart(p, cx, cy, HOVER + PROBE_UP, speed=0.06, tag="③ 抬高复核")
            if not ok:
                print("   ✗ 抬高失败：%s" % info.get("why")); continue
            hi = probe(p, args.radius, "抬高后复核")
            if hi is None:
                continue

            both = stable_peaks(lo, hi)
            print("   两次一致（真物体）：%d 个" % len(both))
            for pk in both:
                print("      (%.3f, %.3f) 高 %.1f cm %s"
                      % (pk["xy"][0], pk["xy"][1], pk["h_cm"], "✓可达" if in_reach(*pk["xy"]) else "✗超区"))
            all_stable += both

            cand = [pk for pk in both if in_reach(*pk["xy"]) and args.min_h <= pk["h_cm"] <= 12.0]
            if not cand:
                print("   没有稳定且可达的目标")
                continue
            merged = merge_peaks(cand)
            print("   合并后物体 %d 个：" % len(merged))
            for g in merged:
                print("      (%.3f, %.3f) 高 %.1f cm 点数 %d%s"
                      % (g["xy"][0], g["xy"][1], g["h_cm"], g["n"],
                         "（%d 个峰合并）" % g["members"] if g["members"] > 1 else ""))
            merged.sort(key=lambda g: (-g["h_cm"], -g["n"]))

            # 逐个验证"真的够得着"：算抓取高度 -> ikpath 预演
            tz0 = lo.get("table_z", 0.71)
            for g in merged:
                gd = max(0.008, min(0.030, (g["h_cm"] / 100) * 0.5))
                mz = max(g["top_z"] + TOOL_LEN - gd, tz0 + MIN_TIP_CLEAR + TOOL_LEN)
                _, ikr = call(p, {"cmd": "ikpath", "x": g["xy"][0], "y": g["xy"][1], "z": mz})
                if not ikr or not ikr.get("ok"):
                    print("      ✗ (%.3f, %.3f) 逆解不可达" % (g["xy"][0], g["xy"][1]))
                    continue
                if ikr["max_delta_deg"] > MAX_DELTA or ikr["max_dev_cm"] > MAX_DEV_CM:
                    print("      ✗ (%.3f, %.3f) 路径不安全：关节 %.0f° 横甩 %.1fcm"
                          % (g["xy"][0], g["xy"][1], ikr["max_delta_deg"], ikr["max_dev_cm"]))
                    continue
                target = dict(g)
                target["table_z"] = tz0
                target["mount_z"] = mz
                print("   → 选中 (%.3f, %.3f) 高 %.1f cm  抓取高度 %.4f  关节 %.0f°"
                      % (g["xy"][0], g["xy"][1], g["h_cm"], mz, ikr["max_delta_deg"]))
                break
            if target:
                break
            print("   都在可达区外或路径不安全")
        if target:
            break

        print("目标不可达或路径不通过；保持当前姿态，不自动移动底盘。")
        break

    if target is None:
        print("未找到稳定可达目标，已停止；不自动回程。")
        return 2

    if best_grid:
        print("\n=== 最后一次近距高程图（每字符约 1cm）===")
        print(ascii_grid(best_grid))

    ox, oy = target["xy"]
    tz = target["table_z"]
    top = target["top_z"]
    h = top - tz
    gd = max(0.008, min(0.030, h * 0.5))
    mount_z = target.get("mount_z") or (top + TOOL_LEN - gd)
    floor = tz + MIN_TIP_CLEAR + TOOL_LEN
    if mount_z < floor:
        mount_z = floor
    print("\n=== 抓取计划 ===")
    print("目标 (%.4f, %.4f)  顶面 %.4f  桌面 %.4f  高 %.1f cm" % (ox, oy, top, tz, h * 100))
    print("抓深 %.1f cm -> 安装帧 %.4f  指尖 %.4f（离桌 %.1f cm）"
          % (gd * 100, mount_z, mount_z - TOOL_LEN, (mount_z - TOOL_LEN - tz) * 100))

    if args.dry:
        print("\n--dry：定位完成，不执行抓取")
        return 0

    # ---------- 3) 对准 + 一次下降 ----------
    print("\n=== 执行 ===")
    ok, info = move_cart(p, ox, oy, HIGH, speed=0.12, tag="④ 高空对准", table_z=tz)
    if not ok:
        print("✗ 中止：%s" % info.get("why")); return 1
    ok, info = move_cart(p, ox, oy, mount_z, speed=0.05, tag="⑤ 垂直下降", table_z=tz)
    if not ok:
        print("✗ 中止：%s" % info.get("why")); return 1

    # ---------- 4) 合爪 ----------
    dt, g1 = call(p, {"cmd": "grip", "w": 0.010, "v": 0.10, "eff": 2,
                      "blocking": False, "settle": 1.4})
    if not g1 or not g1.get("ok"):
        print("合爪失败或结果未知，停止，不自动抬臂或重试。")
        return 3
    got = g1["width"] > 0.016 and g1["effort"] > 12
    print("⑥ 合爪  宽 %.4f m  力矩 %.1f Nm  -> %s"
          % (g1["width"], g1["effort"], "夹住" if got else "没夹住"))
    if not got:
        print("夹持未确认，保持姿态等待现场检查。")
        return 3

    # ---------- 5) 抬升验证 ----------
    lift = min(mount_z + 0.15, 1.14)
    ok, info = move_cart(p, ox, oy, lift, speed=0.06, tag="⑦ 抬升", table_z=tz)
    if ok:
        gg = call(p, {"cmd": "state"})[1]["grip"]
        print("   抬升后  宽 %.4f m  力矩 %.1f Nm" % (gg["width"], gg["effort"]))
        print("\n抬升完成，夹爪读数不能单独证明抓到物体，请现场确认。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        print("✗ 与执行器的连接断了（脚本可能没传上去）")
        sys.exit(1)
