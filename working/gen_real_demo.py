"""用真实采集的两张照片生成演示页（单文件 HTML，图片内嵌，可直接嵌 PPT）。

数据现场重算，不读旧 JSON —— 否则改了判定规则演示页还是老结论。
"""
import base64
import glob
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision import diff as D  # noqa: E402
from vision.detector import StabilityTracker  # noqa: E402
from vision.main import VisionApp  # noqa: E402
from vision.sources import Frame, utc_now_iso  # noqa: E402

D_OUT = Path("vision/docs/demo_real")
D_OUT.mkdir(parents=True, exist_ok=True)
OUT = Path("vision/docs/demo_real.html")
CFG = "vision/config/desktop.json"


def _process(prefix: str, name: str):
    img = cv2.imread(glob.glob(f"vision/records/{prefix}*_raw.jpg")[0])
    app = VisionApp(SimpleNamespace(camera=0, config=CFG, mode="real", output=str(D_OUT),
                                    min_conf=0.8, mock_cycle=False,
                                    skip_image_check=False, allow_frozen=False, url=None))
    app._source = None
    app.tracker = StabilityTracker(1, 1e9)      # 手机单帧拍摄：一张即定稿
    frame = Frame(image=img, captured_at=utc_now_iso(), source="phone:upload",
                  mode="real", index=1)
    objs, ok, issues = app.observe(frame)
    ann = app.annotate(frame, objs, issues)
    cv2.imwrite(str(D_OUT / f"{name}_ann.jpg"), ann, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return app.scene, ok, issues


s0_scene, s0_ok, s0_iss = _process("570c6cba", "s0")
s1_scene, s1_ok, s1_iss = _process("4c336d45", "s1")
diff = D.compare(s0_scene, s1_scene, min_conf=0.8, check_images=False)
verdict = D.gate_verdict(diff)

print(f"S0 quality_ok={s0_ok} issues={s0_iss or '无'}")
for o in s0_scene["objects"]:
    print(f"   {o['object_id']:16s} {o['zone']:6s} conf={o['confidence']:.2f}")
print(f"S1 quality_ok={s1_ok} issues={s1_iss or '无'}")
for o in s1_scene["objects"]:
    print(f"   {o['object_id']:16s} {o['zone']:6s} conf={o['confidence']:.2f}")
print(f"\n整体 status={diff['status']}  verdict={verdict}")
for x in diff["differences"]:
    print(f"   {x['object_id']:16s} {x['status']:10s} {x['from_zone']} -> {x['to_zone']}")

D_OUT.joinpath("data.json").write_text(
    json.dumps({"s0": s0_scene, "s1": s1_scene, "diff": diff, "verdict": verdict},
               ensure_ascii=False, indent=2), encoding="utf-8")

b64 = {n: base64.b64encode((D_OUT / f"{n}_ann.jpg").read_bytes()).decode()
       for n in ("s0", "s1")}
s0 = {o["object_id"]: o for o in s0_scene["objects"]}
s1 = {o["object_id"]: o for o in s1_scene["objects"]}
diffs = diff["differences"]

CN = {"block_red_01": "红色积木", "block_green_01": "绿色积木",
      "A": "A", "B": "B", "C": "C", "D": "D", "TEMP": "TEMP", "unknown": "未知"}


def cn(oid):
    return CN.get(oid, oid)


def zone_of(sc, oid):
    o = sc.get(oid)
    return CN.get(o["zone"], o["zone"]) if o else "—"


def conf_of(sc, oid):
    o = sc.get(oid)
    return f"{o['confidence']:.2f}" if o else "—"


rows = []
for d in diffs:
    cls = {"unchanged": "ok", "misplaced": "warn", "uncertain": "bad"}.get(d["status"], "warn")
    label = {"unchanged": "原位", "misplaced": "被挪动", "uncertain": "不确定"}.get(d["status"], d["status"])
    rows.append(
        f'<tr data-row><td>{cn(d["object_id"])}</td>'
        f'<td>{CN.get(d["from_zone"], d["from_zone"])}</td>'
        f'<td class="arrow">→</td>'
        f'<td>{CN.get(d["to_zone"], d["to_zone"])}</td>'
        f'<td class="{cls}">{label}</td>'
        f'<td class="num">{d["confidence"]:.2f}</td></tr>')
TABLE_ROWS = "\n".join(rows)

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE:BENCH 视觉验收 · 真实采集</title>
<style>
:root{--bg:#0f1115;--panel:#181b21;--line:#282d36;--fg:#e8eaed;--dim:#8f97a3;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff}
*{box-sizing:border-box;margin:0;padding:0}
/* 不能用 align-items:center —— 内容高于视口时会把顶部顶出屏幕且滚不回去 */
body{background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     min-height:100vh;display:flex;justify-content:center;padding:20px}
.wrap{width:100%;max-width:1240px;margin:auto}
.head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.head h1{font-size:21px;font-weight:650;letter-spacing:.3px}
.badge{font-size:12px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.badge.live{color:var(--ok);border-color:#2a5c40;background:#12241a}
.grid{display:grid;grid-template-columns:1.55fr 1fr;gap:16px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.stage{position:relative;aspect-ratio:16/9;border:1px solid var(--line);border-radius:12px;
       overflow:hidden;background:#000}
/* 容器定高 + 图片全部绝对定位：两张图才能交叉淡入。
   之前让 .on 变 relative，结果两张都被塞进文档流，上下堆叠。 */
.stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
           opacity:0;transition:opacity .55s ease}
.stage img.on{opacity:1}
.tag{position:absolute;right:12px;top:12px;z-index:3;font-size:12.5px;padding:4px 10px;
     border-radius:6px;background:#0009;backdrop-filter:blur(4px);border:1px solid #ffffff22}
.side{display:flex;flex-direction:column;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.step{display:flex;gap:11px;align-items:flex-start;padding:9px 0;border-bottom:1px solid #ffffff0a;
      opacity:.32;transition:opacity .4s}
.step:last-child{border-bottom:0}
.step.on{opacity:1}
.dot{flex:0 0 22px;height:22px;border-radius:50%;border:1px solid var(--line);display:flex;
     align-items:center;justify-content:center;font-size:12px;color:var(--dim);margin-top:1px}
.step.on .dot{background:var(--blue);color:#04121f;border-color:var(--blue);font-weight:700}
.step b{font-weight:600;font-size:14px}
.step p{font-size:12.5px;color:var(--dim);margin-top:1px}
.zone-chg{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.zone-chg em{font-style:normal;color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:12px;padding:6px 8px;
   border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid #ffffff0a}
td.num{font-family:ui-monospace,Consolas,monospace;color:var(--dim)}
td.arrow{color:var(--dim);text-align:center;width:26px}
td.ok{color:var(--ok)}td.warn{color:var(--warn)}td.bad{color:var(--bad)}
tbody{opacity:.45;transition:opacity .5s}
tbody.hot{opacity:1}
.verdict{display:flex;align-items:center;gap:11px;padding:12px 14px;border-radius:10px;
         background:#2a2110;border:1px solid #6b5424;opacity:0;transition:opacity .5s}
.verdict.on{opacity:1}
.verdict.v-ok{background:#0f2a1c;border-color:#2a5c40}
.verdict.v-bad{background:#2a1214;border-color:#6b2426}
.verdict .big{font-size:16px;font-weight:700;color:var(--warn);letter-spacing:.4px}
.verdict.v-ok .big{color:var(--ok)}
.verdict.v-bad .big{color:var(--bad)}
.verdict .why{font-size:12.5px;color:var(--dim)}
.foot{display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap}
.btn{background:#1e2634;border:1px solid #33507a;color:var(--fg);padding:7px 15px;
     border-radius:8px;font-size:13px;cursor:pointer}
.btn:hover{background:#26324a}
.bar{flex:1;height:3px;background:#ffffff12;border-radius:3px;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;width:0;background:var(--blue);transition:width .5s linear}
.meta{font-size:12px;color:var(--dim);font-family:ui-monospace,Consolas,monospace}
</style></head><body><div class="wrap">

<div class="head">
  <h1>RE:BENCH · 桌面状态验收</h1>
  <span class="badge live">真实采集</span>
  <span class="meta">mode=real · source=phone:upload · 1280×720 · calib=desktop-v1</span>
</div>

<div class="grid">
  <div class="stage">
    <span class="tag" id="tag">S0 基准</span>
    <img id="im0" class="on" src="data:image/jpeg;base64,__S0__" alt="S0">
    <img id="im1" src="data:image/jpeg;base64,__S1__" alt="S1">
  </div>

  <div class="side">
    <div class="card" id="steps">
      <div class="step on" data-step="0"><span class="dot">1</span><div>
        <b>建立基准 S0</b>
        <p class="zone-chg">红色 <em>D</em> · 绿色 <em>TEMP</em> · 置信度 0.95</p></div></div>
      <div class="step" data-step="1"><span class="dot">2</span><div>
        <b>把两块左右对调</b>
        <p class="zone-chg">红色 <em>TEMP</em> · 绿色 <em>D</em></p></div></div>
      <div class="step" data-step="2"><span class="dot">3</span><div>
        <b>系统判定</b>
        <p>逐块比较 S0 与 S1，全部落入其他分区</p></div></div>
    </div>

    <div class="card">
      <table>
        <thead><tr><th>目标</th><th>基准</th><th></th><th>当前</th><th>判定</th><th>conf</th></tr></thead>
        <tbody>__ROWS__</tbody>
      </table>
    </div>

    <div class="verdict v-__VCLASS__" id="vd">
      <div>
        <div class="big">__VERDICT__</div>
        <div class="why">__WHY__</div>
      </div>
    </div>
  </div>
</div>

<div class="foot">
  <button class="btn" id="play">暂停</button>
  <div class="bar"><i id="prog"></i></div>
  <span class="meta" id="count">1 / 3</span>
</div>

</div>
<script>
const steps=[...document.querySelectorAll('.step')];
const rows=[...document.querySelectorAll('tr[data-row]')];
const imgs=[document.getElementById('im0'),document.getElementById('im1')];
const tag=document.getElementById('tag');
const vd=document.getElementById('vd');
const prog=document.getElementById('prog');
const count=document.getElementById('count');
const btn=document.getElementById('play');
const TAGS=['S0 基准','S1 对调后','差异结论'];
let cur=0,timer=null,paused=false;
const DUR=3000;

function show(i){
  cur=i;
  imgs[0].classList.toggle('on',i===0);
  imgs[1].classList.toggle('on',i>0);
  tag.textContent=TAGS[i];
  steps.forEach((s,k)=>s.classList.toggle('on',k===i));
  document.querySelector('tbody').classList.toggle('hot',i>=2);
  vd.classList.toggle('on',i>=2);
  count.textContent=(i+1)+' / 3';
  prog.style.transition='none';prog.style.width='0';
  requestAnimationFrame(()=>{prog.style.transition='width '+DUR+'ms linear';prog.style.width='100%';});
}
function next(){ if(paused) return; show((cur+1)%3); }
function start(){ clearInterval(timer); timer=setInterval(next,DUR); }
btn.onclick=()=>{paused=!paused;btn.textContent=paused?'继续':'暂停';
  if(paused){clearInterval(timer);prog.style.transition='none';prog.style.width='0';}else{show(cur);start();}};
show(0);start();
</script></body></html>"""

VCLASS = {"READY": "ok", "NOT_READY": "bad"}.get(verdict, "warn")
WHY = {
    "READY": "所有目标都在原位，可以开始下一轮实验",
    "NOT_READY": "明确定位到目标已离开原位 → 不可开始下一轮实验",
}.get(verdict, "存在无法判定的目标，需要人工复核")

html = (HTML.replace("__S0__", b64["s0"])
            .replace("__S1__", b64["s1"])
            .replace("__ROWS__", TABLE_ROWS)
            .replace("__VCLASS__", VCLASS)
            .replace("__WHY__", WHY)
            .replace("__VERDICT__", verdict))
OUT.write_text(html, encoding="utf-8")
print(f"\n已生成 {OUT}  ({OUT.stat().st_size/1024:.0f} KB)")
