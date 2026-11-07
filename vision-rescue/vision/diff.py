"""S0/S1 差异比较：只输出 unchanged / misplaced / new / uncertain。

四条不许违反的规则（任务书第 7 节）：
1. 没有 confirmed missing。"S0 有、S1 没检出" → uncertain + not_observed；
2. 双侧可靠才允许下 unchanged / misplaced 结论，置信度取双方最小值；
3. 不认识的色块 → unknown_00N + uncertain，直接阻止验收；
4. 模式 / 来源 / 标定版本 / 分辨率 / 图像引用任一不一致 → 整体 uncertain。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .snapshot import (
    DIFF_SCHEMA,
    PROJECT_ROOT,
    SnapshotError,
    load_schema,
    validate,
)
from .sources import utc_now_iso

STATUSES = ("unchanged", "misplaced", "new", "uncertain")
_PRIORITY = {"unchanged": 0, "misplaced": 1, "new": 2, "uncertain": 3}


def _zone_of(obj: dict | None) -> str:
    if obj is None:
        return "unknown"
    z = obj.get("zone", "unknown")
    return z if z in ("A", "B", "C", "D", "TEMP") else "unknown"


def _reliable(obj: dict, min_conf: float) -> bool:
    return (
        obj.get("visibility") == "visible"
        and _zone_of(obj) != "unknown"
        and obj.get("center_px") is not None
        and float(obj.get("confidence", 0.0)) >= float(min_conf)
    )


def _img_exists(reference: str) -> bool:
    if not reference:
        return False
    p = Path(reference)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.is_file()


def preconditions(baseline: dict, current: dict, check_images: bool = True) -> list[str]:
    """S0/S1 能不能比。返回问题列表（空 = 前提成立）。"""
    issues: list[str] = []
    if baseline.get("mode") != current.get("mode"):
        issues.append("mode_mismatch")
    if baseline.get("source") != current.get("source"):
        issues.append("source_mismatch")
    b_cal, c_cal = baseline.get("calibration", {}), current.get("calibration", {})
    if b_cal.get("id") != c_cal.get("id"):
        issues.append("calibration_mismatch")
    if not b_cal.get("valid") or not c_cal.get("valid"):
        issues.append("calibration_invalid")

    b_img, c_img = baseline.get("image", {}), current.get("image", {})
    if (b_img.get("width"), b_img.get("height")) != (c_img.get("width"), c_img.get("height")):
        issues.append("resolution_mismatch")
    if check_images:
        if not _img_exists(b_img.get("reference", "")):
            issues.append("baseline_image_missing")
        if not _img_exists(c_img.get("reference", "")):
            issues.append("current_image_missing")

    if not baseline.get("quality_ok"):
        issues.append("baseline_quality_not_ok")
    if not current.get("quality_ok"):
        issues.append("current_quality_not_ok")
    if not baseline.get("objects"):
        issues.append("baseline_empty")
    return issues


def compare(baseline: dict, current: dict, *, min_conf: float = 0.8,
            check_images: bool = True) -> dict:
    """逐 ID 比较，返回符合 differences.schema.json 的完整结构。"""
    issues = preconditions(baseline, current, check_images)
    force_uncertain = bool(issues)

    base_map = {o["object_id"]: o for o in baseline.get("objects", [])}
    cur_map = {o["object_id"]: o for o in current.get("objects", [])}
    baseline_complete = bool(baseline.get("quality_ok")) and all(
        _reliable(o, min_conf) for o in base_map.values()
    )

    differences: list[dict] = []
    for oid in sorted(set(base_map) | set(cur_map)):
        b, c = base_map.get(oid), cur_map.get(oid)
        from_zone = _zone_of(b) if b is not None else "unknown"
        to_zone = _zone_of(c) if c is not None else "unknown"
        status = "uncertain"
        reasons: list[str] = []
        conf = 0.0

        if b is not None and c is not None:
            rb, rc = _reliable(b, min_conf), _reliable(c, min_conf)
            b_seen = b.get("visibility") != "unknown" and b.get("center_px") is not None
            c_seen = c.get("visibility") != "unknown" and c.get("center_px") is not None
            conf = min(float(b.get("confidence", 0.0)), float(c.get("confidence", 0.0)))
            if rb and rc:
                if from_zone == to_zone:
                    status = "unchanged"
                else:
                    status = "misplaced"
                    reasons.append("zone_changed")
            else:
                status = "uncertain"
                if not c_seen:
                    # 语义是"S0 有、S1 没看见"，绝不能顺手写成 missing
                    reasons.append("not_observed")
                    reasons.extend("current:" + r for r in (c.get("reasons") or []))
                elif not b_seen:
                    reasons.append("baseline_not_observed")
                    reasons.extend("baseline:" + r for r in (b.get("reasons") or []))
                else:
                    if not rb:
                        reasons.extend("baseline:" + r
                                       for r in (b.get("reasons") or ["low_confidence"]))
                    if not rc:
                        reasons.extend("current:" + r
                                       for r in (c.get("reasons") or ["low_confidence"]))
                    if from_zone == to_zone:
                        reasons.append("both_side_unreliable")

        elif b is not None:  # S0 有、S1 没检出：绝不能说是 missing
            status = "uncertain"
            reasons.append("not_observed")
            conf = float(b.get("confidence", 0.0)) * 0.5

        else:  # 只在 S1 出现
            conf = float(c.get("confidence", 0.0))
            if oid.startswith("unknown_") or (c or {}).get("reasons") and \
                    "unrecognized_object" in (c or {}).get("reasons", []):
                status = "uncertain"
                reasons.append("unrecognized_object")
            elif _reliable(c, min_conf) and baseline_complete:
                status = "new"
                from_zone = "unknown"
            else:
                status = "uncertain"
                reasons.append("identity_or_baseline_unconfirmed")

        if force_uncertain and status != "uncertain":
            reasons.append("precondition_failed:" + issues[0])
            status = "uncertain"

        differences.append({
            "object_id": str(oid),
            "status": status,
            "from_zone": from_zone,
            "to_zone": to_zone,
            "confidence": round(min(1.0, max(0.0, conf)), 3),
            "reasons": reasons,
        })

    if force_uncertain:
        overall = "uncertain"
    elif not differences:
        overall = "uncertain"
        issues.append("no_objects")
    else:
        overall = max((d["status"] for d in differences), key=lambda s: _PRIORITY[s])
        if overall == "unchanged" and any(d["status"] == "uncertain" for d in differences):
            overall = "uncertain"

    return {
        "schema_version": "1.0",
        "baseline_id": str(baseline.get("snapshot_id", "")),
        "current_id": str(current.get("snapshot_id", "")),
        "timestamp": utc_now_iso(),
        "mode": current.get("mode", "mock"),
        "status": overall,
        "issues": issues,
        "differences": differences,
    }


def save_differences(diff: dict, root: Path | str | None = None) -> Path:
    root = Path(root) if root is not None else Path(__file__).resolve().parent / "records"
    root.mkdir(parents=True, exist_ok=True)
    validate(diff, load_schema(DIFF_SCHEMA), "differences")

    path = root / f"diff_{diff['baseline_id'][:8]}_{diff['current_id'][:8]}_{uuid.uuid4().hex[:6]}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def gate_verdict(diff: dict) -> str:
    """给 C 的一句话结论。

    READY     全部 unchanged，可以开始下一轮
    NOT_READY 已确定不合规（有东西被挪动/多出来）
    REVIEW    看不准，必须人来判（存在 uncertain）

    misplaced 是"双侧都可靠才下的结论"，它已经足够判定不能开始，
    不该退化成 REVIEW —— 那会把"明确不行"说成"待复核"，等于把
    判断责任又丢回给人。
    """
    if diff.get("issues"):
        return "NOT_READY"
    statuses = {d["status"] for d in diff.get("differences", [])}
    if statuses == {"unchanged"} and diff.get("status") == "unchanged":
        return "READY"
    if statuses & {"misplaced", "new"}:
        return "NOT_READY"
    return "REVIEW"
