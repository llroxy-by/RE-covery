"""诊断：为什么 B 认得不稳。

不稳定通常不是"算法随机"，而是某个环节对扰动过敏。这里把真实照片里 B 的
标签拿来做一组压力测试（缩放/JPEG/亮度/轻微倾斜/模糊），逐个跑完整提取
+ 识别，看分数怎么散、以及**每次选中的二值化候选是哪个**。

怀疑点：character_mask 同时跑 Otsu 和自适应两种二值化，按
`min(ink_ratio,0.35) - |ar-1|*0.05` 挑一个。B 的笔画又粗又圆，两个候选的
笔画占比差很多（实测 0.40 vs 0.25），一旦两者的 ar 接近，谁赢就取决于
零点几个百分点的长宽比 —— 等于每次都在两个不同"风格"的模板之间横跳。
登记时用了 otsu、查询时用了 adap，分数自然抖。

跑法：python working/diag_b.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision import letters as L  # noqa: E402

PHOTO = ROOT / "working" / "_scratch" / "real_ab.jpg"


def tiles_of_photo():
    img = cv2.imread(str(PHOTO))
    got = []
    for lb in L.find_labels(img):
        t = L.normalize(img, lb)
        m = L.character_mask(t) if t is not None else None
        if m is None:
            continue
        got.append((lb.bbox[0], t))
    got.sort(key=lambda t: t[0])
    return img, got[0][1], got[1][1]


def candidates(tile):
    """直接调生产实现（keep_rows=True），别再复制一份逻辑 —— 复制过一次，
    加了 Otsu 兜底之后诊断表还在报旧结果，白排查了半天。"""
    out = L.character_mask(tile, keep_rows=True)
    if out is None:
        return "", None, []
    mask, rows, who = out
    return who, mask, rows


def perturb(tile, name):
    """返回扰动后的标签图。"""
    t = tile.copy()
    if name == "base":
        return t
    if name.startswith("scale"):
        # 只改像素数，不贴白边 —— normalize 出来的标签图本来就是"整张都是标签"，
        # 贴白边会让 Otsu 把"标签 vs 白边"当成主要分界，测出来全是假象。
        f = float(name[5:]) / 100.0
        h, w = t.shape[:2]
        nw, nh = max(24, int(w * f)), max(24, int(h * f))
        interp = cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC
        return cv2.resize(t, (nw, nh), interpolation=interp)
    if name.startswith("jpeg"):
        q = int(name[4:])
        ok, buf = cv2.imencode(".jpg", t, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if name.startswith("gain"):
        f = float(name[4:]) / 100.0
        return np.clip(t.astype(np.float32) * f, 0, 255).astype(np.uint8)
    if name.startswith("rot"):
        deg = float(name[3:])
        h, w = t.shape[:2]
        big = int(np.ceil(np.hypot(h, w))) + 4
        canvas = np.full((big, big, 3), 244, np.uint8)
        y0, x0 = (big - h) // 2, (big - w) // 2
        canvas[y0:y0 + h, x0:x0 + w] = t
        # 不裁回原尺寸：转 12° 后字需要的空间比原图大，裁回去等于把字切掉
        # （第一版就这样，rot12 只有 0.45）。留整块对角线画布，四角的白
        # 正好顺便测一下 _trim_border 扛不扛得住。
        return cv2.warpAffine(canvas, cv2.getRotationMatrix2D((big / 2, big / 2), deg, 1.0),
                              (big, big), flags=cv2.INTER_LINEAR,
                              borderValue=(244, 244, 244))
    if name.startswith("blur"):
        k = int(name[4:])
        return cv2.GaussianBlur(t, (k, k), 0)
    if name == "noise":
        rng = np.random.default_rng(7)
        return np.clip(t.astype(np.int16) + rng.integers(-12, 13, t.shape), 0, 255).astype(np.uint8)
    raise ValueError(name)


NAMES = (["base"]
         + [f"scale{p}" for p in (70, 80, 90, 110, 125, 145)]
         + [f"jpeg{q}" for q in (95, 82, 65, 50, 35)]
         + [f"gain{g}" for g in (75, 88, 112, 125)]
         + [f"rot{d}" for d in (-6, -3, 3, 6, 12)]
         + [f"blur{k}" for k in (3, 5)]
         + ["noise"])


def main() -> None:
    _, ta, tb = tiles_of_photo()
    bank = L.LetterBank.load(ROOT / "vision" / "config" / "letters.npz")
    print(f"库 {bank.letters()}  阈值 {0.60}\n")

    for tag, tile in (("A", ta), ("B", tb)):
        print(f"===== {tag} =====")
        print(f"{'扰动':>9} {'选中':>5} {'分数':>5} {'认出':>4} {'角':>5}  "
              f"{'otsu(ink/ar/ok)':>22} {'adap(ink/ar/ok)':>22}")
        scores = []
        for name in NAMES:
            best_name, mask, rows = candidates(perturb(tile, name))
            if mask is None:
                print(f"{name:>9} {'-':>5} {'-':>5} {'-':>4} {'-':>5}")
                continue
            got, score = bank.recognize(mask)
            scores.append(score)
            cell = {r["name"]: (f'{r["ink"]}/{r["ar"]}/{r["ok"]}'
                                if r["ink"] is not None else "None") for r in rows}
            print(f"{name:>9} {best_name or '-':>5} {score:>5.3f} {got or '-':>4} "
                  f"{bank.last_angle:>5.0f}  {cell.get('otsu',''):>22} {cell.get('adap',''):>22}")
        arr = np.array(scores)
        print(f"  min {arr.min():.3f}  p25 {np.percentile(arr,25):.3f}  "
              f"中位 {np.median(arr):.3f}  max {arr.max():.3f}  极差 {arr.max()-arr.min():.3f}")
        # 交叉：拿到另一个字母的库里认，看会不会串
        other = L.LetterBank()
        other_tile = tb if tag == "A" else ta
        other.enroll("B" if tag == "A" else "A",
                     candidates(perturb(other_tile, "base"))[1])
        cross = [other.recognize(candidates(perturb(tile, n))[1])[1]
                 for n in NAMES if candidates(perturb(tile, n))[1] is not None]
        print(f"  串到{'B' if tag == 'A' else 'A'}上：max {max(cross):.3f}\n")


if __name__ == "__main__":
    main()
