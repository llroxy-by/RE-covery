import http.server,json,subprocess,threading,queue,time,secrets,os,shlex,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
ROBOT=os.environ.get('ROBOT_HOST','192.168.167.46')
TOKEN=secrets.token_urlsafe(32); pending={}; seq=0; send_lock=threading.Lock(); last_browser=0; busy_until=0; fault=None; ready=False
p=None

def stderr_reader(proc):
 for line in proc.stderr:
  print('SDK:',line.rstrip(),flush=True)
def reader(proc):
 global fault,ready
 for line in proc.stdout:
  if line.startswith('KITT_JSON '):
   obj=json.loads(line[10:])
   if obj.get('ready'):ready=True
   q=pending.get(obj.get('id'))
   if q:q.put(obj)
 if p is proc:
  fault='机器人连接已断开'
  print('桥接已退出，ssh 返回码 %s'%proc.poll(),flush=True)
def call(action,**kw):
 global seq
 if os.environ.get("ROBOT_OFFLINE_REASON"):raise RuntimeError(os.environ["ROBOT_OFFLINE_REASON"])
 if not p or p.poll() is not None:raise RuntimeError('机器人桥接未连接')
 with send_lock:
  seq+=1;n=seq;q=queue.Queue();pending[n]=q
  try:p.stdin.write(json.dumps(dict(id=n,action=action,**kw))+'\n');p.stdin.flush()
  except (BrokenPipeError,OSError,ValueError):
   pending.pop(n,None);raise RuntimeError('机器人桥接未连接（SSH 管道已关闭）')
 try:

  try:r=q.get(timeout=6)
  except queue.Empty:raise RuntimeError('SDK 未返回数据：初始化或通信超时')
  if not r.get('ok'):raise RuntimeError(r.get('error','调用失败'))
  return r
 finally:pending.pop(n,None)
def watchdog():
 global busy_until,fault
 while True:
  time.sleep(.2)
  if busy_until and time.monotonic()-last_browser>1.2:
   try:call('stop')
   except Exception as e:fault=str(e)
   busy_until=0
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def out(self,obj,status=200):
  data=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
 def do_GET(self):
  if self.path=='/presets.js':
   b=(ROOT/'presets.js').read_bytes();self.send_response(200);self.send_header('Content-Type','application/javascript');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b)
  elif self.path=='/':
   b=(ROOT/'index.html').read_bytes().replace(b'__TOKEN__',TOKEN.encode());self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b)
  else:self.out({'error':'Not found'},404)
 def do_POST(self):
  global last_browser,busy_until,fault
  try:
   size=int(self.headers.get('Content-Length','0'))
   if not 0<=size<=65536:return self.out({'error':'请求过大'},409)
   body=self.rfile.read(size)
  except (ValueError,OSError):return self.out({'error':'无效请求'},400)
  if self.headers.get('X-Console-Token')!=TOKEN:return self.out({'error':'未授权'},403)
  if self.headers.get('Origin') not in (None,'http://127.0.0.1:8765','http://localhost:8765'):return self.out({'error':'来源不允许'},403)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size>4096:raise ValueError('请求过大')
   req=json.loads(body);action=req.get('action')
   if action not in ('state','move','stop','base','gripper'):raise ValueError('未知操作')
   if action=='state':last_browser=time.monotonic()
   if action=='gripper':
    if req.get('confirmed') is not True:raise ValueError('请确认夹爪周围无手指与障碍物')
    if fault or time.monotonic()-last_browser>1.2:raise ValueError('连接或状态不可用')
    if time.monotonic()<busy_until:raise ValueError('上一动作尚未结束')
    r=call('gripper',side=req.get('side','left'),width_mm=req['width_mm'],velocity_mps=req['velocity_mps'],effort=req['effort'])
   elif action=='base':
    if req.get('w') and (req.get('confirmed') is not True or time.monotonic()-last_browser>1.2):raise ValueError('请确认底盘现场安全与实时连接')
    if req.get('w') and time.monotonic()<busy_until:raise ValueError('请先完成关节运动')
    r=call('base',w=req['w'],sequence=req['sequence'])
   elif action=='move':
    if fault:raise ValueError(fault)
    if last_browser<=0 or time.monotonic()-last_browser>1.2:raise ValueError('状态已过期')
    if time.monotonic()<busy_until:raise ValueError('上一动作尚未结束')
    if req.get('confirmed') is not True:raise ValueError('请确认现场安全')
    busy_until=time.monotonic()+6
    try:r=call('move',name=req['name'],delta=req['delta'],speed=req['speed'])
    except Exception:
     busy_until=0;raise
    busy_until=time.monotonic()+r['duration']+.5
   else:
    r=call(action)
    if action=='stop':busy_until=0
   self.out(dict(r,mode='real',robot=ROBOT,sdk='1.10.0'))
  except Exception as e:self.out({'error':str(e)},409)
if __name__=='__main__':
 import socket
 def reachable(host=ROBOT,port=22,timeout=2.0):
  try:
   s=socket.create_connection((host,port),timeout)
   s.close();return True
  except (OSError,ValueError):return False
 MUX='/tmp/kitt-console-live.sock'
 def mux_ok():
  if os.environ.get("ROBOT_OFFLINE_REASON"):return False
  try:
   return subprocess.run(['ssh','-S',MUX,'-O','check','galbot@'+ROBOT],capture_output=True,timeout=8).returncode==0
  except Exception:return False
 if mux_ok():
  print('✓ SSH 主连接可用：%s'%ROBOT,flush=True)
 elif os.path.exists(MUX):
  print('SSH 主连接已失效（%s），请重新运行 ./start.sh'%ROBOT,flush=True)
 else:
  print('未找到 SSH 主连接，先运行 ./start.sh（只需输一次机器人密码）',flush=True)
 source=(ROOT/'posture.py').read_text()+'\n'+(ROOT/'gripper.py').read_text()+'\n'+(ROOT/'remote_bridge.py').read_text()
 command='env PYTHONPATH=/data/galbot/lib LD_LIBRARY_PATH=/data/galbot/lib python3 -B -u -c '+shlex.quote(source)
 def spawn_bridge():
  global p,fault,ready
  ready=False;fault=None
  p=subprocess.Popen(['ssh','-S',MUX,'-o','BatchMode=yes','-o','ConnectTimeout=5','-o','ConnectionAttempts=1','-o','ServerAliveInterval=2','-o','ServerAliveCountMax=2','galbot@'+ROBOT,command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
  threading.Thread(target=reader,args=(p,),daemon=True).start()
  threading.Thread(target=stderr_reader,args=(p,),daemon=True).start()
  return p
 def announce():
  for _ in range(30):
   if ready or p is None or p.poll() is not None:break
   time.sleep(.5)
  print('机器人 SDK 已就绪' if ready else '机器人 SDK 未就绪，页面会自动重试',flush=True)
 def supervisor():
  # SSH 主连接晚一点建好（或断线重连）时，自动把桥接拉起来，不用重启控制台
  while True:
   time.sleep(2)
   if p is None or p.poll() is not None:
    if mux_ok():spawn_bridge();print('桥接已连接（自动重连）',flush=True)
 threading.Thread(target=watchdog,daemon=True).start()
 if mux_ok():
  spawn_bridge();print('正在启动机器人桥接…',flush=True)
 threading.Thread(target=announce,daemon=True).start()
 threading.Thread(target=supervisor,daemon=True).start()
 print('http://127.0.0.1:8765',flush=True)
 try:http.server.ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
 except KeyboardInterrupt:pass
 finally:
  if p is None:sys.exit(0)
  try:p.stdin.close();p.wait(timeout=8)
  except Exception:p.terminate()
