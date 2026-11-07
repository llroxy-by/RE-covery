import unittest
from unittest.mock import patch
import gripper
class Robot:
 def __init__(self):self.calls=[]
 def set_gripper_command(self,*args):self.calls.append(args);return 1
class GripperTests(unittest.TestCase):
 def test_guards_and_units(self):
  r=Robot();state=dict(available=True,is_moving=False,width_m=.02)
  with patch.object(gripper,'read_gripper',return_value=state):
   req=dict(width_mm=80,velocity_mps=.01,effort=5);v=gripper.send_gripper(r,req,1)
   self.assertEqual(r.calls[0],('left_gripper',.08,.01,5.,False));self.assertTrue(v['accepted'])
   for key,value in [('width_mm',121),('width_mm',float('nan')),('velocity_mps',0),('effort',100)]:
    with self.assertRaises(ValueError):gripper.send_gripper(r,dict(req,**{key:value}),1)
   self.assertEqual(len(r.calls),1)
  with patch.object(gripper,'read_gripper',return_value=dict(available=False,error='stale')):
   with self.assertRaises(ValueError):gripper.send_gripper(r,req,1)
if __name__=='__main__':unittest.main()
