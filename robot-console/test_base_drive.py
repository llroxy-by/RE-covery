import unittest,tempfile,subprocess,sys,os,json,time,pathlib
from test_server import build_bridge
class Drive(unittest.TestCase):
 def test_translation_lease_and_stop(self):
  with tempfile.TemporaryDirectory() as d:
   source=build_bridge(d);p=pathlib.Path(d)/'galbot_sdk/g1.py';sdk=p.read_text().replace(' def set_base_velocity(self,*a):return 1',' def set_base_velocity(self,*a):print("DRIVE "+str(a),flush=True);return 1').replace(' def stop_base(self):return 1',' def stop_base(self):print("BASE_STOP",flush=True);return 1');p.write_text(sdk)
   proc=subprocess.Popen([sys.executable,'-B','-u','-c',source],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PYTHONPATH=d));logs=[]
   def read_result(id):
    while True:
     line=proc.stdout.readline();self.assertTrue(line);logs.append(line)
     if line.startswith('KITT_JSON '):
      obj=json.loads(line[10:])
      if obj.get('id')==id:return obj
   def call(id,**req):proc.stdin.write(json.dumps(dict(id=id,**req))+'\n');proc.stdin.flush();return read_result(id)
   try:
    self.assertFalse(call(1,action='base',sequence=1,v=.21,w=0)['ok'])
    self.assertTrue(call(2,action='base',sequence=2,v=.1,w=0)['ok'])
    time.sleep(.9);call(3,action='state')
    self.assertTrue(any('DRIVE ([0.1, 0.0, 0.0]' in l for l in logs));self.assertTrue(any('BASE_STOP' in l for l in logs))
    self.assertFalse(call(4,action='base',sequence=1,v=.1,w=0)['ok'])
    self.assertTrue(call(5,action='base',sequence=3,v=0,w=0)['ok'])
   finally:proc.stdin.close();proc.wait(timeout=5);proc.stdout.close();proc.stderr.close()
if __name__=='__main__':unittest.main()
