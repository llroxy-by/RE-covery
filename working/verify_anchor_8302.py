"""在独立端口跑 phone_live 的完整链路，确认 anchor 缺件预检在最新代码下正确。

用途：验证 8300 上「对准画面却被判缺目标」到底是不是用户实时流串扰。
"""
import glob
import io
import json
import ssl
import sys
import urllib.request

import cv2

BASE = "https://127.0.0.1:8302"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def post(path, body=b"", ctype="application/octet-stream"):
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def get(path):
    with urllib.request.urlopen(BASE + path, context=CTX, timeout=15) as r:
        return r.status, r.read()


def jpeg_of(prefix):
    p = glob.glob(f"vision/records/{prefix}*_raw.jpg")[0]
    img = cv2.imread(p)
    h, w = img.shape[:2]
    if w > 1280:              # 模拟手机端缩图上传
        img = cv2.resize(img, (1280, int(h * 1280 / w)))
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes(), p


def feed(tag, data, n=8):
    last = None
    for i in range(n):
        st, js = post("/frame", data)
        if st != 200:
            print(f"[{tag}] 第{i+1}帧 HTTP {st} {js}")
            return None
        last = js
        objs = [(o["id"], o["zone"], o["conf"]) for o in js["objects"]]
        if i == n - 1:
            print(f"[{tag}] 第{i+1}帧 settled: {objs} | issues={js['issues'] or '无'}")
    return last


print("=== 0. 页面存活 ===")
st, html = get("/watch")
HAS_BTN = "建立基准" in html.decode("utf-8", "replace")
print(f"/watch {st} {len(html)}字节  含按钮={HAS_BTN}")

print("\n=== 1. 空桌面：应明确报缺件 ===")
blank = (255 * __import__("numpy").ones((720, 1280, 3))).astype("uint8")
ok, buf = cv2.imencode(".jpg", blank)
feed("空画面", buf.tobytes(), 3)
st, js = post("/anchor")
print(f"anchor ok={js.get('ok')} | {js.get('title')}")
print(f"hints: {'; '.join(js.get('hints') or [])}")

print("\n=== 2. S0 真实照片：应建基准成功 ===")
d0, p0 = jpeg_of("570c6cba")
print(f"用图: {p0}")
feed("S0", d0, 8)
st, js = post("/anchor")
print(f"anchor ok={js.get('ok')} | {js.get('title')}")
if not js.get("ok"):
    print(f"issues={js.get('issues')}")
    print(f"hints={js.get('hints')}")

print("\n=== 3. S1 对调后：应判 misplaced / NOT_READY ===")
d1, p1 = jpeg_of("4c336d45")
print(f"用图: {p1}")
feed("S1", d1, 8)
st, js = post("/compare")
print(f"compare {st} status={js.get('status')} verdict={js.get('verdict')}")
for x in js.get("differences") or []:
    print(f"   {x['object_id']:16s} {x['status']:10s} {x['from_zone']} -> {x['to_zone']}")
print(f"issues={js.get('issues') or '无'}")
