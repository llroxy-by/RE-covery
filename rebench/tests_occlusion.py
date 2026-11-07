"""遮挡记忆 / 盲抓可行性 / 抓取判据 的离线测试。

跑法：python -m unittest rebench.tests_occlusion -v

这些用例刻意围绕四个"会出人命"的判断来写，而不是覆盖率：
  1. 记忆不能凭想象 —— 视线没被挡、深度说空了，就必须认它真的没了；
  2. 盲抓必须有几何依据 —— 障碍物压在正上方时一律拒绝；
  3. 朝向能救场 —— 盒子挡在正前方时，转 90° 手腕应该走得通；
  4. 遮挡下不能宣称"确认抓起" —— 只能到 unverified。
"""

import os
import shutil
import tempfile
import time
import unittest

from .backend import MockBackend
from .occlusion import (Box3D, GripperSpec, WorldModel, assess_pick,
                        check_vertical_approach, OCCLUDED, MISSING, VISIBLE,
                        pick_strategy, suggest_viewpoints)
from .runner import Runner
from .state import Bench

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config", "s0.json")

T0 = 1_000_000.0
CAM = (0.30, 0.00, 1.05)
BLOCK = (0.72, 0.10, 0.735)
# 挡在相机与积木之间（x 更小 = 更靠近机器人/相机），高 15 cm
BOX_FRONT = Box3D(0.48, 0.62, -0.05, 0.25, 0.71, 0.86)
# 直接压在积木正上方
BOX_OVER = Box3D(0.64, 0.80, 0.02, 0.18, 0.60, 0.90)


class TestGeometry(unittest.TestCase):
    def test_segment_hits_only_when_between(self):
        self.assertTrue(BOX_FRONT.segment_hits(CAM, BLOCK))
        # 盒子在目标后面：不算遮挡。否则桌子边缘任何东西都会被判成遮挡。
        behind = Box3D(0.80, 0.95, -0.05, 0.25, 0.71, 0.86)
        self.assertFalse(behind.segment_hits(CAM, BLOCK))
        # 盒子在旁边
        self.assertFalse(Box3D(0.48, 0.62, 0.60, 0.80, 0.71, 0.86)
                         .segment_hits(CAM, BLOCK))

    def test_segment_hits_respects_height(self):
        low = Box3D(0.48, 0.62, -0.05, 0.25, 0.60, 0.68)   # 桌子以下，拦不到视线
        self.assertFalse(low.segment_hits(CAM, BLOCK))

    def test_rect_hits_respects_z_range(self):
        b = Box3D(0.60, 0.70, 0.00, 0.10, 0.80, 0.90)
        self.assertTrue(b.rect_hits(0.65, 0.05, 0.02, 0.02, 0.75, 1.00))
        self.assertFalse(b.rect_hits(0.65, 0.05, 0.02, 0.02, 0.71, 0.78))

    def test_gripper_corridor_is_oriented(self):
        """平行二指两个方向需要的净空完全不同 —— 这是"能抓"的关键。"""
        g = GripperSpec()
        ex0, ey0 = g.corridor_extents(0.040, 0.0)     # 开合沿 x
        ex90, ey90 = g.corridor_extents(0.040, 90.0)  # 开合沿 y
        self.assertGreater(ex0, 0.08, "开合方向至少要半个开度 6cm + 指厚")
        self.assertLess(ey0, 0.05)
        self.assertAlmostEqual(ex0, ey90, places=6)
        self.assertAlmostEqual(ey0, ex90, places=6)


class TestWorldModel(unittest.TestCase):
    def setUp(self):
        self.w = WorldModel(camera_pose=CAM)

    def test_see_records_pose(self):
        o = self.w.see("block_red", BLOCK, slot="box_A", now=T0)
        self.assertEqual(o.state, VISIBLE)
        self.assertEqual(o.pose, BLOCK)
        self.assertEqual(o.seen_frames, 1)

    def test_occluded_does_not_move_memory(self):
        self.w.see("block_red", BLOCK, now=T0)
        self.w.set_obstacles([BOX_FRONT], now=T0 + 5)
        st = self.w.resolve("block_red", slot_reason="occluded", now=T0 + 5)
        self.assertEqual(st, OCCLUDED)
        self.assertEqual(self.w.objects["block_red"].pose, BLOCK,
                         "遮挡绝不能改写记忆位置")

    def test_memory_can_be_overruled(self):
        """视线没被挡、深度说这块空了 → 必须承认东西真的没了。

        记忆如果不能被推翻，机器人就会对着一块早被人拿走的积木反复盲抓。
        """
        self.w.see("block_red", BLOCK, now=T0)
        self.w.set_obstacles([], now=T0 + 5)
        st = self.w.resolve("block_red", slot_reason="empty", now=T0 + 5)
        self.assertEqual(st, MISSING)

    def test_depth_alone_can_prove_occlusion(self):
        """深度层说这个槽位被挡住（valid_ratio 低），即使没有障碍物盒也算。"""
        self.w.see("block_red", BLOCK, now=T0)
        self.assertEqual(self.w.resolve("block_red", slot_reason="occluded", now=T0),
                         OCCLUDED)

    def test_no_memory_never_claims_occlusion(self):
        self.w.set_obstacles([BOX_FRONT])
        self.assertEqual(self.w.resolve("never_seen", slot_reason="occluded", now=T0),
                         MISSING)

    def test_memory_expires(self):
        self.w.see("block_red", BLOCK, now=T0)
        self.w.set_obstacles([BOX_FRONT])
        self.assertEqual(self.w.resolve("block_red", "occluded", now=T0 + 400), MISSING)

    def test_target_quality_levels(self):
        self.w.see("block_red", BLOCK, now=T0)
        self.assertEqual(self.w.target("block_red", now=T0 + 1)["quality"], "fresh")
        self.assertEqual(self.w.target("block_red", now=T0 + 60)["quality"], "stale")
        self.assertEqual(self.w.target("block_red", now=T0 + 999)["quality"], "expired")
        self.assertFalse(self.w.target("block_red", now=T0 + 999)["usable"])

    def test_uncertainty_grows_and_caps(self):
        self.w.see("block_red", BLOCK, now=T0)
        u1 = self.w.target("block_red", now=T0 + 1)["uncertainty_m"]
        u2 = self.w.target("block_red", now=T0 + 10)["uncertainty_m"]
        self.assertLess(u1, u2)
        self.assertLessEqual(u2, 0.036 + 1e-9, "不确定度不能无限长")

    def test_blocked_by_identifies_obstacle(self):
        self.w.see("block_red", BLOCK, now=T0)
        self.w.set_obstacles([Box3D(0.0, 0.1, 0, 0.1, 0, 0.1), BOX_FRONT])
        self.assertEqual(self.w.blocked_by("block_red"), 1)


class TestFeasibility(unittest.TestCase):
    def test_clear_when_nothing_above(self):
        r = check_vertical_approach(BLOCK, [], obj_width_m=0.040)
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "clear")

    def test_front_box_is_passable_by_rotating(self):
        """盒子挡在正前方 10 cm：默认朝向被挡，转 90° 就能下去。

        这是"不被干扰还能抓"的核心：挡住视线 ≠ 挡住下降通道，
        只要手指平行于盒子侧壁，垂直下降是通的。
        """
        no_rot = check_vertical_approach(BLOCK, [BOX_FRONT], obj_width_m=0.040,
                                         try_rotations=False)
        self.assertFalse(no_rot["ok"], "默认开合方向（沿 x）应该撞到盒子")
        rot = check_vertical_approach(BLOCK, [BOX_FRONT], obj_width_m=0.040)
        self.assertTrue(rot["ok"])
        self.assertTrue(rot["rotated"])
        self.assertIn(rot["gripper_yaw_deg"], (45.0, 90.0, 135.0))

    def test_box_directly_above_always_refused(self):
        """压在正上方时就该拒绝 —— 规划器坏了、没有碰撞检测，撞了没人拦。"""
        for yaw in (0.0, 45.0, 90.0, 135.0):
            r = check_vertical_approach(BLOCK, [BOX_OVER], obj_width_m=0.040,
                                        gripper_yaw_deg=yaw)
            self.assertFalse(r["ok"], f"yaw={yaw} 居然放行了")
            self.assertEqual(r["reason"], "corridor_blocked")

    def test_refusal_reports_which_obstacle(self):
        r = check_vertical_approach(BLOCK, [BOX_FRONT, BOX_OVER], obj_width_m=0.040)
        self.assertFalse(r["ok"])
        self.assertEqual(r["obstacle"], 1)
        self.assertTrue(r["message"])

    def test_tight_clearance_is_warned_not_refused(self):
        r = check_vertical_approach(BLOCK, [BOX_FRONT], obj_width_m=0.040)
        self.assertTrue(r["ok"])
        self.assertIn(r["reason"], ("clear", "tight_clearance"))


class TestAssessPick(unittest.TestCase):
    def test_confirmed(self):
        v = assess_pick(width_after=0.034, expected_width_m=0.034,
                        area_visible=True, slot_after=None)
        self.assertEqual(v["verdict"], "ok")

    def test_unverified_when_area_not_visible(self):
        """遮挡下：开口对了也不能说"确认抓起"，只能说"夹住了但看不见"。"""
        v = assess_pick(width_after=0.034, expected_width_m=0.034,
                        area_visible=False, slot_after=None)
        self.assertTrue(v["ok"])
        self.assertEqual(v["verdict"], "unverified")

    def test_empty_grasp_fails(self):
        v = assess_pick(width_after=0.001, expected_width_m=0.034,
                        area_visible=True, slot_after=None)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "width_mismatch")

    def test_holding_something_else_fails(self):
        """宽度像 4cm 但物体还在桌上 —— 夹的是别的东西（比如盒壁）。"""
        v = assess_pick(width_after=0.034, expected_width_m=0.034,
                        area_visible=True, slot_after="tray")
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "still_on_table")


class TestStrategy(unittest.TestCase):
    def _tgt(self, quality="fresh", age=1.0):
        return {"usable": True, "quality": quality, "pose": BLOCK,
                "age_s": age, "uncertainty_m": 0.005}

    def test_fresh_and_clear_means_blind_pick(self):
        feas = check_vertical_approach(BLOCK, [BOX_FRONT], obj_width_m=0.04)
        d = pick_strategy(self._tgt(), feas)
        self.assertEqual(d["action"], "blind_pick")

    def test_fresh_but_blocked_refuses(self):
        feas = check_vertical_approach(BLOCK, [BOX_OVER], obj_width_m=0.04)
        d = pick_strategy(self._tgt(), feas)
        self.assertEqual(d["action"], "refuse")
        self.assertEqual(d["reason"], "corridor_blocked")

    def test_stale_requires_reobserve(self):
        feas = check_vertical_approach(BLOCK, [], obj_width_m=0.04)
        d = pick_strategy(self._tgt("stale", 60.0), feas)
        self.assertEqual(d["action"], "reobserve")

    def test_expired_refuses(self):
        d = pick_strategy({"usable": False, "quality": "expired"}, {"ok": True})
        self.assertEqual(d["action"], "refuse")

    def test_no_memory_refuses(self):
        self.assertEqual(pick_strategy(None, {"ok": True})["action"], "refuse")


class TestReobserve(unittest.TestCase):
    def test_suggests_unblocked_viewpoint_first(self):
        vs = suggest_viewpoints(BLOCK, [BOX_FRONT], CAM)
        self.assertTrue(vs)
        self.assertFalse(vs[0]["blocked"], "第一个候选应该是能看见的")
        self.assertTrue(any(c["blocked"] for c in vs),
                        "应该也能算出哪些候选仍被挡（否则判据没起作用）")


class TestRunnerOcclusion(unittest.TestCase):
    """端到端：MockBackend 模拟"有人把盒子放在积木前面"。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bench = Bench.load(CONFIG)
        self.backend = MockBackend(self.bench, grasp_fail_rate=0.0, seed=1)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _runner(self, memory: bool = True) -> Runner:
        return Runner(self.backend, self.bench,
                      ckpt_path=os.path.join(self.dir, "state.json"),
                      log_path=os.path.join(self.dir, "run.jsonl"),
                      mock=True, occlusion_memory=memory)

    def test_occluded_object_still_gets_reset(self):
        r = self._runner()
        seen = r.warm_memory()
        self.assertIn("block_red", seen, "先看一眼应该记下位置")
        self.backend.place_obstacle("block_red")

        before = self.backend.slots["block_red"]
        res = r.run_round()
        self.assertTrue(res["ready"], res["report"])
        self.assertEqual(self.backend.slots["block_red"],
                         self.bench.get("block_red").home_slot,
                         f"应该被复位（原在 {before}）")

    def test_without_memory_it_is_silently_abandoned(self):
        """对照组：关掉记忆就会静默放弃 —— 这就是改之前的行为。"""
        r = self._runner(memory=False)
        r.warm_memory()
        self.backend.place_obstacle("block_red")
        res = r.run_round()
        self.assertFalse(res["ready"])
        self.assertEqual(res["report"]["block_red"]["status"], "UNOBSERVABLE")

    def test_blocked_corridor_refuses_instead_of_crashing(self):
        """盒子压在积木正上方：应该拒绝，而不是硬抓。"""
        r = self._runner()
        r.warm_memory()
        slot = self.backend.slots["block_red"]
        cx, cy = self.backend.centers[slot]
        self.backend.obstacles.append(
            Box3D(cx - 0.08, cx + 0.08, cy - 0.08, cy + 0.08, 0.60, 0.95))
        self.backend.occluded.add("block_red")

        res = r.run_round()
        self.assertFalse(res["ready"], "走廊被占时不能报 READY")
        self.assertNotEqual(self.backend.slots["block_red"],
                            self.bench.get("block_red").home_slot,
                            "不许把积木挪走")
        # 不应产生任何盲抓动作
        import json
        with open(os.path.join(self.dir, "run.jsonl"), encoding="utf-8") as f:
            evs = [json.loads(x) for x in f]
        self.assertFalse([e for e in evs if e.get("blind") is True],
                         "走廊被占时不该发生盲抓")

    def test_memory_is_not_updated_while_occluded(self):
        r = self._runner()
        r.warm_memory()
        remembered = r.world.objects["block_red"].pose
        self.backend.place_obstacle("block_red")
        r.run_round()
        self.assertEqual(r.world.objects["block_red"].pose, remembered,
                         "遮挡期间位置记忆必须保持不变")


if __name__ == "__main__":
    unittest.main(verbosity=2)
