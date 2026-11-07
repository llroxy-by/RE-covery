"""抓取落点计算：由实测顶面高度推腕部高度，而不是拍一个常数。

教训来源：卡尺首次抓空——薄件（约 1.9cm 高）用固定 mount_z 时，指尖只压到
物体顶面，两指几乎没有包住侧壁，合爪等于夹空气。立方体、瓶子这类厚件不会暴露
这个问题，所以必须按“顶面高度 - 目标抓深”算，而不是按桌面高度加固定值。

约束：指尖离桌至少 12mm（SDK 侧 execute_plan 的硬检查）。当物体较薄、按抓深算出
的指尖低于该下限时，clamp 到下限并给出 clamped 标记与“实际包覆深度”，让调用方
自己决定是否接受（薄件只能接受小包覆，或改抓更厚的部位）。
"""
TOOL_LEN = 0.189        # 安装帧 -> 指尖
MIN_GAP = 0.012         # 指尖离桌硬下限
DEFAULT_DEPTH = 0.025   # 期望指尖落到物体顶面以下的深度


def plan_vertical_grasp(top_z, table_z, tool_len=TOOL_LEN,
                        grip_depth=DEFAULT_DEPTH, min_gap=MIN_GAP):
    """返回垂直抓取的腕部高度与包覆深度。

    top_z     物体顶面高度（实测）
    table_z   目标附近实测桌面高度（不要用别处的旧值）
    """
    if not (0 < tool_len < 0.5):
        raise ValueError('工具长度不合理')
    if table_z <= 0 or top_z <= table_z:
        raise ValueError('顶面必须高于桌面')
    floor_tip = table_z + min_gap
    want_tip = top_z - grip_depth
    tip = max(floor_tip, want_tip)
    clamped = tip > want_tip + 1e-9
    return {
        'mount_z': round(tip + tool_len, 4),
        'tip_z': round(tip, 4),
        'object_h_cm': round((top_z - table_z) * 100, 1),
        'coverage_cm': round((top_z - tip) * 100, 1),   # 指尖压到顶面以下多少
        'clamped': clamped,
        'note': ('薄件：已顶到 12mm 下限，包覆仅 %.1fcm，必要时改抓更厚部位'
                 % ((top_z - tip) * 100)) if clamped else '',
    }


if __name__ == '__main__':
    import json
    import sys
    top, table = float(sys.argv[1]), float(sys.argv[2])
    print(json.dumps(plan_vertical_grasp(top, table), ensure_ascii=False))
