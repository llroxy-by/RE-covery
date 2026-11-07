"""遮挡下的位置记忆 · 盲抓可行性 · 抓取判据修正。

本次场景：积木**本身不动**，但在它前面放了一个盒子，把腕部相机的视线挡住。
要求是"不被干扰、记得原位、照样抓起来"。

四条设计判断，先摆在这里，因为它们决定了下面每个函数的存在：

1. **不能靠"还看得见"来抓。** 遮挡一旦发生，任何以当前帧为准的策略都会把目标
   判成"不见了"：现有链路里 `observe()` 返回 `slot=None` → `judge()` 给
   `UNOBSERVABLE` → `needs_action()` 只收 MISSING/WRONG_SLOT/MISPLACED →
   待复位队列里根本没有它。于是系统"什么都没做"，看起来像"没被干扰"，其实是
   放弃了。必须有一个跨帧的三维世界模型。

2. **也不能无条件盲抓。** 规划器坏了、**没有任何碰撞检测**（任务书 §3.3），
   盲抓等于把 0.19 m 长的手指往一个不知道有没有东西的位置捅。所以盲抓之前必须
   做几何可行性判断：目标正上方那条垂直走廊里有没有障碍物。挡了就拒绝，
   并且要说清楚挡在哪、要退多少。

3. **"抬升后原位必须变空"这条判据在遮挡下会退化。** 槽位本来就看不见，
   `seen["slot"] is None` 恒为真 —— 夹空了也会被当成"原位已空，抓成功"。
   必须把"确认抓起来了"和"夹住了但看不见"分开，后者只能算 unverified。

4. **优先重观测，其次才盲抓。** 腕部相机装在手上（任务书 §4.2），
   手稍微挪一下就能绕开盒子重新看到目标。能看见就不该盲抓 —— 盲抓是最后的
   手段，不是首选。

单位统一：米 / 秒 / `base_link` 坐标系；角度用度。全程只用标准库，
不引入新依赖（机器人侧现成能跑的东西越少越好）。

自测：python -m rebench.occlusion
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------- 常量
# 以下数值全部来自任务书 §2.3 的真机实测，不要按理论值改：
TABLE_Z_DEFAULT = 0.71          # 桌面高度，实测在 0.695–0.716 之间浮动
GRIPPER_MAX_OPEN = 0.12         # 平行二指最大开度
FINGER_THICKNESS = 0.02         # 单指厚度
FINGER_LENGTH = 0.189           # 安装帧 → 指尖，按 0.176 算会低 1.3 cm
WRIST_CAM_OFFSET = 0.07         # 腕部相机偏离工具轴，画面中心 ≠ 夹爪正下方
DEPTH_NOISE = 0.012             # 6–8 帧时域中值后的逐像素噪声量级

# 记忆的信任分级。分两档而不是一刀切，因为"还记得东西在那儿"和
# "敢不敢不看它就下手"是两件不同的事，混成一个阈值会导致要么乱抓、
# 要么明明刚看见过还要求重观测。
FRESH_S = 30.0                  # 这个时间内：允许直接盲抓
MEMORY_TTL_S = 300.0            # 超过：认为真的不见了，标记 missing

VISIBLE, OCCLUDED, MISSING = "visible", "occluded", "missing"


# ----------------------------------------------------------------- 几何
@dataclass(frozen=True)
class Box3D:
    """轴对齐盒子（base_link，米）。障碍物、容器、目标包围盒都用它。"""

    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float

    @property
    def center(self) -> Tuple[float, float, float]:
        return ((self.x0 + self.x1) / 2.0,
                (self.y0 + self.y1) / 2.0,
                (self.z0 + self.z1) / 2.0)

    @property
    def size(self) -> Tuple[float, float, float]:
        return (self.x1 - self.x0, self.y1 - self.y0, self.z1 - self.z0)

    def segment_hits(self, a: Sequence[float], b: Sequence[float],
                     pad: float = 0.0) -> bool:
        """线段 a→b 是否穿过这个盒子（slab 方法，pad 为向外膨胀量）。

        用来回答"盒子挡没挡住相机看向目标的那条视线"。注意是**线段**不是射线：
        盒子在目标后面时不算遮挡，否则桌子边缘任何东西都会被判成遮挡。
        """
        lo = (self.x0 - pad, self.y0 - pad, self.z0 - pad)
        hi = (self.x1 + pad, self.y1 + pad, self.z1 + pad)
        t_min, t_max = 0.0, 1.0
        for i in range(3):
            d = b[i] - a[i]
            if abs(d) < 1e-9:
                if a[i] < lo[i] or a[i] > hi[i]:
                    return False
                continue
            t1 = (lo[i] - a[i]) / d
            t2 = (hi[i] - a[i]) / d
            if t1 > t2:
                t1, t2 = t2, t1
            t_min = max(t_min, t1)
            t_max = min(t_max, t2)
            if t_min > t_max:
                return False
        return True

    def rect_hits(self, cx: float, cy: float, ex: float, ey: float,
                  z0: float, z1: float) -> bool:
        """以 (cx,cy) 为中心、半边长 (ex,ey) 的**矩形**柱，是否与盒子相交。

        为什么是矩形不是圆柱：平行二指是**有朝向**的。开合方向要留出
        半个开度 + 指厚（约 8 cm），垂直于开合方向只要留指片本身的宽度
        （约 1.5 cm）。用圆柱会把"盒子挡在正前方 10 cm"这种情况也判成
        不可抓 —— 其实把手腕转 90°、让手指平行于盒子侧壁就能顺利下去。
        """
        if self.z1 < z0 or self.z0 > z1:
            return False
        return (self.x0 <= cx + ex and self.x1 >= cx - ex
                and self.y0 <= cy + ey and self.y1 >= cy - ey)

    def xy_distance_to(self, x: float, y: float) -> float:
        """点到盒子 xy 投影的最近距离（在盒内为 0）。"""
        dx = max(self.x0 - x, 0.0, x - self.x1)
        dy = max(self.y0 - y, 0.0, y - self.y1)
        return math.hypot(dx, dy)


# ----------------------------------------------------------------- 世界模型
@dataclass
class TrackedObject:
    """一个物体的跨帧记忆。位置只在**看见**的时候更新。"""

    id: str
    pose: Optional[Tuple[float, float, float]] = None   # 最后一次看见的三维位置
    last_seen_ts: float = 0.0
    first_seen_ts: float = 0.0
    seen_frames: int = 0
    state: str = MISSING
    occluder: str = ""                                  # 谁挡住的（诊断用）
    slot: Optional[str] = None

    def age_s(self, now: float) -> float:
        if self.pose is None:
            return float("inf")
        return max(0.0, now - self.last_seen_ts)


@dataclass
class WorldModel:
    """跨帧世界模型：物体的位置记忆 + 当前看到的障碍物。

    它只做三件事：记住看见过的位置、判断"看不见"是遮挡还是真没了、
    给出一个可以拿去抓的目标位姿（带不确定度）。
    """

    table_z: float = TABLE_Z_DEFAULT
    fresh_s: float = FRESH_S
    memory_ttl_s: float = MEMORY_TTL_S
    objects: dict = field(default_factory=dict)
    obstacles: List[Box3D] = field(default_factory=list)
    camera_pose: Optional[Tuple[float, float, float]] = None
    obstacle_ts: float = 0.0

    # ---------------------------------------------------------- 记忆更新
    def track(self, obj_id: str) -> TrackedObject:
        if obj_id not in self.objects:
            self.objects[obj_id] = TrackedObject(id=obj_id)
        return self.objects[obj_id]

    def see(self, obj_id: str, pose: Sequence[float],
            slot: Optional[str] = None, now: Optional[float] = None) -> TrackedObject:
        """这一帧看见了这个物体 —— 刷新记忆。**只有这里会更新 position。**"""
        now = time.time() if now is None else now
        o = self.track(obj_id)
        o.pose = (float(pose[0]), float(pose[1]), float(pose[2]))
        o.last_seen_ts = now
        if o.first_seen_ts <= 0.0:
            o.first_seen_ts = now
        o.seen_frames += 1
        o.state = VISIBLE
        o.occluder = ""
        if slot is not None:
            o.slot = slot
        return o

    def set_obstacles(self, obstacles: Iterable[Box3D],
                      camera_pose: Optional[Sequence[float]] = None,
                      now: Optional[float] = None) -> None:
        self.obstacles = list(obstacles)
        self.obstacle_ts = time.time() if now is None else now
        if camera_pose is not None:
            self.camera_pose = (float(camera_pose[0]), float(camera_pose[1]),
                                float(camera_pose[2]))

    # ---------------------------------------------------------- 遮挡推理
    def blocked_by(self, obj_id: str) -> Optional[int]:
        """相机看向这个物体记忆位置的视线，被哪个障碍物挡住了？返回索引。"""
        o = self.objects.get(obj_id)
        if o is None or o.pose is None or self.camera_pose is None:
            return None
        for i, b in enumerate(self.obstacles):
            if b.segment_hits(self.camera_pose, o.pose):
                return i
        return None

    def resolve(self, obj_id: str, slot_reason: Optional[str] = None,
                now: Optional[float] = None) -> str:
        """这一帧没看见它 —— 是遮挡，还是真不见了？

        证据三条，前一条是必要条件：
          E1 有历史位置（从来没看见过的东西谈不上"遮挡"）
          E2 视线被挡 **或** 感知层说这个槽位被遮挡（`valid_ratio` 低）
          E3 记忆还没过期

        三条都成立才判 occluded。只有 E1 成立（比如视线没被挡、槽位读数是
        empty）就是真的没了 —— 不能因为"我们记得它"就永远认为它在。
        这一条很关键：**记忆必须允许被推翻**，否则机器人会对着一块早被人
        拿走的积木反复盲抓。
        """
        now = time.time() if now is None else now
        o = self.track(obj_id)
        has_mem = o.pose is not None
        idx = self.blocked_by(obj_id)
        depth_says_occluded = slot_reason == "occluded"
        fresh_enough = has_mem and o.age_s(now) <= self.memory_ttl_s

        if has_mem and fresh_enough and (idx is not None or depth_says_occluded):
            o.state = OCCLUDED
            o.occluder = f"obstacle_{idx}" if idx is not None else "depth"
        else:
            o.state = MISSING if not fresh_enough else MISSING
            o.occluder = ""
        return o.state

    def forget(self, obj_id: str) -> None:
        self.objects.pop(obj_id, None)

    # ---------------------------------------------------------- 目标位姿
    def target(self, obj_id: str, now: Optional[float] = None) -> Optional[dict]:
        """给出可以直接拿去抓的目标。不可用时返回 None 并说明原因。

        `quality` 三档：
          fresh   ≤ fresh_s，允许盲抓（手边刚好还"记得很新"）
          stale   记忆还在但不敢盲抓，应先把相机挪过去重看一眼
          expired 超过 memory_ttl_s，不再相信，按 missing 处理
        """
        now = time.time() if now is None else now
        o = self.objects.get(obj_id)
        if o is None or o.pose is None:
            return None
        age = o.age_s(now)
        if age > self.memory_ttl_s:
            return {"usable": False, "quality": "expired", "age_s": round(age, 1),
                    "reason": f"记忆已过期（{age:.0f}s > {self.memory_ttl_s:.0f}s）"}
        quality = "fresh" if age <= self.fresh_s else "stale"
        return {
            "usable": True,
            "quality": quality,
            "pose": o.pose,
            "age_s": round(age, 1),
            # 位置不确定度按时间线性长，封顶。参照实测深度噪声 12 mm，
            # 取 2 mm/s 的漂移假设（人是可能碰一下的），上限取 3 个噪声宽度。
            "uncertainty_m": round(min(3.0 * DEPTH_NOISE, 0.002 * age), 3),
            "state": o.state,
            "occluder": o.occluder,
            "seen_frames": o.seen_frames,
        }


# ----------------------------------------------------------------- 抓手规格
@dataclass(frozen=True)
class GripperSpec:
    max_open_m: float = GRIPPER_MAX_OPEN
    finger_thickness_m: float = FINGER_THICKNESS
    finger_length_m: float = FINGER_LENGTH
    finger_width_m: float = 0.025      # 单根指片垂直于开合方向的宽度
    safety_margin_m: float = 0.02      # 走廊四周净空

    def corridor_extents(self, obj_width_m: float,
                         gripper_yaw_deg: float = 0.0) -> Tuple[float, float]:
        """垂直走廊在 xy 上的半边长 (ex, ey)，随手腕朝向变化。

        平行二指在两个方向上需要的净空完全不同：
          · 开合方向：半个开度（目标 4 cm 时仍按最大 6 cm 算）+ 指厚 2 cm ≈ 8 cm
            —— 手指要张得开才夹得住，这一侧省不了；
          · 垂直方向：只需指片自身宽度的一半 ≈ 1.2 cm。

        所以"盒子挡在正前方 10 cm"这种情况，只要把手腕转 90°、让手指平行于
        盒子侧壁，垂直下降就撞不到 —— 这是**能不撞又抓得到**的关键，
        不是宽松容差。gripper_yaw_deg=0 表示开合方向沿 base 的 x 轴。
        """
        half_gap = max(obj_width_m / 2.0, self.max_open_m / 2.0)
        along = half_gap + self.finger_thickness_m
        across = self.finger_width_m / 2.0
        # 平行二指对 180° 对称，先折到 [0,90] 再插值。
        # 不折的话 yaw=135° 会算出**负的**半边长（0.1025 - 0.07*2 < 0），
        # 判相交时两边同时"通过"，等于把它当成一个无限薄的东西。
        yaw = (gripper_yaw_deg % 180.0 + 180.0) % 180.0
        if yaw > 90.0:
            yaw = 180.0 - yaw
        k = yaw / 90.0
        ex = along + (across - along) * k
        ey = along + (across - along) * (1.0 - k)
        return ex + self.safety_margin_m, ey + self.safety_margin_m


# ----------------------------------------------------------------- 可行性
def check_vertical_approach(target_pose: Sequence[float],
                            obstacles: Sequence[Box3D],
                            obj_width_m: float,
                            grip: GripperSpec = GripperSpec(),
                            safe_z: float = 1.00,
                            table_z: float = TABLE_Z_DEFAULT,
                            gripper_yaw_deg: float = 0.0,
                            try_rotations: bool = True) -> dict:
    """目标正上方那条垂直走廊，能不能走？

    为什么只许垂直：任务书 §4.4/§7.7 —— 低空横移会把东西扫飞、侧向进盒会撞壁，
    而且规划器坏了、**没有任何碰撞检测**。所以抓取路径只能是
    "高空横移到目标正上方 → 原地垂直下降 → 夹 → 垂直抬升"。
    这条路径唯一可能撞的东西，就是正上方那根柱子里有没有障碍物。

    `try_rotations=True` 时，如果当前手腕朝向被挡，会依次试 90°/45°：
    盒子挡在正前方时，把手腕转 90°、让手指平行于盒子侧壁，往往就通了。
    **这不是放宽判据**，走廊仍然是真实几何，只是换了个更合适的进入姿态。
    """
    cx, cy, cz = float(target_pose[0]), float(target_pose[1]), float(target_pose[2])
    z0 = max(cz - 0.02, table_z - 0.01)      # 稍微往下探一点，算上指长
    z1 = safe_z

    def _test(yaw: float) -> Tuple[Optional[int], float, float]:
        ex, ey = grip.corridor_extents(obj_width_m, yaw)
        for i, b in enumerate(obstacles):
            if b.rect_hits(cx, cy, ex, ey, z0, z1):
                return i, ex, ey
        return None, ex, ey

    hitters: List[dict] = []
    # 只试 0/45/90 三个有意义的朝向：平行二指对 180° 对称，135° 等价于 45°
    for yaw in ([gripper_yaw_deg] + ([90.0, 45.0] if try_rotations else [])):
        hit, ex, ey = _test(yaw)
        if hit is None:
            near = min((b.xy_distance_to(cx, cy) for b in obstacles),
                       default=float("inf"))
            tight = near < max(ex, ey) + 0.03
            return {
                "ok": True,
                "reason": "tight_clearance" if tight else "clear",
                "gripper_yaw_deg": round(yaw, 1),
                "rotated": abs(yaw - gripper_yaw_deg) > 1e-6,
                "corridor": {"x": cx, "y": cy, "ex_m": round(ex, 3),
                             "ey_m": round(ey, 3), "z0": round(z0, 3),
                             "z1": round(z1, 3)},
                "nearest_obstacle_m": (None if math.isinf(near)
                                       else round(near, 3)),
                "message": (f"走廊可通行（手腕 {yaw:.0f}°，手指"
                            f"{'平行于障碍物侧壁' if abs(yaw - gripper_yaw_deg) > 1e-6 else '朝向不变'}）"
                            + ("；两侧余量偏小，注意贴壁" if tight else "")),
            }
        hitters.append({"yaw": yaw, "obstacle": hit, "ex": ex, "ey": ey})

    h = hitters[0]
    # 报"主要障碍物"：挡住的朝向数最多的那个（并列时取索引小的）。
    # 不报"第一个挡住默认朝向的"：正前方一个矮盒子可能只挡住默认朝向、
    # 转 90° 就过了，真正让所有朝向都不通的是另一个东西；报错对象会让人
    # 去挪一个挪了也没用的盒子。
    counts: dict = {}
    for x in hitters:
        counts[x["obstacle"]] = counts.get(x["obstacle"], 0) + 1
    dom = max(counts, key=lambda k: (counts[k], -k))
    return {
        "ok": False,
        "reason": "corridor_blocked",
        "obstacle": dom,
        "blocked_once_by": sorted(counts),
        "gripper_yaw_deg": round(gripper_yaw_deg, 1),
        "tried_yaws": [round(x["yaw"], 1) for x in hitters],
        "corridor": {"x": cx, "y": cy, "ex_m": round(h["ex"], 3),
                     "ey_m": round(h["ey"], 3), "z0": round(z0, 3),
                     "z1": round(z1, 3)},
        "message": (f"换个朝向也过不去：障碍物 #{dom} 压在目标正上方"
                    f"（走廊约 {h['ex'] * 200:.0f}×{h['ey'] * 200:.0f} cm，"
                    f"高度 {z0:.2f}–{z1:.2f} m）"
                    + (f"；另被 {sorted(set(counts) - {dom})} 挡过一次"
                       if len(counts) > 1 else "")),
    }


# ----------------------------------------------------------------- 抓取判据
def assess_pick(*,
                width_after: float,
                expected_width_m: float,
                area_visible: bool,
                slot_after: Optional[str],
                width_tol_m: float = 0.015,
                closed_floor_m: float = 0.008) -> dict:
    """夹完抬起来之后，到底算不算抓到了？

    原判据是两条：宽度停在物体尺寸附近 + 抬升后原位变空。第二条在遮挡下
    会退化成"永远为真"——槽位本来就看不见，`slot is None` 不代表空。
    所以这里把"这一片区域到底看得见吗"作为独立前提。

    参数 `area_visible` 问的是**目标所在区域能不能被观测**，不是"物体能不能
    被看见"。两者必须分开：抓起来之后物体当然看不见（它在夹爪里），
    但区域是可见的、空的，这就是一次确认成功的抓取。

      · 宽度对 + 区域可见 + 原位已空   → ok        （确认抓起）
      · 宽度对 + 区域不可见            → unverified（夹住了，但无法确认它离开了原位）
      · 宽度不对                       → failed    （夹空 / 夹到别的东西）
      · 宽度对 + 区域可见 + 原位还有东西 → failed   （夹了别的东西，或没夹起来）

    unverified 不是失败，但**不能拿它去通过验收**（B/C 的规则：不确定必须停）。
    """
    held = (width_after > closed_floor_m
            and abs(width_after - expected_width_m) <= width_tol_m)
    if not held:
        return {"ok": False, "verdict": "failed", "reason": "width_mismatch",
                "detail": (f"开口 {width_after * 100:.1f} cm，"
                           f"目标 {expected_width_m * 100:.1f} cm，判夹空")}
    if not area_visible:
        return {"ok": True, "verdict": "unverified", "reason": "target_still_occluded",
                "detail": "夹住了（开口与目标尺寸吻合），但目标区仍被遮挡，"
                          "无法确认它已离开原位"}
    if slot_after is not None:
        return {"ok": False, "verdict": "failed", "reason": "still_on_table",
                "detail": f"抬升后原位仍有东西（槽位 {slot_after}）"}
    return {"ok": True, "verdict": "ok", "reason": "confirmed",
            "detail": "开口吻合且原位已空"}


# ----------------------------------------------------------------- 重观测
def suggest_viewpoints(target_pose: Sequence[float],
                       obstacles: Sequence[Box3D],
                       camera_pose: Optional[Sequence[float]],
                       offsets_m: Sequence[Tuple[float, float]] = (
                           (0.12, 0.0), (-0.12, 0.0), (0.0, 0.12), (0.0, -0.12),
                           (0.10, 0.10), (0.10, -0.10)),
                       lift_m: float = 0.10) -> List[dict]:
    """把腕部相机挪到哪儿，能重新看见目标？

    腕部相机装在手上，所以"重观测"不是一个额外的相机动作，而是**换个手腕
    位置再拍一张**。这里只做几何判断（新位置到目标那条线是否不再被挡），
    不保证 IK 可达 —— 可达性必须由调用方用 inverse_kinematics 验（任务书
    §3.2：逆解不传当前关节角当 seed 会直接 FAULT）。

    返回按"水平偏移量从小到大"排好的候选，每个带是否真的没被挡。
    """
    if camera_pose is None:
        return []
    cx, cy, cz = camera_pose
    out = []
    for dx, dy in offsets_m:
        cand = (cx + dx, cy + dy, cz + lift_m)
        hit = next((i for i, b in enumerate(obstacles)
                    if b.segment_hits(cand, target_pose)), None)
        out.append({
            "camera_pose": tuple(round(v, 3) for v in cand),
            "delta_m": (round(dx, 3), round(dy, 3), round(lift_m, 3)),
            "blocked": hit is not None,
            "blocked_by": hit,
            "offset_m": round(math.hypot(dx, dy), 3),
        })
    out.sort(key=lambda c: (c["blocked"], c["offset_m"]))
    return out


def pick_strategy(target: dict, feasibility: dict,
                  obstacle_count: int = 0) -> dict:
    """把"记忆状态 + 走廊可行性"合成一个可执行的决策。

    决策表（宁可拒绝，也不要撞）：

      | 记忆     | 走廊     | 动作 |
      |----------|----------|------|
      | fresh    | clear    | 直接盲抓（最快，风险已由走廊检查兜住）|
      | fresh    | blocked  | 拒绝 + 要求挪开障碍物，不允许"绕过去试试" |
      | stale    | clear    | 先把相机挪过去重看一眼，看见了再抓 |
      | stale    | blocked  | 拒绝 |
      | expired  | —        | 当作不存在，不复位该物体 |
    """
    if not target or not target.get("usable"):
        return {"action": "refuse", "reason": "no_memory",
                "message": "没有可用的位置记忆（从未看见或已过期）"}
    q = target["quality"]
    if q == "expired":
        return {"action": "refuse", "reason": "memory_expired",
                "message": "位置记忆已过期，按丢失处理"}
    if not feasibility.get("ok"):
        return {"action": "refuse", "reason": "corridor_blocked",
                "message": feasibility.get("message", "垂直走廊被占"),
                "obstacle": feasibility.get("obstacle")}
    if q == "fresh":
        return {"action": "blind_pick", "reason": "fresh_memory_clear_corridor",
                "message": (f"用 {target['age_s']:.0f}s 前记住的位置直接抓"
                            f"（不确定度 ±{target['uncertainty_m'] * 1000:.0f} mm）")}
    return {"action": "reobserve", "reason": "stale_memory",
            "message": (f"记忆已 {target['age_s']:.0f}s，先把腕部相机挪过去"
                        "确认位置未变，再抓")}


# ----------------------------------------------------------------- 自测
def _selftest() -> None:
    """合成场景：积木在桌面，盒子挡在相机与积木之间。"""
    t0 = 1000.0
    cam = (0.30, 0.00, 1.05)
    block = (0.72, 0.10, 0.735)              # 桌面上、高出桌面 2.5 cm 的积木中心
    box = Box3D(0.48, 0.62, -0.05, 0.25, 0.71, 0.87)   # 挡在相机和目标之间

    w = WorldModel(camera_pose=cam)
    w.see("block_red", block, slot="box_A", now=t0)
    print(f"看见积木 → state={w.objects['block_red'].state} "
          f"pose={w.objects['block_red'].pose}")

    # 盒子放进来，积木这一帧看不见了
    w.set_obstacles([box], camera_pose=cam, now=t0 + 5)
    st = w.resolve("block_red", slot_reason="occluded", now=t0 + 5)
    print(f"盒子挡住 → state={st} occluder={w.objects['block_red'].occluder}")
    print(f"  记忆位置 {w.objects['block_red'].pose}（没有被遮挡影响）")

    tgt = w.target("block_red", now=t0 + 8)
    feas = check_vertical_approach(tgt["pose"], w.obstacles, obj_width_m=0.040)
    dec = pick_strategy(tgt, feas)
    print(f"  目标 {tgt['quality']} 走廊 {feas['reason']} → {dec['action']}")
    print(f"  {dec['message']}")

    # 同一场景，但盒子正好压在积木正上方 → 必须拒绝
    over = Box3D(0.60, 0.85, 0.00, 0.20, 0.60, 0.90)
    feas2 = check_vertical_approach(tgt["pose"], [over], obj_width_m=0.040)
    dec2 = pick_strategy(tgt, feas2)
    print(f"\n盒子压在正上方 → 走廊 {feas2['reason']} → {dec2['action']}")
    print(f"  {dec2['message']}")

    # 遮挡下抬升：宽度对但看不见 → unverified，不能算确认成功
    v = assess_pick(width_after=0.034, expected_width_m=0.040,
                    area_visible=False, slot_after=None)
    print(f"\n遮挡中抬升 → {v['verdict']}：{v['detail']}")

    # 记忆过期
    w2 = WorldModel(camera_pose=cam)
    w2.see("block_red", block, now=t0)
    w2.set_obstacles([box], camera_pose=cam, now=t0)
    print(f"400 秒后 → {w2.resolve('block_red', 'occluded', now=t0 + 400)}"
          f"（记忆过期就该认它丢了，不能一直盲抓）")

    # 重观测建议
    vs = suggest_viewpoints(block, [box], cam)[:2]
    for c in vs:
        print(f"  重观测候选 {c['camera_pose']} 被挡={c['blocked']}")


if __name__ == "__main__":
    _selftest()
