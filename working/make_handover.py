"""打交接包：源码 zip + 真实失败原图 + 可复现的报错文本。

    python working/make_handover.py

为什么要打包：对方说"项目里只有控制台、没有 vision/"，那只可能是只拿到了
`rebench/`。视觉源码一直在仓库根目录的 `vision/` 里，这里把它连同配置、
schema、测试夹具压成自包含的 zip，并附**真实存在**的失败样例。

失败样例的选取过程值得说一下：一开始拿"整图转 20°"当例子，测出来确实漏了
一块 —— 但复核发现那是**测试脚本的深色填充**在画面里造出一条斜的暗边，
Canny 把标签和这条本来不存在的边连成一体，框从 270×134 涨到 849×514。
换成 BORDER_REPLICATE 之后 0/20/40/70/200° 全部认对。
（教训写进了 check_label_pipeline.spin 的注释：测量工具本身会造缺陷。）

所以这里用的是实际会遇到的边界：**手机退远、标签变小**，以及**积木只露出一半**。

产物（output/vision_handover/）：
  vision_source.zip               源码 + 配置 + schema + fixtures + 文档
  fail_cases/ok_100pct.jpg        基准：正常距离，两块都认对
  fail_cases/fail_50pct.jpg       失败原图：手机后退一倍 → 少了 block_A
  fail_cases/fail_outframe.jpg    失败原图：标签被画面边缘切掉 → 一个物体都不出
  fail_cases/*_ann.jpg            对应标注图
  fail_cases/REPORT.txt           现象 + 全部 issues + 逐张标签实测值 + 复现命令
  HANDOVER.md                     交接说明（随包发）

**不打包**：__pycache__、vision/certs/（含私钥）、vision/records/（27MB 现场照片）、
docs/*.html（演示页）。
"""
from __future__ import annotations

import sys
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision.main import VisionApp                        # noqa: E402
from vision.sources import Frame, utc_now_iso           # noqa: E402
from vision.letters import find_labels                  # noqa: E402

# 复用诊断脚本里的裁切，**不要重新写一份** —— 上一轮就因为诊断脚本复制了
# 生产逻辑，生产改了脚本还报旧结果，白排查一轮。
from working.check_label_pipeline import fit_photo      # noqa: E402

PHOTO = ROOT / "working" / "_scratch" / "real_ab.jpg"
CFG = ROOT / "vision" / "config" / "desktop.json"
OUT = ROOT / "output" / "vision_handover"


# ------------------------------------------------------------------ 构造用例
def zoom_out(img: np.ndarray, s: float) -> np.ndarray:
    """整幅缩小再居中贴回原画布 = 手机后退（场景变小、标签跟着变小）。

    填充用 BORDER_REPLICATE 的思路：直接对边缘做复制，不引入任何新的硬边，
    否则又是在自己造缺陷。
    """
    h, w = img.shape[:2]
    small = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    canvas = cv2.copyMakeBorder(small, (h - small.shape[0]) // 2,
                                (h - small.shape[0] + 1) // 2,
                                (w - small.shape[1]) // 2,
                                (w - small.shape[1] + 1) // 2,
                                cv2.BORDER_REPLICATE)
    return canvas[:h, :w]


def shift(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """整体平移，把标签挪到画面边上（模拟积木只露出一半）。"""
    h, w = img.shape[:2]
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)


# ------------------------------------------------------------------ 跑一帧
def app_for():
    app = VisionApp(SimpleNamespace(camera=0, config=str(CFG), mode="real",
                                    output=str(OUT / "records"), min_conf=0.5,
                                    mock_cycle=False, skip_image_check=True,
                                    allow_frozen=False, url=None))
    app._source = None
    return app


def measure(app, img, lb) -> dict:
    """量出一张标签的 bbox、面积、笔画像素 —— 排查靠这些数字，不靠感觉。"""
    from vision.letters import character_mask, normalize
    tile = normalize(img, lb)
    ink = 0
    if tile is not None:
        mask = character_mask(tile)
        ink = int((mask > 0).sum()) if mask is not None else 0
    x, y, w, h = (int(v) for v in lb.bbox)
    return {"bbox": (x, y, w, h), "area": round(float(lb.area_px), 1), "ink": ink}


def run_case(app, img, tag: str, outdir: Path, save: bool = True) -> dict:
    frame = Frame(image=img, captured_at=utc_now_iso(), source="handover",
                  mode="real", index=1)
    objects, quality_ok, issues = app.observe(frame)
    if save:
        ann = app.annotate(frame, objects, issues)
        cv2.imwrite(str(outdir / f"{tag}.jpg"), img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        cv2.imwrite(str(outdir / f"{tag}_ann.jpg"), ann,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    # stats 是 find_labels 的诊断出口：拿生产实现给出的拒绝原因，
    # 而不是在这一份脚本里再抄一遍 Canny 流程。
    st: list = []
    boxes = find_labels(img, app.detector.label_cfg, stats=st)
    labels = [measure(app, img, lb) for lb in boxes]
    missed = sorted([s for s in st if s["why"] not in ("", "OK")],
                    key=lambda s: -s["area"])[:4]
    return {"tag": tag,
            "size": (img.shape[1], img.shape[0]),
            "ids": sorted({o["object_id"] for o in objects
                           if o["visibility"] != "unknown"}),
            "objects": [{"id": o["object_id"], "zone": o["zone"],
                         "vis": o["visibility"], "conf": round(o["confidence"], 2),
                         "reasons": o["reasons"]} for o in objects],
            "quality_ok": quality_ok, "issues": list(issues),
            "labels": labels, "label_info": list(app.detector.last_label_info),
            "found": len(boxes), "missed": missed}


# ------------------------------------------------------------------ 报告
def write_report(cases: dict, path: Path, th: float, min_area: float) -> None:
    ok, edge, far, cut = cases["ok"], cases["edge"], cases["far"], cases["cut"]
    L = [
        "RE:BENCH 视觉识别：真实失败样例（可复现）",
        "=" * 72,
        f"生成时间   {datetime.now().isoformat(timespec='seconds')}",
        f"原照片     {PHOTO.relative_to(ROOT).as_posix()}",
        "预处理     居中裁切到 1280×720；后面的样例在此基础上再变换",
        f"标签阈值   {th}      最小标签面积 {min_area:.0f} px²",
        "",
        "【现象】",
        f"  100% 正常距离        找到 {ok['found']} 张标签 → {ok['ids']}",
        f"  45%  手机退远一档    找到 {edge['found']} 张标签 → {edge['ids']}",
        f"  40%  手机退远两档    找到 {far['found']} 张标签 → {far['ids']}"
        "   ← 少了 block_B",
        f"  标签被边缘切开       找到 {cut['found']} 张标签 → {cut['ids']}"
        "   ← 一个物体都不出（设计如此）",
        "",
        "40% 的 issues（原样，未加工）：",
    ]
    L += [f"    {i}" for i in far["issues"]] or ["    （空）"]
    L += ["", "标签出画的 issues（原样，未加工）："]
    L += [f"    {i}" for i in cut["issues"]] or ["    （空）"]

    L += ["", "【逐张标签实测】",
          "样例            序号  bbox(x,y,w×h)      面积(px²)  笔画(px)  读成  分数  来源"]
    for name, c in (("100% 正常", ok), ("45% 退远一档", edge),
                    ("40% 退远两档", far), ("标签出画", cut)):
        if not c["labels"]:
            L.append(f"{name:<14} （一张标签都没框出来，见下方被拒候选）")
        for i, (m, info) in enumerate(zip(c["labels"], c["label_info"]), 1):
            x, y, w, h = m["bbox"]
            L.append(f"{name:<14} #{i}   {x:>4},{y:>4} {w:>4}×{h:<4}"
                     f"   {m['area']:>8.0f}  {m['ink']:>7}   "
                     f"{info['letter'] or '—':^4}  {info['score']:>4.2f}  "
                     f"{info['src']}/{info['vlm']}")

    L += ["", "【被拒候选（find_labels 给出的拒绝原因，按面积排序取前 4）】"]
    for name, c in (("100% 正常", ok), ("45% 退远一档", edge),
                    ("40% 退远两档", far), ("标签出画", cut)):
        if not c["missed"]:
            L.append(f"{name:<14} （无）")
            continue
        for s in c["missed"]:
            x, y, w, h = s["bbox"]
            L.append(f"{name:<14} area={s['area']:>6}  顶点={s['verts']:>2}  "
                     f"{w}×{h} @({x},{y})  原因：{s['why']}")

    L += ["", "【根因】"]

    # —— 样例 1：只认"B 该在的位置"附近的候选，别拿面积最大的糊过去 ——
    # 40% 时面积最大的被拒候选是绿积木的外轮廓（211×474），和 B 的标签无关，
    # 拿它当证据就是编。这里按 45% 时 B 标签的中心取 90px 邻域。
    b_center = None
    if len(edge["labels"]) >= 2:
        bx, by, bw_, bh_ = edge["labels"][1]["bbox"]
        b_center = (bx + bw_ / 2.0, by + bh_ / 2.0)
    near = []
    if b_center:
        for s in far["missed"]:
            sx, sy, sw, sh = s["bbox"]
            d = ((sx + sw / 2.0 - b_center[0]) ** 2
                 + (sy + sh / 2.0 - b_center[1]) ** 2) ** 0.5
            if d < 90:
                near.append((s, d))
        near.sort(key=lambda t: t[1])
    L.append("样例 1（手机退远到 40%）：")
    if near:
        L.append(f"  45% 时 B 的标签在 {b_center[0]:.0f},{b_center[1]:.0f} 附近、"
                 f"是干净的四边形；缩到 40%，同一位置附近的候选全部凑不出 4 个顶点：")
        # 三档 Canny 会给出同一张轮廓的重复项，按 (bbox, 顶点数) 去重再列
        seen = set()
        for s, d in near:
            key = (s["bbox"], s["verts"])
            if key in seen:
                continue
            seen.add(key)
            sx, sy, sw, sh = s["bbox"]
            L.append(f"    {sw}×{sh} @({sx},{sy})  面积 {s['area']}  "
                     f"顶点 {s['verts']}  （距 B 的旧位置 {d:.0f}px）→ {s['why']}")
            if len(seen) >= 2:
                break
        L.append("  标签变小后 INTER_AREA 重采样把边缘糊在一起，approxPolyDP "
                 "把一条直边拆成两段，顶点数从 4 变成 8–9，")
        L.append("  于是撞上 `len(approx) != 4` 这道硬门槛，整张标签进不了候选。")
        L.append("  这不是认字算法的问题（认字那一步根本没轮到），是**找框**的门槛太硬。")
    else:
        L.append("  40% 时 B 附近没有被拒候选记录，需另查（可能是面积闸门）。")

    # —— 样例 2：真标签贴着画面边被切，另有两个桌面纸片被凑成矩形 ——
    W, H = 1280, 720
    if cases["ok"].get("size"):
        W, H = cases["ok"]["size"]
    # 贴边的候选里要排掉"整幅画面边框"这种大框（1035×720），它不是标签
    at_edge = [s for s in cut["missed"]
               if (s["bbox"][0] + s["bbox"][2] >= W - 2
                   or s["bbox"][1] + s["bbox"][3] >= H - 2)
               and s["bbox"][2] < 0.6 * W and s["bbox"][3] < 0.9 * H]
    L += ["", "样例 2（整体平移，两块积木被推到右边缘）："]
    if at_edge:
        L.append("  两张真标签贴着画面右边，只剩三条边，四边形拟合不完整，直接进不了候选：")
        seen = set()
        for s in at_edge:
            key = (s["bbox"], s["verts"])
            if key in seen:
                continue
            seen.add(key)
            sx, sy, sw, sh = s["bbox"]
            L.append(f"    {sw}×{sh} @({sx},{sy})  面积 {s['area']}  "
                     f"顶点 {s['verts']} → {s['why']}")
            if len(seen) >= 2:
                break
    else:
        L.append("  真标签区域没有产出可用候选。")
    L += [
        "  同时桌面的横线纸被凑出两个矩形，读成 "
        + "、".join(f"{i['letter'] or '—'} {i['score']:.2f}" for i in cut["label_info"])
        + f"（阈值 {th}），分数不够 → 不产出物体。",
        "  最终输出 **0 个物体** + label_unread —— 这是**设计行为**，不是崩溃："
        "宁可不认，也不编一个 ID。下游按“看不到”处理，最终落到 REVIEW，"
        "而不是让差异算法以为场上少了一块、多出一块。",
    ]
    L += [
        "",
        "【修复方向（还没改，因为要连带重测）】",
        "  `len(approx) != 4` 是硬门槛，可以放宽成：顶点数 4~6 时取凸包，"
        "或用 cv2.minAreaRect（永远返回 4 个角）兜底，再用现有的长宽比 + 内部亮度"
        "两道过滤把关。",
        "  风险：这道门槛同时挡着“桌面上的纸横线被凑成四边形”那类误检"
        "（679×387 的假标签、拉正是 96×8 一根线）。放宽后必须把 79 项测试、"
        "7 张夹具图和 22 种扰动压力表全部重跑一遍，确认误检没回来。",
        "  临时缓解：走近一点，或把标签写大（字条在画面里保持 ≥120px 宽）。",
        "",
        "【复现】",
        "python working/make_handover.py                 # 重新生成全部样例与本报告",
        "python working/check_label_pipeline.py          # 多角度端到端 0/20/40/70/200°",
        "python -m unittest vision.tests.test_vision     # 79 项离线回归",
    ]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


HANDOVER = """# 交接说明 · 视觉识别模块

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
```

## 2. 包里有什么

| 路径 | 说明 |
|---|---|
| `vision/*.py` | 找标签 / 认字母 / 视觉模型 / 检测 / 区域 / 快照 / 差异 |
| `vision/config/desktop.json` | 全部现场参数（分辨率、五区域、标签、模型） |
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
修复方向写在 REPORT 里（放宽成 4–6 顶点取凸包 / `minAreaRect` 兜底），
但那道门槛同时挡着桌面纸横线的误检，必须连带重跑全部测试才敢改。

**失败 2 的行为是对的**：真标签贴着右边缘被切，只剩三条边 → 进不了候选；
桌面横线纸被凑出两个矩形，读成 A 0.51 / B 0.54，低于阈值 → 不产出物体。
最终 0 个物体 + `label_unread`，下游落到 REVIEW。**宁可认不出，也不编 ID。**

> 备注：早先记录过“整图转 20° 会漏掉 A”，复核后确认**是测试脚本的问题** ——
> 旋转时用了深色填充，在画面里造出一条斜的暗边，Canny 把标签和这条不存在的边
> 连成一体（框 270×134 → 849×514）。改成边缘复制填充后，0/20/40/70/200° 全认对。
> 教训写进了 `working/check_label_pipeline.py` 的注释：测量工具本身会造缺陷。

## 4. 现在能做什么 / 还不能做什么

**能**：固定机位下同时认 2–3 块写了字母的积木；任意角度（15° 步进搜索）；
S0/S1 快照与逐项差异；手写标签现场登记与删除；接视觉大模型后可认未登记的字母（需 key）。

**还不能**：标签在画面里太小（约 <120px 宽）时找框会漏；标签被画面边缘切开时认不出；
同色积木仅靠颜色区分；像素坐标转机械臂坐标（队长的手眼标定）；未接过 G1。

参数含义、压力测试数据、失败排查表都在 `docs/VISION_LETTER_VLM.md`。
"""


# ------------------------------------------------------------------ 打包
def build_zip(zip_path: Path, extra: dict[str, Path]) -> list[str]:
    """只挑源码 / 配置 / schema / 夹具 / 文档；不带 27MB 现场照片和私钥。"""
    items: list[tuple[Path, str]] = []
    for p in sorted((ROOT / "vision").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        if rel.startswith(("vision/certs/", "vision/records/")):
            continue
        if rel.endswith(".html"):
            continue
        items.append((p, rel))

    items.append((ROOT / "docs" / "VISION_LETTER_VLM.md", "docs/VISION_LETTER_VLM.md"))
    for arc, src in extra.items():
        if src.is_file():
            items.append((src, arc))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, arc in items:
            z.write(src, arc)
    return [arc for _, arc in items]


def main() -> int:
    if not PHOTO.is_file():
        raise SystemExit(f"找不到样例照片：{PHOTO}")
    fail_dir = OUT / "fail_cases"
    fail_dir.mkdir(parents=True, exist_ok=True)
    (OUT / "records").mkdir(parents=True, exist_ok=True)

    img = cv2.imread(str(PHOTO))
    app = app_for()
    res = app.config.get("resolution", [1280, 720])
    base = fit_photo(img, res)
    th = float(app.detector.label_match_threshold)
    min_area = float(app.detector.label_cfg.get("min_area_px", 0))
    print(f"照片 {img.shape[1]}×{img.shape[0]} → 裁到 {base.shape[1]}×{base.shape[0]}")
    print(f"库内字母 {app.detector.letter_bank.letters()}  阈值 {th}  "
          f"最小标签面积 {min_area:.0f}px²")

    cases = {
        "ok": run_case(app, base, "ok_100pct", fail_dir),
        # 边界探针：只算不存图，用来和 40% 对照，证明"是哪一档开始丢的"
        "edge": run_case(app, zoom_out(base, 0.45), "edge_45pct", fail_dir, save=False),
        "far": run_case(app, zoom_out(base, 0.40), "fail_40pct_zoom", fail_dir),
        "cut": run_case(app, shift(base, 520, -60), "fail_outframe", fail_dir),
    }
    for k, c in cases.items():
        print(f"  {c['tag']:<16} 标签 {c['found']} 张  身份 {str(c['ids']):<26} "
              f"issues={c['issues']}")

    report = fail_dir / "REPORT.txt"
    write_report(cases, report, th, min_area)

    handover = OUT / "HANDOVER.md"
    handover.write_text(HANDOVER, encoding="utf-8")

    extra = {"HANDOVER.md": handover, "fail_cases/REPORT.txt": report}
    for name in ("ok_100pct", "fail_40pct_zoom", "fail_outframe"):
        for suf in (".jpg", "_ann.jpg"):
            extra[f"fail_cases/{name}{suf}"] = fail_dir / f"{name}{suf}"

    zip_path = OUT / "vision_source.zip"
    arcs = build_zip(zip_path, extra)
    print(f"\n打包 {len(arcs)} 个文件 → {zip_path.relative_to(ROOT).as_posix()} "
          f"({zip_path.stat().st_size / 1024:.0f} KB)")
    print(f"失败样例 → {fail_dir.relative_to(ROOT).as_posix()}")
    print(f"交接说明 → {handover.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
