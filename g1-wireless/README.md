# Galbot G1 无线底盘与升降调试

Windows 本机通过 OpenSSH 连接已安装 SDK 1.10.0 的 G1。远端 Python 代码由 SSH 命令在内存执行。网页只读，移动按钮是浏览器模拟。真机动作从独立命令行启动。

## 配置与只读检查

在 PowerShell 设置下列值。主机指纹需从可信渠道核对，SSH 密码只在 OpenSSH 的交互提示处输入。

```powershell
$env:G1_ROBOT_HOSTNAME = '<机器人主机名>'
$env:G1_SSH_FINGERPRINT = '<已核对的 ED25519 SHA256 指纹>'
$robotIp = '<机器人屏幕上的 HPU wlan0 IPv4>'
python .\g1-wireless\reverse_check.py --ip $robotIp
python .\g1-wireless\reverse_check.py --ip $robotIp --read-height
python .\g1-wireless\console.py --ip $robotIp
```

调试台地址为 http://127.0.0.1:8766/。只看本机页面可用 `console.py --page-only`。SDK 初始化会占用共享内存，避免同时反复启动多个客户端。

## 真机测试

操作前与队友协调单一控制者，清空对应动作范围，安排现场看护并确认实体急停可用。软件停止回执不能代替现场停稳确认。

- `reverse_check.py --ip ... --reverse-5cm`：按里程计测量直线后退，目标速度 0.01 m/s。到达目标、反馈超时、偏离路径或达到程序时间上限时尝试停止。曾在现场完成一次真机后退，未保留可核对的数值日志。
- `reverse_check.py --ip ... --read-height`：只读当前机身高度，已在真机验证。
- `reverse_check.py --ip ... --raise-5cm`：重新测量当前高度，目标加 5 cm，请求最短 6 秒，检查 SDK 目标范围。已用模拟 SDK 验证，未执行真机升降。

SSH 断开、进程崩溃、SDK 卡住时，不能保证软件停止调用完成。现场以实体急停为准。代码默认进入只读预检，只有指定动作选项并在终端输入对应确认文字才会尝试运动。关节点动和网页真机控制尚未接入。

[Galbot G1 SDK 1.10.0 文档](https://developer.galbot.com/docs/SDK/1.10.0/g1/zh/api_python_reference/)
