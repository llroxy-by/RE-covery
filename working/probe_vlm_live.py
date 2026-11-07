"""验证「模型慢，但视频流不慢」—— 用假模型把这条承诺压一遍。

假模型故意睡 1.2 秒再答，然后用真实照片连推 5 帧：
  · 每一帧的耗时必须保持在几十毫秒（模型在后台线程里算）；
  · 头两帧标签应该显示 vlm=pending（先拿模板结果顶着）；
  · 算完之后应该变成 vlm=hit，且 src=both（模型与模板结论一致）。

假模型不是瞎答：它把收到的 JPEG 解回图像，跑一遍同一套模板比对来"装作"会
认字。这样顺带验证了一件真事 —— 标签经过 base64 + JPEG(90) + 缩到 512
之后，笔画还没被压碎。

    python working/probe_vlm_live.py
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PHOTO = ROOT / "working" / "_scratch" / "real_ab.jpg"
CONFIG = ROOT / "vision" / "config" / "desktop.json"
BANK = ROOT / "vision" / "config" / "letters.npz"

DELAY_S = 1.2


def build_server(bank):
    """一个假的 OpenAI 兼容服务：解开图、跑模板比对、把字母回给我们。"""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):            # noqa: N802
            from urllib.parse import urlparse
            assert urlparse(self.path).path == "/v1/chat/completions", self.path
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            url = [c for c in payload["messages"][0]["content"]
                   if c.get("type") == "image_url"][0]["image_url"]["url"]
            raw = base64.b64decode(url.split(",", 1)[1])

            time.sleep(DELAY_S)       # ← 故意慢，模拟真实网络 + 推理

            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            answer = "?"
            if img is not None:
                from vision.letters import character_mask
                mask = character_mask(img)
                if mask is not None:
                    who, score = bank.recognize(mask)
                    if who and score >= 0.5:
                        answer = who
            body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    from vision.letters import LetterBank
    from vision.tools.phone_live import LiveSession

    bank = LetterBank.load(str(BANK))
    print(f"库内字母 {bank.letters()}")

    httpd, port = build_server(bank)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["label"]["reader"] = "auto"
    cfg["label"]["vlm"] = {"base_url": f"http://127.0.0.1:{port}/v1",
                           "model": "fake-vl", "api_key": "sk-test",
                           "timeout_s": 8, "max_side": 512}
    tmp = Path(tempfile.mkdtemp()) / "cfg.json"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    s = LiveSession(str(tmp), None, 0.8)
    print("reader =", s.app.detector.reader_name())
    jpeg = PHOTO.read_bytes()

    worst = 0.0
    try:
        for i in range(6):
            t0 = time.time()
            j = s.frame(jpeg)
            ms = (time.time() - t0) * 1000
            worst = max(worst, ms)
            labs = " · ".join(
                (l["letter"] or "?").ljust(1) + f" {l['score']:.2f}"
                + f" [{l['src']}|{l['vlm']}]" for l in j["labels"])
            print(f"  帧{i + 1}  {ms:6.0f} ms  objects={[o['id'] for o in j['objects']]}"
                  f"  {labs}")
            if i == 1:
                print(f"       ↑ 模型还在算，帧耗时不能被它拖住")
            time.sleep(0.35)
        st = s.app.detector.vlm.stats()
        print(f"\n模型统计：{st}")
        print(f"最慢一帧 {worst:.0f} ms（模型单次耗时约 {DELAY_S * 1000:.0f} ms）")
        assert worst < 400, f"某一帧被模型拖到 {worst:.0f} ms，实时流会卡"
        assert st["hits"] >= 2, "模型算完了却没命中缓存，peek/warm 逻辑有问题"
        print("OK：模型慢，但视频流没被拖住；算完自动接管")
        return 0
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
