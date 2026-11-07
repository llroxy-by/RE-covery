"""Left HUILING gripper adapter; nonblocking position command, no auto motion."""
import math,re,time
_gripper_stamps={}
def read_gripper(robot,side='left'):
    try:
        if side not in ('left','right'):raise ValueError('无效夹爪侧别')
        with open('/data/galbot/config/robot_body.toml') as f:cfg=f.read()
        left=re.search(r'cur_left_arm_end_tool\s*=\s*"([^"]+)"',cfg)
        right=re.search(r'cur_right_arm_end_tool\s*=\s*"([^"]+)"',cfg)
        tools={'left':left.group(1) if left else 'unknown','right':right.group(1) if right else 'unknown'}
        # Both physical grippers were confirmed on site; config reports a stale right tool.
        state=robot.get_gripper_state(side+'_gripper')
        if state is None:return dict(available=False,tools=tools,error='夹爪未返回反馈')
        stamp=int(state.timestamp_ns);now=time.monotonic();old=_gripper_stamps.get(side)
        if not old or old[0]!=stamp:_gripper_stamps[side]=(stamp,now)
        fresh=stamp>0 and now-_gripper_stamps[side][1]<1.5
        vals=[float(state.width),float(state.velocity),float(state.effort)]
        if not all(math.isfinite(v) for v in vals):raise ValueError('夹爪反馈包含非法数值')
        return dict(available=fresh,tools=tools,width_m=vals[0],velocity_mps=vals[1],effort=vals[2],is_moving=bool(state.is_moving),timestamp_ns=stamp,error=None if fresh else '夹爪反馈已过期')
    except Exception as e:return dict(available=False,error=str(e))
def send_gripper(robot,req,success):
    width=float(req['width_mm'])/1000.;speed=float(req['velocity_mps']);effort=float(req['effort'])
    if not all(math.isfinite(v) for v in [width,speed,effort]):raise ValueError('夹爪参数必须为有限数值')
    if not 0<=width<=.12:raise ValueError('开口宽度须在 0–120mm 内')
    if not .005<=speed<=.05:raise ValueError('夹爪速度须在 0.005–0.05m/s 内')
    if not 1<=effort<=10:raise ValueError('夹持参数须在 1–10 内')
    side=req.get('side','left')
    if side not in ('left','right'):raise ValueError('无效夹爪侧别')
    state=read_gripper(robot,side)
    if not state['available']:raise ValueError(state.get('error','夹爪不可用'))
    if state['is_moving']:raise ValueError('夹爪正在动作，请等待反馈停止')
    status=robot.set_gripper_command(side+'_gripper',width,speed,effort,False)
    if status!=success:raise RuntimeError(str(status))
    return dict(accepted=True,target_width_mm=width*1000.,duration=abs(width-state['width_m'])/speed)
