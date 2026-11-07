"""用真实照片重建随包发布的字母模板库。

只在换了 normalize / character_mask 之后需要跑一次 —— 老模板和新提取方式
不兼容（笔画粗细、采样分辨率都变了），混用会让分数整体塌到 0.5 左右。

现场应该用手机页面上的「登记字母」，这个脚本只负责给仓库留一份能直接用的
默认库（比如答辩时摄像头还没架上就能先跑起来）。

跑法：python working/regen_bank.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision import letters as L  # noqa: E402

PHOTO = ROOT / "working" / "_scratch" / "real_ab.jpg"
OUT = ROOT / "vision" / "config" / "letters.npz"


def main() -> None:
    img = cv2.imread(str(PHOTO))
    if img is None:
        raise SystemExit(f"读不到 {PHOTO}")

    found = []
    for lb in L.find_labels(img):
        tile = L.normalize(img, lb)
        mask = L.character_mask(tile) if tile is not None else None
        if mask is None:
            continue
        found.append((lb.area_px, lb.bbox[0], mask))
    # 取面积最大的两个：按 x 排会把远处那张小得多的误检标签排到前面
    # （实测就排错过，登记进去的是一块 235px 笔画的假 A）
    found.sort(key=lambda t: -t[0])
    found = [(t[1], t[2]) for t in found[:2]]
    found.sort(key=lambda t: t[0])
    if len(found) < 2:
        raise SystemExit(f"只认出 {len(found)} 个字母，至少需要 A、B 两个")

    bank = L.LetterBank()
    for letter, (_, mask) in zip("AB", found[:2]):
        bank.enroll(letter, mask)
        print(f"  登记 {letter}: 笔画 {int((mask > 0).sum())} px")
    bank.save(OUT)
    print(f"已写入 {OUT}")

    # 自检：库里的模板互相之间必须分得开
    solo_a = L.LetterBank()
    solo_a.enroll("A", found[0][1])
    solo_b = L.LetterBank()
    solo_b.enroll("B", found[1][1])
    print(f"  A 认 A {solo_a.recognize(found[0][1])[1]:.3f}   "
          f"A 认 B {solo_a.recognize(found[1][1])[1]:.3f}")
    print(f"  B 认 B {solo_b.recognize(found[1][1])[1]:.3f}   "
          f"B 认 A {solo_b.recognize(found[0][1])[1]:.3f}")
    got_a, s_a = bank.recognize(found[0][1])
    got_b, s_b = bank.recognize(found[1][1])
    print(f"  全库：{got_a} {s_a:.3f} / {got_b} {s_b:.3f}")


if __name__ == "__main__":
    main()
