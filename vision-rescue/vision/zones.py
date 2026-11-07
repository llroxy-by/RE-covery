"""区域归属：把像素中心点映射到 A/B/C/D/TEMP，判不准就老实说 unknown。

规则（任务书第 5 节第 2 条）：
- 落在多边形外 → unknown；落在两个多边形里（配置重叠）→ unknown；
- 落在边界 margin 以内 → unknown（视觉抖动会让区域来回跳，这条是防抖的第一道闸）；
- 任何 unknown 都带具体原因，下游只能判 uncertain，不能当证据。
"""

from __future__ import annotations

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

UNKNOWN = "unknown"
ALLOWED_ZONES = ("A", "B", "C", "D", "TEMP", "unknown")


class ZoneSet:
    def __init__(self, zones: dict, margin_px: int = 14) -> None:
        if not zones:
            raise ValueError("配置里没有任何区域，无法判断归属")
        self._zones: dict[str, np.ndarray] = {}
        for name, verts in zones.items():
            pts = np.asarray(verts, dtype=np.float32).reshape(-1, 2)
            if pts.shape[0] < 3:
                raise ValueError(f"区域 {name} 少于 3 个顶点")
            self._zones[str(name)] = pts
        self.margin_px = float(margin_px)
        if cv2 is None:
            raise RuntimeError("区域归属依赖 OpenCV（cv2.pointPolygonTest）")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._zones.keys())

    def polygon(self, name: str) -> np.ndarray:
        return self._zones[name]

    # ------------------------------------------------------------ 归属判断
    def assign(self, center) -> tuple[str, list[str], float]:
        """返回 (zone, reasons, 到边界的有符号距离)。

        距离 > 0 表示在多边形内部且离边界还有多少像素。
        """
        if center is None:
            return UNKNOWN, ["no_center"], 0.0
        pt = (float(center[0]), float(center[1]))
        hits = []
        for name, poly in self._zones.items():
            d = cv2.pointPolygonTest(poly, pt, True)
            if d >= 0:
                hits.append((name, float(d)))

        if not hits:
            return UNKNOWN, ["outside_all_zones"], 0.0
        if len(hits) > 1:
            joined = ",".join(sorted(n for n, _ in hits))
            return UNKNOWN, [f"zone_overlap:{joined}"], 0.0

        name, dist = hits[0]
        if dist < self.margin_px:
            return UNKNOWN, [f"zone_boundary_ambiguous:{name}"], dist
        return name, [], dist

    def contains(self, name: str, center) -> bool:
        if center is None:
            return False
        poly = self._zones[name]
        return cv2.pointPolygonTest(poly, (float(center[0]), float(center[1])), False) >= 0

    # ------------------------------------------------------------ 配置自检
    def validate_layout(self, step: int = 8) -> list[str]:
        """启动时自检：区域是否大面积重叠、是否越界。返回问题列表（空 = 合规）。"""
        issues: list[str] = []
        names = list(self._zones)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = self._zones[names[i]], self._zones[names[j]]
                ax0, ay0 = a[:, 0].min(), a[:, 1].min()
                ax1, ay1 = a[:, 0].max(), a[:, 1].max()
                bx0, by0 = b[:, 0].min(), b[:, 1].min()
                bx1, by1 = b[:, 0].max(), b[:, 1].max()
                x0, y0 = max(ax0, bx0), max(ay0, by0)
                x1, y1 = min(ax1, bx1), min(ay1, by1)
                if x1 <= x0 or y1 <= y0:
                    continue
                inter = 0
                for yy in range(int(y0), int(y1) + 1, step):
                    for xx in range(int(x0), int(x1) + 1, step):
                        if (cv2.pointPolygonTest(a, (xx, yy), False) >= 0
                                and cv2.pointPolygonTest(b, (xx, yy), False) >= 0):
                            inter += 1
                if inter > 0:
                    issues.append(f"zones_overlap:{names[i]}&{names[j]}")
        return issues


def validate_layout_of(zones: dict, margin_px: int = 0, step: int = 8) -> list[str]:
    """不构造 ZoneSet 也能查重叠（标定工具在写配置前用）。"""
    return ZoneSet(zones, margin_px).validate_layout(step=step)
