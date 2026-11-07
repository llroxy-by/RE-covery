"""合成白底黑字标签，验证「找标签 → 拉正 → 提笔画 → 比对」这条路能不能走通。

这是离线自测。真实手写样本到位后再用它校准阈值。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision.letters import (LetterBank, find_labels, ink_mask,  # noqa: E402
                            normalize)

FONTS = [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX,
         cv2.FONT_HERSHEY_COMPLEX, cv2.FONT_HERSHEY_TRIPLEX]


def make_scene(letter: str, seed: int = 0, font_idx: int = 0,
               angle: float = 0.0) -> np.ndarray:
    """在一块紫红桌面上放一个白色标签，上面用黑字写 letter。"""
    rng = np.random.default_rng(seed)
    img = np.zeros((720, 1280, 3), np.uint8)
    # 桌面：紫红，模拟真实那张（H≈124, S≈133）
    img[:] = cv2.cvtColor(np.full((1, 1, 3), (124, 133, 139), np.uint8),
                          cv2.COLOR_HSV2BGR)[0, 0]
    img = np.clip(img.astype(np.int16) + rng.integers(-8, 9, img.shape), 0, 255).astype(np.uint8)

    # 标签：白底
    tw, th = 220, 160
    tag = np.full((th, tw, 3), 244, np.uint8)
    tag = np.clip(tag.astype(np.int16) + rng.integers(-6, 7, tag.shape), 0, 255).astype(np.uint8)
    font = FONTS[font_idx % len(FONTS)]
    scale, thick = 4.0, 10
    (w, h), _ = cv2.getTextSize(letter, font, scale, thick)
    cv2.putText(tag, letter, ((tw - w) // 2, (th + h) // 2), font, scale, (25, 25, 25), thick,
                cv2.LINE_AA)
    # 手写不可能正：轻微旋转 + 缩放
    M = cv2.getRotationMatrix2D((tw / 2, th / 2), angle, 1.0)
    tag = cv2.warpAffine(tag, M, (tw, th), borderValue=(244, 244, 244))

    # 贴到桌面上，再整体做一次透视，模拟手机斜拍
    x, y = 430, 250
    img[y:y + th, x:x + tw] = tag
    src = np.float32([[x, y], [x + tw, y], [x + tw, y + th], [x, y + th]])
    dst = src + np.float32([[10, 18], [-14, 6], [-6, -12], [16, -8]])
    img = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (1280, 720),
                              borderMode=cv2.BORDER_REFLECT)
    return cv2.GaussianBlur(img, (3, 3), 0)


def main() -> None:
    bank = LetterBank()
    print("=== 登记（模拟现场：把积木放镜头前敲字母）===")
    for i, letter in enumerate("ABC"):
        scene = make_scene(letter, seed=100 + i, font_idx=i, angle=-4 + i * 3)
        labels = find_labels(scene)
        if not labels:
            print(f"  {letter}: 没找到标签")
            continue
        tile = normalize(scene, labels[0])
        mask = ink_mask(tile)
        bank.enroll(letter, mask)
        print(f"  {letter}: 标签 {len(labels)} 个, 最大面积 "
              f"{labels[0].area_px:.0f}px², 笔画像素 {(mask > 0).sum() if mask is not None else 0}")

    print(f"\n库内字母: {bank.letters()}")

    print("\n=== 识别（换字体、换角度、换噪声，非同一次拍摄）===")
    ok = 0
    total = 0
    for i, letter in enumerate("ABC"):
        for j, (font_idx, angle) in enumerate([(3, 6), (1, -7), (2, 2)]):
            scene = make_scene(letter, seed=900 + i * 10 + j, font_idx=font_idx, angle=angle)
            labels = find_labels(scene)
            total += 1
            if not labels:
                print(f"  {letter} -> 没找到标签")
                continue
            mask = ink_mask(normalize(scene, labels[0]))
            got, score = bank.recognize(mask)
            hit = "OK " if got == letter else "MISS"
            ok += got == letter
            print(f"  {hit} 期望 {letter} 实得 {got or '(空)'} score={score:.3f}")

    print(f"\n命中 {ok}/{total}")

    print("\n=== 形状相近的字母会不会混 ===")
    pairs = [("M", "W"), ("6", "9"), ("O", "Q"), ("A", "H")]
    for a, b in pairs:
        bank2 = LetterBank()
        bank2.enroll(a, ink_mask(normalize(make_scene(a, seed=1), find_labels(make_scene(a, seed=1))[0])))
        mask_b = ink_mask(normalize(make_scene(b, seed=2), find_labels(make_scene(b, seed=2))[0]))
        got, score = bank2.recognize(mask_b)
        print(f"  库里只有 {a}，拿 {b} 去认 -> {got} score={score:.3f}")
    _ = pairs
    print("\n（score 越高越应该判为同一个字母；现场要不要设一个阈值，看上面这组的分布）")


if __name__ == "__main__":
    main()
