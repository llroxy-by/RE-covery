"""颜色目标检测：HSV 阈值 → 形态学 → 轮廓 → 质量打分。

不训练模型（P0 明确排除）。置信度是启发式质量分数，不是统计概率，
它只回答"这个检出有多值得信"，不回答"这一定是同一块积木"。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


@dataclass
class Detection:
    object_id: str
    color: str
    center_px: tuple[float, float]
    area_px: float
    bbox: tuple[int, int, int, int]  # x, y, w, h
    fill_ratio: float
    confidence: float
    reasons: list[str] = field(default_factory=list)
    is_unknown: bool = False
    # 从白色标签上读到的字母。空字符串表示没读出来或没启用标签识别。
    letter: str = ""
    letter_score: float = 0.0

    def as_reason_text(self) -> str:
        return ",".join(self.reasons) if self.reasons else "-"


def _clamp01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def _bbox_str(bbox) -> str:
    """把 bbox 压成一行放得进 issue 字符串的形式。"""
    return "@".join(str(int(v)) for v in bbox)


class Detector:
    """按配置里的颜色表检测目标。同色多候选只认最大的一块，但会打折置信度。"""

    def __init__(self, config: dict) -> None:
        if cv2 is None:
            raise RuntimeError("检测依赖 OpenCV，请先安装 requirements.txt")
        self.colors: dict[str, dict] = dict(config.get("colors", {}))
        if not self.colors:
            raise ValueError("配置里没有 colors，无法检测任何目标")
        self.min_area_px = float(config.get("min_area_px", 700))
        self.max_area_ratio = float(config.get("max_area_ratio", 0.22))

        conf = config.get("confidence", {})
        self.base_conf = float(conf.get("base", 0.95))
        self.min_fill_ratio = float(conf.get("min_fill_ratio", 0.55))
        self.margin_penalty = float(conf.get("zone_margin_penalty", 0.15))
        self.jitter_penalty = float(conf.get("jitter_penalty", 0.25))
        # 开运算核要大一点：JPEG 压缩会在色块边缘产生 2–4 px 的彩色镶边，
        # 5x5 开不掉，会被当成"同色第二块"而误降置信度。
        self._open_kernel = np.ones((7, 7), np.uint8)
        self._kernel = np.ones((5, 5), np.uint8)
        # 只有第二块够大（≥ 主候选的 35%）才算真的"多候选"，边缘碎片不算
        self.multi_candidate_ratio = 0.35
        # 齐边遮挡（被书本/盒子边缘切掉一半）剩下的还是干净矩形，fill 仍是 1.0，
        # 光看填充率抓不到。已知目标是什么形状时，用"轮廓长宽比跑掉了"来抓。
        # 没配 expected_aspect 的颜色不做此检查（保持向后兼容）。
        self.shape: dict[str, dict] = {
            k: v for k, v in (config.get("shape") or {}).items()
        }
        # 「背景」必须能被显式声明。桌面是紫红的，S 中位 133 —— 比大多数
        # 物体还"高饱和"。不扣掉的话，找未知物体时整张桌子会连成一整块
        # （实测 72 万像素）超过 max_area_ratio 被丢弃，于是：
        #   ① 桌上的暗部/桌沿在别的曝光下冒出碎块 → 凭空多出 unknown_001
        #   ② 真放一个橙色物体进去，它和桌面连通 → 被一起吞掉，永远发现不了
        # 两个症状看似相反，病根是同一个：背景没被排除。
        self.background_ranges: list = list((config.get("background") or {}).get("ranges") or [])
        # 「不认识的东西」的饱和度/亮度门槛。
        # S：60/255 只有 24%，灰蒙蒙的东西也能过线，别指望它挡背景。
        # V：场上的物件是摆在桌上被照亮的（实测绿块 221、红块 255、橙色异物 229），
        #    而投影阴影只有 82，桌沿暗部 72 —— 拿 V 一刀切干净。
        #    暗到像阴影的东西本来也不该被当成"多出来的一件物体"。
        self.unknown_min_s = float(config.get("unknown_min_s", 60))
        self.unknown_min_v = float(config.get("unknown_min_v", 125))
        # 调试用：每帧记录各颜色的最终掩膜，供 --debug-mask 显示
        self.last_masks: dict[str, np.ndarray] = {}
        self.last_residual: np.ndarray | None = None
        self.last_dets: list[Detection] = []
        self.last_label_info: list[dict] = []

        # "太暗"阈值必须由 HSV 表的 V 下限推出来：V 下限以下的像素本来就检不到，
        # 拍死一个固定阈值会变成永远触发不了（或一触发就全灭）的死代码。
        v_floor = 0
        for spec in self.colors.values():
            for lo, _hi in spec.get("ranges", []):
                v_floor = max(v_floor, int(lo[2]))
        self.v_too_dark = float(v_floor + 40)
        self.v_too_bright = 250.0

        # 标签认字母：颜色只负责"这儿有东西"，字母负责"这是哪一块"。
        # 因为桌面本身就是紫红的，靠颜色定身份在光照一变就会认错；
        # 白底黑字的标签在 HSV 上离桌面很远，稳得多。
        self.label_cfg: dict = dict(config.get("label") or {})
        self.label_enabled = bool(self.label_cfg.get("enabled"))
        # primary=True：标签就是定位依据，颜色完全不参与。适合"桌面颜色和
        # 积木撞色"或"灯光把颜色染偏"的现场。False 时退回"颜色定位 + 字母认身份"。
        self.label_primary = bool(self.label_cfg.get("primary"))
        self.letter_bank = None
        if self.label_enabled:
            from .letters import LetterBank
            bank_path = self.label_cfg.get("bank")
            self.letter_bank = (LetterBank.load(bank_path) if bank_path
                                else LetterBank())
            self.label_match_threshold = float(self.label_cfg.get("match_threshold", 0.60))
            # 连续角度搜索的步长。15° 是实测下限：30° 时最坏角误差 15°，
            # 自比分数从 0.82 掉到 0.59，等于白做。
            self.label_angle_step = int(self.label_cfg.get("angle_step", 15))

        # 认字母的"读法"：
        #   template 只靠本地模板库（必须现场登记，离线可用）
        #   vlm      只靠视觉模型（不用登记，但要联网 + key）
        #   auto     有 key 就"模型为主、模板校验"，没 key 自动退回 template
        # 默认 auto：现场没网也能跑，有网就更准、还能认没登记过的字母。
        self.label_reader = str(self.label_cfg.get("reader", "auto") or "auto").lower()
        self.vlm = None
        if self.label_enabled and self.label_reader in ("vlm", "auto", "hybrid"):
            from .vlm import VlmReader
            vcfg = dict(self.label_cfg.get("vlm") or {})
            self.vlm = VlmReader(
                base_url=str(vcfg.get("base_url", "")),
                model=str(vcfg.get("model", "")),
                api_key=str(vcfg.get("api_key", "")),
                timeout_s=float(vcfg.get("timeout_s", 8.0)),
                max_side=int(vcfg.get("max_side", 512)),
            )

        # 整帧兜底：一个标签框都找不到时，退而让模型直接看整幅画面。
        # 现场实测（2026-11-07 那张举着积木的照片）：标签只有 23×32 px，
        # Canny 在全图上找不到它的四条边，模板比对自然一片空白。而模型
        # 直接看整帧能读出 B —— 慢（10 秒上下），但至少认得出。
        self.last_global_letter = ""     # 最近一次整帧识别结果，给页面显示
        self._global_letter_at = 0.0
        self._global_tile = None         # 送去识别的那一帧，之后用它查缓存
        self._global_warm_at = 0.0
        self._global_ttl = 8.0           # 结果新鲜多久
        self._global_gap = 5.0           # 两次请求之间至少隔这么久

    def reader_name(self) -> str:
        """当前实际生效的读法，给页面显示用。

        配了模型但没就绪时，**把原因一起带上**，而不是干巴巴回"模板比对" ——
        否则页面看起来一切正常，用户会以为模型正在帮忙，实际一次都没调通。
        """
        if self.vlm is not None and self.vlm.available:
            if self.label_reader == "vlm":
                return f"模型 {self.vlm.describe()}"
            return f"模型+模板 {self.vlm.describe()}"
        if self.vlm is not None:
            return f"模板比对（模型未就绪：{self.vlm.why_unavailable()}）"
        return "模板比对"

    # ------------------------------------------------------------ 公开入口
    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], list[str]]:
        """返回 (已知颜色目标, 问题列表)。问题列表非空时快照整体 quality_ok=False。"""
        h, w = frame_bgr.shape[:2]
        frame_area = float(h * w)
        max_area = self.max_area_ratio * frame_area
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

        # 标签主导模式：直接拿标签当物体，完全不看颜色。
        # 实测那张真实照片里，紫光把绿积木染成 H=123（配置的绿色是 40-85），
        # 红积木又被自己的白标签切成两半，颜色定位给出的 bbox 一个是画面
        # 外的东西、一个只有半截 —— 而两个标签本身被稳稳找到了。
        # 既然标签已经可靠，就没必要先靠颜色猜"这儿有东西"。
        if self.label_primary and self._labels_usable():
            return self._detect_by_labels(frame_bgr)

        dets: list[Detection] = []
        issues: list[str] = []
        known_masks: list[np.ndarray] = []
        self.last_masks = {}

        # 标签每帧只找一次（Canny 全图跑，别每块积木重复算一遍），
        # 再按位置分给各自的积木。
        label_boxes: list = []
        if self.label_enabled and self._labels_usable():
            from .letters import find_labels
            label_boxes = find_labels(frame_bgr, self.label_cfg)

        for color, spec in self.colors.items():
            mask = np.zeros((h, w), np.uint8)
            for lo, hi in spec.get("ranges", []):
                mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
            known_masks.append(mask)
            det = self._detect_one(frame_bgr, hsv, mask, color, spec, max_area)
            if det is None:
                continue
            # 模板库空的时候退回颜色身份，不报错也不拦基准 —— 否则刚装好还没
            # 登记字母时整个系统等于不可用，用户会以为是坏的。
            if self.label_enabled and self._labels_usable():
                letter, score = self._read_letter(frame_bgr, det, label_boxes)
                if not letter or score < self.label_match_threshold:
                    # 认不出是哪一块，就不要硬塞一个身份进去 —— 猜错了，整个
                    # 复位验证的结论就是错的。让这块在本帧消失，下游按"没看到"
                    # 处理，最终会落到 REVIEW（看不准，必须人来判），而不是
                    # 编一个 ID 让差异算法以为场上多了一块/少了一块。
                    issues.append(f"{det.object_id}:label_unreadable")
                    if score > 0:
                        issues.append(f"{det.object_id}:label_low_score:{score:.2f}")
                    continue
                det.letter = letter
                det.letter_score = round(float(score), 3)
                det.object_id = f"block_{letter}"
            dets.append(det)
            if det.reasons:
                issues.extend(f"{det.object_id}:{r}" for r in det.reasons)

        unknowns = self._detect_unknowns(frame_bgr, hsv, known_masks, max_area)
        dets.extend(unknowns)
        for u in unknowns:
            issues.append(f"{u.object_id}:unrecognized_object")

        # 同一个 object_id 出现多次说明配置有重复 object_id，属于配置错误
        ids = [d.object_id for d in dets if not d.is_unknown]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            issues.append("duplicate_object_id:" + ",".join(sorted(dup)))

        dets.sort(key=lambda d: (-d.area_px, d.object_id))
        self.last_dets = dets          # 调试/诊断用，别拿它做业务判断
        return dets, issues

    # ------------------------------------------------------------ 单色检测
    def _detect_one(self, frame, hsv, mask, color, spec, max_area) -> Detection | None:
        object_id = spec.get("object_id") or f"block_{color}_01"
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._open_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)
        self.last_masks[color] = mask
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < self.min_area_px or area > max_area:
                continue
            m = cv2.moments(c)
            if m["m00"] <= 0:
                continue
            cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
            x, y, bw, bh = cv2.boundingRect(c)
            fill = area / float(max(bw * bh, 1))
            candidates.append((area, (cx, cy), (x, y, bw, bh), fill))

        if not candidates:
            return None
        candidates.sort(key=lambda t: -t[0])
        area, center, bbox, fill = candidates[0]

        reasons: list[str] = []
        conf = self.base_conf

        if len(candidates) > 1 and candidates[1][0] >= self.multi_candidate_ratio * area:
            reasons.append("multi_candidate")
            conf -= 0.20 if candidates[1][0] > 0.5 * area else 0.10

        if fill < self.min_fill_ratio:
            reasons.append("possibly_occluded")
            conf -= 0.20

        # 齐边遮挡：形状本是方/近方形，被直边物体切掉一部分后轮廓会被拉长。
        # fill 仍是 1.0，只能靠长宽比跑偏来发现。归到 possibly_occluded，
        # 语义就是"这一块看到的可能不是完整的它"。
        shape = self.shape.get(color) or {}
        exp_aspect = shape.get("expected_aspect")
        if exp_aspect:
            tol = float(shape.get("aspect_tolerance", 0.35))
            _, _, bw, bh = bbox          # 必须取选中候选的框，不是循环里最后一个
            ar = bw / float(max(bh, 1))
            if not (1 - tol) * exp_aspect <= ar <= (1 + tol) * exp_aspect:
                if "possibly_occluded" not in reasons:
                    reasons.append("possibly_occluded")
                    conf -= 0.20
                reasons.append(f"shape_irregular:{ar:.2f}")

        v_mean = float(cv2.mean(hsv[:, :, 2], mask=mask)[0])
        s_mean = float(cv2.mean(hsv[:, :, 1], mask=mask)[0])
        if v_mean < self.v_too_dark:
            reasons.append("too_dark")
            conf -= 0.20
        elif v_mean > self.v_too_bright and s_mean < 60:
            # 过曝的特征是"亮到发白、颜色被冲掉"，所以必须同时看饱和度。
            # 只看 V 会把饱和的纯色（比如 V=255 的正红）冤枉成过曝。
            reasons.append("overexposed")
            conf -= 0.15

        h, w = frame.shape[:2]
        x, y, bw, bh = bbox
        if x <= 2 or y <= 2 or x + bw >= w - 2 or y + bh >= h - 2:
            reasons.append("touching_frame_edge")  # 可能已经被挪出画面
            conf -= 0.15

        return Detection(
            object_id=str(object_id),
            color=color,
            center_px=(round(float(center[0]), 2), round(float(center[1]), 2)),
            area_px=round(area, 1),
            bbox=(int(x), int(y), int(bw), int(bh)),
            fill_ratio=round(fill, 3),
            confidence=round(_clamp01(conf), 3),
            reasons=reasons,
            is_unknown=False,
        )

    # ------------------------------------------------------------ 标签主导
    def _read_global(self, img_bgr: np.ndarray) -> str:
        """整帧兜底：没有标签框时，让模型直接看整幅画面读字母。

        为什么不在 /frame 里同步等模型：一次 8-12 秒，页面 380ms 一帧，
        等一次就等于画面冻住十秒。所以沿用 warm/peek 那套 —— 这一帧把图
        丢进后台队列，之后每帧只查缓存，算好了自然冒出来。

        **查缓存用的是当时送出去的那一帧，不是当前帧。** 人一举一动画面
        就变，aHash 变一点就是新 key；拿当前帧去查永远 miss，等于每帧都
        白跑一趟。
        """
        if self.vlm is None or not self.vlm.available:
            return ""
        now = time.time()

        # 上次送出去那帧算好了没有
        if self._global_tile is not None:
            got = self.vlm.peek(self._global_tile)
            if got is not None:
                self.last_global_letter = got
                self._global_letter_at = now
                self._global_tile = None
                return got

        if self.last_global_letter and now - self._global_letter_at < self._global_ttl:
            return self.last_global_letter

        # 还没算完 / 结果过期了，按最小间隔重新排队
        if now - self._global_warm_at >= self._global_gap:
            self._global_warm_at = now
            # 只送画面中央 60%：实测整帧 1280×720 要 11 秒，中央 768×432 只要
            # 4.6 秒，而且背景里的横幅、灯带、玻璃反光全被裁掉了 —— 那些东西
            # 正是把模型带偏的元凶（它会把横幅边缘读成 K、把反光读成 E）。
            h, w = img_bgr.shape[:2]
            crop = img_bgr[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
            if crop.size == 0:
                crop = img_bgr
            self._global_tile = crop
            self.vlm.warm(crop)
        return ""

    def _detect_by_labels(self, img_bgr: np.ndarray) -> tuple[list[Detection], list[str]]:
        """每个矩形标签就是一件物体，身份来自标签上的字母。不使用任何颜色信息。"""
        from .letters import character_mask, find_labels, normalize

        labels = find_labels(img_bgr, self.label_cfg)
        dets: list[Detection] = []
        issues: list[str] = []
        # 整帧直读是**常驻通道**，不是失败兜底。原因：现场那张照片里
        # find_labels 能给出 23 个候选（全是背景玻璃和灯带），一个都认不对，
        # 而真标签只有 23×32 px、压根没成框 —— 靠"先框到位再读"这条路在
        # 这个距离上就是走不通。索性每个检测周期都让模型看一眼画面中央，
        # 内部有 5 秒限频，不会把队列打满。
        self._read_global(img_bgr)
        info: list[dict] = []          # 给页面看的诊断：每个标签读到什么、多少分
        unread: list[tuple] = []       # 读不出字的候选，等整帧看完再决定算不算问题
        pending: list = []             # 模型还在读的候选，同样等整帧看完再算

        for lb in labels:
            tile = normalize(img_bgr, lb)
            mask = character_mask(tile) if tile is not None else None

            # —— 本地模板读法 ——
            t_letter, t_score = "", 0.0
            if mask is not None and self.letter_bank is not None:
                t_letter, t_score = self.letter_bank.recognize(
                    mask, step=self.label_angle_step)

            # —— 视觉模型读法：只查缓存，绝不在这里等 ——
            # 没算过就丢给后台线程，本帧先拿模板结果顶着，算完下一帧接管。
            v_letter, v_state = None, "off"
            if self.vlm is not None and self.vlm.available and tile is not None:
                v_letter = self.vlm.peek(tile)
                if v_letter is None:
                    self.vlm.warm(tile)
                    v_state = "pending"
                else:
                    v_state = "hit"

            letter, score, src = self._fuse(t_letter, t_score, v_letter)

            if not letter:
                # 失败原因要说准：模型还在算和"白纸上没字"是两件事，
                # 前者等一下就好，后者要用户换标签，混成一句话会把人带错方向。
                if v_state == "pending":
                    why = "模型还在读"
                elif src == "vlm_blank":
                    why = "模型看不出"
                elif mask is None:
                    why = "标签上没有字"
                else:
                    why = "分数不够"
                info.append({"bbox": list(lb.bbox), "letter": "", "score": 0.0,
                             "angle": 0.0, "src": src, "vlm": v_state, "why": why})
                if mask is not None and v_state == "pending":
                    # 模型还在算，这一帧先别报"读不出"，否则页面会闪红
                    pending.append(lb)
                elif mask is None:
                    issues.append(f"label_empty:{_bbox_str(lb.bbox)}")
                else:
                    unread.append((lb, score))
                continue

            ok = score >= self.label_match_threshold
            # 角度是「模板比对时最像的那一档」，只有模板真的参与了才有意义。
            # 纯模型读数时 last_angle 还是上一张标签留下的旧值，显示出来是假信息。
            ang = 0.0
            if src in ("template", "both", "vlm_conflict") and self.letter_bank is not None:
                ang = float(getattr(self.letter_bank, "last_angle", 0.0))
            info.append({"bbox": list(lb.bbox), "letter": letter,
                         "score": round(float(score), 3),
                         "angle": round(ang, 1),
                         "src": src, "vlm": v_state, "why": ""})
            if not ok and v_state == "pending":
                pending.append(lb)
                continue
            if not ok:
                # 读不出字的标签**不产出物体**。以前这里会造一个 unknown_00N，
                # 结果是白纸上一条无关的横线被找到、勉强凑出 0.59 分，就凭空
                # 多出一个"物体"，把缺件判断和 issues 数全带歪（实测那张真实
                # 照片就这样冒过 3-13 个幽灵物体）。读不出就当看不见，但留一条
                # issue 让人知道"这儿有一张标签我没读出来"。
                unread.append((lb, score))
                continue
            x, y, bw, bh = lb.bbox
            # 匹配分数和"检测置信度"不是同一个尺度：score 0.60 只是刚过线，
            # 但它仍然是一件被明确看到、明确读出来的物体。这里映射到
            # [0.80, 1.0]，让下游的 min_conf 不会把"认出但不太像"当成"看不见"。
            th = self.label_match_threshold
            conf = 0.80 + 0.20 * (score - th) / max(1e-6, 1.0 - th)
            dets.append(Detection(
                object_id=f"block_{letter}", color="label",
                center_px=(round(x + bw / 2.0, 2), round(y + bh / 2.0, 2)),
                area_px=lb.area_px, bbox=lb.bbox, fill_ratio=lb.fill_ratio,
                confidence=round(min(1.0, conf), 3),
                letter=letter, letter_score=round(float(score), 3)))

        # 读不出的候选要分两类。桌面上的白纸角、键盘白键、便签边缘都会被当成
        # 标签候选，它们一张嘴就是 label_unread，而 quality_ok 直接拿 issues 判否
        # —— 结果就是桌上随便有片白纸，基准永远建不起来（实测那张真实照片上就
        # 有一块 81×89 的白块，正好卡死整个演示）。
        # 判据用同一帧里「已经认出来的标签有多大」：比最小的那个还小一大截，
        # 就按背景白块处理，不参与合格判定；但仍然留在页面诊断里，不会凭空消失。
        known = [d.area_px for d in dets]
        floor = 0.35 * min(known) if known else 0.0

        def _is_noise(lb) -> bool:
            """比同帧最小真标签还小一大截 —— 白纸角、键盘白键这类背景。"""
            if not floor or lb.area_px >= floor:
                return False
            for item in info:
                if item["bbox"] == list(lb.bbox) and not item["letter"]:
                    item["why"] = "太小，按背景白块跳过"
                    item["noise"] = True
            return True

        for lb in pending:
            if _is_noise(lb):
                continue
            issues.append(f"label_pending:{_bbox_str(lb.bbox)}")
        for lb, score in unread:
            if _is_noise(lb):
                continue
            issues.append(f"label_unread:{_bbox_str(lb.bbox)}:{score:.2f}")

        ids = [d.object_id for d in dets if not d.is_unknown]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            issues.append("duplicate_object_id:" + ",".join(sorted(dup)))

        dets.sort(key=lambda d: (-d.area_px, d.object_id))
        self.last_dets = dets
        self.last_label_info = info

        # 兜底触发条件不能只看"有没有框"。现场那张照片里有 23 个背景假候选
        # （玻璃反光、灯带、横福边），labels 一个都不缺，但**一个字母都认不出**
        # —— 真标签只有 23×32 px，压根没成框。所以这里看的是"最后有没有
        # 认出一件东西"，没有就走整帧直读。
        return dets, issues

    # ------------------------------------------------------------ 读法合流
    def _labels_usable(self) -> bool:
        """标签身份能不能用：模板库里有字，或者视觉模型可用，任一条就够。

        以前这里只认「模板库非空」，于是纯模型模式（reader=vlm）在还没登记
        任何字母时直接掉回颜色定位 —— 而颜色正是这套系统最不可靠的一环
        （紫光把绿积木染成 H=123）。模型模式必须能独立成立。
        """
        if self.letter_bank is not None and self.letter_bank.tiles:
            return True
        return self.vlm is not None and self.vlm.available

    def _fuse(self, t_letter: str, t_score: float,
              v_letter: str | None) -> tuple[str, float, str]:
        """把「模板比对」与「视觉模型」两个读数合成一个 (字母, 分数, 来源)。

        v_letter 是**三态**，必须分清，否则会把"还没算"当成"算不出"：
          None → 模型没参与，或者还在后台算（本帧先拿模板顶着）
          ""   → 模型算完了，说看不出来（回了 "?"）
          "D"  → 模型给了字母

        分数是"这个字母有多可信"，不是模型输出概率 —— 模型压根不返回概率，
        0.95 只是"模型认出来了"的记账值。它和模板分数共用 label_match_threshold
        （0.60），所以两者必须落在同一量纲上：刚过线 0.60、很确定 0.95–1.00。
        """
        th = self.label_match_threshold
        have_t = bool(t_letter)

        # 没模型（没 key / 没配 base_url / 显式选 template）→ 原样透传，
        # 这条路径必须和引入模型之前一字不差，现场断网就是靠它。
        if self.vlm is None or not self.vlm.available or self.label_reader == "template":
            return t_letter, t_score, "template"

        if self.label_reader == "vlm":
            if v_letter:
                return v_letter, 0.95, "vlm"
            # 这一档刻意**不退回模板**：用户选 vlm 就是不要字形库那套，
            # 悄悄混进模板结果会让人分不清"认出来的到底是谁"。
            return "", 0.0, ("vlm_blank" if v_letter == "" else "vlm_wait")

        # ---- auto：模型为主、模板校验 ----
        if v_letter:
            if not have_t:
                # 模板库根本没这个字母（新加的 D、或者手写太草）—— 正是模型要补的位。
                return v_letter, 0.95, "vlm"
            if v_letter == t_letter:
                # 两条完全独立的路得出同一个结论，没有理由反而压低分数。
                # 取较高者：模板 0.62 + 模型 A 仍然是一次"确定"的识别。
                return v_letter, min(1.0, max(t_score, 0.95)), "both"
            if t_score < th + 0.20:
                # 模板只是"勉强过线"，算不上有主见 —— 0.6x 的分数在林子里
                # 抓谁都差不多（实测同字母自比 ≥0.82、异字母 ≤0.51，
                # 0.6 只是闸门）。这种"猜"不足以和模型叫板，直接听模型的。
                return v_letter, 0.95, "vlm"
            # 模板很确定（≥0.8），模型也很确定，结论却不同 —— 这才是真打架。
            # 以模型为准（它不看字形库，对手写/倾斜更宽容），但把分数压到
            # 刚过线：页面上的置信度会明显偏低，现场能看出"这块有争议"，
            # 而不是拿 0.95 假装很确定，最后把复位验证的结论带歪。
            return v_letter, round(th + 0.02, 3), "vlm_conflict"

        # 模型没答案（还在算 / 说看不出）→ 退回模板，这正是 auto 兜底的价值
        if have_t:
            return t_letter, t_score, "template"
        return "", 0.0, ("vlm_blank" if v_letter == "" else "vlm_wait")

    # ------------------------------------------------------------ 标签字母
    def _read_letter(self, img_bgr: np.ndarray, det: Detection,
                     labels: list) -> tuple[str, float]:
        """从落在这块积木范围内的标签上读字母（模板 + 模型，同一套合流）。"""
        from .letters import character_mask, normalize

        x, y, bw, bh = det.bbox
        pad_x, pad_y = 0.10 * bw, 0.10 * bh
        x0, y0 = x - pad_x, y - pad_y
        x1, y1 = x + bw + pad_x, y + bh + pad_y

        best: tuple[str, float] = ("", 0.0)
        for lb in labels:
            cx = float(lb.quad[:, 0].mean())
            cy = float(lb.quad[:, 1].mean())
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                continue
            tile = normalize(img_bgr, lb)
            if tile is None:
                continue
            mask = character_mask(tile)

            t_letter, t_score = "", 0.0
            if mask is not None and self.letter_bank is not None:
                t_letter, t_score = self.letter_bank.recognize(
                    mask, step=self.label_angle_step)

            v_letter = None
            if self.vlm is not None and self.vlm.available:
                v_letter = self.vlm.peek(tile)
                if v_letter is None:
                    self.vlm.warm(tile)

            letter, score, _src = self._fuse(t_letter, t_score, v_letter)
            if score > best[1]:
                best = (letter, score)
        return best

    # ------------------------------------------------------------ 未知色块
    def _background_mask(self, hsv) -> np.ndarray | None:
        """配置里声明的背景色（桌面、墙面）。没配就返回 None，行为不变。"""
        if not self.background_ranges:
            return None
        m = np.zeros(hsv.shape[:2], np.uint8)
        for lo, hi in self.background_ranges:
            m |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        return m

    def _detect_unknowns(self, frame, hsv, known_masks, max_area) -> list[Detection]:
        """已知颜色之外的高饱和色块 = 不认识的物体，给 unknown_00N 临时 ID。"""
        s = hsv[:, :, 1]
        v = hsv[:, :, 2]
        residual = ((s >= self.unknown_min_s) & (v >= self.unknown_min_v)).astype(np.uint8) * 255

        # 先扣背景：桌布/墙面本身是彩色的，留着它们 residual 就永远是一片
        # 连通的大陆，异物反而被并进去。
        bg = self._background_mask(hsv)
        if bg is not None:
            residual[bg > 0] = 0

        big = np.ones((13, 13), np.uint8)
        for mask in known_masks:
            # 已知目标边缘的混色像素不算未知物体，先膨胀再扣掉
            residual = cv2.subtract(residual, cv2.dilate(mask, big))

        residual = cv2.morphologyEx(residual, cv2.MORPH_OPEN, self._kernel)
        residual = cv2.morphologyEx(residual, cv2.MORPH_CLOSE, self._kernel)
        self.last_residual = residual
        contours, _ = cv2.findContours(residual, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        found = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < self.min_area_px or area > max_area:
                continue
            m = cv2.moments(c)
            if m["m00"] <= 0:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            found.append((area, (m["m10"] / m["m00"], m["m01"] / m["m00"]),
                          (x, y, bw, bh), area / float(max(bw * bh, 1))))
        found.sort(key=lambda t: -t[0])

        out = []
        for i, (area, center, bbox, fill) in enumerate(found, start=1):
            out.append(Detection(
                object_id=f"unknown_{i:03d}",
                color="unknown",
                center_px=(round(float(center[0]), 2), round(float(center[1]), 2)),
                area_px=round(area, 1),
                bbox=tuple(int(v_) for v_ in bbox),
                fill_ratio=round(fill, 3),
                confidence=round(_clamp01(0.40 if fill >= self.min_fill_ratio else 0.25), 3),
                reasons=["unrecognized_object"],
                is_unknown=True,
            ))
        return out


class StabilityTracker:
    """连续 N 帧区域一致 + 中心抖动在阈值内，才承认这个区域归属。

    未稳定时下游拿到的是 zone=unknown + 原因，按 uncertain 处理，
    而不是靠"多等几帧再显示"把不确定性藏起来。
    """

    def __init__(self, frames: int = 5, max_jitter_px: float = 18.0) -> None:
        self.need = max(1, int(frames))
        self.max_jitter_px = float(max_jitter_px)
        self._hist: dict[str, deque] = {}

    def reset(self) -> None:
        self._hist.clear()

    def update(self, observations: list[tuple[str, str, tuple[float, float] | None]]
               ) -> dict[str, bool]:
        """observations: [(object_id, zone, center or None)] → {object_id: 是否稳定}。"""
        result: dict[str, bool] = {}
        seen = set()
        for object_id, zone, center in observations:
            seen.add(object_id)
            dq = self._hist.setdefault(object_id, deque(maxlen=self.need))
            dq.append((zone, center))
            result[object_id] = self._is_stable(dq)
        for object_id in list(self._hist):
            if object_id not in seen:
                self._hist.pop(object_id, None)
        return result

    def _is_stable(self, dq: deque) -> bool:
        if len(dq) < self.need:
            return False
        zones = {z for z, _ in dq}
        if len(zones) != 1 or "unknown" in zones:
            return False
        centers = [c for _, c in dq if c is not None]
        if len(centers) < self.need:
            return False
        xs = [c[0] for c in centers]
        ys = [c[1] for c in centers]
        return (max(xs) - min(xs) <= self.max_jitter_px
                and max(ys) - min(ys) <= self.max_jitter_px)
