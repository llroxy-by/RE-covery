"""Generate embedded visual kinematics from a LOCAL calibrated URDF. No network/motion."""
import argparse,json,re,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def extract(path):
 edges={j.find('child').get('link'):j for j in ET.parse(path).getroot().findall('joint')}
 seen=set();model=[]
 def visit(link,trail=()):
  if link=='torso_base_link':return
  if link in trail:raise ValueError('URDF cycle')
  j=edges[link];visit(j.find('parent').get('link'),trail+(link,))
  if j.get('name') in seen:return
  seen.add(j.get('name'));o=j.find('origin');a=j.find('axis')
  def vec(e,k,d):
   result=[float(v) for v in (e.get(k,d) if e is not None else d).split()]
   if len(result)!=3:raise ValueError('Expected three model coordinates')
   return result
  model.append(dict(name=j.get('name'),type=j.get('type'),parent=j.find('parent').get('link'),child=j.find('child').get('link'),xyz=vec(o,'xyz','0 0 0'),rpy=vec(o,'rpy','0 0 0'),axis=vec(a,'xyz','1 0 0')))
 for side in ('left_arm','right_arm'):visit(side+'_end_effector_mount_link')
 return model

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--urdf',type=Path);p.add_argument('--check',action='store_true');args=p.parse_args()
 model=extract(args.urdf) if args.urdf else json.loads((ROOT/'joint_visual_model.json').read_text())
 html=(ROOT/'index.html').read_text();core=(ROOT/'joint_visual.js').read_text()
 pattern=r'<script>\s*/\* Pure calibrated kinematics;.*?const armCalibratedModel=.*?;\s*</script>'
 block='<script>\n'+core+'\nconst armCalibratedModel='+json.dumps(model,separators=(',',':'))+';\n</script>'
 updated,n=re.subn(pattern,lambda _:block,html,count=1,flags=re.S)
 if n!=1:raise SystemExit('Could not find embedded visual module')
 if args.check:
  if updated!=html:raise SystemExit('Embedded visual module/model differs; run build_visual_model.py')
  print('Embedded visual module and model match source');return
 (ROOT/'index.html').write_text(updated)
 (ROOT/'joint_visual_model.json').write_text(json.dumps(model,indent=2))
 print('Updated local page and geometry; no robot connection or motion')
if __name__=='__main__':main()
