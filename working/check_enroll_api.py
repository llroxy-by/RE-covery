"""测手机实时服务的字母登记：/label 端点 + 身份切换。

库空时应该仍然按颜色认（不然刚装好就不可用）；登记一个字母后，
同一块积木的 object_id 应该变成 block_A。
"""
from __future__ import annotations

import glob
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

BASE = "https://127.0.0.1:8302"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def post(path, body=b"", ctype="application/octet-stream"):
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def jpeg(p):
    img = cv2.imread(str(p))
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes()


def feed(path, n=8):
    data = jpeg(path)
    last = None
    for _ in range(n):
        st, js = post("/frame", data)
        if st != 200:
            print(f"  HTTP {st} {js}")
            return None
        last = js
    return last


def show(tag, js):
    if not js:
        print(f"  {tag}: 无响应")
        return
    objs = [(o["id"], o["zone"], o["conf"]) for o in js["objects"]]
    print(f"  {tag}: {objs}")
    print(f"       expected={js.get('expected')} issues={js.get('issues') or '无'}")


print("=== 1. 页面里应该有登记输入框 ===")
with urllib.request.urlopen(BASE + "/", context=CTX, timeout=15) as r:
    html = r.read().decode("utf-8", "replace")
print(f"  含『登记字母』: {'登记字母' in html}   含 /label: {'/label' in html}")

print("\n=== 2. 库是空的：应该退回按颜色认身份 ===")
s0 = glob.glob("vision/records/570c6cba*_raw.jpg")[0]
show("S0 原图", feed(s0))

print("\n=== 3. 真实照片上没有标签，登记应该被明确拒绝 ===")
st, js = post("/label", json.dumps({"letter": "A"}).encode(), "application/json")
print(f"  {st} ok={js.get('ok')} | {js.get('title')}")
for h in js.get("hints") or []:
    print(f"     · {h}")

print("\n=== 4. 换成贴了标签的图，登记 A ===")
tagged = Path("working/_scratch/tagged_s0.jpg")
if not tagged.exists():
    print(f"  缺少 {tagged}，先跑 check_label_pipeline.py")
    sys.exit(1)
show("贴标签的 S0", feed(tagged))
st, js = post("/label", json.dumps({"letter": "A"}).encode(), "application/json")
print(f"  {st} ok={js.get('ok')} | {js.get('title')} letters={js.get('letters')}")
for h in js.get("hints") or []:
    print(f"     · {h}")

print("\n=== 5. 再灌同一张图：object_id 应该变成 block_A ===")
show("登记后", feed(tagged))

print("\n=== 6. 换回没标签的原图：那块应该报『字母认不出来』而不是瞎猜 ===")
show("原图（无标签）", feed(s0))
