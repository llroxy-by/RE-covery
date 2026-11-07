# G1 网页控制台 · Joint Lab

在自己的电脑运行，通过 SSH 调用机器人已有 SDK；浏览器打开 **http://127.0.0.1:8765/**。不会上传机器人文件、安装 SDK、修改机器人配置或启动时自动运动。

## 取得代码

```sh
git clone --branch feat/web-console-install https://github.com/llroxy-by/RE-covery.git
cd RE-covery/robot-console
```

已有仓库时，先保留自己的修改，再取得该分支；不要用 reset 或强制拉取覆盖工作区。

## 准备环境

| 位置 | 必需项 |
| --- | --- |
| 开发电脑 | Python 3.9+、OpenSSH 客户端、现代浏览器；下载源码时需要 Git |
| 机器人 | 已安装且正常工作的 G1 SDK 1.10.0，本项目已验证的库目录 `/data/galbot/lib` |
| 网络 | 电脑可访问机器人 SSH 22 端口；有线或无线均可 |

电脑端没有 pip 运行依赖，不需要 Docker、Ubuntu 重装或 Node.js 构建。`requirements.txt` 仅说明环境要求。Node.js 仅用于离线测试。

当前实测设备为 galbot-echo / G1 2.2 A22。不要据此假定其他机型、SDK版本、夹爪或标定也兼容。密码向现场负责人取得，只在 SSH 提示中输入，不放进代码、文档或 Git。

### macOS：直接启动

```sh
ROBOT_HOST=192.168.167.46 sh start.sh
```

IP 是示例，以机器人当前 HPU 网络信息为准。macOS 也可执行 `sh start.sh`，通过有线网口 IPv6 邻居发现已固定指纹的这台机器人。脚本不会修改电脑或机器人网络设置。

### Linux / WSL：指定 IP 启动

先确保 `python3 --version`、`ssh -V` 可用，再运行：

```sh
ROBOT_HOST=192.168.167.46 sh start.sh
```

Linux 不提供自动网口发现，必须指定 IP。没有自动打开浏览器时，手动打开 http://127.0.0.1:8765/。WSL 的网页在 Windows 浏览器打开；若localhost转发不可用，先排查WSL网络，不要将服务改为公网监听。

Windows 原生 PowerShell 不支持此包的 Unix SSH 控制套接字启动方式，请使用 WSL；不要直接粘贴 `sh` 命令到 PowerShell。Linux/WSL兼容路径已在代码中实现，尚未在现场跨平台实测；macOS是本轮实际运行环境。

### 首次 SSH 与指纹

启动脚本会核对 SSH ED25519 指纹。当前固定指纹对应这台现场机器人：

```text
SHA256:1MH5iIhYyb2gK5Q02SOYqO0Ynil5Xqgh14oqNz/5s6k
```

更换机器人必须向现场人员核对其指纹，然后设置 `ROBOT_FINGERPRINT`；不要为了连接而关闭主机密钥校验。首次SSH出现信任提示时，核对一致后再接受。IP相同不等于机器相同。

```sh
ROBOT_HOST=<当前IP> ROBOT_FINGERPRINT='SHA256:<现场核对的指纹>' sh start.sh
# 只做发现/指纹检查，不建立主连接、不启动SDK
ROBOT_HOST=<当前IP> DRY=1 sh start.sh
```

## 启动后怎么用

1. 等待顶部显示“实时反馈正常”。进入右臂或左臂分组。
2. 默认“部位图”用于辨认肩、肘、腕，点圆点或名称选择关节。它不代表实时姿态。
3. 切到“实际姿态”可看校准骨架：实线为反馈，虚线为单关节目标预览。
4. 调整目标角度与速度。拖动和输入只预览；确认现场条件后勾选，再点击“执行目标角度”。与当前角度相同时按钮禁用。
5. 低频点动在“需要一点点试？展开点动”中。窄屏控制栏固定在底部，宽屏固定在图旁。
6. 软件停止使用“停止”按钮或 ESC。实体急停须始终可用。

功能包括双臂/头/腿主动关节点动、模型限位、夹爪开合、姿态识别、动作预设、底盘操纵杆与停止、日志导出。夹爪和底盘使用各自确认控件；底盘关节位置是只读。动作预设和摆手不自动启动。

**这是单关节测试工具，没有碰撞规划。** 骨架预览省略外壳和碰撞体，不是安全路径验证。腿部影响重心；不要在未核对支撑和空间时试动。网页或网络失效时软件停止可能来不及执行；不可替代实体急停。多位开发者应线下协调，避免冲突指令。

## 配置与手动启动

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ROBOT_HOST` | 手动server默认192.168.167.46 | 当前机器人IP或可达主机名，不含用户名 |
| `ROBOT_USER` | galbot | SSH账号 |
| `ROBOT_SOCK` | /tmp/kitt-console-live.sock | 电脑本地SSH复用套接字 |
| `ROBOT_FINGERPRINT` | 上述现场设备指纹 | start.sh校验目标身份 |
| `PYTHON` | python3 | start.sh使用的本机Python命令 |

桥接使用机器人已安装的SDK库和校准URDF路径，不会部署或自动安装缺失组件。SDK与机器人环境不匹配时停止排查，先联系现场人员。

手动方式先保持一个终端连接，再在另一个终端启动网页：

```sh
ssh -M -S /tmp/kitt-console-live.sock -o ControlPersist=no -N galbot@192.168.167.46
# 第二个终端，源码robot-console目录内
ROBOT_HOST=192.168.167.46 python3 -B server.py
```

服务只监听127.0.0.1:8765。同伴应在自己的电脑独立安装，不把自己的127.0.0.1当作队长地址；不需要共享密码、队长浏览器或把控制台暴露到公网。

## 校准模型与换设备

`joint_visual_model.json` 是该现场设备的校准双臂链快照，`index.html` 内嵌同一模型。服务端执行限位从机器人当前URDF读取。换设备或标定后必须更新预览模型，不能继续用旧快照。

经授权，可只读导出机器人已存在的校准模型到**电脑临时目录**，再在电脑生成页面：

```sh
ssh galbot@<当前IP> 'cat /data/galbot/config/galbot_description/galbot_one_golf_description/galbot_one_golf_cali_user.urdf' > /tmp/g1-local-cali.urdf
python3 build_visual_model.py --urdf /tmp/g1-local-cali.urdf
python3 build_visual_model.py --check
```

这里仅在电脑写文件，不向机器人上传。路径不适用时先核对官方机型资料，不修改机器人标定。只改了 `joint_visual.js` 后，也运行 `python3 build_visual_model.py` 同步网页内嵌逻辑。

## 排障

| 现象 | 检查/处理 |
| --- | --- |
| 浏览器拒绝连接 | start.sh终端是否仍在运行，端口是否被占用；127.0.0.1指当前这台电脑 |
| 连接不可用 | SSH主连接是否存在、机器人IP是否变化、SDK初始化报错；看启动终端日志 |
| 按钮灰色 | 关节反馈是否过期、SDK可控清单是否就绪、现场确认是否勾选、目标是否改变、上一动作是否完成 |
| 显示反馈过期 | 系统保留锁定，不强行解除。检查源数据是否持续更新；本机客户端重连不等于机器人需重启 |
| 关节显示只读 | 桥接每2秒重查SDK主动关节清单，不按URDF猜测权限；持续只读时查SDK环境 |
| 重启页面报未授权 | 刷新网页获取该次服务器的会话令牌，不能复制旧令牌 |
| 指纹不一致 | 停止连接，与现场核对地址与指纹，不关闭SSH检查 |
| 初始化失败且共享内存满 | 只读保留 `/dev/shm` 占用和日志证据，联系现场；不要清空共享内存、重装SDK或改服务 |
| 底盘FAULT | 看底盘控制器反馈和其他操作者是否正在控制；不要通过解除安全机制强行运动 |

不要将密码、令牌、私钥、.env、现场图片或完整带凭据日志提交GitHub。

## 停止与退出

先在网页停止动作，现场确认停稳后，在启动终端按Ctrl+C退出本机控制台。需要关闭该电脑建立的SSH主连接时，另执行：

```sh
ssh -S /tmp/kitt-console-live.sock -O exit galbot@<当前IP>
```

不要关闭队友的连接或机器人系统服务。退出网页不等于机器人已经停稳。

## 离线验证与源码

所有测试使用假SDK，不需要连接机器人，也不会运动：

```sh
python3 -B test_guards.py
python3 -B test_joint_inventory.py
python3 -B test_server.py
python3 -B test_posture.py
python3 -B test_liveness.py
python3 -B test_gripper.py
python3 -B test_base_drive.py
python3 -B test_fault_recovery.py
node test_joint_visual.js
node test_presets.js
node test_joystick.js
python3 build_visual_model.py --check
```

`test_server.py`使用独立18765测试端口，不占真实控制台8765。Node.js只需运行测试时安装。

- `server.py`：本机HTTP、会话校验、看门狗、SSH桥接生命周期。
- `remote_bridge.py`、`posture.py`、`gripper.py`：经SSH在内存执行的SDK桥接、FK和夹爪适配。
- `index.html`、`joint_visual.js`、`joint_visual_model.json`、`presets.js`、`joystick.js`：网页与纯前端逻辑。
- `start.sh`：连接、身份校验和本机启动；`build_visual_model.py`：本地模型生成。

本轮已确认网页可读右臂7关节及SDK可控标记，未自动验证右臂关节运动或整个控制台的所有功能。官方SDK依据：[G1 1.10 Python API](https://developer.galbot.com/docs/SDK/1.10.0/g1/zh/api_python_reference)。
