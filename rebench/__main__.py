"""用法：
    python -m rebench --mock --rounds 3
    python -m rebench --real                       # 需要真机 + galbot_sdk
"""

import argparse
import os
import sys

from .backend import MockBackend, SdkBackend
from .runner import Runner, print_gate
from .state import Bench

HERE = os.path.dirname(os.path.abspath(__file__))


def _fmt_poses(seen: dict) -> str:
    if not seen:
        return "（一个都没看见）"
    return "，".join(f"{k} (x={v[0]:.2f}, y={v[1]:.2f}, z={v[2]:.2f})"
                    for k, v in seen.items())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="无真机，跑软件模拟")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--config", default=os.path.join(HERE, "config", "s0.json"))
    ap.add_argument("--run-dir", default=os.path.join(HERE, "runs"))
    ap.add_argument("--fail-rate", type=float, default=0.15, help="mock 抓空率")
    ap.add_argument("--perturb", type=int, default=0, help="每轮开始前随机打乱 n 个物体（连续性压测）")
    ap.add_argument("--occlude", default="",
                    help="mock 下在第 2 轮前把盒子放这个物体前面（如 block_red）："
                         "物体不动、但看不见了，验证遮挡记忆")
    ap.add_argument("--occlusion-pause", action="store_true",
                    help="开局先观测建立记忆，然后暂停等你把盒子放好再动手（真机演示用）")
    ap.add_argument("--no-occlusion-memory", action="store_true",
                    help="关掉遮挡记忆（对照组：遮挡的目标会被静默放弃）")
    args = ap.parse_args(argv)

    bench = Bench.load(args.config)
    backend = MockBackend(bench, grasp_fail_rate=args.fail_rate) if args.mock else SdkBackend()

    run_dir = args.run_dir
    os.makedirs(run_dir, exist_ok=True)
    runner = Runner(
        backend, bench,
        ckpt_path=os.path.join(run_dir, "state.json"),
        log_path=os.path.join(run_dir, "run.jsonl"),
        mock=args.mock,
        occlusion_memory=not args.no_occlusion_memory,
    )

    ready_count = 0
    for i in range(args.rounds):
        if args.mock and args.perturb:
            moved = backend.perturb(args.perturb)
            print(f"[扰动] 人为移动：{', '.join(moved)}")
        if args.occlusion_pause and i == 0:
            # 真机演示用的时序：先看一眼记下位置 → 人把盒子盖上 → 再动手。
            # 顺序不能反：盒子先盖上就没有"它原来在哪儿"这回事了。
            seen = runner.warm_memory()
            print(f"[记忆] 已记下：{_fmt_poses(seen)}")
            try:
                input("       现在把盒子放到积木前面（挡住它），放好后按回车继续…")
            except EOFError:
                print("       （非交互环境，直接继续）")
        if args.mock and args.occlude and i == 0:
            # 真实时序：先看见积木（建立记忆）→ 再盖盒子 → 才动手
            seen = runner.warm_memory()
            print(f"[记忆] 先看一眼，记下位置：{_fmt_poses(seen)}")
            info = backend.place_obstacle(args.occlude)
            if info.get("ok"):
                bx = info["obstacle"]
                print(f"[遮挡] 把盒子放在 {info['obj']} 前面："
                      f"x {bx.x0:.2f}–{bx.x1:.2f}、y {bx.y0:.2f}–{bx.y1:.2f}、"
                      f"高 {bx.z0:.2f}–{bx.z1:.2f} m")
                print(f"       {info['note']} —— 这一轮它应该照样被复位")
            else:
                print(f"[遮挡] 失败：{info.get('reason')}")
        res = runner.run_round()
        print_gate(res)
        ready_count += 1 if res["ready"] else 0

    print(f"连续 {args.rounds} 轮中，{ready_count} 轮通过验收。")
    print(f"日志：{os.path.join(run_dir, 'run.jsonl')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
