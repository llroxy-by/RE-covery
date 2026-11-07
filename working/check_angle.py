"""验证「任意角度字母识别」。

先厘清真机上"旋转"到底从哪来，不然测的就不是真问题：
  · 标签是矩形，find_labels 抓到四个角，normalize 把这块四边形拉成轴对齐的
    矩形 —— 这一拉，标签自己的旋转就被抵消了。所以"积木整体转 30°"这种
    情况本来就不需要额外的角度搜索。
  · 真正留下来的是三种：① 长条标签四角排序在 90°/180° 上会翻（实测那张
    照片里 A 就是躺着的）；② 手写字母不顺着标签边写，歪十几度很常见；
    ③ 标签被手挡掉一角、四边形逼近偏一点。
  · 不管是哪种，落到 character_mask 之后的形态都是"同一副笔画转了个角度，
   ��跟着重采样和重新二值化"。

所以测试在模板空间里做：把登记好的笔画转 θ，放大重采样、二值化回模板，
再拿去认。这比把照片转 θ 更干净（不受标签截断、分辨率变化干扰），
也正是识别时要应付的东西。

跑法：python working/check_angle.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision import letters as L  # noqa: E402

PHOTO = Path(__file__).resolve().parent / "_scratch" / "real_ab.jpg"


def load_real_masks():
    img = cv2.imread(str(PHOTO))
    if img is None:
        raise SystemExit(f"读不到 {PHOTO}")
    labels = L.find_labels(img)
    got = []
    for lb in labels:
        tile = L.normalize(img, lb)
        if tile is None:
            continue
        m = L.character_mask(tile)
        if m is None:
            continue
        got.append((lb.bbox[0], m))
    got.sort(key=lambda t: t[0])
    if len(got) < 2:
        raise SystemExit("真实照片里至少要认出两个字母才能做 A/B 对照")
    return got[0][1], got[1][1]


def spin_mask(mask: np.ndarray, deg: float, seed: int = 0) -> np.ndarray:
    """把笔画转 deg 度，走一遍放大 → 旋转 → 重新二值化 → 归一化。

    故意用 2 倍分辨率转再降回来，模拟真实链路里的重采样损耗；再加一点
    椒盐噪声和 ±1px 抖动，别让测试变成"同一副位图自己跟自己比"。
    """
    rng = np.random.default_rng(seed)
    big = 160
    canvas = np.zeros((big, big), np.uint8)
    up = cv2.resize(mask, (96, 96), interpolation=cv2.INTER_NEAREST)
    y0, x0 = (big - 96) // 2 + int(rng.integers(-1, 2)), (big - 96) // 2 + int(rng.integers(-1, 2))
    canvas[y0:y0 + 96, x0:x0 + 96] = up
    M = cv2.getRotationMatrix2D((big / 2.0, big / 2.0), deg, 1.0)
    rot = cv2.warpAffine(canvas, M, (big, big), flags=cv2.INTER_LINEAR)
    rot = rot.astype(np.int16)
    noise = rng.integers(-25, 26, rot.shape)
    rot = np.clip(rot + noise, 0, 255).astype(np.uint8)
    rot = ((rot > 110).astype(np.uint8)) * 255
    crop = L._ink_crop(rot)
    if crop is None:
        return mask
    return L._fit_canvas(crop)


def main() -> None:
    mask_a, mask_b = load_real_masks()
    bank = L.LetterBank()
    bank.enroll("A", mask_a)
    bank.enroll("B", mask_b)
    print(f"库内字母 {bank.letters()}  A 笔画 {int((mask_a > 0).sum())}  B 笔画 {int((mask_b > 0).sum())}")

    angles = list(range(0, 360, 15))
    print(f"\n=== 自己的字转着认自己（{len(angles)} 个角度）===")
    print(f"{'角':>5} {'旧4向':>7} {'新15°':>7} {'认出':>5}")
    worst_old, worst_new, miss = 1.0, 1.0, 0
    for deg in angles:
        row_old, row_new, rows = [], [], []
        for tag, mask in (("A", mask_a), ("B", mask_b)):
            q = spin_mask(mask, deg, seed=deg + ord(tag))
            solo = L.LetterBank()
            solo.enroll(tag, mask)
            row_old.append(solo.recognize(q, step=90)[1])
            row_new.append(solo.recognize(q, step=15)[1])
            got, sc = bank.recognize(q)
            rows.append(f"{tag}->{got or '-'} {sc:.2f}")
            miss += got != tag
        worst_old = min(worst_old, min(row_old))
        worst_new = min(worst_new, min(row_new))
        print(f"{deg:>5} {row_old[0]:>3.2f}/{row_old[1]:<3.2f} "
              f"{row_new[0]:>3.2f}/{row_new[1]:<3.2f}   {' '.join(rows)}")
    print(f"  最低自比：旧 {worst_old:.3f}   新 {worst_new:.3f}")
    print(f"  全库 top-1 判错 {miss}/{len(angles) * 2}")

    print("\n=== 交叉：A 的字去认 B（必须低于阈值）===")
    worst_cross_old, worst_cross_new = 0.0, 0.0
    for deg in angles:
        for tag, mask in (("A", mask_a), ("B", mask_b)):
            other, om = ("B", mask_b) if tag == "A" else ("A", mask_a)
            solo = L.LetterBank()
            solo.enroll(other, om)
            q = spin_mask(mask, deg, seed=deg + ord(tag))
            worst_cross_old = max(worst_cross_old, solo.recognize(q, step=90)[1])
            worst_cross_new = max(worst_cross_new, solo.recognize(q, step=15)[1])
    print(f"  最坏交叉分：旧 {worst_cross_old:.3f}   新 {worst_cross_new:.3f}")

    print("\n=== 耗时（单次 recognize）===")
    for step in (90, 45, 30, 15):
        t0 = time.perf_counter()
        n = 15
        for i in range(n):
            bank.recognize(mask_a, step=step)
        print(f"  步进 {step:>2}°（{360 // step:>2} 档）: "
              f"{(time.perf_counter() - t0) / n * 1000:>5.1f} ms")


if __name__ == "__main__":
    main()
