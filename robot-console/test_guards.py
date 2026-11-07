"""Exercise the actual bridge against a fake SDK; never accesses hardware."""
import subprocess,sys,tempfile,pathlib,json,unittest
class Guards(unittest.TestCase):
 def test_rejects_invalid_motion(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d)/'galbot_sdk';p.mkdir();(p/'__init__.py').write_text('')
   (p/'g1.py').write_text('''import time
class ControlStatus: SUCCESS=1
class S:
 joint_name='left_arm_joint1';position=0.;velocity=0.
 @property
 def timestamp_ns(self):return time.time_ns()
class Leg(S):joint_name='leg_joint1'
class Wheel(S):joint_name='chassis_joint1'
class GalbotRobot:
 def init(self):return True
 def get_joint_states(self,*a):return [S(),Leg(),Wheel()]
 def get_joint_names(self,active,groups):return ['left_arm_joint1'] if 'left_arm' in groups else ['leg_joint1'] if 'leg' in groups else []
 def set_joint_positions(self,*a):return 1
 def stop_trajectory_execution(self):return 1
 def stop_base(self):return 1
 def get_base_velocity(self):return {"angular_velocity":[0,0,0]}
 def set_base_velocity(self,*a):return 1
 def request_shutdown(self):pass
 def wait_for_shutdown(self):pass
 def destroy(self):pass
''')
   import os
   env=dict(os.environ,PYTHONPATH=d)
   reqs=[dict(id=1,action='move',name='left_arm_joint1',delta=200,speed=.02),dict(id=2,action='move',name='left_arm_joint1',delta=.5,speed=.21),dict(id=3,action='move',name='chassis_joint1',delta=.5,speed=.02),dict(id=4,action='move',name='left_arm_joint1',delta=10,speed=.02),dict(id=5,action='stop'),dict(id=6,action='base',w=.31,sequence=1),dict(id=7,action='base',w=.05,sequence=2),dict(id=8,action='base',w=0,sequence=3),dict(id=9,action='base',w=.05,sequence=2),dict(id=10,action='move',name='leg_joint1',delta=1,speed=.02)]
   fixture=pathlib.Path(d)/'limits.urdf';fixture.write_text('<robot><joint name="left_arm_joint1"><limit lower="-1" upper="1"/></joint><joint name="leg_joint1"><limit lower="-1" upper="1"/></joint><joint name="chassis_joint1"><limit lower="-1" upper="1"/></joint></robot>')
   source=pathlib.Path(__file__).with_name('remote_bridge.py').read_text().replace('/data/galbot/config/galbot_description/galbot_one_golf_description/galbot_one_golf_cali_user.urdf',str(fixture))
   r=subprocess.run([sys.executable,'-B','-c',source],input='\n'.join(map(json.dumps,reqs))+'\n',text=True,capture_output=True,env=env,timeout=8,check=True)
   results=[json.loads(x[10:]) for x in r.stdout.splitlines() if x.startswith('KITT_JSON ')][1:]
   self.assertEqual([x['ok'] for x in results],[False,False,False,True,True,False,True,True,False,True])
if __name__=='__main__':unittest.main()
