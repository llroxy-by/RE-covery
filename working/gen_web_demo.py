"""生成"动态演示页"：红块从 A 平滑移动到 B，逐帧跑真实检测/判定并叠加，
最后给出 S0→S1 的差异结论。产物是一个自包含 HTML（图片内嵌 base64，离线可播）。

    python working/gen_web_demo.py
    → vision/docs/demo_animated.html
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision.main import VisionApp          # noqa: E402
from vision.sources import Frame, utc_now_iso  # noqa: E402
from vision import diff as diff_mod         # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "vision/config/desktop.json"
OUT = ROOT / "vision/docs/demo_animated.html"

BLOCK = 96
COLOR_BGR = {"red": (0, 0, 220), "green": (0, 180, 0), "blue": (220, 80, 0)}
STEPS = 12
HOLD = 7  # 停稳后要留够连续 5 帧，否则稳定性判定还没完成


def base_scene(cfg):
    w, h = cfg["resolution"]
    img = np.full((h, w, 3), 105, np.uint8)
    for name, poly in cfg["zones"].items():
        arr = np.asarray(poly, np.int32)
        cv2.fillPoly(img, [arr], (128, 128, 128))
        cv2.polylines(img, [arr], True, (70, 70, 70), 2)
    return img


def centroid(cfg, zone):
    arr = np.asarray(cfg["zones"][zone], np.float32)
    return float(arr[:, 0].mean()), float(arr[:, 1].mean())


def draw_block(img, cx, cy, bgr):
    x0, y0 = int(cx - BLOCK / 2), int(cy - BLOCK / 2)
    cv2.rectangle(img, (x0, y0), (x0 + BLOCK, y0 + BLOCK), bgr, -1)


def bezier(p0, c, p1, t):
    mt = 1 - t
    return (mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0],
            mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1])


def b64(img, quality=68, size=(960, 540)):
    small = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("编码失败")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def main() -> None:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    app = VisionApp(SimpleNamespace(
        camera=0, config=str(CFG_PATH), mode="mock", output=str(ROOT / "vision/records"),
        min_conf=0.8, mock_cycle=False, skip_image_check=True, allow_frozen=False, url=None))
    app._source = None  # annotate() 里 isinstance 检查对 None 安全

    a_c = centroid(cfg, "A")
    b_c = centroid(cfg, "B")
    target = (b_c[0], b_c[1] + 90)                     # 落到 B 里靠下的位置，避开绿块
    ctrl = ((a_c[0] + target[0]) / 2, (a_c[1] + target[1]) / 2 + 150)  # 弧线，绕开绿块

    # 预跑几帧，让稳定性判定攒够连续帧；否则 S0 里全是 zone_unstable，后面全被判 uncertain
    frames, captions = [], []
    total = STEPS + HOLD
    pre = base_scene(cfg)
    draw_block(pre, b_c[0], b_c[1], COLOR_BGR["green"])
    draw_block(pre, *centroid(cfg, "C"), COLOR_BGR["blue"])
    draw_block(pre, a_c[0], a_c[1], COLOR_BGR["red"])
    for k in range(int(cfg.get("stability_frames", 5))):
        app.observe(Frame(image=pre.copy(), captured_at=utc_now_iso(),
                          source="demo:animated", mode="mock", index=k))
    baseline = app.scene

    for i in range(total):
        t = min(1.0, i / STEPS)
        img = base_scene(cfg)
        draw_block(img, b_c[0], b_c[1], COLOR_BGR["green"])
        draw_block(img, *centroid(cfg, "C"), COLOR_BGR["blue"])
        rx, ry = bezier(a_c, ctrl, target, t)
        draw_block(img, rx, ry, COLOR_BGR["red"])

        frame = Frame(image=img, captured_at=utc_now_iso(), source="demo:animated",
                      mode="mock", index=i + 1)
        objects, quality_ok, issues = app.observe(frame)
        frames.append(b64(app.annotate(frame, objects, issues)))

        red = next(o for o in objects if o["object_id"] == "block_red_01")
        why = ",".join(red["reasons"]) or "visible"
        if i == 0:
            captions.append("① 建立 S0 基准：红块在 A，绿在 B，蓝在 C —— 全部 unchanged")
        elif i == total - 1:
            result = diff_mod.compare(baseline, app.scene, check_images=False)
            red = [d for d in result["differences"] if d["object_id"] == "block_red_01"][0]
            captions.append(f"⑤ 最终判定：{result['status']} · block_red_01 "
                            f"{red['from_zone']}→{red['to_zone']} · 其余 unchanged")
        elif red["zone"] == "B" and not red["reasons"]:
            captions.append("④ 已稳定在 B：连续 5 帧区域一致，归属被接受")
        elif t < 1.0:
            captions.append(f"② 红块从 A 移向 B（第 {i}/{STEPS} 帧）：{why} → uncertain，这是正确行为")
        else:
            captions.append(f"③ 落定在 B，还在攒稳定性（{why}）：未稳定不确认")

    payload = json.dumps({"frames": frames, "captions": captions}, ensure_ascii=False)
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>RE-covery 动态演示</title>
<style>
 :root { --bg:#14161a; --panel:#1d2026; --line:#2c3138; --fg:#e8eaed; --dim:#98a0ac; --ok:#3ddc84; --warn:#ffb020; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--fg);
        font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
 header { display:flex; align-items:center; gap:14px; padding:12px 20px;
          border-bottom:1px solid var(--line); background:var(--panel); }
 h1 { font-size:16px; margin:0; }
 .badge { padding:2px 8px; border-radius:10px; font-size:12px; background:#1d3a24; color:var(--ok); }
 main { padding:16px 20px; max-width:1080px; margin:0 auto; }
 .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }
 img { width:100%; display:block; border-radius:6px; background:#000; }
 .bar { display:flex; align-items:center; gap:12px; margin-top:10px; }
 button { background:#262b33; color:var(--fg); border:1px solid var(--line);
          border-radius:7px; padding:7px 14px; font-size:13px; cursor:pointer; }
 button:hover { border-color:#4aa8ff; color:#4aa8ff; }
 .cap { color:var(--fg); font-size:13.5px; min-height:22px; }
 .dim { color:var(--dim); font-size:12.5px; }
 .track { flex:1; height:4px; background:#2c3138; border-radius:2px; overflow:hidden; }
 .fill { height:100%; width:0; background:#4aa8ff; transition:width .2s linear; }
</style></head><body>
<header><h1>RE-covery · 动态演示</h1>
<span class="badge">mock 合成画面 · 逐帧真实检测</span>
<span class="dim">真实相机版：python -m vision.web --camera 1</span></header>
<main>
 <div class="card">
  <img id="shot" alt="frame">
  <div class="bar">
   <button id="toggle">暂停</button>
   <div class="track"><div class="fill" id="fill"></div></div>
   <span class="dim" id="idx">1 / 1</span>
  </div>
  <p class="cap" id="cap"></p>
  <p class="dim">每帧都跑了一遍真实的 HSV 检测 + 区域归属 + 稳定性判定，叠加的是程序真实输出，
  不是手工画的示意图。移动过程中判 <code>uncertain</code>、稳定后才判 <code>misplaced</code>，
  正是"不稳定不确认"这条规则的现场表现。</p>
 </div>
</main>
<script>
const data = __PAYLOAD__;
let i = 0, playing = true;
const shot = document.getElementById('shot'), cap = document.getElementById('cap');
const idx = document.getElementById('idx'), fill = document.getElementById('fill');
function render() {
  shot.src = data.frames[i];
  cap.textContent = data.captions[i];
  idx.textContent = (i + 1) + ' / ' + data.frames.length;
  fill.style.width = ((i + 1) / data.frames.length * 100) + '%';
}
function step() { if (!playing) return; i = (i + 1) % data.frames.length; render(); }
render();
setInterval(step, 900);
document.getElementById('toggle').onclick = (e) => {
  playing = !playing;
  e.target.textContent = playing ? '暂停' : '播放';
};
</script></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html.replace("__PAYLOAD__", payload), encoding="utf-8")
    print(f"wrote {OUT}  ({OUT.stat().st_size // 1024} KB, {len(frames)} 帧)")


if __name__ == "__main__":
    main()
