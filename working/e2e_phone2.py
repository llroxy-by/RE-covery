"""补测：竖屏照片、缺件、遮挡。直接调 PhoneSession，不绕 HTTP。"""
import sys
from pathlib import Path
import cv2, numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision.tools.phone_capture import PhoneSession, fit_photo

OUT = ROOT / "vision" / "records" / "_e2e_phone2"
if OUT.exists():
    shutil_rm = __import__("shutil").rmtree(OUT, ignore_errors=True)

ZC = {"A": (250, 330), "B": (630, 330), "C": (1010, 330)}
PAL = {"red": (40, 40, 200), "green": (60, 190, 60), "blue": (210, 110, 30)}


def scene(blocks, occlude=None):
    W, H = 1280, 720
    img = np.full((H, W, 3), 118, np.uint8)
    img[:150] = 96
    for cx, cy in ZC.values():
        cv2.rectangle(img, (cx - 170, cy - 150), (cx + 170, cy + 150), (128, 128, 128), -1)
    for color, zone in blocks.items():
        cx, cy = ZC[zone]
        b, g, r = PAL[color]
        cv2.rectangle(img, (cx - 48, cy - 48), (cx + 48, cy + 48), (b, g, r), -1)
    if occlude:
        cx, cy = ZC[occlude]
        cv2.rectangle(img, (cx, cy - 60), (cx + 70, cy + 60), (150, 150, 150), -1)  # 遮住右半边
    return img


def to_photo(img, mode):
    W, H = 1280, 720
    if mode == "portrait":       # 竖拍 3:4，上下留大片空白
        pw, ph = 1080, 1440
        big = cv2.resize(img, (pw, int(pw * H / W)))
        c = np.full((ph, pw, 3), 118, np.uint8)
        y0 = (ph - big.shape[0]) // 2
        c[y0:y0 + big.shape[0]] = big
        return c
    if mode == "wide":           # 横拍 16:9，几乎不裁
        return cv2.resize(img, (1920, 1080))
    raise ValueError(mode)


def enc(img):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return buf.tobytes()


fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        fails.append(label)


print("=== A. 竖屏 3:4 照片 ===")
s = PhoneSession(str(ROOT / "vision/config/desktop.json"), str(OUT), 0.8)
img = to_photo(scene({"red": "A", "green": "B", "blue": "C"}), "portrait")
print(f"  原图 {img.shape[1]}x{img.shape[0]} → 裁切后 1280x720")
r = s.handle("0", enc(img))
os_ = {o["object_id"]: o for o in s.app.scene["objects"]}
check("竖屏仍能建 S0", r.get("ok") is True, str(r.get("issues"))[:120])
check("竖屏检出三色", len(os_) == 3, str(list(os_)))
check("竖屏分区未错位",
      all(os_[k]["zone"] == v for k, v in
          [("block_red_01", "A"), ("block_green_01", "B"), ("block_blue_01", "C")]),
      str({k: v["zone"] for k, v in os_.items()}))

print("=== B. 横拍 16:9 ===")
s2 = PhoneSession(str(ROOT / "vision/config/desktop.json"), str(OUT), 0.8)
r = s2.handle("0", enc(to_photo(scene({"red": "A", "green": "B", "blue": "C"}), "wide")))
check("横拍建 S0", r.get("ok") is True, str(r.get("issues"))[:120])
check("横拍检出三色", len(s2.app.scene["objects"]) == 3)

print("=== C. 少一块（红块被拿走）===")
s3 = PhoneSession(str(ROOT / "vision/config/desktop.json"), str(OUT), 0.8)
s3.handle("0", enc(to_photo(scene({"red": "A", "green": "B", "blue": "C"}), "wide")))
r = s3.handle("1", enc(to_photo(scene({"green": "B", "blue": "C"}), "wide")))
by = {d["object_id"]: d for d in r["differences"]}
red = by.get("block_red_01", {})
print(f"  红块结论 = {red.get('status')}  理由 = {red.get('reasons')}")
check("少一块不得判 missing", "missing" not in (red.get("status") or ""), red.get("status"))
check("少一块必须是 uncertain", red.get("status") == "uncertain", red.get("status"))
check("少一块理由含 not_detected 或 not_observed",
      any("not_detected" == x or "not_observed" == x for x in red.get("reasons", [])),
      str(red.get("reasons")))
check("整体不得 READY", r.get("verdict") != "READY", str(r.get("verdict")))

print("=== D. 遮挡（红块右半被挡）===")
s4 = PhoneSession(str(ROOT / "vision/config/desktop.json"), str(OUT), 0.8)
s4.handle("0", enc(to_photo(scene({"red": "A", "green": "B", "blue": "C"}), "wide")))
r = s4.handle("1", enc(to_photo(scene({"red": "A", "green": "B", "blue": "C"}, occlude="A"), "wide")))
by = {d["object_id"]: d for d in r["differences"]}
red = by.get("block_red_01", {})
print(f"  红块结论 = {red.get('status')}  理由 = {red.get('reasons')}")
check("遮挡不得判 unchanged", red.get("status") != "unchanged", red.get("status"))
check("遮挡必须是 uncertain", red.get("status") == "uncertain", red.get("status"))

print()
print("=" * 46)
print("全部通过" if not fails else f"失败 {len(fails)} 项：" + "; ".join(fails))
raise SystemExit(0 if not fails else 1)
