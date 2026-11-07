# 视觉模块接口说明（给 B / C / 队长）

对应 `docs/TEAM_A_VISION.md` 第 9 节。**A 只做观测与差异，不控制机械臂、不承诺抓取可行。**

---

## 1. 给 B（Web 控制台）

### 读什么文件

| 文件 | 内容 | 用法 |
|---|---|---|
| `vision/records/<uuid>.json` | 一份场景快照（schema v1） | 读 `objects` 渲染目标清单 |
| `vision/records/<uuid>_annotated.jpg` | 带区域框、ID、置信度的标注图 | 直接当预览图显示 |
| `vision/records/<uuid>_raw.jpg` | 原图 | 需要复核时用 |
| `vision/records/diff_*.json` | S0/S1 差异结果 | 读 `status` + `differences` 渲染差异表 |
| `vision/records/rejected/*.json` | **被拒绝的 S0**（质量不合格） | 显示为“基准未建立”并给出 `issues` |

图像引用在 JSON 的 `image.reference` 里，是**项目相对路径**（如 `vision/records/xxx_raw.jpg`），不含个人绝对路径。

### 字段枚举（前端下拉/配色按这个来）

| 字段 | 取值 | 含义 |
|---|---|---|
| `zone` | `A` `B` `C` `D` `TEMP` `unknown` | 物体所在区域；`unknown` = 判不准（边界、区域外、未稳定） |
| `visibility` | `visible` `occluded` `out_of_view` `unknown` | 观测到什么；`unknown` ≠ 物体不存在 |
| `differences[].status` | `unchanged` `misplaced` `new` `uncertain` | 差异结论 |
| 整体 `status` | 同上 | 优先级 `uncertain > new > misplaced > unchanged` |
| `mode` | `real` `mock` | **mock 的结果不得用于机器人验收** |

注意两个词不要混：`unknown` 是“没看清”，`uncertain` 是“比不出来”。前端不要用同一个灰色表达。

### 三个固定样例（联调用，均为 mock 合成图）

| 样例 | 路径 | 预期 `status` |
|---|---|---|
| 完全正确 | 同一次运行两次按 `0`/`1` | `unchanged` |
| 单物体移位 | `records/diff_*.json`（红块 A→B） | `misplaced` |
| 遮挡 | `records/diff_*.json`（蓝块被盖） | `uncertain` + `not_observed` |

样例图在 `vision/tests/fixtures/`（合成、无真实人像）。

---

## 2. 给 C（复位规划与验收）

### 怎么读差异

```python
from vision.diff import gate_verdict
verdict = gate_verdict(diff_json)   # "READY" / "REVIEW" / "NOT_READY"
```

| verdict | 条件 | C 该做什么 |
|---|---|---|
| `READY` | `issues == []` 且整体 `unchanged` 且逐项全部 `unchanged` | 才允许考虑验收通过 |
| `REVIEW` | 有 `misplaced` / `new`，无 `uncertain` | 生成复位动作，但需人确认 |
| `NOT_READY` | 有 `uncertain` 或 `issues` 非空 | **停止**，不得生成确定性抓取命令 |

### 硬规则（C 必须遵守）

1. `uncertain` 的物体**不得**生成确定性抓取位姿 —— 只有区域归属，没有可靠中心。
2. `A` 的 `unchanged` 只代表“区域层面一致”，**不保证姿态可抓、不保证抓取成功**。
3. **像素坐标不是抓取坐标**。`center_px` 只用于画图和相对位置，转机械臂需要外参 + 手眼/基座标定，由队长负责。
4. 空目标列表不能“真空式验收成功”：没有目标时整体为 `uncertain`。
5. `mode=mock` 的快照一律不参与机器人验收。

### 常见 `reasons` 与处置

| reason | 含义 | 处置 |
|---|---|---|
| `not_detected` / `not_observed` | 画面里没看到 | 查遮挡、光照、物体是否出视野；**不是 missing** |
| `zone_boundary_ambiguous:<zone>` | 离区域边界太近 | 把物体放回区域中间，或重标区域 |
| `zone_unstable` | 连续 5 帧区域不一致 | 等画面稳定，人退出手部遮挡 |
| `possibly_occluded` | 轮廓填充率过低 | 移开遮挡物 |
| `multi_candidate` | 同色出现第二块 | 同色物体身份不可靠，需人工确认 |
| `too_dark` / `overexposed` | 亮度异常 | 补光或避开反光 |
| `unrecognized_object` | 出现不认识的色块 | 已分配 `unknown_00N` 临时 ID，**阻止验收** |

---

## 3. 给队长（机器人接入）

- 唯一输入口是 `vision/sources.py` 的 `VideoSource`：`read() -> Frame`、`close()`。
- 接 G1 腕部相机：实现 `CameraAdapter(frame_provider, descriptor, resolution=...)`，`frame_provider` 返回 BGR `ndarray`；检测/快照/差异/界面全部不用改。
- 换相机必须重标：分辨率、区域顶点、HSV 三样一起改，`calibration_id` 跟着变；旧基准自动失效。
- 依赖：`opencv-python`、`numpy`、`jsonschema`（Python 3.10+）。零 G1 SDK 依赖。
- 时间戳：UTC ISO 8601（毫秒 + `Z`）；分辨率写在每份 scene 的 `image.width/height`。

---

## 4. PR 描述模板（照抄填空，别夸大）

```markdown
## 做了什么
- 新增 vision/：固定机位桌面视觉 P0（取像 → 区域归属 → 色块定位 → 快照 → S0/S1 差异）
- 输出严格按 A/C 协议 v1（scene / differences 两个 JSON Schema，Draft 2020-12）
- 只输出 unchanged / misplaced / new / uncertain，**无 confirmed missing**

## 启动命令
python -m vision.main --camera <N> --config vision/config/desktop.json --mode real
python -m vision.tools.probe_cameras      # 找哪个编号是真实时画面
python -m vision.tools.calibrate          # 鼠标点四角标定区域

## 证据
- 单测：python -m unittest vision.tests.test_vision vision.tests.test_calibrate → NN 项通过
- mock 端到端：红块 A→B 判 misplaced，绿蓝 unchanged；遮挡判 uncertain/not_observed
- 真实摄像头：<已跑 / 未跑，原因填这里>
- 测试记录：vision/records/TEST_LOG_TEMPLATE.md（八类各 5 次）

## 已知限制
- 像素坐标不是抓取坐标，转机械臂需队长做外参/手眼标定
- 单目 2D：同色多块身份不可靠，报 uncertain 不交换 ID
- confidence 是启发式质量分，不是统计概率
- 姿态（yaw/倾斜）不在 P0
- mock 结果不得用于机器人验收

## 待对接
- B：消费 records/*.json + diff_*.json（字段枚举见 vision/docs/INTERFACE.md）
- C：用 diff.gate_verdict() 取 READY/REVIEW/NOT_READY
- 队长：实现 CameraAdapter 接腕部相机
```
