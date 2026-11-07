"""RE:BENCH 服务：一个本地 HTTP 服务 + 一个"扫描"接口。

和上一版最本质的区别：**没有逐帧检测循环了**。

上一版让浏览器每 55ms 推一帧上来读字母。每帧都必须"宁可不认"，否则误报
会一直挂在屏幕上；于是它只能收得很紧，紧到远处的标签干脆不认 —— 现场
就是"又慢又漏"。

现在只有两种状态：

  待机  —— 画面在浏览器里本地播（30fps），服务端一个像素都不收，CPU 是 0。
  扫描  —— 浏览器采 8 帧一起 POST 上来，服务端多尺度检测 + 跨帧投票 +
           模板与模型交叉验证，两三秒后回一份可以照着核对的结论。

一次扫描允许慢，所以它可以做得比实时链路准得多：多尺度补缩小标签、投票
压掉单帧错读、最清晰帧定几何、模型专治"模板没把握"的块。
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np

from .core import Baseline, Reader, zone_lookup
from .page import PAGE
from .scan import Scanner, save_annotated


class Bench:
    """服务端状态：扫描、基准、登记。扫描是互斥的，同一时刻只跑一次。"""

    def __init__(self, config_path: str) -> None:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.cfg = cfg
        self.reader = Reader(cfg.get("label") or {})
        self.scanner = Scanner(self.reader)
        self.base = Baseline()
        self.zones = cfg.get("zones") or {}
        self.base_res = tuple(cfg.get("resolution") or (1280, 720))
        self.tol_px = float((cfg.get("compare") or {}).get("tol_px", 90))

        self.scan_lock = threading.Lock()
        self.last_scan: dict = {"ok": True, "blocks": [], "stats": {}}
        self.scans = 0
        self.started = time.time()

    # ------------------------------------------------------------------
    def decode(self, frames_b64: list[str], w: int, h: int) -> list[np.ndarray]:
        out = []
        for s in frames_b64:
            if not s:
                continue
            try:
                raw = base64.b64decode(s.split(",")[-1])
            except (ValueError, TypeError):
                continue
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if w and h and (img.shape[1] != w or img.shape[0] != h):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            out.append(img)
        return out

    def scan(self, frames_b64: list[str], w: int, h: int,
             use_vlm: bool = True) -> dict:
        if not self.scan_lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "msg": "上一次还在识别，稍等"}
        try:
            frames = self.decode(frames_b64, w, h)
            if not frames:
                return {"ok": False, "msg": "没收到画面帧"}
            res = self.scanner.scan(frames, use_vlm=use_vlm)
            self.scans += 1
            res["scans"] = self.scans
            res["has_base"] = bool(self.base.items)
            res["letters"] = self.reader.letters()
            res["reader"] = self.reader.reader_name()
            if res.get("ok"):
                self.last_scan = res
            return res
        finally:
            self.scan_lock.release()

    # ------------------------------------------------------------------
    def _confident(self) -> list[dict]:
        return [b for b in (self.last_scan.get("blocks") or [])
                if b.get("letter") and b.get("conf") in ("high", "mid")]

    def _zone_of(self):
        fw, fh = (self.last_scan.get("frame") or self.base_res)[:2]
        return zone_lookup(self.zones, int(fw), int(fh), self.base_res)

    def anchor(self) -> dict:
        items = self._confident()
        if not items:
            return {"ok": False, "msg": "还没读到有把握的字母，先扫一次"}
        n = self.base.capture(items, self._zone_of())
        letters = "、".join(i["letter"] for i in self.base.items)
        return {"ok": True, "msg": f"基准已建：{n} 块（{letters}）", "count": n}

    def compare(self) -> dict:
        if not self.base.items:
            return {"ok": False, "msg": "先点「建立基准」"}
        items = self._confident()
        if not items:
            return {"ok": False, "msg": "这次没读到有把握的字母，没法比较"}
        res = self.base.compare(items, self._zone_of(), self.tol_px)
        # 只有低可信结果时把话说清楚：这不是"积木被拿走了"，是"没认出来"
        unsure = [b["letter"] or "?" for b in (self.last_scan.get("blocks") or [])
                  if b.get("conf") in ("low", "none")]
        res.update({"ok": True, "unsure": unsure})
        return res

    def snapshot(self) -> dict:
        img = self.scanner.last_annotated
        if img is None:
            return {"ok": False, "msg": "还没扫过，先扫描一次"}
        path = save_annotated(img)
        return {"ok": True, "msg": f"已存 {path}", "path": path}

    def enroll(self, letter: str, index: int | None = None) -> dict:
        blocks = self.scanner.last_blocks or []
        if index is not None:
            if not (0 <= index < len(blocks)):
                return {"ok": False, "msg": "这块已经失效了，重新扫一次"}
            return self.reader.enroll_mask(blocks[index].mask, letter)
        frame = self.scanner.last_frame
        if frame is None:
            return {"ok": False, "msg": "还没有画面"}
        return self.reader.enroll(frame, letter)


class Handler(BaseHTTPRequestHandler):
    bench: Bench
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 关掉请求日志
        pass

    # ------------------------------------------------------------------
    def _send(self, body: bytes, code: int = 200, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: dict, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), code)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(PAGE.encode(), 200, "text/html; charset=utf-8")
            return
        if path == "/zones":
            self._json({"zones": self.bench.zones,
                        "base": list(self.bench.base_res),
                        "tol_px": self.bench.tol_px,
                        "letters": self.bench.reader.letters(),
                        "reader": self.bench.reader.reader_name()})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""

        if path == "/scan":
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "msg": "请求不是合法 JSON"}, 400)
                return
            try:
                self._json(self.bench.scan(
                    payload.get("frames") or [],
                    int(payload.get("w") or 0),
                    int(payload.get("h") or 0),
                    bool(payload.get("vlm", True))))
            except Exception as exc:  # noqa: BLE001 - 一次扫描出错不能让服务挂
                self._json({"ok": False, "msg": f"{type(exc).__name__}: {exc}"}, 500)
            return

        route = {"/anchor": self.bench.anchor,
                 "/compare": self.bench.compare,
                 "/snapshot": self.bench.snapshot}.get(path)
        if route is not None:
            try:
                self._json(route())
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "msg": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if path in ("/enroll", "/forget"):
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            letter = str(payload.get("letter", "")).strip().upper()
            if path == "/enroll":
                idx = payload.get("index")
                self._json(self.bench.enroll(
                    letter, int(idx) if isinstance(idx, (int, float)) else None))
            else:
                self._json(self.bench.reader.forget(letter))
            return

        self._json({"ok": False, "error": "not found"}, 404)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RE:BENCH 扫描识别（Mac 本机摄像头）")
    ap.add_argument("--config", default="vision/config/mac.json")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    Handler.bench = Bench(args.config)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    b = Handler.bench
    print("=" * 58, flush=True)
    print(f"[SCAN] {url}", flush=True)
    print(f"[SCAN] 模板库：{'、'.join(b.reader.letters()) or '空'}", flush=True)
    print(f"[SCAN] 读字母：{b.reader.reader_name()}", flush=True)
    print("[SCAN] 画面只是本地预览；点「扫描识别」或按空格才真正识别", flush=True)
    print("=" * 58, flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[SCAN] 退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
