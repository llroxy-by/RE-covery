"""URDF forward kinematics in torso coordinates; no motion commands."""
import math

def _eye():return [[float(i==j) for j in range(4)] for i in range(4)]
def _mul(a,b):return [[sum(a[i][k]*b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
def _rot(axis,q):
    n=math.sqrt(sum(v*v for v in axis))
    if n<1e-12:raise ValueError('模型旋转轴无效')
    x,y,z=[v/n for v in axis];c=math.cos(q);s=math.sin(q);v=1-c
    m=_eye();r=[[x*x*v+c,x*y*v-z*s,x*z*v+y*s],[y*x*v+z*s,y*y*v+c,y*z*v-x*s],[z*x*v-y*s,z*y*v+x*s,z*z*v+c]]
    for i in range(3):m[i][:3]=r[i]
    return m

def _origin(j):
    o=j.find('origin');m=_eye()
    if o is None:return m
    xyz=[float(x) for x in o.get('xyz','0 0 0').split()];r,p,y=[float(x) for x in o.get('rpy','0 0 0').split()]
    m=_mul(_mul(_rot([0,0,1],y),_rot([0,1,0],p)),_rot([1,0,0],r))
    for i in range(3):m[i][3]=xyz[i]
    return m

def detect_posture(model,joints):
    by={j['name']:j for j in joints};edges={j.find('child').get('link'):j for j in model.findall('joint')}
    cache={'torso_base_link':_eye()}
    def tf(link):
        if link in cache:return cache[link]
        if link not in edges:raise ValueError('缺少躯干坐标链：'+link)
        j=edges[link];m=_origin(j);kind=j.get('type');name=j.get('name')
        if kind!='fixed':
            state=by.get(name)
            if not state or not state.get('fresh') or not math.isfinite(state['position']):raise ValueError('缺少有效关节反馈：'+name)
            a=j.find('axis');axis=[float(v) for v in (a.get('xyz','1 0 0') if a is not None else '1 0 0').split()]
            if kind in ('revolute','continuous'):m=_mul(m,_rot(axis,state['position']))
            else:raise ValueError('未支持的关节类型：'+kind)
        cache[link]=_mul(tf(j.find('parent').get('link')),m);return cache[link]
    out={'source':'URDF + live joint feedback','frame':'torso_base_link','arms':{}}
    for side in ('left_arm','right_arm'):
        try:
            if any(not by.get(side+'_joint'+str(i),{}).get('fresh') for i in range(1,8)):raise ValueError('手臂关节反馈不完整或过期')
            shoulder=tf(side+'_link1');wrist=tf(side+'_end_effector_mount_link')
            d=[wrist[i][3]-shoulder[i][3] for i in range(3)]
            dz=d[2];label='手腕高于肩部' if dz>.08 else '手腕低于肩部' if dz<-.08 else '手腕接近肩高'
            moving=any(abs(by[side+'_joint'+str(i)]['velocity'])>.015 for i in range(1,8))
            out['arms'][side]={'valid':True,'label':label,'raised':dz>.08,'moving':moving,'wrist_from_shoulder_m':d,'wrist_torso_m':[wrist[i][3] for i in range(3)]}
        except (ValueError,KeyError,TypeError) as e:out['arms'][side]={'valid':False,'label':'无法判断','reason':str(e)}
    head=by.get('head_joint2',{})
    out['head']={'valid':bool(head.get('fresh')),'pitch_joint_deg':math.degrees(head['position']) if head.get('fresh') else None,'moving':abs(head.get('velocity',0))>.015}
    return out

def liveness_plan(model,joints):
    """20 degree elbow motion raising each wrist; refuses unsuitable geometry."""
    current=detect_posture(model,joints);by={j['name']:j for j in joints};steps=[]
    for side in ('left_arm','right_arm'):
        arm=current['arms'][side]
        if not arm['valid'] or arm['moving']:raise ValueError('双臂需要有效且静止的反馈')
        name=side+'_joint4';j=by[name];options=[]
        for delta in (math.radians(20),-math.radians(20)):
            target=j['position']+delta
            if not j.get('limits') or not j['limits'][0]<=target<=j['limits'][1]:continue
            candidate=[dict(x,position=target) if x['name']==name else dict(x) for x in joints]
            proposed=detect_posture(model,candidate)['arms'][side]
            if not proposed['valid']:continue
            shift=[proposed['wrist_torso_m'][i]-arm['wrist_torso_m'][i] for i in range(3)]
            if shift[2]>.001:options.append((shift[2],target,math.sqrt(sum(v*v for v in shift))))
        if not options:raise ValueError('当前'+side+'姿态不适合小幅上下摆动，请先调整姿态')
        dz,target,displacement=max(options)
        steps.extend([dict(name=name,target=target,label=side+'上抬',rise_mm=dz*1000,displacement_mm=displacement*1000,amplitude_deg=20,speed_rad_s=.2),dict(name=name,target=j['position'],label=side+'回位')])
    return steps
