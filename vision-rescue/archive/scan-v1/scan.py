"""一次扫描的完整识别：多帧、多尺度、双通道投票。

为什么不再逐帧实时识别
----------------------
实时链路里每一条判据都必须「宁可不认」：读错会一直挂在屏幕上，误报比漏读
更烦人。于是它只能收得很紧，紧到远处的标签干脆不认。

而用户真正要的不是每秒 18 次猜测，而是**摆好之后给个信号，然后拿到一份
可以照着核对的结论**。一次扫描允许花两三秒，于是可以做四件实时链路做
不了的事：

1. **多帧投票**：采 6-8 帧，同一块标签横跨几帧读到的字母取众数。单帧的
   抖动、反光、瞬时模糊会被票掉。
2. **多尺度检测**：同一帧跑 1.0× 和 1.6× 两档 Canny。远景里 30px 的标签
   在原尺度凑不出四边形，放大档里才凑得出来 —— 这是「漏识别」的主因。
3. **挑最清晰的帧定几何**：按下扫描那一瞬间常带轻微运动模糊，用拉普拉斯
   方差挑出最清晰的一帧来定框。
4. **模板和模型互相验证**：两边都读出来且一致 → 高可信；只有一边读出来
   → 低可信，只提示不判定。没人跟它抢的时候，模型最容易瞎猜。

扫描里仍然沿用实时链路那四道形状闸门（白度/笔画粗细/填充率/分数差）。
放松闸门换来的召回，是靠误报付账的 —— 用户已经被「眼睛被认成 A」坑过一次。
召回靠的是投票、多尺度和模型交叉验证，不是靠降低"这到底是不是一张纸"的
标准。
"""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from vision.letters import LabelBox, character_mask, find_labels, normalize

from .core import Reader, ring_contrast, stroke_thin


# --------------------------------------------------------------------- 工具
def sharpness(img: np.ndarray) -> float:
    """清晰度：拉普拉斯响应的方差。模糊帧的值会掉一个数量级。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def _iou(a, b) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / max(aw * ah + bw * bh - inter, 1.0)


def _match(boxes: list[LabelBox], bbox) -> LabelBox | None:
    """在另一帧的候选里找同一块标签。

    不用纯 IoU：手轻微一动，框会整体平移，40px 的标签平移 15px 后 IoU 就
    掉到 0.3 以下了。所以再给一条"中心落在框内、且尺寸相近"的通道。
    """
    best, best_key = None, 0.0
    x, y, w, h = [float(v) for v in bbox]
    cx, cy = x + w / 2, y + h / 2
    for b in boxes:
        iou = _iou(b.bbox, bbox)
        bx, by, bw, bh = [float(v) for v in b.bbox]
        bcx, bcy = bx + bw / 2, by + bh / 2
        dist = ((bcx - cx) ** 2 + (bcy - cy) ** 2) ** 0.5
        near = max(0.0, 1.0 - dist / max(28.0, 0.9 * max(w, h)))
        size_ok = 0.55 <= (bw * bh) / max(w * h, 1.0) <= 1.8
        key = max(iou, near * 0.85) if size_ok else iou
        if key > best_key:
            best, best_key = b, key
    return best if best_key >= 0.28 else None


@dataclass
class Block:
    """扫描出的一个候选块（可能读出了字母，也可能只是"像标签"）。"""

    bbox: tuple[int, int, int, int]
    letter: str = ""
    score: float = 0.0
    margin: float = 0.0
    src: str = ""            # template | vlm | both
    conf: str = "none"       # high | mid | low | none
    reason: str = ""
    agree: float = 0.0       # 投票一致率
    frames: int = 0          # 有几帧读到了这块
    votes: dict = field(default_factory=dict)
    vlm: str = ""
    shape_ok: bool = False
    strong: bool = False
    contrast: float = 0.0
    thin: float = 0.0
    fill: float = 0.0
    px: tuple[int, int] = (0, 0)
    tile: np.ndarray | None = field(default=None, repr=False)
    mask: np.ndarray | None = field(default=None, repr=False)

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return x + w / 2.0, y + h / 2.0

    def as_dict(self) -> dict:
        x, y, w, h = self.bbox
        return {"letter": self.letter, "bbox": [int(x), int(y), int(w), int(h)],
                "cx": round(x + w / 2.0, 1), "cy": round(y + h / 2.0, 1),
                "score": round(self.score, 2), "margin": round(self.margin, 2),
                "src": self.src, "conf": self.conf,
                "agree": round(self.agree, 2), "frames": self.frames,
                "votes": dict(self.votes), "vlm": self.vlm,
                "px": [int(self.px[0]), int(self.px[1])], "reason": self.reason}


# --------------------------------------------------------------------- 扫描
class Scanner:
    """把若干帧变成一份"画面里有哪些字母块"的结论。"""

    def __init__(self, reader: Reader) -> None:
        self.r = reader
        cfg = reader.cfg
        sc = cfg.get("scan") or {}
        # 扫描用的角度步进比实时更细：一次只算十几块，多花 3ms 换更稳的字形匹配
        self.step = int(sc.get("angle_step", 10))
        self.max_frames = int(sc.get("max_frames", 3))
        self.max_blocks = int(sc.get("max_blocks", 12))
        self.vlm_max_blocks = int(sc.get("vlm_max_blocks", 6))
        self.vlm_workers = int(sc.get("vlm_workers", 3))
        self.scales = tuple(sc.get("scales", (1.0, 1.5)))
        self.last_blocks: list[Block] = []
        self.last_dropped: list[dict] = []
        self.last_stats: dict = {}
        self.last_frame: np.ndarray | None = None
        self.last_annotated: np.ndarray | None = None

    # ------------------------------------------------------------ 检测
    def _detect(self, img: np.ndarray) -> list[LabelBox]:
        """一帧上跑多档尺度，把结果并起来。

        `min_area_px` / `min_side_px` 必须跟着缩放走：放大 1.6 倍后同样的
        标签面积变成 2.56 倍，仍用原阈值等于把线收紧了一倍多，放大的意义
        就没了。
        """
        base_area = float(self.cfg_get("min_area_px", 700))
        base_side = float(self.cfg_get("min_side_px", 26))
        found: list[LabelBox] = []
        for s in self.scales:
            c = dict(self.r.cfg)
            c["min_area_px"] = max(140, int(base_area / s ** 2 * 0.6))
            c["min_side_px"] = max(10, int(base_side / s * 0.65))
            if abs(s - 1.0) < 1e-6:
                im = img
            else:
                im = cv2.resize(img, None, fx=s, fy=s,
                                interpolation=cv2.INTER_CUBIC)
            for lb in find_labels(im, c):
                if abs(s - 1.0) < 1e-6:
                    found.append(lb)
                else:
                    found.append(LabelBox(
                        tuple(int(round(v / s)) for v in lb.bbox),
                        lb.quad / s, lb.area_px / s ** 2, lb.fill_ratio))
        return self._merge(found)

    def cfg_get(self, key: str, default):
        return self.r.cfg.get(key, default)

    @staticmethod
    def _merge(boxes: list[LabelBox], iou_limit: float = 0.45) -> list[LabelBox]:
        out: list[LabelBox] = []
        for b in sorted(boxes, key=lambda x: -x.area_px):
            if any(_iou(b.bbox, k.bbox) > iou_limit for k in out):
                continue
            out.append(b)
        return out

    # ------------------------------------------------------------ 单块读数
    def _read_one(self, img: np.ndarray, lb: LabelBox,
                  v_plane: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray] | None:
        tile = normalize(img, lb)
        if tile is None:
            return None
        mask = character_mask(tile)
        if mask is None:
            return None
        return tile, mask

    def _vote(self, block: Block, lb: LabelBox, frames: list[np.ndarray],
              per_frame: dict[int, list[LabelBox]]) -> None:
        """跨帧读同一块标签，取众数。"""
        reads: list[tuple[str, float, float, np.ndarray, np.ndarray]] = []
        for i, boxes in per_frame.items():
            m = _match(boxes, lb.bbox)
            if m is None:
                continue
            got = self._read_one(frames[i], m)
            if got is None:
                continue
            tile, mask = got
            letter, score, margin = self.r.recognize(mask, step=self.step)
            reads.append((letter, float(score), float(margin), tile, mask))
        if not reads:
            return

        block.frames = len(reads)
        letters = [x[0] for x in reads if x[0]]
        block.votes = dict(Counter(letters))
        if letters:
            ranked = Counter(letters).most_common()
            # 打平就别硬选一个。眼睛那种"白眼球+黑瞳孔"的块，四帧读出来
            # 常常是 A 两票 B 两票 —— 给它一个"A"纯属误导，不如留空。
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                block.tile = max(reads, key=lambda x: x[3].size)[3]
                block.letter, block.score, block.margin = "", 0.0, 0.0
                block.reason = "投票打平 " + "/".join(
                    f"{k}{v}" for k, v in ranked[:3])
                return
            letter, cnt = ranked[0]
            block.agree = cnt / float(len(reads))
            hit = max((x for x in reads if x[0] == letter), key=lambda x: x[1])
            block.score, block.margin = hit[1], hit[2]
            block.tile = hit[3]
            if block.score < 0.55 and block.agree < 0.75:
                # 又低又不稳：眼睛这类"白底深色团"就是这种分布（A 两票 B 一票、
                # 0.54 分）。给它一个字母纯属误导，留空交给模型复核。
                block.letter = ""
                block.reason = "读不稳 " + " ".join(
                    f"{k}{v}" for k, v in ranked[:3])
            else:
                block.letter = letter
        else:
            # 一帧都没读出来：留面积最大的那张裁片，用户点「教」时用它登记
            block.tile = max(reads, key=lambda x: x[3].size)[3]

    # ------------------------------------------------------------ 主流程
    def scan(self, frames: list[np.ndarray], use_vlm: bool = True) -> dict:
        t0 = time.perf_counter()
        frames = [f for f in frames
                  if isinstance(f, np.ndarray) and f.size and f.ndim == 3]
        if not frames:
            return {"ok": False, "msg": "没收到画面帧"}
        h, w = frames[0].shape[:2]
        frames = [f for f in frames if f.shape[:2] == (h, w)]
        if not frames:
            return {"ok": False, "msg": "帧尺寸不一致"}

        sharp = [sharpness(f) for f in frames]
        # 几何帧只看后半段：按下扫描之后手会收回去，前半段多半还在动。
        # 后半段里挑最清晰的 —— 模糊帧定出来的框会整体偏一两像素，投票就全错位。
        tail = list(range(len(frames) // 2, len(frames))) or [len(frames) - 1]
        geom_i = max(tail, key=lambda i: (sharp[i], i))
        geom = frames[geom_i]

        # 投票帧：整体最清晰的几帧，几何帧必须在里面
        idx = sorted(range(len(frames)), key=lambda i: -sharp[i])[: self.max_frames]
        if geom_i not in idx:
            idx[-1] = geom_i
        idx.sort()

        t_det = time.perf_counter()
        per_frame: dict[int, list[LabelBox]] = {}
        for i in idx:
            per_frame[i] = self._detect(frames[i])
        det_ms = (time.perf_counter() - t_det) * 1000

        boxes = per_frame.get(geom_i) or []
        # 比整幅画面还大的一块：不可能是贴在小积木上的标签
        big_limit = self.r.g_big * min(h, w)
        boxes = [b for b in boxes if min(b.bbox[2], b.bbox[3]) <= big_limit]
        boxes = boxes[: self.max_blocks]

        blocks: list[Block] = []
        dropped: list[dict] = []
        v_plane = cv2.cvtColor(geom, cv2.COLOR_BGR2HSV)[:, :, 2]
        t_read = time.perf_counter()
        for lb in boxes:
            made = self._read_one(geom, lb)
            block = Block(bbox=tuple(int(v) for v in lb.bbox))
            block.fill = float(lb.fill_ratio)
            block.contrast = ring_contrast(geom, lb.bbox, v_plane=v_plane)
            if made is None:
                block.reason = "没有笔画"
                block.thin = 1.0
                dropped.append({"bbox": list(block.bbox), "why": "没有笔画"})
                continue
            block.tile, block.mask = made
            block.thin = stroke_thin(block.mask)
            block.px = (int(lb.bbox[2]), int(lb.bbox[3]))
            block.shape_ok = (block.contrast >= self.r.g_contrast
                              and block.thin <= self.r.g_thin
                              and block.fill >= self.r.g_fill)
            # 先在几何帧上读一次：连"最像的字母"都只有 0.4 分的候选，不值得
            # 再花三帧去投票（现场一张桌面照能框出几十个这种候选）。
            first, fscore, fmargin = self.r.recognize(block.mask, step=self.step)
            block.letter, block.score, block.margin = first, fscore, fmargin
            block.frames = 1
            block.agree = 1.0 if first else 0.0
            if not (block.shape_ok or fscore >= 0.60):
                block.conf = "none"
                if block.contrast < self.r.g_contrast:
                    block.reason = f"不够白 {block.contrast:.2f}"
                elif block.thin > self.r.g_thin:
                    block.reason = f"笔画太粗 {block.thin:.2f}"
                elif block.fill < self.r.g_fill:
                    block.reason = f"填充率 {block.fill:.2f}"
                else:
                    block.reason = f"分低 {fscore:.2f}"
                dropped.append({"bbox": list(block.bbox), "why": block.reason,
                                "score": round(fscore, 2)})
                continue

            self._vote(block, lb, frames, per_frame)
            # 白底白字（积木本身是白的，直接在白面上写）过不了亮度这一关，
            # 但字形极干净时放它过去 —— 和实时链路同一条规则。
            block.strong = (block.score >= 0.82 and block.margin >= 0.12
                            and block.thin <= 0.26 and block.fill >= self.r.g_fill)
            if not (block.shape_ok or block.strong):
                block.conf = "none"
                block.reason = block.reason or f"形状不像纸 {block.contrast:.2f}"
                dropped.append({"bbox": list(block.bbox), "why": block.reason,
                                "score": round(block.score, 2)})
                continue
            self._grade(block)
            blocks.append(block)
        read_ms = (time.perf_counter() - t_read) * 1000

        # 模型只在"模板没把握"的地方补一刀。模板已经 0.9 的块去问模型，
        # 除了多花两秒，还能把它的结论搅浑。
        t_vlm = time.perf_counter()
        vlm_calls = 0
        self._calls = 0
        if use_vlm and self.r.vlm.available:
            need = [b for b in blocks if self._worth_asking(b)]
            need.sort(key=self._ask_priority)
            need = need[: self.vlm_max_blocks]
            if need:
                self._ask_vlm(need)

        # 一块都没读准：这是"标签太小，检测这一关就过不去"的情况。
        center_letter = ""
        if (not any(b.conf in ("high", "mid") for b in blocks)
                and use_vlm and self.r.vlm.available):
            # 远处（比如人举着积木离机位一米）标签只有 40×50px，光照一染就比
            # 手还暗 —— 任何几何检测都框不到它，但把画面中央放大喂给模型能读。
            # 这条只在一无所得时兜底，且结果只当提示，不参与基准判定。
            crop = geom[int(h * 0.18):int(h * 0.82), int(w * 0.18):int(w * 0.82)]
            res = self.r.vlm.read(crop)
            self._calls += 1
            if res.letter:
                center_letter = res.letter
        vlm_calls = self._calls
        vlm_ms = (time.perf_counter() - t_vlm) * 1000

        for b in blocks:
            self._grade(b)                       # 模型结论进来后重新定级
            if b.vlm and b.conf in ("low", "none"):
                b.reason = b.reason or f"模型读 {b.vlm}，模板没把握"

        blocks.sort(key=lambda b: (b.conf != "high", b.bbox[0]))
        self.last_blocks = blocks
        self.last_dropped = dropped
        self.last_frame = geom
        self.last_stats = {
            "frames": len(frames),
            "used": len(idx),
            "geom_frame": geom_i,
            "sharp": [round(s) for s in sharp],
            "candidates": len(boxes),
            "read": len([b for b in blocks if b.letter]),
            "high": len([b for b in blocks if b.conf == "high"]),
            "mid": len([b for b in blocks if b.conf == "mid"]),
            "low": len([b for b in blocks if b.conf == "low"]),
            "dropped": len(dropped),
            "vlm_calls": vlm_calls,
            "detect_ms": round(det_ms),
            "read_ms": round(read_ms),
            "vlm_ms": round(vlm_ms),
            "ms": round((time.perf_counter() - t0) * 1000),
        }
        self.last_annotated = annotate(geom, blocks, dropped)
        letters = " ".join(b.letter for b in blocks
                           if b.conf in ("high", "mid") and b.letter)
        return {"ok": True,
                "blocks": [b.as_dict() for b in blocks],
                "dropped": dropped,
                "center_letter": center_letter,
                "frame": [int(w), int(h)],
                "stats": self.last_stats,
                "msg": (f"读到 {letters}" if letters else
                        ("没框到标签" if not blocks else "框到了但没读准"))}

    # ------------------------------------------------------------ 定级
    def _grade(self, b: Block) -> None:
        """把「模板分 + 帧间一致率 + 模型意见」压成一个可信等级。

        这个函数是**幂等**的：VLM 阶段前后各调一次，结果不会因为调用次数
        变化。等级只由 block 上的事实决定。
        """
        if not (b.shape_ok or b.strong):
            b.conf = "none"
            return
        if not b.letter:
            b.conf = "low"
            b.reason = b.reason or "模板没读出"
            return
        if b.src == "vlm":
            # 模板一句都没读出来，字母完全来自模型。模型在"没人跟它抢"的
            # 时候最爱瞎猜，所以要求这块同时满足：够大、且明显比四周白。
            ok = (max(b.px) >= self.r.g_vlm_unknown_px
                  and b.contrast >= self.r.g_vlm_contrast)
            b.conf = "mid" if ok else "low"
            if not ok and not b.reason:
                b.reason = f"只有模型读 {b.letter}（白度 {b.contrast:.2f}）"
            return
        if b.vlm and b.vlm == b.letter:
            b.src = "both"
            # 两边都读成同一个字母，这一对证据比任何单边都强。但"模板第一名"
            # 本身必须站得住：桌面上的一块花纹也能既被模板读到 0.61、又被模型
            # 读成 A，而它的分数差距只有 0.01 —— 那种不是字母，是花纹。
            if b.margin >= 0.05 and b.score >= 0.70:
                b.conf = "high"
            elif b.margin >= 0.05 and b.score >= 0.60:
                b.conf = "mid"
            else:
                b.conf = "low"
                b.reason = f"认不准 {b.letter}（甩不开第二名 {b.margin:.2f}）"
            return
        if b.vlm and b.vlm != b.letter:
            if (b.score < 0.70 and max(b.px) >= 100
                    and b.contrast >= self.r.g_vlm_contrast):
                # 模板只有 0.5 出头，模型却给出另一个字母：库里没这个字形时，
                # 模板必然把它顶到最像的那个（C 会被读成 B）。这种信模型。
                b.reason = f"模型 {b.vlm}（模板只到 {b.score:.2f}）"
                b.letter, b.src, b.conf = b.vlm, "vlm", "mid"
                return
            b.src = "template"
            b.conf = "low"
            b.reason = f"模板 {b.letter} / 模型 {b.vlm}"
            return

        b.src = "template"
        if (b.score >= 0.72 and b.margin >= 0.05 and b.agree >= 0.5
                and b.frames >= 2):
            b.conf = "high"
        elif b.score >= 0.62 and b.margin >= 0.04 and b.agree >= 0.40:
            b.conf = "mid"
        else:
            b.conf = "low"
            b.reason = b.reason or (f"分低 {b.score:.2f}" if b.margin >= 0.04
                                    else f"认不准（甩不开第二名 {b.margin:.2f}）")

    # ------------------------------------------------------------ 要不要叫模型
    def _worth_asking(self, b: Block) -> bool:
        """这一块值不值得花一次模型调用（现场实测一次 1-4 秒）。

        不值得的典型：模板给了 0.42 分、跟谁都不像的椅子边/灯带。问它只会
        等四秒换回来一个瞎猜的字母。值得的是：有像样的候选字母但没把握，
        或者压根读不出却明显是一张够大的白纸。已经高可信的不再问 —— 那只会
        多花几秒，还会把结论搅浑。
        """
        if b.conf == "high":
            return False
        if b.letter and b.score >= 0.45:
            return True
        return (not b.letter) and max(b.px) >= 90

    @staticmethod
    def _ask_priority(b: Block) -> tuple:
        """先问"读不出但够大"的，再问"有候选但分低"的。"""
        if not b.letter:
            return (0 if max(b.px) >= 120 else 1, -max(b.px), -b.score)
        return (2, -b.score, -max(b.px))

    def _ask_vlm(self, blocks: list[Block]) -> int:
        """并行问模型。同一张裁片在缓存里就不再问。"""
        def one(b: Block) -> None:
            if b.tile is None:
                return
            tile = b.tile
            # 40×45px 的裁片直接送模型，它只能看到一坨糊；先放大到 ~320px
            side = max(tile.shape[:2])
            if side < 200:
                s = min(4.0, 320.0 / max(side, 1))
                tile = cv2.resize(tile, None, fx=s, fy=s,
                                  interpolation=cv2.INTER_CUBIC)
            got = self.r.vlm.peek(tile)
            if got is None:
                res = self.r.vlm.read(tile)
                self._calls += 1
                if res.error:
                    b.reason = f"模型出错：{res.error[:40]}"
                    return
                got = res.letter
            b.vlm = got or ""
            if got and not b.letter:
                # 模板一句都没读出来：模型说了算，但要求这块够大，
                # 否则它会对着反光、屏幕、人脸硬给一个字母。
                b.letter, b.src = got, "vlm"
                b.conf = "mid" if max(b.px) >= self.r.g_vlm_unknown_px else "low"

        with ThreadPoolExecutor(max_workers=max(1, self.vlm_workers)) as pool:
            list(pool.map(one, blocks))
        return len(blocks)


# --------------------------------------------------------------------- 画图
def annotate(img: np.ndarray, blocks: list[Block],
             dropped: list[dict] | None = None) -> np.ndarray:
    """把结论画在图上（存快照用；页面上的框由浏览器自己画）。"""
    out = img.copy()
    for b in blocks:
        col = {"high": (120, 220, 60), "mid": (60, 200, 220),
               "low": (40, 160, 255), "none": (140, 140, 140)}.get(b.conf,
                                                                   (140, 140, 140))
        x, y, w, h = b.bbox
        cv2.rectangle(out, (x, y), (x + w, y + h), col, 2)
        tag = (b.letter or "?") + f" {b.score:.2f}/{b.conf}"
        cv2.putText(out, tag, (x, max(16, y - 7)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.62, col, 2, cv2.LINE_AA)
    for d in dropped or []:
        x, y, w, h = d["bbox"]
        cv2.rectangle(out, (x, y), (x + w, y + h), (120, 120, 120), 1)
        if d.get("why"):
            cv2.putText(out, str(d["why"])[:14], (x, y + h + 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (150, 150, 150), 1,
                        cv2.LINE_AA)
    return out


def save_annotated(img: np.ndarray, out_dir: str = "vision/records") -> str:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"scan-{datetime.now():%Y%m%d-%H%M%S}.jpg"
    cv2.imwrite(str(path), img)
    return str(path)
