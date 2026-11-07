"""Read-only image target contract. No SDK imports or motion commands."""
import math

def inspect_target(request):
    roi=request.get('roi')
    size=request.get('frame')
    if not isinstance(roi,list) or len(roi)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in roi):
        raise ValueError('请先在画面上框选一个抓取目标')
    x,y,w,h=roi
    if min(x,y)<0 or min(w,h)<=0 or x+w>1.000001 or y+h>1.000001:
        raise ValueError('选区超出画面')
    if not isinstance(size,list) or len(size)!=2 or any(type(v) is not int or not 1<=v<=8192 for v in size):
        raise ValueError('画面尺寸无效')
    return {'executable':False,'stage':'image_target_only','camera':'HEAD_LEFT_CAMERA',
            'roi_normalized':roi,'frame':size,'center_px':[(x+w/2)*size[0],(y+h/2)*size[1]],
            'missing':['目标对应的同步深度及RGB对齐','当前相机内参及到机器人基座的变换','夹爪TCP、抓取朝向及开口','逆解、碰撞检查和分阶段到位检查','现场运动确认'],
            'message':'二维目标已接收；尚不能生成可执行抓取轨迹'}
