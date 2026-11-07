"""浏览器观察台：实时看画面、掩膜、检测结果，并在网页上触发 S0/S1。

    python -m vision.web --camera 1                  # http://127.0.0.1:8000
    python -m vision.web --mode mock --port 8123      # 没相机时先看效果
    python -m vision.web --host 0.0.0.0               # 想让手机也能看（同一局域网）

只用标准库 http.server：不引入 Flask，不多一个依赖。画面走 MJPEG 流，
状态走 /state（JSON），按钮走 /action（0=建基准 1=比较 m=切场景 d=调参建议）。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import cv2

from .main import VisionApp
from .sources import MOCK_SCENARIOS, MockSource, SourceError, create_source

JPEG_QUALITY = 80


class CaptureService:
    """一条后台抓帧线程 + 一把锁。HTTP 线程只读最新结果，不直接碰摄像头。"""

    def __init__(self, app_args, config_path: str) -> None:
        self.args = app_args
        self.app = VisionApp(SimpleNamespace(
            camera=app_args.camera, config=config_path, mode=app_args.mode,
            output=app_args.output, min_conf=app_args.min_conf, mock_cycle=False,
            skip_image_check=False, allow_frozen=app_args.allow_frozen, url=app_args.url,
        ))
        self.source = None
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._masks_jpeg: bytes | None = None
        self._state: dict = {"ok": False, "error": "启动中", "objects": [], "issues": []}
        self._pending: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- 生命周期
    def start(self) -> None:
        self.source = create_source(self.args.mode, self.args.camera,
                                   self.app.config, allow_frozen=self.args.allow_frozen,
                                   url=self.args.url)
        self.app._source = self.source  # annotate/capture 里要看来源类型
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.source is not None:
            self.source.close()

    # ---------------------------------------------------------- 动作
    def act(self, key: str) -> dict:
        if key not in "01md":
            return {"ok": False, "error": f"未知动作 {key}"}
        with self._lock:
            self._pending.append(key)
        time.sleep(0.05)
        return {"ok": True, "queued": key}

    # ---------------------------------------------------------- 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.source.read()
            except SourceError as exc:
                with self._lock:
                    self._state = {"ok": False, "error": str(exc), "objects": [], "issues": []}
                return

            objects, quality_ok, issues = self.app.observe(frame)
            ann = self.app.annotate(frame, objects, issues)
            main_jpeg = _encode(ann)
            masks_jpeg = _encode(self.app.mask_mosaic(frame), quality=70)

            with self._lock:
                self._jpeg, self._masks_jpeg = main_jpeg, masks_jpeg
                pending, self._pending = self._pending, []
                state = {
                    "ok": True,
                    "mode": frame.mode,
                    "source": frame.source,
                    "time": frame.captured_at,
                    "size": [frame.width, frame.height],
                    "quality_ok": quality_ok,
                    "issues": issues,
                    "objects": objects,
                    "scenario": getattr(self.source, "scenario", None),
                    "baseline": (self.app.baseline or {}).get("snapshot_id"),
                }
                if self.app.baseline is not None:
                    state["diff"] = self._last_diff
                self._state = state

            for key in pending:
                self._handle(key, frame, objects, issues)

    _last_diff: dict | None = None

    def _handle(self, key: str, frame, objects, issues) -> None:
        if key == "0":
            self.app.capture_baseline(frame, objects, issues)
            self._last_diff = None  # 新基准出来，旧差异作废
        elif key == "1":
            from . import diff as diff_mod
            if self.app.baseline is None:
                print("[WEB] 还没有 S0 基准，先按 0", flush=True)
                return
            # 必须先把 S1 落盘：scene.image.reference 为空会被判 current_image_missing
            ann = self.app.annotate(frame, objects, issues)
            saved = self.app.store.save(self.app.scene, frame.image, ann)
            result = diff_mod.compare(self.app.baseline, self.app.scene,
                                      min_conf=self.app.min_conf)
            path = diff_mod.save_differences(result, self.app.output_dir)
            self._last_diff = dict(result, saved=str(path), s1=str(saved["json"]),
                                   verdict=diff_mod.gate_verdict(result))
            print(f"[WEB] diff status={result['status']} verdict={self._last_diff['verdict']}", flush=True)
        elif key == "m":
            if isinstance(self.source, MockSource):
                print(f"[WEB] mock 场景 -> {self.source.next_scenario()}", flush=True)
                self.app.tracker.reset()
        elif key == "d":
            for h in self.app.tuning_hints(objects):
                print(f"[TUNE] {h}", flush=True)

    # ---------------------------------------------------------- 读取
    def frame_bytes(self, which: str) -> bytes | None:
        with self._lock:
            return self._jpeg if which == "main" else self._masks_jpeg

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()


def _encode(img, quality: int = JPEG_QUALITY) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else b""


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>RE-covery 视觉观察台</title>
<style>
  :root { --bg:#14161a; --panel:#1d2026; --line:#2c3138; --fg:#e8eaed; --dim:#98a0ac;
          --ok:#3ddc84; --warn:#ffb020; --bad:#ff5c5c; --accent:#4aa8ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { display:flex; align-items:center; gap:16px; padding:12px 20px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  h1 { font-size:16px; margin:0; font-weight:600; }
  .badge { padding:2px 8px; border-radius:10px; font-size:12px; background:#2a2f36; color:var(--dim); }
  .badge.real { background:#1d3a24; color:var(--ok); }
  .badge.mock { background:#3a341d; color:var(--warn); }
  main { display:grid; grid-template-columns:minmax(0,2.2fr) minmax(280px,1fr); gap:16px; padding:16px 20px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }
  .card h2 { font-size:13px; margin:0 0 8px; color:var(--dim); font-weight:600;
             text-transform:uppercase; letter-spacing:.04em; }
  img { width:100%; display:block; border-radius:6px; background:#000; }
  .row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  button { background:#262b33; color:var(--fg); border:1px solid var(--line);
           border-radius:7px; padding:8px 14px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); color:var(--accent); }
  button.primary { background:#1e3a5f; border-color:#2f5f96; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--dim); font-weight:600; }
  code { font-family:ui-monospace,Consolas,monospace; font-size:12px; }
  .vis { color:var(--ok); } .mis { color:var(--warn); } .unc { color:var(--bad); }
  .kv { display:flex; justify-content:space-between; gap:8px; padding:3px 0; font-size:12.5px; }
  .kv span:first-child { color:var(--dim); }
  .muted { color:var(--dim); font-size:12.5px; }
  .issues li { color:var(--warn); }
</style></head><body>
<header>
  <h1>RE-covery · 视觉观察台</h1>
  <span class="badge" id="mode">--</span>
  <span class="badge" id="src">--</span>
  <span class="badge" id="qual">--</span>
  <span class="muted" id="clock"></span>
</header>
<main>
  <section>
    <div class="card" style="margin-bottom:16px">
      <div class="row">
        <button class="primary" onclick="act('0')">0 · 建立 S0 基准</button>
        <button class="primary" onclick="act('1')">1 · 建立 S1 并比较</button>
        <button onclick="act('m')">m · 切换 mock 场景</button>
        <button onclick="act('d')">d · 调参建议</button>
      </div>
      <img src="/stream" alt="live">
    </div>
    <div class="card">
      <h2>HSV 掩膜（白 = 被阈值选中）</h2>
      <img src="/masks" alt="masks">
      <p class="muted">掩膜全黑 = 阈值没选中；有白块没框 = 面积过滤太狠；边缘毛刺 = 光照不均。</p>
    </div>
  </section>
  <aside>
    <div class="card" style="margin-bottom:16px">
      <h2>状态</h2>
      <div id="kvbox"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>目标（当前帧）</h2>
      <table><thead><tr><th>object_id</th><th>zone</th><th>vis</th><th>conf</th></tr></thead>
      <tbody id="objs"></tbody></table>
      <div id="reasons" class="muted" style="margin-top:8px"></div>
    </div>
    <div class="card">
      <h2>差异（S0 vs S1）</h2>
      <div id="diffbox" class="muted">还没有比较结果。按 0 建基准，挪用积木，再按 1。</div>
    </div>
  </aside>
</main>
<script>
const cls = s => s === 'unchanged' ? 'vis' : (s === 'uncertain' ? 'unc' : 'mis');
function act(k) { fetch('/action?k=' + k).catch(()=>{}); }
function row(k, v) { return '<div class="kv"><span>' + k + '</span><span>' + v + '</span></div>'; }
async function tick() {
  try {
    const s = await (await fetch('/state')).json();
    document.getElementById('mode').textContent = s.mode ? ('mode=' + s.mode) : '未启动';
    document.getElementById('mode').className = 'badge ' + (s.mode || '');
    document.getElementById('src').textContent = s.source || '--';
    const q = document.getElementById('qual');
    q.textContent = s.ok ? (s.quality_ok ? 'quality OK' : 'quality FAIL') : 'ERROR';
    q.style.color = s.ok && s.quality_ok ? 'var(--ok)' : 'var(--bad)';
    document.getElementById('clock').textContent = s.time || '';
    if (s.error) { document.getElementById('kvbox').innerHTML = row('错误', s.error); return; }
    document.getElementById('kvbox').innerHTML =
      row('画面', (s.size||[]).join('×')) +
      row('场景', s.scenario || '-') +
      row('基准', s.baseline ? s.baseline.slice(0,8) : '未建立') +
      row('issues', (s.issues||[]).length);
    document.getElementById('objs').innerHTML = (s.objects||[]).map(o =>
      '<tr><td><code>' + o.object_id + '</code></td><td>' + o.zone + '</td><td>' + o.visibility +
      '</td><td>' + o.confidence.toFixed(2) + '</td></tr>').join('');
    const rs = (s.objects||[]).filter(o => (o.reasons||[]).length)
      .map(o => o.object_id + ': ' + o.reasons.join(', '));
    document.getElementById('reasons').innerHTML = rs.length ? rs.join('<br>') : '无异常原因';
    if (s.diff) {
      const d = s.diff;
      document.getElementById('diffbox').innerHTML =
        '<div class="kv"><span>整体</span><span class="' + cls(d.status) + '">' + d.status +
        '</span></div><div class="kv"><span>判据</span><span>' + (d.verdict||'-') +
        '</span></div><table style="margin-top:8px"><thead><tr><th>ID</th><th>status</th><th>from→to</th></tr></thead><tbody>' +
        d.differences.map(x => '<tr><td><code>' + x.object_id + '</code></td><td class="' + cls(x.status) +
        '">' + x.status + '</td><td>' + x.from_zone + ' → ' + x.to_zone + '</td></tr>').join('') +
        '</tbody></table>';
    }
  } catch (e) {}
}
setInterval(tick, 1000); tick();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    service: CaptureService | None = None

    def log_message(self, *args):  # 静音访问日志
        return

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            self._send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/state":
            self._send_bytes(json.dumps(self.service.state(), ensure_ascii=False).encode(),
                             "application/json; charset=utf-8")
        elif path == "/action":
            key = ""
            if "k=" in self.path:
                key = self.path.split("k=")[1][:1]
            self._send_bytes(json.dumps(self.service.act(key), ensure_ascii=False).encode(),
                             "application/json; charset=utf-8")
        elif path in ("/stream", "/masks"):
            self._mjpeg("main" if path == "/stream" else "masks")
        else:
            self.send_error(404)

    def _send_bytes(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _mjpeg(self, which: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while not self.service.stopped:
                buf = self.service.frame_bytes(which)
                if buf:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(buf)).encode() + b"\r\n\r\n")
                    self.wfile.write(buf)
                    self.wfile.write(b"\r\n")
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="浏览器观察台（实时画面 + 掩膜 + S0/S1）")
    p.add_argument("--camera", type=int, default=1)
    p.add_argument("--url", default=None, help="手机 IP 摄像头视频流")
    p.add_argument("--config", default="vision/config/desktop.json")
    p.add_argument("--mode", choices=["real", "mock"], default="real")
    p.add_argument("--output", default=None)
    p.add_argument("--min-conf", type=float, default=0.8)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1", help="想让局域网/手机访问就填 0.0.0.0")
    p.add_argument("--mock-scenario", choices=list(MOCK_SCENARIOS), default=None)
    p.add_argument("--allow-frozen", action="store_true")
    args = p.parse_args(argv)

    service = CaptureService(args, args.config)
    try:
        service.start()
    except SourceError as exc:
        print(f"[FATAL] 取像失败：{exc}")
        return 2

    if args.mode == "mock" and args.mock_scenario:
        service.source.set_scenario(args.mock_scenario)

    Handler.service = service
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[WEB] 打开 http://{args.host}:{args.port}  (Ctrl+C 退出)")
    print(f"[WEB] mode={args.mode} camera={args.camera} url={args.url or '-'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[WEB] 退出")
    finally:
        httpd.server_close()
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
