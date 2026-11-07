# 共享内存占满：只读诊断证据

检查对象：galbot-echo，通过有线 SSH 读取；没有修改机器人文件、权限或服务。

## 已确认事实

- `/dev/shm` 总量 31G，已用 31G，可用 0，100%。
- `fastrtps_*`：2980 个文件，实际占用 28,833,525,760 字节。
- `fast_*`：3672 个文件，实际占用 3,940,155,392 字节。
- 按文件修改时间统计，在 01:39–02:42 的多数分钟内，每分钟有 80 个相关文件、428,441,600 字节（约 409MiB）的实际占用。
- 6146 个相关文件、28,940,902,400 字节（约 27GiB）未出现在当前账号可读的进程映射里。121 个进程无法读取，故不能据此证明这些文件都已无人使用，不能直接删除。
- 通信日志有 3313 份 `service_hpu_comm` 进程实例日志。文件数不是重启计数，可能包括系统日志保留历史。
- systemd 实测：`galbot_svc_hpu_comm.service`，`Restart=always`，`RestartUSec=100ms`，`NRestarts=1581`。
- journal 中 02:55:57 和 02:56:00 均出现：

```text
terminate called after throwing an instance of 'std::filesystem::__cxx11::filesystem_error'
filesystem error: cannot create directories: Permission denied [/userdata/update/ota_task]
Main process exited, code=killed, status=6/ABRT
Scheduled restart job, restart counter is at 1580 / 1581
```

- 02:56:01 最新实例进入持续等待发现服务连接状态；SDK 此后初始化/通信超时。

## 判断及待确认项

明确故障是通信服务创建更新目录被拒绝，随后发生大量异常退出与自动重启。重启的时间规律与 DDS 共享内存增长吻合，强烈提示反复启动留下通信共享内存是主要来源；仍需维护人员核对退出清理与文件归属，不能宣称每个残留都已归因。

控制台 SDK 客户端的启停也可能贡献部分残留，不能完全排除；当前本控制台已暂停 SDK 自动重连。

## 请现场技术人员处理

1. 核对服务运行账号、`/userdata/update` 的实际权限和挂载情况，以及正常设备应有的目录布局。
2. 按官方维护流程修复目录创建失败和服务重启问题。不要只反复重启整机。
3. 确认不再使用的 DDS 共享内存对象后进行受控清理；不要执行全量 `rm -rf /dev/shm/*`。
4. 恢复后验证重启计数不再增长，共享内存使用量稳定，SDK init 成功、关节与底盘反馈正常。

本报告不是授权修改设备的操作单。系统目录、服务和共享内存清理由现场技术人员处理。
