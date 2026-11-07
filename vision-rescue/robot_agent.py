"""机器人侧常驻代理：一次 init，按 JSONL 协议应答。

不写任何文件、不发运动指令、只读关节状态与深度。经 SSH 以 base64 单 token 注入执行。

命令：
  {"cmd":"view"}                       取一帧时域中位深度视图（含实时相机位姿）
  {"cmd":"target","roi":[x,y,w,h]}     在上一帧视图的归一化选区内求三维点 + IK 可达性
  {"cmd":"ping"} / {"cmd":"quit"}
"""
import base64
import json
import os
import sys
import time

import numpy as np
import cv2
import galbot_sdk as g
from galbot_sdk import MachineType, SensorType as S

CAM_FRAME = "left_arm_camera_color_optical_frame"
MOUNT_FRAME = "left_arm_end_effector_mount_link"
TCP_FRAME = "left_gripper_tcp_link"
MEDIAN_N = 6
Z_MIN, Z_MAX = 0.12, 1.20


def q2R(x, y, z, w):
    n = (x * x + y * y + z * z + w * w) ** 0.5 or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def say(obj):
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


class Agent:
    def __init__(self):
        self.robot = g.GalbotRobot.get_instance(MachineType.G1)
        ok = self.robot.init(enable_sensor_set={S.LEFT_ARM_DEPTH_CAMERA}, enable_sync_mode=False)
        self.motion = g.GalbotMotion.get_instance(MachineType.G1)
        mok = self.motion.init()
        K = self.robot.get_camera_intrinsic(S.LEFT_ARM_DEPTH_CAMERA)["K"]
        self.fx, self.fy, self.cx, self.cy = K[0], K[4], K[2], K[5]
        self.state = None          # 上一次 view 的 (depth_m, R, t, tcp, ts)
        say({"ok": True, "ready": {"robot": bool(ok), "motion": bool(mok),
                                   "fx": round(self.fx, 2), "fy": round(self.fy, 2),
                                   "cx": round(self.cx, 2), "cy": round(self.cy, 2)}})

    # ---------------------------------------------------------------- 基础
    def joint_state(self):
        s = self.motion.get_chain_joint_state()
        return s

    def pose_of(self, frame, js):
        st, pose = self.motion.forward_kinematics(frame, "base_link", js)
        if pose and len(pose) == 7:
            return np.array(pose[:3], float), q2R(*pose[3:]), list(pose)
        return None, None, None

    def grab_median(self):
        frames = []
        for _ in range(MEDIAN_N):
            d = self.robot.get_depth_data(S.LEFT_ARM_DEPTH_CAMERA)
            if isinstance(d, dict) and d.get("data"):
                w, h, unit = d["width"], d["height"], d["depth_scale"]
                frames.append(np.frombuffer(bytes(d["data"]), dtype="<u2")
                              .reshape(h, w).astype(np.float32) / unit)
        if not frames:
            return None, None
        stack = np.stack(frames)
        good = (stack > Z_MIN) & (stack < Z_MAX)
        with np.errstate(invalid="ignore"):
            med = np.nanmedian(np.where(good, stack, np.nan), axis=0)
        valid = (good.mean(axis=0) > 0.4) & np.isfinite(med)
        return np.where(valid, med, 0.0).astype(np.float32), valid

    # ---------------------------------------------------------------- 命令
    def cmd_view(self):
        t0 = time.time()
        dep, valid = self.grab_median()
        if dep is None:
            return {"ok": False, "error": "取不到深度帧"}
        js = self.joint_state()
        pos, R, _ = self.pose_of(CAM_FRAME, js)
        # 末端参考帧：优先 TCP，取不到时退回腕部安装帧（稳定可用）
        _, _, tcp_pose = self.pose_of(TCP_FRAME, js)
        reach_frame = TCP_FRAME
        if tcp_pose is None:
            _, _, tcp_pose = self.pose_of(MOUNT_FRAME, js)
            reach_frame = MOUNT_FRAME
        if pos is None:
            return {"ok": False, "error": "取不到相机位姿"}
        self.state = (dep, R, pos, tcp_pose, reach_frame, time.time())

        lo, hi = 0.20, 1.10
        norm = np.clip((dep - lo) / (hi - lo), 0, 1)
        img = (norm * 255).astype(np.uint8)
        viz = cv2.applyColorMap(img, cv2.COLORMAP_BONE)
        viz[~valid] = np.array([18, 18, 18], np.uint8)
        viz = cv2.medianBlur(viz, 3)
        okj, buf = cv2.imencode(".jpg", viz, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not okj:
            return {"ok": False, "error": "编码失败"}
        return {"ok": True, "jpeg": base64.b64encode(buf.tobytes()).decode(),
                "w": int(dep.shape[1]), "h": int(dep.shape[0]),
                "valid_pct": round(100.0 * float(valid.mean()), 1),
                "cam_pos": [round(float(v), 4) for v in pos],
                "reach_frame": reach_frame,
                "tcp": [round(float(v), 4) for v in tcp_pose] if tcp_pose else None,
                "ms": int((time.time() - t0) * 1000)}

    def cmd_target(self, roi):
        if self.state is None:
            return {"ok": False, "error": "请先刷新深度视图"}
        dep, R, pos, tcp_pose, reach_frame, ts = self.state
        h, w = dep.shape
        x, y, rw, rh = [float(v) for v in roi]
        x0, y0 = int(max(0, x) * w), int(max(0, y) * h)
        x1, y1 = int(min(1, x + rw) * w), int(min(1, y + rh) * h)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return {"ok": False, "error": "选区太小"}
        sub = dep[y0:y1, x0:x1]
        vs, us = np.nonzero(sub > 0)
        if vs.size < 60:
            return {"ok": False, "error": "选区内有效深度点太少（%d），请对准有结构的表面" % vs.size}
        z = sub[vs, us]
        P = np.stack([(us + x0 - self.cx) * z / self.fx,
                      (vs + y0 - self.cy) * z / self.fy, z], axis=1)
        Pb = (R @ P.T).T + pos
        center = np.median(Pb, axis=0)
        spread = float(np.median(np.linalg.norm(Pb - center, axis=1))) * 1000.0
        out = {"ok": True, "n": int(vs.size), "age_ms": int((time.time() - ts) * 1000),
               "xyz": [round(float(v), 4) for v in center],
               "spread_mm": round(spread, 1),
               "z_range": [round(float(np.percentile(Pb[:, 2], 5)), 3),
                           round(float(np.percentile(Pb[:, 2], 95)), 3)]}
        if tcp_pose:
            d = float(np.linalg.norm(center - np.array(tcp_pose[:3])))
            out["dist_ee_m"] = round(d, 3)
            out["reach_frame"] = reach_frame
            # 以当前末端姿态为抓取朝向，做一次 IK 可达性检查（只求解，不执行）
            js = self.joint_state()
            try:
                st, sol = self.motion.inverse_kinematics(
                    [float(center[0]), float(center[1]), float(center[2])] + [float(v) for v in tcp_pose[3:7]],
                    ["left_arm"], reach_frame, "base_link",
                    {"left_arm": list(js["left_arm"])}, True)
                out["ik"] = {"status": str(st).replace("MotionStatus.", "")}
                if sol:
                    out["ik"]["joints"] = [round(float(v), 4) for v in sol["left_arm"]]
            except Exception as exc:
                out["ik"] = {"status": "ERR", "why": "%s: %s" % (type(exc).__name__, exc)}
        return out


def main():
    import select
    agent = Agent()
    idle_limit = float(os.environ.get("AGENT_IDLE_SEC", "300"))
    while True:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], idle_limit)
        except (OSError, ValueError):
            break
        if not ready:
            say({"ok": True, "bye": "idle_timeout"})
            break
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            say({"ok": False, "error": "bad json"})
            continue
        cmd = req.get("cmd")
        if cmd == "quit":
            say({"ok": True, "bye": True})
            break
        if cmd == "ping":
            say({"ok": True, "pong": True})
            continue
        try:
            if cmd == "view":
                say(agent.cmd_view())
            elif cmd == "target":
                say(agent.cmd_target(req.get("roi") or []))
            else:
                say({"ok": False, "error": "unknown cmd"})
        except Exception as exc:
            say({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    try:
        agent.robot.request_shutdown(); agent.robot.wait_for_shutdown(); agent.robot.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    main()
