import unittest,xml.etree.ElementTree as ET,math
from posture import detect_posture
class Posture(unittest.TestCase):
 def test_known_geometry_and_stale(self):
  xml='<robot>'
  for side in ['left_arm','right_arm']:
   for i in range(1,8):
    parent='torso_base_link' if i==1 else side+'_link'+str(i-1)
    xml+=f'<joint name="{side}_joint{i}" type="revolute"><parent link="{parent}"/><child link="{side}_link{i}"/><axis xyz="0 1 0"/></joint>'
   xml+=f'<joint name="{side}_tip" type="fixed"><parent link="{side}_link7"/><child link="{side}_end_effector_mount_link"/><origin xyz="1 0 0"/></joint>'
  model=ET.fromstring(xml+'</robot>');js=[dict(name=s+'_joint'+str(i),position=(-math.pi/2 if i==1 else 0),velocity=0,fresh=True) for s in ['left_arm','right_arm'] for i in range(1,8)]
  v=detect_posture(model,js);self.assertTrue(v['arms']['left_arm']['raised']);self.assertAlmostEqual(v['arms']['left_arm']['wrist_from_shoulder_m'][2],1)
  js[0]['position']=math.pi/2;self.assertFalse(detect_posture(model,js)['arms']['left_arm']['raised'])
  js[0]['fresh']=False;self.assertFalse(detect_posture(model,js)['arms']['left_arm']['valid'])
  self.assertFalse(detect_posture(model,[])['head']['valid'])
if __name__=='__main__':unittest.main()
