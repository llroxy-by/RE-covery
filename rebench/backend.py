"""机器人后端抽象层。

MockBackend 让你在没有真机的情况下开发全部流程逻辑；
SdkBackend 是真机封装，带 TODO 标出必须现场核对的调用。

关键约定：抓取成败只由 gripper_width() 判断，不看轨迹返回值。
"""

import random
import time
from dataclasses import dataclass


@dataclass
class GripperReading:
    width_m: float
    force_n: float = 0.0
    is_moving: bool = False


class Backend:
    """所有后端必须实现的接口。"""

    def move_line(self, pose, speed: float) -> bool:
        raise NotImplementedError

    def open_gripper(self, width_m: float, force_n: float) -> None:
        raise NotImplementedError

    def close_gripper(self, target_width_m: float, force_n: float) -> None:
        raise NotImplementedError

    def gripper_width(self) -> GripperReading:
        raise NotImplementedError

    def observe(self) -> dict:
        """返回 {obj_id: {"slot": str|None, "dx_mm": float, "dy_mm": float, "yaw_deg": float}}"""
        raise NotImplementedError

    def observe_world(self) -> dict:
        """带三维位姿与障碍物的观测，供遮挡记忆使用。

        返回：
          {"objects":   {obj_id: {"visible": bool,        # 这一帧看见物体本体了吗
                                  "area_visible": bool,   # 目标所在区域可观测吗
                                  "slot": str|None,       # 该区域当前占用的槽位
                                  "pose": (x,y,z)|None,   # base_link，米
                                  "slot_reason": "occluded"|"empty"|None}},
           "obstacles": [Box3D, ...],
           "camera_pose": (x,y,z)|None}

        `visible` 与 `area_visible` 必须分开：
          · 抓起来之后物体看不见（在夹爪里），但区域是可观测的、空的
            → area_visible=True, visible=False, slot=None，这正是"确认抓起"；
          · 被盒子挡住 → area_visible=False，这时 slot=None **不能**当成"已空"，
            只能算 unverified。混成一个字段会让夹空被误判成抓成功。

        默认实现用 observe() 退化构造（可见但没有三维位置、没有障碍物），
        这样旧后端不改也能跑 —— 只是遮挡记忆拿不到位姿，会自己降级成
        "无记忆"而不是报错。真机要在 SdkBackend 里重写这个方法。
        """
        obs = self.observe()
        objs = {}
        for oid, v in obs.items():
            slot = v.get("slot")
            objs[oid] = {"visible": slot is not None, "slot": slot,
                         "area_visible": True,
                         "pose": None,
                         "slot_reason": None if slot is not None else "empty"}
        return {"objects": objs, "obstacles": [], "camera_pose": None}


class MockBackend(Backend):
    """纯软件模拟：验证状态机、降级阶梯、checkpoint 是否正确。

    刻意保留 15% 抓空率——你必须先看到降级逻辑生效，再上真机。

    这套坐标是**模拟用的**，只为让遮挡逻辑能在没有真机时跑通；
    真机上的数值来自标定，不要拿这里的常数去对现场。
    """

    # 模拟桌面布局（base_link，米）。名字对不上 s0.json 的槽位时回落到网格。
    MOCK_LAYOUT = {
        "box_A": (0.62, -0.18), "box_B": (0.62, 0.00), "box_C": (0.62, 0.18),
        "box_D": (0.78, -0.18), "tray": (0.78, 0.00), "table_free": (0.78, 0.18),
    }
    MOCK_TABLE_Z = 0.71
    MOCK_CAMERA = (0.30, 0.0, 1.05)

    def __init__(self, bench, grasp_fail_rate: float = 0.15, seed: int = 0):
        self.bench = bench
        self.rng = random.Random(seed)
        self.fail_rate = grasp_fail_rate
        self._width = 0.100
        self._holding = None
        self._target = None
        self._offsets = {}
        self.slots = {o.id: o.home_slot for o in bench.objects}
        # 初始扰动：一个被挪到自由区、一个占错槽——模拟真实的测试后现场
        if bench.objects:
            self.slots[bench.objects[0].id] = "table_free"
            if len(bench.objects) > 1:
                self.slots[bench.objects[1].id] = "tray"
        # 遮挡相关状态
        self.occluded: set = set()
        self.obstacles: list = []
        self.camera_pose = self.MOCK_CAMERA
        self.centers = dict(self.MOCK_LAYOUT)
        for i, name in enumerate(bench.slots):
            self.centers.setdefault(name, (0.62 + 0.16 * (i % 3),
                                            -0.18 + 0.18 * (i // 3)))

    def move_line(self, pose, speed: float) -> bool:
        time.sleep(0.01)
        if isinstance(pose, tuple) and len(pose) >= 2 and pose[0] in ("grasp", "grasp_pose"):
            self._target = pose[1] if pose[0] == "grasp" else "memory"
        return True

    def open_gripper(self, width_m: float, force_n: float) -> None:
        self._width = width_m
        self._holding = None
        self._target = None

    def close_gripper(self, target_width_m: float, force_n: float) -> None:
        if self._holding is not None:
            self._width = max(target_width_m, 0.004)
            return
        if self._target is not None and self.rng.random() > self.fail_rate:
            self._width = target_width_m
            self._holding = self._target      # 抓到了，物体离开原位
        else:
            self._width = 0.001               # 抓空：闭合到底

    def gripper_width(self) -> GripperReading:
        return GripperReading(width_m=self._width)

    def observe(self) -> dict:
        obs = {}
        for o in self.bench.objects:
            slot = None if (o.id == self._holding or o.id in self.occluded) else self.slots.get(o.id)
            if o.id not in self._offsets:
                self._offsets[o.id] = (
                    self.rng.uniform(-8, 8),
                    self.rng.uniform(-8, 8),
                    self.rng.uniform(-6, 6),
                )
            dx, dy, dyaw = self._offsets[o.id]
            obs[o.id] = {"slot": slot, "dx_mm": dx, "dy_mm": dy, "yaw_deg": dyaw}
        return obs

    def observe_world(self) -> dict:
        """模拟带三维位姿与障碍物的观测。"""
        from .occlusion import Box3D      # 局部导入，避免无谓的循环依赖风险
        obs = self.observe()
        objs = {}
        for o in self.bench.objects:
            info = obs.get(o.id) or {}
            slot = info.get("slot")
            pose = None
            if slot is not None and slot in self.centers:
                cx, cy = self.centers[slot]
                pose = (cx + info.get("dx_mm", 0.0) / 1000.0,
                        cy + info.get("dy_mm", 0.0) / 1000.0,
                        self.MOCK_TABLE_Z + 0.02)
            reason = None
            if slot is None:
                reason = "occluded" if o.id in self.occluded else "empty"
            objs[o.id] = {"visible": slot is not None, "slot": slot,
                          # 被盒子挡住时整片区域都不可观测；抓着物体时区域仍可见
                          "area_visible": o.id not in self.occluded,
                          "pose": pose, "slot_reason": reason}
        return {"objects": objs, "obstacles": list(self.obstacles),
                "camera_pose": self.camera_pose}

    def place_obstacle(self, before_obj: str = None) -> dict:
        """模拟"有人把盒子放在积木前面"：物体不动、且从视野里消失。

        盒子摆在相机与目标之间（沿 base 的 -x 方向），高度覆盖桌面上方 15 cm，
        所以视线被挡住，但**目标位置本身没变** —— 正是要验的场景。
        """
        from .occlusion import Box3D
        if before_obj is None:
            before_obj = next((o.id for o in self.bench.objects), None)
        slot = self.slots.get(before_obj)
        if slot is None or slot not in self.centers:
            return {"ok": False, "reason": "no_target"}
        cx, cy = self.centers[slot]
        box = Box3D(x0=cx - 0.22, x1=cx - 0.10,
                    y0=cy - 0.12, y1=cy + 0.12,
                    z0=self.MOCK_TABLE_Z, z1=self.MOCK_TABLE_Z + 0.15)
        self.obstacles.append(box)
        self.occluded.add(before_obj)
        return {"ok": True, "obj": before_obj, "obstacle": box,
                "note": f"{before_obj} 仍在 ({cx:.2f},{cy:.2f})，但视线被挡"}

    def simulate_pick(self, obj_id: str, target_slot: str) -> None:
        """仅 mock 使用：模拟放置成功后物体到达目标槽位。"""
        self.slots[obj_id] = target_slot
        self._offsets[obj_id] = (self.rng.uniform(-5, 5), self.rng.uniform(-5, 5), self.rng.uniform(-4, 4))
        if self._holding == obj_id:
            self._holding = None
        # 物体被挪走了，原来挡在它前面的盒子不再遮住它（盒子的坐标没变，
        # 但它本来就是为那个位置摆的；mock 里不做物理，直接解除遮挡）
        self.occluded.discard(obj_id)
        self._target = None

    def perturb(self, n: int = 2) -> list:
        """仅 mock 使用：模拟"有人跑了一轮实验"，随机挪动 n 个物体。"""
        moved = []
        pool = [o.id for o in self.bench.objects]
        other_slots = ["table_free", "tray", "box_C", "box_D"]
        for obj_id in self.rng.sample(pool, min(n, len(pool))):
            self.slots[obj_id] = self.rng.choice(other_slots)
            self._offsets[obj_id] = (self.rng.uniform(-12, 12), self.rng.uniform(-12, 12), self.rng.uniform(-9, 9))
            moved.append(obj_id)
        return moved


class SdkBackend(Backend):
    """真机后端。所有 TODO 处必须按现场实测签名改写，不要照抄文档。"""

    def __init__(self):
        from galbot_sdk.g1 import GalbotRobot, GalbotMotion  # noqa: F401

        self.robot = GalbotRobot()
        if not self.robot.init():
            raise RuntimeError("robot.init() 失败")
        time.sleep(3)
        self.motion = GalbotMotion(self.robot)
        # TODO(现场核对): 用 get_joint_group_names() / get_frame_names() 的实际返回值替换
        self.arm_group = "left_arm"
        self.gripper_group = "left_gripper"
        self.camera_frame = "left_arm_camera_color_optical_frame"

    def move_line(self, pose, speed: float) -> bool:
        # TODO(现场核对): move_line 的实际签名与返回枚举
        return self.motion.move_line(pose, speed=speed) is not None

    def open_gripper(self, width_m: float, force_n: float) -> None:
        self.robot.set_gripper_command(
            width=width_m, speed=0.05, force=force_n,
            is_blocking=True, timeout_s=3.0,
        )

    def close_gripper(self, target_width_m: float, force_n: float) -> None:
        self.robot.set_gripper_command(
            width=target_width_m, speed=0.02, force=force_n,
            is_blocking=True, timeout_s=3.0,
        )

    def gripper_width(self) -> GripperReading:
        # TODO(现场核对): 字段名，以及全开 / 全闭时的零点读数
        st = self.robot.get_gripper_state()
        return GripperReading(
            width_m=float(st.width),
            force_n=float(getattr(st, "force", 0.0)),
        )

    def observe(self) -> dict:
        # TODO: 这一段是你的主要工作量
        # 1) get_rgb_data(SensorType.XXX) / get_depth_data(SensorType.XXX)
        # 2) 用 get_camera_intrinsic() + FK 到 base_link（**不要用 get_sensor_extrinsic**，
        #    臂上相机返回的是静态标称值，不随关节变化）
        # 3) 每个槽位附近做深度平面分割：有没有物体、中心偏移多少毫米
        # 4) 拍不到就返回 slot=None，交给上层走 L3 换视角 / L4 人工
        raise NotImplementedError("感知尚未接入，先跑 --mock 验证流程")

    def observe_world(self) -> dict:
        """真机版：一次性给出三维位姿 + 障碍物 + 相机位姿。

        没实现时退化成基类默认（只有槽位级结果），上层会自动降级成"无记忆"，
        不会崩 —— 但遮挡场景也就抓不了。四步：

        1) 相机位姿：`FK("left_arm_camera_color_optical_frame", "base_link", js)`
        2) 目标三维点：深度图取目标像素块，反投影后用上面的外参转到 base_link，
           再做 6–8 帧时域中值（任务书 §4.5：场内有 100+ 台同型号机器人，
           红外互相干扰，单帧深度不够用）
        3) 障碍物：桌面以上、比台面近的连通块 → 每个块给一个 Box3D
           （xy 包围盒 + z 从台面到块顶）。注意黑桌布会让深度大面积失效，
           目标下面垫白纸。
        4) camera_pose 填第 1 步的结果 —— 遮挡推理要用它算"视线有没有被挡"。
        """
        raise NotImplementedError(
            "SdkBackend.observe_world 未实现：遮挡记忆需要三维位姿与障碍物盒。\n"
            "  不改也能跑（会自动降级成无记忆），但盒子挡住时不会去抓。\n"
            "  实现要点见本函数的注释与 docs/OCCLUSION_MEMORY.md")
