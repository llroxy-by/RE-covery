# RE-covery

Galbot G1 机器人（SDK 1.10.0）的只读取图工具链：枚举全部相机、按需抓帧、生成对比页，全过程不使能、不发运动指令、不在机器人上落盘。

## 关键结论

- 头部左右目与四路环视：`jpeg 1280×960`，可正常取图。
- 手臂：**深度**（Intel RealSense）由 SDK 直读，`16uc1 1280×720` 正常；**RGB** 依赖厂商独立驱动进程 `left/right_arm_camera_capture`，驱动未运行时 `init()` 仍返回 `True`，但取帧恒为空。
- `SensorType` 枚举数多于实装数（如 `ARM_DOWN_CAMERA` 在本机未实装，`init()` 直接拒绝）。

详细原理与排障过程见 [docs/arm-camera-implementation.md](docs/arm-camera-implementation.md)。

## 目录

```
docs/
  arm-camera-implementation.md   实现与排障记录（环境、根因、架构、踩坑、复现）
scripts/
  g1-check.ps1       只读预检：子网 / TCP 22 / SSH 主机密钥指纹，不登录
  robot-cam.ps1      主工具：-List / -Split / -Grab / -Diag / -Selftest
  cam-hwprobe.ps1    只读硬件探针：进程表 / V4L / 配置 / SDK docstring
  depth-preview.py   16bit 大端灰度 PNG → 8bit 预览 + 有效像素统计
  gallery.py         cams/<时间戳>/ → 单文件 HTML 对比页
```

## 快速开始

```powershell
# 0) 预检（不登录，不写 known_hosts）
.\scripts\g1-check.ps1 -RobotIp <robot-ip>

# 1) 枚举相机（不 init，零资源占用）
.\scripts\robot-cam.ps1 -List

# 2) 全量抓取（推荐：一相机一独立进程）
.\scripts\robot-cam.ps1 -Split

# 3) 只抓指定通道
.\scripts\robot-cam.ps1 -Split -Only LEFT_ARM_CAMERA,RIGHT_ARM_CAMERA

# 4) 生成对比页与深度预览
python3 scripts/gallery.py cams/<时间戳>
python3 scripts/depth-preview.py cams/<时间戳>/LEFT_ARM_DEPTH_CAMERA.png
```

产物：`cams/<时间戳>/`，每路一张图 + `index.json`（格式、分辨率、字节数、错误列表）+ `gallery.html`。

> `-OutDir` 不指定时默认输出到**脚本同级**的 `cams/`（即 `scripts/cams/`）。想统一放在仓库根目录，加 `-OutDir .\cams`。

## 环境要求

| 侧 | 要求 |
|---|---|
| 控制端 | Windows + PowerShell 5.1，`ssh.exe`（OpenSSH for Windows） |
| 机器人 | Galbot G1，SDK 1.10.0，`PYTHONPATH=/data/galbot/lib`，Python 3.8 |
| 网络 | 控制端与机器人在同一 `/23` 网段 |

机器人侧只用标准库（`zlib`/`struct`/`array`），**无需安装任何第三方包**；图像经 ssh stdout 以 base64 回传，机器人不落盘。

## 使用边界

本项目**只读取图**：不使能、不发运动指令、不写机器人文件、不修改系统配置、不重启服务。

- 取图前确认无人正在使用同一路相机（`ps -eo pid,etime,args | grep -iE 'camera|percept|galbot'`）。
- 拉动任何机器人侧驱动或服务前，先取得现场负责人许可；`Perception_Algorithm_Service` 会争用同一台 RealSense。
- 收尾用精确进程名关闭，禁止 `pkill -f camera` 这类宽匹配。

## License

内部使用，未另行声明。
