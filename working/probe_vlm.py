"""探明 WorkBuddy 本地视觉模型网关的调用格式。

环境里已经注入了整套凭证：
  WORKBUDDY_VLM_CHAT_URL   http://127.0.0.1:61801/skills/vlm/chat
  WORKBUDDY_VLM_AUTH_TOKEN IPY_...
  WORKBUDDY_VLM_MODEL      kinfra-office-agent
  WORKBUDDY_VLM_CONTEXT    base64 的会话上下文

先用一张真实标签图试探：是 OpenAI 兼容的 chat/completions 吗？图片走
data URI 还是别的字段？探明了再写正式封装。

跑法：python working/probe_vlm.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision import letters as L  # noqa: E402

URL = os.environ.get("WORKBUDDY_VLM_CHAT_URL", "")
TOKEN = os.environ.get("WORKBUDDY_VLM_AUTH_TOKEN", "")
MODEL = os.environ.get("WORKBUDDY_VLM_MODEL", "")
CTX = os.environ.get("WORKBUDDY_VLM_CONTEXT", "")

PROMPT = "这张图里手写/印的是什么字母？只回一个大写字母。看不清就回 ?"


def png_b64(img) -> str:
    ok, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode("ascii")


def post(payload: dict, headers: dict, tag: str) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
            print(f"[{tag}] {r.status} {body[:600]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"[{tag}] HTTP {e.code} {body[:600]}")
    except Exception as e:                       # noqa: BLE001
        print(f"[{tag}] {type(e).__name__}: {e}")


def main() -> None:
    if not URL:
        raise SystemExit("没有 WORKBUDDY_VLM_CHAT_URL")
    img = cv2.imread(str(ROOT / "working" / "_scratch" / "real_ab.jpg"))
    lb = sorted(L.find_labels(img), key=lambda b: -b.area_px)[0]
    tile = L.normalize(img, lb, 320)
    p = png_b64(tile)
    print(f"图 {tile.shape[1]}x{tile.shape[0]}  base64 {len(p)} 字符  model={MODEL}")

    auth = f"Bearer {TOKEN}"
    base_h = {"Content-Type": "application/json", "Authorization": auth}

    # 1) 最标准的 OpenAI vision 格式
    msg = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{p}"}},
    ]}]
    post({"model": MODEL, "messages": msg, "max_tokens": 16}, base_h, "openai-vision")

    # 2) 带 session 上下文
    h2 = dict(base_h)
    if CTX:
        h2["X-Vlm-Context"] = CTX
        h2["X-WorkBuddy-Context"] = CTX
    post({"model": MODEL, "messages": msg, "max_tokens": 16}, h2, "openai+ctx")

    # 3) 纯文本，看网关本身通不通
    post({"model": MODEL, "messages": [{"role": "user", "content": "回一个字母 A"}],
          "max_tokens": 8}, base_h, "text-only")


if __name__ == "__main__":
    main()
