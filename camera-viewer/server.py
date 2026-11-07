"""Local viewer; --robot is for on-site technician deployment only."""
import argparse
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

frame = None
stamp = 0
lock = threading.Lock()
stop = threading.Event()

def capture():
    global frame, stamp
    from galbot_sdk.g1 import GalbotRobot, SensorType, RgbOutputFormat
    robot = GalbotRobot()
    try:
        if not robot.init({SensorType.HEAD_LEFT_CAMERA}):
            raise RuntimeError('相机初始化失败')
        if stop.wait(5):
            return
        while not stop.is_set():
            data = robot.get_rgb_data(SensorType.HEAD_LEFT_CAMERA, RgbOutputFormat.JPEG, True)
            jpeg = bytes(data.get('data', b'')) if data else b''
            if jpeg.startswith(b'\xff\xd8'):
                with lock:
                    frame, stamp = jpeg, time.monotonic()
            stop.wait(.1)
    finally:
        robot.request_shutdown()
        robot.wait_for_shutdown()
        robot.destroy()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            body = Path(__file__).with_name('index.html').read_bytes()
            kind = 'text/html; charset=utf-8'
        elif path == '/frame.jpg':
            with lock:
                body = frame if time.monotonic() - stamp < 3 else None
            if body is None:
                self.send_error(503, 'Camera not ready')
                return
            kind = 'image/jpeg'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def log_message(self, *args):
        pass

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--robot', action='store_true')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8780)
    a = p.parse_args()
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    worker = None
    if a.robot:
        worker = threading.Thread(target=capture, daemon=True)
        worker.start()
    print('Camera viewer: http://%s:%s' % (a.host, a.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=10)
