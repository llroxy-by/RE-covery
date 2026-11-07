# RE:BENCH 现场 Runbook

> 目标：在任何一次真机操作前，先用 10 分钟把"会毁掉整场演示"的未知项消掉。

## 一、上机前必做（按顺序，不要跳）

### 1. 版本与环境

```bash
python -c "import galbot_sdk, sys; print(galbot_sdk.__file__)"
```

确认 SDK 版本与机器人环境版本匹配（V1.10.0 ↔ 环境 V1.18）。不匹配会出现"接口存在但行为异常"。

### 2. 三条事实核对（把输出贴到群里存档）

```python
from galbot_sdk.g1 import GalbotRobot
r = GalbotRobot(); r.init()

print(r.get_joint_group_names())      # 组名真值
print(r.get_frame_names())            # 相机 frame 真值
print(r.get_gripper_state())          # 字段名 + 单位

# 关键：全开与全闭的零点读数
r.set_gripper_command(0.100, 0.05, 5.0, True, 3.0)
print("OPEN :", r.get_gripper_state())
r.set_gripper_command(0.000, 0.05, 5.0, True, 3.0)
print("CLOSE:", r.get_gripper_state())
```

**为什么这条最重要**：全闭读数如果是 0.002 而不是 0，`runner.py` 里 `w <= 0.008` 的判据就会误判。拿到真值后立刻改阈值。

### 3. 急停与安全距离

- 急停按钮松开、手放在急停上；
- 手臂运动包络 1 m 内清空；
- 第一次动臂用 `max_speed=0.1`，只用单臂。

### 4. TCP 轴向验证（一次试抓）

不要假设"夹爪开合轴 = Y、接近方向 = X"。做一次空抓：夹爪张开 → 移到物体上方 5 cm → 低速下降 → 闭合，观察两指是否**横跨**物体。如果夹到角上，说明 yaw 偏了 90°，在生成抓取位姿时补一个旋转。

## 二、无真机时的开发与演练

```bash
python -m rebench --mock --rounds 3                      # 基础流程
python -m rebench --mock --rounds 10 --perturb 2         # 连续 10 轮压测（每轮人为打乱 2 个）
python -m rebench --mock --rounds 5 --fail-rate 0.5      # 极端失败率，看降级阶梯
```

看 `rebench/runs/run.jsonl` 里的 `ev` 字段：`pick_fail` 的 `level` 分布就是你演示时要讲的"失败类型"。

## 三、接入真机的顺序（严格串行，每步验证完再下一步）

| 步 | 做什么 | 通过标准 |
|---|---|---|
| 1 | `SdkBackend` 只实现 `open/close/gripper_width` | 手动开合 5 次，读数稳定可复现 |
| 2 | 实现 `move_line`，只做"抬起 10 cm" | 不报错，位置到位 |
| 3 | 硬编码一个物体的抓取点位（**先不用视觉**） | 连续 5 次抓取成功 ≥4 |
| 4 | 接入 `observe()`，只做"槽位内有无物体"二元判断 | 三物体判定准确 |
| 5 | 接入位姿微调（dx/dy/yaw） | 偏差 <15 mm |
| 6 | 联调 `run_round()` 全流程 | 一轮无人工介入 |

**第 3 步是整个项目的分水岭。** 如果第 3 步做不到 4/5，不要往上加视觉，先查夹爪力和下降速度——大概率是下降太快把物体推走了。

## 四、异常处置

| 现象 | 首查 |
|---|---|
| 夹爪读数一直是 0 | 组名错 / 服务未加载 / 返回单位是 mm |
| 抓空率高但视觉定位准 | 下降速度 >0.05 m/s，或夹持力 <8 N |
| "抓到了"但复查物体还在原位 | 假阳性：夹到盒壁，需加"抬升后原位为空"判据 |
| 轨迹执行超时 | 目标位姿超出可达范围，先做 IK 预筛 |
| 程序中途崩溃 | 直接重启，`runs/state.json` 会从断点续跑 |

## 五、遮挡场景（盒子挡在积木前面）

目标不动、但被挡住的复位，见 `docs/OCCLUSION_MEMORY.md`。两条命令先跑通逻辑再上机：

```bash
python -m rebench --mock --rounds 1 --occlude block_red                     # 应该 READY
python -m rebench --mock --rounds 1 --occlude block_red --no-occlusion-memory  # 应该 NOT READY（对照组）
```

真机上 `SdkBackend.observe_world()` **还没实现**，此时行为与改之前完全一样（遮挡目标静默放弃），
第一帧会打一行 `[遮挡记忆] 后端没有实现 observe_world…` 提示。补法见文档 §5。

| 现象 | 首查 |
|---|---|
| 遮挡后目标被判 UNOBSERVABLE 且没动作 | `observe_world` 没实现，或缺 `camera_pose` |
| 提示"走廊被占"但看着没东西 | 盒子确实压在目标正上方（此时不该抓），或障碍物盒算大了 |
| 盲抓记 unverified 但最终验收 OK | 正常：动作层成功、验收层靠复位后的观测确认 |
| 盲抓一直夹空 | 腕部相机偏轴 7 cm，记忆位置没折算到工具轴 |
