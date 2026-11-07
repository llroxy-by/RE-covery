"""槽位级视觉观测模块。

设计前提（务必先接受这三条，否则会走弯路）：
1. 相机固定在工作位、桌面高度已知 —— 不追求任意场景泛化。
2. 身份靠颜色，位置靠槽位 —— 不做 6D 位姿估计。
3. 输出是"每个槽位有没有东西、偏了多少"，不是图像、不是检测框。

自测：python -m rebench.perception
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class Slot:
    name: str
    cx: float          # 槽位中心 x（base 坐标系，米）
    cy: float
    half_x: float      # 半边长（米）
    half_y: float


@dataclass
class SceneModel:
    table_z: float = 0.0              # 台面高度（base 坐标系，米）
    height_mm: float = 8.0            # 高于台面多少毫米算"有物体"
    min_pixels: int = 120             # 小于该像素数视为没有物体
    min_valid_ratio: float = 0.40     # 槽位内有效深度像素比例低于此值 → 不可观测
    max_range_m: float = 0.60         # 深度有效范围上限
    palette: Dict[str, Tuple[int, int, int]] = field(default_factory=lambda: {
        "red": (198, 62, 58),
        "blue": (58, 92, 190),
        "green": (72, 158, 92),
        "yellow": (218, 186, 72),
    })
    color_tol: float = 85.0           # RGB 欧氏距离阈值，超出判为未知色


def deproject(depth: np.ndarray, K: np.ndarray) -> np.ndarray:
    """深度图 → 相机坐标系点云（米）。depth 无效值应为 0 或 NaN。"""
    h, w = depth.shape
    u, v = np.meshgrid(np.arange(w), np.arange(h))
    z = depth.astype(np.float64)
    x = (u - K[0, 2]) * z / K[0, 0]
    y = (v - K[1, 2]) * z / K[1, 1]
    return np.stack([x, y, z], axis=-1)


def to_base(pts_cam: np.ndarray, T_cam2base: np.ndarray) -> np.ndarray:
    """相机系 → base 系。T 为 4x4 齐次变换。"""
    R = T_cam2base[:3, :3]
    t = T_cam2base[:3, 3]
    return pts_cam.reshape(-1, 3) @ R.T + t


def estimate_table_z(pts_base: np.ndarray, model: SceneModel) -> float:
    """用低分位数估计台面高度，避免被物体抬偏。"""
    z = pts_base[:, 2]
    z = z[np.isfinite(z) & (z > 0) & (z < model.max_range_m + 0.3)]
    if z.size < 100:
        raise ValueError("有效深度点不足，检查相机或内参")
    return float(np.percentile(z, 15))


def _classify_color(rgb: np.ndarray, model: SceneModel) -> Optional[str]:
    best, best_d = None, 1e18
    for name, ref in model.palette.items():
        d = float(np.sum((rgb - np.array(ref, dtype=np.float64)) ** 2))
        if d < best_d:
            best, best_d = name, d
    return best if (best_d < model.color_tol ** 2 and rgb.max() - rgb.min() >= 30.0) else None


def _yaw_deg(xy: np.ndarray) -> float:
    """主轴方向，矩形对称性下折到 0-90 度。"""
    c = xy - xy.mean(axis=0)
    cov = np.cov(c.T)
    if not np.all(np.isfinite(cov)):
        return 0.0
    w, v = np.linalg.eigh(cov)
    major = v[:, int(np.argmax(w))]
    ang = np.degrees(np.arctan2(major[1], major[0])) % 180.0
    return float(ang % 90.0)


def observe_slots(
    depth: np.ndarray,
    color: np.ndarray,
    K: np.ndarray,
    T_cam2base: np.ndarray,
    slots: List[Slot],
    model: SceneModel,
) -> Dict[str, dict]:
    """核心观测函数。返回每个槽位的结构化结果，不含任何图像数据。"""
    pts = to_base(deproject(depth, K), T_cam2base)
    xs, ys, zs = pts[:, 0], pts[:, 1], pts[:, 2]
    rgb = color.reshape(-1, 3).astype(np.float64)

    valid = np.isfinite(zs) & (zs > 0.05) & (zs <= model.max_range_m + 1e-6)
    table_z = model.table_z if model.table_z > 0 else estimate_table_z(pts[valid], model)

    out: Dict[str, dict] = {}
    for s in slots:
        inbox = (
            (np.abs(xs - s.cx) <= s.half_x)
            & (np.abs(ys - s.cy) <= s.half_y)
            & valid
        )
        n_box = int(np.count_nonzero(inbox))
        # 分母必须用"该槽位在台面上应占的像素数"，不能用矩形内的点数：
        # 深度无效的像素会反投影到原点，若原点恰在槽位内，分母会被撑大而误判为遮挡。
        u0 = K[0, 0] * (s.cx - s.half_x) / table_z + K[0, 2]
        u1 = K[0, 0] * (s.cx + s.half_x) / table_z + K[0, 2]
        v0 = K[1, 1] * (s.cy - s.half_y) / table_z + K[1, 2]
        v1 = K[1, 1] * (s.cy + s.half_y) / table_z + K[1, 2]
        if u1 < 0 or u0 > depth.shape[1] or v1 < 0 or v0 > depth.shape[0]:
            out[s.name] = {"found": False, "valid_ratio": 0.0, "reason": "out_of_view"}
            continue
        ratio = min(1.0, n_box / max((u1 - u0) * (v1 - v0), 1.0))
        if ratio < model.min_valid_ratio:
            out[s.name] = {"found": False, "valid_ratio": round(ratio, 3), "reason": "occluded"}
            continue

        height_mm = (table_z - zs[inbox]) * 1000.0
        on_obj = height_mm > model.height_mm
        n_obj = int(np.count_nonzero(on_obj))
        if n_obj < model.min_pixels:
            out[s.name] = {"found": False, "valid_ratio": round(ratio, 3), "reason": "empty"}
            continue

        xy = np.stack([xs[inbox][on_obj], ys[inbox][on_obj]], axis=1)
        cx, cy = xy.mean(axis=0)
        med_rgb = np.median(rgb[inbox][on_obj], axis=0)
        out[s.name] = {
            "found": True,
            "valid_ratio": round(ratio, 3),
            "dx_mm": round(float(cx - s.cx) * 1000.0, 1),
            "dy_mm": round(float(cy - s.cy) * 1000.0, 1),
            "yaw_deg": round(_yaw_deg(xy), 1),
            "color": _classify_color(med_rgb, model),
            "pixels": n_obj,
            "height_mm": round(float(np.median(height_mm[on_obj])), 1),
        }
    return out


def to_observations(bench, slot_report: Dict[str, dict]) -> Dict[str, dict]:
    """槽位级结果 → state.judge() 需要的物体级观测。身份靠颜色匹配。"""
    obs: Dict[str, dict] = {}
    for spec in bench.objects:
        hit = None
        for slot_name, r in slot_report.items():
            if r.get("found") and r.get("color") == spec.color:
                hit = (slot_name, r)
                break
        if hit is None:
            obs[spec.id] = {"slot": None}
        else:
            slot_name, r = hit
            obs[spec.id] = {
                "slot": slot_name,
                "dx_mm": r["dx_mm"],
                "dy_mm": r["dy_mm"],
                "yaw_deg": r["yaw_deg"],
            }
    return obs


def _selftest() -> None:
    """合成场景自测：台面 z=0.60，两个槽位，各放一块积木（高出 45mm）。"""
    h, w = 240, 320
    fx = fy = 380.0
    K = np.array([[fx, 0, w / 2], [0, fy, h / 2], [0, 0, 1.0]])

    depth = np.full((h, w), 0.60)
    color = np.full((h, w, 3), 235, dtype=np.uint8)

    # 台面在 base 系 z=0.60，相机与 base 重合（恒等变换），像素 → 世界近似
    T = np.eye(4)

    def paint(cx, cy, half, rgb):
        u0 = int(w / 2 + cx * fx / 0.60 - half * fx / 0.60)
        u1 = int(w / 2 + cx * fx / 0.60 + half * fx / 0.60)
        v0 = int(h / 2 + cy * fy / 0.60 - half * fy / 0.60)
        v1 = int(h / 2 + cy * fy / 0.60 + half * fy / 0.60)
        depth[v0:v1, u0:u1] = 0.555
        color[v0:v1, u0:u1] = rgb

    paint(-0.10, 0.00, 0.020, (198, 62, 58))   # 红块，偏左槽 5mm
    paint(0.10, 0.02, 0.020, (58, 92, 190))    # 蓝块，偏上 20mm

    slots = [
        Slot("tray", cx=-0.095, cy=0.0, half_x=0.04, half_y=0.04),
        Slot("box_A", cx=0.10, cy=0.0, half_x=0.04, half_y=0.04),
        Slot("box_B", cx=0.0, cy=0.15, half_x=0.04, half_y=0.04),
    ]
    res = observe_slots(depth, color, K, T, slots, SceneModel(table_z=0.60))
    for k, v in res.items():
        print(f"{k:8s} {v}")
    print("\n预期：tray 找到红色且 dx≈-5mm；box_A 找到蓝色且 dy≈+20mm；box_B 为空。")


if __name__ == "__main__":
    _selftest()
