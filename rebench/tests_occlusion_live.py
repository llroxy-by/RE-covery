"""手机遮挡演示的自主检测回归测试（全部使用合成帧，不需要摄像头）。

跑法：python -m unittest rebench.tests_occlusion_live -v
"""

import unittest

import cv2
import numpy as np

from .occlusion import MISSING, OCCLUDED, VISIBLE
from .tools.occlusion_live import BASELINE_FRAMES, OcclusionSession


def jpeg(img):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


class OcclusionLiveTest(unittest.TestCase):
    def setUp(self):
        self.desk = np.full((720, 1280, 3), 110, np.uint8)
        self.color_scene = self.desk.copy()
        cv2.rectangle(self.color_scene, (400, 300), (520, 420),
                      (30, 30, 220), -1)
        cv2.rectangle(self.color_scene, (700, 340), (810, 450),
                      (30, 180, 30), -1)
        self.foreground_scene = self.desk.copy()
        cv2.rectangle(self.foreground_scene, (400, 300), (520, 420),
                      (25, 25, 25), -1)
        cv2.rectangle(self.foreground_scene, (700, 340), (810, 450),
                      (230, 230, 230), -1)

    def session(self, scene, use_color=True):
        s = OcclusionSession(use_color=use_color)
        for _ in range(BASELINE_FRAMES):
            s.frame(jpeg(self.desk))
        result = s.frame(jpeg(scene))
        return s, result

    @staticmethod
    def cover(scene, bottom):
        img = scene.copy()
        cv2.rectangle(img, (330, 240), (600, bottom), (52, 48, 46), -1)
        return img

    def test_color_boxes_cover_entire_objects(self):
        _s, result = self.session(self.color_scene)
        objects = {o["id"]: o for o in result["objects"]}
        self.assertEqual(set(objects), {"block_red_01", "block_green_01"})
        for oid, expected in (("block_red_01", 121), ("block_green_01", 111)):
            x, y, w, h = objects[oid]["bbox"]
            self.assertGreaterEqual(w, expected, "必须框住整块，而不是 72×72 小模板")
            self.assertGreaterEqual(h, expected)
            self.assertLessEqual(w, expected + 5, "允许抗锯齿边缘，但不能扩成大区域")
            self.assertLessEqual(h, expected + 5)
            self.assertEqual(objects[oid]["state"], VISIBLE)

    def test_foreground_auto_ids_occlusion_and_recovery(self):
        s, initial = self.session(self.foreground_scene, use_color=False)
        self.assertEqual(len(initial["objects"]), 2)
        target = next(o for o in initial["objects"] if o["bbox"][0] == 400)
        other = next(o for o in initial["objects"] if o["bbox"][0] == 700)
        self.assertEqual(target["bbox"][2:], [121, 121])
        self.assertEqual(other["bbox"][2:], [111, 111])
        original_ids = {o["id"] for o in initial["objects"]}
        original_pose = tuple(s.world.objects[target["id"]].pose)

        covered = s.frame(jpeg(self.cover(self.foreground_scene, 680)))
        states = {o["id"]: o["state"] for o in covered["objects"]}
        self.assertEqual(states[target["id"]], OCCLUDED)
        self.assertEqual(states[other["id"]], VISIBLE)
        self.assertEqual(set(states), original_ids, "盒子不能被建成第三个幽灵物体")
        self.assertEqual(covered["decisions"][target["id"]]["action"], "blind_pick")
        self.assertEqual(tuple(s.world.objects[target["id"]].pose), original_pose)

        recovered = s.frame(jpeg(self.foreground_scene))
        self.assertEqual({o["id"] for o in recovered["objects"]}, original_ids)
        restored = next(o for o in recovered["objects"] if o["id"] == target["id"])
        self.assertEqual(restored["state"], VISIBLE)
        for a, b in zip(restored["pose"], original_pose):
            self.assertAlmostEqual(a, b, places=3)
        self.assertTrue(any("记忆校验：位置一致" in e["text"] for e in recovered["events"]))

    def test_removed_object_is_missing_not_occluded(self):
        for use_color, scene in ((True, self.color_scene),
                                 (False, self.foreground_scene)):
            with self.subTest(use_color=use_color):
                s, initial = self.session(scene, use_color=use_color)
                self.assertTrue(initial["objects"])
                removed = s.frame(jpeg(self.desk))
                self.assertTrue(all(o["state"] == MISSING for o in removed["objects"]))
                self.assertEqual(removed["decisions"], {},
                                 "空桌不是障碍物，绝不能对已经拿走的目标盲抓")
                self.assertEqual(removed["obstacles"], 0)

    def test_front_near_and_overhead_have_distinct_decisions(self):
        cases = ((680, "blind_pick", "clear"),
                 (520, "blind_pick", "tight_clearance"),
                 (420, "refuse", "corridor_blocked"))
        for bottom, action, corridor in cases:
            with self.subTest(bottom=bottom):
                s, _initial = self.session(self.color_scene)
                result = s.frame(jpeg(self.cover(self.color_scene, bottom)))
                decision = result["decisions"]["block_red_01"]
                self.assertEqual(decision["action"], action)
                self.assertEqual(decision["corridor"], corridor)
                if bottom == 520:
                    self.assertEqual(decision["yaw"], 90.0,
                                     "前方近盒子应通过旋转手腕获得净空")


if __name__ == "__main__":
    unittest.main()
