"""Exercise the real server.py HTTP layer against a fake SDK bridge; never touches hardware.

Runs on independent port 18765 with the production Origin allow-list:

    python3 -B test_server.py
"""
import importlib.util, os, sys, json, time, tempfile, pathlib, subprocess, threading
import urllib.request, urllib.error

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

ROOT = pathlib.Path(__file__).resolve().parent
BRIDGE_URDF = '/data/galbot/config/galbot_description/galbot_one_golf_description/galbot_one_golf_cali_user.urdf'

FAKE_SDK = '''import time
class ControlStatus: SUCCESS=1
class S:
 joint_name='left_arm_joint1';position=0.;velocity=0.
 @property
 def timestamp_ns(self):return time.time_ns()
class GalbotRobot:
 def init(self):return True
 def get_joint_states(self,*a):return [S()]
 def get_joint_names(self,active,groups):return ['left_arm_joint1'] if 'left_arm' in groups else []
 def set_joint_positions(self,*a):return 1
 def stop_trajectory_execution(self):return 1
 def stop_base(self):return 1
 def get_base_velocity(self):return {"angular_velocity":[0,0,0]}
 def set_base_velocity(self,*a):return 1
 def request_shutdown(self):pass
 def wait_for_shutdown(self):pass
 def destroy(self):pass
'''


def build_bridge(tmp):
    pkg = pathlib.Path(tmp) / 'galbot_sdk'
    pkg.mkdir()
    (pkg / '__init__.py').write_text('')
    (pkg / 'g1.py').write_text(FAKE_SDK)
    fixture = pathlib.Path(tmp) / 'limits.urdf'
    fixture.write_text('<robot><joint name="left_arm_joint1"><limit lower="-1" upper="1"/></joint></robot>')
    return (ROOT / 'remote_bridge.py').read_text().replace(BRIDGE_URDF, str(fixture))


def post(body, token=None, origin='http://127.0.0.1:8765', raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request('http://127.0.0.1:18765/api', data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('X-Console-Token', token)
    if origin:
        req.add_header('Origin', origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    tmp = tempfile.mkdtemp()
    proc = subprocess.Popen([sys.executable, '-B', '-u', '-c', build_bridge(tmp)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env=dict(os.environ, PYTHONPATH=tmp))

    spec = importlib.util.spec_from_file_location('kitt_server', str(ROOT / 'server.py'))
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    server.p = proc
    threading.Thread(target=server.reader, args=(proc,), daemon=True).start()
    threading.Thread(target=server.stderr_reader, args=(proc,), daemon=True).start()
    threading.Thread(target=server.watchdog, daemon=True).start()
    try:
        httpd = server.http.server.ThreadingHTTPServer(('127.0.0.1', 18765), server.Handler)
    except OSError:
        proc.kill()
        print('失败：离线测试端口127.0.0.1:18765已被占用')
        return 1
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.8)

    tok, results = server.TOKEN, []

    def chk(name, got, want):
        ok = got == want
        results.append(ok)
        print(('PASS ' if ok else 'FAIL ') + name + ' -> ' + repr(got) + ('' if ok else '  want ' + repr(want)))

    try:
        chk('无 token 拒绝', post({'action': 'state'})[0], 403)
        chk('跨源拒绝', post({'action': 'state'}, token=tok, origin='http://evil.example')[0], 403)
        chk('未轮询即运动被拒',
            post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 10, 'speed': .02, 'confirmed': True}, tok)[1].get('error'),
            '状态已过期')
        s, b = post({'action': 'state'}, tok)
        chk('state 真实模式与关节数', (s, b.get('mode'), len(b.get('joints', []))), (200, 'real', 1))
        chk('state 含 baseline 与限位', bool(b['joints'][0]['baseline'] is not None and b['joints'][0]['limits']), True)
        chk('未确认现场安全被拒',
            post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 10, 'speed': .02, 'confirmed': False}, tok)[1].get('error'),
            '请确认现场安全')
        chk('超限位被拒',
            post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 200, 'speed': .02, 'confirmed': True}, tok)[1].get('error'),
            '目标超出该关节模型限位')
        chk('超速被拒',
            post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 10, 'speed': .21, 'confirmed': True}, tok)[1].get('error'),
            '速度必须为 0.01–0.20 rad/s')
        chk('合法点动被接受',
            post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 10, 'speed': .02, 'confirmed': True}, tok)[1].get('accepted'), True)
        post({'action': 'state'}, tok)
        chk('运动中再次运动被拒',
            post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 10, 'speed': .02, 'confirmed': True}, tok)[1].get('error'),
            '上一动作尚未结束')
        chk('关节忙时禁止底盘',
            post({'action': 'base', 'w': .05, 'sequence': 1, 'confirmed': True}, tok)[1].get('error'),
            '请先完成关节运动')
        chk('stop 正常', post({'action': 'stop'}, tok)[1].get('stopped'), True)
        post({'action': 'state'}, tok)
        chk('底盘未确认被拒',
            post({'action': 'base', 'w': .05, 'sequence': 2, 'confirmed': False}, tok)[1].get('error'),
            '请确认底盘现场安全与实时连接')
        chk('底盘续租成功',
            post({'action': 'base', 'w': .05, 'sequence': 3, 'confirmed': True}, tok)[1].get('accepted'), True)
        chk('底盘乱序序号被拒',
            post({'action': 'base', 'w': .05, 'sequence': 3, 'confirmed': True}, tok)[1].get('error'),
            '过期底盘指令已拒绝')
        chk('底盘超速被拒',
            post({'action': 'base', 'w': .31, 'sequence': 4, 'confirmed': True}, tok)[1].get('error'),
            '底盘转速须在 ±0.3 rad/s 内')
        chk('底盘停止成功', post({'action': 'base', 'w': 0, 'sequence': 5}, tok)[1].get('accepted'), True)
        chk('未知操作被拒', post({'action': 'hack'}, tok)[1].get('error'), '未知操作')
        chk('超大请求被拒', post({'action': 'state'}, tok, raw=b'x' * 5000)[1].get('error'), '请求过大')

        post({'action': 'state'}, tok)
        post({'action': 'move', 'name': 'left_arm_joint1', 'delta': 10, 'speed': .01, 'confirmed': True}, tok)
        time.sleep(1.8)
        chk('心跳丢失后看门狗清零 busy', server.busy_until, 0)

        proc.kill()
        proc.wait()
        time.sleep(0.5)
        chk('桥接断开后报错', post({'action': 'state'}, tok)[1].get('error'), '机器人桥接未连接')
    finally:
        httpd.shutdown()
        if proc.poll() is None:
            proc.kill()

    print('\n%d/%d passed' % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
