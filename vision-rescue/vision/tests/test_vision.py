"""视觉模块的离线回归测试（不需要摄像头，mock 合成画面即可）。

跑法（仓库根目录）：
    python -m unittest vision.tests.test_vision -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

try:
    import cv2  # 只用于把 HSV 转成 BGR 构造测试画面
except ImportError:  # 与 detector 的降级策略保持一致
    cv2 = None

from vision import diff as diff_mod
from vision import snapshot as snap
from vision.detector import Detector, StabilityTracker
from vision.sources import (Frame, FrozenFrameGuard, MockSource, SourceError,
                           _strip_credentials, fit_to_resolution, utc_now_iso)
from vision.zones import ZoneSet

CONFIG = json.loads((Path("vision/config/desktop.json")).read_text(encoding="utf-8"))
RES = tuple(CONFIG["resolution"])

# 单元测试不该受"现场登记出来的字母模板"影响：desktop.json 的 label 段指向
# vision/config/letters.npz，那是真人把积木摆到镜头前一个个敲字母登记出来的。
# 这台机器上有没有这个文件、里面是 A/B 还是别的，都不能决定测试结果。
# label 的专项测试见 TestLetterLabels，它们自己构造内存库。
CONFIG["label"] = {"enabled": False}

# 测试夹具要三色，生产配置按实物裁剪（桌上只有两块）。
# 这里补一条 blue 给测试用 —— 别拿生产配置当测试数据，否则现场少放一块积木就会
# 让一堆与业务无关的用例连带挂掉。
TEST_BLUE = {"object_id": "block_blue_01",
             "ranges": [[[95, 100, 60], [130, 255, 255]]]}
if "blue" not in CONFIG["colors"]:
    CONFIG["colors"]["blue"] = TEST_BLUE

ZONES_TWO = {
    "A": [[100, 100], [400, 100], [400, 300], [100, 300]],
    "B": [[500, 100], [800, 100], [800, 300], [500, 300]],
}


def fake_frame(mode: str = "mock", source: str = "test:fake") -> Frame:
    img = np.zeros((RES[1], RES[0], 3), np.uint8)
    return Frame(image=img, captured_at=utc_now_iso(), source=source, mode=mode, index=1)


def obj(oid, zone, vis="visible", conf=0.95, center=(300.0, 200.0), reasons=()):
    return {"object_id": oid, "zone": zone, "visibility": vis, "confidence": conf,
            "center_px": None if center is None else list(center), "reasons": list(reasons)}


def scene(sid, objects, mode="mock", source="test:fake", calib="desktop-v1",
          valid=True, quality=True, issues=(), res=RES):
    return {
        "schema_version": "1.0", "snapshot_id": sid, "timestamp": utc_now_iso(),
        "mode": mode, "source": source,
        "image": {"reference": f"fixtures/{sid}.jpg", "width": res[0], "height": res[1]},
        "calibration": {"id": calib, "valid": valid, "coordinate_frame": "image_pixels"},
        "quality_ok": quality, "issues": list(issues), "objects": objects,
    }


# ---------------------------------------------------------------- 区域
class TestZones(unittest.TestCase):
    def setUp(self):
        self.zs = ZoneSet(ZONES_TWO, margin_px=14)

    def test_center_inside(self):
        self.assertEqual(self.zs.assign((250, 200))[0], "A")
        self.assertEqual(self.zs.assign((650, 200))[0], "B")

    def test_outside_all(self):
        zone, reasons, _ = self.zs.assign((50, 50))
        self.assertEqual(zone, "unknown")
        self.assertIn("outside_all_zones", reasons)

    def test_boundary_is_unknown_not_guessed(self):
        zone, reasons, dist = self.zs.assign((104, 200))
        self.assertEqual(zone, "unknown")
        self.assertTrue(reasons[0].startswith("zone_boundary_ambiguous:A"))
        self.assertLess(dist, 14)

    def test_overlap_config_is_unknown(self):
        zs = ZoneSet({"A": [[0, 0], [100, 0], [100, 100], [0, 100]],
                      "B": [[50, 50], [150, 50], [150, 150], [50, 150]]})
        zone, reasons, _ = zs.assign((75, 75))
        self.assertEqual(zone, "unknown")
        self.assertIn("zone_overlap:A,B", reasons)

    def test_layout_selfcheck_detects_overlap(self):
        zs = ZoneSet({"A": [[0, 0], [100, 0], [100, 100], [0, 100]],
                      "B": [[50, 50], [150, 50], [150, 150], [50, 150]]})
        self.assertTrue(zs.validate_layout())
        self.assertEqual(ZoneSet(ZONES_TWO).validate_layout(), [])

    def test_no_center(self):
        self.assertEqual(self.zs.assign(None)[0], "unknown")


# ---------------------------------------------------------------- 检测
class TestDetector(unittest.TestCase):
    def setUp(self):
        # 这组测试验的是颜色/区域/形状，不验字母。必须显式关掉标签识别：
        # 生产配置里的字母库是现场登记出来的，机器上有没有这个文件、里面
        # 有几个字母，不该决定测试的结果。
        cfg = {k: v for k, v in CONFIG.items() if k != "label"}
        cfg["label"] = {"enabled": False}
        self.det = Detector(cfg)

    def _scene_img(self, blocks):
        img = np.full((RES[1], RES[0], 3), 105, np.uint8)
        for zone, bgr in blocks:
            poly = np.asarray(CONFIG["zones"][zone])
            cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
            img[cy - 48:cy + 48, cx - 48:cx + 48] = bgr
        return img

    def test_three_colors_found(self):
        img = self._scene_img([("A", (0, 0, 220)), ("B", (0, 180, 0)), ("C", (220, 80, 0))])
        dets, issues = self.det.detect(img)
        ids = {d.object_id for d in dets}
        self.assertEqual(ids, {"block_red_01", "block_green_01", "block_blue_01"})
        self.assertEqual(issues, [])
        for d in dets:
            self.assertGreaterEqual(d.confidence, 0.9)

    def test_missing_color_not_detected(self):
        img = self._scene_img([("A", (0, 0, 220))])
        dets, _ = self.det.detect(img)
        self.assertEqual({d.object_id for d in dets}, {"block_red_01"})

    def test_unknown_color_gets_temp_id(self):
        img = self._scene_img([("A", (0, 0, 220))])
        poly = np.asarray(CONFIG["zones"]["TEMP"])
        cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
        # 橙色：既不是配置里的已知色，也不落在 background 区间里。
        # 原来用紫色 (200,0,200) 是错的 —— 那张真实桌面就是紫红的（H 中位 123），
        # 紫色会被 background 扣掉。测试若依赖"桌面恰好不是紫"就失去意义了。
        img[cy - 48:cy + 48, cx - 48:cx + 48] = (0, 140, 255)
        dets, issues = self.det.detect(img)
        unk = [d for d in dets if d.is_unknown]
        self.assertEqual(len(unk), 1)
        self.assertEqual(unk[0].object_id, "unknown_001")
        self.assertTrue(any("unrecognized_object" in i for i in issues))

    def test_background_color_is_not_reported_as_unknown(self):
        """桌面本身就是紫红的。它必须被 background 扣掉，不能算"多出来一件东西"。

        真实桌面照片里桌面占 81%、S 中位 133 —— 不扣的话整张桌子连成一块
        72 万像素的区域，会超过 max_area_ratio 被丢弃；更糟的是真异物会跟
        桌面连通被一起吞掉，系统永远发现不了桌上多了东西。
        """
        bg = CONFIG.get("background", {}).get("ranges") or []
        self.assertTrue(bg, "desktop.json 必须声明 background，否则桌面会被当成异物")
        lo, hi = bg[0]
        h_mid = int((lo[0] + hi[0]) / 2)
        img = self._scene_img([("A", (0, 0, 220))])
        hsv_like = cv2.cvtColor(
            np.full((96, 96, 3), (h_mid, 200, 140), np.uint8), cv2.COLOR_HSV2BGR)
        img[300:396, 600:696] = hsv_like
        dets, _ = self.det.detect(img)
        self.assertEqual([d for d in dets if d.is_unknown], [],
                         "背景色块被当成了未知物体")

    def test_dark_shadow_is_not_reported_as_unknown(self):
        """积木的投影阴影亮度只有 82，不是"桌上多了一件东西"。"""
        img = self._scene_img([("A", (0, 0, 220))])
        # 低亮度 + 高饱和：色相不属于背景区间，只能靠 V 门槛拦下
        img[300:396, 600:696] = (60, 40, 85)
        dets, _ = self.det.detect(img)
        self.assertEqual([d for d in dets if d.is_unknown], [],
                         "暗部阴影被当成了未知物体")

    def test_dim_frame_flags_brightness(self):
        # 压到 0.45：红块 V≈99 仍在 HSV 下限(70)之上，但已低于"太暗"阈值 → 必须自曝
        img = self._scene_img([("A", (0, 0, 220))])
        img = (img * 0.45).astype(np.uint8)
        dets, issues = self.det.detect(img)
        self.assertTrue(dets)
        self.assertTrue(all("too_dark" in d.reasons for d in dets))
        self.assertTrue(any("too_dark" in i for i in issues))

    def test_blackout_detects_nothing_and_claims_nothing(self):
        img = self._scene_img([("A", (0, 0, 220))])
        img = (img * 0.2).astype(np.uint8)  # 低于 HSV 下限 → 检不到，也不该编造
        dets, _ = self.det.detect(img)
        self.assertEqual([d for d in dets if not d.is_unknown], [])

    def test_multi_candidate_reduces_confidence(self):
        img = self._scene_img([("A", (0, 0, 220))])
        poly = np.asarray(CONFIG["zones"]["B"])
        cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
        img[cy - 40:cy + 40, cx - 40:cx + 40] = (0, 0, 220)  # 同色第二块
        dets, _ = self.det.detect(img)
        red = [d for d in dets if d.object_id == "block_red_01"][0]
        self.assertIn("multi_candidate", red.reasons)
        self.assertLess(red.confidence, 0.95)

    def test_half_occluded_square_is_flagged(self):
        # 齐边遮住右半边：剩下的仍是干净矩形，fill_ratio 依然是 1.0，
        # 只有长宽比从 1.0 变成 0.5 —— 这条用例专门守住这个盲点。
        # 注意：默认配置里不放 shape（长方形积木横竖摆放会误伤），
        # 这里显式注入，验证的是"能力还在"。
        det = Detector({**CONFIG, "shape": {"red": {"expected_aspect": 1.0,
                                                    "aspect_tolerance": 0.45}}})
        img = self._scene_img([("A", (0, 0, 220))])
        poly = np.asarray(CONFIG["zones"]["A"])
        cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
        img[cy - 48:cy + 48, cx:cx + 48] = (105, 105, 105)  # 灰色遮挡物盖住右半
        dets, _ = det.detect(img)
        red = [d for d in dets if d.object_id == "block_red_01"]
        self.assertTrue(red, "剩下半边也该检得到，只是不能当完整的它")
        red = red[0]
        self.assertGreater(red.fill_ratio, 0.9, "前提：这么整齐的遮挡 fill 抓不到")
        self.assertIn("possibly_occluded", red.reasons)
        self.assertTrue(any(r.startswith("shape_irregular") for r in red.reasons), red.reasons)
        self.assertLess(red.confidence, 0.8, "必须掉到不可信区间，让上层判 uncertain")

    def test_normal_square_not_flagged_as_irregular(self):
        # 反向用例：形状正常的方块不能被误标，否则每天都在刷假警
        det = Detector({**CONFIG, "shape": {"red": {"expected_aspect": 1.0,
                                                    "aspect_tolerance": 0.45}}})
        img = self._scene_img([("A", (0, 0, 220))])
        dets, _ = det.detect(img)
        red = [d for d in dets if d.object_id == "block_red_01"][0]
        self.assertNotIn("shape_irregular", " ".join(red.reasons))
        self.assertNotIn("possibly_occluded", red.reasons)

    def test_no_shape_config_means_no_aspect_check(self):
        # 默认配置没有 shape：长方形积木横放竖放都不该被当成遮挡
        det = Detector(CONFIG) if "shape" not in CONFIG else Detector(
            {k: v for k, v in CONFIG.items() if k != "shape"})
        img = self._scene_img([("A", (0, 0, 220))])
        poly = np.asarray(CONFIG["zones"]["A"])
        cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
        img[cy - 90:cy + 90, cx - 30:cx + 30] = (0, 0, 220)   # 竖着的长方形
        dets, _ = det.detect(img)
        red = [d for d in dets if d.object_id == "block_red_01"][0]
        self.assertLess(red.bbox[2] / red.bbox[3], 0.6, "确实是细长的")
        self.assertNotIn("shape_irregular", " ".join(red.reasons))

    def test_saturated_bright_color_is_not_overexposed(self):
        # V 打到 255 但 S 仍很高 = 饱和纯色，不是过曝（过曝是发白、S 掉下来）
        det = Detector(CONFIG)
        img = self._scene_img([("A", (0, 0, 255))])   # 亮到爆的纯红，S 依旧满
        dets, _ = det.detect(img)
        red = [d for d in dets if d.object_id == "block_red_01"][0]
        self.assertNotIn("overexposed", red.reasons, red.reasons)


class TestSourceHelpers(unittest.TestCase):
    def test_fit_same_size_is_noop(self):
        img = np.zeros((720, 1280, 3), np.uint8)
        out = fit_to_resolution(img, (1280, 720))
        self.assertEqual(out.shape, img.shape)

    def test_fit_scales_same_aspect(self):
        img = np.zeros((540, 960, 3), np.uint8)  # 手机常见的 16:9 540p 流
        out = fit_to_resolution(img, (1280, 720))
        self.assertEqual(out.shape[:2], (720, 1280))

    def test_fit_rejects_different_aspect(self):
        img = np.zeros((480, 640, 3), np.uint8)  # 4:3
        with self.assertRaises(SourceError):
            fit_to_resolution(img, (1280, 720))

    def test_fit_without_target(self):
        img = np.zeros((480, 640, 3), np.uint8)
        self.assertEqual(fit_to_resolution(img, None).shape, img.shape)

    def test_url_credentials_not_leaked(self):
        self.assertEqual(_strip_credentials("http://user:pass@192.168.1.7:8080/video"),
                         "http://192.168.1.7:8080/video")
        self.assertEqual(_strip_credentials(None), "")


class TestFrozenGuard(unittest.TestCase):
    """冻结画面必须报错：否则 S1 与 S0 完全一样，会被判 unchanged 而错误放行。"""

    def test_identical_frames_raise(self):
        guard = FrozenFrameGuard(max_identical=3)
        img = np.zeros((40, 40, 3), np.uint8)
        for i in range(3):
            guard.check(img)  # 还没到阈值
        with self.assertRaises(SourceError):
            guard.check(img)

    def test_live_frames_pass(self):
        guard = FrozenFrameGuard(max_identical=2)
        for i in range(20):
            guard.check(np.full((40, 40, 3), i % 256, np.uint8))
        # 没抛异常即通过

    def test_change_resets_counter(self):
        guard = FrozenFrameGuard(max_identical=2)
        a = np.zeros((10, 10, 3), np.uint8)
        b = a + 1
        guard.check(a)
        guard.check(a)   # count=1
        guard.check(b)   # 画面变了 → 计数清零
        guard.check(b)   # count=1
        with self.assertRaises(SourceError):
            guard.check(b)  # count=2 → 超阈值


class TestStability(unittest.TestCase):
    def test_needs_n_frames(self):
        st = StabilityTracker(frames=5, max_jitter_px=18)
        for i in range(4):
            res = st.update([("block_red_01", "A", (300.0, 200.0))])
            self.assertFalse(res["block_red_01"])
        self.assertTrue(st.update([("block_red_01", "A", (302.0, 201.0))])["block_red_01"])

    def test_zone_flip_is_unstable(self):
        st = StabilityTracker(frames=3, max_jitter_px=18)
        st.update([("x", "A", (300.0, 200.0))])
        st.update([("x", "A", (300.0, 200.0))])
        self.assertFalse(st.update([("x", "B", (300.0, 200.0))])["x"])

    def test_jitter_is_unstable(self):
        st = StabilityTracker(frames=3, max_jitter_px=10)
        st.update([("x", "A", (300.0, 200.0))])
        st.update([("x", "A", (300.0, 200.0))])
        self.assertFalse(st.update([("x", "A", (340.0, 200.0))])["x"])


# ---------------------------------------------------------------- 差异
class TestDiff(unittest.TestCase):
    def compare(self, s0, s1):
        return diff_mod.compare(s0, s1, check_images=False)

    def test_all_unchanged(self):
        objs = [obj("block_red_01", "A"), obj("block_green_01", "B")]
        d = self.compare(scene("s0", objs), scene("s1", objs))
        self.assertEqual(d["status"], "unchanged")
        self.assertEqual({x["status"] for x in d["differences"]}, {"unchanged"})
        self.assertEqual(diff_mod.gate_verdict(d), "READY")

    def test_single_move(self):
        s0 = scene("s0", [obj("block_red_01", "A"), obj("block_green_01", "B")])
        s1 = scene("s1", [obj("block_red_01", "B", center=(650.0, 200.0)),
                          obj("block_green_01", "B")])
        d = self.compare(s0, s1)
        self.assertEqual(d["status"], "misplaced")
        red = [x for x in d["differences"] if x["object_id"] == "block_red_01"][0]
        self.assertEqual((red["from_zone"], red["to_zone"]), ("A", "B"))

    def test_confirmed_misplaced_is_not_ready_not_review(self):
        # 双侧都可靠才判 misplaced，结论已经足够硬：不能开始下一轮。
        # 退化成 REVIEW 等于把"明确不行"说成"待复核"，责任又推回给人。
        s0 = scene("s0", [obj("block_red_01", "A"), obj("block_green_01", "B")])
        s1 = scene("s1", [obj("block_red_01", "B", center=(650.0, 200.0)),
                          obj("block_green_01", "A", center=(250.0, 200.0))])
        d = self.compare(s0, s1)
        self.assertEqual({x["status"] for x in d["differences"]}, {"misplaced"})
        self.assertEqual(diff_mod.gate_verdict(d), "NOT_READY")

    def test_swap_keeps_identity(self):
        s0 = scene("s0", [obj("block_red_01", "A"), obj("block_green_01", "B")])
        s1 = scene("s1", [obj("block_red_01", "B"), obj("block_green_01", "A")])
        d = self.compare(s0, s1)
        self.assertEqual({x["status"] for x in d["differences"]}, {"misplaced"})
        self.assertEqual(len(d["differences"]), 2)

    def test_not_observed_is_uncertain_not_missing(self):
        s0 = scene("s0", [obj("block_red_01", "A"), obj("block_green_01", "B")])
        s1 = scene("s1", [obj("block_red_01", "A"),
                          obj("block_green_01", "unknown", vis="unknown", conf=0.0,
                              center=None, reasons=["not_detected"])])
        d = self.compare(s0, s1)
        green = [x for x in d["differences"] if x["object_id"] == "block_green_01"][0]
        self.assertEqual(green["status"], "uncertain")
        self.assertIn("not_observed", green["reasons"])
        self.assertEqual(d["status"], "uncertain")
        self.assertNotEqual(diff_mod.gate_verdict(d), "READY")

    def test_unknown_object_blocks_acceptance(self):
        s0 = scene("s0", [obj("block_red_01", "A")])
        s1 = scene("s1", [obj("block_red_01", "A"),
                          obj("unknown_001", "TEMP", conf=0.4, reasons=["unrecognized_object"])])
        d = self.compare(s0, s1)
        unk = [x for x in d["differences"] if x["object_id"] == "unknown_001"][0]
        self.assertEqual(unk["status"], "uncertain")
        self.assertIn("unrecognized_object", unk["reasons"])
        self.assertEqual(diff_mod.gate_verdict(d), "REVIEW")

    def test_mode_mismatch_forces_uncertain(self):
        objs = [obj("block_red_01", "A")]
        d = self.compare(scene("s0", objs, mode="real"), scene("s1", objs, mode="mock"))
        self.assertIn("mode_mismatch", d["issues"])
        self.assertEqual(d["status"], "uncertain")
        self.assertEqual(d["differences"][0]["status"], "uncertain")

    def test_calibration_mismatch_forces_uncertain(self):
        objs = [obj("block_red_01", "A")]
        d = self.compare(scene("s0", objs, calib="desktop-v1"),
                         scene("s1", objs, calib="desktop-v2"))
        self.assertIn("calibration_mismatch", d["issues"])
        self.assertEqual(d["status"], "uncertain")

    def test_low_confidence_is_uncertain(self):
        s0 = scene("s0", [obj("block_red_01", "A", conf=0.95)])
        s1 = scene("s1", [obj("block_red_01", "A", conf=0.55)])
        d = self.compare(s0, s1)
        self.assertEqual(d["differences"][0]["status"], "uncertain")

    def test_occluded_is_uncertain(self):
        s0 = scene("s0", [obj("block_red_01", "A")])
        s1 = scene("s1", [obj("block_red_01", "A", vis="occluded", conf=0.6,
                              reasons=["possibly_occluded"])])
        d = self.compare(s0, s1)
        self.assertEqual(d["differences"][0]["status"], "uncertain")

    def test_empty_scene_is_not_vacuous_success(self):
        d = self.compare(scene("s0", []), scene("s1", []))
        self.assertEqual(d["status"], "uncertain")
        self.assertIn("baseline_empty", d["issues"])
        self.assertNotEqual(diff_mod.gate_verdict(d), "READY")

    def test_confidence_is_conservative_min(self):
        s0 = scene("s0", [obj("block_red_01", "A", conf=0.95)])
        s1 = scene("s1", [obj("block_red_01", "A", conf=0.85)])
        d = self.compare(s0, s1)
        self.assertEqual(d["differences"][0]["confidence"], 0.85)


# ---------------------------------------------------------------- Schema 与快照
class TestSchemaAndSnapshot(unittest.TestCase):
    def test_schemas_are_valid_draft2020(self):
        snap.load_schema(snap.SCENE_SCHEMA)
        snap.load_schema(snap.DIFF_SCHEMA)

    def test_sample_scene_from_taskbook_passes(self):
        data = {
            "schema_version": "1.0", "snapshot_id": "s1-demo",
            "timestamp": "2026-11-06T08:00:00Z", "mode": "mock", "source": "fixture:demo",
            "image": {"reference": "fixtures/s1.jpg", "width": 1280, "height": 720},
            "calibration": {"id": "desktop-v1", "valid": True, "coordinate_frame": "image_pixels"},
            "quality_ok": True, "issues": [],
            "objects": [{"object_id": "block_red_01", "zone": "B", "visibility": "visible",
                         "confidence": 0.94, "center_px": [420, 280], "reasons": []}],
        }
        snap.validate(data, snap.load_schema(snap.SCENE_SCHEMA), "scene")

    def test_extra_field_is_rejected(self):
        data = scene("s1", [obj("block_red_01", "A")])
        data["extra"] = 1
        with self.assertRaises(snap.SnapshotError):
            snap.validate(data, snap.load_schema(snap.SCENE_SCHEMA), "scene")

    def test_visible_requires_center(self):
        with self.assertRaises(snap.SnapshotError):
            snap.object_record(object_id="x", zone="A", visibility="visible",
                               confidence=0.9, center_px=None, reasons=[])

    def test_save_and_immutability(self):
        # 放在项目内，才能验证"图像引用是项目相对路径"这条交付要求
        tmp = Path("vision/records/_unittest_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            store = snap.SnapshotStore(tmp)
            frame = fake_frame()
            img = np.full((RES[1], RES[0], 3), 100, np.uint8)
            sc = snap.build_scene(frame=frame, objects=[obj("block_red_01", "A")],
                                  quality_ok=True, issues=[], calibration_id="desktop-v1",
                                  calibration_valid=True)
            out = store.save(sc, img, img)
            self.assertTrue(out["json"].is_file())
            self.assertTrue(out["raw"].is_file())
            self.assertTrue(out["annotated"].is_file())
            self.assertTrue(sc["image"]["reference"].endswith("_raw.jpg"))
            self.assertFalse(Path(sc["image"]["reference"]).is_absolute())
            self.assertTrue(sc["image"]["reference"].startswith("vision/records/"))
            with self.assertRaises(snap.SnapshotError):
                store.save(sc, img, img)  # 同名快照拒绝覆盖
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_baseline_blockers(self):
        frame = fake_frame()
        good = snap.build_scene(frame=frame, objects=[obj("block_red_01", "A")],
                                quality_ok=True, issues=[], calibration_id="desktop-v1",
                                calibration_valid=True)
        self.assertEqual(snap.baseline_blockers(good, 0.8), [])

        bad = snap.build_scene(frame=frame,
                               objects=[obj("block_red_01", "unknown", vis="unknown",
                                            conf=0.0, center=None, reasons=["not_detected"])],
                               quality_ok=False, issues=["block_red_01:not_detected"],
                               calibration_id="desktop-v1", calibration_valid=True)
        blockers = snap.baseline_blockers(bad, 0.8)
        self.assertIn("quality_not_ok", blockers)
        self.assertIn("block_red_01:not_visible", blockers)


# ---------------------------------------------------------------- mock 端到端
class TestMockPipeline(unittest.TestCase):
    def test_mock_source_is_labelled(self):
        src = MockSource(CONFIG["zones"], RES, "ok")
        f = src.read()
        self.assertEqual(f.mode, "mock")
        self.assertIn("mock:", f.source)
        self.assertEqual((f.width, f.height), RES)

    def test_mock_scenario_switch(self):
        src = MockSource(CONFIG["zones"], RES, "ok")
        self.assertNotEqual(src.next_scenario(), "ok")
        with self.assertRaises(SourceError):
            src.set_scenario("nope")

    def test_end_to_end_ok_then_moved(self):
        """mock 合成 S0(ok) 与 S1(moved)，跑完整链路：检测→区域→快照→差异。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            det = Detector(CONFIG)
            zs = ZoneSet(CONFIG["zones"], CONFIG["zone_margin_px"])
            store = snap.SnapshotStore(tmp)

            def capture(scenario):
                src = MockSource(CONFIG["zones"], RES, scenario)
                fr = src.read()  # 单帧、无稳定性累积，故这里按"已稳定"构造
                dets, issues = det.detect(fr.image)
                objs = []
                for d in dets:
                    zone, zreasons, _ = zs.assign(d.center_px)
                    objs.append(snap.object_record(
                        object_id=d.object_id, zone=zone, visibility="visible",
                        confidence=d.confidence, center_px=d.center_px,
                        reasons=d.reasons + zreasons))
                objs.sort(key=lambda o: o["object_id"])
                sc = snap.build_scene(frame=fr, objects=objs, quality_ok=not issues,
                                      issues=issues, calibration_id="desktop-v1",
                                      calibration_valid=True)
                store.save(sc, fr.image, fr.image)
                return sc

            s0, s1 = capture("ok"), capture("moved")
            self.assertEqual(snap.baseline_blockers(s0, 0.8), [])
            d = diff_mod.compare(s0, s1, check_images=True)
            red = [x for x in d["differences"] if x["object_id"] == "block_red_01"][0]
            self.assertEqual(red["status"], "misplaced")
            self.assertEqual((red["from_zone"], red["to_zone"]), ("A", "B"))
            for other in ("block_green_01", "block_blue_01"):
                row = [x for x in d["differences"] if x["object_id"] == other][0]
                self.assertEqual(row["status"], "unchanged")
            self.assertEqual(d["status"], "misplaced")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mock_extra_scenario_triggers_unknown(self):
        det = Detector(CONFIG)
        src = MockSource(CONFIG["zones"], RES, "extra")
        dets, issues = det.detect(src.read().image)
        self.assertTrue(any(d.is_unknown for d in dets))
        self.assertTrue(any("unrecognized_object" in i for i in issues))


class TestFixtures(unittest.TestCase):
    """用落盘的固定样例图复测整条链路（任务书第 5 节第 7 条）。"""

    fixtures = Path("vision/tests/fixtures")

    def setUp(self):
        if not (self.fixtures / "s0.jpg").is_file():
            self.skipTest("样例图缺失，先跑 python -m vision.tests.make_fixtures")
        from types import SimpleNamespace
        from vision.main import VisionApp
        self.tmp = Path(tempfile.mkdtemp())
        # 样例图有三色，生产配置只有两色；用注入了 blue 的 CONFIG 落一个临时配置，
        # 免得"现场桌面上少一块积木"把样例回归也带崩
        self.cfg_path = self.tmp / "fixture_config.json"
        self.cfg_path.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
        self.app = VisionApp(SimpleNamespace(
            camera=0, config=str(self.cfg_path), mode="mock",
            output=str(self.tmp), min_conf=0.8, mock_cycle=False,
            skip_image_check=True, allow_frozen=False))

    def tearDown(self):
        shutil.rmtree(getattr(self, "tmp", "."), ignore_errors=True)

    def _scene(self, name):
        import cv2
        img = cv2.imread(str(self.fixtures / name))
        self.assertIsNotNone(img, f"读不到样例图 {name}")
        frame = Frame(image=img, captured_at=utc_now_iso(),
                      source="fixture:synth", mode="mock", index=1)
        need = CONFIG["stability_frames"]
        for _ in range(need):  # 静态图连喂 N 帧，模拟"画面稳定"
            self.app.observe(frame)
        return self.app.scene

    def test_all_fixtures_match_expected(self):
        expected = json.loads((self.fixtures / "expected.json").read_text(encoding="utf-8"))
        baseline = self._scene("s0.jpg")
        self.assertEqual(snap.baseline_blockers(baseline, 0.8), [],
                         "样例 s0 应当是一份合格基准")
        for name, meta in expected.items():
            if meta["expected_status_vs_s0"] == "baseline":
                continue
            with self.subTest(fixture=name):
                current = self._scene(name)
                result = diff_mod.compare(baseline, current, check_images=False)
                self.assertEqual(result["status"], meta["expected_status_vs_s0"],
                                 f"{name} 预期 {meta['expected_status_vs_s0']}，实得 {result['status']}，"
                                 f"issues={result['issues']}")

    def test_occluded_fixture_never_reports_missing(self):
        baseline = self._scene("s0.jpg")
        current = self._scene("s1_occluded.jpg")
        result = diff_mod.compare(baseline, current, check_images=False)
        blue = [d for d in result["differences"] if d["object_id"] == "block_blue_01"][0]
        self.assertEqual(blue["status"], "uncertain")
        self.assertNotIn("missing", json.dumps(result))
        self.assertNotEqual(diff_mod.gate_verdict(result), "READY")


# ---------------------------------------------------------------- 标签字母
def _tag_scene(letter: str, tag_size=(110, 78), seed: int = 0) -> np.ndarray:
    """紫红桌面上放一张白标签，上面黑字写 letter。"""
    img = np.full((RES[1], RES[0], 3), 0, np.uint8)
    img[:, :] = cv2.cvtColor(np.full((1, 1, 3), (124, 133, 139), np.uint8),
                             cv2.COLOR_HSV2BGR)[0, 0]
    tw, th = tag_size
    tag = np.full((th, tw, 3), 242, np.uint8)
    # 线宽 4 而不是 6：太粗的印刷体二值化后笔画占比能到 0.74，会被
    # character_mask 的"实心块"上限挡掉，测的就不是识别而是那道闸门了。
    (w, h), _ = cv2.getTextSize(letter, cv2.FONT_HERSHEY_SIMPLEX, 1.9, 4)
    cv2.putText(tag, letter, ((tw - w) // 2, (th + h) // 2), cv2.FONT_HERSHEY_SIMPLEX,
                1.9, (20, 20, 20), 4, cv2.LINE_AA)
    x, y = 520, 280
    img[y:y + th, x:x + tw] = tag
    return img


class TestLetterLabels(unittest.TestCase):
    def setUp(self):
        from vision.letters import LetterBank, find_labels, ink_mask, normalize
        self.find_labels = find_labels
        self.ink_mask = ink_mask
        self.normalize = normalize
        self.bank = LetterBank()

    def _mask_of(self, img):
        labels = self.find_labels(img, {"min_area_px": 1200})
        self.assertTrue(labels, "没找到白色标签")
        return self.ink_mask(self.normalize(img, labels[0]))

    def test_finds_label_on_colored_desk(self):
        labels = self.find_labels(_tag_scene("A"), {"min_area_px": 1200})
        self.assertEqual(len(labels), 1)

    def test_same_letter_matches_higher_than_other(self):
        a = self._mask_of(_tag_scene("A"))
        b = self._mask_of(_tag_scene("B"))
        self.bank.enroll("A", a)
        got_a, score_a = self.bank.recognize(a)
        got_b, score_b = self.bank.recognize(b)
        self.assertEqual(got_a, "A")
        self.assertGreater(score_a, score_b,
                           "同一个字母的相似度应该高于别的字母")
        # 异字母的分数要明显低一截。这里不写死绝对阈值：putText 渲染的印刷体
        # A 和 B 骨架很像（竖直主干 + 曲线），实测 0.56；而真实那张手写的
        # A vs B 只有 0.45。测试用的合成字比真人写的更像，绑死 0.55 会误报。
        self.assertLess(score_b, score_a * 0.8,
                        f"异字母 {score_b:.3f} 与同字母 {score_a:.3f} 拉不开差距")

    def test_blank_white_patch_is_not_a_letter(self):
        """白色积木顶面会被误当标签，但它上面没字，必须被拒。"""
        img = _tag_scene("A")
        # 盖一张更大的纯白块，模拟"白色积木顶面"（控制在面积上限内）
        img[290:410, 480:700] = 246
        labels = self.find_labels(img, {"min_area_px": 1200})
        self.assertTrue(labels, "白块本身应该被找到")
        masks = [self.ink_mask(self.normalize(img, lb)) for lb in labels]
        self.assertTrue(all(m is None for m in masks),
                        "纯白面上没有笔画，不该产出模板")

    def test_empty_bank_falls_back_to_color_identity(self):
        cfg = {k: v for k, v in CONFIG.items() if k != "label"}
        cfg["label"] = {"enabled": True, "bank": ""}
        det = Detector(cfg)
        img = np.full((RES[1], RES[0], 3), 105, np.uint8)
        poly = np.asarray(CONFIG["zones"]["A"])
        cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
        img[cy - 48:cy + 48, cx - 48:cx + 48] = (0, 0, 220)
        dets, _ = det.detect(img)
        self.assertIn("block_red_01", {d.object_id for d in dets},
                      "没登记任何字母时应该按颜色认，否则系统等于不可用")

    def test_enrolled_bank_switches_identity_to_letter(self):
        cfg = {k: v for k, v in CONFIG.items() if k != "label"}
        cfg["label"] = {"enabled": True, "bank": "", "match_threshold": 0.55}
        det = Detector(cfg)
        det.letter_bank.enroll("A", self._mask_of(_tag_scene("A")))

        # 一块红色积木，标签贴在它自己身上 —— 标签必须在积木框内，
        # 否则这块会判 label_unreadable 直接消失（这是设计，不是 bug）
        img = np.full((RES[1], RES[0], 3), 0, np.uint8)
        img[:, :] = cv2.cvtColor(np.full((1, 1, 3), (124, 133, 139), np.uint8),
                                 cv2.COLOR_HSV2BGR)[0, 0]
        img[260:420, 460:800] = (0, 0, 220)
        tw, th = 110, 78
        tag = np.full((th, tw, 3), 242, np.uint8)
        (w, h), _ = cv2.getTextSize("A", cv2.FONT_HERSHEY_SIMPLEX, 1.9, 6)
        cv2.putText(tag, "A", ((tw - w) // 2, (th + h) // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    1.9, (20, 20, 20), 6, cv2.LINE_AA)
        img[300:378, 520:630] = tag

        dets, issues = det.detect(img)
        ids = {d.object_id for d in dets}
        self.assertIn("block_A", ids, f"登记 A 后身份应变成 block_A，实得 {ids}")
        self.assertTrue(all("label_unreadable" not in i for i in issues), issues)

    def test_letter_recognised_at_arbitrary_angle(self):
        """歪着摆的积木也要认得出。

        积木靠手摆，歪 15°、30° 太正常了。只试 0/90/180/270 的话，歪 20° 时
        四个正交朝向全对不齐 —— 实测自比分数 0.45-0.61，全部掉到阈值以下。
        这里把同一副笔画转 30°、45° 再认，要求仍然认对。
        """
        from vision.letters import _fit_canvas, _ink_crop
        a = self._mask_of(_tag_scene("A"))
        b = self._mask_of(_tag_scene("B"))
        self.bank.enroll("A", a)

        for deg in (30.0, 45.0, 210.0):
            big = 128
            canvas = np.zeros((big, big), np.uint8)
            canvas[(big - 48) // 2:(big + 48) // 2,
                   (big - 48) // 2:(big + 48) // 2] = a
            M = cv2.getRotationMatrix2D((big / 2.0, big / 2.0), deg, 1.0)
            rot = cv2.warpAffine(canvas, M, (big, big), flags=cv2.INTER_LINEAR)
            rot = (rot > 110).astype(np.uint8) * 255
            q = _fit_canvas(_ink_crop(rot))
            got, score = self.bank.recognize(q)
            self.assertEqual(got, "A", f"转 {deg}° 后认成了 {got!r}")
            self.assertGreater(score, 0.60, f"转 {deg}° 只有 {score:.3f}")

        # 歪着的 B 不能因为"多转几档总能凑上"而被认成 A。
        # 这里不比绝对阈值：putText 渲染的印刷体 B 和手写 A 的骨架天生更像
        # （实测 0.62），而真实那张手写的 A vs B 只有 0.51。比的是**判别性**
        # —— 两个字母都在库里时，歪着的 B 必须压倒性地选 B。
        big = 128
        canvas = np.zeros((big, big), np.uint8)
        canvas[(big - 48) // 2:(big + 48) // 2,
               (big - 48) // 2:(big + 48) // 2] = b
        rot = cv2.warpAffine(canvas, cv2.getRotationMatrix2D((big / 2.0, big / 2.0), 30.0, 1.0),
                             (big, big), flags=cv2.INTER_LINEAR)
        q_b = _fit_canvas(_ink_crop(((rot > 110).astype(np.uint8)) * 255))

        self.bank.enroll("B", b)
        got, score_best = self.bank.recognize(q_b)
        self.assertEqual(got, "B", f"歪 30° 的 B 认成了 {got!r}")

        only_a = type(self.bank)()
        only_a.enroll("A", a)
        _, score_a = only_a.recognize(q_b)
        self.assertGreater(score_best - score_a, 0.15,
                           f"B {score_best:.3f} 与 A {score_a:.3f} 拉不开差距")


class _FakeVlm:
    """视觉模型替身：不联网，直接吐出预设答案。

    真的 VlmReader 一调就发 HTTP，单元测试不能依赖网络；但"模型说什么、
    模板说什么、最后听谁的"这套合流规则恰恰最该钉死 —— 弄错了现场看到的是
    "认出来了，但 ID 是错的"，比认不出更糟。
    """

    def __init__(self, answer=None, available=True):
        self.answer = answer
        self.available = available
        self.warmed = 0

    def peek(self, tile):
        return self.answer

    def warm(self, tile):
        self.warmed += 1

    def stats(self):
        return {"available": self.available}

    def describe(self):
        return "fake-vl"

    def why_unavailable(self):
        return "" if self.available else "没配 api_key"


class TestVlmFusion(unittest.TestCase):
    """模板比对 + 视觉模型的三种读法（template / vlm / auto）。"""

    def setUp(self):
        from vision.letters import LetterBank, find_labels, ink_mask, normalize
        self.find_labels = find_labels
        self.ink_mask = ink_mask
        self.normalize = normalize
        self.bank = LetterBank()

    # ---- 工具 ----
    def _mask_of(self, img):
        labels = self.find_labels(img, {"min_area_px": 1200})
        self.assertTrue(labels, "没找到白色标签")
        return self.ink_mask(self.normalize(img, labels[0]))

    def _det(self, **label):
        cfg = {k: v for k, v in CONFIG.items() if k != "label"}
        lab = {"enabled": True, "primary": True, "bank": "",
               "match_threshold": 0.6, "min_area_px": 1200}
        lab.update(label)
        cfg["label"] = lab
        return Detector(cfg)

    # ---- 合流规则 ----
    def test_template_mode_is_untouched(self):
        """没模型的路径必须和引入模型之前一字不差 —— 断网就靠它。"""
        det = self._det(reader="template")
        self.assertIsNone(det.vlm)
        self.assertEqual(det._fuse("A", 0.71, "B"), ("A", 0.71, "template"))
        self.assertEqual(det._fuse("", 0.0, "B"), ("", 0.0, "template"))

    def test_auto_prefers_model_when_template_has_nothing(self):
        """库里没有的字母（新加的 D）由模型补上 —— 这是接模型的主要理由。"""
        det = self._det(reader="auto")
        det.vlm = _FakeVlm("D")
        letter, score, src = det._fuse("", 0.0, "D")
        self.assertEqual((letter, src), ("D", "vlm"))
        self.assertGreaterEqual(score, det.label_match_threshold)

    def test_auto_agreement_does_not_lower_score(self):
        """两条独立的路结论一致时，没有理由反而把分数压到模板那个低分。"""
        det = self._det(reader="auto")
        det.vlm = _FakeVlm("A")
        letter, score, src = det._fuse("A", 0.62, "A")
        self.assertEqual((letter, src), ("A", "both"))
        self.assertGreater(score, 0.9, "一致时应该按较高者记账")

    def test_auto_conflict_takes_model_but_flags_low_confidence(self):
        """打架时听模型的，但分数压在刚过线，别拿 0.95 假装很确定。"""
        det = self._det(reader="auto")
        det.vlm = _FakeVlm("B")
        letter, score, src = det._fuse("A", 0.92, "B")
        th = det.label_match_threshold
        self.assertEqual(letter, "B")
        self.assertEqual(src, "vlm_conflict")
        self.assertGreaterEqual(score, th, "压得太狠会让这块直接消失")
        self.assertLess(score, 0.7, "打架却给高分，现场就看不出有争议")

    def test_auto_weak_template_guess_does_not_count_as_conflict(self):
        """模板 0.6x 的"猜"不算意见：新字母 D 不该被标成有争议。

        实测同字母自比 ≥0.82、异字母 ≤0.51，0.60 只是闸门。把"勉强过线的
        猜测"当成"另一派意见"，会让每一块没登记过的积木都显示低置信度，
        现场看起来像系统在瞎猜。
        """
        det = self._det(reader="auto")
        det.vlm = _FakeVlm("D")
        letter, score, src = det._fuse("A", 0.63, "D")
        self.assertEqual((letter, src), ("D", "vlm"))
        self.assertGreater(score, 0.9)

    def test_auto_falls_back_to_template_while_model_pending(self):
        det = self._det(reader="auto")
        det.vlm = _FakeVlm(None)          # 还在后台算
        self.assertEqual(det._fuse("A", 0.83, None), ("A", 0.83, "template"))
        # 模型算完但说看不出 —— 同样退回模板，不能把已经认出来的字丢掉
        self.assertEqual(det._fuse("A", 0.83, ""), ("A", 0.83, "template"))

    def test_vlm_only_never_silently_uses_template(self):
        """选了 vlm 就是不要字形库，混进模板结果会让人分不清是谁认的。"""
        det = self._det(reader="vlm")
        det.vlm = _FakeVlm(None)
        self.assertEqual(det._fuse("A", 0.93, None)[0], "")
        self.assertEqual(det._fuse("A", 0.93, "")[0], "")

    # ---- 端到端（检测入口）----
    def test_unenrolled_letter_detected_through_model(self):
        """模板库只有 A，画面里是一块 D —— 模型要能直接把它认出来。"""
        det = self._det(reader="auto")
        det.letter_bank.enroll("A", self._mask_of(_tag_scene("A")))
        det.vlm = _FakeVlm("D")
        dets, _issues = det.detect(_tag_scene("D"))
        self.assertEqual({d.object_id for d in dets}, {"block_D"})
        self.assertEqual(det.last_label_info[0]["src"], "vlm")

    def test_pending_model_holds_without_ghost_object(self):
        """模型还在算时，既不能凭空造一个物体，也不能报"读不出"闪红。"""
        det = self._det(reader="auto", match_threshold=0.75)
        det.letter_bank.enroll("A", self._mask_of(_tag_scene("A")))
        det.vlm = _FakeVlm(None)
        dets, issues = det.detect(_tag_scene("D"))
        self.assertEqual([d.object_id for d in dets], [])
        self.assertTrue(any(i.startswith("label_pending:") for i in issues), issues)
        self.assertFalse(any(i.startswith("label_unread:") for i in issues), issues)

    def test_reader_name_is_honest_about_missing_key(self):
        """页面要能一眼看出"模型到底接上没有"，不能默默退回模板。"""
        det = self._det(reader="vlm")
        self.assertFalse(det.vlm.available)
        self.assertIn("未就绪", det.reader_name())
        det.vlm = _FakeVlm("A")
        self.assertIn("模型", det.reader_name())


class TestVlmReaderOffline(unittest.TestCase):
    """VlmReader 里不联网的那些逻辑：解析、密钥、缓存键。"""

    def setUp(self):
        from vision.vlm import VlmReader
        self.VlmReader = VlmReader

    def test_unconfigured_reader_reports_why(self):
        r = self.VlmReader()
        self.assertFalse(r.available)
        self.assertTrue(r.why_unavailable())
        # 没配置时 read() 必须立刻返回错误，而不是卡在网络上
        res = r.read(np.zeros((48, 48, 3), np.uint8))
        self.assertTrue(res.error)

    def test_env_secret_is_not_stored_in_plain_text(self):
        import os
        r = self.VlmReader(base_url="http://x/v1", model="m",
                           api_key="env:RE_BENCH_TEST_KEY_UNSET")
        self.assertFalse(r.available, "环境变量没设时不该当成已配置")
        os.environ["RE_BENCH_TEST_KEY_UNSET"] = "sk-test"
        try:
            r2 = self.VlmReader(base_url="http://x/v1", model="m",
                                api_key="env:RE_BENCH_TEST_KEY_UNSET")
            self.assertTrue(r2.available)
            self.assertEqual(r2.api_key, "sk-test")
        finally:
            os.environ.pop("RE_BENCH_TEST_KEY_UNSET", None)

    def test_base_url_gets_completion_path(self):
        r = self.VlmReader(base_url="https://x/v1/", model="m", api_key="k")
        self.assertEqual(r.base_url, "https://x/v1/chat/completions")
        r2 = self.VlmReader(base_url="https://x/v1/chat/completions", model="m",
                            api_key="k")
        self.assertEqual(r2.base_url, "https://x/v1/chat/completions")

    def test_parse_letter_handles_real_answers(self):
        from vision.vlm import _parse_letter
        self.assertEqual(_parse_letter("D"), "D")
        self.assertEqual(_parse_letter(" d \n"), "D")
        self.assertEqual(_parse_letter("字母是 B。"), "B")
        self.assertEqual(_parse_letter("?"), "")
        self.assertEqual(_parse_letter("看不出"), "")
        self.assertEqual(_parse_letter(""), "")

    def test_extract_text_covers_vendor_shapes(self):
        from vision.vlm import _extract_text
        self.assertEqual(_extract_text(
            {"choices": [{"message": {"content": "A"}}]}), "A")
        self.assertEqual(_extract_text(
            {"choices": [{"message": {"content": [{"text": "B"}]}}]}), "B")
        self.assertEqual(_extract_text({"output": {"text": "C"}}), "C")
        self.assertEqual(_extract_text({"nope": 1}), "")

    def test_cache_key_survives_small_jitter(self):
        """缓存按感知哈希查：手机画面每帧都有细微抖动，key 一变就白算。"""
        img = _tag_scene("A")[270:370, 500:650].copy()
        k1 = self.VlmReader().key_of(img)
        noisy = np.clip(img.astype(np.int16) + 2, 0, 255).astype(np.uint8)
        self.assertEqual(k1, self.VlmReader().key_of(noisy))
        other = _tag_scene("B")[270:370, 500:650].copy()
        self.assertNotEqual(k1, self.VlmReader().key_of(other))


_VLM_SEEN: dict = {}


class TestVlmProtocol(unittest.TestCase):
    """对着一个假的 OpenAI 兼容服务验协议。

    这条路的价值在于：不需要真 key 也能验证"发出去的请求到底是什么样"。
    base_url 少补一段 /chat/completions、图片没塞进 messages、Authorization
    写成别的形式 —— 这些错都不会在本地报错，只会在现场变成一个 401/404，
    而那时离答辩只剩半小时。
    """

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):        # 别把测试输出刷满
                pass

            def do_POST(self):                 # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                _VLM_SEEN["path"] = self.path
                _VLM_SEEN["auth"] = self.headers.get("Authorization")
                try:
                    _VLM_SEEN["payload"] = json.loads(raw.decode("utf-8"))
                except ValueError:
                    _VLM_SEEN["payload"] = None
                if self.path.startswith("/bad/"):
                    body = b'{"error":"boom"}'
                    self.send_response(500)
                else:
                    body = json.dumps({"choices": [{"message": {"content": "D"}}]}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        from vision.vlm import VlmReader
        self.VlmReader = VlmReader
        _VLM_SEEN.clear()
        self.tile = _tag_scene("D")[250:400, 470:700]

    def _reader(self, prefix="/v1"):
        return self.VlmReader(base_url=f"http://127.0.0.1:{self.port}{prefix}",
                              model="fake-vl", api_key="sk-test", timeout_s=5)

    def test_request_shape_is_openai_compatible(self):
        res = self._reader().read(self.tile)
        self.assertEqual(res.letter, "D")
        self.assertEqual(_VLM_SEEN["path"], "/v1/chat/completions",
                         "base_url 没自动补 /chat/completions")
        self.assertEqual(_VLM_SEEN["auth"], "Bearer sk-test")
        p = _VLM_SEEN["payload"]
        self.assertEqual(p["model"], "fake-vl")
        self.assertEqual(p["temperature"], 0)
        self.assertLessEqual(p["max_tokens"], 16, "只要一个字母，别让它长篇大论")
        content = p["messages"][0]["content"]
        kinds = [c.get("type") for c in content]
        self.assertIn("text", kinds)
        self.assertIn("image_url", kinds)
        url = [c for c in content if c.get("type") == "image_url"][0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"), url[:40])

    def test_warm_then_peek_uses_cache_and_calls_once(self):
        r = self._reader()
        self.assertIsNone(r.peek(self.tile), "还没算过就该返回 None")
        r.warm(self.tile)
        self.assertTrue(r.wait_idle(10), "后台队列没跑完")
        self.assertEqual(r.peek(self.tile), "D")
        n = r.calls
        self.assertEqual(n, 1)
        r.warm(self.tile)                     # 同一张图，不该再排队
        r.wait_idle(2)
        self.assertEqual(r.calls, n, "缓存命中还去调模型，等于白烧配额")

    def test_http_error_is_reported_and_not_cached(self):
        """失败不能固化进缓存：网络恢复后要能重试。"""
        r = self._reader("/bad")
        res = r.read(self.tile)
        self.assertIn("500", res.error)
        self.assertEqual(res.letter, "")
        self.assertNotIn(r.key_of(self.tile), r.cache)

    def test_repeated_failure_gives_up_instead_of_pretending(self):
        """key 填错时要自己投降。

        不投降的话，页面一直显示"模型+模板"，实际永远拿不到模型结果 ——
        这种"假装在工作"比直接报错难查得多，现场只会觉得"接了模型怎么没变准"。
        """
        r = self._reader("/bad")
        r.backoff_s = 0.0
        r.max_failures = 2
        r.warm(_tag_scene("A")[250:400, 470:700])
        r.wait_idle(10)
        r.warm(_tag_scene("B")[250:400, 470:700])
        r.wait_idle(10)
        self.assertTrue(r.given_up)
        self.assertFalse(r.available)
        self.assertIn("停用", r.why_unavailable())


if __name__ == "__main__":
    unittest.main(verbosity=2)
