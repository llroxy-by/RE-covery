"""Mac 本机摄像头实时检测（不用手机，不用 HTTPS 自签证书）。

    python -m vision.tools.mac_live
    python -m vision.tools.mac_live --config vision/config/mac.json --port 8301

和 phone_live.py 的区别只在"画面从哪来"：

    phone_live  手机浏览器 getUserMedia → HTTPS 上传帧 → 服务端检测 → 回传标注图
    mac_live    Mac 浏览器 getUserMedia → HTTP 上传帧（127.0.0.1）→ 同一套检测 → 回传标注图

检测链路、LiveSession、所有 POST 接口完全复用 phone_live，所以两边结果一致。

为什么不用 HTTPS：浏览器规定 getUserMedia 只在"安全上下文"里可用，而
`http://127.0.0.1` 和 `http://localhost` 本身就算安全上下文。手机通过局域网 IP
访问才必须 HTTPS。既然只用 Mac 本机摄像头，自签证书那一整套（以及手机上的
"连接不是私密连接 → 高级 → 继续前往"）就全都省掉了。

为什么要挑设备：Mac 上装了"连续互通相机"后，Chrome 的设备列表里会出现
「iPhone 摄像头」并可能排在前面 —— 那还是手机镜头。这里默认挑内建摄像头
（FaceTime / Built-in），带「iPhone / 连续互通 / 桌面视图」字样的排在后面并标注，
需要时可以在页面上手动切。
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import cv2

from . import phone_live
from ..snapshot import RECORDS_DIR

MAC_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE:BENCH · Mac 实时视觉</title>
<style>
:root{--bg:#0d0f13;--panel:#171a20;--line:#2a2f38;--fg:#e9ebef;--dim:#8b929e;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);min-height:100vh;
     font:14.5px/1.6 -apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 372px;gap:14px;padding:14px;
      align-items:start;max-width:1680px;margin:0 auto}
.hd{grid-column:1/-1;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hd h1{font-size:16.5px;font-weight:650;letter-spacing:.2px}
.pill{font-size:11.5px;padding:3px 9px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--ok);border-color:#2a5c40}
.pill.bad{color:var(--bad);border-color:#6b2426}
.pill.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.spacer{flex:1}
.view{position:relative;border:1px solid var(--line);border-radius:12px;overflow:hidden;
      background:#000}
.view video,.view img.ann{display:block;width:100%;aspect-ratio:16/9;background:#000}
/* 用 cover 而不是 contain：送检的帧是「居中裁成 16:9」，浏览器显示也得是同一个裁法，
   否则 4:3 摄像头的上下被留白，标注框会和画面错位。 */
.view video{object-fit:cover}
.view img.ann{object-fit:contain}
/* 标注图盖在原始画面上：两层同尺寸，标注图没来之前看到的是实时画面，
   不会出现"黑屏等结果"的空窗。 */
.view img.ann{position:absolute;inset:0;height:100%}
.hud{position:absolute;left:9px;top:9px;font:11px/1.5 ui-monospace,Consolas,monospace;
     color:#cfd6e2;background:#000a;padding:4px 8px;border-radius:5px;pointer-events:none}
.warnbar{position:absolute;left:0;right:0;bottom:0;padding:9px 12px;font-size:13.5px;
         background:#6b1f24ee;color:#ffe2e2;text-align:center;display:none;font-weight:600}
.warnbar.on{display:block}
.panel{display:flex;flex-direction:column;gap:10px}
.card{border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:11px 12px}
.card h2{font-size:12px;color:var(--dim);font-weight:600;letter-spacing:.3px;margin-bottom:7px}
.msg{font-size:12.5px;color:var(--dim);min-height:34px}
.msg.bad{color:var(--bad)}.msg.ok{color:var(--ok)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:12.5px;padding:4px 9px;border-radius:7px;background:#1c2028;border:1px solid var(--line)}
.chip b{font-family:ui-monospace,Consolas,monospace;font-weight:600}
.chip .z{color:var(--blue);font-family:ui-monospace,monospace}
.chip.miss{background:#2a1214;border-color:#6b2426;color:#ffb4b4}
.lbrow{font-size:12px;line-height:1.55;color:#9fd2ff;
       font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}
.row{display:flex;gap:8px}
button{flex:1;padding:12px 10px;border-radius:10px;border:1px solid #33507a;background:#1b2536;
       color:var(--fg);font-size:14.5px;font-weight:600;cursor:pointer;font-family:inherit}
button.primary{background:#1d4b2e;border-color:#2f7a4a;color:#c9ffdd}
button:disabled{opacity:.4;cursor:default}
input,select{padding:11px;border-radius:10px;border:1px solid #2a2f38;background:#141821;
       color:var(--fg);font-size:14.5px;font-family:inherit}
/* 输入框的默认宽度是按字符数算的，不给 min-width:0 就会把「删除」挤出面板 */
input#letter{flex:1 1 0;min-width:0;text-align:center;text-transform:uppercase;letter-spacing:2px}
.card .row button{flex:0 0 auto;padding:11px 16px}
select{width:100%;font-size:12.5px;padding:8px}
.res{border:1px solid var(--line);border-radius:11px;padding:10px 12px;background:var(--panel);
     font-size:13px;display:none}
.res h3{font-size:13.5px;margin-bottom:6px}
.res table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:5px}
.res td,.res th{padding:5px 4px;border-bottom:1px solid #ffffff0c;text-align:left}
.res th{color:var(--dim);font-size:11px;font-weight:600}
.res td.m{color:var(--warn)}.res td.u{color:var(--bad)}.res td.k{color:var(--ok)}
.verdict{margin-top:6px;font-weight:700;font-size:13.5px}
.verdict.READY{color:var(--ok)}.verdict.NOT_READY{color:var(--bad)}.verdict.REVIEW{color:var(--warn)}
.hint{margin-top:5px;font-size:12px;color:var(--dim)}
.foot{grid-column:1/-1;font-size:11.5px;color:#5c636e;text-align:center;padding:2px 0 8px}
@media(max-width:1000px){.wrap{grid-template-columns:minmax(0,1fr)}}
</style></head><body>
<div class="wrap">
  <div class="hd">
    <h1>RE:BENCH · 实时视觉</h1>
    <span class="pill" id="st">连接中</span>
    <span class="pill" id="base">未建基准</span>
    <span class="pill mono" id="fps">0 fps</span>
    <span class="spacer"></span>
    <select id="cam" style="width:auto;max-width:330px"><option>正在枚举摄像头…</option></select>
  </div>

  <div class="view">
    <video id="v" playsinline muted autoplay></video>
    <img class="ann" id="shot" alt="">
    <div class="hud" id="hud">—</div>
    <div class="warnbar" id="warnbar"></div>
  </div>

  <div class="panel">
    <div class="card">
      <h2>状态</h2>
      <div class="msg" id="msg">正在请求摄像头权限…</div>
      <div class="chips" id="chips" style="margin-top:7px"></div>
    </div>
    <div class="card">
      <h2>标签读数</h2>
      <div class="lbrow" id="lbrow">—</div>
    </div>
    <div class="row">
      <button id="anchor" class="primary">建立基准</button>
      <button id="cmp" disabled>与基准比较</button>
    </div>
    <div class="card">
      <h2>登记字母（模板库为空或认不准时用）</h2>
      <div class="row" style="margin-top:2px">
        <input id="letter" maxlength="2" placeholder="字母" autocapitalize="characters"
               autocomplete="off">
        <button id="enroll" style="flex:0 0 30%">登记</button>
        <button id="unlabel" style="flex:0 0 24%">删除</button>
      </div>
      <div class="hint">画面里只留一块积木再登记，否则会登记错笔画。</div>
    </div>
    <div class="card">
      <h2>分区标定</h2>
      <div class="row"><button id="snap">存一张画面</button></div>
      <div class="hint"><b>画面里的黄框就是 A / B / C / D / TEMP</b>，把积木放进框里就是分区，
        默认这套框能直接用。不用重新圈，除非桌子/label 位置跟框对不上：
        那才点上面这个按钮，存一张图再用命令行重新圈。</div>
    </div>
    <div class="res" id="res"></div>
  </div>

  <div class="foot" id="foot">
    画面来自本机摄像头 · 只在 127.0.0.1 上提供服务 · 关掉这个终端即停止
  </div>
</div>
<script>
const v=document.getElementById('v'), shot=document.getElementById('shot');
const hud=document.getElementById('hud'), msgEl=document.getElementById('msg');
const chips=document.getElementById('chips'), st=document.getElementById('st');
const baseEl=document.getElementById('base'), fpsEl=document.getElementById('fps');
const res=document.getElementById('res'), lbRow=document.getElementById('lbrow');
const bAnchor=document.getElementById('anchor'), bCmp=document.getElementById('cmp');
const selCam=document.getElementById('cam');
const cv=document.createElement('canvas'); cv.width=1280; cv.height=720;
const cx=cv.getContext('2d');

let stream=null, busy=false, hasBase=false, frames=0, t0=0, missed=0;

const CN={'block_red_01':'红色积木','block_green_01':'绿色积木','block_blue_01':'蓝色积木'};
const cn=s=>{
  if(CN[s]) return CN[s];
  const m=/^block_([A-Za-z0-9])$/.exec(s);
  if(m) return m[1]+' 块';
  return String(s).replace(/^block_/,'').replace(/_01$/,'');
};
function setMsg(t,cls){ msgEl.textContent=t; msgEl.className='msg'+(cls?' '+cls:''); }

function friendly(iss){
  const out=[];
  for(const i of iss){
    if(i.startsWith('label_pending:')) continue;
    if(i.startsWith('label_empty:')){ out.push('有一张标签上没写字'); continue; }
    if(i.startsWith('label_unread:')){ out.push('有标签没读出字母（分数偏低）'); continue; }
    const m=/^([A-Za-z0-9_]+):(.+)$/.exec(i);
    if(!m){ out.push(i); continue; }
    const name=cn(m[1]), k=m[2];
    if(k==='not_detected') out.push('看不到'+name);
    else if(k==='zone_unknown') out.push(name+'位置在变');
    else if(k==='possibly_occluded') out.push(name+'被挡或被裁掉一角');
    else if(k==='unrecognized_object') out.push('发现没登记的色块');
    else if(k==='low_confidence') out.push(name+'看不准');
    else if(k==='touching_frame_edge') out.push(name+'贴到画面边');
    else if(k==='label_unreadable') out.push(name+'上的字母认不出来');
    else if(k.startsWith('label_low_score')) out.push(name+'上的字母太模糊');
    else out.push(name+'：'+k);
  }
  return out;
}

function drawCover(){
  const sw=v.videoWidth, sh=v.videoHeight;
  if(!sw||!sh) return false;
  const tr=cv.width/cv.height, sr=sw/sh;
  let cw,ch;
  if(sr>tr){ ch=sh; cw=sh*tr; } else { cw=sw; ch=sw/tr; }
  cx.drawImage(v,(sw-cw)/2,(sh-ch)/2,cw,ch,0,0,cv.width,cv.height);
  return true;
}

function render(j){
  missed=0;
  frames++; if(!t0) t0=performance.now();
  fpsEl.textContent=(frames/((performance.now()-t0)/1000)).toFixed(1)+' fps';
  hasBase=j.has_baseline;
  baseEl.textContent=hasBase?'基准已建':'未建基准';
  baseEl.className='pill'+(hasBase?' ok':'');
  st.textContent=j.quality_ok?'画面正常':'画面异常';
  st.className='pill '+(j.quality_ok?'ok':'bad');
  bCmp.disabled=!hasBase;
  hud.textContent='objects='+j.objects.length+'  issues='+(j.issues.length||0)
                 +'  q='+(j.quality_ok?'ok':'bad')+'  tags='+((j.labels||[]).length)
                 +'  reader='+(j.reader||'-');
  if(j.labels){
    const rd=j.reader?('['+j.reader+'] '):'';
    // 模型直读画面中央的结果放在最前面。现场那张照片里 23 个候选全是背景
    // 玻璃和灯带，一个都不对；这时候只显示"标签 23 个 · 读不出"，人会以为
    // 系统彻底瞎了，而实际上模型已经读对了 —— 只是结果排在后面看不见。
    const gl=j.frame_letter?('◆ 模型直读：'+j.frame_letter+'　|　'):'';
    lbRow.textContent=gl+rd+(j.labels.length
      ? '标签 '+j.labels.length+' 个 · '+j.labels.map(l=>{
          if(!l.letter) return '读不出('+(l.why||'')+')';
          const warn=(l.src==='vlm_conflict')?'⚠':'';
          return warn+(l.letter||'')+' '+l.score.toFixed(2)
                 +(l.angle?'@'+l.angle+'°':'')
                 +(l.score>=(j.threshold||0)?'':' (低于'+(j.threshold||0)+')');
        }).join(' · ')
      : (j.frame_letter
          ? '没框到标签纸（标签太小），上面是模型直读的结果'
          : '没找到标签：把贴了字母的那一面朝向镜头，或者靠近一点'));
  }
  chips.innerHTML=j.objects.map(o=>
    '<span class="chip">'+cn(o.id)+' <span class="z">'+o.zone+'</span> <b>'
    +(o.vis!=='unknown'?o.conf.toFixed(2):'未检出')+'</b></span>').join('');
  const seen=new Set(j.objects.filter(o=>o.vis!=='unknown').map(o=>o.id));
  const miss=(j.expected||[]).filter(id=>!seen.has(id));
  const wb=document.getElementById('warnbar');
  if(miss.length){
    wb.classList.add('on');
    wb.textContent='画面里没有：'+miss.map(cn).join('、')+'  → 把镜头视野放宽/把积木挪进画面';
    setMsg('缺 '+miss.length+' 件：现在算"出画"状态','bad');
  }else{
    wb.classList.remove('on');
    if(!j.quality_ok) setMsg('画面不合格：'+friendly(j.issues).slice(0,2).join('；'),'bad');
    else setMsg('检测正常 · '+j.objects.filter(o=>o.vis!=='unknown').length+' 件都在画面内','ok');
  }
}

async function pump(){
  // 页面在后台标签页时别推帧：Safari 会把隐藏标签页的 video 帧降频甚至停掉，
  // 这时候继续 toBlob+上传只是白烧 CPU。
  if(busy||!stream||v.readyState<2||document.hidden) return;
  busy=true;
  try{
    if(drawCover()){
      // 0.82 → 0.72：上传从 ~130KB 降到 ~90KB，标签边长还在 80px 以上，
      // 检测结果不变，但浏览器编码和上传都省了一截。
      const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.72));
      // /frame 里要等模型读完字，正常 1-3 秒，偶尔更久。给个上限：
      // 万一服务端卡住（比如模型网关不通），不能让这个 await 一直挂着，
      // 否则下面的 finally 永远不执行，画面就定格在最后一帧 —— 现场看着
      // 就是"视频不动，只有一张照片"。
      const ac=new AbortController();
      const to=setTimeout(()=>ac.abort(),15000);
      let r;
      try{
        r=await fetch('/frame',{method:'POST',body:blob,signal:ac.signal});
      } finally { clearTimeout(to); }
      const j=await r.json();
      if(j.image) shot.src='data:image/jpeg;base64,'+j.image;
      if(j.error){ setMsg(j.error,'bad'); } else { render(j); }
      missed=0;
    }
  }catch(e){
    missed++;
    if(missed>3) setMsg('连接中断：'+e.message,'bad');
  }
  // 只用一个锁。以前这里是 busy=false 但外面还有个 pumping 标志，
  // 置位之后没有人复位 —— 第一帧放完就再进不来，画面永远停在那张照片上。
  finally{ busy=false; }
}

function showRes(j){
  res.style.display='block';
  let h='<h3>'+j.title+'</h3>';
  if(j.verdict) h+='<div class="verdict '+j.verdict+'">'+j.verdict+'</div>';
  if(j.differences&&j.differences.length){
    h+='<table><tr><th>目标</th><th>基准</th><th></th><th>当前</th><th>判定</th></tr>';
    for(const d of j.differences){
      const cls=d.status==='unchanged'?'k':(d.status==='uncertain'?'u':'m');
      const lab=d.status==='unchanged'?'原位':(d.status==='misplaced'?'被挪动':'不确定');
      h+='<tr><td>'+cn(d.object_id)+'</td><td>'+d.from_zone+'</td><td>→</td><td>'
        +d.to_zone+'</td><td class="'+cls+'">'+lab+'</td></tr>';
    }
    h+='</table>';
  }
  if(j.issues&&j.issues.length) h+='<div class="hint">问题：'+friendly(j.issues).join('；')+'</div>';
  if(j.hints&&j.hints.length) h+='<div class="hint">'+j.hints.join('<br>')+'</div>';
  res.innerHTML=h;
}

bAnchor.onclick=async()=>{
  bAnchor.disabled=true;
  try{
    const j=await (await fetch('/anchor',{method:'POST'})).json();
    if(j.error){ setMsg(j.error,'bad'); return; }
    showRes(j); setMsg(j.title, j.ok?'ok':'bad');
    if(j.ok){ hasBase=true; bCmp.disabled=false; }
  }finally{ bAnchor.disabled=false; }
};
bCmp.onclick=async()=>{
  bCmp.disabled=true;
  try{
    const j=await (await fetch('/compare',{method:'POST'})).json();
    if(j.error) setMsg(j.error,'bad'); else showRes(j);
  }finally{ bCmp.disabled=false; }
};

const inpLetter=document.getElementById('letter'), bEnroll=document.getElementById('enroll');
async function doLabel(path){
  const L=(inpLetter.value||'').trim().toUpperCase();
  if(!L){ setMsg('先填这块积木上写的是哪个字母','bad'); return null; }
  const j=await (await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
                                   body:JSON.stringify({letter:L})})).json();
  if(j.error){ setMsg(j.error,'bad'); return null; }
  const tips=(j.hints||[]).map(h=>'<div class="hint">'+h+'</div>').join('');
  res.style.display='block';
  res.innerHTML='<h3>'+(j.title||'')+'</h3>'+tips;
  return j;
}
bEnroll.onclick=async()=>{
  bEnroll.disabled=true;
  try{
    const j=await doLabel('/label');
    if(!j) return;
    if(j.ok){
      const lk=j.looked_like||[];
      const warn=(lk[0]&&lk[0]!==j.letter&&lk[1]>=0.6)
        ? '　⚠️ 这张字更像 '+lk[0]+'（'+lk[1].toFixed(2)+'）' : '';
      setMsg('已登记 '+j.letter+'（笔画 '+j.ink_px+'px）　库里：'+(j.letters||[]).join('、')+warn,
             warn?'bad':'ok');
      if(!warn) inpLetter.value='';
    }else setMsg(j.title,'bad');
  }finally{ bEnroll.disabled=false; }
};
inpLetter.addEventListener('keydown',e=>{ if(e.key==='Enter') bEnroll.click(); });
const bUnlabel=document.getElementById('unlabel');
bUnlabel.onclick=async()=>{
  const L=(inpLetter.value||'').trim().toUpperCase();
  if(!L){ setMsg('先填要删除的字母','bad'); return; }
  if(!confirm('把库里 '+L+' 的模板全部删掉？')) return;
  bUnlabel.disabled=true;
  try{
    const j=await doLabel('/unlabel');
    if(!j) return;
    setMsg(j.ok?j.title:(j.title||'删除失败'), j.ok?'ok':'bad');
    if(j.ok) inpLetter.value='';
  }finally{ bUnlabel.disabled=false; }
};

document.getElementById('snap').onclick=async()=>{
  try{
    const j=await (await fetch('/snapshot',{method:'POST'})).json();
    if(j.error){ setMsg(j.error,'bad'); return; }
    res.style.display='block';
    res.innerHTML='<h3>'+j.title+'</h3>'
      +(j.hints||[]).map(h=>'<div class="hint" style="word-break:break-all">'+h+'</div>').join('');
    setMsg('已存图，按下面那行命令去点分区','ok');
  }catch(e){ setMsg('存图失败：'+e.message,'bad'); }
};

// ── 摄像头：默认挑内建，别让"连续互通相机"把 iPhone 当成本机镜头 ────────────
const PHONEISH=/iphone|ipad|连续互通|continuity|desk ?view|桌面视图/i;
const BUILTIN=/facetime|built-?in|内建|macbook|imac|studio display/i;

async function listCams(activeId){
  let devs=[];
  try{ devs=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='videoinput'); }
  catch(e){ return; }
  if(!devs.length) return;
  const named=devs.map((d,i)=>({id:d.deviceId,label:d.label||('摄像头 '+(i+1))}));
  // 内建排前面，手机/连续互通排后面
  named.sort((a,b)=>{
    const pa=PHONEISH.test(a.label)?1:0, pb=PHONEISH.test(b.label)?1:0;
    if(pa!==pb) return pa-pb;
    const ba=BUILTIN.test(a.label)?0:1, bb=BUILTIN.test(b.label)?0:1;
    return ba-bb;
  });
  selCam.innerHTML=named.map(d=>'<option value="'+d.id+'">'+d.label
    +(PHONEISH.test(d.label)?'（手机/连续互通）':'')+'</option>').join('');
  if(activeId) selCam.value=activeId;
  const chosen=named.find(d=>d.id===activeId)||named[0];
  document.getElementById('foot').textContent =
    '画面来自本机摄像头：'+chosen.label+' · 只在 127.0.0.1 上提供服务 · 关掉这个终端即停止';
}

async function openCam(deviceId){
  if(stream) stream.getTracks().forEach(t=>t.stop());
  const want = {width:{ideal:1280},height:{ideal:720}};
  let s;
  try{
    s = await navigator.mediaDevices.getUserMedia(
      deviceId ? {video:Object.assign({deviceId:{exact:deviceId}},want),audio:false}
               : {video:want,audio:false});
  }catch(e){
    // 记着的那个设备已经拔了/换成别的机器了：deviceId 是**精确约束**，
    // 找不到就直接失败，页面会一直停在"拿不到摄像头"。退一步用默认设备。
    if(deviceId && (e.name==='OverconstrainedError'||e.name==='NotFoundError'||e.name==='NotReadableError')){
      localStorage.removeItem('rebench.cam');
      s = await navigator.mediaDevices.getUserMedia({video:want,audio:false});
    } else throw e;
  }
  stream=s;
  v.srcObject=stream;
  await v.play();
  const id=stream.getVideoTracks()[0].getSettings().deviceId;
  try{ localStorage.setItem('rebench.cam', id); }catch(e){}
  await listCams(id);
  return id;
}

selCam.onchange=async()=>{
  try{ await openCam(selCam.value); setMsg('已切换摄像头，等第一帧…'); }
  catch(e){ setMsg('切换摄像头失败：'+e.name+' '+e.message,'bad'); }
};

(async()=>{
  try{
    // 先按默认设备拿一次流：不拿到权限，enumerateDevices 拿不到设备名
    const id=await openCam(localStorage.getItem('rebench.cam')||'');
    if(id) localStorage.setItem('rebench.cam',id);
    setMsg('摄像头已就绪，开始逐帧检测');
    t0=performance.now();
    // 250ms 太密了：每个周期要 toBlob(720p) + 上传 + 解码回传的标注图，
    // 在 MacBook Air 上浏览器主线程基本被占满，点按钮都会顿一下。
    // 380ms ≈ 2.6fps，判定用不着更高，手感立刻回来。
    setInterval(pump,380);
  }catch(e){
    const tip=(e.name==='NotAllowedError')
      ? '浏览器或系统拒绝了摄像头权限：点地址栏左侧的小图标允许，或在「系统设置 → 隐私与安全性 → 摄像头」里给浏览器打勾'
      : (e.name==='NotFoundError' ? '没找到可用摄像头' : (e.message||''));
    setMsg('拿不到摄像头：'+e.name+' — '+tip,'bad');
  }
})();
</script></body></html>"""


class MacHandler(phone_live.Handler):
    """只在 "/" 上换成 Mac 版页面，其余接口/行为原样继承。"""

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path in ("/", "/index.html"):
            body = MAC_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path == "/snapshot":
            self._snapshot()
            return
        super().do_POST()

    def _snapshot(self) -> None:
        """把当前这帧原样存成 jpg，专门给分区标定用。

        摄像头权限在终端这条链路上经常拿不到，标定工具就开不了摄像头；
        但服务端手上一直有画面，所以让它吐一张出来 —— 标定工具 `--image`
        读这张图，坐标系和实时帧完全一致。
        """
        frame = self.session.last_frame
        if frame is None:
            self._json({"ok": False, "error": "还没有画面，等摄像头出第一帧再存"}, 400)
            return
        out_dir = RECORDS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"mac-{datetime.now():%Y%m%d-%H%M%S}.jpg"
        if not cv2.imwrite(str(path), frame):
            self._json({"ok": False, "error": f"写文件失败：{path}"}, 500)
            return
        # 提示里用相对路径：这行命令是要照着敲的，绝对路径太长没法看
        try:
            shown = path.resolve().relative_to(Path.cwd().resolve())
        except ValueError:
            shown = path
        self._json({
            "ok": True,
            "title": f"已存 {shown}",
            "path": str(shown),
            "hints": [
                "标定分区：",
                f"../.venv-vision/bin/python -m vision.tools.calibrate --image {shown} "
                "--config vision/config/mac.json --rect --zones A,B,C,TEMP",
                "标完重启本服务，新分区才生效",
            ],
        })



def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Mac 本机摄像头实时视觉（HTTP，仅本机）")
    p.add_argument("--config", default="vision/config/desktop.json")
    p.add_argument("--output", default=None)
    p.add_argument("--min-conf", type=float, default=0.8)
    p.add_argument("--port", type=int, default=8300)
    p.add_argument("--host", default="127.0.0.1",
                   help="默认只监听本机；改成 0.0.0.0 前先想清楚，摄像头画面会进局域网")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = p.parse_args(argv)

    session = phone_live.LiveSession(args.config, args.output, args.min_conf)
    session.last_msg = "等待本机摄像头…"
    MacHandler.session = session

    httpd = ThreadingHTTPServer((args.host, args.port), MacHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 56, flush=True)
    print(f"[MAC] 打开 {url}", flush=True)
    print("[MAC] 首次会问摄像头权限：浏览器里点「允许」", flush=True)
    print("[MAC] 只监听 127.0.0.1，局域网里别的设备打不开", flush=True)
    print("=" * 56, flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[MAC] 退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
