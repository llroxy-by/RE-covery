"""Offline delayed-inventory regression; never initializes a real SDK."""
import ast,unittest
from pathlib import Path
from types import SimpleNamespace
class Inventory(unittest.TestCase):
 def test_delayed_inventory_and_failure_deny_motion(self):
  source=ast.parse(Path(__file__).with_name('remote_bridge.py').read_text())
  fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_position_inventory')
  now=[0.];mode=['empty'];calls=[]
  def names(active,groups):
   calls.append((active,groups))
   if mode[0]=='error':raise RuntimeError('offline')
   return ['right_arm_joint1'] if mode[0]=='ready' and groups==['right_arm'] else []
  env={'time':SimpleNamespace(monotonic=lambda:now[0]),'robot':SimpleNamespace(get_joint_names=names),'position_groups':('head','left_arm','right_arm','leg'),'last_inventory':-float('inf'),'active_position_joints':set()}
  exec(compile(ast.Module(body=[fn],type_ignores=[]),'<offline>','exec'),env)
  refresh=env['refresh_position_inventory'];refresh();self.assertEqual(env['active_position_joints'],set())
  mode[0]='ready';now[0]=.5;refresh();self.assertEqual(len(calls),4)
  now[0]=2.1;refresh();self.assertEqual(env['active_position_joints'],{'right_arm_joint1'});self.assertTrue(all(active is True for active,_ in calls))
  mode[0]='error';now[0]=4.2;refresh();self.assertFalse(env['active_position_joints']);self.assertIn('offline',env['inventory_error'])
if __name__=='__main__':unittest.main()
