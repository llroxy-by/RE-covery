"""RE-covery 视觉模块命令行入口。

用法（仓库根目录）：
    python -m vision.main --camera 0 --config vision/config/desktop.json --mode real

按键：
    0  建立 S0 基准（当前画面）
    1  建立 S1 并与 S0 比较
    m  切换 mock 场景（仅 --mode mock）
    q  退出
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

from . import diff as diff_mod
from .detector import Detector, StabilityTracker
from .snapshot import (
    PROJECT_ROOT,
    SnapshotStore,
    baseline_blockers,
    build_scene,
    object_record,
)
from .sources import MOCK_SCENARIOS, MockSource, SourceError, create_source
from .zones import UNKNOWN, ZoneSet


def _utf8_stdout() -> None:
    """行缓冲 + UTF-8：管道里也能看到实时日志顺序（错误不会跑到正常日志前面）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class VisionApp:
    def __init__(self, args) -> None:
        self.args = args
        self.config = load_config(args.config)
        self.zones = ZoneSet(self.config["zones"], self.config.get("zone_margin_px", 14))
        self.detector = Detector(self.config)
        self.tracker = StabilityTracker(
            self.config.get("stability_frames", 5),
            self.config.get("max_center_jitter_px", 18),
        )
        self.min_conf = float(args.min_conf)
        self.output_dir = Path(args.output) if args.output else PROJECT_ROOT / "vision" / "records"
        self.store = SnapshotStore(self.output_dir)
        self.rejected_store = SnapshotStore(self.output_dir / "rejected")
        self.layout_issues = self.zones.validate_layout()
        self.baseline: dict | None = None
        self.scene: dict | None = None

    # ------------------------------------------------------------ 观测组装
    def expected_ids(self) -> list[str]:
        """这一套配置下，画面里"应该有几件、分别叫什么"。

        按字母认身份时 object_id 是 block_A，已经不是配置里的 block_red_01。
        如果这里还拿颜色 ID 去对，就会每一帧都报"看不到 block_red_01"，
        哪怕那块积木好端端摆在画面正中。

        纯模型模式（reader=vlm）且模板库还是空的，"应该有几件"根本无从得知：
        这时返回空表，而不是退回颜色 ID —— 拿一份没人遵守的颜色清单去对，
        只会每帧报一堆假缺件，把真正的问题淹掉。
        """
        bank = getattr(self.detector, "letter_bank", None)
        if bank is not None and getattr(bank, "tiles", None):
            return [f"block_{l}" for l in bank.letters()]
        vlm = getattr(self.detector, "vlm", None)
        if vlm is not None and getattr(vlm, "available", False) \
                and getattr(self.detector, "label_primary", False):
            return []
        return [spec.get("object_id") or f"block_{c}_01"
                for c, spec in self.config.get("colors", {}).items()]

    def observe(self, frame) -> tuple[list[dict], bool, list[str]]:
        dets, issues = self.detector.detect(frame.image)
        issues = list(issues)

        raw_pairs = []
        for d in dets:
            zone, zone_reasons, dist = self.zones.assign(d.center_px)
            raw_pairs.append((d, zone, zone_reasons, dist))

        stable = self.tracker.update(
            [(d.object_id, zone, d.center_px) for d, zone, _, _ in raw_pairs]
        )

        objects: list[dict] = []
        seen: set[str] = set()
        for d, zone, zone_reasons, _dist in raw_pairs:
            seen.add(d.object_id)
            reasons = list(d.reasons) + list(zone_reasons)
            conf = d.confidence
            if zone_reasons:
                conf -= self.config.get("confidence", {}).get("zone_margin_penalty", 0.15)
            if not stable.get(d.object_id, False):
                reasons.append("zone_unstable")
                conf -= self.config.get("confidence", {}).get("jitter_penalty", 0.25)
                zone = UNKNOWN
            conf = max(0.0, min(1.0, conf))

            if d.is_unknown:
                visibility = "visible"
            elif "possibly_occluded" in d.reasons:
                visibility = "occluded"
            else:
                visibility = "visible"

            objects.append(object_record(
                object_id=d.object_id,
                zone=zone,
                visibility=visibility,
                confidence=conf,
                center_px=d.center_px,
                reasons=reasons,
            ))

        # 配置里应该有、但画面里没检出的目标：保留 ID，但什么都不断言
        for oid in self.expected_ids():
            if oid in seen:
                continue
            objects.append(object_record(
                object_id=oid,
                zone=UNKNOWN,
                visibility="unknown",
                confidence=0.0,
                center_px=None,
                reasons=["not_detected"],
            ))
            issues.append(f"{oid}:not_detected")

        objects.sort(key=lambda o: o["object_id"])

        calibration_valid = (
            not self.layout_issues
            and (frame.width, frame.height) == tuple(self.config.get("resolution", [0, 0]))
        )
        if self.layout_issues:
            issues.extend(self.layout_issues)
        if not calibration_valid and not self.layout_issues:
            issues.append(
                f"resolution_mismatch:frame={frame.width}x{frame.height},"
                f"config={self.config.get('resolution')}"
            )

        # label_pending 是"视觉模型还在读"的临时状态，不是画面缺陷。拿它去否掉
        # quality_ok，页面会在换积木后的头一两秒闪红 —— 而那两秒本来就正常。
        blocking = [i for i in issues if not i.startswith("label_pending:")]
        quality_ok = (
            calibration_valid
            and not blocking
            and all(
                o["visibility"] == "visible"
                and o["zone"] != UNKNOWN
                and o["confidence"] >= self.min_conf
                and not o["reasons"]
                for o in objects
            )
        )
        self.scene = build_scene(
            frame=frame,
            objects=objects,
            quality_ok=quality_ok,
            issues=issues,
            calibration_id=self.config.get("calibration_id", "unknown"),
            calibration_valid=calibration_valid,
        )
        return objects, quality_ok, issues

    # ------------------------------------------------------------ 标注画面
    @staticmethod
    def _label(img, text: str, org, fg=(255, 255, 255), bg=(0, 0, 0),
               scale: float = 0.46, thickness: int = 1) -> None:
        """带底色的文字：白字压在浅色桌面上也能看清，比描边干净。"""
        (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        x, y = int(org[0]), int(org[1])
        cv2.rectangle(img, (x - 3, y - th - 4), (x + tw + 3, y + base), bg, -1)
        cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, fg, thickness, cv2.LINE_AA)

    def annotate(self, frame, objects: list[dict], issues: list[str]) -> np.ndarray:
        img = frame.image.copy()
        for name in self.zones.names:
            poly = self.zones.polygon(name).astype(np.int32)
            cv2.polylines(img, [poly], True, (0, 200, 255), 2)
            x, y = int(poly[:, 0].min()), int(poly[:, 1].min())
            self._label(img, name, (x + 4, y - 8), fg=(0, 200, 255), bg=(20, 20, 20), scale=0.55)

        missing: list[str] = []
        for o in objects:
            c = o["center_px"]
            if c is None:  # 没检出的目标：单独列在左下角，绝不在图上画假位置
                missing.append(f'{o["object_id"]} not_observed')
                continue
            cx, cy = int(c[0]), int(c[1])
            ok = o["zone"] != UNKNOWN and o["visibility"] == "visible"
            colour = (0, 220, 0) if ok else (0, 140, 255)
            cv2.circle(img, (cx, cy), 6, colour, -1)
            cv2.circle(img, (cx, cy), 13, colour, 1)
            label = f'{o["object_id"]} {o["zone"]} {o["confidence"]:.2f}'
            self._label(img, label, (cx + 18, cy - 10), fg=(255, 255, 255),
                        bg=(0, 90, 0) if ok else (0, 70, 130))
            if o["reasons"]:
                self._label(img, ",".join(o["reasons"])[:44], (cx + 18, cy + 20),
                            fg=(255, 255, 255), bg=(0, 70, 130), scale=0.42)

        for i, text in enumerate(missing):
            self._label(img, text, (14, frame.height - 14 - 22 * (len(missing) - 1 - i)),
                        fg=(255, 255, 255), bg=(110, 40, 40))

        hud = [
            f"mode={frame.mode}  source={frame.source}",
            f"frame={frame.width}x{frame.height}  t={frame.captured_at}",
            f"calib={self.config.get('calibration_id')}  objects={len(objects)}  issues={len(issues)}",
        ]
        if isinstance(self._source, MockSource):
            hud.append(f"mock_scenario={self._source.scenario}  (m 切换)")
        hud.append("0=S0  1=S1  d=tune hints  m=mock scene  q=quit")

        box_h = 14 + 20 * len(hud)
        overlay = img.copy()
        cv2.rectangle(overlay, (0, 0), (560, box_h), (25, 25, 25), -1)
        cv2.addWeighted(overlay, 0.62, img, 0.38, 0, img)
        for i, line in enumerate(hud):
            cv2.putText(img, line, (12, 22 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (240, 240, 240), 1, cv2.LINE_AA)
        return img

    # ------------------------------------------------------------ 调参辅助
    def mask_mosaic(self, frame) -> np.ndarray:
        """把每种颜色的掩膜并排显示：掩膜全黑 = HSV 阈值不对，有白块没框 = 面积/形状过滤太狠。"""
        tiles: list[np.ndarray] = []
        for color in self.config.get("colors", {}):
            m = self.detector.last_masks.get(color)
            if m is None:
                m = np.zeros(frame.image.shape[:2], np.uint8)
            tiles.append(m)
        if self.detector.last_residual is not None:
            tiles.append(self.detector.last_residual)
        names = list(self.config.get("colors", {})) + (["unknown"] if self.detector.last_residual is not None else [])

        panel_h = 200
        panels = []
        for mask, name in zip(tiles, names):
            scale = panel_h / mask.shape[0]
            panel = cv2.resize(mask, (int(mask.shape[1] * scale), panel_h),
                               interpolation=cv2.INTER_NEAREST)
            panel = cv2.cvtColor(panel, cv2.COLOR_GRAY2BGR)
            cv2.putText(panel, name, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 255, 0), 2, cv2.LINE_AA)
            panels.append(panel)
        return np.hstack(panels)

    def tuning_hints(self, objects: list[dict]) -> list[str]:
        """把画面上的症状翻译成"改哪个参数"。照着改，一次只改一个。"""
        hints: list[str] = []
        by_id = {o["object_id"]: o for o in objects}
        for color, spec in self.config.get("colors", {}).items():
            oid = spec.get("object_id") or f"block_{color}_01"
            obj = by_id.get(oid)
            if obj is None:
                continue
            mask = self.detector.last_masks.get(color)
            px = int(np.count_nonzero(mask)) if mask is not None else 0
            reasons = obj["reasons"]
            if obj["visibility"] == "unknown":
                if px == 0:
                    hints.append(f"{oid}: 掩膜 0 像素 → 先确认它真的露出在画面里且没被盖住；"
                                 f"若确实露出仍 0 像素，再降饱和度下限"
                                 f"({spec['ranges'][0][0][1]}→50)、亮度下限"
                                 f"({spec['ranges'][0][0][2]}→40)，或扩大色相范围")
                else:
                    hints.append(f"{oid}: 掩膜有 {px} 像素但没成形 → min_area_px "
                                 f"({self.config.get('min_area_px')}) 太大，或物体太小/太远")
            elif any(r.startswith("zone_boundary_ambiguous") for r in reasons):
                hints.append(f"{oid}: 离区域边界太近 → 把物体挪到区域中间，"
                             f"或把 zone_margin_px ({self.config.get('zone_margin_px')}) 调小到 8")
            elif "zone_unstable" in reasons:
                hints.append(f"{oid}: 区域在跳 → 人/手别晃、相机别动；"
                             f"确实稳定了还报，就把 stability_frames 调小或 max_center_jitter_px 调大")
            elif "multi_candidate" in reasons:
                hints.append(f"{oid}: 同色出现第二块 → 提高饱和度下限收窄颜色（{spec['ranges'][0][0][1]}→140），"
                             f"或把同色的东西拿开")
            elif "possibly_occluded" in reasons:
                hints.append(f"{oid}: 轮廓填充率低 → 被挡住了一部分，移开遮挡物")
            elif "too_dark" in reasons:
                hints.append(f"{oid}: 太暗 → 补光，或把亮度下限从 "
                             f"{spec['ranges'][0][0][2]} 降到 40")
            elif "overexposed" in reasons:
                hints.append(f"{oid}: 过曝 → 避开反光/直射光，或提高亮度上限")
            elif obj["zone"] == UNKNOWN:
                hints.append(f"{oid}: 落在五个区域之外 → 挪进区域，或把区域框大")
        return hints

    # ------------------------------------------------------------ 快照动作
    def capture_baseline(self, frame, objects, issues) -> None:
        scene = self.scene
        blockers = baseline_blockers(scene, self.min_conf)
        ann = self.annotate(frame, objects, issues)
        if blockers:
            saved = self.rejected_store.save(scene, frame.image, ann)
            print("[S0] 拒绝建立基准：")
            for b in blockers:
                print(f"     - {b}")
            hints = self.tuning_hints(objects)
            if hints:
                print("[S0] 怎么改（按 d 随时重看）:")
                for h in hints:
                    print(f"     - {h}")
            print(f"[S0] 证据已存入 rejected/：{saved['json']}")
            return
        saved = self.store.save(scene, frame.image, ann)
        self.baseline = scene
        print(f"[S0] 基准已建立 id={scene['snapshot_id'][:8]}  "
              f"objects={len(scene['objects'])}  quality_ok=True")
        print(f"     json={saved['json']}")
        print(f"     raw ={saved['raw']}")
        if self.args.mock_cycle and isinstance(self._source, MockSource):
            print(f"[S0] --mock-cycle 切到 mock 场景 {self._source.next_scenario()}")
            self.tracker.reset()

    def capture_current(self, frame, objects, issues) -> None:
        if self.baseline is None:
            print("[S1] 还没有 S0 基准，先按 0")
            return
        ann = self.annotate(frame, objects, issues)
        saved = self.store.save(self.scene, frame.image, ann)
        result = diff_mod.compare(self.baseline, self.scene, min_conf=self.min_conf,
                                  check_images=not self.args.skip_image_check)
        path = diff_mod.save_differences(result, self.output_dir)

        print(f"[S1] id={self.scene['snapshot_id'][:8]}  json={saved['json']}")
        print(f"[DIFF] status={result['status']}  verdict={diff_mod.gate_verdict(result)}")
        for d in result["differences"]:
            print(f"       {d['object_id']:<18} {d['status']:<10} "
                  f"{d['from_zone']:>6} -> {d['to_zone']:<6} "
                  f"conf={d['confidence']:.2f} {','.join(d['reasons'])}")
        if result["issues"]:
            print(f"[DIFF] issues: {', '.join(result['issues'])}")
        print(f"[DIFF] saved={path}")

    # ------------------------------------------------------------ 主循环
    def run(self) -> int:
        args = self.args
        try:
            self._source = create_source(args.mode, args.camera, self.config,
                                         allow_frozen=args.allow_frozen, url=args.url)
        except SourceError as exc:
            print(f"[FATAL] 取像失败：{exc}", file=sys.stderr)
            return 2
        if isinstance(self._source, MockSource) and args.mock_scenario:
            self._source.set_scenario(args.mock_scenario)

        auto = [c for c in (args.auto or "").replace(",", "") if c in "01mqd"]
        last_key_at = 0
        print(f"[RUN] mode={args.mode} source={self._source.source} "
              f"zones={','.join(self.zones.names)} "
              f"colors={','.join(self.config.get('colors', {}))}")
        print("[RUN] 0=建立 S0 基准  1=建立 S1 并比较  d=调参建议  m=切 mock 场景  q=退出")
        if self.layout_issues:
            print(f"[WARN] 区域配置存在重叠：{self.layout_issues}，标定视为无效")

        frames = 0
        try:
            while True:
                frame = self._source.read()
                frames += 1
                if frames == 1:
                    want = tuple(self.config.get("resolution", []))
                    if want and (frame.width, frame.height) != want:
                        print(f"[HINT] 画面 {frame.width}x{frame.height} 与配置 "
                              f"resolution={list(want)} 不符。若长期用这个相机，"
                              f"请把 config 里的 resolution 改成实际值并重新标定五个区域，"
                              f"否则 S0 会被拒绝（calibration_invalid）。")
                objects, quality_ok, issues = self.observe(frame)
                ann = self.annotate(frame, objects, issues)

                if not args.no_preview:
                    cv2.imshow("RE-covery vision", ann)
                    if args.debug_mask:
                        cv2.imshow("RE-covery masks (white = 被 HSV 选中)", self.mask_mosaic(frame))

                key = None
                if auto:
                    if frames > 6 and frames - last_key_at >= args.auto_gap:
                        key = auto.pop(0)
                        last_key_at = frames
                elif args.no_preview:
                    if frames % 30 == 0:  # 无窗口时用日志证明画面在走（时间戳必须递增）
                        print(f"[LIVE] frame={frames} time={frame.captured_at} "
                              f"objects={len(objects)} quality_ok={quality_ok}")
                else:
                    k = cv2.waitKey(1) & 0xFF
                    if k == 0xFF or k == -1:
                        continue
                    key = chr(k)

                if key == "0":
                    self.capture_baseline(frame, objects, issues)
                elif key == "1":
                    self.capture_current(frame, objects, issues)
                elif key == "m":
                    if isinstance(self._source, MockSource):
                        print(f"[MOCK] 场景 -> {self._source.next_scenario()}")
                        self.tracker.reset()
                    else:
                        print("[MOCK] 当前是真实相机，m 键无效")
                elif key == "d":
                    hints = self.tuning_hints(objects)
                    if hints:
                        print("[TUNE] 按症状改参数（一次只改一个，改完看掩膜窗）:")
                        for h in hints:
                            print(f"       - {h}")
                    else:
                        print("[TUNE] 当前没有需要调整的地方")
                if key == "q":
                    break

                if args.max_frames and frames >= args.max_frames:
                    print(f"[RUN] 到达 --max-frames {args.max_frames}，退出")
                    break
        except SourceError as exc:
            print(f"[FATAL] 取像中断：{exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n[RUN] 用户中断")
        finally:
            self._source.close()
            if not args.no_preview:
                cv2.destroyAllWindows()
        print(f"[RUN] 结束，共处理 {frames} 帧")
        return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="RE-covery 视觉模块（P0 桌面固定机位）")
    p.add_argument("--camera", type=int, default=0, help="摄像头编号，内置/外接通常是 0/1/2")
    p.add_argument("--url", default=None,
                   help="手机 IP 摄像头视频流，如 http://192.168.1.7:8080/video "
                        "（给了 --url 就忽略 --camera；不用装任何 Windows 驱动）")
    p.add_argument("--config", default="vision/config/desktop.json", help="区域与 HSV 配置")
    p.add_argument("--mode", choices=["real", "mock"], default="real",
                   help="real=真实摄像头；mock=合成画面（必须显式选择）")
    p.add_argument("--output", default=None, help="快照输出目录，默认 vision/records")
    p.add_argument("--min-conf", type=float, default=0.8, help="下结论所需的最低置信度")
    p.add_argument("--no-preview", action="store_true", help="不开窗口（配合 --auto 做无头抓取）")
    p.add_argument("--auto", default=None, help="自动按键序列，例如 0,1,q（无头演示/回归用）")
    p.add_argument("--auto-gap", type=int, default=6, help="自动序列每个按键之间间隔的帧数")
    p.add_argument("--max-frames", type=int, default=0, help="处理多少帧后自动退出（0=不限制）")
    p.add_argument("--mock-scenario", default=None, choices=list(MOCK_SCENARIOS),
                   help="mock 起始场景")
    p.add_argument("--mock-cycle", action="store_true",
                   help="mock 模式下建立 S0 后自动切到下一个场景，方便演示差异")
    p.add_argument("--skip-image-check", action="store_true",
                   help="比较时不校验图像文件是否存在（仅用于纯逻辑测试）")
    p.add_argument("--debug-mask", action="store_true",
                   help="额外开一个掩膜窗口（白=被 HSV 选中），调参必开")
    p.add_argument("--allow-frozen", action="store_true",
                   help="关闭冻结画面守卫（只在调试虚拟摄像头/合成设备时用，演示请勿开启）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    _utf8_stdout()
    args = parse_args(argv)
    return VisionApp(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
