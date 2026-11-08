import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "index.html").read_bytes()
snapshot = {"status": "waiting for SSH login"}
received = 0.0

class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        if self.command == "GET" and self.path == "/state" and code == 200:
            return
        super().log_request(code, size)

    def do_GET(self):
        if self.headers.get("Host") != "127.0.0.1:8766":
            self.send_error(403)
            return
        if self.path == "/":
            content, mime = PAGE, "text/html; charset=utf-8"
        elif self.path == "/state":
            data = dict(snapshot)
            data["fresh"] = data["status"] == "sample" and time.monotonic() - received <= 2
            content, mime = json.dumps(data).encode(), "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        try:
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

def read_samples(process):
    global snapshot, received
    for raw in process.stdout:
        if raw.startswith(b"G1_STATE "):
            try:
                data = json.loads(raw[len(b"G1_STATE "):])
            except ValueError:
                continue
            received = time.monotonic()
            snapshot = data
    prior_status = snapshot["status"]
    exit_code = process.wait()
    reason = (
        "5 分钟只读采样已到期，请重新启动调试台"
        if exit_code == 124
        else f"SSH 会话已结束（退出码 {exit_code}，上次状态：{prior_status}）"
    )
    snapshot = dict(snapshot, status=reason)

def pinned_key(ip):
    expected = os.environ.get("G1_SSH_FINGERPRINT", "")
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expected):
        raise SystemExit("Set G1_SSH_FINGERPRINT to the independently verified ED25519 fingerprint")
    scanned = subprocess.run(
        ["ssh-keyscan", "-T", "5", "-t", "ed25519", ip],
        capture_output=True, text=True, timeout=8,
    )
    for line in scanned.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[:2] == [ip, "ssh-ed25519"]:
            digest = hashlib.sha256(base64.b64decode(fields[2], validate=True)).digest()
            fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
            if fingerprint == expected:
                return line + "\n"
    raise SystemExit("HPU ED25519 fingerprint could not be verified")

def robot_hostname():
    host = os.environ.get("G1_ROBOT_HOSTNAME", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", host):
        raise SystemExit("Set G1_ROBOT_HOSTNAME to the expected robot hostname")
    return host


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", type=ipaddress.IPv4Address, help="IPv4 address shown on the robot")
    parser.add_argument("--page-only", action="store_true")
    args = parser.parse_args()
    if args.ip is None and not args.page_only:
        parser.error("--ip is required for SSH")
    with ThreadingHTTPServer(("127.0.0.1", 8766), Handler) as server:
        print("Open http://127.0.0.1:8766/ | Ctrl+C to quit", flush=True)
        if args.page_only:
            server.serve_forever()
            return
        host = robot_hostname()
        payload = (ROOT / "read_robot.py").read_text(encoding="utf-8")
        source = base64.b64encode(payload.replace("G1_HOST_PLACEHOLDER", host).encode()).decode("ascii")
        remote = (
            'hostname; df -h /dev/shm >&2; '
            f'test "$(hostname)" = {host} && '
            'timeout 300 env PYTHONPATH=/data/galbot/lib LD_LIBRARY_PATH=/data/galbot/lib '
            'python3 -B -u -c "import base64; exec(base64.b64decode(\'' + source + '\'))"'
        )
        with tempfile.TemporaryDirectory() as key_dir:
            known = Path(key_dir) / "known_hosts"
            known.write_text(pinned_key(str(args.ip)), encoding="ascii")
            run_ssh(server, args.ip, remote, known)

def run_ssh(server, ip, remote, known):
    with subprocess.Popen([
        "ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
        "-o", "UserKnownHostsFile=" + str(known), "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
        "galbot@" + str(ip), remote,
    ], stdout=subprocess.PIPE) as ssh:
        threading.Thread(target=read_samples, args=(ssh,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if ssh.poll() is None:
                ssh.terminate()

if __name__ == "__main__":
    main()

