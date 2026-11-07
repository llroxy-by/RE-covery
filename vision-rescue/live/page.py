"""实时页面。所有渲染都在浏览器本地完成，服务端只回坐标。"""

PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE:BENCH · 实时</title>
<style>
:root{--bg:#0b0d11;--panel:#151920;--line:#252b35;--fg:#e8ebf0;--dim:#8b929e;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;
      padding:14px;height:100vh;max-width:1700px;margin:0 auto}
.stage{display:flex;flex-direction:column;gap:10px;min-width:0}
.bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h1{font-size:15px;font-weight:650;letter-spacing:.3px}
.pill{font-size:11.5px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);
      color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.pill.ok{color:var(--ok);border-color:#245c3d}
.pill.bad{color:var(--bad);border-color:#6b2426}
.pill.warn{color:var(--warn);border-color:#5c4520}
.sp{flex:1}
.view{position:relative;flex:1;min-height:0;display:flex;align-items:center;justify-content:center}
#frame{position:relative;border:1px solid var(--line);border-radius:14px;overflow:hidden;
       background:#000;width:100%;aspect-ratio:16/9}
#v{width:100%;height:100%;object-fit:cover;display:block}
#cv{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.msg{position:absolute;left:12px;bottom:12px;right:12px;font-size:13px;padding:7px 11px;
     border-radius:9px;background:#000000b0;color:#cfd6e2;backdrop-filter:blur(6px);display:none}
.msg.on{display:block}
.msg.bad{color:#ffd7d7;background:#5c1c20cc}
.panel{display:flex;flex-direction:column;gap:10px;overflow:auto}
.card{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:11px 12px}
.card h2{font-size:11.5px;color:var(--dim);font-weight:600;letter-spacing:.4px;margin-bottom:8px}
.read{font-size:26px;font-weight:700;letter-spacing:4px;font-variant-numeric:tabular-nums;
      min-height:38px;color:#d8f5e3;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.read .q{color:#7b8391;letter-spacing:1px;font-size:14px;font-weight:500}
.detail{font-size:11.5px;color:var(--dim);margin-top:4px;font-family:ui-monospace,Consolas,monospace;
        word-break:break-all;line-height:1.65}
.row{display:flex;gap:8px}
button{flex:1;padding:11px 8px;border-radius:10px;border:1px solid #2f4568;background:#18202e;
       color:var(--fg);font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover:not(:disabled){background:#1d2839}
button.primary{background:#173f28;border-color:#2c7047;color:#c8ffdd}
button.primary:hover:not(:disabled){background:#1c4d31}
button:disabled{opacity:.38;cursor:default}
input,select{padding:10px;border-radius:10px;border:1px solid var(--line);background:#0f131a;
       color:var(--fg);font-size:14px;font-family:inherit}
input#letter{flex:1;min-width:0;text-align:center;text-transform:uppercase;letter-spacing:2px}
.hint{font-size:11.5px;color:#6f7783;margin-top:6px;line-height:1.6}
.res{display:none;border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:11px 12px}
.res.on{display:block}
.res h3{font-size:13px;margin-bottom:6px}
.verdict{font-weight:700;font-size:13px;margin-bottom:6px}
.verdict.READY{color:var(--ok)}.verdict.NOT_READY{color:var(--bad)}.verdict.REVIEW{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{padding:5px 3px;border-bottom:1px solid #ffffff0d;text-align:left}
th{color:var(--dim);font-size:10.5px;font-weight:600}
td.m{color:var(--warn)}td.s{color:var(--ok)}
@media(max-width:960px){.wrap{grid-template-columns:minmax(0,1fr);height:auto}
  .view{flex:none}}
</style></head><body>
<div class="wrap">
  <div class="stage">
    <div class="bar">
      <h1>RE:BENCH</h1>
      <span class="pill" id="pCam">摄像头</span>
      <span class="pill" id="pRate">检测 —</span>
      <span class="pill" id="pBase">未建基准</span>
      <span class="sp"></span>
      <label class="pill" style="cursor:pointer;user-select:none">
        <input type="checkbox" id="dbg" style="vertical-align:-1px;margin-right:4px">候选框
      </label>
      <select id="cam" style="max-width:300px;font-size:12px;padding:6px"></select>
    </div>
    <div class="view" id="view">
      <div id="frame">
        <video id="v" playsinline muted autoplay></video>
        <canvas id="cv"></canvas>
        <div class="msg" id="msg"></div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="card">
      <h2>读到</h2>
      <div class="read" id="read">—</div>
      <div class="detail" id="detail"></div>
    </div>
    <div class="row">
      <button id="bAnchor" class="primary">建立基准</button>
      <button id="bCmp" disabled>与基准比较</button>
    </div>
    <div class="res" id="res"></div>
    <div class="card">
      <h2>登记字母（认不准时现场教一次）</h2>
      <div class="row">
        <input id="letter" maxlength="2" placeholder="A" autocomplete="off" autocapitalize="characters">
        <button id="bEnroll" style="flex:0 0 34%">登记</button>
        <button id="bForget" style="flex:0 0 26%">删除</button>
      </div>
      <div class="hint">画面里只留一块积木再登记，否则会存错笔画。登记一次永久有效。</div>
    </div>
    <div class="row">
      <button id="bPeek">读画面中央</button>
      <button id="bSnap">存标注图</button>
    </div>
  </div>
</div>
<script>
const V = document.getElementById('v');
const CV = document.getElementById('cv');
const G = CV.getContext('2d');
// 老一点的 Safari 没有 roundRect，退化成直角，不能因为缺个 API 让整页黑屏
if (!G.roundRect) {
  G.roundRect = function (x, y, w, h) { this.rect(x, y, w, h); return this; };
}
const MSG = document.getElementById('msg');
const P_CAM = document.getElementById('pCam');
const P_RATE = document.getElementById('pRate');
const P_BASE = document.getElementById('pBase');
const READ = document.getElementById('read');
const DETAIL = document.getElementById('detail');
const RES = document.getElementById('res');
const B_ANCHOR = document.getElementById('bAnchor');
const B_CMP = document.getElementById('bCmp');
const SEL = document.getElementById('cam');
const DBG = document.getElementById('dbg');
let lastSize = '';

// 送检画面固定 16:9。显示区也是 16:9 且 object-fit:cover，两边裁法一致，
// 所以服务端坐标 × (显示宽度 / 送检宽度) 就是屏幕坐标，不需要再算裁切偏移。
const SEND_W = 1280, SEND_H = 720;
const WORK = document.createElement('canvas');
WORK.width = SEND_W; WORK.height = SEND_H;
const WCTX = WORK.getContext('2d', {willReadFrequently: false});

let stream = null, running = false, hasBase = false;
let last = {tracks: [], frame: [SEND_W, SEND_H]};   // 最近一次检测结果
let shown = new Map();                             // id -> 正在显示（带缓动）的框
let zones = [], zoneBase = [SEND_W, SEND_H];
let sent = 0, got = 0, msAvg = 0, lastAt = 0, errors = 0, tStart = performance.now();

function msg(text, bad) {
  MSG.textContent = text || '';
  MSG.className = 'msg' + (text ? ' on' : '') + (bad ? ' bad' : '');
}

/* ── 画面：直接放本地摄像头流，30fps，和服务端无关 ─────────────────── */
const PHONEISH = /iphone|ipad|连续互通|continuity|desk ?view|桌面视图/i;
const BUILTIN = /facetime|built-?in|内建|macbook|imac|studio display/i;

async function listCams(active) {
  let devs = [];
  try { devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput'); }
  catch (e) { return; }
  if (!devs.length) return;
  const named = devs.map((d, i) => ({id: d.deviceId, label: d.label || ('摄像头 ' + (i + 1))}));
  named.sort((a, b) => {
    const pa = PHONEISH.test(a.label) ? 1 : 0, pb = PHONEISH.test(b.label) ? 1 : 0;
    if (pa !== pb) return pa - pb;
    const ba = BUILTIN.test(a.label) ? 0 : 1, bb = BUILTIN.test(b.label) ? 0 : 1;
    return ba - bb;
  });
  SEL.innerHTML = named.map(d => '<option value="' + d.id + '">' + d.label
    + (PHONEISH.test(d.label) ? '（手机）' : '') + '</option>').join('');
  if (active) SEL.value = active;
}

async function openCam(id) {
  if (stream) stream.getTracks().forEach(t => t.stop());
  const want = {width: {ideal: 1280}, height: {ideal: 720}, frameRate: {ideal: 30}};
  let s;
  try {
    s = await navigator.mediaDevices.getUserMedia(
      id ? {video: Object.assign({deviceId: {exact: id}}, want), audio: false}
         : {video: want, audio: false});
  } catch (e) {
    if (id) { localStorage.removeItem('rebench.cam'); s = await navigator.mediaDevices.getUserMedia({video: want, audio: false}); }
    else throw e;
  }
  stream = s;
  V.srcObject = s;
  await V.play();
  const id2 = s.getVideoTracks()[0].getSettings().deviceId || '';
  try { localStorage.setItem('rebench.cam', id2); } catch (e) {}
  await listCams(id2);
  return id2;
}

SEL.onchange = async () => {
  try { await openCam(SEL.value); msg('已切换摄像头'); }
  catch (e) { msg('切换失败：' + e.name, true); }
};

/* ── 送检：把视频按 16:9 居中裁切画进小画布 ────────────────────────── */
function grab() {
  const sw = V.videoWidth, sh = V.videoHeight;
  if (!sw || !sh) return false;
  const tr = SEND_W / SEND_H, sr = sw / sh;
  let cw, ch;
  if (sr > tr) { ch = sh; cw = sh * tr; } else { cw = sw; ch = sw / tr; }
  WCTX.drawImage(V, (sw - cw) / 2, (sh - ch) / 2, cw, ch, 0, 0, SEND_W, SEND_H);
  return true;
}

function send() {
  return new Promise(resolve => {
    WORK.toBlob(async blob => {
      if (!blob) return resolve(null);
      const ac = new AbortController();
      const to = setTimeout(() => ac.abort(), 4000);
      try {
        const r = await fetch('/detect?w=' + SEND_W + '&h=' + SEND_H,
                              {method: 'POST', body: blob, signal: ac.signal});
        resolve(await r.json());
      } catch (e) { resolve(null); }
      finally { clearTimeout(to); }
    }, 'image/jpeg', 0.72);
  });
}

/* ── 检测循环：自适应间隔，慢了自动降频，永远不排队 ───────────────── */
async function loop() {
  if (!running) return;
  if (document.hidden || !stream || V.readyState < 2 || !V.videoWidth) {
    setTimeout(loop, 250);
    return;
  }
  const t0 = performance.now();
  let j = null;
  if (grab()) {
    sent++;
    j = await send();
  }
  const dt = performance.now() - t0;
  const now = performance.now();
  if (j && j.ok !== false) {
    got++; errors = 0;
    msAvg = msAvg ? msAvg * 0.8 + dt * 0.2 : dt;
    if (j.has_base !== undefined) { hasBase = j.has_base; }
    if (!j.busy) { last = j; paintRead(j); }
    P_RATE.className = 'pill ' + (j.busy ? 'warn' : 'ok');
    P_RATE.textContent = '检测 ' + rate() + '/s · ' + Math.round(j.ms) + 'ms'
      + (j.busy ? ' · 繁忙' : '');
    P_BASE.textContent = hasBase ? '基准已建' : '未建基准';
    P_BASE.className = 'pill' + (hasBase ? ' ok' : '');
    B_CMP.disabled = !hasBase;
  } else {
    errors++;
    if (errors >= 4) msg('检测请求失败：服务还活着吗？', true);
  }
  // 检测节奏：把单次往返压在 ~55ms 一个周期（≈18fps）。检测变慢时
  // 自动拉长间隔，服务端忙的时候（busy）再多让 15ms，别把槽位占满。
  const wait = Math.min(300, Math.max(20, 55 - dt)) + (j && j.busy ? 15 : 0);
  setTimeout(loop, wait);
}

function rate() {
  const s = (performance.now() - tStart) / 1000;
  return s > 1 ? (got / s).toFixed(1) : '—';
}

/* ── 读数面板 ─────────────────────────────────────────────────────── */
function paintRead(j) {
  const ts = j.tracks || [];
  const letters = ts.map(t => t.letter).filter(Boolean);
  if (letters.length) {
    READ.innerHTML = letters.join(' ');
  } else if (j.center_letter) {
    READ.innerHTML = j.center_letter + ' <span class="q">模型读</span>';
  } else {
    READ.innerHTML = '<span class="q">没读到字母</span>';
  }
  const st = j.stats || {};
  const w = st.why || {};
  const why = [];
  if (w.no_ink) why.push('无字 ' + w.no_ink);
  if (w.dark) why.push('不够白 ' + w.dark);
  if (w.thick) why.push('笔画粗 ' + w.thick);
  if (w.big) why.push('太大 ' + w.big);
  if (w.shape) why.push('形状 ' + w.shape);
  if (w.score) why.push('分低 ' + w.score);
  if (w.margin) why.push('认不准 ' + w.margin);
  DETAIL.textContent = '白块 ' + (st.candidates || 0) + ' · 读到 ' + (st.read || 0)
    + (why.length ? ' · 滤掉 ' + why.join(' ') : '')
    + ' · ' + (st.read_ms || 0) + 'ms　' + (j.reader || '');
  if (!ts.length && !j.center_letter) {
    if (w.score || w.margin) {
      msg('看到像标签的白块但认不准（分低）：把这块积木正对镜头再登记一次，会明显变准');
    } else if (w.thick || w.dark) {
      msg('框到 ' + (st.candidates || 0) + ' 个白块但都不像字母标签（不够白/笔画太粗）：'
          + '把白纸正对镜头、拿近到约占画面 1/8');
    } else if ((st.candidates || 0) > 0) {
      msg('找到的方块上没有字：字母要在白纸上，笔画用深色粗笔');
    } else {
      msg('没找到贴字母的白标签：把有字母的那面朝向镜头，靠近一点');
    }
  } else {
    msg('');
  }
}

/* ── 叠加层：60fps 重绘，框做缓动，不跟着检测频率跳 ───────────────── */
const STAGE = document.getElementById('view');
const FRAME = document.getElementById('frame');

// 显示框必须**精确 16:9**：送检时我们把视频居中裁成了 16:9，屏幕上如果
// 用其它比例，object-fit:cover 会再裁一次 —— 标注框就整体偏移。
function fitBox() {
  const c = STAGE.getBoundingClientRect();
  if (!c.width || !c.height) return;
  let w = c.width, h = w * 9 / 16;
  if (h > c.height) { h = c.height; w = h * 16 / 9; }
  FRAME.style.width = Math.round(w) + 'px';
  FRAME.style.height = Math.round(h) + 'px';
}
window.addEventListener('resize', fitBox);
function fitCanvas() {
  const r = CV.getBoundingClientRect();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const key = Math.round(r.width) + 'x' + Math.round(r.height) + '@' + dpr;
  // 每帧都改 canvas.width 会清空画布并触发重新分配，只在尺寸真变了才动
  if (key !== lastSize) {
    lastSize = key;
    CV.width = Math.round(r.width * dpr);
    CV.height = Math.round(r.height * dpr);
    G.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  return r;
}

function draw() {
  const r = fitCanvas();
  const fw = (last.frame && last.frame[0]) || SEND_W;
  const k = r.width / fw;                       // 送检像素 → CSS 像素
  G.clearRect(0, 0, r.width, r.height);

  // 分区线跟候选框一起藏在调试开关后面：那套坐标是按另一台机器画的，
  // 摆在这台机器的画面上是错的，又容易让人以为"框歪了所以认错了"。
  if (DBG.checked && Object.keys(zones).length) {
    G.lineWidth = 1;
    G.font = '600 11px ui-monospace,Menlo,monospace';
    const zk = r.width / (zoneBase[0] || SEND_W);
    for (const [name, pts] of Object.entries(zones)) {
      G.beginPath();
      pts.forEach((p, i) => {
        const x = p[0] * zk, y = p[1] * zk;
        if (i === 0) G.moveTo(x, y); else G.lineTo(x, y);
      });
      G.closePath();
      G.setLineDash([5, 6]);
      G.strokeStyle = 'rgba(90,170,255,.34)';
      G.stroke();
      G.setLineDash([]);
      G.fillStyle = 'rgba(150,200,255,.5)';
      G.fillText(name, pts[0][0] * zk + 7, pts[0][1] * zk + 15);
    }
  }

  const live = new Map();
  // 调试用：把所有"框到白块但没读出字"的候选画成虚线细框，调机位时看这个
  if (DBG.checked) {
    G.setLineDash([3, 4]);
    G.lineWidth = 1;
    G.strokeStyle = 'rgba(140,180,255,.45)';
    G.font = '500 10px ui-monospace,Menlo,monospace';
    for (const c of (last.candidates || [])) {
      const [x, y, w, h] = c.bbox;
      G.strokeRect(x * k, y * k, w * k, h * k);
      G.fillStyle = 'rgba(150,190,255,.72)';
      G.fillText((c.why || '') + ' ' + (c.score || 0).toFixed(2), x * k + 2, y * k - 3);
    }
    G.setLineDash([]);
  }
  for (const t of (last.tracks || [])) {
    const [x, y, w, h] = t.bbox;
    const target = {x: x * k, y: y * k, w: w * k, h: h * k, t};
    live.set(t.id, target);
    const cur = shown.get(t.id);
    if (!cur) { shown.set(t.id, {...target}); continue; }
    for (const p of ['x', 'y', 'w', 'h']) cur[p] += (target[p] - cur[p]) * 0.45;
    cur.t = t;
  }
  for (const id of [...shown.keys()]) if (!live.has(id)) shown.delete(id);

  for (const b of shown.values()) {
    const t = b.t;
    const ok = !!t.letter;
    const col = ok ? '61,220,132' : '255,176,32';
    G.lineWidth = 2.2;
    G.strokeStyle = 'rgba(' + col + ',.95)';
    G.shadowColor = 'rgba(' + col + ',.45)';
    G.shadowBlur = 10;
    G.beginPath();
    const rr = Math.min(8, b.w / 5, b.h / 5);
    G.roundRect(b.x, b.y, b.w, b.h, rr);
    G.stroke();
    G.shadowBlur = 0;

    const tag = ok ? t.letter : '?';
    G.font = '700 15px ui-monospace,Menlo,monospace';
    const tw = Math.max(24, G.measureText(tag).width + 14);
    const th = 22;
    const ty = b.y - th - 5 < 2 ? b.y + 4 : b.y - th - 5;
    G.fillStyle = 'rgba(' + col + ',.94)';
    G.beginPath();
    G.roundRect(b.x, ty, tw, th, 6);
    G.fill();
    G.fillStyle = '#07130c';
    G.fillText(tag, b.x + 7, ty + 16);
    if (t.score) {
      G.font = '600 11px ui-monospace,Menlo,monospace';
      G.fillStyle = 'rgba(' + col + ',.85)';
      G.fillText(t.score.toFixed(2), b.x + tw + 6, ty + 15);
    }
  }
  requestAnimationFrame(draw);
}

/* ── 按钮 ─────────────────────────────────────────────────────────── */
async function post(path, body) {
  const r = await fetch(path, body === undefined ? {method: 'POST'}
    : {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify(body)});
  return r.json();
}

B_ANCHOR.onclick = async () => {
  B_ANCHOR.disabled = true;
  try {
    const j = await post('/anchor');
    RES.className = 'res on';
    RES.innerHTML = '<h3>' + (j.msg || (j.ok ? '基准已建' : '建基准失败')) + '</h3>'
      + (j.ok ? '<div class="hint">摆好之后就别动了，改动积木后点「与基准比较」</div>' : '');
    if (j.ok) { hasBase = true; B_CMP.disabled = false; P_BASE.textContent = '基准已建'; P_BASE.className = 'pill ok'; }
  } finally { B_ANCHOR.disabled = false; }
};

B_CMP.onclick = async () => {
  B_CMP.disabled = true;
  try {
    const j = await post('/compare');
    RES.className = 'res on';
    if (!j.ok) { RES.innerHTML = '<h3>' + (j.msg || '比较失败') + '</h3>'; return; }
    const LBL = {same: '原位', moved: '被挪动'};
    let h = '<div class="verdict ' + j.verdict + '">'
      + ({READY: '✓ 和基准一致', NOT_READY: '✕ 有变化', REVIEW: '? 看不清'}[j.verdict] || j.verdict)
      + '</div><table><tr><th>字母</th><th>基准位置</th><th></th><th>现在</th><th>位移</th><th>判定</th></tr>';
    for (const r of j.rows) {
      h += '<tr><td>' + r.letter + '</td><td>' + (r.from || '—') + '</td><td>→</td><td>'
        + (r.to || '—') + '</td><td>' + r.px + 'px</td><td class="' + (r.status === 'same' ? 's' : 'm') + '">'
        + LBL[r.status] + '</td></tr>';
    }
    h += '</table>';
    if (j.missing.length) h += '<div class="hint">少了：' + j.missing.join('、') + '</div>';
    if (j.extra.length) h += '<div class="hint">多了：' + j.extra.join('、') + '</div>';
    RES.innerHTML = h;
  } finally { B_CMP.disabled = false; }
};

const INPUT = document.getElementById('letter');
document.getElementById('bEnroll').onclick = async () => {
  const L = (INPUT.value || '').trim().toUpperCase();
  if (!L) { msg('先填这块积木上写的是哪个字母', true); return; }
  const j = await post('/enroll', {letter: L});
  RES.className = 'res on';
  RES.innerHTML = '<h3>' + (j.msg || '') + '</h3>';
  if (j.ok) { INPUT.value = ''; msg('已登记 ' + L, false); }
  else msg(j.msg || '登记失败', true);
};
INPUT.addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('bEnroll').click(); });

document.getElementById('bForget').onclick = async () => {
  const L = (INPUT.value || '').trim().toUpperCase();
  if (!L) { msg('先填要删除的字母', true); return; }
  const j = await post('/forget', {letter: L});
  RES.className = 'res on';
  RES.innerHTML = '<h3>' + (j.msg || '') + '</h3>';
  msg(j.msg || '', !j.ok);
};

document.getElementById('bSnap').onclick = async () => {
  const j = await post('/snapshot');
  RES.className = 'res on';
  RES.innerHTML = '<h3>' + (j.msg || '') + '</h3>';
};

// 自动模式讲究"宁可不认"；这条是用户明确要求读一次，可以慢、可以只读画面中央
document.getElementById('bPeek').onclick = async (e) => {
  const b = e.currentTarget;
  b.disabled = true;
  const old = b.textContent;
  b.textContent = '读…';
  try {
    const j = await post('/peek');
    RES.className = 'res on';
    RES.innerHTML = '<h3>' + (j.msg || '') + '</h3>';
    if (j.ok) READ.innerHTML = j.letter + ' <span class="q">模型读</span>';
  } finally { b.disabled = false; b.textContent = old; }
};

/* ── 启动 ─────────────────────────────────────────────────────────── */
(async () => {
  try {
    const id = await openCam(localStorage.getItem('rebench.cam') || '');
    P_CAM.textContent = '画面就绪';
    P_CAM.className = 'pill ok';
    msg('');
  } catch (e) {
    P_CAM.textContent = '无画面';
    P_CAM.className = 'pill bad';
    const tip = e.name === 'NotAllowedError'
      ? '浏览器拒绝了摄像头：点地址栏左侧的图标允许，或在「系统设置 → 隐私与安全性 → 摄像头」里勾上浏览器'
      : (e.name === 'NotFoundError' ? '没找到摄像头' : (e.message || ''));
    msg('拿不到摄像头：' + tip, true);
  }
  try {
    const z = await (await fetch('/zones')).json();
    zones = z.zones || {};
    zoneBase = z.base || [SEND_W, SEND_H];
  } catch (e) {}
  running = true;
  tStart = performance.now();
  fitBox();
  requestAnimationFrame(draw);
  loop();
})();
</script></body></html>"""
