# RE:BENCH · 积木字母实时视觉

摄像头对着桌面，实时读出积木上白标签的字母（A/B/C…），能**建立基准**、再**比较**有没有被挪动。
整套东西只依赖一个本机 Python 进程 + 浏览器，不用 GPU、不用模型权重，认不准时才叫一次视觉模型。

```
浏览器 <video> 30fps 本地放流  ──┐
                                ├─ 每帧 JPEG 上行 → Python 检测 → 0.9KB JSON 下行
canvas 叠加层 60fps 画框  ◄────┘
```

## 现在能做什么

| 能力 | 状态 |
|---|---|
| 实时读字母（模板匹配，本地 30-45ms/帧） | 可用 |
| 跨帧稳定（同一块连读两帧才显示） | 可用 |
| 建立基准 / 与基准比较（只看位移，阈值 90px） | 可用 |
| 现场教字母（认不准时登记一次，永久进库） | 可用 |
| 远距离兜底（标签只有 40×50px 时整帧直读） | 可用，慢（4-15s），只做提示 |
| 假目标过滤（眼睛、椅子、灯带、屏幕、条幅） | 可用，见「四道闸门」 |
| 机器人摄像头（远端推流） | 见「接机器人摄像头」 |

实测（1280×720，Mac 内置摄像头，同批样本）：

- 近景 A/B：**0.89 / 0.90**，框位准确，单帧 43ms（检测 30ms + 读字母 13ms）
- 缩到 40% 的远景：**0.90 / 0.87**，仍然读出
- 眼睛合成图：**0 个框**（模板分 0.50-0.54，被闸门挡住）
- 展会广角现场照（灯带/屏幕/条幅）：**0 个假框**
- 基准 → 画面平移 260px → 判定 **moved 263/264px**

## 快速开始

```bash
# 依赖（Python 3.9+）
python3 -m venv .venv-vision
.venv-vision/bin/pip install -r vision/requirements.txt

# 跑起来（浏览器自动打开 http://127.0.0.1:8400/）
cd vision-rescue && ./start.command
```

`start.command` 会读 `vision/config/.env.local` 里的 `VISION_API_KEY`（可选；没有也能跑，只是没有模型兜底）。
第一次浏览器会问摄像头权限，点「允许」。

不想用摄像头也能验证：`fail_cases/` 里有真实样本，

```bash
.venv-vision/bin/python -m unittest vision.tests.test_vision    # 离线单测
```

## 目录

```
vision-rescue/
├── live/                  ← 实时应用（本文件描述的这套）
│   ├── core.py            读数：找标签 → 四道闸门 → 模板比对 → 跨帧稳定 → 基准
│   ├── server.py          一个检测槽位 + JSON 接口，不做任何绘图
│   ├── page.py            页面本体：画在浏览器里，60fps 叠加层
│   └── scan.py            一次信号做透扫描的实验版（未接入页面，见 archive/）
├── vision/                ← 底层库：字母模板、标签定位、视觉模型、快照、区域
│   ├── letters.py         find_labels / normalize / character_mask / LetterBank
│   ├── vlm.py             视觉模型客户端（异步 warm/peek，单并发）
│   ├── config/mac.json    全部现场参数（门槛、模型、分区、阈值）
│   └── config/letters.npz 字母模板库
├── fail_cases/            真实失败/成功样本，调参用
├── archive/scan-v1/       扫描实验版（保留，不参与运行）
└── start.command          双击启动
```

## 一条帧的路

1. **浏览器**：`getUserMedia` 拿到本地流 → `<video>` 直接播（30fps，与检测解耦）→
   每帧把 `<canvas>` 裁成 16:9、缩到 1280 宽、JPEG 0.72 推给 `/detect`。
2. **服务端**：单槽位。上一帧没算完就**直接丢掉新帧**并回上一次的结论，永远不排队 ——
   这是旧版卡死的根因（排队让画面越卡越慢）。
3. **读数** `core.Reader.read()`：
   找白色四边形 → 归一化 → 二值化取笔画 → **四道闸门** → 模板比对（带 memo）→ 过闸的才算 hit。
4. **稳定** `Stabilizer`：IOU 关联跨帧，同一块**连读两帧**才给它字母，避免单帧读错闪一下。
5. **回传**：约 0.9KB JSON（框、字母、分数、拒绝原因、耗时），浏览器 canvas 画。

## 四道闸门（误报就是从这里进来的）

只看「像不像某个字母」永远分不开眼睛和 A：眼睛是白眼球 + 深色瞳孔，二值化后的笔画结构
跟字母几乎一样，模板分能到 0.6。所以先判断**这是不是一张贴在积木上的纸**：

| 闸门 | 量什么 | 默认 | 挡掉 |
|---|---|---|---|
| 白度 `contrast_min` | 标签比四周亮几倍 | ≥1.12 | 眼睛（≈1.0-1.19）、人脸、白墙、屏幕 |
| 笔画 `thin_max` | 最粗笔画 / 短边 | ≤0.30 | 瞳孔、实心色块、椅子扶手 |
| 填充 `fill_min` | 四边形面积 / 外接框 | ≥0.50 | 一条边、一条缝 |
| 分差 `score_min`+`margin_min` | 第一名分数、与第二名差距 | ≥0.70 / ≥0.06 | 「跟谁都不像」的花纹 |

阈值全在 `vision/config/mac.json` → `label.precision`，改完重启即可，不用动代码。
勾上页面右上角「候选框」，每个被拒的候选会在框上写字：`不够白 1.02`、`笔画太粗 0.42`、
`分低 0.61`、`认不准 A/C 0.63:0.62` —— 照着数字调最快。

## 接口（机器人/脚本可直接用，不需要页面）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/zones` | 分区多边形、基准分辨率、位移阈值 |
| POST | `/detect?w=1280&h=720` | body 是 JPEG 原始字节，返回这一帧的 JSON |
| POST | `/anchor` | 用当前这一帧建立基准 |
| POST | `/compare` | 与基准比，返回 `READY / NOT_READY / REVIEW` |
| POST | `/peek` | 手动读一次画面中央（慢，4-15s） |
| POST | `/snapshot` | 把当前帧 + 标注存到 `vision/records/` |
| POST | `/enroll` | body `{"letter":"C"}`，从当前帧登记模板 |
| POST | `/forget` | body `{"letter":"C"}`，删掉该字母的模板 |

`/detect` 返回的关键字段：

```json
{
  "ms": 43.2, "fps": 17.8,
  "tracks": [{"id": 3, "letter": "B", "bbox": [812,404,96,104],
              "score": 0.90, "src": "template", "fresh": true, "zone": "C"}],
  "center_letter": "", "has_base": true, "letters": ["A", "B"],
  "stats": {"candidates": 6, "read": 2, "why": {"dark": 3, "score": 1}}
}
```

## 接机器人摄像头

页面默认用浏览器本机摄像头（`getUserMedia`）。机器人摄像头有三种接法：

**① 机器人推帧，纯后端（最快上手）** —— 不需要浏览器，只要能把 JPEG 发出来：

```python
import requests                      # 机器人端
while True:
    jpg = camera.read_jpeg()          # 你的取流代码
    r = requests.post("http://<mac-ip>:8400/detect?w=1280&h=720",
                      data=jpg, headers={"Content-Type": "image/jpeg"},
                      timeout=5).json()
    print([t["letter"] for t in r["tracks"]])   # ['A', 'B']
```

**② 机器人有浏览器/能开页面**：把机器人摄像头的 MJPEG/WebRTC 流当作 `<video>` 源即可，
`live/page.py` 里换掉 `getUserMedia` 那一段（30 行左右）。这条路我没在真机上验证过，
要你的流地址和格式。

**③ 手机/另一台机器当相机**：`python -m vision.tools.phone_live --port 8300`，
那套是「手机浏览器拍、页面显示标注图」，帧率被回传图锁在 2-3fps，只适合对位置，不适合看实时。

> 机器人端如果是局域网，记得给服务加 `--host 0.0.0.0`（默认只听 127.0.0.1）。

## 已知限制

- **远距离靠模型猜**。标签 40×50px 时模板没法用，只能整帧直读，慢且只当提示；
  想稳就把积木拿近（白纸约占画面 1/8），或现场用「登记」教一次。
- **只在 A/B 上有充分样本**。其它字母要先各自登记一次，否则模型会硬给一个答案。
- **分区（A/B/C/D 黄框）不参与判定**。那套坐标是按另一台机器的取景画的，
  拿它当判据会把「没动」报成「被挪动」；它现在只是给人看的参考线（调试开关里）。
- **模板库是单机文件**（`vision/config/letters.npz`），换机位要重新登记。

## 交接

- `HANDOVER.md`：按模块打包时的旧交接说明（`vision/` 那层），仍然有效的是「离线测试怎么跑」。
- `archive/scan-v1/`：另一条思路的实验版 —— 一次信号触发、抓 8 帧、多尺度、跨帧投票、
  模板与模型互证，准确率更高但延迟 1-2s。主线仍是实时，这个版本留着备用。
