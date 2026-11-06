from datetime import datetime

def validate_snapshot(request_data: dict) -> dict:
    result = {
        "status": "READY",
        "reason_codes": [],
        "message": "数据校验通过。"
    }

    zones = {z["zone_id"]: z for z in request_data.get("zones", [])}
    for z_id, z_data in zones.items():
        if z_data.get("capacity") != 1:
            return {"status": "BLOCKED", "reason_codes": ["UNSUPPORTED_CAPACITY"], "message": f"区域 {z_id} 容量不是 1。"}

    targets = {t["object_id"]: t["target_zone"] for t in request_data.get("targets", [])}
    for obj, t_zone in targets.items():
        if t_zone == "TEMP":
            return {"status": "BLOCKED", "reason_codes": ["INVALID_INPUT"], "message": "目标区域不能是 TEMP。"}
        if t_zone not in zones:
            return {"status": "BLOCKED", "reason_codes": ["INVALID_INPUT"], "message": f"目标区域 {t_zone} 未定义。"}

    snapshot = request_data.get("snapshot", {})
    objects = {o["object_id"]: o for o in snapshot.get("objects", [])}
    zone_states = {zs["zone_id"]: zs for zs in snapshot.get("zone_states", [])}
    
    for z_id, z_state in zone_states.items():
        occ = z_state.get("occupant")
        if occ is not None and z_state.get("status") == "KNOWN":
            if occ not in objects:
                return {"status": "BLOCKED", "reason_codes": ["INCONSISTENT_STATE"], "message": f"区域 {z_id} 里的 {occ} 不在物体列表中。"}
            if objects[occ].get("status") == "KNOWN" and objects[occ].get("current_zone") != z_id:
                return {"status": "BLOCKED", "reason_codes": ["INCONSISTENT_STATE"], "message": f"位置冲突：{occ} 声称在 {objects[occ].get('current_zone')}，但 {z_id} 声称它在这里。"}

    if not snapshot.get("complete", False):
        return {"status": "NEEDS_REVIEW", "reason_codes": ["INCOMPLETE_OBSERVATION"], "message": "观察不完整，需要重新拍摄。"}

    try:
        now_time = datetime.fromisoformat(request_data["now"])
        cap_time = datetime.fromisoformat(snapshot["captured_at"])
        max_age = request_data.get("max_snapshot_age_seconds", 30)
        if (now_time - cap_time).total_seconds() > max_age:
            return {"status": "NEEDS_REVIEW", "reason_codes": ["STALE_SNAPSHOT"], "message": "观察数据已过期，需要重新拍摄。"}
    except Exception as e:
        return {"status": "BLOCKED", "reason_codes": ["INVALID_INPUT"], "message": f"时间格式错误: {str(e)}"}

    for obj_id, obj in objects.items():
        if obj.get("status") != "KNOWN":
            return {"status": "NEEDS_REVIEW", "reason_codes": [f"OBJECT_{obj.get('status')}"], "message": f"物体 {obj_id} 状态不明。"}

    for z_id, z_state in zone_states.items():
        if z_state.get("status") != "KNOWN":
            return {"status": "NEEDS_REVIEW", "reason_codes": ["OBSERVATION_UNCERTAIN"], "message": f"区域 {z_id} 状态不明或未确认空。"}

    if zone_states.get("TEMP", {}).get("occupant") is not None:
        return {"status": "BLOCKED", "reason_codes": ["BUFFER_OCCUPIED"], "message": "TEMP 区域被占用，无法规划。"}

    return result