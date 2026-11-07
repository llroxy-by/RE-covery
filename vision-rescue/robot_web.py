"""Local robot camera dashboard, reusing the existing detection endpoint."""
import os
import math
import json, threading, time, urllib.request
import cv2, numpy as np
from vision.vlm import _encode, _extract_text
from pathlib import Path
from http.server import ThreadingHTTPServer
from live.server import Handler, Bench
from grasp_preflight import inspect_target
from robot_link import RobotAgent

PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>机器人视觉观察台</title>
<style>body{background:#10151e;color:#eef3fa;font:16px system-ui;margin:32px}main{display:grid;grid-template-columns:3fr 1fr;gap:24px}canvas{width:100%;background:#000;border-radius:16px}aside{background:#1c2533;padding:24px;border-radius:16px}h1{font-size:26px}#letters{font-size:48px;color:#65e1ad}p{line-height:1.7}small{color:#aab9cf}.stack{display:flex;flex-direction:column;gap:20px}.card{background:#1c2533;padding:16px;border-radius:16px}.card h2{font-size:17px;margin:0 0 10px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}button{background:#2b3a4f;color:#eef3fa;border:0;padding:9px 14px;border-radius:9px;font:inherit;cursor:pointer}button:hover{background:#3a4d67}button:disabled{opacity:.5;cursor:default}#dview{cursor:crosshair;touch-action:none}#three{font-size:15px;line-height:1.6;white-space:pre-wrap}.ok{color:#65e1ad}.warn{color:#ffbc66}.bad{color:#ff8a8a}</style>
<h1>机器人视觉观察台</h1><p id="status">正在连接机器人相机…</p><main><div class="stack"><canvas id="view"></canvas><section class="card"><h2>手臂深度视图 · 在此框选抓取目标</h2><canvas id="dview"></canvas><div class="row"><button id="drefresh">刷新深度视图</button><button id="dreset">清除选区</button><span id="dstate">未加载</span></div></section></div><aside><h2>当前识别</h2><div id="letters">等待画面</div><p id="model"></p><p id="detail"></p><p>在实时画面拖拽框选识别区域</p><button id="resetroi">恢复全画面</button><p id="roiinfo">当前：全画面</p><h2>三维目标</h2><button id="graspcheck">定位选区三维坐标</button><div id="three">尚未定位</div><p><a href="http://127.0.0.1:8765/" target="_blank" style="color:#7fc0ff">打开已有关节控制台</a></p><h2>物品类型</h2><button id="scan">识别当前物品</button><canvas id="objectview" hidden></canvas><p id="objects">点击识别（模型需要数秒）</p><small>头部左相机 · 字母积木识别<br>三维定位来自左手臂深度相机，只读不运动。<br>识别运行在本机，摄像头运行在机器人。</small></aside></main>
<script>
const c=document.querySelector('#view'),ctx=c.getContext('2d'),s=document.querySelector('#status'),l=document.querySelector('#letters');
let busy=false,last=0,seq=0,decoding=false,pending=null,lastDetect=0,latestBlob=null;
const crop=document.createElement('canvas');
let boxes=[],boxesAt=0,roi=null,drag=null,roiVersion=0;
c.style.touchAction='none';c.style.cursor='crosshair';
function point(e){const r=c.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))]}
function selection(a,b){return [Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1])]}
c.onpointerdown=e=>{if(!latestBlob)return;c.setPointerCapture(e.pointerId);drag={start:point(e),end:point(e)}};
c.onpointermove=e=>{if(drag)drag.end=point(e)};
c.onpointerup=e=>{if(!drag)return;const r=selection(drag.start,point(e));drag=null;if(r[2]*c.width<20||r[3]*c.height<20)return;roi=r;roiVersion++;boxes=[];document.querySelector('#roiinfo').textContent='当前：已框选区域';document.querySelector('#objectview').hidden=true;document.querySelector('#objects').textContent='区域已更改，请重新识别'};
c.onpointercancel=()=>{drag=null};
document.querySelector('#resetroi').onclick=()=>{roi=null;roiVersion++;boxes=[];document.querySelector('#roiinfo').textContent='当前：全画面';document.querySelector('#objectview').hidden=true};
async function selectedImage(blob,region){const im=await createImageBitmap(blob);const r=region||[0,0,1,1];const x=Math.round(r[0]*im.width),y=Math.round(r[1]*im.height),w=Math.min(im.width-x,Math.max(1,Math.round(r[2]*im.width))),h=Math.min(im.height-y,Math.max(1,Math.round(r[3]*im.height)));const target=document.createElement('canvas');target.width=w;target.height=h;target.getContext('2d').drawImage(im,x,y,w,h,0,0,w,h);im.close();const result=await new Promise(resolve=>target.toBlob(resolve,'image/jpeg',0.95));return {blob:result,x,y,w,h}}
const dv=document.querySelector('#dview'),dctx=dv.getContext('2d');
let droi=null,ddrag=null,dbase=null;
function dpoint(e){const r=dv.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))]}
function drawDepth(){if(!dbase)return;dctx.drawImage(dbase,0,0,dv.width,dv.height);const r=ddrag?selection(ddrag.s,ddrag.e):droi;if(!r)return;dctx.save();dctx.strokeStyle='#4aa8ff';dctx.lineWidth=3;dctx.setLineDash([10,5]);dctx.strokeRect(r[0]*dv.width,r[1]*dv.height,r[2]*dv.width,r[3]*dv.height);dctx.restore()}
dv.onpointerdown=e=>{if(!dbase)return;dv.setPointerCapture(e.pointerId);ddrag={s:dpoint(e),e:dpoint(e)}};
dv.onpointermove=e=>{if(ddrag){ddrag.e=dpoint(e);drawDepth()}};
dv.onpointerup=e=>{if(!ddrag)return;const r=selection(ddrag.s,dpoint(e));ddrag=null;if(r[2]*dv.width<16||r[3]*dv.height<16){drawDepth();return}droi=r;drawDepth();document.querySelector('#three').textContent='已框选，点「定位选区三维坐标」'};
dv.onpointercancel=()=>{ddrag=null;drawDepth()};
document.querySelector('#dreset').onclick=()=>{droi=null;document.querySelector('#three').textContent='尚未定位';drawDepth()};
document.querySelector('#drefresh').onclick=async()=>{const st=document.querySelector('#dstate'),btn=document.querySelector('#drefresh');btn.disabled=true;st.textContent='正在取深度…（约 1 秒）';try{const r=await fetch('/depth',{cache:'no-store'});const d=await r.json();if(!r.ok||!d.ok)throw Error(d.error||'取深度失败');const img=new Image();img.onload=()=>{dv.width=img.width;dv.height=img.height;dbase=img;drawDepth()};img.src='data:image/jpeg;base64,'+d.jpeg;st.textContent='有效深度 '+d.valid_pct+'% · 取帧 '+d.ms+' ms';}catch(e){st.textContent='刷新失败：'+e.message}finally{btn.disabled=false}};
document.querySelector('#graspcheck').onclick=async()=>{const out=document.querySelector('#three');if(!droi){out.textContent='请先在上方深度视图里框选目标';return}out.textContent='正在解算…';try{const r=await fetch('/target3d',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({roi:droi})});const d=await r.json();if(!d.ok)throw Error(d.error||'解算失败');const ik=d.ik?d.ik.status:'-';out.textContent='XYZ (base_link)  '+d.xyz.map(v=>v.toFixed(3)).join(' , ')+'\\n点数 '+d.n+' · 离散度 '+d.spread_mm+' mm · 画面年龄 '+d.age_ms+' ms\\n高度 z '+d.z_range[0]+' ~ '+d.z_range[1]+' m · 到末端 '+(d.dist_ee_m==null?'-':d.dist_ee_m)+' m\\nIK '+ik+'（'+d.reach_frame+'）';}catch(e){out.textContent='解算失败：'+e.message}};
function drawRegion(){const r=drag?selection(drag.start,drag.end):roi;if(!r)return;ctx.save();ctx.strokeStyle='#4aa8ff';ctx.lineWidth=3;ctx.setLineDash([10,5]);ctx.strokeRect(r[0]*c.width,r[1]*c.height,r[2]*c.width,r[3]*c.height);ctx.restore()}

function drawBoxes(context,items,color){context.save();context.lineWidth=3;context.font='bold 22px sans-serif';for(const item of items){const [x,y,w,h]=item.bbox;context.strokeStyle=color;context.strokeRect(x,y,w,h);const label=item.name;const width=context.measureText(label).width+12;const top=Math.max(0,y-30);context.fillStyle=color;context.fillRect(x,top,width,28);context.fillStyle='#07101a';context.fillText(label,x+6,top+22)}context.restore()}

async function render(){if(decoding||!pending)return;decoding=true;const blob=pending;pending=null;try{const img=await createImageBitmap(blob);if(c.width!==img.width||c.height!==img.height){c.width=img.width;c.height=img.height}ctx.drawImage(img,0,0);if(Date.now()-boxesAt<1000)drawBoxes(ctx,boxes,'#65e1ad');drawRegion();img.close()}finally{decoding=false;if(pending)requestAnimationFrame(render)}}
document.querySelector('#scan').onclick=async()=>{if(!latestBlob)return;const button=document.querySelector('#scan'),out=document.querySelector('#objects');button.disabled=true;out.textContent='正在识别本次画面…';const at=new Date().toLocaleTimeString(),snapshot=latestBlob,selectedRoi=roi&&roi.slice(),version=roiVersion;document.querySelector('#objectview').hidden=true;try{const selected=await selectedImage(snapshot,selectedRoi);const r=await fetch('/objects',{method:'POST',body:selected.blob,signal:AbortSignal.timeout(35000)});const d=await r.json();if(version!==roiVersion)return;if(!r.ok)throw Error(d.error||'识别失败');out.textContent=at+' 快照（非实时跟踪）：'+(d.objects.map(o=>o.name).join('、')||'未确认物品');const oc=document.querySelector('#objectview'),oi=await createImageBitmap(selected.blob);oc.width=oi.width;oc.height=oi.height;const ox=oc.getContext('2d');ox.drawImage(oi,0,0);oi.close();drawBoxes(ox,d.objects.map(o=>({name:o.name,bbox:[o.bbox[0]*oc.width,o.bbox[1]*oc.height,(o.bbox[2]-o.bbox[0])*oc.width,(o.bbox[3]-o.bbox[1])*oc.height]})),'#ffbc66');oc.hidden=false}catch(e){out.textContent='识别失败：'+e.message}finally{button.disabled=false}};
function connect(){const ws=new WebSocket('ws://127.0.0.1:8781');ws.binaryType='blob';
ws.onmessage=async e=>{last=Date.now();latestBlob=e.data;pending=e.data;render();s.textContent='相机已连接 · 实时画面';
if(busy||Date.now()-lastDetect<350)return;busy=true;lastDetect=Date.now();const started=Date.now(),version=roiVersion;let selected;
try{selected=await selectedImage(e.data,roi&&roi.slice());}catch(err){busy=false;return}
const detectionBlob=selected.blob;
try{const r=await fetch('/detect',{method:'POST',body:detectionBlob,signal:AbortSignal.timeout(15000)});const d=await r.json();if(Date.now()-last>4000)return;if(!d.ok)throw Error(d.error);if(version!==roiVersion)return;if(!d.busy&&Date.now()-started<1000){boxes=d.tracks.filter(t=>t.fresh).map(t=>({name:t.letter,bbox:[t.bbox[0]+selected.x,t.bbox[1]+selected.y,t.bbox[2],t.bbox[3]]}));boxesAt=started;}l.textContent=d.tracks.filter(t=>t.fresh).map(t=>t.letter).join(' · ')||'未确认字母';document.querySelector('#detail').textContent='检测耗时 '+d.ms+' ms · '+d.frame.join(' × ');const mr=await fetch('/model');const m=await mr.json();document.querySelector('#model').textContent=m.available?'模型已配置：'+m.model+' · 调用 '+m.calls+' 次': '模型不可用：'+m.why;}catch(err){l.textContent='识别异常';document.querySelector('#detail').textContent=err.message}finally{busy=false}};
ws.onclose=()=>{s.textContent='相机断开，正在重连';last=0;boxes=[];latestBlob=null;setTimeout(connect,2000)};ws.onerror=()=>ws.close();}
setInterval(()=>{if(Date.now()-last>4000){ctx.clearRect(0,0,c.width,c.height);l.textContent='等待新画面';s.textContent='暂无新帧 · 检查相机连接'}},1000);connect();
</script></html>'''
class RobotHandler(Handler):
    object_lock=threading.Lock()
    agent=None
    agent_lock=threading.Lock()
    def _agent(self):
        cls=type(self)
        with cls.agent_lock:
            if cls.agent is None:
                cls.agent=RobotAgent()
            return cls.agent
    def do_POST(self):
        if self.path == '/target3d':
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<4096:
                    raise ValueError('请求大小无效')
                data=json.loads(self.rfile.read(n))
                roi=data.get('roi')
                if (not isinstance(roi,list) or len(roi)!=4
                        or any(type(v) not in (int,float) or not math.isfinite(v) for v in roi)):
                    raise ValueError('请先在深度视图里框选目标')
                x,y,w,h=roi
                if min(x,y)<0 or min(w,h)<=0 or x+w>1.000001 or y+h>1.000001:
                    raise ValueError('选区超出画面')
                out=self._agent().target([x,y,w,h])
                return self._json(out,200 if out.get('ok') else 502)
            except (ValueError,TypeError) as exc:
                return self._json({'ok':False,'error':str(exc)},400)
        if self.path == '/grasp/preview':
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<4096:
                    raise ValueError('请求大小无效')
                data=json.loads(self.rfile.read(n))
                if not isinstance(data,dict):
                    raise ValueError('请求无效')
                return self._json(inspect_target(data))
            except (ValueError,TypeError) as exc:
                return self._json({'error':str(exc),'executable':False},400)
        if self.path != '/objects':
            return super().do_POST()
        try:
            n=int(self.headers.get('Content-Length','0'))
        except ValueError:
            return self._json({'error':'无效请求'},400)
        if not 0<n<=16*1024*1024:
            return self._json({'error':'图像大小无效'},400)
        body=self.rfile.read(n)
        if not self.object_lock.acquire(False):
            return self._json({'error':'正在识别，请稍后'},429)
        try:
            image=cv2.imdecode(np.frombuffer(body,np.uint8),cv2.IMREAD_COLOR)
            if image is None:
                return self._json({'error':'图像无效'},400)
            m=self.bench.reader.vlm
            if not m.api_key:
                return self._json({'error':'模型密钥未配置'},503)
            prompt='识别图中最多8个清晰可见的实体物品。不猜测模糊物体，不识别人身份。只返回JSON对象，格式为 {"objects":[{"name":"红色杯子","bbox":[0.1,0.2,0.4,0.8]}]}。bbox严格为相对整张图宽高归一化到0到1的左上x、左上y、右下x、右下y，紧贴物品边缘。没有可确认物品返回空数组。'
            payload={'model':m.model,'temperature':0,'max_tokens':500,'messages':[{'role':'user','content':[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+_encode(image,1280)}}]}]}
            req=urllib.request.Request(m.base_url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+m.api_key})
            with urllib.request.urlopen(req,timeout=28) as response:
                result=json.loads(response.read())
            text=_extract_text(result)
            parsed=json.loads(text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
            objects=[]
            for item in parsed.get('objects',[])[:8]:
                box=item.get('bbox',[])
                if (len(box)==4 and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and 0<=v<=1 for v in box)
                        and box[0]<box[2] and box[1]<box[3] and isinstance(item.get('name'),str)):
                    objects.append({'name':item['name'][:60],'bbox':box})
            self._json({'objects':objects})
        except Exception as exc:
            self._json({'error':'模型请求失败：'+type(exc).__name__},502)
        finally:
            self.object_lock.release()

    def do_GET(self):
        if self.path == '/':
            return self._send(PAGE.encode(),200,'text/html; charset=utf-8')
        if self.path == '/model':
            m=self.bench.reader.vlm
            return self._json({'available':m.available,'model':m.model,'calls':m.calls,'why':m.why_unavailable()})
        if self.path == '/depth':
            out=self._agent().view()
            if not out.get('ok'):
                return self._json({'ok':False,'error':out.get('error','取深度失败')},502)
            return self._json(out)
        return super().do_GET()

if __name__ == '__main__':
    os.chdir(Path(__file__).resolve().parent)
    RobotHandler.bench=Bench('vision/config/mac.json')
    RobotHandler.bench.zones={}
    httpd=ThreadingHTTPServer(('127.0.0.1',8410),RobotHandler)
    print('Robot dashboard: http://127.0.0.1:8410/',flush=True)
    httpd.serve_forever()
