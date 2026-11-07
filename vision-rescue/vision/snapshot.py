"""快照：组装 scene JSON → 按 schema 校验 → 原图/标注图/JSON 一起落盘。

不可变快照 + 校验失败即失败（任务书第 5 节第 6 条）：
先写图、再校验、最后原子写 JSON；任一步失败就清掉本次产物，不留半份证据。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from jsonschema import Draft202012Validator, FormatChecker


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
SCENE_SCHEMA = SCHEMA_DIR / "scene.schema.json"
DIFF_SCHEMA = SCHEMA_DIR / "differences.schema.json"

RECORDS_DIR = Path(__file__).resolve().parent / "records"


class SnapshotError(RuntimeError):
    """快照无法生成/校验/落盘。调用方不得把它当成一次有效观测。"""


# ------------------------------------------------------------------ schema
def load_schema(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return schema


def validate(instance: dict, schema: dict, label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        lines = [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:6]]
        raise SnapshotError(f"{label} 未通过 schema 校验：" + "；".join(lines))


# ------------------------------------------------------------------ 组装
def build_scene(
    *,
    frame,
    objects: list[dict],
    quality_ok: bool,
    issues: list[str],
    calibration_id: str,
    calibration_valid: bool,
) -> dict:
    """按 A/C 统一协议 v1 组装 scene。字段一个不能多，一个不能少。"""
    return {
        "schema_version": "1.0",
        "snapshot_id": str(uuid.uuid4()),
        "timestamp": frame.captured_at,
        "mode": frame.mode,
        "source": frame.source,
        "image": {"reference": "", "width": frame.width, "height": frame.height},
        "calibration": {
            "id": str(calibration_id),
            "valid": bool(calibration_valid),
            "coordinate_frame": "image_pixels",
        },
        "quality_ok": bool(quality_ok),
        "issues": list(issues),
        "objects": list(objects),
    }


def object_record(
    *,
    object_id: str,
    zone: str,
    visibility: str,
    confidence: float,
    center_px,
    reasons: list[str],
) -> dict:
    if visibility == "visible" and center_px is None:
        raise SnapshotError(f"{object_id}: visibility=visible 必须有中心坐标")
    return {
        "object_id": str(object_id),
        "zone": zone,
        "visibility": visibility,
        "confidence": round(float(confidence), 3),
        "center_px": None if center_px is None else [round(float(center_px[0]), 2),
                                                     round(float(center_px[1]), 2)],
        "reasons": list(reasons),
    }


def is_reliable(obj: dict, min_conf: float) -> bool:
    """双侧可靠才允许下 unchanged/misplaced 结论。"""
    return (
        obj.get("visibility") == "visible"
        and obj.get("zone") not in (None, "unknown")
        and float(obj.get("confidence", 0.0)) >= float(min_conf)
        and obj.get("center_px") is not None
    )


def baseline_blockers(scene: dict, min_conf: float) -> list[str]:
    """能不能拿这份快照当 S0 基准。返回的每条都是拒绝理由。"""
    blockers: list[str] = []
    cal = scene.get("calibration", {})
    if not cal.get("valid"):
        blockers.append("calibration_invalid")
    if not scene.get("quality_ok"):
        blockers.append("quality_not_ok")
    objs = scene.get("objects", [])
    if not objs:
        blockers.append("no_objects")
    for obj in objs:
        oid = obj.get("object_id", "?")
        if obj.get("visibility") != "visible":
            blockers.append(f"{oid}:not_visible")
        elif obj.get("zone") == "unknown":
            blockers.append(f"{oid}:zone_unknown")
        elif float(obj.get("confidence", 0.0)) < float(min_conf):
            blockers.append(f"{oid}:low_confidence")
        elif obj.get("reasons"):
            blockers.append(f"{oid}:" + ",".join(obj["reasons"]))
    for issue in scene.get("issues", []):
        blockers.append(f"issue:{issue}")
    return blockers


# ------------------------------------------------------------------ 落盘
class SnapshotStore:
    """写 records/<id>.json + <id>_raw.jpg + <id>_annotated.jpg。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else RECORDS_DIR

    def save(self, scene: dict, raw, annotated) -> dict:
        if cv2 is None:
            raise SnapshotError("未安装 OpenCV，无法写图像")
        self.root.mkdir(parents=True, exist_ok=True)

        sid = scene["snapshot_id"]
        raw_path = self.root / f"{sid}_raw.jpg"
        ann_path = self.root / f"{sid}_annotated.jpg"
        json_path = self.root / f"{sid}.json"
        for p in (raw_path, ann_path, json_path):
            if p.exists():
                raise SnapshotError(f"快照目标已存在，拒绝覆盖：{p}")

        written: list[Path] = []
        try:
            if not cv2.imwrite(str(raw_path), raw):
                raise SnapshotError(f"原图写入失败：{raw_path}")
            written.append(raw_path)
            if not cv2.imwrite(str(ann_path), annotated):
                raise SnapshotError(f"标注图写入失败：{ann_path}")
            written.append(ann_path)

            scene["image"]["reference"] = _rel(raw_path)

            schema = load_schema(SCENE_SCHEMA)
            validate(scene, schema, "scene")

            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(scene, f, ensure_ascii=False, indent=2)
            os.replace(tmp, json_path)
            written.append(json_path)
        except Exception:
            for p in written:
                try:
                    p.unlink()
                except OSError:
                    pass
            raise

        return {"scene": scene, "json": json_path, "raw": raw_path, "annotated": ann_path}


def load_scene(path: Path | str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rel(path: Path) -> str:
    """项目相对路径，避免把个人绝对路径写进交付物。"""
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()
