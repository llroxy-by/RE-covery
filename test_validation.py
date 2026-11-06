# test_validation.py
import pytest
from validation import validate_snapshot

# 正常的样例数据
def get_valid_request():
    return {
        "now": "2026-11-06T10:00:05+08:00",
        "max_snapshot_age_seconds": 30,
        "zones": [
            {"zone_id": "LEFT", "capacity": 1},
            {"zone_id": "RIGHT", "capacity": 1},
            {"zone_id": "TEMP", "capacity": 1}
        ],
        "targets": [
            {"object_id": "obj_1", "target_zone": "RIGHT"}
        ],
        "snapshot": {
            "captured_at": "2026-11-06T10:00:00+08:00",
            "complete": True,
            "objects": [{"object_id": "obj_1", "current_zone": "LEFT", "status": "KNOWN"}],
            "zone_states": [
                {"zone_id": "LEFT", "occupant": "obj_1", "status": "KNOWN"},
                {"zone_id": "RIGHT", "occupant": None, "status": "KNOWN"},
                {"zone_id": "TEMP", "occupant": None, "status": "KNOWN"}
            ]
        }
    }

# 1. 正常情况
def test_ready():
    req = get_valid_request()
    res = validate_snapshot(req)
    assert res["status"] == "READY"

# 2. 容量错误 (BLOCKED)
def test_blocked_capacity():
    req = get_valid_request()
    req["zones"][0]["capacity"] = 2
    res = validate_snapshot(req)
    assert res["status"] == "BLOCKED"
    assert "UNSUPPORTED_CAPACITY" in res["reason_codes"]

# 3. 物体 MISSING (NEEDS_REVIEW)
def test_needs_review_missing():
    req = get_valid_request()
    req["snapshot"]["objects"][0]["status"] = "MISSING"
    req["snapshot"]["objects"][0]["current_zone"] = None
    res = validate_snapshot(req)
    assert res["status"] == "NEEDS_REVIEW"

# 4. 快照过期 (NEEDS_REVIEW)
def test_stale_snapshot():
    req = get_valid_request()
    req["now"] = "2026-11-06T10:05:00+08:00" # 迟到了5分钟，过期了
    res = validate_snapshot(req)
    assert res["status"] == "NEEDS_REVIEW"
    assert "STALE_SNAPSHOT" in res["reason_codes"]