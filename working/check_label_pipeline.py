"""端到端：把真实那张照片丢进完整链路，看能不能同时认出 A 和 B，并且不怕摆歪。

为什么要重写：这个脚本原来是给「颜色定位 + 字母认身份」那套写的，第 1 步要靠
颜色先框出两块积木。后来发现紫色灯光下颜色彻底不可靠（绿积木 H=123 被配置
的 40-85 漏掉、红积木被白标签切成两半），改成了「标签即定位」，颜色完全不
参与。前提变了，脚本也就没意义了，重写。

现在验的是三件事：
  1. 一张真实照片里两块积木都被找到，身份是 block_A / block_B；
  2. 同一张照片整体转 20°/40°，身份不变（角度搜索 + 四边形矫正共同作用）；
  3. 分数在阈值之上，且不被"另一个字母"压过。

跑法：python working/check_label_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vision.main import VisionApp  # noqa: E402
from vision.sources import Frame, utc_now_iso  # noqa: E402

PHOTO = ROOT / "working" / "_scratch" / "real_ab.jpg"
CFG = str(ROOT / "vision" / "config" / "desktop.json")
OUT = ROOT / "working" / "_scratch"


def app_for():
    app = VisionApp(SimpleNamespace(camera=0, config=CFG, mode="real",
                                    output=str(OUT), min_conf=0.5, mock_cycle=False,
                                    skip_image_check=True, allow_frozen=False, url=None))
    app._source = None
    return app


def fit_photo(img: np.ndarray, target) -> np.ndarray:
    """手机画面常和 16:9 不一致；居中裁切再缩放，别把分区坐标扯歪。"""
    tw, th = int(target[0]), int(target[1])
    h, w = img.shape[:2]
    tgt = tw / th
    if abs(w / h - tgt) > 0.01:
        if w / h > tgt:
            nw = int(round(h * tgt))
            x0 = (w - nw) // 2
            img = img[:, x0:x0 + nw]
        else:
            nh = int(round(w / tgt))
            y0 = (h - nh) // 2
            img = img[y0:y0 + nh, :]
    return cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)


def spin(img: np.ndarray, deg: float) -> np.ndarray:
    """整张照片转 deg 度 —— 模拟"手机/桌面被摆歪"。

    两个坑，都是我自己踩的：

    1. 转完必须**中心裁回原尺寸**。第一版是把旋转后的正方形画布 cv2.resize 回
       1280×720，那是各向异性缩放（高度被压 0.49 倍），标签直接被压扁，测出来
       的结果全是"系统认不出"，其实是我把图毁了。

    2. 填充必须用 **BORDER_REPLICATE**，不能用深色画布。用深色画布时，旋转会在
       画面里造出一条笔直的暗边，Canny 把标签和这条**本来不存在的边**连成一体 →
       框从 270×134 涨到 849×514（还贴着 y=0）、墨量 587→911、分数掉到 0.47，
       于是 20° 报 "MISS"。查了很久才发现是测试脚本伪造的缺陷：
       换成边缘复制填充后，20° 是 A 0.92 / B 0.92，0/20/40/70/200° 全过。
       结论：**测量工具本身会造缺陷**，看到可疑结果先怀疑自己的构造方式。
    """
    h, w = img.shape[:2]
    big = int(np.ceil(np.hypot(h, w)))
    canvas = cv2.copyMakeBorder(img, (big - h) // 2, (big - h + 1) // 2,
                                (big - w) // 2, (big - w + 1) // 2,
                                cv2.BORDER_REPLICATE)[:big, :big]
    M = cv2.getRotationMatrix2D((big / 2.0, big / 2.0), deg, 1.0)
    rot = cv2.warpAffine(canvas, M, (big, big), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
    y0 = (big - h) // 2
    x0 = (big - w) // 2
    return rot[y0:y0 + h, x0:x0 + w]


def main() -> None:
    img = cv2.imread(str(PHOTO))
    if img is None:
        raise SystemExit(f"读不到 {PHOTO}")
    app = app_for()
    res = app.config.get("resolution", [1280, 720])
    base = fit_photo(img, res)
    print(f"照片 {img.shape[1]}x{img.shape[0]} → 裁到 {base.shape[1]}x{base.shape[0]}")
    print(f"库内字母 {app.detector.letter_bank.letters()}  "
          f"阈值 {app.detector.label_match_threshold}")

    for deg in (0.0, 20.0, 40.0, 70.0, 200.0):
        frame_img = base if deg == 0 else spin(base, deg)
        frame = Frame(image=frame_img, captured_at=utc_now_iso(), source="dbg",
                      mode="real", index=1)
        app.observe(frame)
        ids = sorted({d.object_id for d in app.detector.last_dets})
        detail = []
        for d in app.detector.last_dets:
            if d.object_id.startswith("unknown"):
                continue
            detail.append(f"{d.object_id} {d.letter_score:.2f}"
                          + (f"@{d.bbox}" if False else ""))
        info = app.detector.last_label_info
        labels = " | ".join(
            (f"{i['letter']} {i['score']:.2f}@{i.get('angle', 0):.0f}°"
             if i["letter"] else f"读不出({i['why']})") for i in info)
        ok = "OK " if ids == ["block_A", "block_B"] else "MISS"
        print(f"  {ok} 转 {deg:>5.0f}°  身份 {ids}")
        print(f"        标签 {labels}")
        _ = detail


if __name__ == "__main__":
    main()
