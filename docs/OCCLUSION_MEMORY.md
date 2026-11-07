# 遮挡记忆与盲抓 · 实现说明

> 场景：积木**不动**，在它前面放一个盒子，把腕部相机的视线挡住。
> 要求：不被干扰、记得原位、照样抓起来。
>
> 代码：`rebench/occlusion.py`（世界模型 + 几何判据）、`rebench/runner.py`（接入）、
> `rebench/backend.py`（观测契约）。测试：`rebench/tests_occlusion.py`（32 项）。

## 1. 现状

| 项 | 状态 |
|---|---|
| 世界模型 / 遮挡推理 / 走廊判据 / 抓取判据 | ✅ 已实现，32 项离线测试全过 |
| mock 端到端（盒子挡在前面 → 仍然复位成功） | ✅ `python -m rebench --mock --rounds 1 --occlude block_red` |
| 对照组（关掉记忆 → 静默放弃） | ✅ 同命令加 `--no-occlusion-memory` |
| **真机 `SdkBackend.observe_world`** | ❌ **未实现**，需要按 §5 补约 40 行 |

没实现时不会崩：`Runner._world_obs()` 捕获 `NotImplementedError`，退回原槽位级观测，
并在第一帧打一行 `[遮挡记忆] 后端没有实现 observe_world…`。也就是说**现在的真机行为和改之前完全一样**，
遮挡目标照样被静默放弃 —— 这不是 bug，是诚实降级。

## 2. 为什么原链路会"什么都不做"

```
observe()  →  slot=None           （看不见）
judge()    →  UNOBSERVABLE        （不能判定，不敢说它不见了 —— 这一层是对的）
needs_action() → 只收 MISSING / WRONG_SLOT / MISPLACED
               → 待复位队列里没有它
run_round()    → 跳过
```

于是机器人**什么都没干**，看起来像"没被干扰"，实际是放弃了。所以缺的不是"更好的检测"，
而是一个**跨帧的三维世界模型**：它必须能在看不见的时候，仍然回答"这东西在哪儿"。

## 3. 四条判据

### 3.1 记忆只在看见时更新

```python
w.see("block_red", (0.78, 0.18, 0.73))   # 只有这里会改位置
w.resolve("block_red", slot_reason="occluded")   # 这条只改状态，不动位置
```

遮挡期间记忆位置必须**一动不动**（有专门的测试钉这条）。否则"遮挡"会被当成"物体在漂移"。

### 3.2 记忆必须允许被推翻

三条证据，前一条是必要条件：

| 证据 | 含义 |
|---|---|
| E1 有历史位置 | 从没看见过的东西谈不上"遮挡" |
| E2 视线被挡 **或** 深度层说该区域被遮 | 用 `Box3D.segment_hits(相机, 记忆点)` 判 |
| E3 记忆未过期（默认 300 s） | 过期就认它真的没了 |

三条都成立 → `occluded`；只有 E1 → `missing`。

**为什么要留这个出口**：如果"记得它"就永远认为它在，机器人会对着早就被人拿走的积木
反复盲抓，而且每次都"夹空"再重试，把 40 秒超时耗光。

### 3.3 盲抓必须有几何依据

规划器坏了、**没有任何碰撞检测**（任务书 §3.3），所以盲抓前必须算清楚：目标正上方那条
垂直走廊里有没有东西。

平行二指是**有朝向**的，两个方向需要的净空差 5 倍：

| 方向 | 净空 | 为什么 |
|---|---|---|
| 开合方向 | 半个开度（目标 4 cm 也按 6 cm 算）+ 指厚 2 cm + 余量 = **10 cm** | 手指要张得开 |
| 垂直方向 | 指片宽度一半 + 余量 = **3.25 cm** | 只是指片本身 |

所以"盒子挡在正前方 10 cm"这种情况，**把手腕转 90°、让手指平行于盒子侧壁就能下去**。
这是"不被干扰还能抓"的关键，不是放宽容差 —— 走廊仍然是真实几何，只是换了个更合适的进入姿态。
代码里 `try_rotations=True` 会依次试 0°/90°/45°，并在决策里返回实际用的 `gripper_yaw_deg`。

盒子**压在目标正上方**时，怎么转都过不去 → 一律拒绝，并报出是哪个障碍物在挡
（报"挡住朝向最多的那个"，不是"第一个挡住默认朝向的"，否则会让人去挪一个挪了也没用的盒子）。

### 3.4 遮挡下不能说"确认抓起"

原判据是"开口停在目标尺寸附近 + 抬升后原位变空"。第二条在遮挡下**会退化成恒真**：
槽位本来就看不见，`slot is None` 不代表空 —— 夹空也会被判成抓成功。

现在把"这块区域看得见吗"作为独立前提：

| 开口 | 区域可见 | 原位 | 判定 |
|---|---|---|---|
| 对 | 是 | 空 | `ok` 确认抓起 |
| 对 | **否** | — | `unverified` 夹住了，但无法确认它离开了原位 |
| 不对 | — | — | `failed` 夹空 |
| 对 | 是 | 还有东西 | `failed` 夹的是别的东西 |

`unverified` 不算失败（物体确实在手里），**但不能拿去通过验收**。本轮的 `pick` 返回
`verified=False`，最终由"复位后再看一眼"的完整观测来确认 —— mock 里就是这个时序：
盲抓记 `unverified`，放回 home 后 `judge` 才转 OK。

## 4. 决策表

| 记忆 | 走廊 | 动作 |
|---|---|---|
| fresh（≤30 s） | 通 | `blind_pick` 直接用记忆位置抓 |
| fresh | 被挡 | `refuse` 要求挪开障碍物 |
| stale（30–300 s） | 通 | `reobserve` 先把腕部相机挪过去确认，再抓 |
| stale | 被挡 | `refuse` |
| expired（>300 s） | — | 当作不存在，不复位 |

`reobserve` 的候选位姿由 `suggest_viewpoints()` 给出：把相机挪到 `(±12 cm, ±12 cm, +10 cm)`
六个候选位，逐个做视线遮挡判断，返回按"偏移量小、不挡"排序的列表。
**它只做几何判断，不保证 IK 可达** —— 可达性必须由调用方用
`inverse_kinematics(pose7, ["left_arm"], frame, "base_link", {"left_arm": 当前关节角}, True)`
验过（任务书 §3.2：不传当前关节角当 seed 会直接 FAULT）。

## 5. 真机要补的那 40 行

`rebench/backend.py` → `SdkBackend.observe_world()`。返回：

```python
{
  "objects": {obj_id: {"visible": bool, "area_visible": bool,
                       "slot": str|None, "pose": (x,y,z) in base_link,
                       "slot_reason": "occluded"|"empty"|None}},
  "obstacles": [Box3D, ...],
  "camera_pose": (x,y,z),
}
```

骨架（**未在真机验证，API 名以现场 `get_frame_names()` / `get_joint_group_names()` 为准**）：

```python
def observe_world(self):
    import numpy as np
    from .occlusion import Box3D
    from .perception import deproject, to_base, SceneModel, estimate_table_z

    S = SensorType
    # ① 取深度 + 内参
    d = self.robot.get_depth_data(S.LEFT_ARM_DEPTH_CAMERA)
    K = self.robot.get_camera_intrinsic(S.LEFT_ARM_DEPTH_CAMERA)["K"]
    depth = np.asarray(d["data"], np.uint16).reshape(d["height"], d["width"]) / d["depth_scale"]

    # ② 时域中值 6–8 帧：场内有 100+ 台同型号机器人，红外互相干扰，
    #    单帧深度的逐像素标准差到不了 12 mm 量级（任务书 §4.5）
    depth = np.median(np.stack([depth] + [self._one_depth() for _ in range(5)]), axis=0)

    # ③ 相机位姿必须用 FK，不能用 get_sensor_extrinsic（臂上相机返回静态标称值）
    js = self.motion.get_chain_joint_state()
    T = self.motion.forward_kinematics(
        "left_arm_camera_color_optical_frame", "base_link", js)   # 4×4

    pts = to_base(deproject(depth, K), T)                          # 现成函数，复用
    table_z = estimate_table_z(pts, SceneModel())

    # ④ 每个物体：取它槽位附近的点簇，质心 + 中位高度
    #    这一步可以直接复用 perception.observe_slots() 的槽位内点集
    objs = {}
    for spec in self.bench.objects:            # 需要把 bench 传进 backend
        cloud = self._cloud_near(spec)         # 槽位范围内 (N,3)
        if cloud is None:
            objs[spec.id] = {"visible": False, "area_visible": False,
                             "slot": None, "pose": None, "slot_reason": "occluded"}
        else:
            c = cloud.mean(axis=0)
            objs[spec.id] = {"visible": True, "area_visible": True,
                             "slot": self._slot_of(c), "pose": tuple(c),
                             "slot_reason": None}

    # ⑤ 障碍物：台面以上、x 比台面中心更靠近 base 的连通块 → 一个 Box3D
    obstacles = []
    for cl in self._clusters_above_table(pts, table_z, min_points=800):
        obstacles.append(Box3D(x0=cl.x.min(), x1=cl.x.max(),
                               y0=cl.y.min(), y1=cl.y.max(),
                               z0=table_z,   z1=cl.z.max()))

    # ⑥ camera_pose 必须填，遮挡推理要用它算"视线有没有被挡"
    return {"objects": objs, "obstacles": obstacles,
            "camera_pose": tuple(T[:3, 3])}
```

三个最容易写错的地方：

1. **`get_sensor_extrinsic` 不能用**。它对臂上相机返回的是静态标称值，不随关节变化，
   相机一动位置就全错。必须 FK。
2. **`area_visible` 和 `visible` 是两个字段**。抓起来之后物体当然看不见（在夹爪里），
   但区域是可见的、空的 —— 混成一个字段，夹空会被误判成抓成功。
3. **障碍物要按"台面以上"来分**，不要把桌布/阴影也算进去；黑桌布会让深度大面积失效
   （有效像素 79% → 13%），目标下面垫白纸。

## 6. 现场流程

**mock（现在就能跑）**

```bash
# 盒子挡在前面，仍然复位成功
python -m rebench --mock --rounds 1 --occlude block_red

# 对照组：关掉记忆 → block_red UNOBSERVABLE、NOT READY
python -m rebench --mock --rounds 1 --occlude block_red --no-occlusion-memory
```

`--occlude` 会先 `warm_memory()` 记下位置，再放盒子 —— 顺序不能反：盒子先盖上，
就没有"它原来在哪儿"这回事了。

**真机（补完 §5 之后）**

```bash
python -m rebench --real --rounds 1 --occlusion-pause
# 打印：[记忆] 已记下：block_red (x=0.78, y=0.18, z=0.73) …
#      现在把盒子放到积木前面（挡住它），放好后按回车继续…
```

放盒子时注意：

- 盒子**不要压在积木正上方**，否则走廊判据会拒绝（这是对的，别去关它）；
- 盒子挡视线、但不挡垂直下降通道 —— 就是"放在前面"，不是"扣在上面"；
- 桌面垫白纸，盒内壁浅色。

## 7. 安全边界（这几条不能为了演示效果放宽）

1. **走廊被占就停**。没有碰撞检测，撞了没人拦。`refuse` 是正确结果，不是失败。
2. **绝不低空横移**。抓取路径只能是"高空横移对准 → 原地垂直下降 → 夹 → 垂直抬升"
   （任务书 §7.7：离桌面 2 cm 平着挪会把物体扫飞）。
3. **盲抓只在记忆 fresh 时做**。超过 30 s 一律先重观测 —— 这段时间里人是可能碰过它的。
4. **`unverified` 不许当验收通过**。它在动作层成功，在验收层是未知。
5. 任何情况下不要 `request_shutdown()`（关整机），收尾只用 `destroy()`。

## 8. 已验证 / 未验证

**已验证（32 项测试 + 两条端到端）**

- 遮挡不改写记忆；遮挡下仍能复位成功；关掉记忆则 `UNOBSERVABLE` / NOT READY；
- 盒子在正前方：默认朝向被挡 → 转 90° 通过（`rotated=True`）；
- 盒子压在正上方：0/45/90 三个朝向全拒，报出正确的障碍物索引；
- 模型过期（>300 s）→ `missing`，不再盲抓；
- 遮挡下抬升 → `unverified`，不是 `ok`；
- 视觉上"记忆能被推翻"：视线没被挡 + 深度说空 → `missing`（不会对不存在的积木反复盲抓）。

**未验证**

- **真机 `observe_world` 未实现，全部数值都是 mock 的**（`MOCK_LAYOUT` 只是模拟用）；
- 障碍物分割用的是高度图 + 连通域，还没在真实深度图上调过阈值；
- `reobserve` 的候选位姿没做 IK 可达性检查；
- 7 cm 的腕部相机偏轴意味着**记忆位置离"夹爪正下方"有偏差**，真机上要么按
  `T_cam2tool` 折算到工具轴，要么先用一次夹空找到偏移量 —— 这条必须在第一次上机时校准，
  否则"对准了却夹空"。

## 9. 命令速查

```bash
python -m unittest rebench.tests_occlusion        # 32 项，0.3 s
python -m rebench.occlusion                       # 合成场景自测（打印推理过程）
python -m rebench --mock --rounds 1 --occlude block_red
python -m rebench --real --rounds 1 --occlusion-pause
```
