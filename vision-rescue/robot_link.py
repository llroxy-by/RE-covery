"""本地侧：经 SSH 拉起机器人常驻代理，按 JSONL 收发。

代理脚本以 base64 单 token 注入 `python3 -c`，机器人上不落盘。
线程安全：一把锁串行化请求；超时即杀掉连接，下次调用自动重启。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

AGENT = Path(__file__).with_name("robot_agent.py")
DEFAULT_HOST = os.environ.get("ROBOT_HOST", "galbot@192.168.167.46")
CONTROL_PATH = os.environ.get("ROBOT_SOCK", "/tmp/kitt-console-live.sock")


class RobotAgent:
    def __init__(self, host: str | None = None) -> None:
        self.host = host or DEFAULT_HOST
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.ready: dict | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------ 进程
    def _ssh_cmd(self) -> list[str]:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T"]
        if CONTROL_PATH and Path(CONTROL_PATH).exists():
            cmd += ["-o", "ControlPath=" + CONTROL_PATH]
        cmd.append(self.host)
        payload = base64.b64encode(AGENT.read_bytes()).decode()
        cmd.append("PYTHONPATH=/data/galbot/lib python3 -c "
                   "\"import base64;exec(base64.b64decode('%s'))\"" % payload)
        return cmd

    def start(self, wait_ready: float = 25.0) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        self.stop()
        try:
            self.proc = subprocess.Popen(self._ssh_cmd(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1)
        except OSError as exc:
            self.last_error = "启动失败：%s" % exc
            return False
        deadline = time.time() + wait_ready
        while time.time() < deadline:
            line = self._readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ready"):
                self.ready = msg["ready"]
                return True
            if msg.get("ok") is False:
                self.last_error = msg.get("error") or "代理启动失败"
                self.stop()
                return False
        self.last_error = self.last_error or "代理无响应（超时）"
        self.stop()
        return False

    def _readline(self, timeout: float) -> str | None:
        if not self.proc or not self.proc.stdout:
            return None
        box: list[str | None] = [None]

        def read() -> None:
            try:
                box[0] = self.proc.stdout.readline()
            except (ValueError, OSError):
                box[0] = None

        th = threading.Thread(target=read, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            return None
        return box[0]

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.write('{"cmd":"quit"}\n')
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        time.sleep(0.05)
        try:
            proc.terminate()
        except OSError:
            pass

    # ------------------------------------------------------------ 请求
    def call(self, req: dict, timeout: float = 30.0) -> dict:
        with self.lock:
            if not self.start():
                return {"ok": False, "error": self.last_error or "机器人代理不可用"}
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.stop()
                return {"ok": False, "error": "连接已断开，请重试"}
            t0 = time.time()
            while time.time() - t0 < timeout:
                line = self._readline(timeout=timeout - (time.time() - t0))
                if line is None:
                    self.stop()
                    return {"ok": False, "error": "机器人响应超时"}
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except ValueError:
                    continue
            self.stop()
            return {"ok": False, "error": "响应超时"}

    def view(self) -> dict:
        return self.call({"cmd": "view"}, timeout=35)

    def target(self, roi: list[float]) -> dict:
        return self.call({"cmd": "target", "roi": roi}, timeout=30)
