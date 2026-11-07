# 抓取与放盒会话

适用于本项目已连接的 G1 / SDK 1.10.0。电脑运行本地客户端，机器人复用已有 SDK；执行器通过 SSH 标准输入在内存运行，不上传文件或安装 SDK。

## 入口

在仓库根目录，复用现有 Python 环境（需要 numpy、opencv-python）：

```sh
# 当前视角只读识别，不移动或开爪
.venv-vision/bin/python -B vision-rescue/tools/lowprobe_grasp.py --dry

# 常驻只读会话，从标准输入接收 JSONL
.venv-vision/bin/python -B vision-rescue/tools/pickplace_session.py

# 现场确认后使用快速档，仍需给每一步提供当前场景的坐标和检查参数
.venv-vision/bin/python -B vision-rescue/tools/pickplace_session.py --execute --profile fast

# 只读网页预览
.venv-vision/bin/python -B vision-rescue/tools/preview_server.py --port 8420

# 离线回归，不连接机器人
.venv-vision/bin/python -B vision-rescue/tools/test_grasp_safety.py
```

会话中的只读命令：

```json
{"cmd":"state"}
{"action":"capture","tag":"scene","probe":true,"radius":0.18}
{"action":"quit"}
```

`ROBOT_HOST` 可指定 SSH 目标，`ROBOT_SOCK` 可指定已有连接的控制套接字；认证交给本机 SSH，不把密码写入源码。默认地址是现场设备的历史地址，换网络后需核对。

现场确认人员、工作区与急停条件后，启动参数 `--execute` 才允许运动。当前入口为开发用 JSONL 工具，不是打开网页即自动抓取的成品。

## 连续执行

`batch` 接收步骤数组，按反馈自动衔接，任一步失败即停止；不需要 AI 每走一步再发送下一步。支持：

- `move`：显式指定 `xyz`、`table_z`、`min_tip` 和 `speed`；始终新建一次性预演并执行同一组关节角，不缓存旧场景下的逆解。
- `expect_grip`：核对静止状态和允许的开口范围。
- `capture`：保存本机照片与识别快照。
- 普通 SDK 命令：例如 `state`、`grip`；运动仍受执行器开关约束。

常驻连接减少重复初始化；批次内部按反馈直接衔接，不为每一步等待聊天回复。`move` 现在用一次 `move_checked` 请求完成预演、检查和同解执行，省去预演结果回到电脑后再发执行指令的往返。夹爪默认根据新鲜、连续稳定的反馈结束等待，不再固定等待 1.3–4 秒；反馈陈旧或超时会停止批次。

速度档位：

| 档位 | 运输 rad/s | 接近 rad/s | 夹爪 m/s |
|---|---:|---:|---:|
| precise | 0.06 | 0.03 | 0.04 |
| balanced（默认） | 0.08 | 0.04 | 0.05 |
| fast | 0.12 | 0.06 | 0.07 |

`move` 的 `phase: "transport"` 使用运输速度；不填 phase 时使用接近速度。显式 `speed` 或夹爪 `v` 优先于档位，不会偷偷覆盖原有低速设置。运输档只能用于已核对的高处搬运，不能把贴桌接近标成运输来提速。SDK 平滑轨迹、碰撞检查、一次性解和到位检查仍然保留。

新代码与档位已通过离线验证，并在真机执行过；尚无相同路径的提速对比。历史运输实测用 0.08 rad/s、约 16 cm 的路径耗时 9 秒；0.12 是速度上限提高 50%，不等于整轮任务保证缩短 50%。

批次返回 `timing`（每步耗时和步骤间 gap_ms）；本机日志新增 RPC 耗时和时间戳，用于区分网络、控制器运动和本地等待。默认输出压缩摘要，完整结果可用 `--verbose` 查看。要保留旧夹爪固定等待方式，可显式指定 `wait_until_stopped: false` 和 `settle`。

不要直接重放实测记录里的坐标。物体、盒子、相机位姿、桌面高度和夹爪工具长度必须与当前场景一致。采样 FK 和碰撞服务只能检查其已知模型，不代表现实障碍物已完整建模。

## 兼容与验证范围

旧 `cart` 直接求逆解后下发的入口已拒绝执行，改用 `ikpath` 返回的 `plan_id` 加 `execute_plan`。旧手册里的内嵌源码和旧版抓取脚本未同步迁移，不能据此认为它们仍可直接执行。

`lowprobe_grasp.py --execute` 是实验性自动扫描流程；本次完成的是人工确认目标后的分步抓取/放盒，不是任意场景自动识别抓取验证。

本机 23 项离线回归通过；此前现场已完成绿色抓取、橙色平放抓取，并放入同一盒子，见 `GRASP_PROGRESS_2026-11-07.md`。此后的速度档位、反馈等待和单请求运动已在真机使用；最新结果见 SORT_TEST_STATUS_2026-11-07.md，尚不代表完整自动分类已验证。照片、逐条运行数据、SSH 认证信息不随代码提交。
