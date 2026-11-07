"""视觉模型自检：填完 key 之后，先跑这个，别等到答辩现场才发现 401。

    set DASHSCOPE_API_KEY=sk-xxxx                （cmd）
    $env:DASHSCOPE_API_KEY="sk-xxxx"             （PowerShell）
    python -m vision.tools.vlm_check

它做三件事：
  1. 报清楚配置有没有生效（base_url / model / key 从哪来）；
  2. 拿 cv2 现场画 A、B、D 三张字条，逐个送给模型，看认不认得出
     —— 这一步验的是"真能认字"，不是"接口通了"；
  3. 报每次调用的耗时和原始返回，超时/额度不足一眼能看出来。

有真实照片时换成：
    python -m vision.tools.vlm_check --image working/_scratch/real_ab.jpg
它会把照片里的每张标签裁下来分别问模型，并把结果和模板库的读数并排打出来
—— 两边不一致的地方，就是现场最可能认错的积木。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def make_tag(letter: str, tw: int = 150, th: int = 110) -> np.ndarray:
    """白底黑字的字条，和桌上贴的那张同构（够模型看清笔画就行）。"""
    tag = np.full((th, tw, 3), 242, np.uint8)
    (w, h), _ = cv2.getTextSize(letter, cv2.FONT_HERSHEY_SIMPLEX, 2.2, 5)
    cv2.putText(tag, letter, ((tw - w) // 2, (th + h) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 2.2, (25, 25, 25), 5, cv2.LINE_AA)
    return tag


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="视觉模型连通性 + 识别准确性自检")
    p.add_argument("--config", default="vision/config/desktop.json")
    p.add_argument("--image", default="", help="有真实照片就用它，比合成字条准")
    p.add_argument("--letters", default="ABD", help="合成模式下测哪些字母")
    args = p.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    label = cfg.get("label") or {}
    vcfg = dict(label.get("vlm") or {})

    from vision.vlm import VlmReader
    reader = VlmReader(
        base_url=str(vcfg.get("base_url", "")),
        model=str(vcfg.get("model", "")),
        api_key=str(vcfg.get("api_key", "")),
        timeout_s=float(vcfg.get("timeout_s", 8.0)),
        max_side=int(vcfg.get("max_side", 512)),
    )

    print("=" * 60)
    print(f"reader  = {label.get('reader', 'auto')}")
    print(f"base_url= {vcfg.get('base_url') or '（空）'}")
    print(f"model   = {vcfg.get('model') or '（空）'}")
    raw = str(vcfg.get("api_key", ""))
    if raw.startswith("env:"):
        print(f"api_key = 环境变量 {raw[4:]} → {'已设置' if reader.api_key else '❌ 没设置！'}")
        if not reader.api_key:
            print(f"\n  这台机器上没读到 {raw[4:]}。先设它，再重跑：")
            print(f'    cmd:        set {raw[4:]}=sk-xxxx')
            print(f'    PowerShell: $env:{raw[4:]}="sk-xxxx"')
            print("    临时验证也可以直接改 desktop.json 的 label.vlm.api_key")
    else:
        print(f"api_key = {'已填写' if raw else '（空）'}")

    if not reader.available:
        print(f"\n❌ 模型没就绪：{reader.why_unavailable()}")
        print("   系统不会崩，会自动退回模板比对（reader=auto 时）。")
        return 1
    print("=" * 60)

    if args.image:
        scenes = _from_photo(Path(args.image), label)
        if not scenes:
            print("照片里没找到标签，换一张，或去掉 --image 用合成字条测试")
            return 1
    else:
        scenes = [(letter, make_tag(letter)) for letter in args.letters]

    ok = 0
    worst_ms = 0
    for expect, tile in scenes:
        res = reader.read(tile)
        worst_ms = max(worst_ms, res.ms)
        got = res.letter or "?"
        mark = "OK  " if (not expect or got == expect) else "偏  "
        if not res.error and res.letter and (not expect or got == expect):
            ok += 1
        print(f"  {mark} 期望 {expect or '—'}  模型读 {got}   {res.ms:5d} ms"
              + (f"   原始返回={res.raw!r}" if res.error or res.raw.strip() not in
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZ" else ""))
        if res.error:
            print(f"        错误：{res.error}")
            if "401" in res.error or "403" in res.error:
                print("        key 不对或没权限：确认这个 key 开通了该模型的"
                      "『兼容模式』调用权限，且没有多余空格/引号")
            elif "404" in res.error:
                print("        model 名字不对，或 base_url 少了 /v1")
            elif "timeout" in res.error.lower() or "timed out" in res.error.lower():
                print("        超时：把 label.vlm.timeout_s 调大，或换更快的模型")
                break

    total = len(scenes)
    print("-" * 60)
    print(f"认得 {ok}/{total}，最慢一次 {worst_ms} ms")
    if ok == total:
        print("✅ 模型可用。启动 phone_live 后页面会显示『模型+模板 <model>』")
        return 0
    print("⚠️ 有认错的。先看是不是字条太歪/太小；真实场景请用 --image 试")
    return 2


def _from_photo(path: Path, label_cfg: dict) -> list[tuple[str, np.ndarray]]:
    """把照片里每张标签裁下来。拿不到模板库读数时就只报模型读数。"""
    from vision.letters import LetterBank, character_mask, find_labels, normalize

    img = cv2.imread(str(path))
    if img is None:
        print(f"读不出这张图：{path}")
        return []
    labels = find_labels(img, label_cfg)
    print(f"照片里找到 {len(labels)} 张标签")
    bank = None
    if label_cfg.get("bank"):
        try:
            bank = LetterBank.load(str(label_cfg["bank"]))
        except Exception:                     # noqa: BLE001
            bank = None
    out = []
    for lb in labels:
        tile = normalize(img, lb)
        if tile is None:
            continue
        t = ""
        if bank is not None and bank.tiles:
            mask = character_mask(tile)
            if mask is not None:
                who, sc = bank.recognize(mask, step=int(label_cfg.get("angle_step", 15)))
                if who:
                    t = f"{who} {sc:.2f}"
        print(f"  标签 {tuple(int(v) for v in lb.bbox)}  模板读数 {t or '—'}")
        # expect 留空：真实照片我们也不知道标准答案，只把两个读数并排打出来
        out.append(("", tile))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
