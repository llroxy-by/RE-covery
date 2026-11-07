# 队友提交核对与执行优化

## 已读取的 GitHub 提交

- [555589d：自主识别整块物体并区分遮挡与拿走](https://github.com/llroxy-by/RE-covery/commit/555589dbae612a4e9eb803d2be6d2f5f29dd1d79)，分支 `feat/occlusion-memory`。读取了提交差异、演示文档，以及 `rebench/backend.py`、`runner.py`、`perception.py`、`occlusion.py`。
- [d6d5fce：离线控制台演示](https://github.com/llroxy-by/RE-covery/commit/d6d5fce8fbf9e651714abf3b1039ff90f2187f29)，分支 `feat/approved-web-demo`。其 README 明确：动作、耗时、置信度、历史是模拟，没有接机器人或视觉接口。

Git 命令传输两次网络失败后，改用 GitHub 连接器读取当前分支列表与文件。因此本次已审阅这些代码，但没有把这些分支声称为已合入本地。

## 与当前工作的关系

遮挡分支在感知/决策层与我们的任务有关：自动找到整块目标、保留身份记忆、区分目标被遮挡和已被拿走；`WorldModel`、`check_vertical_approach` 等可以成为后续集成点。

目前它不是新的真机提速实现：手机演示几何仍为假设相机位姿与平面反投影；`SdkBackend.observe()`、`observe_world()` 仍抛 `NotImplementedError`，运动及夹爪接口标有 TODO。不能直接把这些模拟坐标、运动封装替换进已实测的 G1 执行器。此次保持它们不变，改进的是 `vision-rescue` 的实际执行链路；尚未接入队友的遮挡世界模型。

## 已完成的本地优化

1. `move_checked`：一条请求内完成 IK、几何检查、完整状态碰撞检查、同一逆解执行与反馈检查；没有重算执行解或改成无反馈的连续发指令。
2. 夹爪按反馈衔接：要求反馈更新、确认活动或已在目标位置，再读取连续稳定的停稳状态；不依靠固定长 sleep，也不把旧的 moving=False 当作新指令完成。
3. 快速档运输 0.12 rad/s（历史实测 0.08），接近 0.06 rad/s；保留原有 0.12 rad/s 执行器上限，未提高系统/驱动/硬件限速。显式逐步速度参数仍优先。
4. 批次耗时/RPC 时戳可追踪；终端不再重复打印所有步骤的大包结果。
5. 深度识别失败会令 capture 失败，阻止批次继续。

23 项离线回归与 Python 3.8 语法检查通过。真机只读取过 4 次夹爪状态，确认 timestamp_ns 更新；没有发运动、开爪或模式切换指令。新快速档与按反馈等待尚需后续实际抓取验证，不能把预计收益写成已测加速结果。

历史橙色那轮的 SDK 记录：9 次移动共约 30.27 秒，9 次 IK 预演共约 0.68 秒，2 次夹爪命令共约 3.51 秒。该记录不含聊天决策之间的空档；新增时序日志正是为补上这一部分。

SDK 依据：[G1 1.10.0 Python API](https://developer.galbot.com/docs/SDK/1.10.0/g1/zh/api_python_reference)，`set_joint_positions` 是带速度限制的平滑关键帧轨迹，不用于高频逐帧伺服。
