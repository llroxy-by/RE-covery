# ota_task 目录最小修复记录

目标：galbot-echo，2026-11-07。
用户已批准方案并完成执行前最终命令二次确认。

已执行（sudo 密码仅交互输入，未保存）：

```sh
sudo install -d -o galbot -g galbot -m 0755 /userdata/update/ota_task
```

执行前检查目标不存在且非软链接。原父目录 `/userdata/update` 的属主 root:root、0755 保留。

结果：新子目录 galbot:galbot、0755；以 galbot 用户执行 test -w 成功。
快照：before-ota-directory-repair.txt、after-ota-directory-repair.txt。

回退：仅当新目录为空时，按已确认方案使用 sudo rmdir /userdata/update/ota_task；如服务已经生成任务文件，不删除内容，停止并核对。

尚未执行：删除共享内存、停止或重启系统服务、重启机器人。
当前共享内存仍满，因此尚不能宣称通信服务或 SDK 已完全恢复。
下一步建议在现场确认停稳后正常重启，以释放旧共享内存；重启后只读核对日志、NRestarts 和内存增长，验证修复是否解决实际服务故障。可能仍有其他故障，需根据新日志处理。

## 重启后验收（2026-11-07 07:58，机器人报告时间）

- `/userdata/update/ota_task` 保持 galbot:galbot、0755。
- service_hpu_comm 为 active/running；开机约 3 分钟及其后复查 NRestarts 均为 0。
- 日志显示 Client connect success: 192.168.1.66:14001；当前启动日志未见原目录创建权限错误。
- 共享内存 3.8G/31G（13%）；启动本控制台 SDK 后复查约 4.0G/31G（13%），仍有 27G 可用。短时观察未重现原有每约3秒服务重启的情况，不能代表长期稳定性已经验证。
- 网页真实 API：16/16 关节新鲜反馈，底盘反馈可用，双夹爪反馈均可用。
- 没有发送运动指令，连续摆手默认关闭；当前左臂姿态不符合20°向上摆动计划条件。
