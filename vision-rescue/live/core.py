"""RE:BENCH 实时核心：找纸标签 → 读字母 → 跨帧稳住 → 和基准比。

这里**不画图、不发图、不管 HTTP**。调用方给一张 BGR 帧，它回答
"画面里现在有哪些字母块、在哪里、稳不稳"。

和旧实现最本质的区别是**每帧产出的是几十字节的 JSON，不是一张 JPEG**。
旧版每个周期要把标注图编码 + base64 回传 + 浏览器 img.src 解码，
单这一项就把帧率钉在 2-3fps；本地这套算法本身只要 20-40ms。
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from vision.letters import LetterBank, character_mask, find_labels, normalize
from vision.vlm import VlmReader


# --------------------------------------------------------------------- 读数
def ring_contrast(img: np.ndarray, bbox, pad: float = 0.40,
                  v_plane: np.ndarray | None = None) -> float:
    """标签比它四周亮多少倍。

    这是把"白纸贴纸"和"人脸/眼睛/墙/椅子"分开最有效的一条。眼睛是"白眼球
    +深色瞳孔"，二值化出来的笔画结构跟字母几乎一样，模板分也常常能到
    0.6；但它四周是脸，亮度跟它差不多 —— 比值 ≈ 1.0。而贴在彩色积木上的
    白标签，四周是积木或桌面，比值一般在 1.3 以上。
    """
    x, y, w, h = [int(v) for v in bbox]
    H, W = img.shape[:2]
    x0, y0 = max(0, int(x - w * pad)), max(0, int(y - h * pad))
    x1, y1 = min(W, int(x + w * (1 + pad))), min(H, int(y + h * (1 + pad)))
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    # V 通道整帧只转一次（外面传进来）；这里再转就是每个候选一次 cvtColor，
    # 八个候选能吃掉 30ms。
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


@dataclass
class Hit:
    """某一帧里读到的一个字母块。"""

    bbox: tuple[int, int, int, int]
    quad: np.ndarray
    letter: str
    score: float
    src: str  # template | vlm | ink（有字但读不出）

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return x + w / 2.0, y + h / 2.0


class Reader:
    """把一帧变成若干 Hit。"""

    def __init__(self, label_cfg: dict | None = None) -> None:
        cfg = label_cfg or {}
        self.cfg = cfg
        self.threshold = float(cfg.get("match_threshold", 0.6))
        self.angle_step = int(cfg.get("angle_step", 15))
        self.bank = LetterBank.load(cfg["bank"]) if cfg.get("bank") else LetterBank()
        self.max_labels = int(cfg.get("max_labels", 8))

        # 精度闸门。默认值是按实测画的线，可以在 config 的 precision 里改。
        pr = cfg.get("precision") or {}
        self.g_score = float(pr.get("score_min", 0.70))
        self.g_margin = float(pr.get("margin_min", 0.06))
        self.g_contrast = float(pr.get("contrast_min", 1.12))
        self.g_thin = float(pr.get("thin_max", 0.30))
        self.g_fill = float(pr.get("fill_min", 0.50))
        self.g_big = float(pr.get("max_side_ratio", 0.45))
        # 中央兜底更严：它没有模板比对做交叉验证，只能靠"这块确实像贴纸"。
        # 眼睛的白度是 1.19（四周是脸），贴纸在积木上是 1.26-1.33，卡 1.22。
        self.g_center_contrast = float(pr.get("center_contrast_min", 1.22))
        # 模型读"没登记过的字母"要求这块足够大：120px 以上的白纸黑字，
        # 基本不可能是误框；小的就只认库里有的字母，别问模型。
        self.g_vlm_unknown_px = float(pr.get("vlm_unknown_min_px", 120))

        vc = cfg.get("vlm") or {}
        self.vlm = VlmReader(
            base_url=vc.get("base_url", ""),
            model=vc.get("model", ""),
            api_key=vc.get("api_key", ""),
            timeout_s=float(vc.get("timeout_s", 12)),
            max_side=int(vc.get("max_side", 512)),
            workers=1,          # 实时场景一律单并发：宁可晚一点，不能排队积压
        )
        # 模板读不出来时，模型只读"画面中央那个通过了形状检查的标签"
        self.center_letter = ""
        self.center_at = 0.0
        self._center_tile: np.ndarray | None = None
        self._center_try = 0.0
        self.last_stats: dict = {}
        self.last_candidates: list[dict] = []
        # 同一个笔画 mask 的比对结果是确定性的，没必要每帧重算。
        # 24 个角度 × 3 个尺度 × 25 个平移 = 每块 5ms，桌子上四块就是 20ms，
        # 而画面基本是静止的：同一块积木连着几十帧给出的 mask 一模一样。
        self._memo: dict[bytes, tuple] = {}
        self._memo_hit = 0

    # ------------------------------------------------------------------
    def read(self, img: np.ndarray) -> tuple[list[Hit], dict]:
        t0 = time.perf_counter()
        boxes = find_labels(img, self.cfg)
        n_raw = len(boxes)
        boxes.sort(key=lambda b: -b.area_px)
        boxes = boxes[: self.max_labels]
        H, W = img.shape[:2]
        big_limit = self.g_big * min(H, W)
        v_plane = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 2]

        # 只有**字母真读出来并且过得了形状检查**的才算命中。
        # 旧版把"框到了但没读出"的候选也当物体铺到画面上，现场那面墙上全是
        # 灯带/屏幕/条幅，一次能刷出 20 多个假框 —— 看着像"认出一堆东西"，
        # 实际全是噪音。现在候选框只进 debug 字段，画面上只出现真的字。
        hits: list[Hit] = []
        cands: list[dict] = []
        why = {"no_ink": 0, "big": 0, "dark": 0, "thick": 0, "shape": 0,
               "score": 0, "margin": 0}
        from_vlm = 0
        center_best = None      # (面积, tile) —— 画面中央最像标签的那一个
        cx0, cy0, cx1, cy1 = W * 0.25, H * 0.2, W * 0.75, H * 0.8

        for lb in boxes:
            x, y, bw, bh = lb.bbox
            tile = normalize(img, lb)
            if tile is None:
                continue
            mask = character_mask(tile)
            if mask is None:
                why["no_ink"] += 1
                cands.append({"bbox": [int(v) for v in lb.bbox], "score": 0.0,
                              "why": "无字", "px": [int(tile.shape[1]), int(tile.shape[0])]})
                continue
            # 比整幅画面还大的一块：不可能是贴在小积木上的标签
            if min(bw, bh) > big_limit:
                why["big"] += 1
                cands.append({"bbox": [int(v) for v in lb.bbox], "score": 0.0,
                              "why": "太大", "px": [int(tile.shape[1]), int(tile.shape[0])]})
                continue

            contrast = ring_contrast(img, lb.bbox, v_plane=v_plane)
            thin = stroke_thin(mask)
            fill = lb.fill_ratio
            letter, score = self._recognize(mask)
            rank = getattr(self.bank, "last_ranking", []) or []
            second = rank[1][1] if len(rank) > 1 else 0.0
            margin = (score - second) if rank else 0.0

            ok_shape = contrast >= self.g_contrast and thin <= self.g_thin and fill >= self.g_fill
            # 白底白字（积木本身是白的，直接在白面上写）过不了亮度对比这一关，
            # 但字形本身很干净时放它过去：笔画细 + 分数高 + 甩开第二名明显。
            strong = (score >= 0.82 and margin >= 0.12 and thin <= 0.26
                      and fill >= self.g_fill)
            looks_label = ok_shape or strong

            if not looks_label:
                if contrast < self.g_contrast:
                    why["dark"] += 1
                    reason = f"不够白 {contrast:.2f}"
                elif thin > self.g_thin:
                    why["thick"] += 1
                    reason = f"笔画太粗 {thin:.2f}"
                else:
                    why["shape"] += 1
                    reason = "形状不像纸"
            elif score < self.g_score:
                why["score"] += 1
                reason = f"分低 {score:.2f}"
            elif margin < self.g_margin and len(rank) > 1:
                why["margin"] += 1
                reason = f"认不准 {letter}/{rank[1][0]} {score:.2f}:{second:.2f}"
            else:
                hits.append(Hit(lb.bbox, lb.quad, letter, float(score), "template"))
                continue

            # 走到这里说明"看着像标签但模板读不出/不敢认"。
            # 让模型读它 —— 但只在这块通过了形状检查时。否则模型会把条幅、
            # 屏幕上的字读成 K/E/J/D/A，凭空多出一个积木。
            if looks_label and max(tile.shape[:2]) >= 90 and self.vlm.available:
                got = self.vlm.peek(tile)
                if got and self._vlm_answer_ok(got, tile):
                    hits.append(Hit(lb.bbox, lb.quad, got, 0.7, "vlm"))
                    from_vlm += 1
                    continue
                self.vlm.warm(tile)

            cands.append({"bbox": [int(v) for v in lb.bbox],
                          "score": round(float(score), 2), "why": reason,
                          "px": [int(tile.shape[1]), int(tile.shape[0])]})

            cx, cy = x + bw / 2, y + bh / 2
            if (looks_label and contrast >= self.g_center_contrast
                    and thin <= self.g_thin
                    and cx0 <= cx <= cx1 and cy0 <= cy <= cy1
                    and (center_best is None or lb.area_px > center_best[0])):
                center_best = (lb.area_px, tile)

        self._center_tile = center_best[1] if center_best else None
        if time.time() - self.center_at > 3.0:
            self.center_letter = ""      # 结论放 3 秒就过期，别一直挂着

        # 一块都没读到 → 让模型看画面中央那个最像标签的。纯异步，本帧先
        # 返回上一轮的结果，绝不为了它等模型。
        if not hits:
            self._center_fallback()

        self.last_stats = {
            "candidates": n_raw,
            "read": len(hits),
            "vlm": from_vlm,
            "why": why,
            "read_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        self.last_candidates = cands
        return hits, self.last_stats

    # ------------------------------------------------------------------
    def _recognize(self, mask: np.ndarray) -> tuple[str, float]:
        """比对（带 memo）。同一个 mask 的结果是确定的，静止画面直接命中。"""
        key = hashlib.blake2b(
            np.packbits((mask > 0).astype(np.uint8)).tobytes(),
            digest_size=10).digest()
        got = self._memo.get(key)
        if got is not None:
            self._memo_hit += 1
            self.bank.last_ranking = got[2]     # 让调用方读到的排名与当次一致
            return got[0], got[1]
        letter, score = self.bank.recognize(mask, step=self.angle_step)
        rank = list(getattr(self.bank, "last_ranking", []) or [])
        if len(self._memo) > 600:
            self._memo.clear()                  # 现场够用，不值得上 LRU
        self._memo[key] = (letter, float(score), rank)
        return letter, float(score)

    def _vlm_answer_ok(self, letter: str, tile: np.ndarray) -> bool:
        """模型的答案能不能用。

        模型被问"这是哪个字母"时几乎从不回答"看不出" —— 拿他的脸、耳机、
        椅背去问，它也会给一个字母。所以答案要过两道：① 这块标签够大
        （120px 以上，白纸黑字的可能性极高）；② 或者这个字母本来就在库里
        （模型只起"确认"作用，不是凭空发现）。
        库里一个字母都没有时（第一次用），只能信它，那就要求够大。
        """
        bank = self.bank.letters()
        if not bank:
            return max(tile.shape[:2]) >= self.g_vlm_unknown_px
        if letter in bank:
            return True
        return max(tile.shape[:2]) >= self.g_vlm_unknown_px

    def read_center(self, img: np.ndarray, wait_s: float = 15.0) -> dict:
        """手动"读画面中央"：把正中一块交给模型，等它答。

        为什么要有这个按钮：连续检测必须宁缺毋滥（误报比漏读更烦人），
        所以它只认"白得明显、笔画细、分数高"的标签。可有些人就是举着积木
        离机位一米，标签只有 40×45px —— 这种情况任何自动判据都不可靠，
        但**用户明确要求读一次**的时候，读错他自己一眼就能看出来。
        自动模式严格、手动模式可用，两件事分开做。
        """
        if not self.vlm.available:
            return {"ok": False, "msg": "没配模型 key，只能靠模板库"}
        h, w = img.shape[:2]
        crop = img[int(h * 0.20):int(h * 0.80), int(w * 0.20):int(w * 0.80)].copy()
        key = self.vlm.key_of(crop)
        got = self.vlm.peek(crop)
        if not got:
            self.vlm.warm(crop)
            deadline = time.time() + wait_s
            while time.time() < deadline:
                time.sleep(0.25)
                got = self.vlm.peek(crop)
                if got:
                    break
        if got:
            return {"ok": True, "letter": got, "msg": f"模型读：{got}"}
        # 模型算完了但答案是"看不出" —— 和"压根没算出来"是两回事，要分清
        if key in self.vlm.cache:
            return {"ok": False, "msg": "模型看不出（画面中央没有明显的单个字母）"}
        return {"ok": False, "msg": self.vlm.last_error or "模型没响应"}

    def _center_fallback(self) -> None:
        """模板一个都没读出来时，让模型看画面中央那一块。

        现场实测：手举着积木离机位 1 米，标签只有 40×45px，任何模板匹配都
        没戏，但把那一块裁出来放大喂给模型就能读对。

        送进去的**必须是过了形状检查的标签**，不能是"整个画面中央"：
        否则人坐在镜头前，模型会对着他的脸/眼睛/耳机硬读出一个字母，
        屏幕上就出现一个根本不存在的积木。没有像标签的东西就什么都不读。
        """
        if not self.vlm.available or self._center_tile is None:
            return
        now = time.time()
        if now - self._center_try < 1.2:
            return
        self._center_try = now
        got = self.vlm.peek(self._center_tile)
        if got and self._vlm_answer_ok(got, self._center_tile):
            self.center_letter = got
            self.center_at = now
        else:
            self.vlm.warm(self._center_tile)

    def reader_name(self) -> str:
        return f"模板+模型 {self.vlm.describe()}" if self.vlm.available else "仅模板"

    # ------------------------------------------------------------------
    def enroll(self, img: np.ndarray, letter: str) -> dict:
        """把画面里**唯一**那张标签登记成 letter。

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
        cands = []
        rejected = 0
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
            # 登记这一步也要过形状检查：把眼睛/椅子边登记成"A"，脏模板会
            # 长期留在库里，之后所有识别都拿它去比 —— 现场表现就是"到处
            # 都是 A"，而且越登记越糟。
            contrast = ring_contrast(img, lb.bbox, v_plane=v_plane)
            thin = stroke_thin(mask)
            if contrast < self.g_contrast and thin > 0.22:
                rejected += 1
                continue
            cands.append((lb, mask, tile))
        if not cands:
            return {"ok": False,
                    "msg": ("没找到像标签的白纸" if not rejected else
                            "框到的东西不像标签（不够白或笔画太粗），换一块再试"),
                    "hints": ["把有字母的那面正对镜头", "白纸上用深色粗笔写字",
                              "积木拿近到白纸约占画面 1/8"]}
        if len(cands) > 1:
            return {"ok": False,
                    "msg": f"画面里有 {len(cands)} 张标签，只留要登记的那一块"}

        _, mask, tile = cands[0]
        before, bscore = self._recognize(mask)
        self.bank.enroll(letter, mask)
        self._memo.clear()          # 库里多了模板，旧结论全部失效
        if self.cfg.get("bank"):
            self.bank.save(self.cfg["bank"])
        warn = ""
        if before and before != letter and bscore >= self.threshold:
            warn = f"（这张字在旧库里更像 {before} {bscore:.2f}）"
        return {"ok": True,
                "msg": f"已登记 {letter} {warn}",
                "letters": self.bank.letters()}

    def forget(self, letter: str) -> dict:
        n = self.bank.forget(letter)
        self._memo.clear()
        if n and self.cfg.get("bank"):
            self.bank.save(self.cfg["bank"])
        return {"ok": bool(n),
                "msg": f"已删掉 {letter} 的 {n} 份模板" if n else f"库里没有 {letter}",
                "letters": self.bank.letters()}


# --------------------------------------------------------------------- 稳帧
@dataclass
class Track:
    tid: int
    bbox: tuple[int, int, int, int]
    letter: str
    score: float
    src: str
    votes: deque = field(default_factory=lambda: deque(maxlen=6))
    missed: int = 0
    born: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return x + w / 2.0, y + h / 2.0


class Stabilizer:
    """检测每帧都在抖，直接把原始结果铺到画面上就是一片闪烁。

    这里按 bbox 做最近邻跟踪，每块保留最近 6 帧的字母投票，取众数展示。
    """

    def __init__(self, max_missed: int = 2, iou_gate: float = 0.15) -> None:
        self.tracks: list[Track] = []
        self.max_missed = max_missed
        self.iou_gate = iou_gate
        self._next = 1

    @staticmethod
    def _iou(a, b) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        x1, y1 = max(ax, bx), max(ay, by)
        x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        inter = (x2 - x1) * (y2 - y1)
        return inter / float(aw * ah + bw * bh - inter)

    def update(self, hits: list[Hit]) -> list[Track]:
        for t in self.tracks:
            t.missed += 1
        used: set[int] = set()
        for h in hits:
            best, best_score = None, 0.0
            for t in self.tracks:
                if id(t) in used:
                    continue
                iou = self._iou(h.bbox, t.bbox)
                if iou > best_score:
                    best, best_score = t, iou
            if best is not None and best_score >= self.iou_gate:
                best.bbox = h.bbox
                best.score = h.score
                best.src = h.src
                best.missed = 0
                best.votes.append(h.letter)
                used.add(id(best))
                c = Counter(v for v in best.votes if v)
                best.letter = c.most_common(1)[0][0] if c else ""
            else:
                t = Track(self._next, h.bbox, h.letter, h.score, h.src,
                          missed=0, born=time.time())
                self._next += 1
                t.votes.append(h.letter)
                self.tracks.append(t)
                used.add(id(t))
        self.tracks = [t for t in self.tracks if t.missed <= self.max_missed]
        # 模板读出来的连两帧就显示；模型读的多等一帧（它的答案更容易飘）。
        # 分数极高（≥0.90）的模板命中不用等 —— 那是"一眼就认出来"的情况。
        out = []
        for t in self.tracks:
            seen = len([v for v in t.votes if v])
            need = 3 if t.src == "vlm" else 2
            if seen >= need or (t.src == "template" and t.score >= 0.90):
                out.append(t)
        return out


# --------------------------------------------------------------------- 基准
class Baseline:
    """记下"摆好的那一刻"，之后比位置。"""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.at = 0.0

    def capture(self, tracks: list[Track], zone_of) -> int:
        self.items = [{"letter": t.letter, "cx": t.center[0], "cy": t.center[1],
                       "w": t.bbox[2], "h": t.bbox[3], "zone": zone_of(t.center)}
                      for t in tracks if t.letter]
        self.at = time.time()
        return len(self.items)

    def compare(self, tracks: list[Track], zone_of, tol_px: float) -> dict:
        live = [{"letter": t.letter, "cx": t.center[0], "cy": t.center[1],
                 "zone": zone_of(t.center)} for t in tracks if t.letter]
        base = list(self.items)
        rows, missing, extra = [], [], []
        taken: set[int] = set()

        for b in base:
            cand = [(i, l) for i, l in enumerate(live)
                    if i not in taken and l["letter"] == b["letter"]]
            if not cand:
                missing.append(b["letter"])
                continue
            i, l = min(cand, key=lambda p: (p[1]["cx"] - b["cx"]) ** 2
                       + (p[1]["cy"] - b["cy"]) ** 2)
            taken.add(i)
            dx = l["cx"] - b["cx"]
            dy = l["cy"] - b["cy"]
            dist = (dx * dx + dy * dy) ** 0.5
            # 判定只看位移。分区（A/B/C/D）是给眼睛看的参考框，默认那套是按
            # 另一台机器的取景画的，拿它当判据会把"没动"报成"被挪动"。
            moved = dist > tol_px
            rows.append({"letter": b["letter"], "from": b["zone"], "to": l["zone"],
                         "px": int(round(dist)),
                         "status": "moved" if moved else "same"})
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
