import os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scene_store
class SceneTests(unittest.TestCase):
 def test_version_and_staleness(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,ROBOT_SCENE_FILE=d+'/scene.json'):
   self.assertFalse(scene_store.read()['ok'])
   a=scene_store.publish({'ok':True,'objects':[]},time.time())
   self.assertTrue(scene_store.read(a['scene_id'])['ok'])
   scene_store.publish({'ok':True,'objects':[{'id':'new'}]},time.time())
   self.assertFalse(scene_store.read(a['scene_id'])['ok'])
   scene_store.publish({'ok':True,'objects':[]},time.time()-5)
   self.assertTrue(scene_store.read()['stale'])
if __name__=='__main__':unittest.main()
