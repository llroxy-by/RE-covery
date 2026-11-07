# RE-covery · Galbot G1 开发工具

## 网页机器人控制台

完整源码在 [`robot-console/`](robot-console/)，安装、启动、操作、模型更新和排障步骤见：

**[网页控制台安装使用说明](robot-console/README.md)**

```sh
git clone --branch feat/web-console-install https://github.com/llroxy-by/RE-covery.git
cd RE-covery/robot-console
ROBOT_HOST=<机器人当前IP> sh start.sh
```

电脑需要 Python 3.9+ 和 OpenSSH。运行时无 pip 依赖；macOS、Linux及WSL路径见文档。机器人需已安装匹配的 G1 SDK 1.10.0，本仓库不部署机器人系统组件。

控制台只在电脑本机运行，通过 SSH 内存桥接读取与控制，打开页面不自动运动。真机操作前须现场确认并保留实体急停；可视化预览不包含碰撞规划。

其他开发目录（视觉、抓取、相机工具）有各自入口，不是启动网页控制台的依赖。项目操作约束见 [AGENTS.md](AGENTS.md)。
