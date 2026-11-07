"""识别预览 · 抓取工作区

把腕部深度相机当前看到的东西画成网页：高程热力图 + 检出的凸起 + 坐标。
只读预览，不通过网页读取请求触发运动。

    python3 tools/preview_server.py --port 8420
"""
from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "robot_exec.py"
HOST = "galbot@192.168.167.46"
SOCK = "/tmp/kitt-console-live.sock"
HIGH = 1.06
HOVER = 0.965


class Robot:
    """一条常驻 SSH 连接，串行化所有命令。

    注意：执行器进程会独占腕部深度相机。不用的时候必须 close()，
    否则抓取脚本拿不到图。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc = None
        self.seq = 0
        self.boot = None
        self.last_use = 0.0

    def _start(self) -> None:
        from exec_transport import connect
        self.proc, self.boot = connect(allow_motion=False)
        self.last_use = time.time()

    def call(self, obj: dict, max_lines: int = 800) -> dict:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.last_use = time.time()
            from exec_transport import call
            return call(self.proc, obj)[1]

    def close(self) -> None:
        """退出执行器，让出深度相机。"""
        with self.lock:
            p, self.proc = self.proc, None
        if p is None:
            return
        try:
            from exec_transport import close
            close(p)
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


ROBOT = Robot()

IDLE_RELEASE_S = 90.0     # 页面关掉后多久自动释放相机


def _idle_watch():
    while True:
        time.sleep(10)
        if ROBOT.alive() and time.time() - ROBOT.last_use > IDLE_RELEASE_S:
            ROBOT.close()


threading.Thread(target=_idle_watch, daemon=True).start()


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>识别预览 · 抓取工作区</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;background:#0d1117;color:#e6edf3;
       font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
  header{padding:14px 20px;border-bottom:1px solid #21262d;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0;font-weight:600}
  .pill{background:#161b22;border:1px solid #30363d;border-radius:999px;padding:2px 10px;font-size:12px;color:#8b949e}
  main{display:grid;grid-template-columns:minmax(340px,520px) 1fr;gap:18px;padding:18px 20px}
  canvas{width:100%;height:auto;background:#000;border-radius:10px;border:1px solid #21262d;display:block;cursor:crosshair}
  aside{min-width:0}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
  th{color:#8b949e;font-weight:500}
  tr.sel{background:#1f6feb33}
  button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;
         padding:7px 14px;cursor:pointer;font-size:13px}
  button:hover{background:#30363d}
  button.primary{background:#238636;border-color:#2ea043}
  button:disabled{opacity:.45;cursor:not-allowed}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
  .k{color:#8b949e}
  .big{font-size:20px;font-variant-numeric:tabular-nums}
  #legend{display:flex;gap:6px;align-items:center;margin-top:8px;font-size:12px;color:#8b949e}
  .bar{height:10px;flex:1;border-radius:3px;
       background:linear-gradient(90deg,#0b1a2b,#1b4f72,#1e8449,#b7950b,#c0392b,#f5f5f5)}
  #msg{font-size:12px;color:#f0883e;min-height:18px}
  /* 开关 */
  .sw{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none;font-size:13px}
  .sw input{display:none}
  .sw i{width:40px;height:22px;border-radius:999px;background:#30363d;position:relative;
        transition:background .15s;flex:none}
  .sw i::after{content:'';position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;
               background:#8b949e;transition:left .15s,background .15s}
  .sw input:checked + i{background:#238636}
  .sw input:checked + i::after{left:21px;background:#fff}
  .sw b{font-weight:500}
  #dot{width:8px;height:8px;border-radius:50%;background:#484f58;display:inline-block}
  #dot.on{background:#3fb950;box-shadow:0 0 0 3px #3fb95033}
  #pause{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
         background:#0d1117cc;border-radius:10px;font-size:15px;color:#8b949e;letter-spacing:1px}
  .wrap{position:relative}
</style>
</head>
<body>
<header>
  <h1>腕部深度相机 · 近距离识别</h1>
  <label class="sw" title="关掉会立刻释放相机，方便其他脚本使用">
    <input type="checkbox" id="live" checked><i></i><b>实时识别</b>
  </label>
  <span class="pill"><span id="dot"></span> <span id="livetxt">启动中</span></span>
  <span class="pill" id="pose">读取中…</span>
  <span class="pill" id="meta"></span>
</header>

<main>
  <div class="wrap">
    <canvas id="c" width="560" height="560"></canvas>
    <div id="pause">已暂停 · 相机已释放</div>
    <div id="legend">
      <span>低</span><div class="bar"></div><span>高</span>
      <span id="maxh" style="margin-left:8px"></span>
    </div>
    <div class="row">
      <button id="refresh" class="primary">立刻识别一次</button>
      <button id="snap">存一张原始深度图</button>
      <span class="k" id="hint2" style="font-size:12px">只读预览：网页不发运动指令</span>
    </div>
    <div id="msg"></div>
  </div>

  <aside>
    <div class="row">
      <div><div class="k">桌面高度</div><div class="big" id="tbl">—</div></div>
      <div style="margin-left:24px"><div class="k">深度有效</div><div class="big" id="valid">—</div></div>
      <div style="margin-left:24px"><div class="k">检出凸起</div><div class="big" id="npk">—</div></div>
    </div>
    <table>
      <thead><tr><th>#</th><th>位置 x, y (m)</th><th>高出桌面</th><th>偏轴</th><th>点数</th></tr></thead>
      <tbody id="rows"><tr><td colspan="5" class="k">点"重新识别"</td></tr></tbody>
    </table>
    <p class="k" id="hint" style="margin-top:14px">
      画布：横轴 = x（向前），纵轴 = y（向左），每格 5 mm。灰框 = 左臂可达区。
      点画布可查看该点坐标。
    </p>
    <img id="snapimg" style="width:100%;border-radius:8px;margin-top:10px;display:none">
  </aside>
</main>

<script>
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
let LAST = null, SEL = -1, CLICK = null;

const C = [[0,'#0b1a2b'],[8,'#123b5c'],[16,'#1b6f8c'],[28,'#1e8449'],[45,'#b7950b'],[65,'#c0392b'],[90,'#f5f5f5']];
function color(h){
  let c = C[0][1];
  for(const [t,col] of C){ if(h >= t) c = col; }
  return c;
}

function draw(){
  const d = LAST;
  ctx.fillStyle = '#05070a'; ctx.fillRect(0,0,cv.width,cv.height);
  if(!d || !d.grid){ return; }
  const n = d.grid.length, cell = cv.width / n;
  // 高程热力图
  for(let y=0;y<n;y++){
    for(let x=0;x<n;x++){
      const h = d.grid[y][x];
      ctx.fillStyle = h <= 0 ? '#0a0d12' : color(h);
      ctx.fillRect(x*cell, y*cell, Math.ceil(cell), Math.ceil(cell));
    }
  }
  // 可达区（x 0.58-0.87, y -0.05-0.30）
  const cx0 = d.origin[0], cy0 = d.origin[1], res = d.grid_res_cm/100;
  const toPx = (x,y) => [ (x-cx0)/res*cell, (y-cy0)/res*cell ];
  ctx.strokeStyle = '#6e7681'; ctx.lineWidth = 1.5; ctx.setLineDash([5,4]);
  const [ax,ay] = toPx(0.58,-0.05), [bx,by] = toPx(0.87,0.30);
  ctx.strokeRect(ax,ay,bx-ax,by-ay); ctx.setLineDash([]);
  ctx.fillStyle = '#8b949e'; ctx.font = '11px sans-serif';
  ctx.fillText('可达区', ax+4, ay+14);

  // 凸起
  (d.peaks||[]).forEach((p,i)=>{
    const [px,py] = toPx(p.xy[0], p.xy[1]);
    const sel = i === SEL;
    ctx.beginPath(); ctx.arc(px,py,sel?11:8,0,7);
    ctx.strokeStyle = sel ? '#58a6ff' : '#f0f6fc'; ctx.lineWidth = sel?3:2; ctx.stroke();
    ctx.fillStyle = sel ? '#58a6ff' : '#f0f6fc'; ctx.font = 'bold 12px sans-serif';
    ctx.fillText('#'+(i+1)+' '+p.h_cm.toFixed(1)+'cm', px+12, py-6);
  });

  // 工具轴
  if(d.mount){
    const [mx,my] = toPx(d.mount[0], d.mount[1]);
    ctx.strokeStyle='#3fb950'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(mx-9,my); ctx.lineTo(mx+9,my);
    ctx.moveTo(mx,my-9); ctx.lineTo(mx,my+9); ctx.stroke();
  }
  // 点击点
  if(CLICK){
    const [qx,qy] = toPx(CLICK[0], CLICK[1]);
    ctx.strokeStyle='#f0883e'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.arc(qx,qy,6,0,7); ctx.stroke();
    ctx.fillStyle='#f0883e'; ctx.font='12px sans-serif';
    ctx.fillText(CLICK[0].toFixed(3)+', '+CLICK[1].toFixed(3), qx+10, qy+16);
  }
}

cv.onclick = (e)=>{
  if(!LAST || !LAST.grid) return;
  const r = cv.getBoundingClientRect();
  const n = LAST.grid.length;
  const x = (e.clientX-r.left)/r.width*n, y = (e.clientY-r.top)/r.height*n;
  const wx = LAST.origin[0] + x/n*(2*LAST.radius), wy = LAST.origin[1] + y/n*(2*LAST.radius);
  CLICK = [wx, wy]; draw();
};

function rows(d){
  const tb = document.getElementById('rows');
  if(!d.peaks || !d.peaks.length){ tb.innerHTML = '<tr><td colspan="5" class="k">没检测到凸起</td></tr>'; return; }
  tb.innerHTML = d.peaks.map((p,i)=>
    `<tr class="${i===SEL?'sel':''}" onclick="SEL=${i};draw();rows(LAST)">
       <td>#${i+1}</td><td>${p.xy[0].toFixed(3)}, ${p.xy[1].toFixed(3)}</td>
       <td>${p.h_cm.toFixed(1)} cm</td><td>${p.dist_axis_cm.toFixed(1)} cm</td><td>${p.n}</td>
     </tr>`).join('');
}

let TIMER = null, LIVE = true, BUSY = false;

function setBadge(on, txt){
  document.getElementById('dot').className = on ? 'on' : '';
  document.getElementById('livetxt').textContent = txt;
  document.getElementById('pause').style.display = on ? 'none' : 'flex';
}

async function scan(){
  if(!LIVE || BUSY) return;
  BUSY = true;
  try{
    const d = await (await fetch('/api/probe')).json();
    document.getElementById('msg').textContent = d.ok ? '' : (d.error||'识别失败');
    if(d.ok){
      LAST = d; SEL = -1; CLICK = null;
      document.getElementById('tbl').textContent = d.table_z.toFixed(4)+' m';
      document.getElementById('valid').textContent = d.valid_pct.toFixed(0)+'%';
      document.getElementById('npk').textContent = (d.peaks||[]).length;
      document.getElementById('meta').textContent =
        `${d._wall_ms} ms · 相机距 ${d.cam_dist.toFixed(2)} m · ${new Date().toLocaleTimeString()}`;
      let mx = 0; (d.grid||[]).forEach(r=>r.forEach(v=>{ if(v>mx) mx=v; }));
      document.getElementById('maxh').textContent = `最高 ${mx} mm`;
      draw(); rows(d);
    }
  }catch(e){
    document.getElementById('msg').textContent = '请求失败';
  }
  BUSY = false;
}

function setLive(on){
  LIVE = on;
  document.getElementById('live').checked = on;
  if(TIMER){ clearInterval(TIMER); TIMER = null; }
  if(on){
    setBadge(true, '识别中');
    scan();
    TIMER = setInterval(()=>{ if(!document.hidden) scan(); }, 1200);
  }else{
    setBadge(false, '已暂停');
    fetch('/api/live?on=0').catch(()=>{});   // 让服务端释放相机
  }
}

document.getElementById('live').onchange = e => setLive(e.target.checked);
document.getElementById('refresh').onclick = () => { if(!LIVE) setLive(true); else scan(); };
document.getElementById('snap').onclick = async ()=>{
  const d = await (await fetch('/api/snap')).json();
  const img = document.getElementById('snapimg');
  if(d.b64){ img.src = 'data:image/jpeg;base64,'+d.b64; img.style.display='block'; }
  else document.getElementById('msg').textContent = d.error||'取图失败';
};

window.addEventListener('beforeunload', ()=>{ fetch('/api/live?on=0', {keepalive:true}).catch(()=>{}); });
document.addEventListener('visibilitychange', ()=>{
  if(document.hidden && LIVE){ if(TIMER) clearInterval(TIMER); TIMER = null; }
  else if(!document.hidden && LIVE && !TIMER){ TIMER = setInterval(()=>scan(), 1200); }
});

fetch('/api/state').then(r=>r.json()).then(d=>{
  if(d.ok && d.mount) document.getElementById('pose').textContent =
    `腕部 ${d.mount[0].toFixed(3)}, ${d.mount[1].toFixed(3)}, ${d.mount[2].toFixed(3)} m`;
});
setLive(true);
setInterval(()=>{
  fetch('/api/state').then(r=>r.json()).then(d=>{
    if(d.ok && d.mount) document.getElementById('pose').textContent =
      `腕部 ${d.mount[0].toFixed(3)}, ${d.mount[1].toFixed(3)}, ${d.mount[2].toFixed(3)} m`;
  }).catch(()=>{});
}, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                return self._page()
            if self.path.startswith("/api/probe"):
                r = ROBOT.call({"cmd": "probe", "frames": 6, "radius": 0.13, "peaks": 6})
                if r.get("ok"):
                    st = ROBOT.call({"cmd": "state"})
                    r["mount"] = st.get("mount")
                return self._json(r)
            if self.path.startswith("/api/state"):
                return self._json(ROBOT.call({"cmd": "state"}))
            if self.path.startswith(("/api/up", "/api/hover")):
                return self._json({"ok": False, "error": "识别预览为只读；运动需在现场确认后分步执行"}, 405)
            if self.path.startswith("/api/snap"):
                return self._json(ROBOT.call({"cmd": "depthjpeg", "frames": 3, "scale": 2, "q": 70}))
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            return self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8420)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("preview on http://127.0.0.1:%d/" % a.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
