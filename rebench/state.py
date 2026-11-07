"""标准状态 S0 的定义、加载与判定。

设计原则：S0 是离散位姿表，不是基准图像。
每个物体只回答两个问题：现在在哪个槽位、偏差多少毫米。
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


OK = "OK"                    # 已复位
MISPLACED = "MISPLACED"      # 在正确槽位附近，但偏差超容差
WRONG_SLOT = "WRONG_SLOT"    # 在某个槽位，但不是它该在的那个
MISSING = "MISSING"          # 所有可观测槽位都找不到
UNOBSERVABLE = "UNOBSERVABLE"  # 被遮挡 / 相机看不到，不能判定


@dataclass
class ObjSpec:
    id: str
    color: str
    home_slot: str
    width_mm: float          # 夹持方向上的实际宽度，用于算夹爪目标开口
    tol_mm: float = 15.0
    tol_deg: float = 10.0


@dataclass
class Bench:
    slots: Dict[str, dict]
    objects: List[ObjSpec] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "Bench":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        objs = [ObjSpec(**o) for o in raw["objects"]]
        return cls(slots=raw["slots"], objects=objs)

    def get(self, obj_id: str) -> Optional[ObjSpec]:
        for o in self.objects:
            if o.id == obj_id:
                return o
        return None


def judge(bench: Bench, obs: Dict[str, dict]) -> Dict[str, dict]:
    """把一次观测转换成每个物体的四态判定。

    obs 形如 {"block_red": {"slot": "tray", "dx_mm": 4.0, "dy_mm": -2.0, "yaw_deg": 3.0}}
    slot 为 None 表示可见范围内没有找到。
    """
    result = {}
    for spec in bench.objects:
        seen = obs.get(spec.id)
        if seen is None or seen.get("slot") is None:
            result[spec.id] = {"status": UNOBSERVABLE, "detail": "未观测到，需换视角后重判"}
            continue
        if seen["slot"] == "__occluded__":
            result[spec.id] = {"status": UNKNOWN_IF_OCCLUDED, "detail": "槽位被遮挡"}
            continue

        d = ((seen.get("dx_mm", 0.0) ** 2 + seen.get("dy_mm", 0.0) ** 2) ** 0.5)
        dyaw = abs(seen.get("yaw_deg", 0.0))

        if seen["slot"] != spec.home_slot:
            result[spec.id] = {"status": WRONG_SLOT, "detail": f"在 {seen['slot']}，应在 {spec.home_slot}"}
        elif d > spec.tol_mm or dyaw > spec.tol_deg:
            result[spec.id] = {"status": MISPLACED, "detail": f"偏差 {d:.1f} mm / {dyaw:.1f} deg"}
        else:
            result[spec.id] = {"status": OK, "detail": f"偏差 {d:.1f} mm"}
    return result


UNKNOWN_IF_OCCLUDED = UNOBSERVABLE


def needs_action(report: Dict[str, dict]) -> List[str]:
    """需要物理介入的物体，按严重程度排序：先缺的，再错的。"""
    order = {MISSING: 0, WRONG_SLOT: 1, MISPLACED: 2}
    todo = [oid for oid, r in report.items() if r["status"] in order]
    return sorted(todo, key=lambda o: order[report[o]["status"]])
