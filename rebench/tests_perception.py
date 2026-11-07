"""perception 的单元测试。不依赖 pytest，直接运行：

    python -m rebench.tests_perception

测试分三组：
  组一 数学正确性   —— 反投影、坐标变换、台面估计
  组二 判据正确性   —— 有/空/遮挡/视野外/未知色
  组三 精度边界     —— 偏移量、主轴角度
"""

import unittest

import numpy as np

from .perception import (
    SceneModel,
    Slot,
    _classify_color,
    _yaw_deg,
    deproject,
    estimate_table_z,
    observe_slots,
    to_base,
)


def make_intrinsics(w=320, h=240, f=380.0):
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])


def empty_scene(z=0.60, w=320, h=240, gray=235):
    depth = np.full((h, w), z)
    color = np.full((h, w, 3), gray, dtype=np.uint8)
    return depth, color


def paint(depth, color, K, cx, cy, half, rgb, z=0.555):
    """在图上画一块正方形（世界坐标 → 像素坐标）。"""
    h, w = depth.shape
    fx, fy = K[0, 0], K[1, 1]
    u0 = int(w / 2 + (cx - half) * fx / 0.60)
    u1 = int(w / 2 + (cx + half) * fx / 0.60)
    v0 = int(h / 2 + (cy - half) * fy / 0.60)
    v1 = int(h / 2 + (cy + half) * fy / 0.60)
    depth[v0:v1, u0:u1] = z
    color[v0:v1, u0:u1] = rgb


class TestMath(unittest.TestCase):
    def test_deproject_center_pixel(self):
        """光心处的像素应反投影到 (0, 0, z)。"""
        K = make_intrinsics()
        depth = np.full((240, 320), 0.5)
        pts = deproject(depth, K)
        cx, cy = int(K[0, 2]), int(K[1, 2])
        np.testing.assert_allclose(pts[cy, cx], [0, 0, 0.5], atol=1e-9)

    def test_deproject_offset_pixel(self):
        """右移一个像素，x 应增加 z/fx。"""
        K = make_intrinsics()
        depth = np.full((240, 320), 0.5)
        pts = deproject(depth, K)
        cx, cy = int(K[0, 2]), int(K[1, 2])
        np.testing.assert_allclose(pts[cy, cx + 10, 0] - pts[cy, cx, 0], 10 * 0.5 / K[0, 0])

    def test_deproject_mm_input_is_caught(self):
        """毫米输入应给出明显离谱的坐标 —— 用来演示单位坑。"""
        K = make_intrinsics()
        depth = np.full((240, 320), 600.0)   # 误把 600mm 当 600m
        pts = deproject(depth, K)
        self.assertGreater(abs(pts[120, 160, 2]), 100.0)

    def test_to_base_translation(self):
        pts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        T = np.eye(4)
        T[:3, 3] = [0.1, 0.2, 0.3]
        out = to_base(pts, T)
        np.testing.assert_allclose(out[0], [0.1, 0.2, 0.3])

    def test_to_base_rotation_90(self):
        """绕 Z 转 90 度：(1,0,0) → (0,1,0)。"""
        T = np.eye(4)
        T[:3, :3] = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        out = to_base(np.array([[1.0, 0.0, 0.0]]), T)
        np.testing.assert_allclose(out[0], [0.0, 1.0, 0.0], atol=1e-9)

    def test_table_z_percentile_not_mean(self):
        """台面上放一块很小的物体，估计值不能被抬高。"""
        rng = np.random.default_rng(0)
        z = rng.normal(0.60, 0.001, 5000)
        z[:200] = 0.555          # 4% 的像素是物体
        pts = np.stack([np.zeros_like(z), np.zeros_like(z), z], axis=1)
        est = estimate_table_z(pts, SceneModel(max_range_m=1.0))
        self.assertAlmostEqual(est, 0.60, delta=0.003)

    def test_table_z_raises_on_empty(self):
        with self.assertRaises(ValueError):
            estimate_table_z(np.zeros((10, 3)), SceneModel())


class TestJudgement(unittest.TestCase):
    def setUp(self):
        self.K = make_intrinsics()
        self.T = np.eye(4)
        self.slots = [
            Slot("tray", cx=0.0, cy=0.0, half_x=0.04, half_y=0.04),
            Slot("box_A", cx=0.20, cy=0.0, half_x=0.04, half_y=0.04),
        ]
        self.model = SceneModel(table_z=0.60)

    def test_found_with_offset(self):
        depth, color = empty_scene()
        paint(depth, color, self.K, 0.01, 0.0, 0.02, (198, 62, 58))
        res = observe_slots(depth, color, self.K, self.T, self.slots, self.model)
        self.assertTrue(res["tray"]["found"])
        self.assertEqual(res["tray"]["color"], "red")
        self.assertAlmostEqual(res["tray"]["dx_mm"], 10.0, delta=4.0)

    def test_empty_slot(self):
        depth, color = empty_scene()
        res = observe_slots(depth, color, self.K, self.T, self.slots, self.model)
        self.assertFalse(res["box_A"]["found"])
        self.assertEqual(res["box_A"]["reason"], "empty")

    def test_occluded_vs_empty_is_distinct(self):
        """遮挡与空必须能分开 —— 这是诚实性的技术落点。"""
        depth, color = empty_scene()
        h, w = depth.shape
        depth[h // 2 - 20:h // 2 + 20, w // 2 - 20:w // 2 + 20] = 0.0   # 挖空 80%
        res = observe_slots(depth, color, self.K, self.T, self.slots, self.model)
        self.assertFalse(res["tray"]["found"])
        self.assertEqual(res["tray"]["reason"], "occluded")
        self.assertEqual(res["box_A"]["reason"], "empty")

    def test_out_of_view(self):
        depth, color = empty_scene()
        far = [Slot("nowhere", cx=5.0, cy=5.0, half_x=0.04, half_y=0.04)]
        res = observe_slots(depth, color, self.K, self.T, far, self.model)
        self.assertEqual(res["nowhere"]["reason"], "out_of_view")

    def test_unknown_color_returns_none(self):
        depth, color = empty_scene()
        paint(depth, color, self.K, 0.0, 0.0, 0.02, (128, 128, 128))
        res = observe_slots(depth, color, self.K, self.T, self.slots, self.model)
        self.assertTrue(res["tray"]["found"])
        self.assertIsNone(res["tray"]["color"])

    def test_flat_texture_not_detected(self):
        """桌面纹理（1mm 起伏）不应被当成物体。"""
        depth, color = empty_scene()
        depth += np.random.default_rng(1).normal(0, 0.001, depth.shape)
        res = observe_slots(depth, color, self.K, self.T, self.slots, self.model)
        self.assertFalse(res["tray"]["found"])


class TestPrecision(unittest.TestCase):
    def test_yaw_of_rotated_rectangle(self):
        ang = np.radians(30.0)
        xs, ys = np.meshgrid(np.linspace(-0.02, 0.02, 40), np.linspace(-0.01, 0.01, 20))
        xy = np.stack([xs.ravel(), ys.ravel()], axis=1)
        R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
        rot = xy @ R.T
        self.assertAlmostEqual(_yaw_deg(rot), 30.0, delta=2.0)

    def test_yaw_is_modulo_90(self):
        """120 度的矩形应折回 30 度。"""
        ang = np.radians(120.0)
        xs, ys = np.meshgrid(np.linspace(-0.02, 0.02, 40), np.linspace(-0.01, 0.01, 20))
        xy = np.stack([xs.ravel(), ys.ravel()], axis=1)
        R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
        self.assertAlmostEqual(_yaw_deg(xy @ R.T), 30.0, delta=2.0)

    def test_color_threshold_behavior(self):
        model = SceneModel()
        self.assertEqual(_classify_color(np.array([198.0, 62.0, 58.0]), model), "red")
        self.assertIsNone(_classify_color(np.array([128.0, 128.0, 128.0]), model))


if __name__ == "__main__":
    unittest.main(verbosity=2)
