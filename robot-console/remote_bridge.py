"""Runs in memory over SSH; no robot files are installed."""
import json,sys,time,threading,math
import xml.etree.ElementTree as ET
LIMIT_PATH="/data/galbot/config/galbot_description/galbot_one_golf_description/galbot_one_golf_cali_user.urdf"
def load_limits():
    root=ET.parse(LIMIT_PATH).getroot()
    return {j.get("name"):(float(j.find("limit").get("lower")),float(j.find("limit").get("upper"))) for j in root.findall("joint") if j.find("limit") is not None and j.find("limit").get("lower") is not None}
limits=load_limits()
posture_model=ET.parse(LIMIT_PATH).getroot()
from galbot_sdk.g1 import GalbotRobot,ControlStatus
robot=GalbotRobot(); lock=threading.RLock(); last=time.monotonic(); moving=False; baseline={}; last_stamps={}
position_groups=('head','left_arm','right_arm','leg')
active_position_joints=set()
base_v=0.;base_w=0.; base_deadline=0.; base_seq=0; base_error=None; base_error_at=0.; base_ctrl='未知'
def emit(obj): print('KITT_JSON '+json.dumps(obj),flush=True)
def sname(status):
    try:return ControlStatus(status).name
    except Exception:return str(status)
def active_base_controller():
    fn=getattr(robot,'get_active_controller',None)
    if fn is None:return '未知'
    try:return str(fn('chassis'))
    except Exception as e:return '未知('+str(e)+')'
def states():
    result=[]
    for s in robot.get_joint_states([],[]):
        if not any(s.joint_name.startswith(g+'_joint') for g in position_groups+('chassis',)):continue
        p=float(s.position)
        if not math.isfinite(p):continue
        stamp=int(s.timestamp_ns); old=last_stamps.get(s.joint_name)
        if old is None or old[0]!=stamp:last_stamps[s.joint_name]=(stamp,time.monotonic())
        fresh=stamp>0 and time.monotonic()-last_stamps[s.joint_name][1]<1.5
        baseline.setdefault(s.joint_name,p)
        result.append(dict(name=s.joint_name,position=p,velocity=float(s.velocity),fresh=fresh,baseline=baseline[s.joint_name],limits=limits.get(s.joint_name),controllable=s.joint_name in active_position_joints and s.joint_name in limits,group=s.joint_name.rsplit('_joint',1)[0]))
    return result
def watchdog():
    global moving,base_v,base_w,base_error,base_error_at,base_ctrl
    while True:
        time.sleep(.1)
        with lock:
            if base_w or base_v:
                try:
                    expired=time.monotonic()>base_deadline
                    status=robot.stop_base() if expired else robot.set_base_velocity([base_v,0.,0.],[0.,0.,base_w],0.)
                    if status!=ControlStatus.SUCCESS:
                        base_ctrl=active_base_controller()
                        base_error=('停止' if expired else '速度')+'指令被底盘控制器拒绝：'+sname(status)+' · 当前控制器 '+base_ctrl
                        base_error_at=time.time();base_v=0.;base_w=0.
                        try:robot.stop_base()
                        except Exception:pass
                except Exception as e:
                    base_error='底盘指令异常：'+str(e);base_error_at=time.time();base_v=0.;base_w=0.
                    try:robot.stop_base()
                    except Exception:pass
        if moving and time.monotonic()-last>1.5:
            with lock:
                robot.stop_trajectory_execution();moving=False
try:
    if not robot.init():raise RuntimeError('SDK 初始化失败')
    active_position_joints={name for g in position_groups for name in robot.get_joint_names(True,[g])}
    time.sleep(1)
    threading.Thread(target=watchdog,daemon=True).start()
    emit({'ready':True})
    for line in sys.stdin:
        try:
            req=json.loads(line); action=req.get('action'); last=time.monotonic()
            with lock:
                if action=='state':
                    js=states()
                    posture=detect_posture(posture_model,js) if 'detect_posture' in globals() else None
                    try:
                        right_state=robot.get_gripper_state('right_gripper')
                        right_diagnostic=None if right_state is None else {'width_m':float(right_state.width),'is_moving':bool(right_state.is_moving),'timestamp_ns':int(right_state.timestamp_ns)}
                    except Exception as e:right_diagnostic={'error':str(e)}
                    try:alive_plan=liveness_plan(posture_model,js);alive_error=None
                    except Exception as e:alive_plan=None;alive_error=str(e)
                    result={'alive_plan':alive_plan,'alive_error':alive_error,'joints':js,'posture':posture,'gripper':read_gripper(robot) if 'read_gripper' in globals() else None,'right_gripper_diagnostic':right_diagnostic,'right_gripper':read_gripper(robot,'right') if 'read_gripper' in globals() else None,'base':robot.get_base_velocity(),'base_error':base_error,'base_error_at':base_error_at,'base_controller':base_ctrl}
                elif action=='gripper':
                    if base_w or base_v:raise ValueError('请先停止底盘')
                    result=send_gripper(robot,req,ControlStatus.SUCCESS)
                elif action=='base':
                    sequence=int(req['sequence'])
                    if sequence<=base_seq:raise ValueError('过期底盘指令已拒绝')
                    base_seq=sequence
                    w=float(req['w']);v=float(req.get('v',0.))
                    if not math.isfinite(v) or abs(v)>.2:raise ValueError('底盘前后速度须在 ±0.2 m/s 内')
                    if not math.isfinite(w) or abs(w)>.3:raise ValueError('底盘转速须在 ±0.3 rad/s 内')
                    if w or v:
                        if not robot.get_base_velocity():raise ValueError('底盘状态不可用')
                        base_error=None;base_error_at=0.
                        base_v=v;base_w=w;base_deadline=time.monotonic()+.6
                    else:
                        base_v=0.;base_w=0.;status=robot.stop_base()
                        if status!=ControlStatus.SUCCESS:
                            base_ctrl=active_base_controller()
                            base_error='停止指令被底盘控制器拒绝：'+sname(status)+' · 当前控制器 '+base_ctrl
                            base_error_at=time.time()
                            raise RuntimeError(base_error)
                    result={'accepted':True,'w':w,'v':v}
                elif action=='stop':
                    base_v=0.;base_w=0.;base_status=robot.stop_base()
                    status=robot.stop_trajectory_execution();moving=False
                    if base_status!=ControlStatus.SUCCESS:raise RuntimeError('停止指令被底盘控制器拒绝：'+sname(base_status)+' · 当前控制器 '+active_base_controller())
                    if status!=ControlStatus.SUCCESS:raise RuntimeError('轨迹停止失败：'+sname(status))
                    result={'stopped':True}
                elif action=='move':
                    if base_w or base_v:raise ValueError('请先停止底盘旋转')
                    name=req['name'];delta=float(req['delta']);speed=float(req['speed'])
                    if not math.isfinite(delta) or abs(delta)==0:raise ValueError('角度必须为非零有限数值')
                    if not math.isfinite(speed) or not .01<=speed<=.20:raise ValueError('速度必须为 0.01–0.20 rad/s')
                    joint=next((s for s in states() if s['name']==name),None)
                    if not joint or not joint['fresh']:raise ValueError('关节状态过期或不可用')
                    if not joint['controllable']:raise ValueError('该关节为被动关节或缺少限位，不支持位置控制')
                    target=joint['position']+math.radians(delta)
                    bounds=limits.get(name)
                    if not bounds or not bounds[0]<=target<=bounds[1]:raise ValueError('目标超出该关节模型限位')
                    status=robot.set_joint_positions([target],[],[name],False,speed,abs(math.radians(delta))/speed+10.0)
                    if status!=ControlStatus.SUCCESS:raise RuntimeError(str(status))
                    moving=True;result={'accepted':True,'target':target,'duration':abs(math.radians(delta))/speed}
                else:raise ValueError('未知操作')
            emit({'id':req.get('id'),'ok':True,**result})
        except Exception as e:emit({'id':req.get('id') if isinstance(req,dict) else None,'ok':False,'error':str(e)})
finally:
    if base_w or base_v:robot.stop_base()
    if moving:robot.stop_trajectory_execution()
    robot.request_shutdown();robot.wait_for_shutdown();robot.destroy()
