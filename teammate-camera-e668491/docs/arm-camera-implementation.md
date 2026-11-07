# Galbot G1 手臂相机取图功能：实现与排障记录

> 记录时间：2026-11-07　|　目标机：Galbot G1（`galbot@192.168.167.46`）　|　SDK：1.10.0
> 结论摘要：**手臂深度相机可直读取图；手臂 RGB 相机依赖独立驱动进程 `left/right_arm_camera_capture`，驱动未运行时 `init()` 仍返回 `True`、取帧恒为空。**

---

## 1. 背景与目标

需要在比赛现场从 G1 机器人上**只读取图**，不使能、不发运动指令、不在机器人上留下任何文件，用于后续视觉方案验证。

约束条件（现场协作红线）：

- 单人指定控制者发运动/使能指令，本功能**完全不涉及运动**；
- 不各自启动 `robot-console/server.py`；
- 不向机器人写文件、不改系统、不清理 `/dev/shm`；
- 取相机前需确认无人正在使用同一路相机。

---

## 2. 环境事实

| 项目 | 值 |
|---|---|
| 机器人地址 | `192.168.167.46`，用户 `galbot`，现场 Wi-Fi `/23` 网段 |
| SSH 主机密钥 | ED25519 `SHA256:1MH5iIhYyb2gK5Q02SOYqO0Ynil5Xqgh14oqNz/5s6k` |
| 系统 | Linux 5.10.216-tegra（Jetson Orin），Ubuntu 20.04 |
| 机器人 Python | 3.8.10 |
| SDK | `galbot_sdk` 1.10.0，未安装，仅以目录形式提供：`PYTHONPATH=/data/galbot/lib` |
| 机型自述 | `sn=R001ABDD22AA0180, robotSeries=G1, hardwareVersion=2.2, bodyType=A, ArmType=A22` |
| 视频节点 | `/dev/video0`–`/dev/video17`（18 个 V4L 节点） |

`v4l2-ctl --list-devices` 显示的实际硬件：

| 设备 | 归属 |
|---|---|
| `sg3s11afxk 32-001b` / `33-001b`（GMSL，经 `gmsl_service_daemon` 发布） | 头部左 / 右目 RGB |
| `Intel(R) RealSense(TM) Depth Camera`（usb `xhci-2.2`） | 左臂（RGB + 深度） |
| `Intel(R) RealSense(TM) Depth Camera`（usb `xhci-3.1`） | 右臂（RGB + 深度） |
| `USB Camera`（usb `xhci-2.3`） | 环视 |

**关键推论**：手臂是 RealSense，深度与 RGB 同属一个物理模组，但取图通路不同——深度由 SDK 直读设备，RGB 由厂商驱动进程发布。这是"深度成功、RGB 空帧"的机理来源。

---

## 3. SDK 接口与相机清单

```python
robot.init(enable_sensor_set: Set[SensorType], enable_sync_mode=False) -> bool
robot.get_rgb_data(camera_id, format=RgbOutputFormat.JPEG, once=True) -> dict
robot.get_depth_data(camera_id) -> dict      # 仅 *ARM_DEPTH_CAMERA
robot.get_ir_data(camera_id)    -> dict      # 仅 *ARM_INFRA_CAMERA_*，需 ir_enabled
```

返回 dict 含 `header / format / width / height / data`（深度另含 `depth_scale`）。`get_rgb_data` 默认已输出 JPEG 压缩字节，两端都不需要解码器。

`SensorType` 全量枚举（`sorted(S.__members__)`）：

```
BACK_IMU, BACK_LIDAR, BASE_LIDAR, BASE_ULTRASONIC, CHASSIS_IMU, CHASSIS_LIDAR,
HEAD_IMU, HEAD_LEFT_CAMERA, HEAD_LIDAR, HEAD_RIGHT_CAMERA,
LEFT_ARM_CAMERA, LEFT_ARM_DEPTH_CAMERA, LEFT_ARM_DOWN_CAMERA,
LEFT_ARM_INFRA_CAMERA_1, LEFT_ARM_INFRA_CAMERA_2,
LEFT_FRONT_SURROUND_CAMERA, LEFT_REAR_SURROUND_CAMERA, LIDAR_IMU,
RIGHT_ARM_CAMERA, RIGHT_ARM_DEPTH_CAMERA, RIGHT_ARM_DOWN_CAMERA,
RIGHT_ARM_INFRA_CAMERA_1, RIGHT_ARM_INFRA_CAMERA_2, ...
```

> **枚举 ≠ 实装**。手臂在枚举里有三个名字（`ARM_CAMERA` / `ARM_DOWN_CAMERA` / `ARM_DEPTH_CAMERA`），但物理上只有两颗镜头；`ARM_DOWN_CAMERA` 的 `init()` 直接被拒，正说明它对应未实装的一路。

### 实测结果（2026-11-07 09:57 全量抓取）

| 通道 | 结果 |
|---|---|
| `HEAD_LEFT_CAMERA` / `HEAD_RIGHT_CAMERA` | ✅ jpeg 1280×960 |
| `LEFT_FRONT/RIGHT_FRONT/LEFT_REAR/RIGHT_REAR_SURROUND_CAMERA` | ✅ jpeg 1280×960 |
| `LEFT_ARM_DEPTH_CAMERA` / `RIGHT_ARM_DEPTH_CAMERA` | ✅ 16uc1 1280×720（有效像素 65.7% / 41.9%） |
| `LEFT_ARM_CAMERA` / `RIGHT_ARM_CAMERA` | ❌ `no frame in any format variant (9 tries)` |
| `LEFT/RIGHT_ARM_DOWN_CAMERA` | ❌ `init refused in a fresh process with a single sensor` |
| `LEFT/RIGHT_ARM_INFRA_CAMERA_1/2` | ❌ `no frame in any format variant (3 tries)` |

---

## 4. 排障过程与根因

### 4.1 现象

```
FAIL LEFT_ARM_CAMERA  empty frame after 7 tries (not fitted / stream disabled)
FAIL RIGHT_ARM_CAMERA empty frame after 7 tries (not fitted / stream disabled)
remote summary: ok=0 fail=2
```

`ssh_exit=0`、`init -> True`、会话正常，唯独取帧返回空 dict。

### 4.2 三轮排除

1. **排除"同进程多相机冲突"**：改为「一相机一独立 Python 进程」（`-Split`），仍然空帧。
2. **排除"格式协商失败"**：单进程内按 `jpeg/once → jpeg/wait(阻塞) → rgb8 → bgr8 → 默认` 顺序重试，共 9 次全空。**阻塞式 `once=False` 也取不到，说明该路流没有任何发布者**，不是启动慢。
3. **只读取证**（`cam-hwprobe.ps1`）：查进程表、V4L 设备、配置、SDK docstring，得到决定性证据。

### 4.3 根因

```
MARK_PS_ 1616  /data/galbot/tools/camera/gmsl_service_daemon --pub        # 只有头部相机服务在跑
```

- `left_arm_camera_capture` / `right_arm_camera_capture` **不在进程表**；
- 机器人自带示例文件明写：*"please confirm that the robot's left arm camera driver `/data/galbot/bin/left_arm_camera_capture` …"*；
- 两路手臂是 RealSense，深度由 SDK 直读设备，**不依赖该驱动**。

因此：**驱动未运行 → 手臂 RGB 无发布者 → 空帧；深度仍可正常取图。** 曾经一次"恢复出图"是驱动恰好在跑的瞬时窗口，不是问题被修复。

> **重要认知**：`init() -> True` 只表示会话获批，**不代表相机流可用**。流被占用/未发布时 `get_rgb_data` 静默返回空 dict，不抛异常。因此 `empty frame` 的判读顺序应为：①其他进程占用 → ②驱动未运行 → ③配置禁用 → ④物理未实装 → ⑤`ir_enabled=false`。

### 4.4 恢复方法（机器人侧变更，需授权）

```bash
# 先看厂商脚本里的真实调用方式（只读）
cat /data/galbot/tools/camera/start_camera_services.sh

# 按该脚本的参数启动单路驱动，后台运行并留日志
/data/galbot/bin/left_arm_camera_capture --config /data/galbot/config/default/left_arm_camera_capture \
  > /tmp/lac.log 2>&1 &

# 正向验证
pgrep -af arm_camera_capture
tail -n 30 /tmp/lac.log

# 用完精确关闭（禁止 pkill -f camera 这类宽匹配，会连带杀掉头部相机与感知服务）
pkill -f left_arm_camera_capture
```

配置目录：`/data/galbot/config/{factory,default,data_collection}/{left,right}_arm_camera_capture`。

启动该驱动会与 `Perception_Algorithm_Service` 争用同一台 RealSense，**必须事先取得队长许可**。

---

## 5. 实现方案

单文件工具 `robot-cam.ps1`（Windows PowerShell 5.1），四种模式：

| 模式 | 作用 |
|---|---|
| `-List` | 只枚举 `SensorType`，不 `init`，零资源占用 |
| `-Split` | **推荐**。一相机一独立 Python 进程，16 路互不影响 |
| `-Grab` | 一次 `init` 全部相机（本机多传感器 `init` 常被拒） |
| `-Diag` | 隔离 `init()` 返回 `False` 的原因 |
| `-Selftest` | 本地验证 PNG 编码器，不连机器人 |

### 5.1 架构要点

1. **远端程序通过 Base64 传输**：脚本文本 → UTF8 → Base64 → 单 token 命令 `echo <b64> | base64 -d | bash`。原因是 PowerShell 5.1 向原生程序传参时会破坏多行参数（详见第 6 节）。
2. **机器人侧不落盘**：图像在远端编码后经 ssh stdout 以 base64 回传，只在本机写文件。
3. **不依赖第三方库**：远端仅用标准库 `zlib + struct + array` 手写 PNG 编码器（深度 16bit 灰度、红外 mono8、RGB 直通 JPEG）。机器人上的 Python 不保证有 numpy/cv2/PIL，也不应安装。
4. **正向取证**：所有输出统一 `MARK_` 前缀，结尾必须有 `MARK_DONE`；缺失即判 `UNKNOWN`，**不使用"缺失即正常"的否定式判据**。
5. **单进程隔离**：`GalbotRobot` 是单例，只有进程内第一次 `init()` 生效，因此每路相机用独立子进程，失败不互相牵连。
6. **资源释放**：`finally` 中固定执行 `request_shutdown → wait_for_shutdown → destroy`，避免自己成为下一个占用者。

### 5.2 本地辅助工具

| 脚本 | 用途 |
|---|---|
| `depth-preview.py` | 纯标准库解码 16bit 大端灰度 PNG → 8bit 预览 + 有效像素统计（2%–90% 百分位 + gamma 0.65，否则远场噪声会把画面压成全黑） |
| `gallery.py` | 将 `cams/<时间戳>/` 渲染成单文件 HTML 对比页（图片 base64 内嵌，可直接分发） |
| `cam-hwprobe.ps1` | 只读硬件探针：进程表 / V4L / 配置 / SDK docstring，不 `init`、不写盘 |

---

## 6. 踩过的坑（可复用结论）

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | `bash: -c: line 21: syntax error near unexpected token` | 单引号 here-string 是**字面量**，`''` 不会折叠成 `'`，远端收到 `'''<code>'''` | 改用 heredoc，或整体 Base64 |
| 2 | 多行远端脚本到 bash 后空格与换行全被吞 | PowerShell 5.1 传多行参数给原生程序时会剥除空白（实测 1884 字符 → 1596 字符） | 传输层只用无空白单 token：Base64 |
| 3 | `NativeCommandError` 中断脚本 | `$ErrorActionPreference='Stop'` 下原生程序写 stderr 即抛终止性错误，`2>$null` 拦不住 | 调用前后临时降为 `Continue`，`finally` 还原 |
| 4 | Base64 回传数据损坏 | `Out-String` 默认按控制台宽度折行 | 一律 `Out-String -Width 4096`，且逐行读取，不整体字符串化 |
| 5 | `ssh.exe : unknown option --` | PowerShell 把整条字符串当作单个 argv | `& ssh.exe @argList` 数组传参，远端命令作为最后一个独立元素 |
| 6 | 密码提示不出现 | 同时重定向 stdin 与 stderr 会吞掉提示 | 只捕获 stdout，stdin/stderr 留在控制台 |
| 7 | 误判"相机没装" | `init() -> True` 与"流可用"是两件事 | 先查进程占用与驱动，再谈硬件 |
| 8 | 差点杀掉整机视觉 | 宽匹配 `pkill -f camera` | 只按具体进程名精确结束 |

---

## 7. 复现步骤

```powershell
# 0) 预检：子网 / TCP 22 / 主机密钥指纹（只读，不登录）
.\g1-check.ps1 -RobotIp 192.168.167.46

# 1) 枚举相机（不 init）
.\robot-cam.ps1 -List

# 2) 全量抓取（推荐 -Split）
.\robot-cam.ps1 -Split

# 3) 只抓指定通道
.\robot-cam.ps1 -Split -Only LEFT_ARM_CAMERA,RIGHT_ARM_CAMERA

# 4) 生成对比页 / 深度预览
python3 gallery.py cams\<时间戳>
python3 depth-preview.py cams\<时间戳>\LEFT_ARM_DEPTH_CAMERA.png
```

产物：`cams/<时间戳>/` 下每路一图 + `index.json`（记录格式、分辨率、字节数、错误列表）+ `gallery.html`。

---

## 8. 已知限制

- 手臂 RGB 需厂商驱动进程在跑，SDK 层无法自行拉起；
- 手臂红外四路需要 `ir_enabled`，当前未启用；
- `ARM_DOWN_CAMERA` 在本机未实装，`init()` 直接拒绝属预期行为；
- 深度图 `depth_scale` 未在文档中给出单位确认，远场值（>40 m 量级）应视为无效噪声；
- 本方案为**只读取图**，不含运动控制、标定与手眼变换。
