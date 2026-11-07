"""FAULT 不应永久闩锁：一次控制器拒绝之后，下一次操作必须能重新下发。

用假 SDK 跑真实 remote_bridge.py，不接触硬件。
    python3 -B test_fault_recovery.py
"""
import unittest, tempfile, subprocess, sys, os, json, time, pathlib
from test_server import build_bridge

FAKE = '''import time, enum
class ControlStatus(enum.IntEnum):
 SUCCESS=1
 FAULT=2
 TIMEOUT=3
class S:
 joint_name='left_arm_joint1';position=0.;velocity=0.
 @property
 def timestamp_ns(self):return time.time_ns()
class GalbotRobot:
 calls=0
 def init(self):return True
 def get_joint_states(self,*a):return [S()]
 def get_joint_names(self,active,groups):return ['left_arm_joint1'] if 'left_arm' in groups else []
 def set_joint_positions(self,*a):return ControlStatus.SUCCESS
 def stop_trajectory_execution(self):return ControlStatus.SUCCESS
 def stop_base(self):return ControlStatus.SUCCESS
 def get_base_velocity(self):return {"angular_velocity":[0,0,0],"linear_velocity":[0,0,0]}
 def get_active_controller(self,group):return 'chassis_twist_ctrl'
 def set_base_velocity(self,*a):
  GalbotRobot.calls+=1
  print("DRIVE %d %s"%(GalbotRobot.calls,a),flush=True)
  return ControlStatus.FAULT if GalbotRobot.calls==1 else ControlStatus.SUCCESS
 def request_shutdown(self):pass
 def wait_for_shutdown(self):pass
 def destroy(self):pass
'''


class FaultRecovery(unittest.TestCase):
 def test_fault_does_not_latch(self):
  with tempfile.TemporaryDirectory() as d:
   source = build_bridge(d)
   (pathlib.Path(d) / 'galbot_sdk/g1.py').write_text(FAKE)
   proc = subprocess.Popen([sys.executable, '-B', '-u', '-c', source], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           env=dict(os.environ, PYTHONPATH=d))
   def call(**req):
    proc.stdin.write(json.dumps(req) + '\n'); proc.stdin.flush()
    while True:
     line = proc.stdout.readline()
     self.assertTrue(line, '桥接提前退出')
     if line.startswith('KITT_JSON '):
      obj = json.loads(line[10:])
      if obj.get('id') == req.get('id'):
       return obj
   try:
    self.assertTrue(call(id=1, action='base', sequence=1, v=.1, w=0)['ok'])
    time.sleep(.4)                       # 让看门狗真的发一次，命中 FAULT
    state = call(id=2, action='state')
    self.assertTrue(state['base_error'], '首次 FAULT 应被记录')
    self.assertIn('FAULT', state['base_error'])
    self.assertEqual(state['base_controller'], 'chassis_twist_ctrl')
    self.assertTrue(call(id=3, action='base', sequence=2, v=.1, w=0)['ok'], 'FAULT 后必须允许重新下发')
    time.sleep(.4)
    state = call(id=4, action='state')
    self.assertFalse(state['base_error'], '恢复成功后错误应清除')
   finally:
    proc.stdin.close(); proc.wait(timeout=5); proc.stdout.close(); proc.stderr.close()


if __name__ == '__main__':
 unittest.main()
