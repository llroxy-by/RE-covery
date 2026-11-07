"""生成一份"静态版观察台"HTML：把实时页面该有的样子（画面+掩膜+目标表+差异表）
用真实录制数据渲染成一个可以离线打开的文件，方便截图/发给队友/塞进 PPT。

    python working/gen_web_preview.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision.detector import Detector
from vision.main import VisionApp
from vision.sources import MockSource
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "vision" / "records"
OUT = ROOT / "vision" / "docs" / "preview_sample.html"


def b64(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def main() -> None:
    cfg = json.loads((ROOT / "vision/config/desktop.json").read_text(encoding="utf-8"))
    app = VisionApp(SimpleNamespace(
        camera=0, config=str(ROOT / "vision/config/desktop.json"), mode="mock",
        output=str(RECORDS), min_conf=0.8, mock_cycle=False,
        skip_image_check=True, allow_frozen=False, url=None))

    # 用 mock 的 moved 场景造一份"当前帧"数据（与命令行演示一致）
    src = MockSource(cfg["zones"], cfg["resolution"], "moved")
    app._source = src  # annotate() 里要判断来源类型（mock/真实）
    for _ in range(6):
        frame = src.read()
        objects, quality_ok, issues = app.observe(frame)
    ann = app.annotate(frame, objects, issues)
    masks = app.mask_mosaic(frame)
    cv2.imwrite(str(ROOT / "working/_preview_ann.jpg"), ann, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    cv2.imwrite(str(ROOT / "working/_preview_masks.jpg"), masks, [int(cv2.IMWRITE_JPEG_QUALITY), 78])

    diff_path = sorted(RECORDS.glob("diff_*.json"))[-1]
    diff = json.loads(diff_path.read_text(encoding="utf-8"))

    rows = "".join(
        f"<tr><td><code>{o['object_id']}</code></td><td>{o['zone']}</td>"
        f"<td>{o['visibility']}</td><td>{o['confidence']:.2f}</td></tr>"
        for o in objects)
    drows = "".join(
        f"<tr><td><code>{d['object_id']}</code></td>"
        f"<td class=\"{'vis' if d['status']=='unchanged' else ('unc' if d['status']=='uncertain' else 'mis')}\">"
        f"{d['status']}</td><td>{d['from_zone']} → {d['to_zone']}</td></tr>"
        for d in diff["differences"])

    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>RE-covery 观察台（静态样例）</title>
<style>
 :root {{ --bg:#14161a; --panel:#1d2026; --line:#2c3138; --fg:#e8eaed; --dim:#98a0ac;
          --ok:#3ddc84; --warn:#ffb020; --bad:#ff5c5c; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
 header {{ display:flex; align-items:center; gap:14px; padding:12px 20px;
           border-bottom:1px solid var(--line); background:var(--panel); }}
 h1 {{ font-size:16px; margin:0; }}
 .badge {{ padding:2px 8px; border-radius:10px; font-size:12px; background:#3a341d; color:var(--warn); }}
 main {{ display:grid; grid-template-columns:minmax(0,2.2fr) minmax(280px,1fr);
         gap:16px; padding:16px 20px; }}
 .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }}
 .card h2 {{ font-size:12px; margin:0 0 8px; color:var(--dim); letter-spacing:.04em; }}
 img {{ width:100%; display:block; border-radius:6px; background:#000; }}
 table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
 th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }}
 th {{ color:var(--dim); }}
 code {{ font-family:ui-monospace,Consolas,monospace; }}
 .vis {{ color:var(--ok); }} .mis {{ color:var(--warn); }} .unc {{ color:var(--bad); }}
 .muted {{ color:var(--dim); font-size:12.5px; }}
</style></head><body>
<header><h1>RE-covery · 视觉观察台</h1>
<span class="badge">静态样例（数据来自 mock:moved）</span>
<span class="muted">实时版：python -m vision.web --camera 1</span></header>
<main>
 <section>
  <div class="card" style="margin-bottom:16px">
   <h2>实时画面（带区域 / ID / 置信度）</h2>
   <img src="{b64(ROOT / 'working/_preview_ann.jpg')}" alt="annotated">
  </div>
  <div class="card">
   <h2>HSV 掩膜（白 = 被阈值选中）</h2>
   <img src="{b64(ROOT / 'working/_preview_masks.jpg')}" alt="masks">
   <p class="muted">掩膜全黑 = 阈值没选中；有白块没框 = 面积过滤太狠；边缘毛刺 = 光照不均。</p>
  </div>
 </section>
 <aside>
  <div class="card" style="margin-bottom:16px">
   <h2>目标（当前帧）</h2>
   <table><thead><tr><th>object_id</th><th>zone</th><th>vis</th><th>conf</th></tr></thead>
   <tbody>{rows}</tbody></table>
  </div>
  <div class="card">
   <h2>差异 S0 → S1</h2>
   <p class="muted">整体：<b class="mis">{diff['status']}</b> ·
      判据：<b>{'REVIEW'}</b></p>
   <table><thead><tr><th>object_id</th><th>status</th><th>from→to</th></tr></thead>
   <tbody>{drows}</tbody></table>
   <p class="muted" style="margin-top:8px">来源：{diff_path.name}</p>
  </div>
 </aside>
</main></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
