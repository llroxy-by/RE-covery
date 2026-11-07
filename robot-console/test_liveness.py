import unittest,xml.etree.ElementTree as ET
from posture import liveness_plan
class Liveness(unittest.TestCase):
 def fixture(self):
  xml='<robot>';joints=[]
  for side in ('left_arm','right_arm'):
   for i in range(1,8):
    parent='torso_base_link' if i==1 else side+'_link'+str(i-1)
    xml+=f'<joint name="{side}_joint{i}" type="revolute"><parent link="{parent}"/><child link="{side}_link{i}"/><axis xyz="0 1 0"/></joint>'
    joints.append(dict(name=side+'_joint'+str(i),position=0.,velocity=0.,fresh=True,limits=[-1,1]))
   xml+=f'<joint name="{side}_tip" type="fixed"><parent link="{side}_link7"/><child link="{side}_end_effector_mount_link"/><origin xyz="0.5 0 0"/></joint>'
  return ET.fromstring(xml+'</robot>'),joints
 def test_alternating_bounded_plan(self):
  model,js=self.fixture();p=liveness_plan(model,js)
  self.assertEqual([s['name'] for s in p],['left_arm_joint4']*2+['right_arm_joint4']*2)
  self.assertAlmostEqual(abs(p[0]['target']),__import__('math').radians(20));self.assertEqual(p[0]['speed_rad_s'],.2);self.assertGreater(p[0]['rise_mm'],1);self.assertEqual(p[1]['target'],0);self.assertEqual(p[3]['target'],0)
  js[0]['fresh']=False
  with self.assertRaises(ValueError):liveness_plan(model,js)
 def test_limits_refuse(self):
  model,js=self.fixture()
  for j in js:j['limits']=[0,0]
  with self.assertRaises(ValueError):liveness_plan(model,js)
if __name__=='__main__':unittest.main()
