# RE-covery 相机观察台

机器人通过 Galbot SDK 获取头部左相机 JPEG，经 WebSocket 发送到 PC 网页。PC 只需 Python 和浏览器，无需安装机器人 SDK。

## 已验证

2026-11-07，在 G1、SDK 1.10.0 环境收到连续 JPEG 帧，并在浏览器确认现场画面。未涉及运动控制。相机服务依赖机器人已有的 `galbot_sdk` 和 `websockets`；Python 3.8 环境已运行验证。

## 1. 机器人端

将 `server.py` 和 `ws_server.py` 放入已获授权的项目目录。在该目录启动：

```sh
env PYTHONPATH=/data/galbot/lib LD_LIBRARY_PATH=/data/galbot/lib python3 -B -u ws_server.py
```

上述库路径适用于本次实测设备，其他设备应先确认 SDK 路径。复用已有依赖，不覆盖系统 SDK。服务监听 `127.0.0.1:8781`；只初始化 `HEAD_LEFT_CAMERA`，读取 JPEG，不操作关节、底盘或夹爪。使用 Ctrl+C 结束，程序请求 SDK 关闭并释放资源。未设置开机自启。

## 2. PC 建立转发

另开终端，替换机器人地址后运行并保持终端开启：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:8781:127.0.0.1:8781 galbot@ROBOT_IP
```

首次连接核对主机指纹，使用自己的授权凭据。无需把视频端口暴露到公网。

## 3. PC 打开网页

在包含 `server.py` 和 `index.html` 的目录运行：

```sh
python3 server.py
```

浏览器打开 http://127.0.0.1:8780/ 。选择“WebSocket 实时图像”，地址为 `ws://127.0.0.1:8781`，点击“连接画面”。支持停止、全屏、连接断开后自动重连；超过四秒未收到帧会隐藏旧画面。

网页也保留连续 JPEG 和 MJPEG 接口输入选项。默认 PC 启动不会导入 SDK。`server.py --robot` 是可选 HTTP 采集模式，不是以上 WebSocket 流程的必要步骤。

## 故障排查

- 网页打不开：确认 PC 的 `python3 server.py` 仍在运行。
- 一直重连：确认 SSH 转发仍在运行，且本机 8781 端口监听成功。网页自动重连不能代替 SSH 隧道恢复。
- 等待相机数据：查看机器人端终端是否有初始化或 SDK 错误；不要重复启动多个采集进程。
- 地址已被占用：确认是否已有相机服务或隧道，不要直接终止未知进程。
- 关闭服务：先在网页点击停止，再在机器人采集终端 Ctrl+C，最后结束转发。SDK 关闭可能等待底层资源响应。

当前传输是逐帧 JPEG，带宽随画面尺寸和帧率变化；不是 WebRTC。页面接收时间不是相机曝光时间，不能据此计算端到端延迟。SDK 调用如果卡住，服务会停止产生新帧，需要检查机器人端错误。

## 文件

- `ws_server.py`：WebSocket 服务，复用单个后台采集线程，发送最新帧。
- `server.py`：SDK 采集与本地网页 HTTP 服务；SDK 仅在启用机器人采集时导入。
- `index.html`：浏览器显示页面。

## 文档依据

[Galbot G1 SDK 1.10.0 Python 示例](https://developer.galbot.com/docs/SDK/1.10.0/g1/zh/examples_python)，以及现场相机指南。仓库不包含登录密码、私钥或现场图像。
