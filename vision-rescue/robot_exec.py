"""机器人侧常驻执行器：一次 init，之后按 JSONL 收发命令。

命令（每行一个 JSON，返回一行 JSON）：
  {"id":1,"cmd":"state"}
  {"id":2,"cmd":"cart","x":0.77,"y":0.16,"z":1.02,"tilt":0}
  {"id":3,"cmd":"joints","q":[7 个关节角]}
  {"id":4,"cmd":"grip","w":0.09,"v":0.06,"eff":3}
  {"id":5,"cmd":"depth"}            -> 返回深度统计（不下发图像）
  {"id":6,"cmd":"quit"}
"""
from __future__ import annotations

import base64
import json
import sys
import time
import secrets

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

import galbot_sdk as g

S = g.SensorType
X_AXIS_BASE = np.array([0.0, 0.0, -1.0])
TOOL_LEN_M = 0.189          # 交接估计值，尚需核对当前工具标定


def q2R(q):
    x, y, z, w = [float(v) for v in q]
    n = (x * x + y * y + z * z + w * w) ** 0.5
    if not np.isfinite(n) or n < 1e-9:
        raise ValueError("无效姿态四元数")
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def R2q(R):
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        s = (tr + 1.0) ** 0.5 * 2
        w, x, y, z = .25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = (1.0 + R[0, 0] - R[1, 1] - R[2, 2]) ** 0.5 * 2
        w, x, y, z = (R[2, 1] - R[1, 2]) / s, .25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = (1.0 + R[1, 1] - R[0, 0] - R[2, 2]) ** 0.5 * 2
        w, x, y, z = (R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, .25 * s, (R[1, 2] + R[2, 1]) / s
    else:
        s = (1.0 + R[2, 2] - R[0, 0] - R[1, 1]) ** 0.5 * 2
        w, x, y, z = (R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, .25 * s
    return [x, y, z, w]


def tool_orientation(tilt_deg: float):
    """工具轴：0° 垂直向下；tilt 为绕 x 轴倾斜，向 -y 方向倒。"""
    th = np.radians(float(tilt_deg))
    xa = np.array([0.0, -np.sin(th), -np.cos(th)])
    xa /= np.linalg.norm(xa)
    ya = np.array([1.0, 0.0, 0.0])
    za = np.cross(xa, ya)
    za /= np.linalg.norm(za)
    ya = np.cross(za, xa)
    return np.stack([xa, ya, za], axis=1)


def camera_ray(u, v, info):
    """Use the delivered image calibration, not the physical lens name."""
    K = np.asarray(info['K'], dtype=float).reshape(3, 3)
    if not np.all(np.isfinite(K)) or K[0, 0] <= 0 or K[1, 1] <= 0:
        raise ValueError('无效相机内参')
    D = np.asarray(info.get('D', []), dtype=float)
    pixel = np.array([[[float(u), float(v)]]])
    model = info.get('distortion_model')
    if model in ('plumb_bob', 'rational_polynomial'):
        xy = cv2.undistortPoints(pixel, K, D if D.size else None)[0, 0]
    elif model == 'equidistant':
        xy = cv2.fisheye.undistortPoints(pixel, K, D)[0, 0]
    else:
        raise ValueError('未知图像畸变模型，不能按镜头名称猜测')
    return np.array([xy[0], xy[1], 1.])


class Exec:
    def __init__(self):
        self._plans = {}
        t0 = time.time()
        self.r = g.GalbotRobot.get_instance(g.MachineType.G1)
        # 只 init 一次，并且直接带深度相机（同进程二次 init 不会真正启用传感器）
        try:
            self.sensor_ok = bool(self.r.init(
                enable_sensor_set={S.LEFT_ARM_DEPTH_CAMERA, S.HEAD_LEFT_CAMERA},
                enable_sync_mode=False))
        except Exception:
            self.sensor_ok = False
            self.r.init(enable_sensor_set=set(), enable_sync_mode=False)
        self.sensor_ready = self.sensor_ok
        self.m = g.GalbotMotion.get_instance(g.MachineType.G1)
        if not self.m.init():
            raise RuntimeError("GalbotMotion init FAILED")
        self.boot_ms = int((time.time() - t0) * 1000)

    # ---------- 基础 ----------
    def joints(self):
        js = self.m.get_chain_joint_state()
        return {k: [round(float(v), 5) for v in val] for k, val in js.items()}

    def mount(self, js=None):
        js = js or self.m.get_chain_joint_state()
        st, mo = self.m.forward_kinematics("left_arm_end_effector_mount_link", "base_link", js)
        if st != g.MotionStatus.SUCCESS or mo is None or len(mo) != 7 or not np.all(np.isfinite(mo)):
            return None
        return np.array(mo[:3]), q2R(mo[3:])

    def state(self):
        js = self.m.get_chain_joint_state()
        mp = self.mount(js)
        gs = self.r.get_gripper_state("left_gripper")
        out = {"ok": True, "joints": {k: [round(float(v), 4) for v in val] for k, val in js.items()},
               "grip": {"width": round(float(gs.width), 4), "effort": round(float(gs.effort), 2),
                        "moving": bool(gs.is_moving),
                        "timestamp_ns": int(getattr(gs, 'timestamp_ns', 0))}}
        if mp is not None:
            p, R = mp
            out["mount"] = [round(float(v), 4) for v in p]
            out["tool_axis"] = [round(float(v), 3) for v in (R @ np.array([1.0, 0, 0]))]
        return out

    # 左臂 D405 实测内参（1280x720），用于接口偶发拿不到 K 时兜底
    K_FALLBACK = [657.416, 0.0, 636.383, 0.0, 656.949, 356.112, 0.0, 0.0, 1.0]

    def ensure_sensor(self):
        """深度相机在启动时就已启用；这里只做可用性检查。"""
        if not self.sensor_ready:
            raise RuntimeError("深度相机未启用（可能被其他进程占用，如观察台 8410）")
        return True

    def _depth(self):
        self.ensure_sensor()
        d = self.r.get_depth_data(S.LEFT_ARM_DEPTH_CAMERA)
        if not d or "width" not in d:
            raise RuntimeError("取不到深度数据（相机被占用或未就绪）")
        return d

    def intrinsics(self):
        try:
            d = self.r.get_camera_intrinsic(S.LEFT_ARM_DEPTH_CAMERA)
            if isinstance(d, dict) and "K" in d and len(d["K"]) >= 6:
                return list(d["K"])
        except Exception:
            pass
        raise RuntimeError("当前相机内参不可用，停止三维定位；不使用历史标定兜底")

    # ---------- 命令 ----------
    def cmd_state(self, req):
        return self.state()

    def cmd_joints(self, req):
        q = [float(v) for v in req["q"]]
        if len(q) != 7 or not np.all(np.isfinite(q)):
            return {"ok": False, "error": "需要七个有限关节角"}
        t0 = time.time()
        st = self.r.set_joint_positions(q, joint_groups=["left_arm"], is_blocking=True,
                                        speed_rad_s=float(req.get("speed", 0.08)),
                                        timeout_s=float(req.get("timeout", 30.0)))
        out = self.state()
        out["ok"] = st == g.ControlStatus.SUCCESS
        out["status"] = str(st).replace("ControlStatus.", "")
        out["move_ms"] = int((time.time() - t0) * 1000)
        return out

    def cmd_cart(self, req):
        return {"ok": False, "error": "使用 ikpath + execute_plan；禁止重新求逆解后直接运动"}

    def cmd_collision(self, req):
        """Read-only health check; this does not execute a trajectory."""
        import galbot_sdk.g1 as gm
        params = gm.Parameter()
        params.set_timeout(5.)
        states = []
        if req.get("explicit"):
            js = self.m.get_chain_joint_state()
            state = gm.RobotStates()
            state.whole_body_joint = [float(v) for k in ("leg", "head", "left_arm", "right_arm") for v in js[k]]
            state.base_state = [0., 0., 0., 0., 0., 0., 1.]
            states.append(state)
        status, pairs = self.m.check_collision_detail(states, False, False, params)
        return {"ok": status == g.MotionStatus.SUCCESS and pairs is not None,
                "status": str(status),
                "collisions": [{"link1": p.link1, "link2": p.link2}
                               for p in (pairs or []) if p.is_collision]}

    def cmd_execute_plan(self, req):
        plan = self._plans.pop(req.get("plan_id"), None)
        if plan is None or time.monotonic() - plan["created"] > 15:
            return {"ok": False, "error": "路径不存在、已使用或已过期"}
        current = self.m.get_chain_joint_state()
        for group, old in plan["start"].items():
            now = current.get(group, [])
            if len(now) != len(old) or not np.all(np.isfinite(now)) or np.max(np.abs(np.array(now) - old)) > np.radians(.5):
                return {"ok": False, "error": "预演后姿态已变化，需要重新预演"}
        speed = float(req.get("speed", .05))
        table = float(req["table_z"])
        if not np.isfinite(speed) or not .01 <= speed <= .12 or not np.isfinite(table):
            return {"ok": False, "error": "速度或桌面高度无效"}
        if plan["min_tip"] < table + .012 or plan["max_delta"] > np.radians(45) or plan["max_dev"] > .08:
            return {"ok": False, "error": "路径超出几何检查范围，需拆分步骤"}
        # The official checker must succeed; FAULT is not permission to bypass it.
        import galbot_sdk.g1 as gm
        states = []
        for sample in plan["samples"]:
            state = gm.RobotStates()
            state.whole_body_joint = [float(v) for k in ("leg", "head", "left_arm", "right_arm") for v in sample[k]]
            state.base_state = [0., 0., 0., 0., 0., 0., 1.]
            states.append(state)
        params = gm.Parameter()
        params.set_timeout(5.)
        status, pairs = self.m.check_collision_detail(states, False, False, params)
        if status != g.MotionStatus.SUCCESS or pairs is None or any(p.is_collision for p in pairs):
            return {"ok": False, "error": "碰撞检查失败或发现碰撞", "status": str(status)}
        current = self.m.get_chain_joint_state()
        if time.monotonic() - plan["created"] > 15 or any(
                len(current.get(k, [])) != len(v) or not np.all(np.isfinite(current[k])) or
                np.max(np.abs(np.array(current[k]) - v)) > np.radians(.5)
                for k, v in plan["start"].items()):
            return {"ok": False, "error": "碰撞检查期间姿态已改变或路径过期"}
        # No IK call here, and no automatic retries after a possibly partial move.
        status = self.r.set_joint_positions(plan["q"], joint_groups=["left_arm"],
                    is_blocking=True, speed_rad_s=speed, timeout_s=40.)
        out = self.state()
        out["ok"] = status == g.ControlStatus.SUCCESS
        out["status"] = str(status)
        mp = self.mount()
        out["err_mm"] = float(np.linalg.norm(mp[0] - plan["target"])) * 1000 if mp is not None else None
        if out["err_mm"] is None or out["err_mm"] > 40:
            out["ok"] = False
        return out

    def cmd_move_checked(self, req):
        """Plan and execute in one RPC, retaining the same one-use solution."""
        table = float(req['table_z'])
        minimum = float(req['min_tip'])
        delta = float(req.get('max_delta', 40))
        deviation = float(req.get('max_dev', 5))
        if not np.all(np.isfinite([table, minimum, delta, deviation])) or not (
                minimum >= table + .012 and 0 < delta <= 45 and 0 < deviation <= 8):
            return {'ok': False, 'error': '路径检查参数无效'}
        plan = self.cmd_ikpath(req)
        if not plan.get('ok'):
            return {'ok': False, 'error': '逆解或预演失败', 'plan': plan}
        if (plan['max_delta_deg'] > delta or plan['max_dev_cm'] > deviation
                or plan['min_tip_z'] < minimum):
            self._plans.pop(plan['plan_id'], None)
            return {'ok': False, 'error': '路径不通过', 'plan': plan}
        result = self.cmd_execute_plan(dict(req, plan_id=plan['plan_id']))
        result['plan'] = plan
        if result.get('err_mm') is None or result['err_mm'] > 3:
            result['ok'] = False
        return result

    def cmd_grip(self, req):
        width = float(req.get("w", .09))
        velocity = float(req.get("v", .03))
        effort = float(req.get("eff", 3))
        settle = float(req.get("settle", .12))
        if not np.all(np.isfinite([width, velocity, effort, settle])) or not (
                0 <= width <= .12 and 0 < velocity <= .2 and 0 < effort <= 100 and 0 <= settle <= 5):
            return {"ok": False, "error": "夹爪参数超出 SDK 范围"}
        t0 = time.time()
        adaptive = bool(req.get('wait_until_stopped', False))
        before = self.r.get_gripper_state('left_gripper') if adaptive else None
        if adaptive and (before is None or not np.isfinite(float(before.width))):
            return {'ok': False, 'error': '夹爪反馈不可用，未下发命令'}
        st = self.r.set_gripper_command("left_gripper", width,
                                        velocity_mps=velocity,
                                        effort=effort,
                                        is_blocking=False if adaptive else bool(req.get("blocking", True)))
        if adaptive and st == g.ControlStatus.SUCCESS:
            timeout = float(req.get('wait_timeout', 5.))
            if not np.isfinite(timeout) or not .2 <= timeout <= 5:
                timeout = 5.
            started = time.monotonic()
            baseline_stamp = getattr(before, 'timestamp_ns', None)
            baseline_width = float(before.width)
            stable = 0
            previous = None
            previous_stamp = baseline_stamp
            activity = False
            while time.monotonic() - started < timeout:
                gs = self.r.get_gripper_state('left_gripper')
                if gs is None:
                    return {'ok': False, 'error': '夹爪反馈丢失，未继续后续动作'}
                measured = float(gs.width)
                if not np.isfinite(measured):
                    return {'ok': False, 'error': '夹爪宽度反馈无效'}
                stamp = getattr(gs, 'timestamp_ns', None)
                activity |= bool(gs.is_moving) or abs(measured - baseline_width) > .0005
                if baseline_stamp is not None and stamp is not None:
                    fresh = stamp > baseline_stamp and (activity or abs(width - baseline_width) < .0005)
                    if previous_stamp is not None and stamp <= previous_stamp:
                        time.sleep(.04)
                        continue
                    previous_stamp = stamp
                else:
                    fresh = activity or (abs(width - baseline_width) < .0005 and time.monotonic() - started >= .12)
                stable = stable + 1 if (fresh and not gs.is_moving and previous is not None
                                              and abs(measured - previous) < .0005) else 0
                previous = measured
                if stable >= 3:
                    break
                time.sleep(.04)
            else:
                return {'ok': False, 'error': '等待夹爪停稳超时，未继续后续动作',
                        'width': float(gs.width), 'moving': bool(gs.is_moving)}
        elif not adaptive:
            time.sleep(settle)
        gs = self.r.get_gripper_state("left_gripper")
        return {"ok": st == g.ControlStatus.SUCCESS, "status": str(st).replace("ControlStatus.", ""),
                "width": round(float(gs.width), 4), "effort": round(float(gs.effort), 2),
                "moving": bool(gs.is_moving),
                "ms": int((time.time() - t0) * 1000)}

    def cmd_depth(self, req):
        t0 = time.time()
        d = self._depth()
        w, h, unit = d["width"], d["height"], d["depth_scale"]
        raw = np.frombuffer(bytes(d["data"]), dtype="<u2").reshape(h, w).astype(np.float32) / unit
        msk = (raw > 0.06) & (raw < 0.80)
        return {"ok": True, "w": w, "h": h, "valid_pct": round(100.0 * msk.sum() / (w * h), 1),
                "z_p50": round(float(np.median(raw[msk])), 3) if msk.sum() else None,
                "ms": int((time.time() - t0) * 1000)}

    def cmd_base(self, req):
        """底盘小步移动：给速度 + 持续时间，结束立刻停。"""
        vx = float(req.get("vx", 0.0))
        vy = float(req.get("vy", 0.0))
        wz = float(req.get("wz", 0.0))
        dur = float(req.get("dur", 0.0))
        t0 = time.time()
        st = self.r.set_base_velocity([vx, vy, 0.0], [0.0, 0.0, wz], duration_s=dur)
        time.sleep(max(0.0, dur) + 0.25)
        self.r.stop_base()
        time.sleep(0.4)
        try:
            vel = self.r.get_base_velocity()
        except Exception:
            vel = None
        return {"ok": st == g.ControlStatus.SUCCESS, "status": str(st).replace("ControlStatus.", ""),
                "vx": vx, "dur": dur, "vel_after": vel,
                "ms": int((time.time() - t0) * 1000)}

    def cmd_overview(self, req):
        """头部彩色全局观察：找出桌面上的候选物体，反投影到 base 平面。"""
        if cv2 is None:
            return {"ok": False, "error": "cv2 不可用"}
        t0 = time.time()
        table_z = float(req.get("table_z", 0.712))
        dur = float(req.get("dur", 0.0))

        img = None
        for _ in range(5):
            rgb = self.r.get_rgb_data(S.HEAD_LEFT_CAMERA)
            if rgb and rgb.get("data"):
                img = cv2.imdecode(np.frombuffer(bytes(rgb["data"]), np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    break
        if img is None:
            return {"ok": False, "error": "头部彩色取不到图"}
        t_img = time.time()

        js = self.m.get_chain_joint_state()
        st, cf = self.m.forward_kinematics("head_left_camera_color_optical_frame", "base_link", js)
        if cf is None or len(cf) < 7:
            return {"ok": False, "error": "头部相机位姿取不到"}
        p_c = np.array(cf[:3])
        R_c = q2R(cf[3:7])

        camera_info = self.r.get_camera_intrinsic(S.HEAD_LEFT_CAMERA)
        K = list(camera_info['K'])
        fx, fy, cx0, cy0 = K[0], K[4], K[2], K[5]

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 桌面是深色布，物体通常更亮或更鲜艳
        sat = hsv[:, :, 1].astype(np.int16)
        val = hsv[:, :, 2].astype(np.int16)
        mask = ((sat > 70) & (val > 55)) | (val > 165)
        mask = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

        n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        H, W = img.shape[:2]
        cands = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < 400:
                continue
            x0 = int(stats[i, cv2.CC_STAT_LEFT]); y0 = int(stats[i, cv2.CC_STAT_TOP])
            ww = int(stats[i, cv2.CC_STAT_WIDTH]); hh = int(stats[i, cv2.CC_STAT_HEIGHT])
            if ww > W * 0.5 or hh > H * 0.5:
                continue                      # 太大的是桌面/机身
            u, v = cents[i]
            dvec = camera_ray(u, v, camera_info)
            dvec /= np.linalg.norm(dvec)
            theta = np.arccos(dvec[2])
            dirv = R_c @ dvec
            if abs(dirv[2]) < 1e-4:
                continue
            s_ = (table_z - p_c[2]) / dirv[2]
            if not (0.1 < s_ < 2.5):
                continue
            P = p_c + dirv * s_
            # 由像素尺寸估算实际尺寸（用该角度下的等效焦距）
            eff = fx / max(0.25, np.cos(theta))
            real_w = ww * s_ / eff
            real_h = hh * s_ / eff
            cands.append({"area": area, "uv": [round(float(u), 1), round(float(v), 1)],
                          "xy": [round(float(P[0]), 3), round(float(P[1]), 3)],
                          "size_cm": [round(real_w * 100, 1), round(real_h * 100, 1)],
                          "dist_m": round(float(s_), 2)})
        cands.sort(key=lambda c: -c["area"])
        return {"ok": True, "n": len(cands), "table_z": table_z,
                "img_ms": int((t_img - t0) * 1000),
                "ms": int((time.time() - t0) * 1000),
                "cands": cands[:10]}

    @staticmethod
    def _gripper_mask(Pb, p_m, R_m):
        """把实测的夹爪包围盒投到 base 系，返回"该点属于夹爪"的布尔掩码。

        盒子（安装帧）：X 0.05~0.20，Y ±0.06，Z -0.03~0.03
        """
        loc = (Pb - p_m) @ R_m
        return ((loc[:, 0] > 0.045) & (loc[:, 0] < 0.205) &
                (np.abs(loc[:, 1]) < 0.062) & (np.abs(loc[:, 2]) < 0.032))

    def cmd_depthjpeg(self, req):
        """返回压缩后的灰度深度图（近处亮），给预览页叠加识别框。"""
        t0 = time.time()
        nf = max(1, int(req.get("frames", 3)))
        frames = []
        for _ in range(nf):
            d = self._depth()
            w, h, unit = d["width"], d["height"], d["depth_scale"]
            frames.append(np.frombuffer(bytes(d["data"]), dtype="<u2").reshape(h, w).astype(np.float32) / unit)
        stk = np.stack(frames, 0); stk[stk <= 0] = np.nan
        med = np.nan_to_num(np.nanmedian(stk, 0), nan=0.0)
        lo = float(req.get("lo", 0.10)); hi = float(req.get("hi", 1.10))
        vis = np.clip((hi - med) / max(1e-3, hi - lo), 0, 1)
        vis[med <= 0] = 0
        img = (vis * 255).astype(np.uint8)
        scale = int(req.get("scale", 2))
        if scale > 1:
            img = cv2.resize(img, (w // scale, h // scale), interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(req.get("q", 80))])
        return {"ok": True, "w": w, "h": h, "ow": int(img.shape[1]), "oh": int(img.shape[0]),
                "scale": scale,
                "b64": base64.b64encode(enc.tobytes()).decode(),
                "ms": int((time.time() - t0) * 1000)}

    def cmd_headjpeg(self, req):
        self.ensure_sensor()
        data = self.r.get_rgb_data(S.HEAD_LEFT_CAMERA)
        if not data or not data.get("data"):
            return {"ok": False, "error": "头部相机暂无图像"}
        img = cv2.imdecode(np.frombuffer(bytes(data["data"]), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return {"ok": False, "error": "头部图像解码失败"}
        ok, enc = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        st, pose = self.m.forward_kinematics("head_left_camera_color_optical_frame", "base_link",
                                           self.m.get_chain_joint_state())
        intrinsic = self.r.get_camera_intrinsic(S.HEAD_LEFT_CAMERA)
        return {"ok": bool(ok), "b64": base64.b64encode(enc.tobytes()).decode(),
                "camera_pose": [float(v) for v in pose] if st == g.MotionStatus.SUCCESS and pose is not None else None,
                "camera_info": intrinsic,
                "K": [float(v) for v in intrinsic["K"]] if isinstance(intrinsic, dict) and "K" in intrinsic else None}

    def cmd_scene_regions(self, req):
        """Fuse wrist depth into head-image regions; read-only, no demo poses."""
        self.ensure_sensor()
        regions = req['regions']
        if not isinstance(regions, list) or not 1 <= len(regions) <= 12:
            raise ValueError('区域数量无效')
        table = float(req['table_z'])
        if not np.isfinite(table) or not .6 < table < .8:
            raise ValueError('桌面高度无效')
        intrinsic = self.r.get_camera_intrinsic(S.HEAD_LEFT_CAMERA)
        if intrinsic.get('distortion_model') != 'plumb_bob':
            raise ValueError('当前区域融合只支持已核对的头部 plumb_bob 图像')
        js = self.m.get_chain_joint_state()
        status, pose = self.m.forward_kinematics('head_left_camera_color_optical_frame', 'base_link', js)
        if status != g.MotionStatus.SUCCESS or pose is None or len(pose) != 7:
            raise RuntimeError('头部相机位姿不可用')
        ext = self.r.get_sensor_extrinsic(S.LEFT_ARM_DEPTH_CAMERA, 'base_link')[0][:7]
        if len(ext) != 7 or not np.all(np.isfinite(ext)):
            raise RuntimeError('深度相机位姿无效')
        frames = []
        for _ in range(4):
            data = self._depth()
            w, h = int(data['width']), int(data['height'])
            frames.append(np.frombuffer(bytes(data['data']), dtype='<u2').reshape(h, w).astype(float) / float(data['depth_scale']))
        stack = np.stack(frames)
        stack[stack <= 0] = np.nan
        depth = np.nan_to_num(np.nanmedian(stack, axis=0), nan=0.)
        valid = (depth > .06) & (depth < 1.)
        v, u = np.nonzero(valid)
        z = depth[valid]
        Kd = self.intrinsics()
        pc = np.stack([(u - Kd[2]) * z / Kd[0], (v - Kd[5]) * z / Kd[4], z], axis=1)
        points = pc @ q2R(ext[3:]).T + np.asarray(ext[:3])
        head_points = (points - np.asarray(pose[:3])) @ q2R(pose[3:])
        front = head_points[:, 2] > .05
        points, head_points = points[front], head_points[front]
        K = np.asarray(intrinsic['K'], dtype=float).reshape(3, 3)
        pixels, _ = cv2.projectPoints(head_points, np.zeros(3), np.zeros(3), K,
                                      np.asarray(intrinsic.get('D', [0.] * 5), dtype=float))
        pixels = pixels.reshape(-1, 2)
        results = []
        for region in regions:
            x0, y0, x1, y1 = [float(n) for n in region['box']]
            if not np.all(np.isfinite([x0, y0, x1, y1])) or not (0 <= x0 < x1 <= 1280 and 0 <= y0 < y1 <= 960):
                raise ValueError('图像区域无效')
            mask = ((pixels[:, 0] >= x0) & (pixels[:, 0] <= x1) &
                    (pixels[:, 1] >= y0) & (pixels[:, 1] <= y1))
            selected = points[mask]
            low = float(region.get('min_height', .006))
            high = float(region.get('max_height', .35))
            if not np.all(np.isfinite([low, high])) or not 0 <= low < high <= .35:
                raise ValueError('物体高度范围无效')
            obj = selected[(selected[:, 2] > table + low) & (selected[:, 2] < table + high)]
            out = {'name': region['name'], 'n': int(len(obj)), 'total': int(len(selected)),
                   'ok': bool(len(obj) >= 60)}
            if len(obj) >= 60:
                out.update({'center': np.median(obj, axis=0).round(4).tolist(),
                            'top_z': round(float(np.percentile(obj[:, 2], 95)), 4),
                            'z_p10': round(float(np.percentile(obj[:, 2], 10)), 4),
                            'xy_p10': np.percentile(obj[:, :2], 10, axis=0).round(4).tolist(),
                            'xy_p90': np.percentile(obj[:, :2], 90, axis=0).round(4).tolist()})
            results.append(out)
        return {'ok': True, 'regions': results, 'table_z': table,
                'camera_pose': [float(v) for v in pose], 'observed_at': time.time()}

    def cmd_target(self, req):
        """在可达区内找最高的一个物体：1cm 高度图 -> 平滑 -> 取峰 -> 加权重心。

        不做连通域，避免稀疏点云下簇形状抖动。
        """
        t0 = time.time()
        nf = int(req.get("frames", 6))
        xmin = float(req.get("xmin", 0.55)); xmax = float(req.get("xmax", 0.88))
        ymin = float(req.get("ymin", -0.10)); ymax = float(req.get("ymax", 0.32))

        ext = self.r.get_sensor_extrinsic(S.LEFT_ARM_DEPTH_CAMERA, "base_link")[0][:7]
        t = np.array(ext[:3], float); R = q2R(ext[3:7])
        K = self.intrinsics(); fx, fy, cx, cy = K[0], K[4], K[2], K[5]

        frames = []
        for _ in range(max(2, nf)):
            d = self._depth()
            w, h, unit = d["width"], d["height"], d["depth_scale"]
            frames.append(np.frombuffer(bytes(d["data"]), dtype="<u2").reshape(h, w).astype(np.float32) / unit)
        stk = np.stack(frames, 0); stk[stk <= 0] = np.nan
        med = np.nan_to_num(np.nanmedian(stk, 0), nan=0.0)

        msk = (med > 0.06) & (med < 0.80)
        vs, us = np.nonzero(msk); zz = med[msk]
        P = np.stack([(us - cx) * zz / fx, (vs - cy) * zz / fy, zz], axis=1)
        Pb = (R @ P.T).T + t

        mp = self.mount()
        if mp is None:
            return {"ok": False, "error": "no mount"}
        p_m = mp[0]
        lat0 = np.linalg.norm(Pb[:, :2] - p_m[:2], axis=1)
        far = lat0 > 0.10
        zpool = Pb[far][:, 2] if far.sum() > 2000 else Pb[:, 2]
        z_low = float(np.percentile(zpool, 8))
        near = np.abs(zpool - z_low) < 0.02
        tz = float(np.median(zpool[near])) if near.sum() > 100 else z_low
        tz = float(np.clip(tz, 0.62, 0.78))

        # 只取可达窗口内、指尖以下的点
        gm = self._gripper_mask(Pb, p_m, mp[1])
        inwin = (Pb[:, 0] > xmin) & (Pb[:, 0] < xmax) & (Pb[:, 1] > ymin) & (Pb[:, 1] < ymax) & \
                (Pb[:, 2] > tz + 0.020) & (Pb[:, 2] < tz + 0.15) & (~gm)
        if inwin.sum() < 300:
            return {"ok": False, "error": "窗口内点太少", "table_z": round(tz, 4)}

        sub = Pb[inwin]
        # 1cm 高度图
        RES = 0.01
        gx = ((sub[:, 0] - xmin) / RES).astype(int)
        gy = ((sub[:, 1] - ymin) / RES).astype(int)
        nx = int((xmax - xmin) / RES) + 1; ny = int((ymax - ymin) / RES) + 1
        hmap = np.zeros((ny, nx), np.float32)
        cnt = np.zeros((ny, nx), np.float32)
        for a, b, z in zip(gy, gx, sub[:, 2] - tz):
            if 0 <= a < ny and 0 <= b < nx:
                hmap[a, b] += z; cnt[a, b] += 1
        avg = np.where(cnt > 0, hmap / np.maximum(cnt, 1), 0)
        ker = np.ones((3, 3), np.float32) / 9.0
        try:
            import cv2
            sm = cv2.filter2D(avg, -1, ker)
        except Exception:
            sm = avg
        # 取多个局部峰（按高度排序），供上层挑"够得着"的那个
        peaks = []
        work = sm.copy()
        for _ in range(int(req.get("peaks", 3))):
            pk = np.unravel_index(int(np.argmax(work)), work.shape)
            ph = float(work[pk])
            if ph < 0.02:
                break
            px = xmin + (pk[1] + 0.5) * RES
            py = ymin + (pk[0] + 0.5) * RES
            dxy = np.linalg.norm(sub[:, :2] - np.array([px, py]), axis=1)
            sel = dxy < 0.05
            if sel.sum() >= 50:
                wgt = np.clip(sub[sel, 2] - tz, 0, None) ** 1.5
                cxy = (sub[sel, :2] * wgt[:, None]).sum(0) / max(wgt.sum(), 1e-6)
                top = float(np.percentile(sub[sel][:, 2], 97))
                peaks.append({"xy": [round(float(cxy[0]), 3), round(float(cxy[1]), 3)],
                              "peak_xy": [round(px, 3), round(py, 3)],
                              "top_z": round(top, 4),
                              "h_cm": round((top - tz) * 100, 1),
                              "n": int(sel.sum())})
            # 抹掉这个峰，避免重复
            y0 = max(0, pk[0] - 4); y1 = min(ny, pk[0] + 5)
            x0 = max(0, pk[1] - 4); x1 = min(nx, pk[1] + 5)
            work[y0:y1, x0:x1] = 0
        if not peaks:
            return {"ok": False, "error": "没有高于 2cm 的物体", "table_z": round(tz, 4)}
        out = {"ok": True, "table_z": round(tz, 4), "cands": peaks,
               "ms": int((time.time() - t0) * 1000)}
        out.update(peaks[0])
        return out

    def cmd_probe(self, req):
        """近距离探测：手停在目标上方 ~7cm 时读深度，找工具轴附近的凸起。

        专治"矮物体在 40cm 外看不出来"：D405 在 25-30cm 处深度噪声只有 1-2mm，
        2cm 高的积木也能分出高程。窗口默认以工具轴为心、半径 11cm。
        """
        t0 = time.time()
        nf = int(req.get("frames", 6))
        rad = float(req.get("radius", 0.11))
        ring_out = float(req.get("ring", 0.24))
        min_h = float(req.get("min_h_mm", 4.0)) / 1000.0

        ext = self.r.get_sensor_extrinsic(S.LEFT_ARM_DEPTH_CAMERA, "base_link")[0][:7]
        t = np.array(ext[:3], float); R = q2R(ext[3:7])
        K = self.intrinsics(); fx, fy, cx, cy = K[0], K[4], K[2], K[5]

        frames = []
        for _ in range(max(2, nf)):
            d = self._depth()
            w, h, unit = d["width"], d["height"], d["depth_scale"]
            frames.append(np.frombuffer(bytes(d["data"]), dtype="<u2").reshape(h, w).astype(np.float32) / unit)
        stk = np.stack(frames, 0); stk[stk <= 0] = np.nan
        med = np.nan_to_num(np.nanmedian(stk, 0), nan=0.0)

        msk = (med > 0.05) & (med < 0.85)
        vs, us = np.nonzero(msk); zz = med[msk]
        if zz.size < 500:
            return {"ok": False, "error": "深度有效点太少", "valid_pct": round(100.0 * msk.sum() / msk.size, 1)}
        P = np.stack([(us - cx) * zz / fx, (vs - cy) * zz / fy, zz], axis=1)
        Pb = (R @ P.T).T + t

        mp = self.mount()
        if mp is None:
            return {"ok": False, "error": "no mount"}
        p_m, R_m = mp
        gm = self._gripper_mask(Pb, p_m, R_m)

        d2 = np.linalg.norm(Pb[:, :2] - p_m[:2], axis=1)
        inwin = (d2 < rad) & (~gm)
        ring = (d2 > rad) & (d2 < ring_out) & (~gm)
        if ring.sum() < 300:
            ring = (~gm) & (d2 < ring_out)
        if ring.sum() < 200 or inwin.sum() < 200:
            return {"ok": False, "error": "窗口内点太少",
                    "inwin": int(inwin.sum()), "ring": int(ring.sum()),
                    "valid_pct": round(100.0 * msk.sum() / msk.size, 1)}

        # 桌面：整个视野里"最主的那层平面"（直方图众数）→ 再拟合平面消除倾斜。
        # 不要只取环形区：环里混进物体就会把桌面抬高 2cm，所有高度判断全废。
        # 桌面占视野绝大部分，众数一定落在桌面上。
        near = (~gm) & (d2 < 0.42)
        if near.sum() < 500:
            near = ~gm
        zr = Pb[near][:, 2]
        lo = max(0.55, float(p_m[2]) - 0.45)
        hi = min(0.80, float(p_m[2]) - 0.02)
        hist, edges = np.histogram(zr[(zr > lo) & (zr < hi)], bins=np.arange(lo, hi, 0.005))
        if hist.sum() > 50:
            tz0 = float(edges[int(np.argmax(hist))] + 0.0025)
        else:
            tz0 = float(np.median(zr))
        tz0 = float(np.clip(tz0, 0.60, 0.80))

        # 平面拟合 z = a*x + b*y + c（迭代剔除离群点），桌面并非水平
        pr_ = Pb[near]
        Xr, Yr, Zr = pr_[:, 0], pr_[:, 1], pr_[:, 2]
        a = b = 0.0
        c = tz0
        selp = np.abs(Zr - tz0) < 0.014
        for _ in range(4):
            if selp.sum() < 120:
                break
            A = np.stack([Xr[selp], Yr[selp], np.ones(int(selp.sum()))], axis=1)
            coef, *_ = np.linalg.lstsq(A, Zr[selp], rcond=None)
            a, b, c = [float(v) for v in coef]
            resid = Zr - (a * Xr + b * Yr + c)
            selp = np.abs(resid) < 0.006
        # 高斯-牛顿可能给出荒谬倾斜，兜底退回常数平面
        if abs(a) > 0.15 or abs(b) > 0.15:
            a = b = 0.0
            c = tz0

        def plane_z(x, y):
            return a * np.asarray(x) + b * np.asarray(y) + c

        tz = float(plane_z(p_m[0], p_m[1]))

        sub = Pb[inwin]
        # 每点相对"本处桌面平面"的高度（桌面有倾斜，必须逐点减）
        hvec = sub[:, 2] - plane_z(sub[:, 0], sub[:, 1])
        obj = sub[(hvec > min_h) & (hvec < 0.14)]
        if obj.shape[0] < 80:
            return {"ok": False, "error": "工具轴附近没有高于桌面的物体",
                    "table_z": round(tz, 4), "n": int(obj.shape[0]),
                    "h_max_cm": round(float(hvec.max()) * 100, 1),
                    "valid_pct": round(100.0 * msk.sum() / msk.size, 1),
                    "ms": int((time.time() - t0) * 1000)}

        # 先做 5mm 高程图并平滑，再取最高点；只在峰附近算重心。
        # 直接对全部点求重心会把几个物体混成一个，目标会飘到 10cm 外。
        RES = 0.005
        nx = int(2 * rad / RES) + 1
        oh = obj[:, 2] - plane_z(obj[:, 0], obj[:, 1])
        gi = (((obj[:, 1] - (p_m[1] - rad)) / RES)).astype(int)
        gj = (((obj[:, 0] - (p_m[0] - rad)) / RES)).astype(int)
        hm = np.full((nx, nx), np.nan, np.float32)
        for ia, ib, hh in zip(gi, gj, oh):
            if 0 <= ia < nx and 0 <= ib < nx:
                v = float(hh)
                if np.isnan(hm[ia, ib]) or v > hm[ia, ib]:
                    hm[ia, ib] = v
        hf = np.nan_to_num(hm, nan=-1.0)
        try:
            hf = cv2.GaussianBlur(hf, (5, 5), 0)
        except Exception:
            pass
        pk = np.unravel_index(int(np.argmax(hf)), hf.shape)
        peak_x = p_m[0] - rad + (pk[1] + 0.5) * RES
        peak_y = p_m[1] - rad + (pk[0] + 0.5) * RES

        dxy = np.linalg.norm(obj[:, :2] - np.array([peak_x, peak_y]), axis=1)
        cap = obj[dxy < 0.030]
        if cap.shape[0] < 60:
            cap = obj[dxy < 0.045]
        if cap.shape[0] < 30:
            return {"ok": False, "error": "峰附近点太少", "table_z": round(tz, 4),
                    "ms": int((time.time() - t0) * 1000)}
        cxy = np.median(cap[:, :2], 0)
        rad_off = float(np.linalg.norm(cxy - p_m[:2]))
        tz_here = float(plane_z(cxy[0], cxy[1]))
        cap_h = cap[:, 2] - plane_z(cap[:, 0], cap[:, 1])
        h_top = float(np.percentile(cap_h, 95))
        top = float(tz_here + h_top)

        # 多峰：把窗口里所有凸起都列出来，上层挑最近的那个
        peaks = []
        work = hf.copy()
        for _ in range(int(req.get("peaks", 4))):
            q = np.unravel_index(int(np.argmax(work)), work.shape)
            qh = float(work[q])
            if qh < min_h:
                break
            qx = p_m[0] - rad + (q[1] + 0.5) * RES
            qy = p_m[1] - rad + (q[0] + 0.5) * RES
            dq = np.linalg.norm(obj[:, :2] - np.array([qx, qy]), axis=1)
            cq = obj[dq < 0.030]
            if cq.shape[0] >= 40:
                pq = np.median(cq[:, :2], 0)
                tzq = float(plane_z(pq[0], pq[1]))
                hq = float(np.percentile(cq[:, 2] - plane_z(cq[:, 0], cq[:, 1]), 95))
                peaks.append({"xy": [round(float(pq[0]), 4), round(float(pq[1]), 4)],
                              "h_cm": round(hq * 100, 1),
                              "top_z": round(float(tzq + hq), 4),
                              # 绝对高度（纯深度、不经桌面拟合）：抬臂复测时用它判断真假
                              "abs_top": round(float(np.percentile(cq[:, 2], 95)), 4),
                              "n": int(cq.shape[0]),
                              "grid_ij": [int(q[0]), int(q[1])],
                              "dist_axis_cm": round(float(np.linalg.norm(pq - p_m[:2])) * 100, 1)})
            y0 = max(0, q[0] - 8); y1 = min(nx, q[0] + 9)
            x0 = max(0, q[1] - 8); x1 = min(nx, q[1] + 9)
            work[y0:y1, x0:x1] = -1.0
        peaks.sort(key=lambda p: p["dist_axis_cm"])

        grid = [[0 if np.isnan(v) else int(round(float(v) * 1000)) for v in row] for row in hm]

        return {"ok": True, "table_z": round(tz_here, 4),
                "plane": [round(a, 3), round(b, 3), round(c, 4)],
                "xy": [round(float(cxy[0]), 4), round(float(cxy[1]), 4)],
                "top_z": round(top, 4), "h_cm": round((top - tz_here) * 100, 1),
                "n": int(obj.shape[0]), "peaks": peaks,
                "offset_from_axis_cm": round(rad_off * 100, 1),
                "edge": bool(rad_off > rad - 0.02),
                "cam_dist": round(float(np.linalg.norm(np.array(t) - np.array([cxy[0], cxy[1], top]))), 3),
                "grid_res_cm": RES * 100, "grid_n": nx, "grid": grid,
                "origin": [round(float(p_m[0] - rad), 4), round(float(p_m[1] - rad), 4)],
                "radius": rad,
                "valid_pct": round(100.0 * msk.sum() / msk.size, 1),
                "ms": int((time.time() - t0) * 1000)}

    def cmd_ikpath(self, req):
        """逆解 + 关节插值路径预演。只算不动。

        用来在真正下发前判断这一步安不安全：
          min_tip_z  整条路径上指尖的最低高度（防压桌子）
          max_dev    末端偏离直线路径的最大距离（防大幅横甩）
          max_delta  单关节最大变化
        """
        p_des = np.array([float(req["x"]), float(req["y"]), float(req["z"])])
        R_des = tool_orientation(float(req.get("tilt", 0.0)))
        tgt = [float(v) for v in list(p_des) + R2q(R_des)]
        js = {k: [float(v) for v in values] for k, values in self.m.get_chain_joint_state().items()}
        seed = [float(v) for v in js["left_arm"]]
        n = min(100, max(12, int(req.get("steps", 24))))
        t0 = time.time()
        st, sol = self.m.inverse_kinematics(tgt, ["left_arm"], "left_arm_end_effector_mount_link",
                                            "base_link", {"left_arm": seed}, True)
        if st != g.MotionStatus.SUCCESS or not (sol and sol.get("left_arm")):
            return {"ok": False, "status": str(st).replace("MotionStatus.", ""),
                    "ms": int((time.time() - t0) * 1000)}
        q = [float(v) for v in sol["left_arm"]]
        if len(q) != 7 or len(seed) != 7 or not np.all(np.isfinite(q + seed + list(p_des))):
            return {"ok": False, "status": "INVALID_IK"}
        dq = [float(b - a) for a, b in zip(seed, q)]

        mp0 = self.mount(js)
        if mp0 is None:
            return {"ok": False, "status": "FK_START_UNAVAILABLE"}
        p0 = mp0[0]
        seg = p_des - p0
        seg_len = float(np.linalg.norm(seg))
        min_tip = 9.9
        max_dev = 0.0
        samples = []
        for k in range(n + 1):
            qq = [seed[i] + dq[i] * k / n for i in range(7)]
            try:
                st2, mo = self.m.forward_kinematics("left_arm_end_effector_mount_link", "base_link",
                                                    dict(js, left_arm=qq))
            except Exception:
                return {"ok": False, "status": "FK_FAIL", "ms": int((time.time() - t0) * 1000)}
            if st2 != g.MotionStatus.SUCCESS or mo is None or len(mo) != 7 or not np.all(np.isfinite(mo)):
                return {"ok": False, "status": "FK_EMPTY", "ms": int((time.time() - t0) * 1000)}
            p = np.array([float(v) for v in mo[:3]])
            samples.append(dict(js, left_arm=qq))
            tip = p + q2R(mo[3:]) @ np.array([TOOL_LEN_M, 0., 0.])
            min_tip = min(min_tip, float(tip[2]))
            if seg_len > 1e-6:
                w = float(np.dot(p - p0, seg) / (seg_len ** 2))
                w = min(1.0, max(0.0, w))
                perp = float(np.linalg.norm((p - p0) - seg * w))
                max_dev = max(max_dev, perp)
        if np.linalg.norm(p - p_des) > .005 or np.linalg.norm(q2R(mo[3:]) - R_des) > .05:
            return {"ok": False, "status": "IK_FK_MISMATCH"}
        # Bind this exact IK result to the checked starting configuration.
        token = secrets.token_hex(12)
        self._plans = {token: {"q": q, "start": js, "target": p_des,
                               "created": time.monotonic(), "samples": samples,
                               "min_tip": min_tip, "max_dev": max_dev,
                               "max_delta": max(abs(v) for v in dq)}}
        return {"ok": True, "status": str(st).replace("MotionStatus.", ""),
                "plan_id": token, "collision_checked": False,
                "max_delta_deg": round(max(abs(v) for v in dq) * 57.2958, 1),
                "delta_deg": [round(v * 57.2958, 1) for v in dq],
                "min_tip_z": round(min_tip, 4),
                "max_dev_cm": round(max_dev * 100, 1),
                "path_len_cm": round(seg_len * 100, 1),
                "ms": int((time.time() - t0) * 1000)}

    def cmd_ik(self, req):
        p_des = np.array([float(req["x"]), float(req["y"]), float(req["z"])])
        R_des = tool_orientation(float(req.get("tilt", 0.0)))
        tgt = [float(v) for v in list(p_des) + R2q(R_des)]
        js = {k: [float(v) for v in values] for k, values in self.m.get_chain_joint_state().items()}
        seed = [float(v) for v in js["left_arm"]]
        t0 = time.time()
        st, sol = self.m.inverse_kinematics(tgt, ["left_arm"], "left_arm_end_effector_mount_link",
                                            "base_link", {"left_arm": seed}, True)
        ok = st == g.MotionStatus.SUCCESS and bool(sol and sol.get("left_arm"))
        out = {"ok": ok, "status": str(st).replace("MotionStatus.", ""),
               "ms": int((time.time() - t0) * 1000)}
        if ok:
            d = [float(np.degrees(b - a)) for a, b in zip(seed, [float(v) for v in sol["left_arm"]])]
            out["max_delta_deg"] = round(max(abs(v) for v in d), 1)
        return out

    # ---------- 找物体：深度 -> base 点云 -> 桌面 -> 连通域 ----------
    def cmd_find(self, req):
        t0 = time.time()
        nf = int(req.get("frames", 4))
        ext = self.r.get_sensor_extrinsic(S.LEFT_ARM_DEPTH_CAMERA, "base_link")[0][:7]
        t = np.array(ext[:3], float)
        R = q2R(ext[3:7])
        K = self.intrinsics()
        fx, fy, cx, cy = K[0], K[4], K[2], K[5]

        frames = []
        for _ in range(max(1, nf)):
            d = self._depth()
            w, h, unit = d["width"], d["height"], d["depth_scale"]
            frames.append(np.frombuffer(bytes(d["data"]), dtype="<u2").reshape(h, w).astype(np.float32) / unit)
        stk = np.stack(frames, 0)
        stk[stk <= 0] = np.nan
        med = np.nan_to_num(np.nanmedian(stk, 0), nan=0.0)

        msk = (med > 0.06) & (med < 0.80)
        vs, us = np.nonzero(msk)
        us_idx, vs_idx = us, vs
        zz = med[msk]
        P = np.stack([(us - cx) * zz / fx, (vs - cy) * zz / fy, zz], axis=1)
        Pb = (R @ P.T).T + t

        # 桌面高度：桌面是视野里最低的那层表面 → 用低分位数估计，抗物体干扰
        mp0 = self.mount()
        if mp0 is None:
            return {"ok": False, "error": "no mount pose"}
        lat0 = np.linalg.norm(Pb[:, :2] - mp0[0][:2], axis=1)
        far = lat0 > 0.10
        zpool = Pb[far][:, 2] if far.sum() > 2000 else Pb[:, 2]
        z_low = float(np.percentile(zpool, 8))
        near = np.abs(zpool - z_low) < 0.02
        tz = float(np.median(zpool[near])) if near.sum() > 100 else z_low
        tz = float(np.clip(tz, 0.62, 0.78))          # 物理上桌面只可能在这个范围

        mp = self.mount()
        if mp is None:
            return {"ok": False, "error": "no mount pose"}
        p_m = mp[0]
        rel = Pb[:, 2] - tz
        lat = np.linalg.norm(Pb[:, :2] - p_m[:2], axis=1)
        # 排除夹爪自身（工具轴附近）+ 只留桌面上方的合理高度
        # 用实测包围盒精确剔除夹爪
        gm = self._gripper_mask(Pb, p_m, self.mount()[1])
        cand = (Pb[:, 2] > tz + 0.018) & (Pb[:, 2] < tz + 0.15) & (lat < 0.32) & (~gm)

        RES = 0.008
        X0, Y0 = float(p_m[0]) - 0.22, float(p_m[1]) - 0.22
        W = H = 56
        MIN_CELL_PTS = 4          # 格子占用门槛：挡掉孤立飞点，避免噪声把整片连起来
        raw = {}
        for k in np.nonzero(cand)[0]:
            a = int((Pb[k, 1] - Y0) / RES)
            b = int((Pb[k, 0] - X0) / RES)
            if 0 <= a < H and 0 <= b < W:
                raw.setdefault((a, b), []).append(k)
        cells = {c: ks for c, ks in raw.items() if len(ks) >= MIN_CELL_PTS}

        seen = set()
        comps = []
        for c in list(cells):
            if c in seen:
                continue
            sc = [c]; seen.add(c); grp = []
            while sc:
                y0, x0 = sc.pop(); grp.append((y0, x0))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        n2 = (y0 + dy, x0 + dx)
                        if n2 in cells and n2 not in seen:
                            seen.add(n2); sc.append(n2)
            if len(grp) >= 5:
                comps.append(grp)

        dbg = {"cand_pts": int(cand.sum()), "cells": len(cells), "comps": len(comps)}
        objs = []
        for grp in comps:
            ks = [k for cell in grp for k in cells[cell]]
            if len(ks) < 700:          # 太小的簇一律当噪声
                continue
            sub = Pb[ks]
            sz = np.percentile(sub, 97, 0) - np.percentile(sub, 3, 0)
            if sz[0] > 0.16 or sz[1] > 0.16:
                continue
            if sz[0] < 0.018 or sz[1] < 0.018:   # 细长/飞点
                continue
            # 像素质心与包围框（给预览页画框用）
            pk = ks
            uu = float(np.median(us_idx[pk])); vv = float(np.median(vs_idx[pk]))
            objs.append({"n": len(ks),
                         "xy": [round(float(v), 4) for v in np.median(sub[:, :2], 0)],
                         "top_z": round(float(np.percentile(sub[:, 2], 97)), 4),
                         "h_cm": round((float(np.percentile(sub[:, 2], 97)) - tz) * 100, 1),
                         "size_cm": [round(float(v) * 100, 1) for v in sz],
                         "uv": [round(uu, 1), round(vv, 1)]})
        objs.sort(key=lambda o: -o["h_cm"])
        out = {"ok": True, "table_z": round(tz, 4), "mount": [round(float(v), 4) for v in p_m],
               "n": len(objs), "objects": objs[:8], "dbg": dbg,
               "ms": int((time.time() - t0) * 1000)}
        if req.get("img"):
            lo = float(req.get("lo", 0.10)); hi = float(req.get("hi", 1.10))
            vis = np.clip((hi - med) / max(1e-3, hi - lo), 0, 1)
            vis[med <= 0] = 0
            img = (vis * 255).astype(np.uint8)
            sc = int(req.get("scale", 2))
            if sc > 1:
                img = cv2.resize(img, (w // sc, h // sc), interpolation=cv2.INTER_AREA)
            ok2, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(req.get("q", 80))])
            out["b64"] = base64.b64encode(enc.tobytes()).decode()
            out["ow"] = int(img.shape[1]); out["oh"] = int(img.shape[0]); out["scale"] = sc
            out["valid_pct"] = round(100.0 * msk.sum() / msk.size, 1)
        return out


def main():
    ex = Exec()
    print(json.dumps({"ok": True, "ready": True, "boot_ms": ex.boot_ms,
                      "depth_cam": ex.sensor_ok}), flush=True)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            cmd = req.get("cmd")
            rid = req.get("id")
            if cmd == "quit":
                print(json.dumps({"id": rid, "ok": True, "bye": True}), flush=True)
                break
            try:
                t0 = time.time()
                if cmd in {"cart", "joints", "grip", "base", "execute_plan", "move_checked"} and not globals().get("ALLOW_MOTION", False):
                    raise RuntimeError("执行器当前为只读模式")
                if cmd == "state":
                    out = ex.cmd_state(req)
                elif cmd == "cart":
                    out = ex.cmd_cart(req)
                elif cmd == "execute_plan":
                    out = ex.cmd_execute_plan(req)
                elif cmd == "move_checked":
                    out = ex.cmd_move_checked(req)
                elif cmd == "joints":
                    out = ex.cmd_joints(req)
                elif cmd == "grip":
                    out = ex.cmd_grip(req)
                elif cmd == "depth":
                    out = ex.cmd_depth(req)
                elif cmd == "find":
                    out = ex.cmd_find(req)
                elif cmd == "ik":
                    out = ex.cmd_ik(req)
                elif cmd == "collision":
                    out = ex.cmd_collision(req)
                elif cmd == "ikpath":
                    out = ex.cmd_ikpath(req)
                elif cmd == "target":
                    out = ex.cmd_target(req)
                elif cmd == "probe":
                    out = ex.cmd_probe(req)
                elif cmd == "depthjpeg":
                    out = ex.cmd_depthjpeg(req)
                elif cmd == "headjpeg":
                    out = ex.cmd_headjpeg(req)
                elif cmd == "scene_regions":
                    out = ex.cmd_scene_regions(req)
                elif cmd == "overview":
                    out = ex.cmd_overview(req)
                elif cmd == "base":
                    out = ex.cmd_base(req)
                else:
                    out = {"ok": False, "error": "unknown cmd"}
                out["id"] = rid
                out["total_ms"] = int((time.time() - t0) * 1000)
            except Exception as exc:
                out = {"id": rid, "ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
            print(json.dumps(out), flush=True)
    finally:
        ex.r.request_shutdown()
        ex.r.wait_for_shutdown()
        ex.r.destroy()



if __name__ == "__main__":
    main()
