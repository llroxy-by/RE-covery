# 机器人相机接视觉识别

新增 vision-rescue/robot_vision.py，复用 camera-viewer 的 JPEG WebSocket 与 live.server.Bench 的字母识别、模板和视觉模型。输出逐行 JSON，默认读取 300 帧后退出；断流超过 5 秒即报错退出。只保留最新待识别帧。分区未经真机标定，输出 zone=null。

交由现场技术人员在批准的项目目录解压。相机沿用仓库已有 G1 SDK 1.10.0 代码，本次未修改 SDK 调用。不要重复启动已有相机服务。SDK 路径与版本须由现场确认。

终端一：按 camera-viewer/README.md 启动已有 ws_server.py。

终端二：使用项目专用 Python 环境（Python 3.9 或以上，已有 opencv-python、numpy、websockets；缺少依赖时由现场人员在项目环境安装，不覆盖系统依赖）：

```sh
export VISION_API_KEY='你的模型密钥'
python -B vision-rescue/robot_vision.py --frames 300
```

模型沿用 vision/config/mac.json 的 label.vlm 配置，请确认 base_url 和 model 是你有权使用的服务。识别会将标签图像发送至该地址。密钥仅从环境变量读取。

机器人若只有 Python 3.8，可保留相机在机器人上运行，将 vision-rescue 放到电脑的 Python 3.9+ 环境，通过原相机说明中的端口转发运行同一命令。无需修改机器人系统 Python。

JSON 中 tracks 是积木字母结果，fresh 表示当前帧匹配；center_letter 是原模块缓存的中央识别提示，不应作为实时控制指令。此入口不控制机器人运动、不提供网页；原来的相机网页可继续查看画面。新相机视角下需要重新登记模板和调整阈值。空 tracks 表示未确认识别，不代表模型调用失败。

验证范围：本地启动与语法检查；未进行机器人摄像头或在线模型实测。GitHub 接口返回 404，使用的是本地 origin 指向 llroxy-by/RE-covery 的工作副本。
