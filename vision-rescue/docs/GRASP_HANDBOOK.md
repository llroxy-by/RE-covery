# 机器人 3D 抓取 · 运行手册（自带全部源码）

> 这份文档把**所有代码本体**都内嵌进来了，不用去仓库翻文件，照着做就能跑。
> 由 `tools/build_handbook.py` 自动生成，代码改了重新生成即可。

---

## 一、这套东西能干什么

1. 用**头部彩色相机**做全局观察，粗定位桌面上的物体
2. 规划一次机械臂移动，把**腕部深度相机**送到目标上方
3. 用深度点云算物体的精确三维位置和顶面高度
4. **一次垂直下降**到抓取高度，合爪，抬升

不是"一格一格试探"。落点由深度直接算：

```
腕部高度 = 物体顶面 z + 指长(0.189 m) − 抓深(0.028 m)
```

---

## 二、环境

| 项 | 值 |
|---|---|
| 机器人 | `galbot-echo`（Galbot G1） |
| 地址 | `192.168.167.46`，用户 `galbot` |
| SDK | `PYTHONPATH=/data/galbot/lib`，Python 3.8 |
| 本地 | macOS，任何装了 python3 + numpy + opencv 的环境都行 |

### 首次连接（装公钥，只需一次）

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub galbot@192.168.167.46
```

### 建立持久主连接（强烈建议，后面所有操作复用）

```bash
ssh -o ControlMaster=yes -o ControlPath=/tmp/kitt-console-live.sock \
    -o ControlPersist=1800 galbot@192.168.167.46 'echo ok'
```

所有脚本默认走 `/tmp/kitt-console-live.sock` 这个主连接，没有会自动新建。

---

## 三、怎么跑

### 方式 1：一键抓取（最常用）

```bash
cd vision-rescue
python3 tools/oneshot_grasp.py
```

它会：抬臂 -> 张开夹爪 -> 深度扫描 -> 找物体 -> 逆解校验 -> 一次下降 -> 合爪 -> 抬升。

### 方式 2：带头部观察的完整流程

```bash
python3 tools/plan_grasp.py
```

先用头部彩色看全局，决定往哪边移动，再让腕部深度精扫。延迟实测：
头部观察 268–350 ms，移动 255 ms–2 s，深度精扫 617–824 ms。

### 方式 3：目标太远时用底盘

```bash
python3 tools/drive_and_grasp.py
```

目标 x 超过 0.85 m 时，左右臂垂直抓取够不到，它会小步前进把目标拉进可达区。

### 方式 4：观察台（人工核对）

```bash
cd vision-rescue
./start.command
# 浏览器打开 http://127.0.0.1:8410/
```

⚠️ 观察台会占用腕部深度相机。**跑抓取脚本前必须先关掉它**，否则执行器拿不到深度数据。

---

## 四、`robot_exec.py` 命令协议

机器人上常驻一个进程，**只 init 一次**（省掉每次 3–5 秒的 SDK 初始化），
之后本地按行发 JSON，机器人按行回 JSON。

| 命令 | 作用 | 主要参数 |
|---|---|---|
| `state` | 关节角 + 腕部位姿 + 夹爪宽度/力矩 | — |
| `cart` | 逆解并移动到笛卡尔位姿 | `x,y,z,tilt,speed` |
| `joints` | 直接关节角移动 | `q[7],speed` |
| `grip` | 夹爪 | `w,v,eff,blocking,settle` |
| `depth` | 深度统计（不传图） | — |
| `find` | 深度找物体，返回连通域清单 | `frames` |
| `target` | 高度图求峰找最高物体（更稳） | `frames,peaks,xmin..ymax` |
| `ik` | 只算逆解，不动 | `x,y,z,tilt` |
| `overview` | 头部彩色全局观察 | `table_z` |
| `base` | 底盘小步移动 | `vx,vy,wz,dur` |
| `quit` | 退出 | — |

### ⚠️ 协议必须按 id 匹配

SDK 会往 **stdout** 打日志（`[info] Resolved robot type...`）。
如果客户端"发一行读一行"，会把日志当响应，导致**结果错位**（表现为数据每次都不一样、耗时显示 0 ms）。

正确写法（`tools/*.py` 里都是这个模式）：

```python
_ID = 0
def call(p, obj):
    global _ID
    _ID += 1
    obj = dict(obj, id=_ID)
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
    for _ in range(300):
        line = p.stdout.readline().strip()
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        if r.get("id") == _ID:
            return r
```

---

## 五、关键标定值（都是真机实测，别改）

| 量 | 值 | 说明 |
|---|---|---|
| 工具轴 | 安装帧 `left_arm_end_effector_mount_link` 的 **+X** | 不是 +Z |
| 指长 | **0.189 m** | 安装帧到指尖，按 0.176 算会一直偏低 1.3 cm |
| 腕部相机偏离工具轴 | **0.07 m** | 画面中心 ≠ 夹爪正下方 |
| 桌面高度 | base 下 **z ≈ 0.707 m** | 会随腿部姿态浮动 |
| 夹爪开度 | 0 – 0.12 m，手指厚约 2 cm | |
| 深度相机 | RealSense D405，1280×720，`depth_scale=10000` | 最佳 7–50 cm |
| 头部彩色 | 1280×960，`fx=412.7  cx=668.2  cy=485.2` | **鱼眼**，反投影要用等距模型 `θ=r/f` |

### 左臂垂直抓取可达区（实测）

```
     y=-0.05 +0.00 +0.05 +0.10 +0.15 +0.20 +0.25 +0.30
x=0.60  O   O   O   O   O   O   O   O
x=0.65  O   O   O   O   O   O   O   O
x=0.70  O   O   O   O   O   O   O   O
x=0.75  O   O   O   O   O   O   O   O
x=0.80  .   O   O   O   O   O   O   O
x=0.85  .   .   O   O   O   O   O   O
x=0.90  .   .   .   .   .   .   .   .
```

**目标要放在 x 0.62–0.80、y 0.00–0.28 这个矩形里。**

---

## 六、安全红线（踩过，都是真的）

### 1. 逆解返回 SUCCESS ≠ 解是对的

逆解可能返回**另一个构型分支**的解。真发生过一次：目标只挪了 6 cm，
逆解却给了一个让手臂横向甩出 **43 cm** 的解，执行后手臂直接扫过去，差点打到人。

**必须做的校验：**

```python
ik = call(p, {"cmd":"ik", "x":x, "y":y, "z":z})
assert ik["max_delta_deg"] <= 45, "关节增量过大，拒绝执行"
# 执行后立刻核对实际位置
r = call(p, {"cmd":"cart", "x":x, "y":y, "z":z, "speed":0.05})
assert r["err_mm"] <= 40, "实际位置与目标差太多，疑似跳变，立刻停"
```

### 2. 绝对不要用"直线关节插值"大幅回位

从偏姿态回预设姿态，直线插值会让末端**先下沉**（实测降到离桌面 2 cm）。
要用**笛卡尔小步爬行**：分几个路点，每个路点先算逆解，增量超限就缩小步长。

参考 `/tmp/crawl.py` 的思路（本次实操 4 个路点，增量 26–41°，误差 ≤1.9 mm）。

### 3. `request_shutdown()` 会关掉整机

不是退出 SDK 实例。收尾只调 `r.destroy()`。

### 4. 夹爪阻塞模式会空等

`set_gripper_command(is_blocking=True)` 夹住东西时实测等 **8.8 秒**才返回。
判定是否夹住要用**宽度 + 力矩双判据**：

- 夹空：宽度到限位（约 0.010），力矩约 9.5 Nm
- 夹住实物：宽度卡住（0.021–0.023），力矩 30–39 Nm

### 5. 不要低空横移

离桌面 2 cm 高度横着挪，会把积木直接扫飞。正确顺序：**高空横移对准 → 原地垂直下降**。

### 6. 黑色桌布是深度杀手

有效像素从 79% 掉到 13%，点云稀疏导致聚类碎裂、每次扫描结果都不一样。
**垫一张白纸立刻解决**（实测有效像素 13% → 53~79%）。

### 7. 同一进程不要二次 `init()`

第二次 `init(enable_sensor_set={...})` 不会真正启用传感器，`get_depth_data` 会返回没有 `width` 的字典。
一次 init 时把要用的相机都带上（头部彩色 + 腕部深度可以同时启用）。

---

## 七、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 拿不到深度（没有 `width` 字段） | 相机被别的进程占了 | 关掉观察台（8410），或同进程二次 init |
| 扫描结果每次都不一样 | 桌面太黑 / 没按 id 匹配响应 | 垫白纸；检查 id 匹配 |
| `motion_plan` / `check_collision` 全部 FAULT | WBC 读不到整机状态，规划器坏了 | 不用规划器，走 `set_joint_positions` 直接控制 |
| 夹爪不动、力矩 9.5 Nm 顶着 | 指腹压死在桌面上 | 先把手臂抬高再合爪 |
| 逆解 FAULT | 目标超出可达区 | 看上面的可达区表，或用底盘小步前进 |
| 手臂位置乱跑 | 逆解跳构型分支 | 加关节增量上限 + 执行后核对误差 |

---

## 八、完整源码

以下全部文件按原样内嵌。


### 1. `robot_exec.py`（594 行）

机器人侧常驻执行器。一次 init，之后走 JSONL 命令。所有抓取脚本都基于它。


```python
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

import json
import sys
import time

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

import galbot_sdk as g

S = g.SensorType
X_AXIS_BASE = np.array([0.0, 0.0, -1.0])
TOOL_LEN_M = 0.189          # 安装帧 -> 指尖（实测）


def q2R(q):
    x, y, z, w = [float(v) for v in q]
    n = (x * x + y * y + z * z + w * w) ** 0.5 or 1.0
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


class Exec:
    def __init__(self):
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
        self.m.init()
        self.boot_ms = int((time.time() - t0) * 1000)

    # ---------- 基础 ----------
    def joints(self):
        js = self.m.get_chain_joint_state()
        return {k: [round(float(v), 5) for v in val] for k, val in js.items()}

    def mount(self, js=None):
        js = js or self.m.get_chain_joint_state()
        st, mo = self.m.forward_kinematics("left_arm_end_effector_mount_link", "base_link", js)
        if mo is None or len(mo) < 7:
            return None
        return np.array(mo[:3]), q2R(mo[3:])

    def state(self):
        js = self.m.get_chain_joint_state()
        mp = self.mount(js)
        gs = self.r.get_gripper_state("left_gripper")
        out = {"ok": True, "joints": {k: [round(float(v), 4) for v in val] for k, val in js.items()},
               "grip": {"width": round(float(gs.width), 4), "effort": round(float(gs.effort), 2),
                        "moving": bool(gs.is_moving)}}
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
        return list(self.K_FALLBACK)

    # ---------- 命令 ----------
    def cmd_state(self, req):
        return self.state()

    def cmd_joints(self, req):
        q = [float(v) for v in req["q"]]
        t0 = time.time()
        st = self.r.set_joint_positions(q, joint_groups=["left_arm"], is_blocking=True,
                                        speed_rad_s=float(req.get("speed", 0.08)),
                                        timeout_s=float(req.get("timeout", 30.0)))
        out = self.state()
        out["status"] = str(st).replace("ControlStatus.", "")
        out["move_ms"] = int((time.time() - t0) * 1000)
        return out

    def cmd_cart(self, req):
        p_des = np.array([float(req["x"]), float(req["y"]), float(req["z"])])
        R_des = tool_orientation(float(req.get("tilt", 0.0)))
        tgt = [float(v) for v in list(p_des) + R2q(R_des)]
        js = self.m.get_chain_joint_state()
        seed = [float(v) for v in js["left_arm"]]
        t0 = time.time()
        st, sol = self.m.inverse_kinematics(tgt, ["left_arm"], "left_arm_end_effector_mount_link",
                                            "base_link", {"left_arm": seed}, True)
        ik_ms = int((time.time() - t0) * 1000)
        if not (sol and sol.get("left_arm")):
            return {"ok": False, "ik": str(st).replace("MotionStatus.", ""), "ik_ms": ik_ms}
        q = [float(v) for v in sol["left_arm"]]
        t0 = time.time()
        s2 = self.r.set_joint_positions(q, joint_groups=["left_arm"], is_blocking=True,
                                        speed_rad_s=float(req.get("speed", 0.08)),
                                        timeout_s=float(req.get("timeout", 30.0)))
        out = self.state()
        out["status"] = str(s2).replace("ControlStatus.", "")
        out["ik_ms"] = ik_ms
        out["move_ms"] = int((time.time() - t0) * 1000)
        mp = self.mount()
        if mp is not None:
            out["err_mm"] = round(float(np.linalg.norm(mp[0] - p_des)) * 1000, 1)
        return out

    def cmd_grip(self, req):
        t0 = time.time()
        st = self.r.set_gripper_command("left_gripper", float(req.get("w", 0.09)),
                                        velocity_mps=float(req.get("v", 0.05)),
                                        effort=float(req.get("eff", 3)),
                                        is_blocking=bool(req.get("blocking", True)))
        time.sleep(float(req.get("settle", 0.12)))
        gs = self.r.get_gripper_state("left_gripper")
        return {"ok": True, "status": str(st).replace("ControlStatus.", ""),
                "width": round(float(gs.width), 4), "effort": round(float(gs.effort), 2),
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
        return {"ok": True, "status": str(st).replace("ControlStatus.", ""),
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

        K = [412.7, 0.0, 668.2, 0.0, 412.7, 485.2, 0.0, 0.0, 1.0]
        try:
            d = self.r.get_camera_intrinsic(S.HEAD_LEFT_CAMERA)
            if isinstance(d, dict) and "K" in d and len(d["K"]) >= 6:
                K = list(d["K"])
        except Exception:
            pass
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
            # 鱼眼等距模型：theta = r / f
            dx = u - cx0
            dy = v - cy0
            r_px = float(np.hypot(dx, dy))
            theta = r_px / fx
            if theta > np.radians(88):
                continue
            phi = float(np.arctan2(dy, dx))
            dvec = np.array([np.sin(theta) * np.cos(phi),
                             np.sin(theta) * np.sin(phi),
                             np.cos(theta)])
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
        far = lat0 > 0.12
        zpool = Pb[far][:, 2] if far.sum() > 2000 else Pb[:, 2]
        lo = max(0.45, float(p_m[2]) - 0.45); hi = float(p_m[2]) - 0.04
        hist, edges = np.histogram(zpool, bins=np.arange(lo, hi, 0.01))
        if hist.sum() == 0:
            return {"ok": False, "error": "no table"}
        tz = float(edges[int(np.argmax(hist))] + 0.005)
        near = np.abs(Pb[:, 2] - tz) < 0.015
        for _ in range(4):
            if near.sum() < 200:
                break
            zc = float(np.median(Pb[near][:, 2]))
            near = np.abs(Pb[:, 2] - zc) < 0.010
        if near.sum() >= 200:
            tz = float(np.median(Pb[near][:, 2]))

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

    def cmd_ik(self, req):
        p_des = np.array([float(req["x"]), float(req["y"]), float(req["z"])])
        R_des = tool_orientation(float(req.get("tilt", 0.0)))
        tgt = [float(v) for v in list(p_des) + R2q(R_des)]
        js = self.m.get_chain_joint_state()
        seed = [float(v) for v in js["left_arm"]]
        t0 = time.time()
        st, sol = self.m.inverse_kinematics(tgt, ["left_arm"], "left_arm_end_effector_mount_link",
                                            "base_link", {"left_arm": seed}, True)
        ok = bool(sol and sol.get("left_arm"))
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
        zz = med[msk]
        P = np.stack([(us - cx) * zz / fx, (vs - cy) * zz / fy, zz], axis=1)
        Pb = (R @ P.T).T + t

        # 桌面高度：只在"远离工具轴"的点上找主峰，避免夹爪自身抢峰
        mp0 = self.mount()
        if mp0 is None:
            return {"ok": False, "error": "no mount pose"}
        lat0 = np.linalg.norm(Pb[:, :2] - mp0[0][:2], axis=1)
        far = lat0 > 0.12
        zpool = Pb[far][:, 2] if far.sum() > 2000 else Pb[:, 2]
        lo = max(0.45, float(mp0[0][2]) - 0.45)
        hi = float(mp0[0][2]) - 0.04
        hist, edges = np.histogram(zpool, bins=np.arange(lo, hi, 0.01))
        if hist.sum() == 0:
            return {"ok": False, "error": "no table points"}
        tz = float(edges[int(np.argmax(hist))] + 0.005)
        near = np.abs(Pb[:, 2] - tz) < 0.015
        for _ in range(4):
            if near.sum() < 200:
                break
            zc = float(np.median(Pb[near][:, 2]))
            near = np.abs(Pb[:, 2] - zc) < 0.010
        if near.sum() >= 200:
            tz = float(np.median(Pb[near][:, 2]))

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

        RES = 0.006
        X0, Y0 = float(p_m[0]) - 0.20, float(p_m[1]) - 0.20
        W = H = 68
        cells = {}
        for k in np.nonzero(cand)[0]:
            a = int((Pb[k, 1] - Y0) / RES)
            b = int((Pb[k, 0] - X0) / RES)
            if 0 <= a < H and 0 <= b < W:
                cells.setdefault((a, b), []).append(k)

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

        objs = []
        for grp in comps:
            ks = [k for cell in grp for k in cells[cell]]
            if len(ks) < 400:          # 太小的簇一律当噪声
                continue
            sub = Pb[ks]
            sz = np.percentile(sub, 97, 0) - np.percentile(sub, 3, 0)
            if sz[0] > 0.16 or sz[1] > 0.16:
                continue
            if sz[0] < 0.018 or sz[1] < 0.018:   # 细长/飞点
                continue
            objs.append({"n": len(ks),
                         "xy": [round(float(v), 4) for v in np.median(sub[:, :2], 0)],
                         "top_z": round(float(np.percentile(sub[:, 2], 97)), 4),
                         "h_cm": round((float(np.percentile(sub[:, 2], 97)) - tz) * 100, 1),
                         "size_cm": [round(float(v) * 100, 1) for v in sz]})
        objs.sort(key=lambda o: -o["h_cm"])
        return {"ok": True, "table_z": round(tz, 4), "mount": [round(float(v), 4) for v in p_m],
                "n": len(objs), "objects": objs[:8], "ms": int((time.time() - t0) * 1000)}


def main():
    ex = Exec()
    print(json.dumps({"ok": True, "ready": True, "boot_ms": ex.boot_ms,
                      "depth_cam": ex.sensor_ok}), flush=True)
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
            if cmd == "state":
                out = ex.cmd_state(req)
            elif cmd == "cart":
                out = ex.cmd_cart(req)
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
            elif cmd == "target":
                out = ex.cmd_target(req)
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


if __name__ == "__main__":
    main()
```


### 2. `tools/plan_grasp.py`（168 行）

主流程：头部全局观察 -> 规划落点 -> 移动一次 -> 腕部深度精扫 -> 一次下降抓取。


```python
"""头部全局观察 -> 规划落点 -> 移动一次 -> 腕部深度精扫 -> 一次下降抓取。

分工：
  头部彩色（鱼眼等距模型）只负责"往哪边看"，输出粗定位；
  腕部深度负责精确三维，决定落点；
  中间只移动一次，不做低空横移。
"""
import base64
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "robot_exec.py"
HOST = "galbot@192.168.167.46"
SOCK = "/tmp/kitt-console-live.sock"

TOOL_LEN = 0.189
GRIP_DEPTH = 0.025
HIGH = 1.06
# 实测可达区（垂直抓取，z 0.90-0.95）
X_MIN, X_MAX = 0.62, 0.88
Y_MIN, Y_MAX = -0.03, 0.30
SCAN_PT = (0.72, 0.12)          # 可达区中心的默认扫描位


def connect():
    payload = base64.b64encode(AGENT.read_bytes()).decode()
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T"]
    if Path(SOCK).exists():
        cmd += ["-o", "ControlPath=" + SOCK]
    cmd += [HOST, "PYTHONPATH=/data/galbot/lib python3 -u -c "
                  "\"import base64;exec(base64.b64decode('%s'))\"" % payload]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    # SDK 会往 stdout 打日志，跳过非 JSON 行，直到拿到 ready
    for _ in range(40):
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith('{"ok"'):
            try:
                return p, json.loads(line)
            except Exception:
                pass
    return p, {"boot_ms": 0, "depth_cam": None}


_ID = [0]


def call(p, obj):
    """带 id 的收发：跳过 SDK 打到 stdout 的日志行，避免读到错位的结果。"""
    _ID[0] += 1
    rid = _ID[0]
    obj = dict(obj)
    obj["id"] = rid
    t = time.time()
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()
    for _ in range(200):
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("id") == rid:
            return (time.time() - t) * 1000, r
    return (time.time() - t) * 1000, None


def in_zone(x, y):
    return X_MIN <= x <= X_MAX and Y_MIN <= y <= Y_MAX


def main():
    t_all = time.time()
    p, boot = connect()
    print("启动 %.1f s" % (boot["boot_ms"] / 1000.0))

    st = call(p, {"cmd": "state"})[1]
    print("腕部当前位置 %s" % (st["mount"],))

    # ---------- ① 头部全局观察 ----------
    dt, ov = call(p, {"cmd": "overview", "table_z": 0.712})
    if not ov.get("ok"):
        print("头部观察失败：%s" % ov.get("error"))
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return
    print("\n① 头部观察 %.0f ms  候选 %d 个" % (dt, ov["n"]))
    zone_c = [c for c in ov["cands"] if in_zone(c["xy"][0], c["xy"][1])]
    for c in ov["cands"][:5]:
        mark = "★在可达区" if in_zone(c["xy"][0], c["xy"][1]) else ""
        print("   xy=(%.3f, %.3f) 约 %.1f×%.1f cm  %s" % (c["xy"][0], c["xy"][1],
                                                          c["size_cm"][0], c["size_cm"][1], mark))

    if zone_c:
        tx, ty = zone_c[0]["xy"]
        print("   → 头部指示目标在可达区 (%.3f, %.3f)" % (tx, ty))
    else:
        tx, ty = SCAN_PT
        print("   → 头部没有落在可达区的明确目标，按默认扫描位 (%.2f, %.2f)" % (tx, ty))

    # ---------- ② 移动一次 ----------
    dt, mv = call(p, {"cmd": "cart", "x": tx, "y": ty, "z": HIGH, "speed": 0.15})
    print("② 移动到扫描位 %.0f ms  ok=%s err=%s mm" % (dt, mv.get("ok"), mv.get("err_mm")))
    if not mv.get("ok"):
        print("   移动失败（超出可达），放弃")
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return

    # ---------- ③ 腕部深度精扫 ----------
    call(p, {"cmd": "grip", "w": 0.105, "v": 0.07, "eff": 3})
    dt, f = call(p, {"cmd": "find", "frames": 5})
    print("③ 深度精扫 %.0f ms  桌面 z=%.3f  检出 %d 个" % (dt, f["table_z"], f["n"]))
    for o in f["objects"]:
        print("   xy=(%.3f, %.3f) 高 %.1fcm 尺寸 %s %s" % (o["xy"][0], o["xy"][1], o["h_cm"],
                                                         o["size_cm"], "★" if in_zone(o["xy"][0], o["xy"][1]) else ""))
    if not f["n"]:
        print("   扫描范围内没有物体")
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return

    # ---------- ④ 挑目标 + 可达性 ----------
    obj = None
    for o in f["objects"]:
        sz = o["size_cm"]
        if not (2.0 <= o["h_cm"] <= 12.0) or not in_zone(o["xy"][0], o["xy"][1]):
            continue
        if sz[0] < 2.5 or sz[1] < 2.5:        # 太小抓不住

            continue
        mz = o["top_z"] + TOOL_LEN - GRIP_DEPTH
        if call(p, {"cmd": "ik", "x": o["xy"][0], "y": o["xy"][1], "z": mz})[1].get("ok"):
            obj = o
            break
    if obj is None:
        print("   没有既在可达区、又解得出逆解的目标")
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return

    ox, oy, top = obj["xy"][0], obj["xy"][1], obj["top_z"]
    mount_z = top + TOOL_LEN - GRIP_DEPTH
    print("④ 目标 xy=(%.3f, %.3f) 顶面 %.3f → 腕部 %.3f" % (ox, oy, top, mount_z))

    # ---------- ⑤ 一次下降 + 合爪 ----------
    t_g = time.time()
    dt, mv = call(p, {"cmd": "cart", "x": ox, "y": oy, "z": mount_z, "speed": 0.15})
    print("⑤ 单次下降 %.0f ms  误差 %s mm" % (dt, mv.get("err_mm")))
    dt, g = call(p, {"cmd": "grip", "w": 0.012, "v": 0.10, "eff": 2, "blocking": False, "settle": 1.0})
    got = g["width"] > 0.024 and g["effort"] > 15
    print("⑥ 合爪     %.0f ms  宽度 %.4f  力矩 %.1f → %s"
          % (dt, g["width"], g["effort"], "夹住" if got else "没夹住"))
    if got:
        call(p, {"cmd": "cart", "x": ox, "y": oy, "z": min(mount_z + 0.12, 1.14), "speed": 0.06})
        print("⑦ 已抬升")
    print("\n抓取段耗时 %.1f s ／ 全流程 %.1f s" % (time.time() - t_g, time.time() - t_all))

    call(p, {"cmd": "quit"})
    p.wait(timeout=10)


if __name__ == "__main__":
    main()
```


### 3. `tools/oneshot_grasp.py`（140 行）

精简版：直接深度定位 -> 单次垂直下降 -> 合爪。适合已知有目标时快速跑。


```python
"""一次性抓取：深度定位 -> 可达性过滤 -> 高空对准 -> 单次垂直下降 -> 合爪 -> 抬升。

核心思想：落点完全由深度决定（top_z + 指长 - 抓深），不再逐格试探。
"""
import base64
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "robot_exec.py"
HOST = "galbot@192.168.167.46"
SOCK = "/tmp/kitt-console-live.sock"

TOOL_LEN = 0.189      # 安装帧 -> 指尖（实测，别用 0.176）
GRIP_DEPTH = 0.025    # 指尖落到物体顶面以下多少
HIGH = 1.06           # 高空对准高度


def connect():
    payload = base64.b64encode(AGENT.read_bytes()).decode()
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T"]
    if Path(SOCK).exists():
        cmd += ["-o", "ControlPath=" + SOCK]
    cmd += [HOST, "PYTHONPATH=/data/galbot/lib python3 -u -c "
                  "\"import base64;exec(base64.b64decode('%s'))\"" % payload]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    # SDK 会往 stdout 打日志，跳过非 JSON 行，直到拿到 ready
    for _ in range(40):
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith('{"ok"'):
            try:
                return p, json.loads(line)
            except Exception:
                pass
    return p, {"boot_ms": 0, "depth_cam": None}


_ID = [0]


def call(p, obj):
    """带 id 的收发：跳过 SDK 打到 stdout 的日志行，避免读到错位的结果。"""
    _ID[0] += 1
    rid = _ID[0]
    obj = dict(obj)
    obj["id"] = rid
    t = time.time()
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()
    for _ in range(200):
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("id") == rid:
            return (time.time() - t) * 1000, r
    return (time.time() - t) * 1000, None


def main():
    p, boot = connect()
    print("启动 %.1f s（深度相机 %s）" % (boot["boot_ms"] / 1000.0, boot.get("depth_cam")))

    st = call(p, {"cmd": "state"})[1]
    print("当前腕部 %s  夹爪 %.3f m" % (st["mount"], st["grip"]["width"]))

    call(p, {"cmd": "cart", "x": st["mount"][0], "y": st["mount"][1], "z": HIGH, "speed": 0.10})
    call(p, {"cmd": "grip", "w": 0.105, "v": 0.07, "eff": 3})

    # ---- 一次扫描 ----
    dt, f = call(p, {"cmd": "find", "frames": 5})
    if not f.get("ok"):
        print("扫描失败：%s" % f.get("error"))
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return
    print("扫描 %.0f ms  桌面 z=%.3f  检出 %d 个" % (dt, f["table_z"], f["n"]))
    for o in f["objects"]:
        print("   xy=(%.3f, %.3f) 高 %.1fcm 尺寸 %s 点数 %d"
              % (o["xy"][0], o["xy"][1], o["h_cm"], o["size_cm"], o["n"]))

    # ---- 可达性过滤，挑第一个够得着的 ----
    obj = None
    for o in f["objects"]:
        if not (2.5 <= o["h_cm"] <= 12.0):
            continue
        mz = o["top_z"] + TOOL_LEN - GRIP_DEPTH
        _, ik_high = call(p, {"cmd": "ik", "x": o["xy"][0], "y": o["xy"][1], "z": HIGH})
        _, ik_low = call(p, {"cmd": "ik", "x": o["xy"][0], "y": o["xy"][1], "z": mz})
        print("   检查 xy=(%.3f, %.3f)：高空 %s / 抓取点 %s"
              % (o["xy"][0], o["xy"][1], ik_high.get("status"), ik_low.get("status")))
        if ik_high.get("ok") and ik_low.get("ok"):
            obj = o
            break
    if obj is None:
        print("没有既合适又够得着的目标")
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return

    ox, oy, top = obj["xy"][0], obj["xy"][1], obj["top_z"]
    mount_z = top + TOOL_LEN - GRIP_DEPTH
    print("\n目标 xy=(%.3f, %.3f) 顶面 z=%.3f → 腕部降到 z=%.3f（指尖到 %.3f）"
          % (ox, oy, top, mount_z, mount_z - TOOL_LEN))

    t0 = time.time()
    dt, r = call(p, {"cmd": "cart", "x": ox, "y": oy, "z": HIGH, "speed": 0.10})
    print("① 高空对准  %.0f ms  误差 %s mm" % (dt, r.get("err_mm")))

    dt, r = call(p, {"cmd": "cart", "x": ox, "y": oy, "z": mount_z, "speed": 0.05})
    print("② 一次下降  %.0f ms  误差 %s mm（不再分步）" % (dt, r.get("err_mm")))

    dt, g = call(p, {"cmd": "grip", "w": 0.012, "v": 0.015, "eff": 2,
                     "blocking": False, "settle": 0.35})
    got = g["width"] > 0.024 and g["effort"] > 15
    print("③ 合爪      %.0f ms  宽度 %.4f  力矩 %.1f → %s"
          % (dt, g["width"], g["effort"], "夹住" if got else "没夹住"))

    if got:
        dt, r = call(p, {"cmd": "cart", "x": ox, "y": oy,
                         "z": min(mount_z + 0.12, 1.14), "speed": 0.06})
        gg = call(p, {"cmd": "state"})[1]["grip"]
        print("④ 抬升      %.0f ms  宽度 %.4f  力矩 %.1f" % (dt, gg["width"], gg["effort"]))
    print("\n=== 对准到夹住：%.1f s ===" % (time.time() - t0))

    call(p, {"cmd": "quit"})
    p.wait(timeout=10)


if __name__ == "__main__":
    main()
```


### 4. `tools/drive_and_grasp.py`（175 行）

带底盘：目标超出可达区时，小步前进把它拉进来再抓。


```python
"""底盘小步前进把目标拉进可达区，然后一次抓取。

闭环：扫 -> 看目标是否可达 -> 不可达就前进一小步 -> 再扫，最多 4 轮。
"""
import base64
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "robot_exec.py"
HOST = "galbot@192.168.167.46"
SOCK = "/tmp/kitt-console-live.sock"

TOOL_LEN = 0.189
GRIP_DEPTH = 0.025
HIGH = 1.06
REACH_X = 0.845          # 超过这个 x 基本抓不到
STEP_V, STEP_T = 0.035, 1.6      # 每次前进约 5-6 cm

_ID = [0]


def connect():
    payload = base64.b64encode(AGENT.read_bytes()).decode()
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T"]
    if Path(SOCK).exists():
        cmd += ["-o", "ControlPath=" + SOCK]
    cmd += [HOST, "PYTHONPATH=/data/galbot/lib python3 -u -c "
                  "\"import base64;exec(base64.b64decode('%s'))\"" % payload]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    for _ in range(40):
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith('{"ok"'):
            try:
                return p, json.loads(line)
            except Exception:
                pass
    return p, {"boot_ms": 0}


def call(p, obj):
    _ID[0] += 1
    rid = _ID[0]
    obj = dict(obj, id=rid)
    t = time.time()
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()
    for _ in range(300):
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("id") == rid:
            return (time.time() - t) * 1000, r
    return (time.time() - t) * 1000, None


def pick(objs):
    """挑一个像样的方块：高度 2.5-12cm，两个方向都不小于 2.5cm。"""
    for o in objs:
        if 2.5 <= o["h_cm"] <= 12.0 and o["size_cm"][0] >= 2.5 and o["size_cm"][1] >= 2.5:
            return o
    return None


def main():
    t_all = time.time()
    p, boot = connect()
    print("启动 %.1f s" % (boot.get("boot_ms", 0) / 1000.0))

    st = call(p, {"cmd": "state"})[1]
    # 手臂先抬到安全高度再让底盘动
    call(p, {"cmd": "cart", "x": st["mount"][0], "y": st["mount"][1], "z": HIGH, "speed": 0.12})
    call(p, {"cmd": "grip", "w": 0.105, "v": 0.07, "eff": 3})

    target = None
    for it in range(4):
        dt, f = call(p, {"cmd": "find", "frames": 5})
        objs = f.get("objects", [])
        print("\n第 %d 次扫描（%.0f ms）：%d 个物体" % (it + 1, dt, len(objs)))
        for o in objs[:5]:
            print("   xy=(%.3f, %.3f) 高 %.1fcm 尺寸 %s" % (o["xy"][0], o["xy"][1],
                                                          o["h_cm"], o["size_cm"]))
        cand = pick(objs)
        if cand is None:
            print("   没有像样的方块")
            break
        x, y = cand["xy"]
        print("   选中 xy=(%.3f, %.3f)" % (x, y))
        if x <= REACH_X:
            target = cand
            print("   ✓ 已进入可达区")
            break
        d = x - (REACH_X - 0.03)
        dur = min(2.2, max(0.8, d / STEP_V))
        print("   超出可达 %.1f cm → 前进 %.1f s（约 %.1f cm）" % ((x - REACH_X) * 100, dur, STEP_V * dur * 100))
        dt2, mv = call(p, {"cmd": "base", "vx": STEP_V, "dur": dur})
        print("   底盘移动 %.0f ms  status=%s" % (dt2, mv.get("status")))
        time.sleep(0.5)

    if target is None:
        print("\n没能把目标弄进可达区，放弃")
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return

    # ---- 抓取 ----
    ox, oy, top = target["xy"][0], target["xy"][1], target["top_z"]
    mount_z = top + TOOL_LEN - GRIP_DEPTH
    print("\n目标 xy=(%.3f, %.3f) 顶面 %.3f → 腕部 %.3f" % (ox, oy, top, mount_z))

    ik = call(p, {"cmd": "ik", "x": ox, "y": oy, "z": mount_z})[1]
    print("逆解：%s" % ik.get("status"))
    if not ik.get("ok"):
        call(p, {"cmd": "quit"}); p.wait(timeout=10); return

    t_g = time.time()
    dt, a = call(p, {"cmd": "cart", "x": ox, "y": oy, "z": HIGH, "speed": 0.15})
    print("① 高空对准 %.0f ms 误差 %s mm" % (dt, a.get("err_mm")))

    # 到位后复核：目标是否真的在工具轴正下方（相机偏轴 7cm，必须闭环校正）
    for k in range(3):
        _, st2 = call(p, {"cmd": "state"})
        mx, my = st2["mount"][0], st2["mount"][1]
        _, f2 = call(p, {"cmd": "find", "frames": 4})
        near = None
        best = 9.9
        for o in f2.get("objects", []):
            d = ((o["xy"][0] - mx) ** 2 + (o["xy"][1] - my) ** 2) ** 0.5
            if d < best:
                best, near = d, o
        if near is None:
            print("   复核：视野里找不到目标")
            break
        dx = near["xy"][0] - mx
        dy = near["xy"][1] - my
        print("   复核 %d：最近目标 xy=(%.3f, %.3f) 偏离工具轴 (%.1f, %.1f) cm"
              % (k + 1, near["xy"][0], near["xy"][1], dx * 100, dy * 100))
        if abs(dx) < 0.012 and abs(dy) < 0.012:
            print("   ✓ 已对准")
            ox, oy, top = near["xy"][0], near["xy"][1], near["top_z"]
            mount_z = top + TOOL_LEN - GRIP_DEPTH
            break
        ox, oy = mx + dx, my + dy
        call(p, {"cmd": "cart", "x": ox, "y": oy, "z": HIGH, "speed": 0.12})
    dt, a = call(p, {"cmd": "cart", "x": ox, "y": oy, "z": mount_z, "speed": 0.15})
    print("② 一次下降 %.0f ms 误差 %s mm" % (dt, a.get("err_mm")))
    dt, g = call(p, {"cmd": "grip", "w": 0.012, "v": 0.10, "eff": 2,
                     "blocking": False, "settle": 1.0})
    got = g["width"] > 0.024 and g["effort"] > 15
    print("③ 合爪     %.0f ms 宽度 %.4f 力矩 %.1f → %s"
          % (dt, g["width"], g["effort"], "夹住" if got else "没夹住"))
    if got:
        call(p, {"cmd": "cart", "x": ox, "y": oy, "z": min(mount_z + 0.14, 1.14), "speed": 0.08})
        gg = call(p, {"cmd": "state"})[1]["grip"]
        print("④ 已抬升，宽度 %.4f 力矩 %.1f" % (gg["width"], gg["effort"]))

    print("\n抓取段 %.1f s ／ 全程 %.1f s" % (time.time() - t_g, time.time() - t_all))
    call(p, {"cmd": "quit"})
    p.wait(timeout=10)


if __name__ == "__main__":
    main()
```


### 5. `robot_link.py`（137 行）

本地侧 SSH 拉起代理并按 JSONL 收发的封装（观察台用）。


```python
"""本地侧：经 SSH 拉起机器人常驻代理，按 JSONL 收发。

代理脚本以 base64 单 token 注入 `python3 -c`，机器人上不落盘。
线程安全：一把锁串行化请求；超时即杀掉连接，下次调用自动重启。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

AGENT = Path(__file__).with_name("robot_agent.py")
DEFAULT_HOST = os.environ.get("ROBOT_HOST", "galbot@192.168.167.46")
CONTROL_PATH = os.environ.get("ROBOT_SOCK", "/tmp/kitt-console-live.sock")


class RobotAgent:
    def __init__(self, host: str | None = None) -> None:
        self.host = host or DEFAULT_HOST
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.ready: dict | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------ 进程
    def _ssh_cmd(self) -> list[str]:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T"]
        if CONTROL_PATH and Path(CONTROL_PATH).exists():
            cmd += ["-o", "ControlPath=" + CONTROL_PATH]
        cmd.append(self.host)
        payload = base64.b64encode(AGENT.read_bytes()).decode()
        cmd.append("PYTHONPATH=/data/galbot/lib python3 -c "
                   "\"import base64;exec(base64.b64decode('%s'))\"" % payload)
        return cmd

    def start(self, wait_ready: float = 25.0) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        self.stop()
        try:
            self.proc = subprocess.Popen(self._ssh_cmd(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1)
        except OSError as exc:
            self.last_error = "启动失败：%s" % exc
            return False
        deadline = time.time() + wait_ready
        while time.time() < deadline:
            line = self._readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ready"):
                self.ready = msg["ready"]
                return True
            if msg.get("ok") is False:
                self.last_error = msg.get("error") or "代理启动失败"
                self.stop()
                return False
        self.last_error = self.last_error or "代理无响应（超时）"
        self.stop()
        return False

    def _readline(self, timeout: float) -> str | None:
        if not self.proc or not self.proc.stdout:
            return None
        box: list[str | None] = [None]

        def read() -> None:
            try:
                box[0] = self.proc.stdout.readline()
            except (ValueError, OSError):
                box[0] = None

        th = threading.Thread(target=read, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            return None
        return box[0]

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.write('{"cmd":"quit"}\n')
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        time.sleep(0.05)
        try:
            proc.terminate()
        except OSError:
            pass

    # ------------------------------------------------------------ 请求
    def call(self, req: dict, timeout: float = 30.0) -> dict:
        with self.lock:
            if not self.start():
                return {"ok": False, "error": self.last_error or "机器人代理不可用"}
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.stop()
                return {"ok": False, "error": "连接已断开，请重试"}
            t0 = time.time()
            while time.time() - t0 < timeout:
                line = self._readline(timeout=timeout - (time.time() - t0))
                if line is None:
                    self.stop()
                    return {"ok": False, "error": "机器人响应超时"}
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except ValueError:
                    continue
            self.stop()
            return {"ok": False, "error": "响应超时"}

    def view(self) -> dict:
        return self.call({"cmd": "view"}, timeout=35)

    def target(self, roi: list[float]) -> dict:
        return self.call({"cmd": "target", "roi": roi}, timeout=30)
```


### 6. `robot_agent.py`（208 行）

只读代理：取深度图、框选返回三维点与 IK 可达性。观察台后端。


```python
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
```


### 7. `start.command`（19 行）

观察台启动脚本（会加载 vision/config/.env.local 里的模型密钥）。


```bash
#!/bin/zsh
# 双击运行：RE:BENCH 实时视觉（Mac 本机摄像头，画面在浏览器本地渲染）
#
# key 从 vision/config/.env.local 读，用来让视觉模型兜底认字母。
# 键不存在也能跑，只是只剩本地模板比对。
cd "${0:A:h}"

ENV_FILE="vision/config/.env.local"
if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
fi

PORT="${PORT:-8400}"
echo "启动中… 浏览器会自动打开 http://127.0.0.1:$PORT/"
echo "第一次会问摄像头权限，点「允许」。关掉这个窗口就停止服务。"
echo
exec ../.venv-vision/bin/python -m live.server \
  --config vision/config/mac.json --port "$PORT" "$@"
```


---

## 九、一键自检（跑之前先确认链路通）

```python
import base64, json, subprocess
from pathlib import Path

payload = base64.b64encode(Path("robot_exec.py").read_bytes()).decode()
cmd = ["ssh","-o","BatchMode=yes","-T",
       "-o","ControlPath=/tmp/kitt-console-live.sock","galbot@192.168.167.46",
       "PYTHONPATH=/data/galbot/lib python3 -u -c "
       "\"import base64;exec(base64.b64decode('%s'))\"" % payload]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

for _ in range(40):
    line = p.stdout.readline().strip()
    if line.startswith('{"ok"'):
        print("启动:", line); break

p.stdin.write(json.dumps({"cmd":"state","id":1}) + "\n"); p.stdin.flush()
for _ in range(300):
    line = p.stdout.readline().strip()
    if line.startswith("{"):
        r = json.loads(line)
        if r.get("id") == 1:
            print("腕部:", r["mount"], "夹爪:", r["grip"]["width"]); break

p.stdin.write(json.dumps({"cmd":"depth","id":2}) + "\n"); p.stdin.flush()
for _ in range(300):
    line = p.stdout.readline().strip()
    if line.startswith("{"):
        r = json.loads(line)
        if r.get("id") == 2:
            print("深度有效像素:", r.get("valid_pct"), "%"); break

p.stdin.write(json.dumps({"cmd":"quit","id":99}) + "\n"); p.stdin.flush(); p.wait(timeout=10)
```

正常输出示例：

```
启动: {"ok": true, "ready": true, "boot_ms": 3345, "depth_cam": true}
腕部: [0.47, 0.23, 1.1] 夹爪: 0.105
深度有效像素: 70.5 %
```
