# 有线连接与 SDK 故障诊断

2026-11-06 接手后实测：

- 有线 SSH：`galbot@fe80::69c8:289b:61e:26f4%en5`，复用 `/tmp/kitt-console-live.sock`，可正常执行只读命令。
- 主机名 `galbot-echo`，HPU 到 XCU `192.168.100.66` ping 2/2 成功。
- eth0 `192.168.100.88/24`、eth1 `192.168.1.88/24`，与发现服务配置一致。
- `/dev/shm`：31G 总量，31G 已用，0 可用，100%。inode 仅使用 1%，是容量耗尽。
- SDK 日志包含 `DDS Failed to create segment`、`boost::interprocess_exception::library_error`、`Could not initialize DataSharing writer pool`，并有服务连接超时。
- `service_hpu_comm` 持续输出 `Waiting for discovery server connection`。
- 发现服务仍在，UDP `192.168.1.88:11888` 接收队列约 67MB，近期日志停在 22:36。其是否卡死尚未最终确认。

结论：共享内存耗尽与 SDK 创建共享内存失败直接吻合，是必须先处理的已确认阻塞。尚不能断定是哪一进程导致耗尽，也没有证据要求先 init 导航对象或使用 root。

已停止本控制台重试；未删除机器人文件、重启服务、清理共享内存或重装 SDK。请现场技术人员识别占用者并按设备维护流程恢复。切勿直接 `rm -rf /dev/shm/*`，其中可能包含运行中控制服务的数据。

恢复后验收：共享内存有可用空间 → SDK init 成功 → 16 个关节状态持续更新及底盘状态可读 → 现场确认后再测试运动。当前尚未执行底盘旋转。
