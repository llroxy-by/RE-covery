"""摄像头体检工具：找出哪个编号/地址是真的实时画面。

    python -m vision.tools.probe_cameras                 # 体检 0..4 号设备
    python -m vision.tools.probe_cameras --url http://192.168.1.7:8080/video

只统计、不落盘，不会保存任何画面（避免留下人脸/室内照片）。
判据：
- 时序标准差 ≈ 0 → 每帧一模一样 → 静止画面（虚拟摄像头/冻结），不能用来做差异检测
- 平均亮度 < 5   → 全黑（镜头被挡/隐私开关）
- 时序标准差 > 0.5 → 画面在动 → 可用
"""

from __future__ import annotations

import argparse
import sys

import cv2
import numpy as np

FRAMES = 10


def probe_camera(index: int) -> dict | None:
    cap = cv2.VideoCapture(index)
    try:
        if not cap.isOpened():
            return None
        got = []
        for _ in range(FRAMES):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                got.append(frame)
    finally:
        cap.release()
    if len(got) < FRAMES:
        return {"kind": f"camera {index}", "opened": True, "frames": len(got),
                "size": "-", "mean": float("nan"), "tstd": float("nan"),
                "verdict": f"读帧不足（{len(got)}/{FRAMES}）"}
    return _summarise(f"camera {index}", got)


def probe_url(url: str) -> dict | None:
    cap = cv2.VideoCapture(url)
    try:
        if not cap.isOpened():
            return None
        got = []
        for _ in range(FRAMES):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                got.append(frame)
    finally:
        cap.release()
    if len(got) < FRAMES:
        return {"kind": "url", "opened": True, "frames": len(got),
                "size": "-", "mean": float("nan"), "tstd": float("nan"),
                "verdict": f"读帧不足（{len(got)}/{FRAMES}）"}
    return _summarise("url", got)


def _summarise(kind: str, frames: list) -> dict:
    arr = np.stack([f.astype(np.int16) for f in frames])
    tstd = float(arr.std(axis=0).mean())
    last = frames[-1]
    mean = float(last.mean())
    if tstd > 0.5 and mean >= 5:
        verdict = "OK 实时画面"
    elif mean < 5:
        verdict = "全黑（镜头被挡/隐私开关）"
    elif tstd <= 0.5:
        verdict = "静止画面（虚拟设备/冻结）不可用"
    else:
        verdict = "可疑"
    return {"kind": kind, "opened": True, "frames": len(frames),
            "size": f"{last.shape[1]}x{last.shape[0]}", "mean": mean,
            "tstd": tstd, "verdict": verdict}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    p = argparse.ArgumentParser(description="摄像头/视频流体检（不保存画面）")
    p.add_argument("--max-index", type=int, default=4, help="最大探测编号，默认 4")
    p.add_argument("--url", default=None, help="额外体检一个手机推流地址")
    args = p.parse_args()

    rows = []
    for idx in range(args.max_index + 1):
        r = probe_camera(idx)
        if r is not None:
            rows.append(r)
    if args.url:
        r = probe_url(args.url)
        rows.append(r or {"kind": "url", "opened": False, "frames": 0, "size": "-",
                          "mean": float("nan"), "tstd": float("nan"),
                          "verdict": "打不开（检查 App 推流/WiFi/端口）"})

    if not rows:
        print("没有找到任何可用设备。检查 Windows 隐私设置里的摄像头权限。")
        return 1

    print(f"{'设备':<12}{'分辨率':<12}{'平均亮度':>10}{'时序std':>10}   判断")
    print("-" * 64)
    for r in rows:
        mean = "-" if np.isnan(r["mean"]) else f"{r['mean']:.1f}"
        tstd = "-" if np.isnan(r["tstd"]) else f"{r['tstd']:.2f}"
        print(f"{r['kind']:<12}{r['size']:<12}{mean:>10}{tstd:>10}   {r['verdict']}")

    ok = [r for r in rows if r["verdict"].startswith("OK")]
    print("-" * 64)
    if ok:
        print(f"可用：{', '.join(r['kind'] for r in ok)}")
        print("下一步：python -m vision.main --camera <编号> --mode real   "
              "（或 --url <手机地址>）")
    else:
        print("没有实时画面。先用系统「相机」App 确认摄像头本身是否工作，"
              "再检查手机 App 是否在推流、手机与电脑是否在同一 WiFi。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
