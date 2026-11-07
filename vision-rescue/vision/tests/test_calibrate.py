"""标定工具的逻辑回归测试（不弹窗口，只测"点四个角 → 写配置"这段）。"""

from __future__ import annotations

import json
import unittest

from vision.tools.calibrate import (
    ZONE_ORDER,
    ZoneCollector,
    apply_to_config,
    check_zones,
    rect_from,
)

CFG = json.loads(open("vision/config/desktop.json", encoding="utf-8").read())


def quad(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


class TestZoneCollector(unittest.TestCase):
    def test_four_points_complete_one_zone(self):
        c = ZoneCollector()
        for p in quad(100, 100, 300, 300):
            self.assertEqual(c.add(*p), "ok")
        self.assertTrue(c.is_complete("A"))
        self.assertEqual(c.add(10, 10), "full")  # 点满了不接受第 5 个点
        self.assertFalse(c.all_complete())

    def test_next_blocked_until_four_points(self):
        c = ZoneCollector()
        c.add(100, 100)
        c.add(300, 100)
        self.assertFalse(c.next_zone())
        self.assertEqual(c.current, "A")

    def test_undo_removes_last_point(self):
        c = ZoneCollector()
        c.add(100, 100)
        self.assertTrue(c.undo())
        self.assertFalse(c.undo())

    def test_walk_all_zones(self):
        c = ZoneCollector()
        for _ in ZONE_ORDER:
            for p in quad(0, 0, 10, 10):
                c.add(*p)
            c.next_zone()
        self.assertTrue(c.all_complete())
        self.assertEqual(len(c.snapshot()), 5)

    def test_area_of_clicked_quad(self):
        c = ZoneCollector()
        for p in quad(100, 100, 300, 400):
            c.add(*p)
        self.assertAlmostEqual(c.areas()["A"], 200 * 300, places=0)


class TestRectMode(unittest.TestCase):
    """两点矩形模式：点两个对角就生成轴对齐矩形。"""

    def test_rect_from_any_two_corners(self):
        a = rect_from((300, 400), (100, 120))
        b = rect_from((100, 120), (300, 400))
        self.assertEqual(a, [[100, 120], [300, 120], [300, 400], [100, 400]])
        self.assertEqual(a, b)  # 点对角顺序无关

    def test_two_clicks_complete_a_zone(self):
        c = ZoneCollector(rect=True)
        self.assertEqual(c.add(100, 100), "ok")
        self.assertEqual(c.add(300, 260), "ok")
        self.assertTrue(c.is_complete("A"))
        self.assertEqual(c.snapshot()["A"], [[100, 100], [300, 100], [300, 260], [100, 260]])
        self.assertEqual(c.add(10, 10), "full")  # 第三点被拒

    def test_area_and_walk(self):
        c = ZoneCollector(("A", "B"), rect=True)
        c.add(100, 100); c.add(300, 300)
        self.assertAlmostEqual(c.areas()["A"], 200 * 200, places=0)
        self.assertTrue(c.next_zone())
        self.assertEqual(c.current, "B")
        c.add(400, 100); c.add(600, 300)
        self.assertEqual(len(c.snapshot()), 2)
        self.assertTrue(c.all_complete())

    def test_rect_mode_needs_two_points_only(self):
        c = ZoneCollector(rect=True)
        c.add(100, 100)
        self.assertFalse(c.is_complete("A"))
        self.assertFalse(c.next_zone())


class TestCheckZones(unittest.TestCase):
    def good_zones(self):
        # 按 1280x720 排成互不重叠的五块
        return {
            "A": quad(60, 140, 380, 420),
            "B": quad(420, 140, 740, 420),
            "C": quad(780, 140, 1100, 420),
            "D": quad(60, 470, 740, 660),
            "TEMP": quad(780, 470, 1100, 660),
        }

    def test_good_zones_pass(self):
        self.assertEqual(check_zones(self.good_zones(), (1280, 720)), [])

    def test_missing_zone_reported(self):
        zones = self.good_zones()
        zones.pop("TEMP")
        self.assertTrue(any("缺区域" in i for i in check_zones(zones, (1280, 720))))

    def test_too_small_reported(self):
        zones = self.good_zones()
        zones["A"] = quad(100, 100, 130, 130)  # 30x30 = 900 px²
        self.assertTrue(any("面积过小" in i for i in check_zones(zones, (1280, 720))))

    def test_overlap_reported(self):
        zones = self.good_zones()
        zones["B"] = quad(200, 200, 600, 500)  # 压住 A 和 B
        issues = check_zones(zones, (1280, 720))
        self.assertTrue(any("overlap" in i for i in issues), issues)

    def test_out_of_frame_reported(self):
        zones = self.good_zones()
        zones["A"] = quad(-10, 100, 300, 300)
        self.assertTrue(any("超出画面" in i for i in check_zones(zones, (1280, 720))))

    def test_subset_of_zones_allowed(self):
        """台面窄时只标 A/B/C/TEMP 也能通过（不强制五个都标）。"""
        zones = self.good_zones()
        zones.pop("D")
        order = ("A", "B", "C", "TEMP")
        self.assertEqual(check_zones(zones, (1280, 720), required=order), [])
        # 但按全集检查就该报缺 D
        self.assertTrue(any("缺区域" in i for i in check_zones(zones, (1280, 720))))


class TestApplyConfig(unittest.TestCase):
    def test_writes_zones_resolution_and_new_calib_id(self):
        zones = TestCheckZones().good_zones()
        new = apply_to_config(CFG, zones, (1280, 720))
        self.assertEqual(len(new["zones"]), 5)
        self.assertEqual(new["resolution"], [1280, 720])
        self.assertNotEqual(new["calibration_id"], CFG["calibration_id"])
        self.assertTrue(new["calibration_id"].startswith("desktop-"))

    def test_keeps_other_keys_and_does_not_mutate_input(self):
        zones = TestCheckZones().good_zones()
        before = json.dumps(CFG, sort_keys=True)
        new = apply_to_config(CFG, zones, (1280, 720))
        self.assertEqual(json.dumps(CFG, sort_keys=True), before)  # 原对象未被改
        for key in ("colors", "min_area_px", "stability_frames", "confidence"):
            self.assertEqual(new[key], CFG[key])

    def test_resolution_follows_actual_frame(self):
        zones = TestCheckZones().good_zones()
        new = apply_to_config(CFG, zones, (640, 480))
        self.assertEqual(new["resolution"], [640, 480])


if __name__ == "__main__":
    unittest.main(verbosity=2)
