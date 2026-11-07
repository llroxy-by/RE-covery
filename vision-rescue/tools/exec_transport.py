"""Stream the executor over SSH stdin without creating remote files."""
import json
from collections import deque
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / 'robot_exec.py'


def connect(allow_motion=False):
    host = os.environ.get('ROBOT_HOST', 'galbot@192.168.167.46')
    sock = os.environ.get('ROBOT_SOCK', '/tmp/kitt-console-live.sock')
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-T']
    if Path(sock).exists():
        cmd += ['-o', 'ControlPath=' + sock]
    cmd += [host, "PYTHONPATH=/data/galbot/lib PYTHONDONTWRITEBYTECODE=1 python3 -u -c "
            "'import sys,json;exec(json.loads(sys.stdin.readline()))'"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1)
    p._responses = queue.Queue()
    p._seq = 0
    p._allow_motion = allow_motion
    p._logs = deque(maxlen=80)

    def reader():
        try:
            for line in p.stdout:
                try:
                    p._responses.put(json.loads(line))
                except ValueError:
                    p._logs.append(line.rstrip()[:1000])
        finally:
            p._responses.put(None)

    threading.Thread(target=reader, daemon=True).start()
    def read_errors():
        for line in p.stderr:
            p._logs.append(line.rstrip()[:1000])
    threading.Thread(target=read_errors, daemon=True).start()
    source = 'ALLOW_MOTION = %r\n' % allow_motion + AGENT.read_text(encoding='utf-8')
    # Future imports must precede assignments.
    source = source.replace('from __future__ import annotations', '')
    p.stdin.write(json.dumps(source) + '\n')
    p.stdin.flush()
    try:
        deadline = time.monotonic() + 40
        while True:
            msg = p._responses.get(timeout=max(.01, deadline - time.monotonic()))
            if msg is None:
                raise RuntimeError('执行器提前退出')
            if isinstance(msg, dict) and msg.get('ready'):
                return p, msg
            if time.monotonic() >= deadline:
                raise queue.Empty
    except Exception:
        close(p)
        raise RuntimeError('执行器启动失败或超时')


def call(p, obj, timeout=40):
    motion = {'cart', 'joints', 'execute_plan', 'grip', 'base'}
    if obj.get('cmd') in motion and not p._allow_motion:
        return 0, {'ok': False, 'error': '当前连接只读'}
    p._seq += 1
    rid = p._seq
    start = time.monotonic()
    p.stdin.write(json.dumps(dict(obj, id=rid)) + '\n')
    p.stdin.flush()
    try:
        while True:
            r = p._responses.get(timeout=max(.01, timeout - (time.monotonic() - start)))
            if r is None:
                raise RuntimeError('执行器连接已断开；未重试运动')
            if isinstance(r, dict) and r.get('id') == rid:
                return (time.monotonic() - start) * 1000, r
            if time.monotonic() - start >= timeout:
                raise queue.Empty
    except queue.Empty:
        close(p)
        raise TimeoutError('执行器响应超时；结果未知，未重试运动')


def close(p):
    try:
        p.stdin.close()
        p.wait(timeout=3)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        p.terminate()
