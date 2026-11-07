# 交接说明 · 视觉识别模块

> ⚠️ 这份是按模块打包时的旧交接（只覆盖 `vision/` 那层）。**当前怎么跑、接口是什么、
> 参数怎么调，以根目录 `README.md` 为准**。这里仍然有效的是「离线测试怎么跑」和
> 「源码原来在哪个仓库」两节。

> 包内容是 `vision/` 的完整源码 + 两份真实失败样例。三条命令就能跑起来，
> 不需要摄像头也能验证逻辑（79 项离线测试）。

## 0. 先说清楚：源码本来在哪

视觉源码**不在** `rebench/` 里。仓库根目录结构是：

```
<仓库根>/vision/      ← 视觉模块（本包内容：19 个 py、约 6500 行）
<仓库根>/rebench/     ← 机器人控制台 / 后端（perception.py、runner.py…）
<仓库根>/docs/        ← 任务书与说明
```

`rebench/` 是控制台那套，`vision/` 是独立模块，两者通过 `vision/schemas/` 的
JSON 协议对接。“只有控制台、没有 vision/”，基本是只拷了 `rebench/` 这一层。

## 1. 解包后怎么跑

```bash
# 解压到仓库根目录，让 vision/ 与 rebench/ 同级
cd <仓库根>
python -m pip install -r vision/requirements.txt

python -m unittest vision.tests.test_vision          # 79 项，约 4 秒，不用摄像头
python -m vision.tools.vlm_check                     # 视觉模型自检（可选，要 key）
python -m vision.main --camera 0 --config vision/config/desktop.json --mode real
python -m vision.tools.phone_live --port 8300        # 手机当相机（HTTPS）
python -m vision.tools.mac_live --config vision/config/mac.json   # 只用 Mac 本机摄像头
```

## 2. 包里有什么

| 路径 | 说明 |
|---|---|
| `vision/*.py` | 找标签 / 认字母 / 视觉模型 / 检测 / 区域 / 快照 / 差异 |
| `vision/config/desktop.json` | 全部现场参数（分辨率、五区域、标签、模型） |
| `vision/config/mac.json` | 同参数，Mac 桌面机位专用（标签门槛更松） |
| `vision/config/letters.npz` | 字母模板库（真机登记出的 A、B） |
| `vision/schemas/*.json` | 与 B/C 共用的 JSON 协议（Draft 2020-12） |
| `vision/tests/` | 79 项离线回归 + 夹具图 |
| `docs/VISION_LETTER_VLM.md` | 技术说明：原理、参数、踩过的坑、已知限制 |
| `fail_cases/` | 基准图 + 两份真实失败原图 + 标注图 + `REPORT.txt` |

**未打包**（需要时单独给）：`vision/records/`（27MB 现场快照）、
`vision/certs/`（自签证书，含私钥，不外传）。

## 3. 包里的图是什么

| 文件 | 场景 | 结果 |
|---|---|---|
| `ok_100pct.jpg` | 正常距离 | A 0.87 / B 0.91，两块都认出 |
| `fail_40pct_zoom.jpg` | 手机退远到 40%（约 2.5 倍距离） | 只认出 A，**少了 block_B** |
| `fail_outframe.jpg` | 两块积木被推到画面右边缘 | **0 个物体**（设计行为） |

两张 `*_ann.jpg` 是同一帧的标注图，`REPORT.txt` 里有完整 issues、逐张标签的
实测 bbox/面积/笔画数、被拒候选和拒绝原因。

**失败 1 的根因（已定位，不是猜的）**：B 的标签在 45% 时还是干净的四边形；
缩到 40% 后，同一位置附近的候选被 `approxPolyDP` 近似成 **8–9 个顶点**，
撞上 `find_labels` 里 `len(approx) != 4` 这道硬门槛，于是整张标签进不了候选。
标签变小 → INTER_AREA 把边缘糊在一起 → 一条直边被拆成两段。
**已修（2026-11-07）**：`letters.py` 加了 `_approx_quad()` —— 顶点数 ≠4 时先放大
epsilon 重试，仍不行就退到最小外接旋转矩形。兜底带着几何体检（短边下限、长边上限
260px、长短边比 ≥0.25），否则会把桌面上斜着的细线救回来、再以"包含"关系把真标签
去重掉（这个坑实测踩过一次）。同一张 `fail_40pct_zoom.jpg` 现在 A 0.87 / B 0.87
两块都认出来了，`ok_100pct.jpg` 结果不变，97 项回归全过。

**失败 2 的行为是对的**：真标签贴着右边缘被切，只剩三条边 → 进不了候选；
桌面横线纸被凑出两个矩形，读成 A 0.51 / B 0.54，低于阈值 → 不产出物体。
最终 0 个物体 + `label_unread`，下游落到 REVIEW。**宁可认不出，也不编 ID。**

> 备注：早先记录过“整图转 20° 会漏掉 A”，复核后确认**是测试脚本的问题** ——
> 旋转时用了深色填充，在画面里造出一条斜的暗边，Canny 把标签和这条不存在的边
> 连成一体（框 270×134 → 849×514）。改成边缘复制填充后，0/20/40/70/200° 全认对。
> 教训写进了 `working/check_label_pipeline.py` 的注释：测量工具本身会造缺陷。

## 4. 现在能做什么 / 还不能做什么

**能**：固定机位下同时认 2–3 块写了字母的积木；镜头退远（标签缩到 40%）仍能找框；
任意角度（15° 步进搜索）；
S0/S1 快照与逐项差异；手写标签现场登记与删除；接视觉大模型后可认未登记的字母（需 key）。

**还不能**：标签被画面边缘切开时认不出；桌面上过小的白块（<最宽标签面积的 35%）
按背景跳过、不再拖垮合格判定，但如果它正好是积木标签就会被漏掉；
同色积木仅靠颜色区分；像素坐标转机械臂坐标（队长的手眼标定）；未接过 G1。

参数含义、压力测试数据、失败排查表都在 `docs/VISION_LETTER_VLM.md`。

## 2026-11-07 补充（只用 Mac 摄像头那一版）

- `vision/config/.env.local`（权限 600）存 `VISION_API_KEY`，`launch-mac.command` 直接 source 它，
  双击不再问 token；`mac.json` 里仍然是 `env:VISION_API_KEY`，key 没进配置/代码/日志。
- **模型在实时链路上验过**：`vlm_check` 3/3（A/B/D），`/frame` 返回 `reader=模型+模板 cn:kimi-k2.5`，
  `vlm.failures=0`。
- 修掉两个会当场出丑的问题：
  1. `label_pending`（模型还在读）以前直接算 issue，桌上随便一片白纸就能把 `quality_ok` 按成 false，
     基准永远建不起来。现在 pending 和 unread 走同一套「比同帧最小真标签小 35% 就按背景跳过」。
  2. `letters.py` 的 `q @ mat` 在这台机器的 numpy/BLAS 上每帧刷
     `divide by zero / overflow encountered in matmul`（结果是对的）。加 `np.errstate` 静音，
     模板矩阵统一 float32，读数不变（A 0.866 / B 0.973），97 项回归全过。
