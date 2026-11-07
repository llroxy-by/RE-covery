"""扫描式页面。画面在浏览器本地播，识别只在"按一下"之后发生一次。"""

PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE:BENCH · 扫描识别</title>
<style>
:root{--bg:#0b0d11;--panel:#151920;--line:#252b35;--fg:#e8ebf0;--dim:#8b929e;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff;--teal:#35d0c0}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:14px;
      padding:14px;height:100vh;max-width:1760px;margin:0 auto}
.stage{display:flex;flex-direction:column;gap:10px;min-width:0}
.bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h1{font-size:15px;font-weight:650;letter-spacing:.3px}
.pill{font-size:11.5px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);
      color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.pill.ok{color:var(--ok);border-color:#245c3d}
.pill.bad{color:var(--bad);border-color:#6b2426}
.pill.warn{color:var(--warn);border-color:#5c4520}
.pill.run{color:var(--blue);border-color:#234a75}
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
.actbar{display:flex;align-items:center;gap:10px}
button{flex:1;padding:11px 8px;border-radius:10px;border:1px solid #2f4568;background:#18202e;
       color:var(--fg);font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover:not(:disabled){background:#1d2839}
button.primary{background:#173f28;border-color:#2c7047;color:#c8ffdd}
button.primary:hover:not(:disabled){background:#1c4d31}
button:disabled{opacity:.38;cursor:default}
button.big{padding:14px;font-size:15px;letter-spacing:.5px;flex:0 0 auto;min-width:230px}
.kbd{font-size:11px;font-weight:500;opacity:.6;margin-left:6px}
.chk{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--dim);cursor:pointer;user-select:none}
.panel{display:flex;flex-direction:column;gap:10px;overflow:auto}
.card{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:11px 12px}
.card h2{font-size:11.5px;color:var(--dim);font-weight:600;letter-spacing:.4px;margin-bottom:8px}
.read{font-size:26px;font-weight:700;letter-spacing:4px;font-variant-numeric:tabular-nums;
      min-height:38px;color:#d8f5e3;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.read .q{color:#7b8391;letter-spacing:1px;font-size:14px;font-weight:500}
.detail{font-size:11.5px;color:var(--dim);margin-top:4px;font-family:ui-monospace,Consolas,monospace;
        word-break:break-all;line-height:1.65}
.row{display:flex;gap:8px}
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
.item{display:flex;align-items:center;gap:8px;padding:7px 6px;border-radius:8px;cursor:pointer;
      border:1px solid transparent}
.item:hover{background:#ffffff08}
.item.sel{background:#12283a;border-color:#2b5480}
.lt{flex:0 0 26px;height:26px;line-height:26px;text-align:center;border-radius:7px;
    font-family:ui-monospace,Menlo,monospace;font-weight:700;font-size:15px}
.lt.h{background:#16452b;color:#8dffb9}
.lt.m{background:#123f47;color:#8fe8df}
.lt.l{background:#4a3a10;color:#ffd479}
.meta{flex:1;min-width:0;font-size:11.5px;color:var(--dim);
      font-family:ui-monospace,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.px{font-size:10.5px;color:#5d6572;font-family:ui-monospace,Menlo,monospace}
@media(max-width:980px){.wrap{grid-template-columns:minmax(0,1fr);height:auto}
  .view{flex:none}}
</style></head><body>
<div class="wrap">
  <div class="stage">
    <div class="bar">
      <h1>RE:BENCH</h1>
      <span class="pill" id="pCam">摄像头</span>
      <span class="pill" id="pState">待机</span>
      <span class="pill" id="pBase">未建基准</span>
      <span class="sp"></span>
      <label class="pill" style="cursor:pointer;user-select:none">
        <input type="checkbox" id="dbg" style="vertical-align:-1px;margin-right:4px">候选框
      </label>
      <select id="cam" style="max-width:280px;font-size:12px;padding:6px"></select>
    </div>
    <div class="view" id="view">
      <div id="frame">
        <video id="v" playsinline muted autoplay></video>
        <canvas id="cv"></canvas>
        <div class="msg" id="msg"></div>
      </div>
    </div>
    <div class="actbar">
      <button id="bScan" class="primary big">扫描识别<span class="kbd">空格</span></button>
      <label class="chk"><input type="checkbox" id="auto" checked>手离开画面后自动扫描</label>
    </div>
  </div>

  <div class="panel">
    <div class="card">
      <h2>读到 <span id="srcName" style="font-weight:400;opacity:.7"></span></h2>
      <div class="read" id="read">—</div>
      <div class="detail" id="detail">还没扫描。摆好积木后按一下空格（或等手离开）。</div>
    </div>
    <div class="card" id="listCard" style="display:none">
      <h2>明细（点一行可以教它）</h2>
      <div id="list"></div>
    </div>
    <div class="row">
      <button id="bAnchor" class="primary">建立基准</button>
      <button id="bCmp" disabled>与基准比较</button>
    </div>
    <div class="res" id="res"></div>
    <div class="card">
      <h2>登记（认不准时现场教一次）</h2>
      <div class="row">
        <input id="letter" maxlength="2" placeholder="A" autocomplete="off" autocapitalize="characters">
        <button id="bEnroll" style="flex:0 0 34%">登记</button>
        <button id="bForget" style="flex:0 0 26%">删除</button>
      </div>
      <div class="hint">先在明细里点中那一块，再填字母 —— 只教它，不会因为画面里有多块而被拒绝。登记一次永久有效。</div>
    </div>
    <div class="row">
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
const P_STATE = document.getElementById('pState');
const P_BASE = document.getElementById('pBase');
const READ = document.getElementById('read');
const DETAIL = document.getElementById('detail');
const SRCNAME = document.getElementById('srcName');
const RES = document.getElementById('res');
const B_ANCHOR = document.getElementById('bAnchor');
const B_CMP = document.getElementById('bCmp');
const B_SCAN = document.getElementById('bScan');
const SEL = document.getElementById('cam');
const DBG = document.getElementById('dbg');
const AUTO = document.getElementById('auto');
const LISTCARD = document.getElementById('listCard');
const LIST = document.getElementById('list');

// 送检画面固定 16:9。显示区也强制 16:9 且 object-fit:cover，两边裁法一致，
// 所以服务端坐标 × (显示宽度 / 送检宽度) 就是屏幕坐标。
const SEND_W = 1280, SEND_H = 720;
const FRAMES = 8;                  // 一次扫描采几帧：投 4 帧，其余留给清晰度挑选
const WORK = document.createElement('canvas');
WORK.width = SEND_W; WORK.height = SEND_H;
const WCTX = WORK.getContext('2d', {willReadFrequently: false});

let stream = null, hasBase = false, scanning = false;
let last = {blocks: [], dropped: [], frame: [SEND_W, SEND_H]};
let scanAt = 0, selected = -1;
let zones = {}, zoneBase = [SEND_W, SEND_H];
let lastSize = '';
const CONF = {high: '高可信', mid: '可信', low: '待定', none: '不算'};
const sleep = ms => new Promise(r => setTimeout(r, ms));

function msg(text, bad) {
  MSG.textContent = text || '';
  MSG.className = 'msg' + (text ? ' on' : '') + (bad ? ' bad' : '');
}

/* ── 摄像头：只有本地预览，待机时服务端一个像素都不收 ───────────────── */
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
  // 扫描要细节，尽量要 1080p：同样的标签多一半像素，远景识别率差别很大
  const want = {width: {ideal: 1920}, height: {ideal: 1080}, frameRate: {ideal: 30}};
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

/* ── 采样：把视频按 16:9 居中裁切画进画布，再逐帧编码 ───────────────── */
function grab() {
  const sw = V.videoWidth, sh = V.videoHeight;
  if (!sw || !sh) return false;
  const tr = SEND_W / SEND_H, sr = sw / sh;
  let cw, ch;
  if (sr > tr) { ch = sh; cw = sh * tr; } else { cw = sw; ch = sw / tr; }
  WCTX.drawImage(V, (sw - cw) / 2, (sh - ch) / 2, cw, ch, 0, 0, SEND_W, SEND_H);
  return true;
}

/* ── 扫描：一次信号，采 8 帧，交给服务端做完整识别 ─────────────────── */
function setBusy(text) {
  B_SCAN.disabled = true;
  B_SCAN.firstChild.textContent = text;
  P_STATE.className = 'pill run';
  P_STATE.textContent = text.replace(/…|\.\.\./g, '');
}
function setIdle() {
  B_SCAN.disabled = false;
  B_SCAN.firstChild.textContent = '扫描识别';
  scanning = false;
}

async function scanNow(quiet) {
  if (scanning) return null;
  if (!stream || V.readyState < 2 || !V.videoWidth) {
    msg('还没有画面：先允许摄像头', true);
    return null;
  }
  scanning = true;
  const frames = [];
  try {
    for (let i = 0; i < FRAMES; i++) {
      if (!grab()) break;
      frames.push(WORK.toDataURL('image/jpeg', 0.92).split(',')[1]);
      setBusy('采集中 ' + (i + 1) + '/' + FRAMES + '…');
      await sleep(110);
    }
    if (!frames.length) { msg('抓不到画面', true); return null; }
    setBusy('识别中…');
    const t0 = performance.now();
    const tick = setInterval(() => {
      setBusy('识别中 ' + ((performance.now() - t0) / 1000).toFixed(1) + 's…');
    }, 200);
    let j;
    try {
      const r = await fetch('/scan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({w: SEND_W, h: SEND_H, frames})
      });
      j = await r.json();
    } finally { clearInterval(tick); }
    if (!j || j.ok === false) {
      msg((j && j.msg) || '识别失败', true);
      P_STATE.className = 'pill bad'; P_STATE.textContent = '失败';
      return null;
    }
    last = {blocks: j.blocks || [], dropped: j.dropped || [],
            frame: j.frame || [SEND_W, SEND_H], center: j.center_letter || ''};
    scanAt = performance.now();
    selected = -1;
    hasBase = !!j.has_base;
    SRCNAME.textContent = j.reader ? '· ' + j.reader : '';
    renderRead(j);
    renderList(j);
    updateBase();
    if (!quiet) msg('');
    return j;
  } catch (e) {
    msg('识别请求失败：' + (e.message || e), true);
    return null;
  } finally {
    setIdle();
  }
}

B_SCAN.onclick = () => scanNow(false);
document.addEventListener('keydown', e => {
  if (e.code === 'Space' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
    e.preventDefault();
    scanNow(false);
  }
});

/* ── 手离开后自动扫描：只比 160×90 的缩略帧，几乎不耗 CPU ─────────── */
const MOT = document.createElement('canvas');
MOT.width = 160; MOT.height = 90;
const MCTX = MOT.getContext('2d', {willReadFrequently: true});
let prevPix = null, moving = false, lastMove = 0;

setInterval(() => {
  if (!stream || V.readyState < 2 || !V.videoWidth) return;
  MCTX.drawImage(V, 0, 0, 160, 90);
  const d = MCTX.getImageData(0, 0, 160, 90).data;
  if (prevPix) {
    let sum = 0, n = 0;
    for (let i = 0; i < d.length; i += 16) { sum += Math.abs(d[i] - prevPix[i]); n++; }
    const diff = sum / n, now = performance.now();
    if (diff > 7) { moving = true; lastMove = now; }
    else if (moving && now - lastMove > 900 && !scanning) {
      moving = false;
      if (AUTO.checked) scanNow(true);
    }
  }
  prevPix = d;
}, 200);

/* ── 结果面板 ─────────────────────────────────────────────────────── */
function good(b) { return b.letter && (b.conf === 'high' || b.conf === 'mid'); }

function renderRead(j) {
  const ok = (j.blocks || []).filter(good);
  if (ok.length) {
    READ.innerHTML = ok.map(b => b.letter).join(' ');
  } else if (j.center_letter) {
    READ.innerHTML = j.center_letter + ' <span class="q">整帧直读</span>';
  } else {
    READ.innerHTML = '<span class="q">没读到字母</span>';
  }
  const st = j.stats || {};
  DETAIL.textContent = '采 ' + (st.frames || 0) + ' 帧（用 ' + (st.used || 0) + '）'
    + ' · 候选 ' + (st.candidates || 0) + ' · 读到 ' + (st.read || 0)
    + ' · 高可信 ' + (st.high || 0) + ' · 待定 ' + (st.low || 0)
    + ' · 检测 ' + (st.detect_ms || 0) + 'ms'
    + ' · 模型 ' + (st.vlm_calls || 0) + ' 次 ' + (st.vlm_ms || 0) + 'ms'
    + ' · 合计 ' + ((st.ms || 0) / 1000).toFixed(1) + 's';
}

function renderList(j) {
  const bs = j.blocks || [];
  if (!bs.length) { LISTCARD.style.display = 'none'; return; }
  LISTCARD.style.display = 'block';
  LIST.innerHTML = bs.map((b, i) => {
    const k = b.conf === 'high' ? 'h' : (b.conf === 'mid' ? 'm' : 'l');
    const src = b.src === 'both' ? '模板+模型' : (b.src === 'vlm' ? '模型' : '模板');
    let meta = (CONF[b.conf] || b.conf);
    if (b.letter) meta += ' · ' + src;
    if (b.score) meta += ' · ' + b.score.toFixed(2);
    if (b.frames) meta += ' · ' + (b.votes && b.votes[b.letter] || 0) + '/' + b.frames + ' 帧';
    if (b.reason) meta += ' · ' + b.reason;
    return '<div class="item" data-i="' + i + '"><span class="lt ' + k + '">'
      + (b.letter || '?') + '</span><span class="meta">' + meta + '</span>'
      + '<span class="px">' + (b.px ? b.px[0] + '×' + b.px[1] : '') + '</span></div>';
  }).join('');
  [...LIST.querySelectorAll('.item')].forEach(el => {
    el.onclick = () => pick(+el.dataset.i);
  });
}

function pick(i) {
  const b = (last.blocks || [])[i];
  if (!b) return;
  selected = i;
  [...LIST.querySelectorAll('.item')].forEach(el => {
    el.classList.toggle('sel', +el.dataset.i === i);
  });
  const input = document.getElementById('letter');
  if (b.letter) input.value = b.letter;
  input.focus();
  msg('已选中这一块' + (b.letter ? '（' + b.letter + '）' : '（还没读出，填个字母教它）'));
}

function updateBase() {
  P_BASE.textContent = hasBase ? '基准已建' : '未建基准';
  P_BASE.className = 'pill' + (hasBase ? ' ok' : '');
  B_CMP.disabled = !hasBase || scanning;
  B_ANCHOR.disabled = scanning;
}

/* ── 叠加层：60fps 重绘，结果不闪 ─────────────────────────────────── */
const STAGE = document.getElementById('view');
const FRAME_EL = document.getElementById('frame');

function fitBox() {
  const c = STAGE.getBoundingClientRect();
  if (!c.width || !c.height) return;
  let w = c.width, h = w * 9 / 16;
  if (h > c.height) { h = c.height; w = h * 16 / 9; }
  FRAME_EL.style.width = Math.round(w) + 'px';
  FRAME_EL.style.height = Math.round(h) + 'px';
}
window.addEventListener('resize', fitBox);

function fitCanvas() {
  const r = CV.getBoundingClientRect();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const key = Math.round(r.width) + 'x' + Math.round(r.height) + '@' + dpr;
  // 每帧都改 canvas.width 会清空画布并重新分配，只在尺寸真变了才动
  if (key !== lastSize) {
    lastSize = key;
    CV.width = Math.round(r.width * dpr);
    CV.height = Math.round(r.height * dpr);
    G.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  return r;
}

const COL = {high: '61,220,132', mid: '53,208,192', low: '255,176,32', none: '140,140,140'};

function draw() {
  const r = fitCanvas();
  const fw = (last.frame && last.frame[0]) || SEND_W;
  const k = r.width / fw;
  G.clearRect(0, 0, r.width, r.height);

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
      G.strokeStyle = 'rgba(90,170,255,.30)';
      G.stroke();
      G.setLineDash([]);
      G.fillStyle = 'rgba(150,200,255,.45)';
      G.fillText(name, pts[0][0] * zk + 7, pts[0][1] * zk + 15);
    }
  }

  // 被闸门拒掉的候选：虚线灰框 + 拒绝原因，调机位时看这个
  if (DBG.checked) {
    G.setLineDash([3, 4]);
    G.lineWidth = 1;
    G.strokeStyle = 'rgba(150,160,175,.5)';
    G.font = '500 10px ui-monospace,Menlo,monospace';
    for (const d of (last.dropped || [])) {
      const [x, y, w, h] = d.bbox;
      G.strokeRect(x * k, y * k, w * k, h * k);
      G.fillStyle = 'rgba(165,175,190,.8)';
      G.fillText((d.why || '') + (d.score !== undefined ? ' ' + d.score : ''),
                 x * k + 2, y * k - 3);
    }
    G.setLineDash([]);
  }

  for (const b of (last.blocks || [])) {
    const [x, y, w, h] = b.bbox;
    const X = x * k, Y = y * k, W2 = w * k, H2 = h * k;
    const col = COL[b.conf] || COL.none;
    const ok = b.conf === 'high' || b.conf === 'mid';
    G.lineWidth = ok ? 2.4 : 1.6;
    if (!ok) G.setLineDash([6, 5]);
    G.strokeStyle = 'rgba(' + col + ',.95)';
    G.shadowColor = 'rgba(' + col + ',.4)';
    G.shadowBlur = ok ? 10 : 0;
    G.beginPath();
    G.roundRect(X, Y, W2, H2, Math.min(8, W2 / 5, H2 / 5));
    G.stroke();
    G.shadowBlur = 0;
    G.setLineDash([]);

    const tag = b.letter || '?';
    G.font = '700 15px ui-monospace,Menlo,monospace';
    const tw = Math.max(24, G.measureText(tag).width + 14);
    const th = 22;
    const ty = Y - th - 5 < 2 ? Y + 4 : Y - th - 5;
    G.fillStyle = 'rgba(' + col + ',.94)';
    G.beginPath();
    G.roundRect(X, ty, tw, th, 6);
    G.fill();
    G.fillStyle = '#07130c';
    G.fillText(tag, X + 7, ty + 16);
    if (b.score) {
      G.font = '600 11px ui-monospace,Menlo,monospace';
      G.fillStyle = 'rgba(' + col + ',.9)';
      G.fillText(b.score.toFixed(2) + (b.conf === 'low' ? '?' : ''), X + tw + 6, ty + 15);
    }
  }
  requestAnimationFrame(draw);
}

/* 状态灯：结果有几秒了，别让人对着 10 秒前的框以为画面实时 */
setInterval(() => {
  if (scanning) return;
  if (!scanAt) { P_STATE.className = 'pill'; P_STATE.textContent = '待机'; return; }
  const age = (performance.now() - scanAt) / 1000;
  P_STATE.className = 'pill ok';
  P_STATE.textContent = '结果 ' + (age < 1 ? '刚刚' : (age < 60 ? Math.round(age) + 's 前' : Math.round(age / 60) + 'min 前'));
}, 500);

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
      + (j.ok ? '<div class="hint">摆好之后就别动了；改动积木后点「与基准比较」（会重新扫一次）</div>' : '');
    if (j.ok) { hasBase = true; updateBase(); }
  } finally { B_ANCHOR.disabled = false; }
};

B_CMP.onclick = async () => {
  B_CMP.disabled = true;
  try {
    const s = await scanNow(true);
    const j = await post('/compare');
    RES.className = 'res on';
    if (!j.ok) { RES.innerHTML = '<h3>' + (j.msg || '比较失败') + '</h3>'; return; }
    const LBL = {same: '原位', moved: '被挪动'};
    let h = '<div class="verdict ' + j.verdict + '">'
      + ({READY: '✓ 和基准一致', NOT_READY: '✕ 有变化', REVIEW: '? 看不清'}[j.verdict] || j.verdict)
      + '</div><table><tr><th>字母</th><th>基准</th><th></th><th>现在</th><th>位移</th><th>判定</th></tr>';
    for (const r of j.rows) {
      h += '<tr><td>' + r.letter + '</td><td>' + (r.from || '—') + '</td><td>→</td><td>'
        + (r.to || '—') + '</td><td>' + r.px + 'px</td><td class="'
        + (r.status === 'same' ? 's' : 'm') + '">' + LBL[r.status] + '</td></tr>';
    }
    h += '</table>';
    if (j.missing.length) h += '<div class="hint">这一扫没读到：' + j.missing.join('、')
      + '（可能被挡住了，也可能这次没认出来）</div>';
    if (j.extra.length) h += '<div class="hint">多出来的：' + j.extra.join('、') + '</div>';
    if (j.unsure && j.unsure.length) h += '<div class="hint">另有 ' + j.unsure.length
      + ' 块没读准，没参与判定</div>';
    RES.innerHTML = h;
  } finally { B_CMP.disabled = false; updateBase(); }
};

const INPUT = document.getElementById('letter');
document.getElementById('bEnroll').onclick = async () => {
  const L = (INPUT.value || '').trim().toUpperCase();
  if (!L) { msg('先填这块积木上写的是哪个字母', true); return; }
  const body = {letter: L};
  if (selected >= 0) body.index = selected;
  const j = await post('/enroll', body);
  RES.className = 'res on';
  RES.innerHTML = '<h3>' + (j.msg || '') + '</h3>';
  if (j.ok) { INPUT.value = ''; msg('已登记 ' + L + '，再扫一次就认得它了'); selected = -1; }
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

/* ── 启动 ─────────────────────────────────────────────────────────── */
(async () => {
  try {
    await openCam(localStorage.getItem('rebench.cam') || '');
    P_CAM.textContent = '画面就绪';
    P_CAM.className = 'pill ok';
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
  updateBase();
  fitBox();
  requestAnimationFrame(draw);
})();
</script></body></html>"""
