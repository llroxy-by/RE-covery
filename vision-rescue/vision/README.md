# RE-covery 视觉模块（P0）

桌面固定机位、键盘触发的场景快照与 S0/S1 差异检测。**不控制机械臂，不承诺抓取成功，不把像素中心当抓取坐标。**

实现依据：`docs/TEAM_A_VISION.md`（A/C 统一数据协议 v1）。

---

## 1. 能做什么

| 能力 | 说明 |
|---|---|
| 实时取像 | `cv2.VideoCapture`，打开失败/读帧失败都直接抛错，**不会静默退回 mock** |
| 区域归属 | A/B/C/D/TEMP 五个多边形；落在边界 14 px 内、区域外、跨区都判 `unknown` 并给原因 |
| 颜色定位 | HSV 阈值 + 形态学 + 轮廓，输出 `object_id / center_px / zone / visibility / confidence / reasons` |
| 稳定性 | 连续 5 帧区域一致且中心抖动 ≤ 18 px 才认这个区域，否则 `zone=unknown + zone_unstable` |
| 冻结拦截 | 连续多帧像素完全一致 → 报错停机。虚拟摄像头放静图时 S1 会和 S0 一模一样，不拦就会误判 `unchanged` 放行 |
| 快照 | 按 `0` 存 S0、按 `1` 存 S1；原图 + 标注图 + JSON 一起落盘，校验不过就整份丢弃 |
| 差异 | 只输出 `unchanged / misplaced / new / uncertain`，**没有 confirmed missing** |

## 2. 安装

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r vision/requirements.txt

# Windows PowerShell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r vision/requirements.txt
```

装不上（网络慢）时换国内镜像：

```bash
python -m pip install -i https://mirrors.aliyun.com/pypi/simple -r vision/requirements.txt
```

## 3. 运行

```bash
# 真实摄像头（现场演示用这个）
python -m vision.main --camera 0 --config vision/config/desktop.json --mode real

# 摄像头编号试 0 / 1 / 2；被会议软件占用会打不开
# 没有摄像头时用合成画面跑通链路（画面左上角有 MOCK 水印，JSON 里 mode=mock）
python -m vision.main --mode mock --mock-scenario ok

# 手机当摄像头：直接读 App 的 MJPEG 流，不用装 Windows 驱动/虚拟摄像头
python -m vision.main --url http://192.168.1.7:8080/video --mode real

# 不知道哪个编号/地址能用：
python -m vision.tools.probe_cameras --max-index 4 --url http://192.168.1.7:8080/video

# 无窗口自动抓取一组 S0/S1（回归、留证据用；mock 下会自动换场景）
python -m vision.main --mode mock --mock-cycle --no-preview --auto 0,1
```

按键：

| 键 | 动作 |
|---|---|
| `0` | 用当前画面建立 S0 基准（不合格会**拒绝**，并把证据写进 `records/rejected/`） |
| `1` | 用当前画面建立 S1，与 S0 比较，打印逐项差异并保存 `diff_*.json` |
| `m` | 切换 mock 场景（`ok → moved → swapped → occluded → extra → dim`），真实相机下无效 |
| `q` | 退出（释放摄像头） |

其它参数：`--output` 改输出目录，`--min-conf 0.8` 改下结论所需置信度，`--max-frames N` 跑 N 帧后退出，`--skip-image-check` 仅用于纯逻辑测试，`--allow-frozen` 关闭冻结画面守卫（只在调试虚拟设备时用）。

## 3.1 本机验证状态（别把没验的当验了）

| 项目 | 状态 | 依据 |
|---|---|---|
| 离线回归 41 项 | ✅ 通过 | `python -m unittest vision.tests.test_vision` |
| mock 端到端 S0→S1→差异 | ✅ 通过 | `--mode mock --mock-cycle --no-preview --auto 0,1`，红块 A→B 判 `misplaced` |
| 遮挡 / 未知物体 / 光线骤暗 | ✅ 判 `uncertain`，不判 missing | 见 `records/diff_*.json` |
| 真实摄像头取像 | ✅ 已通（`--camera 1`，可原生 1280×720） | `python -m vision.tools.probe_cameras`：0 号是虚拟设备（静止）、1 号实时、2 号全黑 |
| `--url` 读手机 MJPEG 流 | ✅ 已通（本地假流验证） | 960×540 自动缩放为 1280×720；640×480 明确拒绝；坏地址给出手机类报错 |
| 机位能否拍到桌面 | ❌ 不行（内置摄像头朝人） | 内置摄像头对着操作者，拍不到桌面 → 需外接 USB 摄像头或手机推流俯拍 |
| 五个区域的真实标定 | ⏳ 待在你自己机器上做 | `config/desktop.json` 的区域顶点目前是按 1280×720 假定的，必须对准你的桌面重标 |

也就是说：**真实摄像头这一段必须你在自己电脑上跑一遍**（`--mode real`，看画面是否在动、HUD 时间戳是否在走），然后把结果填进 `records/TEST_LOG_TEMPLATE.md`。

## 3.2 用手机当摄像头（三种接法）

| 接法 | 电脑侧要装什么 | 用法 | 备注 |
|---|---|---|---|
| **IP 摄像头（推荐）** | 不用装 | 手机装「IP 摄像头 / IP Webcam」，与电脑同一 WiFi，记下 App 显示的地址 → `--url http://手机IP:8080/video` | 最省事；MJPEG 流，`--url` 已支持并实测可读 |
| DroidCam / Iriun | 客户端（会注册虚拟摄像头） | `python -m vision.tools.probe_cameras` 找到新出现的编号 → `--camera N` | 有免费版水印/分辨率限制 |
| Windows「手机连接」 | 系统自带 | 按系统引导把手机摄像头共享给 PC | 部分安卓机型/版本支持 |

手机摆放三条硬要求：**横屏、固定住（支架/书堆，别手持）、关掉自动对焦来回拉风箱**。

分辨率规则：流是 16:9（720p/1080p/540p 都行）会自动缩放到配置的 1280×720，分区坐标仍有效；流是 4:3（如 640×480）会**明确报错**要求你改 config 并重标——因为硬缩会把分区坐标整体扯歪。

## 3.3 网页观察台（浏览器里看实时画面）

```powershell
python -m vision.web --camera 1                  # 打开 http://127.0.0.1:8000
python -m vision.web --mode mock --port 8123      # 没相机时先看效果
python -m vision.web --host 0.0.0.0               # 让同一 WiFi 的手机也能看
```

页面左侧是实时画面（带区域框、ID、zone、置信度）和 HSV 掩膜条，右侧是状态、目标表、S0/S1 差异表；
四个按钮直接触发 `0`（建基准）/`1`（比较）/`m`（切 mock 场景）/`d`（调参建议），等价于命令行按键。

只用标准库 `http.server`：画面走 MJPEG（`/stream`、`/masks`），状态走 `/state` JSON，动作走 `/action?k=0`。
不给机器人/外部网络开端口时用默认 `127.0.0.1` 即可；要手机看再显式加 `--host 0.0.0.0`。

## 3.4 只用 Mac 本机摄像头（不用手机）

```bash
../.venv-vision/bin/python -m vision.tools.mac_live                    # 打开 http://127.0.0.1:8300
../.venv-vision/bin/python -m vision.tools.mac_live --config vision/config/mac.json
```

也可以双击 `launch-mac.command` —— **不用再输 token**：它从
`vision/config/.env.local` 读 `VISION_API_KEY`（已配好，权限 600，`api_key` 里写的还是
`env:VISION_API_KEY`，key 本身没进配置、没进代码）。换 key 只改那一行。
只有那个文件不存在时才会提示手输，直接回车就退化成纯模板库。

| 点 | 说明 |
|---|---|
| 画面从哪来 | Mac 浏览器的 `getUserMedia`，本机摄像头，**不经过手机** |
| 为什么不用 HTTPS | `http://127.0.0.1` 和 `localhost` 本身就属于浏览器认定的安全上下文，自签证书那套全省了，也不用在手机上点"继续前往" |
| 摄像头选择 | 页面右上角下拉。默认挑内建（FaceTime / Built-in），带"iPhone / 连续互通"的排在后面并标注 —— 否则很容易又把手机当成本机镜头 |
| 检测链路 | 与 `phone_live.py` 完全同一套（`LiveSession` + 所有 `/frame`、`/anchor`、`/compare`、`/label` 接口），结果一致 |
| 监听范围 | 只监听 `127.0.0.1`，局域网里别的设备打不开 |

**分区不用重标**：画面里已经画好黄色的 A / B / C / D / TEMP 框（`annotate()` 画的），
把积木放进框里，`zone` 就有值，基准不会被 `zone_unknown` 拒。只有桌子布局跟默认框
完全对不上（比如机位斜放、只摆两列）才需要「存一张画面」+ `calibrate --image` 重圈，
标完重启服务。

**摄像头权限**：命令行跑 Python 拿不到 macOS 摄像头权限（会报
`not authorized to capture video`），所以画面统一走浏览器 —— 浏览器有自己那条
权限，点一次「允许」就行。分包标定因此提供「存一张画面」按钮：服务端把当前帧
原样存成 `vision/records/mac-*.jpg`，再用 `calibrate --image <那张图>` 点分区，
坐标系和实时帧完全一致，不用给终端开摄像头权限。

`vision/config/mac.json` 与 `desktop.json` 只差标签门槛：桌面机位 50–70cm 时，
同一张标签在画面里只有手机的一半大，所以 `min_area_px` 3000→1500、
`min_side_px` 40→26。**分区坐标两边都得现场重标**，不能直接套用。

## 4. 现场标定（这是唯一必须动手的一步）

**推荐：用鼠标点，不要手填坐标**

```powershell
python -m vision.tools.calibrate --camera 1
# 或手机流： python -m vision.tools.calibrate --url http://192.168.137.15:8080/video
# Mac 上免摄像头权限：拿实时页面「存一张画面」存下的图
python -m vision.tools.calibrate --image vision/records/mac-20260101-120000.jpg --rect --zones A,B,C,TEMP
```

左键依次点当前区域的四个角（顺序随意、别交叉），右键撤销，`n` 下一个区域，`p` 回上一个，`r` 清空重标，`s` 保存，`q` 放弃。

**区域本来就是矩形**，机位正对桌面时可以用两点模式省一半点击：

```powershell
python -m vision.tools.calibrate --camera 1 --rect --zones A,B,C,TEMP
```

点矩形的**两个对角**即可，程序自动补成轴对齐矩形。默认四点模式是给斜拍/俯拍准备的——透视会把桌面上的矩形压成梯形，轴对齐矩形对不上，那种机位必须点四个角。区域也不限于矩形：任意不重叠、不越界的凸四边形都行。
保存时会自动检查：面积太小、顶点重复、超出画面、区域重叠都会**拒绝保存**并说明原因；
通过后会把 `zones`、`resolution`、`calibration_id` 一起写进 config —— **旧基准随之作废，回主程序重新按 `0`**。

手动改配置的话，按下面来：

1. 相机固定（三脚架/书堆都行），**不要手持**；关掉自动曝光来回跳的光源，避免台面反光。
2. 先跑一次 `--mode real`，对着画面把 `vision/config/desktop.json` 里的 `zones` 改成你桌面上五个区域的四边形顶点（像素，`[x, y]` 从左上角起算，x 向右 y 向下）。
3. 五个区域不能重叠；启动时会自检，重叠会打印 `zones_overlap:A&B` 并把标定标为无效。
4. 颜色不合适就调 `colors.<color>.ranges` 的 HSV 上下界。红色在色相环上跨 0°，所以是两段（`0–10` 和 `170–180`），少了任何一段都会漏检。
5. 改完区域/分辨率/颜色，**旧基准全部作废**，必须重新按 `0`。

调参建议：把积木放到区域里，看 HUD 上的 `confidence` 和 `reasons`。出现 `zone_boundary_ambiguous` 就把积木往区域中间放，或把区域边界往外挪；出现 `too_dark` 就补光；出现 `multi_candidate` 说明画面里有同色的第二块东西。

## 4.1 用预览调参（照症状改参数）

```powershell
python -m vision.main --camera 1 --debug-mask
```

会多开一个 **masks 窗口**：`red / green / blue / unknown` 四块并排，**白色 = 该像素被 HSV 阈值选中**。调参只看两件事：掩膜里有没有白块、白块干不干净。按 `d` 会打印当前画面下的具体改法。

| 预览上看到的 | 说明 | 改哪里 |
|---|---|---|
| 掩膜全黑，物体明明在画面里 | 阈值没选上 | 降饱和度下限（如 `110→50`）、亮度下限（`70→40`），或扩大色相范围 |
| 掩膜有白块，但没有检测框 | `min_area_px` 太大，或物体太小/太远 | `min_area_px` 调小（`700→350`） |
| 掩膜白块干净，但框挂着 `multi_candidate` | 画面里有同色第二块 | 拿走同色物，或提高饱和度下限收窄颜色 |
| 掩膜边缘一圈毛刺/噪点 | 光照不均或压缩噪声 | 提高饱和度下限；别对着白墙/纸面这类低饱和背景 |
| 标签是 `unknown`，原因是 `zone_boundary_ambiguous` | 离区域边界不到 `margin_px` | 物体往中间挪，或 `zone_margin_px` `14→8` |
| 标签是 `unknown`，原因是 `zone_unstable` | 连续 5 帧区域在跳 | 手别晃、相机别动；确实稳了还报就 `max_center_jitter_px` `18→30` 或 `stability_frames` `5→3` |
| 标签是 `unknown`，原因是 `outside_all_zones` | 落在五个区域外 | 挪进区域，或把区域框大 |
| `reasons` 有 `too_dark` / `overexposed` | 亮度异常 | 补光 / 避开反光，或调 V 上下限 |
| `reasons` 有 `possibly_occluded` | 轮廓填充率低于 0.55 | 移开遮挡物 |

调参纪律：**一次只改一个数**，改完立刻看掩膜和 `conf`；每改完一轮，若区域/分辨率变了，必须重新按 `0` 建基准（配置里的 `calibration_id` 要一起换，用标定工具保存时会自动换）。

## 5. 输出

```
vision/records/
  <uuid>.json                  # scene（schema v1）
  <uuid>_raw.jpg               # 原图，复核用
  <uuid>_annotated.jpg         # 标注图，演示/给 B 用
  diff_<s0>_<s1>_<rand>.json   # 差异
  rejected/                    # 被拒绝的 S0（证据保留，不当基准）
```

字段严格按 `vision/schemas/*.json`，多一个字段都过不了校验。图像引用是项目相对路径。

## 6. 测试

```bash
python -m unittest vision.tests.test_vision -v
```

覆盖：区域归属与边界、检测（三色/缺色/未知色/过暗/多候选）、稳定性、差异九条业务规则（含"未检出 ≠ missing""未知物体阻止验收""模式/标定不一致强制 uncertain""空场景不能真空验收"）、schema 校验、快照不可变。

真机八类测试的记录模板见 `vision/records/TEST_LOG_TEMPLATE.md`，**不要编造记录**。

## 7. 已知限制（写进 PR，不要回避）

1. **像素中心不是抓取坐标。** 像素→机械臂需要相机内外参、手眼/基座标定与安全验证，由队长负责。
2. 单目 2D 无深度：同色多块只认最大的一块，身份靠持久 ID（颜色）维持；同色无法可靠区分时报 `uncertain`，不交换 ID。
3. `confidence` 是启发式质量分（填充率、亮度、边界距离、多候选、抖动），**不是统计概率**。
4. 姿态（yaw/倾斜）不在 P0；`unchanged` 只说明区域层面一致，不保证能抓。
5. 机位一动、分辨率一变、区域配置一改，基准就失效——系统会通过 `issues` 说出来，而不是硬比。
6. mock 模式的任何结果**不得**用于机器人验收（JSON 里 `mode=mock`，差异校验会因此判 `uncertain`）。

## 8. 给 B / C / 队长

完整版见 **`vision/docs/INTERFACE.md`**（含字段枚举、三个联调样例、C 的硬规则、PR 模板）。

- 给 B：`records/*_annotated.jpg` + `records/*.json` + `diff_*.json`；枚举值见 schema；异常示例看 `records/rejected/`。
- 给 C：读 `diff_*.json` 的 `status`、`issues`、`differences`。只有 `status=unchanged` 且 `issues=[]` 且全部逐项 `unchanged` 才谈得上验收（`diff.gate_verdict()` 已封装成 `READY / REVIEW / NOT_READY`）。`uncertain` 的项不得生成确定抓取命令。
- 给队长：`vision/sources.py` 的 `VideoSource` 接口是唯一输入口。接 G1 腕部相机只需实现 `CameraAdapter(frame_provider, descriptor)`，检测/快照/差异/界面不动；换相机必须重标定。

## 9. 目录

```
vision/
  __init__.py
  main.py                 # 命令行、预览、按键
  sources.py              # VideoSource / OpenCvSource / MockSource / CameraAdapter
  detector.py             # HSV + 轮廓 + 质量打分 + StabilityTracker
  zones.py                # 多边形归属 + 区域重叠自检
  snapshot.py             # scene 组装、schema 校验、落盘
  diff.py                 # S0/S1 差异 + gate_verdict
  web.py                  # 浏览器观察台（MJPEG + 状态 + 按钮，仅标准库）
  docs/INTERFACE.md       # 给 B / C / 队长的接口说明与 PR 模板
  tools/
    probe_cameras.py      # 摄像头体检（哪个编号/地址是真实时，不保存画面）
    calibrate.py          # 鼠标点四角标定五区域，直接写 config
  config/desktop.json     # 分辨率、区域顶点、HSV、阈值
  schemas/*.json          # 协议 v1（Draft 2020-12）
  tests/                  # 离线回归测试 + 固定样例
  records/                # 运行产物与测试记录
```
