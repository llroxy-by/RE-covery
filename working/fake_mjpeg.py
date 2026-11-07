"""本地假 MJPEG 服务器：用来验证 --url 读取手机视频流这条代码路径。

不需要真手机：它按 multipart/x-mixed-replace 推合成画面（画面里有移动的方块，
所以不会触发冻结守卫）。跑法：
    python working/fake_mjpeg.py 8899 &
    python -m vision.main --url http://127.0.0.1:8899/video --mode real --max-frames 12
"""

from __future__ import annotations

import http.server
import socketserver
import sys
import time

import cv2
import numpy as np


SIZE = (1280, 720)


def frame_bytes(i: int) -> bytes:
    img = np.full((720, 1280, 3), 105, np.uint8)
    for name, (x, y) in {"A": (250, 330), "B": (630, 330), "C": (1010, 330)}.items():
        cv2.rectangle(img, (x - 150, y - 150), (x + 150, y + 150), (128, 128, 128), -1)
        cv2.putText(img, name, (x - 10, y + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (60, 60, 60), 2, cv2.LINE_AA)
    # 移动的方块：保证帧间有差异
    x = 40 + (i * 7) % 1180
    cv2.rectangle(img, (x, 600), (x + 60, 660), (0, 0, 220), -1)
    if (img.shape[1], img.shape[0]) != SIZE:
        img = cv2.resize(img, SIZE, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        raise RuntimeError("编码失败")
    return buf.tobytes()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in ("/video", ""):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        i = 0
        try:
            while True:
                jpg = frame_bytes(i)
                i += 1
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpg)).encode() + b"\r\n\r\n")
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                time.sleep(0.08)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *args):  # 静音
        return


def main() -> None:
    global SIZE
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    if len(sys.argv) > 2:
        w, h = sys.argv[2].lower().split("x")
        SIZE = (int(w), int(h))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as srv:
        print(f"fake MJPEG on http://127.0.0.1:{port}/video  size={SIZE[0]}x{SIZE[1]}",
              flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
