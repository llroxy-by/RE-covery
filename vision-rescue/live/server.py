"""RE:BENCH 实时服务：一个本地 HTTP 服务 + 一个检测槽位。

设计上只有一条规矩：**每帧的往返必须短**。

  浏览器：<video> 直接放本地摄像头（30fps，零延迟，不经过服务端）
        ↓ 只把缩小的单帧 JPEG POST 上来
  服务端：解码 → 读字母 → 回一份几十字节的 JSON
        ↓
  浏览器：在画布上按坐标画框和字母

所以"画面帧率"和"检测帧率"彻底解耦：画面永远是摄像头的原生帧率，
检测能跑多快就多快，慢了也不会让画面卡住 —— 旧版把标注图 base64 回传，
画面帧率 = 检测帧率，这是它卡死的根。

并发策略：**只留一个检测槽，新的帧来了就把旧的顶掉**。摄像头 15fps、
检测 40ms 时谁都不丢；检测一旦变慢，返回的是上一帧的结论（带 age_ms），
浏览器据此自动降频，永远不排队、不堆积、不雪崩。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from .core import Baseline, Reader, Stabilizer, zone_lookup
from .page import PAGE


class Bench:
    """服务端状态：检测、基准、登记。"""

    def __init__(self, config_path: str) -> None:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.cfg = cfg
        self.reader = Reader(cfg.get("label") or {})
        self.stab = Stabilizer()
        self.base = Baseline()
        self.zones = cfg.get("zones") or {}
        self.base_res = tuple(cfg.get("resolution") or (1280, 720))
        self.tol_px = float((cfg.get("compare") or {}).get("tol_px", 90))

        self.lock = threading.Lock()       # 只保护"最后一帧/最后状态"
        self.busy = threading.Lock()       # 检测槽位，拿不到就跳过这一帧
        self.last_frame: np.ndarray | None = None
        self.state: dict = {"ok": True, "gen": 0, "tracks": [], "ms": 0, "age_ms": 0}
        self.frames = 0
        self.skipped = 0
        self.started = time.time()

    # ------------------------------------------------------------------
    def detect(self, jpeg: bytes, w: int, h: int) -> dict:
        # 槽位被占：直接把上一次的结论还回去，绝不排队。
        if not self.busy.acquire(blocking=False):
            self.skipped += 1
            with self.lock:
                out = dict(self.state)
            out["age_ms"] = int((time.time() - out.get("at", 0)) * 1000)
            out["busy"] = True
            return out

        try:
            t0 = time.perf_counter()
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return {"ok": False, "error": "这一帧解不开"}
            # 客户端送的是它自己裁好的画面；服务端只负责保证宽度等于它声明的
            if w and h and img.shape[1] != w:
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

            hits, stats = self.reader.read(img)
            tracks = self.stab.update(hits)
            zone_of = zone_lookup(self.zones, img.shape[1], img.shape[0], self.base_res)

            with self.lock:
                self.last_frame = img
                self.frames += 1
                self.state = {
                    "ok": True,
                    "gen": self.state.get("gen", 0) + 1,
                    "at": time.time(),
                    "age_ms": 0,
                    "busy": False,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detect_ms": stats.get("read_ms", 0),
                    "tracks": [{"id": t.tid, "letter": t.letter,
                                "bbox": [int(v) for v in t.bbox],
                                "score": round(t.score, 2), "src": t.src,
                                "fresh": t.missed == 0,
                                "zone": zone_of(t.center)}
                               for t in tracks],
                    "stats": stats,
                    "candidates": self.reader.last_candidates,
                    "reader": self.reader.reader_name(),
                    "center_letter": self.reader.center_letter,
                    "letters": self.reader.bank.letters(),
                    "has_base": bool(self.base.items),
                    "fps": self._fps(),
                    "frame": [img.shape[1], img.shape[0]],
                }
                return self.state
        finally:
            self.busy.release()

    def _fps(self) -> float:
        dt = time.time() - self.started
        return round(self.frames / dt, 1) if dt > 0.5 else 0.0

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            img = None if self.last_frame is None else self.last_frame.copy()
            tracks = list(self.state.get("tracks") or [])
        if img is None:
            return {"ok": False, "msg": "还没有画面"}
        for name, pts in self.zones.items():
            sx = img.shape[1] / float(self.base_res[0])
            sy = img.shape[0] / float(self.base_res[1])
            poly = np.array([[int(p[0] * sx), int(p[1] * sy)] for p in pts], np.int32)
            cv2.polylines(img, [poly], True, (60, 170, 255), 2)
            cv2.putText(img, name, (poly[0][0] + 6, poly[0][1] + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 170, 255), 2, cv2.LINE_AA)
        for t in tracks:
            x, y, w, h = t["bbox"]
            cv2.rectangle(img, (x, y), (x + w, y + h), (0, 220, 120), 2)
            tag = (t["letter"] or "?") + f" {t['score']:.2f}"
            cv2.putText(img, tag, (x, max(18, y - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.75, (0, 220, 120), 2, cv2.LINE_AA)
        out_dir = Path("vision/records")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"live-{datetime.now():%Y%m%d-%H%M%S}.jpg"
        cv2.imwrite(str(path), img)
        return {"ok": True, "msg": f"已存 {path}", "path": str(path)}

    def peek(self) -> dict:
        """手动读一次画面中央（慢，但用户明确要求就不算误报）。"""
        frame = self.last_frame
        if frame is None:
            return {"ok": False, "msg": "还没有画面"}
        return self.reader.read_center(frame)

    def anchor(self) -> dict:
        with self.lock:
            # 只认这一帧还看得见的：上帧的残影会把"被挪走的积木"算成没动
            tracks = [t for t in self.stab.tracks if t.missed == 0]
            zone_of = zone_lookup(self.zones,
                                  self.state.get("frame", self.base_res)[0],
                                  self.state.get("frame", self.base_res)[1],
                                  self.base_res)
            n = self.base.capture(tracks, zone_of)
        if not n:
            return {"ok": False, "msg": "画面里还没读到字母，没法建基准"}
        letters = "、".join(i["letter"] for i in self.base.items)
        return {"ok": True, "msg": f"基准已建：{n} 块（{letters}）", "count": n}

    def compare(self) -> dict:
        if not self.base.items:
            return {"ok": False, "msg": "先点「建立基准」"}
        with self.lock:
            # 只认这一帧还看得见的：上帧的残影会把"被挪走的积木"算成没动
            tracks = [t for t in self.stab.tracks if t.missed == 0]
            zone_of = zone_lookup(self.zones,
                                  self.state.get("frame", self.base_res)[0],
                                  self.state.get("frame", self.base_res)[1],
                                  self.base_res)
            res = self.base.compare(tracks, zone_of, self.tol_px)
        res["ok"] = True
        return res


class Handler(BaseHTTPRequestHandler):
    bench: Bench
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 关掉每帧一行的日志
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
                        "tol_px": self.bench.tol_px})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            w = int(float(q.get("w", ["0"])[0]))
            h = int(float(q.get("h", ["0"])[0]))
        except ValueError:
            w = h = 0

        if u.path == "/detect":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            if not body:
                self._json({"ok": False, "error": "空帧"}, 400)
                return
            try:
                self._json(self.bench.detect(body, w, h))
            except Exception as exc:  # noqa: BLE001 - 单帧出错不能让整个服务挂
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        route = {"/anchor": self.bench.anchor,
                 "/compare": self.bench.compare,
                 "/peek": self.bench.peek,
                 "/snapshot": self.bench.snapshot}.get(u.path)
        if route is not None:
            try:
                self._json(route())
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "msg": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if u.path in ("/enroll", "/forget"):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            letter = str(payload.get("letter", "")).strip().upper()
            frame = self.bench.last_frame
            if u.path == "/enroll":
                if frame is None:
                    self._json({"ok": False, "msg": "还没有画面"})
                    return
                self._json(self.bench.reader.enroll(frame, letter))
            else:
                self._json(self.bench.reader.forget(letter))
            return

        self._json({"ok": False, "error": "not found"}, 404)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RE:BENCH 实时视觉（Mac 本机摄像头）")
    ap.add_argument("--config", default="vision/config/mac.json")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    Handler.bench = Bench(args.config)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print("=" * 58, flush=True)
    print(f"[LIVE] {url}", flush=True)
    print(f"[LIVE] 模板库：{'、'.join(Handler.bench.reader.bank.letters()) or '空'}", flush=True)
    print(f"[LIVE] 读字母：{Handler.bench.reader.reader_name()}", flush=True)
    print("[LIVE] 首次会问摄像头权限，浏览器里点「允许」", flush=True)
    print("=" * 58, flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[LIVE] 退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
