"""单连接循环抓放：探测 -> 自锚定分类 -> 抓 -> 放进盒子 -> 重复。

为什么要这样写：
  1. 每步重新 SSH + 重新初始化 SDK 是 4-6 s，动作本身只要 1-2 s。
     这里只连一次，整个循环在一个进程里跑完。
  2. 深度平面拟合会整体偏 3-4 cm（视野里有地面/桌沿时会拉偏平面），
     所以高度必须用高度直方图众数自锚定，不能直接用 probe 的 h_cm。

安全：候选不合格就不抓；合爪后宽度/力矩不达标直接张开跳过，不抬升运输。
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from exec_transport import close, connect  # noqa: E402
from grasp_calc import plan_vertical_grasp  # noqa: E402
from pickplace_session import Session  # noqa: E402

TOOL_LEN = 0.189
HOVER = (0.720, 0.120, 1.090)      # 观察/待命位
BOXES = [(0.693, 0.000, 0.756),    # 框B：中心xy + 框沿绝对高
         (0.819, 0.046, 0.755)]    # 框A：用户 19:46 亲自指出，几何已验
MIN_BLOCK_CM, MAX_BLOCK_CM = 1.4, 4.8
MAX_AXIS_CM = 6.0                 # 只抓离探测中心足够近的，避免投影误差
WIDTH_MIN, EFFORT_MIN = 0.018, 15.0


def call(s, req):
    result = s.request(req)
    if not result.get('ok'):
        raise RuntimeError('SDK step failed; no subsequent motion or release: ' + str(result.get('error', result.get('status'))))
    return result


def probe_here(s, radius=0.16):
    r = call(s, {'cmd': 'probe', 'radius': radius, 'frames': 4, 'peaks': 12})
    if not r.get('ok'):
        return None
    g = np.array(r['grid'], float)
    g[g < -1] = np.nan
    vals = g[np.isfinite(g)]
    if vals.size < 200:
        return None
    hist, edges = np.histogram(vals, bins=np.arange(-10, 200, 5))
    mode_cm = edges[int(np.argmax(hist))] / 10.0 + 0.25
    r['_table_true'] = r['table_z'] + mode_cm / 100.0
    r['_mode_cm'] = mode_cm
    return r


def candidates(p):
    out = []
    for k in p.get('peaks') or []:
        h = k['h_cm'] - p['_mode_cm']
        if (MIN_BLOCK_CM <= h <= MAX_BLOCK_CM and k['dist_axis_cm'] <= MAX_AXIS_CM
                and 0.56 <= k['xy'][0] <= 0.94 and 0.040 <= k['xy'][1] <= 0.340):
            out.append({'xy': k['xy'], 'top_z': k['top_z'], 'h_cm': h, 'n': k['n'],
                        'dist': k['dist_axis_cm']})
    out.sort(key=lambda c: c['dist'] if 'dist' in c else abs(c['xy'][0])*0 + c['xy'][1]*0)
    return out


def move(s, x, y, z, table, tilt=0.0, speed=0.10, dev=8.0):
    r = call(s, {'cmd': 'move_checked', 'x': x, 'y': y, 'z': z, 'steps': 40,
                 'min_tip': round(table + 0.0126, 4), 'max_delta': 45, 'max_dev': dev,
                 'speed': speed, 'table_z': table, 'tilt': tilt})
    return r


def main():
    raise RuntimeError('Review copy: automatic execution disabled pending tool calibration, target identity binding, and height verification')
    rounds = 4
    proc, boot = connect(True)
    s = Session(proc, 'fast')
    print('CONNECTED boot=%sms' % boot.get('boot_ms'), flush=True)
    done = []
    try:
        for it in range(1, rounds + 1):
            t0 = time.time()
            mv = move(s, *HOVER, 0.718)
            if not mv.get('ok'):
                print('第%d轮 回待命位失败 %s' % (it, mv.get('error')), flush=True)
                break
            p = probe_here(s, 0.13)
            if p is None:
                print('第%d轮 探测失败' % it, flush=True)
                break
            cands = candidates(p)
            print('第%d轮 桌面(自锚定)=%.4f 众数=%.1fcm 候选=%d %s'
                  % (it, p['_table_true'], p['_mode_cm'], len(cands),
                     [(round(c['xy'][0], 3), round(c['xy'][1], 3), round(c['h_cm'], 1)) for c in cands[:5]]),
                  flush=True)
            if not cands:
                print('第%d轮 没有合格候选，结束' % it, flush=True)
                break
            c = cands[0]
            zstart = HOVER[2]
            refine = move(s, c['xy'][0], c['xy'][1], min(c['top_z'] + 0.22, 1.020), p['_table_true'])
            if refine.get('ok'):
                zstart = min(c['top_z'] + 0.22, 1.020)
                p2 = probe_here(s, 0.08)
                if p2:
                    best = None
                    for k in p2.get('peaks') or []:
                        hh = k['h_cm'] - p2['_mode_cm']
                        if 1.2 <= hh <= 5.0 and k['dist_axis_cm'] <= 6.0:
                            if best is None or k['n'] > best['n']:
                                best = {'xy': k['xy'], 'top_z': k['top_z'], 'h_cm': hh, 'n': k['n']}
                    if best and abs(best['xy'][0] - c['xy'][0]) < 0.05 and abs(best['xy'][1] - c['xy'][1]) < 0.05:
                        c = best
                        p['_table_true'] = p2['_table_true']
                        print('   复探修正 → (%.3f,%.3f) 高%.1fcm 桌面%.4f'
                              % (c['xy'][0], c['xy'][1], c['h_cm'], p['_table_true']), flush=True)
            plan = plan_vertical_grasp(c['top_z'], p['_table_true'], grip_depth=0.020)
            print('   目标 (%.3f,%.3f) 高%.1fcm top=%.3f → mount %.3f 包覆%.1fcm%s'
                  % (c['xy'][0], c['xy'][1], c['h_cm'], c['top_z'], plan['mount_z'],
                     plan['coverage_cm'], ' [clamp]' if plan['clamped'] else ''), flush=True)

            call(s, {'cmd': 'grip', 'w': 0.105, 'v': 0.07, 'eff': 3,
                     'wait_until_stopped': False, 'blocking': False, 'settle': 0.35})
            ok = True
            zs, ztop = [], zstart
            while ztop - 0.07 > plan['mount_z'] + 1e-6:
                ztop -= 0.07
                zs.append((round(ztop, 4), 0.08))
            zs.append((plan['mount_z'], 0.03))
            for z, sp in zs:
                r = move(s, c['xy'][0], c['xy'][1], z, p['_table_true'], speed=sp)
                if not r.get('ok'):
                    pl = r.get('plan') or {}
                    print('   下降失败 z=%.3f %s | min_tip=%s dq=%s dev=%s'
                          % (z, r.get('error'), pl.get('min_tip_z'), pl.get('max_delta_deg'), pl.get('max_dev_cm')), flush=True)
                    ok = False
                    break
            if not ok:
                r = move(s, c['xy'][0], c['xy'][1], plan['mount_z'] + 0.010, p['_table_true'], speed=0.03)
                ok = bool(r.get('ok'))
            if ok:
                g = call(s, {'cmd': 'grip', 'w': 0.012, 'v': 0.012, 'eff': 2,
                             'wait_until_stopped': False, 'blocking': False, 'settle': 0.6})
                w = float(g.get('width') or 0)
                eff = float(g.get('effort') or 0)
                held = (g.get('ok') and not g.get('moving', True) and eff >= EFFORT_MIN and w >= WIDTH_MIN)
                print('   合爪 宽%.4f 力矩%.1f → %s' % (w, eff, '夹住' if held else '没夹住'), flush=True)
                if held:
                    tl = p['_table_true']   # 抬升时下表按指尖反推，避免边界误判
                    up = None
                    zc = plan['mount_z']
                    while zc < 1.088:
                        zc = min(round(zc + 0.07, 4), 1.090)
                        up = move(s, c['xy'][0], c['xy'][1], zc, tl)
                        if not up.get('ok'):
                            break
                    if up and up.get('ok'):
                        tr = move(s, BOXES[len(done) % len(BOXES)][0], BOXES[len(done) % len(BOXES)][1], 1.095, tl)
                        if tr.get('ok'):
                            bx, by, rim = BOXES[len(done) % len(BOXES)]
                            dn = move(s, bx, by, round(rim + 0.219, 4), tl, speed=0.04)
                            if not dn.get('ok'):
                                raise RuntimeError('Placement failed; keeping gripper closed')
                            call(s, {'cmd': 'grip', 'w': 0.105, 'v': 0.05, 'eff': 2,
                                     'wait_until_stopped': False, 'blocking': False, 'settle': 0.5})
                            move(s, bx, by, 1.085, tl)
                            if dn.get('ok'):
                                done.append({'xy': c['xy'], 'h': c['h_cm'], 'width': w})
                                print('   已放入框子（第%d件）' % len(done), flush=True)
                            else:
                                print('   放到框上失败 %s' % dn.get('error'), flush=True)
                        else:
                            print('   运输失败 %s' % tr.get('error'), flush=True)
                    else:
                        print('   抬升失败 %s' % up.get('error'), flush=True)
                else:
                    call(s, {'cmd': 'grip', 'w': 0.105, 'v': 0.07, 'eff': 3,
                             'wait_until_stopped': False, 'blocking': False, 'settle': 0.35})
            print('   本轮 %.1fs' % (time.time() - t0), flush=True)
        print('DONE %s' % json.dumps(done, ensure_ascii=False), flush=True)
    finally:
        close(proc)


if __name__ == '__main__':
    main()
