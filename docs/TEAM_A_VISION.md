# RE-covery：队友 A 视觉开发任务书

> Junction China 2026 · 基于 GALBOT G1 的实体实验 / 机器人测试环境自动复位与验证系统
>
> 本文是开发约定，不代表已经运行机器人。机器人摄像头暂不可用：**立即使用自己电脑内置或 USB 摄像头开发，不等待机器人，不依赖 G1 SDK。**

## 1. 分工与交付目标

- 队长：真机控制、后端、坐标转换及系统集成，最终决定机器人动作是否安全可执行。
- A（你）：取像、固定区域识别、物体定位、场景快照、S0/S1 差异检测、视觉不确定性报告。
- B：Web 控制台，展示图像、区域、物体、差异与异常。
- C：复位规划、状态验收；消费 A 的结构化结果，不能把视觉不确定当成验收成功。

A 不负责直接控制机械臂、不承诺抓取成功、不替 C 制定动作计划。交付可在普通电脑运行的 `vision/`、样例数据、测试记录和接口说明。本文协议先作为 A/C 联调基线，改字段必须同步 B、C、队长。

## 2. 范围与优先级

### P0：必须完成

1. OpenCV 实时取像，摄像头打开失败时明确报错。
2. 固定机位、固定光线，桌面标出互不重叠的 A/B/C/D/TEMP 五个多边形区域。
3. 用 2–3 块颜色明显且颜色互异的积木，HSV 阈值 + 轮廓检测，输出中心像素坐标、区域、置信度和可见性。
4. 按 `0` 保存 S0 基准；按 `1` 保存 S1 并比较；按 `q` 退出。快照图像和 JSON 一起落盘。
5. 区分 `unchanged / misplaced / new / uncertain`。遮挡、未检出、移出视野均不能直接认定 confirmed missing。
6. 提供实际摄像头运行证据与八类测试记录；异常不能自动放行。

### P1：P0 稳定后再做

连续帧稳定性、参数配置工具、画面配准或平面单应性标定、摄像头替换、简单接口服务。重复颜色的身份跟踪、复杂遮挡、模型训练、深度与抓取位姿不属于 P0。先完成稳定演示，不必训练模型。

## 3. 最小演示流程

固定相机和桌面，保证五个区域都在画面内；避免反光、手持相机和自动曝光大幅变化。每块积木分配持久 ID，例如红色 `block_red_01`。P0 要求不同目标颜色互异；同色物体无法可靠区分时报告 uncertain，不能随意交换 ID。

启动预览 → 等待画面稳定 → 摆好基准 → 按 `0` → 移动或交换积木 → 手退出画面 → 按 `1` → 显示带区域和检测框的 S1，以及逐项差异。TEMP 是真实临时存放区，不是 unknown 的别名。

输出画面应显示运行模式、摄像头编号、时间、区域边界、ID、中心坐标、置信度及不确定原因。保存原图与标注图，避免只留截图而无法复核。

## 4. 环境与终端命令

推荐 Python 3.10–3.12。依赖写入 `vision/requirements.txt`：

```text
opencv-python>=4.8,<5
numpy>=1.24,<3
jsonschema>=4.18,<5
```

macOS / Linux，在仓库根目录：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r vision/requirements.txt
python -m vision.main --camera 0 --config vision/config/desktop.json --mode real
```

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r vision/requirements.txt
python -m vision.main --camera 0 --config vision/config/desktop.json --mode real
```

若激活被策略限制，可直接用 `.\.venv\Scripts\python.exe` 执行安装及运行命令，无需修改系统策略。以上入口和参数是需要实现的交付接口，当前任务书并未提供已实现程序。

快速验证摄像头：

```python
import cv2
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    raise RuntimeError('无法打开摄像头：检查权限、占用和编号')
try:
    while True:
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError('摄像头读帧失败，停止生成有效快照')
        cv2.imshow('RE-covery camera', frame)
        if cv2.waitKey(1) & 0xff == ord('q'):
            break
finally:
    cap.release()
    cv2.destroyAllWindows()
```

Mac：在“系统设置 → 隐私与安全性 → 摄像头”允许启动程序的终端或 IDE，必要时重启该应用；不要仅给浏览器权限。Windows：在隐私设置允许桌面应用访问摄像头；内置/USB 对应编号可能是 0、1、2，逐个尝试，关闭占用摄像头的视频会议软件。读帧失败必须报错，不得用旧帧冒充实时画面。

## 5. 建议目录与实现步骤

```text
vision/
  __init__.py
  main.py                 # 命令行、预览及按键
  sources.py              # VideoSource / CameraAdapter
  detector.py             # HSV、轮廓、质量判断
  zones.py                # 多边形归属
  snapshot.py             # 快照及校验
  diff.py                 # S0/S1 比较
  config/desktop.json     # 分辨率、HSV、区域、阈值
  schemas/scene.schema.json
  schemas/differences.schema.json
  requirements.txt
  README.md
  tests/fixtures/          # 已脱敏的图像和预期结果
  records/                # 测试记录
```

1. 先完成取像和退出；`VideoSource.read()` 返回帧、采集时间和来源信息，`close()` 释放资源。OpenCV 实现包装 `cv2.VideoCapture`。
2. 配置固定分辨率和区域顶点，使用 `cv2.pointPolygonTest` 判断中心归属。落在边界附近、区域外或跨区严重时标 unknown，并给原因。
3. BGR 转 HSV，每种颜色 `inRange`，形态学开闭运算，再 `findContours`；红色注意色相环两段阈值。按面积、形状过滤噪声，以矩计算中心。
4. 对目标匹配持久 ID；多候选、过小轮廓、遮挡疑似、亮度异常和连续帧跳动降低可信度。置信度是启发式质量分数，不能宣称统计概率。
5. 建议连续 5 帧区域一致才接受；阈值初值 0.8，需通过实际测试调整。未稳定时仍输出 uncertain，不靠延迟隐藏错误。
6. 按键生成不可变快照，写入 UUID、UTC ISO 8601 时间、来源、标定版本、图像引用；保存失败则快照失败。基准无效时拒绝建立 S0。
7. 实现差异函数并用固定图像复测；再接 B/C。相机移动、分辨率改变或区域配置改变后，旧基准失效，必须重新标定并捕获 S0。

## 6. 坐标、透视与标定边界

像素坐标原点在图像左上角，x 向右、y 向下，单位 px，必须附图像宽高。透视会使同样距离在不同位置对应不同像素长度。P0 可直接在当前图像手工标区域，但机位必须固定。

平面单应性至少需要四个非共线、对应关系已知的桌面点，可将像素映射到桌面平面；它不自动解决高度、镜头畸变、机械臂基座变换或夹爪姿态。像素 → 机械臂空间坐标属于后续队长对接，需要相机内外参、手眼/基座标定与安全验证。**禁止把中心像素直接作为可抓取坐标。**

机器人摄像头接入后，新增 `CameraAdapter` 替换输入，保持检测与差异函数消费统一帧结构。换相机必须更新分辨率、HSV、区域和标定，不能沿用桌面电脑参数，但无需推翻识别逻辑。

## 7. A/C 统一数据协议 v1

约定：`zone` 只允许 A/B/C/D/TEMP/unknown；`visibility` 只允许 visible/occluded/out_of_view/unknown。无法判断原因时用 unknown，不能凭“未检出”推断 out_of_view。未检出的已知物体仍保留 ID，中心为 null、区域 unknown、可见性 unknown。`status` 是差异状态，只允许 unchanged/misplaced/new/uncertain；不提供 confirmed missing。unknown 是观测未知，uncertain 是比较结论，两者不可互换。

以下为可保存并用 `jsonschema` 执行的 JSON Schema（Draft 2020-12）。时间格式需启用 `FormatChecker`。Schema 约束结构；下面的业务规则仍须实现。

### `vision/schemas/scene.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "snapshot_id", "timestamp", "mode", "source", "image", "calibration", "quality_ok", "issues", "objects"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "snapshot_id": {"type": "string", "minLength": 1},
    "timestamp": {"type": "string", "format": "date-time"},
    "mode": {"enum": ["real", "mock"]},
    "source": {"type": "string", "minLength": 1},
    "image": {
      "type": "object", "additionalProperties": false,
      "required": ["reference", "width", "height"],
      "properties": {"reference": {"type": "string", "minLength": 1}, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}}
    },
    "calibration": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "valid", "coordinate_frame"],
      "properties": {"id": {"type": "string"}, "valid": {"type": "boolean"}, "coordinate_frame": {"const": "image_pixels"}}
    },
    "quality_ok": {"type": "boolean"},
    "issues": {"type": "array", "items": {"type": "string"}},
    "objects": {
      "type": "array",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["object_id", "zone", "visibility", "confidence", "center_px", "reasons"],
        "properties": {
          "object_id": {"type": "string", "minLength": 1},
          "zone": {"enum": ["A", "B", "C", "D", "TEMP", "unknown"]},
          "visibility": {"enum": ["visible", "occluded", "out_of_view", "unknown"]},
          "confidence": {"type": "number", "minimum": 0, "maximum": 1},
          "center_px": {"oneOf": [{"type": "null"}, {"type": "array", "prefixItems": [{"type": "number", "minimum": 0}, {"type": "number", "minimum": 0}], "minItems": 2, "maxItems": 2}]},
          "reasons": {"type": "array", "items": {"type": "string"}}
        }
      }
    }
  }
}
```

### `vision/schemas/differences.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object", "additionalProperties": false,
  "required": ["schema_version", "baseline_id", "current_id", "timestamp", "mode", "status", "issues", "differences"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "baseline_id": {"type": "string"},
    "current_id": {"type": "string"},
    "timestamp": {"type": "string", "format": "date-time"},
    "mode": {"enum": ["real", "mock"]},
    "status": {"enum": ["unchanged", "misplaced", "new", "uncertain"]},
    "issues": {"type": "array", "items": {"type": "string"}},
    "differences": {
      "type": "array",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["object_id", "status", "from_zone", "to_zone", "confidence", "reasons"],
        "properties": {
          "object_id": {"type": "string"},
          "status": {"enum": ["unchanged", "misplaced", "new", "uncertain"]},
          "from_zone": {"enum": ["A", "B", "C", "D", "TEMP", "unknown"]},
          "to_zone": {"enum": ["A", "B", "C", "D", "TEMP", "unknown"]},
          "confidence": {"type": "number", "minimum": 0, "maximum": 1},
          "reasons": {"type": "array", "items": {"type": "string"}}
        }
      }
    }
  }
}
```

### 示例数据（仅示例，非真实运行结果）

```json
{
  "schema_version": "1.0", "snapshot_id": "s1-demo",
  "timestamp": "2026-11-06T08:00:00Z", "mode": "mock", "source": "fixture:demo",
  "image": {"reference": "fixtures/s1.jpg", "width": 1280, "height": 720},
  "calibration": {"id": "desktop-v1", "valid": true, "coordinate_frame": "image_pixels"},
  "quality_ok": true, "issues": [],
  "objects": [{"object_id": "block_red_01", "zone": "B", "visibility": "visible", "confidence": 0.94, "center_px": [420, 280], "reasons": []}]
}
```

```json
{
  "schema_version": "1.0", "baseline_id": "s0-demo", "current_id": "s1-demo",
  "timestamp": "2026-11-06T08:00:00Z", "mode": "mock", "status": "misplaced", "issues": [],
  "differences": [{"object_id": "block_red_01", "status": "misplaced", "from_zone": "A", "to_zone": "B", "confidence": 0.94, "reasons": ["zone_changed"]}]
}
```

### 必须实现的业务规则

- 同一快照 object_id 唯一，像素坐标必须在图像范围内；visible 目标必须有中心。低可信、unknown 区域或非 visible 不得产生 confirmed unchanged/misplaced。
- S0、S1 同模式、同来源、同标定版本与分辨率且有效，图像引用存在；否则整体 uncertain。mock 不得参与真实机器人验收。
- 对两个快照 ID 并集比较，每个 ID 都输出一项（包括 unchanged）。双侧可靠且区域相同 → unchanged；双侧可靠且区域不同 → misplaced。
- 只有 S0 覆盖完整且可信、S1 中确认为新增的稳定目标才可判 new，from_zone 为 unknown；无法确认身份或仅出现不认识的色块 → 分配 `unknown_001` 等稳定临时 ID 并报告 uncertain / `unrecognized_object`，阻止验收。
- S0 存在但 S1 未检出 → uncertain / `not_observed`，不能宣称 missing。遮挡、越界、光照变化、读帧失败都不自动放行。
- 差异置信度取参与观测质量的保守值（例如双方最小值），不是凭空增加。
- 整体 status 优先级：uncertain > new > misplaced > unchanged。任一质量异常或未知物体强制 uncertain；空目标不能真空式验收成功。
- C 仅在有效真实快照、目标清单完整、全部 unchanged、无 issues 且连续稳定时考虑验收通过；其余状态停留待复核/待规划。A 的 unchanged 仅说明区域层面一致，不保证姿态或抓取可行。

校验方式示意：

```python
import json
from jsonschema import Draft202012Validator, FormatChecker
schema = json.load(open('vision/schemas/scene.schema.json', encoding='utf-8'))
data = json.load(open('vision/records/s1.json', encoding='utf-8'))
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
```

## 8. 测试清单与记录

每项至少重复 5 次，同时保留失败案例；先用同一固定环境，再改变条件。

| 场景 | 预期 |
|---|---|
| 单物体从 A 移到 B | 对应 ID misplaced，其他 unchanged |
| 两物体交换区域 | 两个 ID 都 misplaced，身份不交换 |
| 完全正确 | 全部 unchanged，质量检查通过 |
| 手/纸遮挡目标 | uncertain，不自动判 missing 或通过 |
| 移出视野 | uncertain；无可靠证据时 visibility=unknown |
| 未知新增色块 | unknown 临时 ID，uncertain，阻止验收 |
| 光线明显变化 | 能稳定识别或明确 uncertain，不能错误放行 |
| 边界摆放/检测抖动 | 未稳定时 uncertain，不反复生成确认动作 |

记录模板：

```text
用例 ID / 操作者 / 日期时间：
代码 commit / 配置及标定版本 / real 或 mock：
摄像头型号、编号、分辨率、光线：
S0 ID、原图及 JSON / S1 ID、原图及 JSON：
操作与人工真实标签：
预期状态 / 实际状态 / 差异 JSON 路径：
重复次数 / 正确次数 / 错误放行次数：
失败原因 / 修复事项 / 复测结果：
```

验收：普通电脑按 README 可启动；五区域和 2–3 目标稳定展示；上述测试各 5 次有记录，正常/错位用例每项至少 4/5 正确，遮挡、未知、视野外及质量异常**错误放行次数必须为 0**。不达标则明确标记限制并修复，不能挑选成功片段冒充完成。

## 9. 接口交接

- 给 B：快照、标注图、差异 JSON、字段枚举与异常示例。先约定文件读取或由队长后端提供接口，A 不必另建 Web 服务。图像引用用项目相对路径或后端可访问 URL，不泄漏个人绝对路径。
- 给 C：S0/S1、目标清单、区域定义、逐项差异与 uncertain 原因；先联调正确、交换、遮挡三个固定样例，再真实摄像头演示。C 不得对 unknown 生成确定抓取命令。
- 给队长：VideoSource 接口、分辨率与时间戳规则、标定配置、运行依赖、性能记录及局限。未来机器人源只替换适配器，机械臂坐标转换由队长负责。
- mock 必须通过显式 `--mode mock` 选择，在画面、JSON、测试记录中全部标注；real 必须来自实际成功读帧。摄像头失败禁止静默回退 mock。

## 10. 进度安排与 Git 提交

按有效开发时间安排：0–1 小时跑通相机与目录；1–3 小时完成区域和色块；3–5 小时完成快照与差异；5–6 小时对齐 C/B；6–8 小时完成测试、失败修复和演示。若时间不足，保留 P0，删除 P1，及时报告阻塞。

仓库根目录创建分支（分支已存在则切换已有分支），每完成一个可运行阶段提交一次：

```bash
git switch -c feat/vision-desktop
git status
git add vision docs/TEAM_A_VISION.md
git diff --cached
git commit -m "feat: add desktop camera vision snapshots and diff"
git push -u origin feat/vision-desktop
```

提交前检查暂存内容，不提交 `.venv/`、缓存、密钥或未经同意的人脸/室内照片。测试样例需脱敏；大视频交付链接及摘要即可。PR 写明已完成项、启动命令、真实/mock 证据、测试统计、已知限制和待队长对接事项，不声称已经运行 G1。

## 11. 可直接复制给 Codex 的提示词

```text
请先阅读 docs/TEAM_A_VISION.md 和仓库现有约定。我要实现 P0 桌面摄像头视觉模块，零基础，请按可运行的小步骤执行。
先实现 vision/sources.py 和 main.py，使用 cv2.VideoCapture(0)，支持 --camera、--config、--mode，明确处理权限、读帧失败和退出释放。不要依赖 G1 SDK，不要操作机器人，不要静默使用 mock。完成后告诉我安装和运行命令，并区分你实际验证过的内容与需要我本机摄像头验证的内容。
```

```text
继续按任务书实现固定区域 A/B/C/D/TEMP、2–3 个不同颜色积木的 HSV+轮廓检测。把阈值和多边形写进配置。输出持久 object_id、像素中心、zone、visibility、confidence 和 reasons。边界、遮挡、身份歧义、抖动必须 uncertain。先复用已有代码，不训练模型。请解释我如何用预览调整参数。
```

```text
实现 0 捕获 S0、1 捕获 S1 和逐项差异，严格遵守任务书 Schema 和业务规则。补全 JSON Schema 文件及固定样例校验，检查未检出不能成为 confirmed missing、未知物体不能通过验收、模式或标定不一致必须 uncertain。生成测试记录空模板，不编造实际运行记录。最后列出供 B/C/队长使用的文件、启动命令和仍需现场验证的项目。
```
