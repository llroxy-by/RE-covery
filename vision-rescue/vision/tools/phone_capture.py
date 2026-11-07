"""手机拍照采集：不用装 App，手机浏览器打开网页拍两张就能建 S0 / 比 S1。

    python -m vision.tools.phone_capture --host 0.0.0.0 --port 8300

然后在手机浏览器打开 http://<电脑IP>:8300 ，按页面提示拍两张：
  第一张 → 建立 S0 基准      第二张 → 与 S0 比较，当场出差异结论

为什么走这条路：Android/iOS 上 `getUserMedia` 要求 HTTPS 安全上下文，
局域网 http 会被浏览器拦；而 `<input capture>` 调起系统相机拍照不受这个限制。
代价是没有连续视频流，因此**稳定性校验被跳过**（单帧无法判断抖动），
所以每张照片的质量需要人自己确认：画面清晰、手已退出、没有反光。
产物与命令行完全一致：records/<uuid>.json / _raw.jpg / _annotated.jpg / diff_*.json
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from .. import diff as diff_mod
from ..detector import StabilityTracker
from ..main import VisionApp
from ..sources import Frame, utc_now_iso
from ..snapshot import baseline_blockers

MAX_UPLOAD = 20 * 1024 * 1024


def fit_photo(img: np.ndarray, target) -> np.ndarray:
    """手机照片通常是 4:3 或竖屏，**居中裁切**成配置的宽高比再缩放。

    不裁直接拉伸会把分区坐标整体扯歪；裁切至少保证画面中心那块比例正确。
    """
    tw, th = int(target[0]), int(target[1])
    h, w = img.shape[:2]
    tgt = tw / th
    if abs(w / h - tgt) > 0.01:
        if w / h > tgt:               # 太宽：裁两侧
            nw = int(round(h * tgt))
            x0 = (w - nw) // 2
            img = img[:, x0:x0 + nw]
        else:                          # 太高：裁上下
            nh = int(round(w / tgt))
            y0 = (h - nh) // 2
            img = img[y0:y0 + nh, :]
    return cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)


class PhoneSession:
    """一个进程内保存基准与最近一次结果。手机端只负责传图。"""

    def __init__(self, config_path: str, output: str | None, min_conf: float) -> None:
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.app = VisionApp(SimpleNamespace(
            camera=0, config=config_path, mode="real", output=output,
            min_conf=min_conf, mock_cycle=False, skip_image_check=False,
            allow_frozen=False, url=None))
        # 单帧拍照没有"连续帧"，把稳定性判定降为 1 帧；语义上等价于"这张就算定稿"
        self.app.tracker = StabilityTracker(1, 1e9)
        self.app._source = None
        self.lock = threading.Lock()
        self.baseline = None
        self.last: dict | None = None
        self.annotated_jpg: bytes | None = None

    def handle(self, slot: str, jpeg: bytes) -> dict:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("上传的不是可解码的图片")
        target = self.cfg.get("resolution", [1280, 720])
        img = fit_photo(img, target)
        frame = Frame(image=img, captured_at=utc_now_iso(), source="phone:upload",
                      mode="real", index=1)

        with self.lock:
            objects, quality_ok, issues = self.app.observe(frame)
            ann = self.app.annotate(frame, objects, issues)
            ok, buf = cv2.imencode(".jpg", ann, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            self.annotated_jpg = buf.tobytes() if ok else None
            if slot == "0":
                blockers = baseline_blockers(self.app.scene, self.app.min_conf)
                if blockers:
                    self.app.rejected_store.save(self.app.scene, frame.image, ann)
                    self.last = {"slot": "0", "ok": False, "title": "S0 被拒绝（基准不合格）",
                                 "issues": blockers, "objects": objects,
                                 "hints": self.app.tuning_hints(objects), "quality_ok": False}
                    return self.last
                saved = self.app.store.save(self.app.scene, frame.image, ann)
                self.baseline = self.app.scene
                self.last = {"slot": "0", "ok": True, "title": "S0 基准已建立",
                             "issues": [], "objects": objects, "quality_ok": quality_ok,
                             "saved": str(saved["json"])}
                return self.last

            if self.baseline is None:
                raise ValueError("还没有 S0 基准，先拍第一张")
            saved = self.app.store.save(self.app.scene, frame.image, ann)
            result = diff_mod.compare(self.baseline, self.app.scene,
                                      min_conf=self.app.min_conf)
            path = diff_mod.save_differences(result, self.app.output_dir)
            self.last = {"slot": "1", "ok": True, "title": "S1 已比较",
                         "status": result["status"],
                         "verdict": diff_mod.gate_verdict(result),
                         "issues": result["issues"],
                         "differences": result["differences"],
                         "objects": objects, "quality_ok": quality_ok,
                         "saved": str(saved["json"]), "diff": str(path)}
            return self.last

    def baseline_id(self) -> str | None:
        return (self.baseline or {}).get("snapshot_id")


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE-covery 手机采集</title>
<style>
 :root {{ --bg:#14161a; --panel:#1d2026; --line:#2c3138; --fg:#e8eaed; --dim:#98a0ac; --ok:#3ddc84; --warn:#ffb020; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg);
        font:16px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
 h1 {{ font-size:18px; margin:0 0 4px; }}
 .dim {{ color:var(--dim); font-size:13px; }}
 .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
          padding:14px; margin:12px 0; }}
 label.btn {{ display:block; text-align:center; padding:18px; border-radius:10px;
              background:#1e3a5f; border:1px solid #2f5f96; font-size:17px; margin-top:10px; }}
 label.btn.alt {{ background:#3a341d; border-color:#6b5a2a; }}
 input[type=file] {{ display:none; }}
 .st {{ font-size:15px; }}
 .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }}
 table {{ width:100%; border-collapse:collapse; font-size:14px; margin-top:8px; }}
 th,td {{ text-align:left; padding:6px; border-bottom:1px solid var(--line); }}
 th {{ color:var(--dim); font-weight:600; }}
 img {{ width:100%; border-radius:8px; margin-top:8px; }}
 a {{ color:#4aa8ff; }}
</style></head><body>
<h1>RE-covery · 手机采集</h1>
<p class="dim">不用装 App。两步：先拍 S0，再拍 S1。</p>

<div class="card">
  <div class="st">当前基准：{baseline}</div>
  <label class="btn">① 拍照 · 建立 S0 基准
    <input type="file" accept="image/*" capture="environment" onchange="up(this,0)">
  </label>
  <label class="btn alt">② 拍照 · 与 S0 比较
    <input type="file" accept="image/*" capture="environment" onchange="up(this,1)">
  </label>
  <p class="dim" id="msg">拍完会自动上传并跳到结果页。</p>
</div>

<div class="card">
  <div class="st">注意事项</div>
  <p class="dim">· 手机固定住（靠在书/支架上），别手持<br>
     · 画面里能同时看到 A/B/C/D/TEMP 五个区域<br>
     · 拍之前把手移开，别挡镜头<br>
     · 四角区域靠边太多可能被裁掉：照片是 4:3 时会居中裁成 16:9</p>
</div>

<script>
async function up(input, slot) {{
  const f = input.files[0];
  if (!f) return;
  document.getElementById('msg').textContent = '上传中…';
  const buf = await f.arrayBuffer();
  const r = await fetch('/upload?slot=' + slot, {{method:'POST',
      headers:{{'Content-Type':'image/jpeg'}}, body: buf}});
  const j = await r.json();
  if (!j.ok && j.error) {{ document.getElementById('msg').textContent = '失败：' + j.error; }}
  else {{ location.href = '/result'; }}
}}
</script></body></html>"""

RESULT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>结果</title><style>
 :root {{ --bg:#14161a; --panel:#1d2026; --line:#2c3138; --fg:#e8eaed; --dim:#98a0ac; --ok:#3ddc84; --warn:#ffb020; --bad:#ff5c5c; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg);
        font:16px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
 h1 {{ font-size:17px; margin:0 0 8px; }}
 .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; margin:12px 0; }}
 .dim {{ color:var(--dim); font-size:13px; }}
 table {{ width:100%; border-collapse:collapse; font-size:14px; }}
 th,td {{ text-align:left; padding:6px; border-bottom:1px solid var(--line); }}
 th {{ color:var(--dim); }}
 .vis {{ color:var(--ok); }} .mis {{ color:var(--warn); }} .unc {{ color:var(--bad); }}
 img {{ width:100%; border-radius:8px; }}
 a.btn {{ display:block; text-align:center; padding:16px; border-radius:10px;
          background:#1e3a5f; border:1px solid #2f5f96; color:var(--fg); text-decoration:none; }}
</style></head><body>
<h1>{title}</h1>
<div class="card">{body}</div>
<img src="/annotated.jpg?t={ts}" alt="annotated">
<a class="btn" href="/">再拍一张</a>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    session: PhoneSession | None = None

    def log_message(self, fmt, *args):
        # 不打印就没法判断"手机到底连上没连上"。来源 IP 直接暴露手机在哪个网段。
        print(f"[PHONE] {self.client_address[0]} {fmt % args}", flush=True)

    def _html(self, text: str, code: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            bid = self.session.baseline_id()
            self._html(PAGE.format(baseline=(bid[:8] + "…") if bid else "未建立"))
        elif u.path == "/result":
            last = self.session.last
            if not last:
                self._html(RESULT.format(title="还没有结果", body="<p class='dim'>先拍一张</p>", ts=0))
                return
            self._html(RESULT.format(title=last["title"], body=_body(last), ts=id(last)))
        elif u.path == "/annotated.jpg":
            jpg = self.session.annotated_jpg
            if not jpg:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpg)
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path != "/upload":
            self.send_error(404)
            return
        slot = (parse_qs(u.query).get("slot") or ["0"])[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            self._json({"ok": False, "error": f"图片大小异常（{length} 字节）"}, 400)
            return
        data = self.rfile.read(length)
        try:
            result = self.session.handle(slot, data)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        self._json({"ok": True, "slot": slot, "status": result.get("status")})


def _body(last: dict) -> str:
    parts = []
    if last.get("status"):
        cls = "vis" if last["status"] == "unchanged" else ("unc" if last["status"] == "uncertain" else "mis")
        parts.append(f"<p>整体：<b class='{cls}'>{last['status']}</b> · 判据：<b>{last.get('verdict','-')}</b></p>")
    rows = []
    for d in last.get("differences", []):
        cls = "vis" if d["status"] == "unchanged" else ("unc" if d["status"] == "uncertain" else "mis")
        rows.append(f"<tr><td>{d['object_id']}</td><td class='{cls}'>{d['status']}</td>"
                    f"<td>{d['from_zone']} → {d['to_zone']}</td></tr>")
    if rows:
        parts.append("<table><tr><th>ID</th><th>status</th><th>from→to</th></tr>"
                     + "".join(rows) + "</table>")
    else:
        rows = "".join(
            f"<tr><td>{o['object_id']}</td><td>{o['zone']}</td><td>{o['visibility']}</td>"
            f"<td>{o['confidence']:.2f}</td></tr>" for o in last.get("objects", []))
        parts.append("<table><tr><th>ID</th><th>zone</th><th>vis</th><th>conf</th></tr>"
                     + rows + "</table>")
    for title, key in (("问题", "issues"), ("调参建议", "hints")):
        if last.get(key):
            items = "".join(f"<li>{x}</li>" for x in last[key])
            parts.append(f"<p class='dim'>{title}：</p><ul class='dim'>{items}</ul>")
    return "".join(parts)


def _lan_ips() -> list[str]:
    """列出手机可能访问到的本机局域网 IPv4。

    跳过 127.*（本机回环，手机用不了）和 169.254.*（没拿到 DHCP 的兜底地址，
    网卡其实没连上，写出来只会让人白试）。
    """
    ips: list[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
            ips.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    # getaddrinfo 常只返回一个地址；再探一下默认出口地址（UDP connect 不发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return ips


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="手机拍照采集（免装 App）")
    p.add_argument("--config", default="vision/config/desktop.json")
    p.add_argument("--output", default=None)
    p.add_argument("--min-conf", type=float, default=0.8)
    p.add_argument("--port", type=int, default=8300)
    p.add_argument("--host", default="0.0.0.0", help="手机要能访问，默认监听所有网卡")
    args = p.parse_args(argv)

    session = PhoneSession(args.config, args.output, args.min_conf)
    Handler.session = session
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    print("=" * 52, flush=True)
    if args.host in ("0.0.0.0", ""):
        ips = _lan_ips()
        if not ips:
            print("[PHONE] 没找到局域网地址：先确认这台电脑已连上 Wi-Fi", flush=True)
        for ip in ips:
            # 192.168.137.0/24 是 Windows 移动热点的固定网段，手机连热点时必须用这个
            tag = "  ← 手机连『移动热点』就用这个" if ip.startswith("192.168.137.") else ""
            print(f"[PHONE] 手机浏览器打开   http://{ip}:{args.port}{tag}", flush=True)
        print("[PHONE] 手机要和电脑连同一个 Wi-Fi（同一网段）", flush=True)
    else:
        print(f"[PHONE] 只监听 {args.host}，手机访问  http://{args.host}:{args.port}", flush=True)
    print(f"[PHONE] 电脑本机自测         http://127.0.0.1:{args.port}", flush=True)
    print("[PHONE] 手机打不开时按顺序查：① 是否同一 Wi-Fi ② 防火墙弹窗是否点过'允许' "
          "③ Wi-Fi 是否开了客户端隔离（校园/公司网常见）", flush=True)
    print("=" * 52, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[PHONE] 退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
