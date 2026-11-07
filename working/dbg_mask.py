"""把 A/B 的标签图和各路二值化结果拼成一张对照图，肉眼确认哪一步坏了。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision import letters as L  # noqa: E402

OUT = Path(__file__).resolve().parent / "_scratch"


def tile_of(idx: int):
    img = cv2.imread(str(OUT / "real_ab.jpg"))
    labels = L.find_labels(img, {"canny": [60, 160]})
    good = [b for b in labels if L.character_mask(L.normalize(img, b)) is not None]
    good.sort(key=lambda b: b.bbox[0])
    return img, L.normalize(img, good[idx])


def canon(img, n=160):
    h, w = img.shape[:2]
    s = n / float(max(h, w))
    return cv2.resize(img, (max(8, int(round(w * s))), max(8, int(round(h * s)))),
                      interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)


def var_bin(gray, name):
    inv = 255 - gray
    out = {}
    _, otsu = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    out["otsu_" + name] = otsu
    block = max(15, (min(gray.shape[:2]) // 3) | 1)
    out["adap_" + name] = cv2.adaptiveThreshold(
        inv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, -8)
    return out


def main():
    for idx, name in ((0, "A"), (1, "B")):
        _, tile = tile_of(idx)
        panels = []
        # 原图
        panels.append((f"{name} tile {tile.shape[1]}x{tile.shape[0]}", tile))
        # 原图直接二值化（旧路径，不归一化分辨率）
        g0 = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
        for k, v in var_bin(g0, "raw").items():
            m = L._clean_edges(v, v.shape)
            c = L._ink_crop(m)
            panels.append((f"{k} ink={0 if c is None else int((c>0).sum())}",
                           L._fit_canvas(c) if c is not None else v))
        # 归一到 160 再二值化（新路径）
        t2 = canon(tile)
        g1 = cv2.cvtColor(t2, cv2.COLOR_BGR2GRAY)
        for k, v in var_bin(g1, "canon").items():
            m = L._clean_edges(v, v.shape)
            c = L._ink_crop(m)
            panels.append((f"{k} ink={0 if c is None else int((c>0).sum())}",
                           L._fit_canvas(c) if c is not None else v))
        # character_mask 最终结果
        fin = L.character_mask(tile)
        panels.append((f"character_mask ink={0 if fin is None else int((fin>0).sum())}",
                       fin if fin is not None else tile))

        cell = 200
        sheet = np.full((cell + 22, cell * len(panels), 3), 30, np.uint8)
        for i, (label, im) in enumerate(panels):
            if im.ndim == 2:
                im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
            h, w = im.shape[:2]
            s = min((cell - 8) / w, (cell - 8) / h)
            im = cv2.resize(im, (max(1, int(w * s)), max(1, int(h * s))),
                            interpolation=cv2.INTER_NEAREST)
            y0 = 22 + (cell - 8 - im.shape[0]) // 2
            x0 = i * cell + (cell - im.shape[1]) // 2
            sheet[y0:y0 + im.shape[0], x0:x0 + im.shape[1]] = im
            cv2.putText(sheet, label[:30], (i * cell + 3, 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 255, 255), 1)
        p = OUT / f"_dbg_mask_{name}.png"
        cv2.imwrite(str(p), sheet)
        print(p)


if __name__ == "__main__":
    main()
