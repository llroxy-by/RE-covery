"""区域标定工具：鼠标点四个角把 A/B/C/D/TEMP 画出来，直接写进 config。

    python -m vision.tools.calibrate --camera 1
    python -m vision.tools.calibrate --url http://192.168.137.15:8080/video

操作：
    左键  依次点当前区域的四个角（顺序随意，但别交叉，要组成凸四边形）
    右键  撤销上一个点
    n     当前区域点满 4 个点后，进入下一个区域
    p     回到上一个区域重标
    r     清空当前区域重标
    s     保存到 config（会同时更新 calibration_id 与 resolution）
    q     不保存退出

保存后**旧基准作废**：必须回到主程序重新按 0 建 S0。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from ..sources import (Frame, MockSource, OpenCvSource, SourceError,
                       VideoSource, utc_now_iso)
from ..zones import ZoneSet, validate_layout_of

ZONE_ORDER = ("A", "B", "C", "D", "TEMP")


def rect_from(p1, p2) -> list[list[int]]:
    """两个对角点 → 轴对齐矩形的四个角（顺时针，左上开始）。"""
    x0, x1 = sorted((int(p1[0]), int(p2[0])))
    y0, y1 = sorted((int(p1[1]), int(p2[1])))
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


class ZoneCollector:
    """只管"点了几个点、够不够四个、算不算一个四边形"，不碰窗口，方便单测。"""

    def __init__(self, order=ZONE_ORDER, rect: bool = False) -> None:
        self.order = list(order)
        self.rect = bool(rect)
        self.need = 2 if self.rect else 4
        self.points: dict[str, list[tuple[int, int]]] = {z: [] for z in self.order}
        self.idx = 0

    @property
    def current(self) -> str:
        return self.order[self.idx]

    def add(self, x: float, y: float) -> str:
        pts = self.points[self.current]
        if len(pts) >= self.need:
            return "full"
        pts.append((int(round(x)), int(round(y))))
        return "ok"

    def undo(self) -> bool:
        pts = self.points[self.current]
        if not pts:
            return False
        pts.pop()
        return True

    def clear_current(self) -> None:
        self.points[self.current] = []

    def next_zone(self) -> bool:
        if not self.is_complete(self.current):
            return False
        if self.idx + 1 >= len(self.order):
            return False
        self.idx += 1
        return True

    def prev_zone(self) -> bool:
        if self.idx == 0:
            return False
        self.idx -= 1
        return True

    def is_complete(self, zone: str) -> bool:
        return len(self.points[zone]) == self.need

    def all_complete(self) -> bool:
        return all(self.is_complete(z) for z in self.order)

    def missing(self) -> list[str]:
        return [z for z in self.order if not self.is_complete(z)]

    def polygon_of(self, zone: str) -> list[list[int]] | None:
        pts = self.points[zone]
        if len(pts) != self.need:
            return None
        return rect_from(pts[0], pts[1]) if self.rect else [list(p) for p in pts]

    def snapshot(self) -> dict[str, list[list[int]]]:
        out = {}
        for z in self.order:
            poly = self.polygon_of(z)
            if poly is not None:
                out[z] = poly
        return out

    def areas(self) -> dict[str, float]:
        out = {}
        for z in self.order:
            poly = self.polygon_of(z)
            if poly is not None:
                out[z] = abs(cv2.contourArea(np.asarray(poly, np.float32)))
        return out


def check_zones(zones: dict, resolution, required=ZONE_ORDER) -> list[str]:
    """标定结果能不能用：数量、面积、重叠。返回问题列表（空 = 通过）。

    required 可以是五个区域的全集，也可以只标其中几个（台面窄的时候
    只标 A/B/C/TEMP 就能演示，不必硬塞五个）。
    """
    issues: list[str] = []
    missing = [z for z in required if z not in zones]
    if missing:
        issues.append("缺区域:" + ",".join(missing))
    for name, pts in zones.items():
        arr = np.asarray(pts, np.float32)
        area = abs(cv2.contourArea(arr))
        if area < 4000:
            issues.append(f"{name}:面积过小({area:.0f}px²)，框大一点")
        if len(np.unique(arr, axis=0)) < 4:
            issues.append(f"{name}:有重复顶点")
        w, h = resolution
        if arr[:, 0].min() < 0 or arr[:, 1].min() < 0 or \
                arr[:, 0].max() >= w or arr[:, 1].max() >= h:
            issues.append(f"{name}:顶点超出画面 {w}x{h}")
    if not missing:
        issues.extend(validate_layout_of(zones, margin_px=0))
    return issues


def apply_to_config(config: dict, zones: dict, resolution) -> dict:
    """把标定结果写进配置：区域、分辨率、新的 calibration_id。

    换标定必须换 id，否则旧 S0 会被当成同一套标定继续比 —— 那是"用旧尺子量新桌子"。
    """
    new = json.loads(json.dumps(config))  # 深拷贝，别改调用方的对象
    new["zones"] = {z: [list(map(int, p)) for p in pts] for z, pts in zones.items()}
    new["resolution"] = [int(resolution[0]), int(resolution[1])]
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = str(config.get("calibration_id", "desktop")).split("-")[0] or "desktop"
    new["calibration_id"] = f"{base}-{stamp}"
    return new


# ------------------------------------------------------------------ 画面
def _draw(frame, collector: ZoneCollector, resolution) -> np.ndarray:
    img = frame.copy()
    for name in collector.order:
        poly = collector.polygon_of(name)
        pts = collector.points[name]
        if poly is not None:
            cv2.polylines(img, [np.asarray(poly, np.int32)], True, (0, 200, 255), 2)
            cx = int(np.mean([p[0] for p in poly]))
            cy = int(np.mean([p[1] for p in poly]))
            cv2.putText(img, name, (cx - 8, cy + 8), cv2.FONT_HERSHEY_SIMPLEX,
                        0.9, (0, 200, 255), 2, cv2.LINE_AA)
        else:
            # 还在点：矩形模式画虚线预览框，四点模式画已点的折线
            if collector.rect and len(pts) == 1:
                cv2.circle(img, pts[0], 6, (0, 0, 255), -1)
                cv2.putText(img, "2nd corner...", (pts[0][0] + 10, pts[0][1] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)
            else:
                if len(pts) >= 2:
                    cv2.polylines(img, [np.asarray(pts, np.int32)], False, (0, 200, 255), 2)
                for p in pts:
                    cv2.circle(img, p, 5, (0, 0, 255), -1)

    cur = collector.current
    left = collector.need - len(collector.points[cur])
    mode = "rect: 2 corners" if collector.rect else "quad: 4 corners"
    hud = [
        f"CALIBRATE  zone={cur}  {mode}  need {left} more click(s)",
        f"frame={resolution[0]}x{resolution[1]}",
        "L-click=corner  R-click=undo  n=next  p=prev  r=reset  s=save  q=quit",
    ]
    cv2.rectangle(img, (0, 0), (resolution[0], 74), (25, 25, 25), -1)
    for i, line in enumerate(hud):
        cv2.putText(img, line, (12, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (240, 240, 240), 1, cv2.LINE_AA)
    return img


def run_gui(source, config: dict, config_path: Path, order=ZONE_ORDER,
            rect: bool = False) -> int:
    collector = ZoneCollector(order, rect=rect)
    win = "RE-covery calibrate"

    def on_mouse(event, x, y, flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            r = collector.add(x, y)
            if r == "full":
                print(f"[CAL] {collector.current} 点够了，按 n 进入下一个区域")
        elif event == cv2.EVENT_RBUTTONDOWN:
            collector.undo()

    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win, on_mouse)
    try:
        while True:
            frame = source.read()
            resolution = (frame.width, frame.height)
            cv2.imshow(win, _draw(frame.image, collector, resolution))
            key = cv2.waitKey(1) & 0xFF
            if key == 0xFF or key == -1:
                continue
            ch = chr(key)
            if ch == "q":
                print("[CAL] 未保存退出")
                return 0
            if ch == "n":
                if not collector.next_zone():
                    print(f"[CAL] {collector.current} 还没点够 {collector.need} 个点，或已是最后一个区域")
                else:
                    print(f"[CAL] 当前区域 -> {collector.current}")
            elif ch == "p":
                if collector.prev_zone():
                    print(f"[CAL] 当前区域 -> {collector.current}（可 r 清空重标）")
            elif ch == "r":
                collector.clear_current()
                print(f"[CAL] 已清空 {collector.current}")
            elif ch == "s":
                zones = collector.snapshot()
                if collector.missing():
                    print(f"[CAL] 还不能保存，缺：{', '.join(collector.missing())}")
                    continue
                issues = check_zones(zones, resolution, required=order)
                if issues:
                    print("[CAL] 标定不合格，未保存：")
                    for i in issues:
                        print(f"      - {i}")
                    continue
                new_cfg = apply_to_config(config, zones, resolution)
                tmp = config_path.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(new_cfg, f, ensure_ascii=False, indent=2)
                tmp.replace(config_path)
                print(f"[CAL] 已写入 {config_path}")
                print(f"[CAL] 新 calibration_id = {new_cfg['calibration_id']}  "
                      f"resolution = {new_cfg['resolution']}")
                print("[CAL] 旧基准已失效：回主程序重新按 0 建 S0")
                return 0
    finally:
        source.close()
        cv2.destroyAllWindows()


class StillImageSource(VideoSource):
    """拿一张静态图片当取像源。

    为什么需要：标定要在窗口里点鼠标，但 Mac 上跑命令行的是终端/宿主 App，
    拿摄像头权限得单独授权，现场经常卡在这一步。改成「实时页面点一下存图 →
    拿这张图标分区」就绕开了权限，标出来的坐标系也和实时帧完全一致
    （存的就是服务端 fit 过的 1280×720）。
    """

    mode = "real"

    def __init__(self, path: str, resolution) -> None:
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise SourceError(f"读不出这张图：{path}")
        tw, th = int(resolution[0]), int(resolution[1])
        if (img.shape[1], img.shape[0]) != (tw, th):
            img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        self.source = f"still:{Path(path).name}"
        self._img = img
        self._n = 0

    def read(self) -> Frame:
        self._n += 1
        return Frame(image=self._img, captured_at=utc_now_iso(),
                     source=self.source, mode=self.mode, index=self._n)

    def close(self) -> None:
        return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="鼠标标定 A/B/C/D/TEMP 五个区域")
    p.add_argument("--camera", type=int, default=1, help="摄像头编号（默认 1）")
    p.add_argument("--url", default=None, help="手机 IP 摄像头视频流")
    p.add_argument("--image", default=None,
                   help="拿一张存好的图片标定（Mac 上免摄像头权限）："
                        "用实时页面「存一张」按钮保存的那张")
    p.add_argument("--config", default="vision/config/desktop.json")
    p.add_argument("--mode", choices=["real", "mock"], default="real",
                   help="mock 可在没有相机时练手（画面里已有五个框，点同样的位置即可）")
    p.add_argument("--rect", action="store_true",
                   help="两点矩形模式：点两个对角即可（机位正对桌面时用，比四点快一倍）")
    p.add_argument("--zones", default="A,B,C,D,TEMP",
                   help="要标哪些区域，逗号分隔。台面窄时可只标 A,B,C,TEMP")
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
    args = parse_args(argv)
    order = tuple(z.strip().upper() for z in args.zones.split(",") if z.strip())
    bad = [z for z in order if z not in ZONE_ORDER]
    if bad or not order:
        print(f"[FATAL] --zones 里有非法区域 {bad}，只允许 {', '.join(ZONE_ORDER)}",
              file=sys.stderr)
        return 2
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    resolution = tuple(config.get("resolution", [1280, 720]))
    try:
        if args.image:
            source = StillImageSource(args.image, resolution)
        elif args.mode == "mock":
            source = MockSource(config["zones"], resolution, "ok")
        else:
            source = OpenCvSource(args.camera, resolution, url=args.url)
    except SourceError as exc:
        print(f"[FATAL] 取像失败：{exc}", file=sys.stderr)
        return 2
    return run_gui(source, config, config_path, order=order, rect=args.rect)


if __name__ == "__main__":
    raise SystemExit(main())
