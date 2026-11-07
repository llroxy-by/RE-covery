"""Standalone scene API; contains no robot/SSH/motion bridge."""
import argparse,json,os,secrets,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import scene_store
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,data,status=200):
  payload=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
 def authorized(self):
  key=os.environ.get('ROBOT_SCENE_TOKEN','')
  if not key or not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+key):
   self.reply({'error':'unauthorized'},403);return False
  return True
 def do_GET(self):
  if not self.authorized():return
  from urllib.parse import urlparse,parse_qs
  if urlparse(self.path).path!='/api/scene':return self.reply({'error':'not found'},404)
  q=parse_qs(urlparse(self.path).query)
  self.reply(scene_store.read(q.get('scene_id',[None])[0]))
 def do_POST(self):
  if not self.authorized():return
  if self.path!='/api/scene':return self.reply({'error':'not found'},404)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if not 0<size<=65536:raise ValueError()
   d=json.loads(self.rfile.read(size));captured=float(d['captured_at'])
   if not d.get('ok') or not isinstance(d.get('tracks',d.get('objects')),list) or not 0<=time.time()-captured<60:raise ValueError()
   d=dict(d,scene_id=secrets.token_hex(16),published_at=time.time(),schema_version=1)
   self.reply(scene_store.save(d))
  except (ValueError,KeyError,TypeError):self.reply({'error':'invalid data or capture time'},400)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8790);a=p.parse_args()
 if not os.environ.get('ROBOT_SCENE_TOKEN'):p.error('ROBOT_SCENE_TOKEN required')
 if os.environ.get('ROBOT_SCENE_URL'):p.error('API host must not set ROBOT_SCENE_URL')
 ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
