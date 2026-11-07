# 遮挡感知与容器取物 · 任务交接说明

写给接手"视觉遮挡预测"这条线的同学。
本文只讲**这台机器人上真实可用**的东西，所有数字都是真机实测，不是理论值。

---

## 一、任务定义

要解决的是**物体被遮挡后的持续性认知与检索**，具体场景：

1. 一开始物体在桌面上，视觉能看到它 → 记录位置
2. 有人把它放进一个盒子里，物体从视野中消失
3. 系统要能判断：**它不是消失了，而是在那个盒子里**
4. 需要时，能规划并执行"把手伸进盒子里把它拿出来"

拆成四个子能力：

| 子能力 | 说明 |
|---|---|
| **检测** | 找到桌面上的物体，给出三维位置 |
| **容器识别** | 认出哪个是"盒子/容器"，知道它的开口范围和内部底面高度 |
| **遮挡推理** | 物体消失时，判断它进入了哪个容器（而不是消失或被拿走） |
| **容器内抓取** | 从容器开口垂直伸入，夹住并取出 |

---

## 二、这台机器人的视觉家底（重要，先看这个）

### 2.1 传感器清单（`galbot_sdk.SensorType`）

| 传感器 | 状态 | 规格 |
|---|---|---|
| `LEFT_ARM_DEPTH_CAMERA` | ✅ **主力**，可用 | RealSense D405，1280×720，16UC1，`depth_scale=10000` |
| `HEAD_LEFT_CAMERA` | ✅ 可用 | 1280×960 JPEG，彩色，**鱼眼** |
| `HEAD_RIGHT_CAMERA` | ✅ 存在 | 同上 |
| `LEFT_ARM_CAMERA`（彩色） | ⚠️ init 成功但**取不到图** | 被 `left_arm_camera_capture` 进程占着 |
| `LEFT_ARM_DOWN_CAMERA` | ❌ **硬件不存在**（init 返回 False） | — |
| `RIGHT_ARM_DEPTH_CAMERA` | ⚠️ 有外参，取不到深度数据 | — |
| 头部深度 | ❌ **不存在** | 头部只有 RGB |

> 多台相机**不能同时 enable**，`init(enable_sensor_set={A,B})` 会返回 False，必须逐路启用。

### 2.2 内参实测

```
左臂深度 D405： fx=657.4  fy=656.9  cx=636.4  cy=356.1   （1280×720）
头部彩色     ： fx=412.7  fy=412.7  cx=668.2  cy=485.2   （1280×960）
```

⚠️ **头部是鱼眼，但畸变参数全 0**。用针孔模型反投影，在偏离中心 280 px 处角度会**低估约 5°**（0.6 m 处约 5 cm 误差）。
越靠画面边缘误差越大。**做三维一定要用腕部深度，不要用头部反投影算距离。**

### 2.3 关键几何关系（踩过坑，必须按这个来）

| 量 | 值 | 说明 |
|---|---|---|
| 工具轴 | 安装帧 `left_arm_end_effector_mount_link` 的 **+X** 轴 | 不是 +Z，按 +Z 算全错 |
| 指长（安装帧→指尖） | **0.189 m** | 按 0.176 算会一直偏低 1.3 cm，表现为"对准了却夹空" |
| 腕部相机偏离工具轴 | **约 0.07 m** | **画面中心 ≠ 夹爪正下方**，这条最容易踩 |
| 桌面高度 | base_link 下 **z ≈ 0.71 m** | 实测在 0.695–0.716 之间浮动（腿部姿态变了就会变） |
| 夹爪 | 平行二指，开度 **0–0.12 m**，手指厚约 2 cm | 抓取前要留够开口余量 |

### 2.4 坐标系

- 所有三维点统一转到 `base_link`，这是最适合做世界模型的坐标系
- 相机位姿用 **`forward_kinematics("left_arm_camera_color_optical_frame", "base_link", js)`**
- ⚠️ **不要用 `get_sensor_extrinsic` 做相机位姿**：它对臂上相机返回的是静态标称值，不随关节变化
- ⚠️ TF 的 `get_transform` 与上面的欧拉/四元数约定不一致（分量被重排过），混用会错得很难查

---

## 三、可用接口速查

### 3.1 感知

```python
from galbot_sdk import MachineType, SensorType as S
r = GalbotRobot.get_instance(MachineType.G1)
r.init(enable_sensor_set={S.LEFT_ARM_DEPTH_CAMERA}, enable_sync_mode=False)

K   = r.get_camera_intrinsic(S.LEFT_ARM_DEPTH_CAMERA)["K"]      # fx,fy,cx,cy
d   = r.get_depth_data(S.LEFT_ARM_DEPTH_CAMERA)                  # {"data","width","height","depth_scale"}
rgb = r.get_rgb_data(S.HEAD_LEFT_CAMERA)                         # JPEG bytes
```

### 3.2 运动学（都可用，毫秒级）

```python
m = GalbotMotion.get_instance(MachineType.G1); m.init()
js = m.get_chain_joint_state()                                   # 各链关节角
m.forward_kinematics(frame, "base_link", js)                     # 正解
m.inverse_kinematics(pose7, ["left_arm"], frame, "base_link",
                     {"left_arm": 当前关节角}, True)              # 逆解
```
> **逆解必须传当前关节角作初值**，不传直接返回 `FAULT`。

### 3.3 执行（关键：规划器坏了）

```python
# ✅ 可用：直接关节控制，实测跟踪误差 < 0.1°
r.set_joint_positions(q7, joint_groups=["left_arm"],
                      is_blocking=True, speed_rad_s=0.08, timeout_s=30)

# ✅ 可用：夹爪
r.set_gripper_command("left_gripper", width_m, velocity_mps=0.03, effort=3, is_blocking=False)

# ❌ 全部 FAULT：motion_plan / check_collision / set_end_effector_pose
#    根因：Failed to read whole body state from WBC（整机状态读不到），重启不恢复
```

⚠️ **`set_gripper_command(is_blocking=True)` 夹住东西时会一直等到超时，实测 8.8 秒。**
判断是否夹住要用"**宽度 + 力矩双判据**"：夹空时宽度到限位、力矩约 9.5 Nm；夹住实物时宽度卡住、力矩 30–39 Nm。

⚠️ **绝对不能调 `r.request_shutdown()`**——它会关掉整机（不是退出 SDK 实例）。收尾只用 `r.destroy()`。

### 3.4 低延迟执行器（推荐直接用）

`robot_exec.py` 常驻机器人，一次 init，之后 JSONL 收发：

| 命令 | 说明 |
|---|---|
| `{"cmd":"state"}` | 关节角 + 腕部位姿 + 夹爪宽度/力矩 → **46–90 ms** |
| `{"cmd":"cart","x","y","z","tilt"}` | 逆解 + 移动到笛卡尔位姿（tilt 为工具倾角） |
| `{"cmd":"joints","q":[7]}` | 直接关节角移动 |
| `{"cmd":"grip","w","v","eff"}` | 夹爪 |
| `{"cmd":"depth"}` | 深度统计 |

对比：每个动作新起进程要付 **3–5 秒** SDK 初始化（`init` 3.2–5.3 s），这是之前"每步之间特别慢"的主因。

---

## 四、针对遮挡任务的技术建议

### 4.1 世界模型（建议先做这个）

维护一个以 `base_link` 为参考的世界状态，每个物体一条：

```json
{
  "id": "obj_3",
  "label": "green_cube",
  "size_cm": [5.9, 5.3, 4.2],
  "state": "in_container",          // visible | occluded | in_container | missing
  "last_seen_pose": [0.77, 0.16, 0.735],
  "last_seen_ts": 1699330000.0,
  "container_id": "box_1",
  "confidence": 0.86
}
```

容器单独一条，需要额外记：
- **开口范围**（xy 包围盒 + 开口所在高度）
- **内部底面高度**（决定抓取能伸多深）
- 壁厚 / 壁高（决定手指进去后有没有余量）

### 4.2 遮挡推理的判定逻辑

物体从 `visible` 变 `missing` 时，**不要直接标成消失**，按这个顺序判：

1. 消失前最后一帧的位置，是否落在某个容器的**开口投影区域**内？
2. 消失过程中，手/物体是否**朝容器方向移动**过（看关节角 + 末端轨迹，这条机器人上很好拿）？
3. 容器内部是否有**新增厚度**（深度图上容器底面抬高）？

三条里有两条成立 → 标记 `in_container`，置信度按命中数给。

> 这台机器人的优势：**腕部相机是跟着手动的**。手伸进盒子的全过程相机都在，遮挡期间的画面不会丢——这比固定相机好做得多。

### 4.3 容器识别的做法

深度图里，容器和普通物体的区别是**"有内部空腔"**：

- 用高度图找局部"下凹"区域：同一 xy 位置上，四周高、中间低
- 开口边缘 = 高度突变的一圈闭合轮廓
- 内部底面 z（一般落在 3–10 cm 深度范围）
- 尺寸过滤：开口 < 5 cm 装不进 5 cm 方块；开口 < 12 cm 平行夹爪进去会撞壁

### 4.4 容器内抓取的真实约束（这条最关键，先确认再写代码）

| 约束 | 数值 | 后果 |
|---|---|---|
| 夹爪张开 | 12 cm | 盒子**开口内径 < 12 cm** 就伸不进去 |
| 手指厚度 | 约 2 cm | 物体贴壁时夹不到 |
| 进深 | 指长 18.9 cm | 盒子**深度 > 12 cm** 时指尖够不到底 |
| 无碰撞检查 | — | **规划器坏了，没有任何碰撞检测**，进盒全靠人看 |
| 进盒路线 | 只能垂直 | 侧向进入会撞壁（且无人可拦） |

**结论：先量盒子。建议先用开口 15 cm × 深 8 cm 以内的浅盒，物体放中间不贴壁。**
盒子内壁如果是深色/黑色，深度会大面积失效（主动红外被吸收），建议内壁浅色。

### 4.5 感知端要注意的两个环境问题

1. **黑桌布是深度杀手**：D405 主动红外被吸收，有效像素从 79% 掉到 13%。目标下面垫白纸立刻改善。
2. **场地里有 100+ 台同型号机器人**，DDS/红外互相干扰。取图一律做**多帧时域中值**（实测 6–8 帧能把逐像素标准差压到 12 mm 量级）。

---

## 五、建议的推进顺序

| 阶段 | 目标 | 验收标准 |
|---|---|---|
| **P0** | 桌面物体检测 + 三维定位 | 桌上放 3 个已知尺寸物体，位置误差 < 1 cm |
| **P1** | 容器检测（开口 + 内底） | 给出开口 bbox 与内底高度，误差 < 1 cm |
| **P2** | 遮挡推理 | 手把物体放进盒子，系统输出 `in_container` + 正确的容器 id |
| **P3** | 容器内抓取 | 从盒子里把物体取出来，放在桌面指定位置 |

每阶段都可以用观察台（`http://127.0.0.1:8410/`）做人工核对：能拖框看三维点、看深度视图。

---

## 六、现成可复用的代码

| 文件 | 内容 |
|---|---|
| `robot_exec.py` | 机器人侧常驻执行器（低延迟控制，强烈建议基于它开发） |
| `robot_agent.py` | 只读代理：取图 + 框选返回三维点 + IK 可达性 |
| `robot_link.py` | 上一行的本地侧驱动 |
| `robot_web.py` | 本地观察台 |
| `presets/look_down.json` | 俯视姿态关节角（腕部相机下俯 59°，深度有效像素 79%） |
| `docs/GRASP_3D.md` | 几何链完整推导 + 全部踩坑记录 |

---

## 七、几个已知的坑，别重复踩

1. `request_shutdown()` 关整机
2. 相机外参对臂上相机是静态值，必须用 FK
3. 逆解必须传当前关节角当 seed
4. 工具轴是安装帧 +X，指长 0.189 m
5. 腕部相机偏轴 7 cm，别拿画面中心当抓取点
6. 夹爪阻塞模式夹住会等 8.8 s
7. **绝对不要低空横移**：离桌面 2 cm 横着挪，会把物体直接扫飞。正确顺序是**高空横移对准 → 原地垂直下降**
8. 多相机不能同时 enable

---

## 附：用 `robot_exec.py` 时的一个必踩坑（13:40 补）

SDK 会往 **stdout** 打日志（如 `[info] Resolved robot type...`），
如果客户端简单地"发一行、读一行"，会把日志当成响应，导致**结果错位**（表现为扫描结果每次都不一样、耗时显示 0 ms）。

正确做法：请求带自增 `id`，响应里 `id` 匹配上才算数。

```python
_ID = 0
def call(p, obj):
    global _ID
    _ID += 1
    obj = dict(obj, id=_ID)
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
    for _ in range(200):
        line = p.stdout.readline().strip()
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        if r.get("id") == _ID:
            return r
```

参考实现：`tools/plan_grasp.py`。
