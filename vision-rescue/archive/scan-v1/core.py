"""一次扫描的共用底座：形状判据、模板库、基准比对。

这里**不做逐帧识别**。识别的组织方式在 `scan.py`：一次扫描拿多帧、跑多
尺度、跨帧投票、模板和模型互相验证。这个文件只放三样东西：

  · 两道形状判据（`ring_contrast` / `stroke_thin`）—— 判断"这是不是一张
    贴在积木上的纸"，而不是"这像哪个字母"。只看字形相似度永远分不开
    眼睛和 A：眼睛是白眼球加黑瞳孔，二值化之后笔画结构跟字母几乎一样。
  · `Reader` —— 模板库 + 模型 + 四道闸门的持有者，扫描和登记共用一份。
  · `Baseline` —— 记下"摆好的那一刻"，之后比位移。
"""

from __future__ import annotations

import hashlib
import time

import cv2
import numpy as np

from vision.letters import LetterBank, character_mask, find_labels, normalize
from vision.vlm import VlmReader


# --------------------------------------------------------------------- 判据
def ring_contrast(img: np.ndarray, bbox, pad: float = 0.40,
                  v_plane: np.ndarray | None = None) -> float:
    """标签比它四周亮多少倍。

    这是把"白纸贴纸"和"人脸/眼睛/墙/椅子"分开最有效的一条。眼睛是"白眼球
    +深色瞳孔"，二值化出来的笔画结构跟字母几乎一样，模板分也常常能到
    0.6；但它四周是脸，亮度跟它差不多 —— 比值 ≈ 1.0-1.19。而贴在彩色积木上的
    白标签，四周是积木或桌面，比值一般在 1.26 以上。

    `v_plane` 传整帧的 V 通道（外面转一次就行）：这里再转一次就是每个候选
    一次 cvtColor，十二个候选能吃掉 30ms。
    """
    x, y, w, h = [int(v) for v in bbox]
    H, W = img.shape[:2]
    x0, y0 = max(0, int(x - w * pad)), max(0, int(y - h * pad))
    x1, y1 = min(W, int(x + w * (1 + pad))), min(H, int(y + h * (1 + pad)))
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    v = (v_plane[y0:y1, x0:x1] if v_plane is not None
         else cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[:, :, 2]).astype(np.float32)
    m = np.ones(v.shape, bool)
    ix, iy = x - x0, y - y0
    m[iy:iy + h, ix:ix + w] = False
    if int(m.sum()) < 40 or h <= 0 or w <= 0:
        return 0.0
    ring = float(np.median(v[m]))
    inner = float(np.median(v[iy:iy + h, ix:ix + w]))
    return inner / max(ring, 1.0)


def stroke_thin(mask: np.ndarray) -> float:
    """最粗的那一坨笔画，占短边的比例。

    字母是**细笔画**：最粗处一般不到标签短边的 20%。眼睛的瞳孔、积木上的
    纯色块、椅子扶手的实心面，二值化后是一整团，比值 0.3-0.6。这一条把
    "实心块"和"写得出来的字"分开，比提高模板阈值可靠得多 —— 提高阈值会
    先把真的细笔手写干掉。
    """
    m = (mask > 0).astype(np.uint8)
    if not m.any():
        return 1.0
    d = cv2.distanceTransform(m, cv2.DIST_L2, 3)
    return float(d.max()) / max(1.0, float(min(mask.shape[:2])))


# --------------------------------------------------------------------- 读数器
class Reader:
    """模板库 + 视觉模型 + 四道闸门。扫描和登记共用同一个实例。"""

    def __init__(self, label_cfg: dict | None = None) -> None:
        cfg = label_cfg or {}
        self.cfg = cfg
        self.threshold = float(cfg.get("match_threshold", 0.6))
        self.angle_step = int(cfg.get("angle_step", 15))
        self.bank = LetterBank.load(cfg["bank"]) if cfg.get("bank") else LetterBank()
        self.max_labels = int(cfg.get("max_labels", 8))

        # 四道闸门。默认值是按实测画的线，可以在 config 的 precision 里改。
        pr = cfg.get("precision") or {}
        self.g_score = float(pr.get("score_min", 0.70))
        self.g_margin = float(pr.get("margin_min", 0.06))
        self.g_contrast = float(pr.get("contrast_min", 1.12))
        self.g_thin = float(pr.get("thin_max", 0.30))
        self.g_fill = float(pr.get("fill_min", 0.50))
        self.g_big = float(pr.get("max_side_ratio", 0.45))
        # 模型读"没登记过的字母"要求这块足够大：120px 以上的白纸黑字，
        # 基本不可能是误框；小的就只认库里有的字母，别问模型。
        self.g_vlm_unknown_px = float(pr.get("vlm_unknown_min_px", 120))
        # 只有模型读出来的字母，要求这块"明显是一张白纸"：白度 1.22 是实测线
        # （眼睛 1.19，贴在彩色积木上的标签 1.26-1.33）。模型被问"这是哪个
        # 字母"时几乎从不回答"看不出"，拿他的脸去问它也会给一个字母。
        self.g_vlm_contrast = float(pr.get("vlm_contrast_min", 1.22))

        vc = cfg.get("vlm") or {}
        self.vlm = VlmReader(
            base_url=vc.get("base_url", ""),
            model=vc.get("model", ""),
            api_key=vc.get("api_key", ""),
            timeout_s=float(vc.get("timeout_s", 12)),
            max_side=int(vc.get("max_side", 512)),
            workers=1,
        )
        # 同一个笔画 mask 的比对结果是确定性的，没必要重算。扫描一次要看
        # 同一块标签的四帧，四帧的 mask 几乎一样 —— 缓存能省掉四分之三。
        self._memo: dict[tuple, tuple] = {}

    # ------------------------------------------------------------------
    def recognize(self, mask: np.ndarray, step: int | None = None):
        """模板比对（带 memo）。返回 (字母, 分数, 与第二名的差距)。"""
        step = int(step or self.angle_step)
        key = (hashlib.blake2b(
            np.packbits((mask > 0).astype(np.uint8)).tobytes(),
            digest_size=10).digest(), step)
        got = self._memo.get(key)
        if got is not None:
            self.bank.last_ranking = got[2]
            return got[0], got[1], got[3]
        letter, score = self.bank.recognize(mask, step=step)
        rank = list(getattr(self.bank, "last_ranking", []) or [])
        second = rank[1][1] if len(rank) > 1 else 0.0
        margin = float(score - second) if rank else 0.0
        if len(self._memo) > 800:
            self._memo.clear()
        self._memo[key] = (letter, float(score), rank, margin)
        return letter, float(score), margin

    def letters(self) -> list[str]:
        return self.bank.letters()

    def reader_name(self) -> str:
        return f"模板+模型 {self.vlm.describe()}" if self.vlm.available else "仅模板"

    def _save(self) -> None:
        self._memo.clear()
        if self.cfg.get("bank"):
            self.bank.save(self.cfg["bank"])

    # ------------------------------------------------------------------
    def enroll_mask(self, mask: np.ndarray, letter: str) -> dict:
        """把一张已经提好的笔画模板登记进库（扫描结果里"教它"走这条）。"""
        letter = (letter or "").strip().upper()
        if not letter:
            return {"ok": False, "msg": "先填字母"}
        if mask is None or not (mask > 0).any():
            return {"ok": False, "msg": "这块没有笔画，登记不了"}
        before, score, _ = self.recognize(mask)
        self.bank.enroll(letter, mask)
        self._save()
        warn = ""
        if before and before != letter and score >= self.threshold:
            warn = f"（这张在旧库里更像 {before} {score:.2f}）"
        return {"ok": True, "msg": f"已登记 {letter} {warn}",
                "letters": self.bank.letters()}

    def enroll(self, img: np.ndarray, letter: str) -> dict:
        """从一整帧里挑出**唯一**那张标签登记。

        画面里有多张标签时拒绝。这不是谨慎是必需：取错了那张，脏模板会
        长期污染库，之后那块真字母永远认不出，还会被认成别的。
        """
        letter = (letter or "").strip().upper()
        if not letter:
            return {"ok": False, "msg": "先填字母"}
        H, W = img.shape[:2]
        big_limit = self.g_big * min(H, W)
        v_plane = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 2]
        boxes = sorted(find_labels(img, self.cfg), key=lambda b: -b.area_px)
        cands, rejected = [], 0
        for lb in boxes[:6]:
            tile = normalize(img, lb)
            if tile is None:
                continue
            mask = character_mask(tile)
            if mask is None:
                continue
            if min(lb.bbox[2], lb.bbox[3]) > big_limit:
                rejected += 1
                continue
            # 登记这一步也要过形状检查：把眼睛/椅子边登记成 A，脏模板会
            # 长期留在库里，之后所有识别都拿它去比 —— 现场表现就是
            # "到处都是 A"，而且越登记越糟。
            contrast = ring_contrast(img, lb.bbox, v_plane=v_plane)
            thin = stroke_thin(mask)
            if contrast < self.g_contrast and thin > 0.22:
                rejected += 1
                continue
            cands.append(mask)
        if not cands:
            return {"ok": False,
                    "msg": ("没找到像标签的白纸" if not rejected else
                            "框到的东西不像标签（不够白或笔画太粗），换一块再试"),
                    "hints": ["把有字母的那面正对镜头", "白纸上用深色粗笔写字"]}
        if len(cands) > 1:
            return {"ok": False,
                    "msg": f"画面里有 {len(cands)} 张标签，只留要登记的那一块"}
        return self.enroll_mask(cands[0], letter)

    def forget(self, letter: str) -> dict:
        n = self.bank.forget(letter)
        if n:
            self._save()
        return {"ok": bool(n),
                "msg": f"已删掉 {letter} 的 {n} 份模板" if n else f"库里没有 {letter}",
                "letters": self.bank.letters()}


# --------------------------------------------------------------------- 基准
class Baseline:
    """记下"摆好的那一刻"，之后只比位移。

    分区（A/B/C/D 那些黄框）不参与判定：那套坐标是按另一台机器的取景画的，
    拿它当判据会把"没动"报成"被挪动"。
    """

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.at = 0.0

    def capture(self, items: list[dict], zone_of) -> int:
        self.items = [{"letter": it["letter"],
                       "cx": float(it["cx"]), "cy": float(it["cy"]),
                       "zone": zone_of((it["cx"], it["cy"]))}
                      for it in items if it.get("letter")]
        self.at = time.time()
        return len(self.items)

    def compare(self, items: list[dict], zone_of, tol_px: float) -> dict:
        live = [{"letter": it["letter"], "cx": float(it["cx"]),
                 "cy": float(it["cy"]), "zone": zone_of((it["cx"], it["cy"]))}
                for it in items if it.get("letter")]
        base = list(self.items)
        rows: list[dict] = []
        taken: set[int] = set()

        for b in base:
            cand = [(i, l) for i, l in enumerate(live)
                    if i not in taken and l["letter"] == b["letter"]]
            if not cand:
                continue
            i, l = min(cand, key=lambda p: (p[1]["cx"] - b["cx"]) ** 2
                       + (p[1]["cy"] - b["cy"]) ** 2)
            taken.add(i)
            dx, dy = l["cx"] - b["cx"], l["cy"] - b["cy"]
            dist = (dx * dx + dy * dy) ** 0.5
            rows.append({"letter": b["letter"], "from": b["zone"],
                         "to": l["zone"], "px": int(round(dist)),
                         "status": "moved" if dist > tol_px else "same"})

        missing = [b["letter"] for b in base
                   if not any(r["letter"] == b["letter"] for r in rows)]
        extra = [l["letter"] for i, l in enumerate(live) if i not in taken]

        if missing or extra or any(r["status"] == "moved" for r in rows):
            verdict = "NOT_READY"
        elif not rows:
            verdict = "REVIEW"
        else:
            verdict = "READY"
        return {"verdict": verdict, "rows": rows, "missing": missing,
                "extra": extra, "base_count": len(base), "live_count": len(live)}


def zone_lookup(zones: dict | None, frame_w: int, frame_h: int, base=(1280, 720)):
    """把配置里的多边形分区（按基准分辨率写死）换算成当前帧的判定函数。"""
    if not zones:
        return lambda c: ""

    sx, sy = frame_w / float(base[0]), frame_h / float(base[1])
    poly = {k: np.array([[p[0] * sx, p[1] * sy] for p in v], np.float32)
            for k, v in zones.items()}

    def look(center) -> str:
        for name, p in poly.items():
            if cv2.pointPolygonTest(p, (float(center[0]), float(center[1])), False) >= 0:
                return name
        return "?"

    return look
