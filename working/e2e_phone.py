"""端到端压测 phone_capture：起服务 → 手机式 HTTP 上传两张 → 看结论。"""
import json, subprocess, sys, time, urllib.request, urllib.error, os
from pathlib import Path

import cv2, numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PORT = 8317
BASE = f"http://127.0.0.1:{PORT}"

# 手机竖拍 4:3 → 1920x1440，故意用非配置分辨率，检验居中裁切
PHONE = (1920, 1440)
# 在 1280x720 逻辑坐标里画，再整体放大到 PHONE 尺寸（保证裁切后落在正确位置）
ZONES = {"A": (250, 330), "B": (630, 330), "C": (1010, 330)}


def desk(blocks):
    """blocks: {color: zone}，返回手机照片 BGR。"""
    W, H = 1280, 720
    img = np.full((H, W, 3), 118, np.uint8)          # 桌面灰
    img[:150] = 96                                    # 远侧阴影
    for name, (cx, cy) in ZONES.items():              # 分区底色（浅一点）
        cv2.rectangle(img, (cx - 170, cy - 150), (cx + 170, cy + 150), (128, 128, 128), -1)
    palette = {"red": (40, 40, 200), "green": (60, 190, 60), "blue": (210, 110, 30)}
    for color, zone in blocks.items():
        cx, cy = ZONES[zone]
        b, g, r = palette[color]
        cv2.rectangle(img, (cx - 48, cy - 48), (cx + 48, cy + 48), (b, g, r), -1)
        cv2.rectangle(img, (cx - 48, cy - 48), (cx + 48, cy + 48), (int(b * .6), int(g * .6), int(r * .6)), 2)
    # 缩到手机照片尺寸：先按 4:3 扩高度，再放大 → 相当于手机把画面拍小了
    big = cv2.resize(img, (PHONE[0], int(PHONE[0] * H / W)), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((PHONE[1], PHONE[0], 3), 118, np.uint8)
    y0 = (PHONE[1] - big.shape[0]) // 2
    canvas[y0:y0 + big.shape[0]] = big
    return canvas


def jpg(img):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    assert ok
    return buf.tobytes()


def post(slot, data):
    req = urllib.request.Request(f"{BASE}/upload?slot={slot}", data=data,
                                 headers={"Content-Type": "image/jpeg"}, method="POST")
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read())


def get(path):
    with opener.open(f"{BASE}{path}", timeout=30) as r:
        return r.status, r.read()


def wait_up(n=60):
    for _ in range(n):
        try:
            get("/")
            return True
        except Exception:
            time.sleep(0.3)
    return False


fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        fails.append(label)


tmp_out = ROOT / "vision" / "records" / "_e2e_phone"
proc = subprocess.Popen(
    [sys.executable, "-m", "vision.tools.phone_capture", "--port", str(PORT), "--output", str(tmp_out)],
    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    print(f"[1] 起服务 :{PORT}")
    up = wait_up()
    check("服务能起来并响应 /", up)
    if not up:
        print(proc.stdout.read() if proc.stdout else "")
        raise SystemExit(1)

    print("[2] GET / 首页")
    st, body = get("/")
    html = body.decode("utf-8", "replace")
    check("首页 200", st == 200)
    check("首页含 S0 按钮", "建立 S0" in html)
    check("首页含 S1 按钮", "与 S0 比较" in html)
    check("首页显示基准未建立", "未建立" in html)

    print("[3] 先测护栏：没 S0 就传 S1")
    try:
        r = post("1", jpg(desk({"red": "A", "green": "B", "blue": "C"})))
        check("无基准时拒绝 S1", False, f"居然返回 {r}")
    except urllib.error.HTTPError as e:
        msg = json.loads(e.read()).get("error", "")
        check("无基准时拒绝 S1", e.code == 400 and "还没有 S0" in msg, f"{e.code} {msg}")

    print("[4] 传 S0（红A 绿B 蓝C）")
    r = post("0", jpg(desk({"red": "A", "green": "B", "blue": "C"})))
    check("S0 返回 ok", r.get("ok") is True, str(r))
    base = json.loads((tmp_out / "diff_placeholder.json").read_text()) if False else None
    jsons = sorted(tmp_out.glob("*.json"))
    check("S0 落了场景 JSON", len(jsons) >= 1, f"文件数 {len(jsons)}")
    scene0 = json.loads(jsons[0].read_text())
    check("S0 mode=real", scene0["mode"] == "real", scene0["mode"])
    check("S0 source=phone:upload", scene0["source"] == "phone:upload", scene0["source"])
    ids = {o["object_id"] for o in scene0["objects"]}
    check("S0 检出三色", ids == {"block_red_01", "block_green_01", "block_blue_01"}, str(ids))
    zones = {o["object_id"]: o["zone"] for o in scene0["objects"]}
    check("S0 分区正确", zones == {"block_red_01": "A", "block_green_01": "B", "block_blue_01": "C"}, str(zones))

    print("[5] GET /result 看 S0 结果页")
    st, body = get("/result")
    check("结果页 200", st == 200)
    check("结果页显示 S0 已建立", "S0 基准已建立" in body.decode("utf-8", "replace"))

    print("[6] GET /annotated.jpg")
    st, body = get("/annotated.jpg")
    check("标注图 200", st == 200)
    check("标注图是 JPEG", body[:2] == b"\xff\xd8", body[:4].hex())
    check("标注图非空", len(body) > 5000, f"{len(body)} 字节")

    print("[7] 传 S1：把红蓝对调（红→C 蓝→A）")
    r = post("1", jpg(desk({"red": "C", "green": "B", "blue": "A"})))
    check("S1 返回 ok", r.get("ok") is True, str(r))
    check("整体判 misplaced", r.get("status") == "misplaced", r.get("status"))

    diffs = sorted(tmp_out.glob("diff_*.json"))
    check("落了差异 JSON", len(diffs) >= 1, f"{len(diffs)} 个")
    d = json.loads(diffs[-1].read_text())
    by_id = {x["object_id"]: x for x in d["differences"]}
    check("红被判 misplaced", by_id["block_red_01"]["status"] == "misplaced", str(by_id["block_red_01"]))
    check("红 from A to C",
          by_id["block_red_01"]["from_zone"] == "A" and by_id["block_red_01"]["to_zone"] == "C",
          f"{by_id['block_red_01']['from_zone']}->{by_id['block_red_01']['to_zone']}")
    check("蓝被判 misplaced", by_id["block_blue_01"]["status"] == "misplaced", str(by_id["block_blue_01"]))
    check("绿 unchanged", by_id["block_green_01"]["status"] == "unchanged", str(by_id["block_green_01"]))
    check("差异 mode 一致", d["mode"] == "real", d["mode"])

    print("[8] GET /result 看 S1 结果页")
    st, body = get("/result")
    h = body.decode("utf-8", "replace")
    check("结果页 200", st == 200)
    check("结果页显示 S1 已比较", "S1 已比较" in h)
    check("结果页列出 misplaced", "misplaced" in h)
    check("结果页含判据", "READY" in h or "REVIEW" in h or "NOT_READY" in h)

    print("[9] 护栏：上传垃圾字节")
    try:
        r = post("1", b"\x00" * 500)
        check("垃圾字节被拒", False, str(r))
    except urllib.error.HTTPError as e:
        check("垃圾字节被拒", e.code == 400, str(e.code))

    print("[10] 护栏：0 字节")
    try:
        r = post("1", b"")
        check("空上传被拒", False, str(r))
    except urllib.error.HTTPError as e:
        check("空上传被拒", e.code == 400, str(e.code))
finally:
    proc.terminate()
    try:
        out = proc.communicate(timeout=8)[0]
    except subprocess.TimeoutExpired:
        proc.kill(); out = ""
    if out and out.strip():
        print("\n--- 服务端输出 ---\n" + out.strip()[:2000])

print()
print("=" * 46)
print("全部通过" if not fails else f"失败 {len(fails)} 项：" + "; ".join(fails))
raise SystemExit(0 if not fails else 1)
