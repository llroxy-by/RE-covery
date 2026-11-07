"""认积木上的白色标签 + 手写字母。

为什么身份不靠颜色：桌面是紫红的（H 123-125, S 133），粉色积木和它几乎同色，
场景光一变颜色就漂，身份跟着错。白底黑字的标签在 HSV 上离桌面很远
（低饱和 + 高亮度），光照变化下稳定得多。

手写字母千变万化，不硬编码字形。做法是「现场登记」：把某块积木放到镜头前，
在页面上敲下它是哪个字母，系统把这块标签的二值笔画存成模板；之后每帧把
新标签的笔画和二值模板比相似度，取最高的那个。好处是不用训练、不用装 OCR、
手写连笔也能认，现场换积木只要重新登记。

局限（必须知道）：笔画模板比对的是「形状」，不是语义。6 和 9、M 和 W
上下颠倒时会很像。所以字母集要选形状差异大的。

关于摆放角度：积木是被手摆的，不可能每次正对着机位，所以比对时把查询字形
**绕笔画质心连续转一圈**（每 15° 一档，24 档）去和模板比，取最像的那一档。
不能只试 0/90/180/270 —— 手稍微一歪（比如 20°）四个正交角度全对不上。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

TILE = 48    # 归一化后的模板边长
CANON = 192  # 拉正时的采样宽度 / 二值化前的统一边长


@dataclass
class LabelBox:
    """画面里一个白色矩形标签。"""
    bbox: tuple[int, int, int, int]
    quad: np.ndarray          # 4x2 float32，按左上/右上/右下/左下排好
    area_px: float
    fill_ratio: float

    def as_dict(self) -> dict:
        return {"bbox": list(self.bbox), "area_px": round(self.area_px, 1),
                "fill_ratio": round(self.fill_ratio, 3)}


def _approx_quad(contour, peri: float, area: float, min_side: int):
    """给「凑不出 4 个顶点」的轮廓一次救援机会。

    小标签（镜头远、广角、或者贴在积木侧面被透视压扁）在 0.03×周长 的逼近
    精度下，一条边会被拆成两段，顶点数变成 6-9。先放大 epsilon 重试；仍不行
    就退到最小外接旋转矩形 —— 对歪着放的标签反而比顶点逼近更稳。

    救援必须带几何体检，否则会救回更糟的东西：桌面上一条斜着的细线，面积
    只有几千像素（能过大轮廓面积门槛），外接矩形却有 500×600，一旦进了候选
    列表就会把真正的标签当成「被包含」去重掉 —— 这是实测踩过的坑。
    所以长短边比和长边上限这两条一定要留着。
    """
    for eps in (0.04, 0.05, 0.06, 0.08, 0.10):
        a = cv2.approxPolyDP(contour, eps * peri, True)
        if len(a) == 4 and _quad_sane(a, min_side):
            return a
    (cx, cy), (w, h), ang = cv2.minAreaRect(contour)
    short, long = min(w, h), max(w, h)
    if short < max(6.0, min_side * 0.5) or long > max(260.0, min_side * 6.0):
        return None
    if long <= 0 or short / long < 0.25:
        return None
    return cv2.boxPoints(((cx, cy), (w, h), ang)).reshape(-1, 1, 2).astype(np.int32)


def _quad_sane(approx, min_side: int) -> bool:
    """四边形体检：得像个标签，不能是细长的一条缝或一整块桌面。"""
    (_, _), (w, h), _ = cv2.minAreaRect(approx)
    short, long = min(w, h), max(w, h)
    if short < max(6.0, min_side * 0.5) or long <= 0:
        return False
    if long > max(260.0, min_side * 6.0):
        return False          # 大到这个程度还凑不出干净四边形，多半是桌面/纸，不是标签
    return short / long >= 0.25


def find_labels(img_bgr: np.ndarray, cfg: dict | None = None,
                stats: list | None = None) -> list[LabelBox]:
    """找贴在积木上的矩形标签。

    不能用"低饱和 + 高亮度 = 白"来找。实测那张真实照片：紫色灯光把白纸
    和白标签一起染成 S≈110-135、V≈220，纸张和标签在 HSV 上几乎是同一个
    颜色，按颜色切根本切不开；而整张纸又是一个巨大的连通块，字都在里面。

    改成找"矩形边界"：标签的物理本质是贴在彩色积木上的一小片纸，它有
    清晰的四条边。Canny 抓边缘 + 多边形逼近成四边形，纸张的横线凑不出
    闭合四边形，自然被排除。

    两道过滤保证不是"随便一个矩形"：
      1. 内部是亮的（label 纸比积木亮得多：标签 V≈221，红积木 V=161、绿积木 V=113）
      2. 上面有深色笔画（没字的矩形不是标签）

    stats 传入一个 list 时，把每个候选的**拒绝原因**追加进去（诊断用）。
    找不出标签时，"分数低"和"根本没框到"是两种完全不同的病，光看返回值
    分不清 —— 别再拿脚本复制一份 Canny 逻辑去猜，直接用这个出口。
    """
    cfg = cfg or {}
    lo, hi = (cfg.get("canny") or [60, 160])[:2]
    min_area = float(cfg.get("min_area_px", 3000))
    max_ratio = float(cfg.get("max_area_ratio", 0.10))
    min_side = int(cfg.get("min_side_px", 40))

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    # 多档 Canny 一起上。固定一对阈值绑死在某一个背景上：笔记本纸上
    # 60/160 正好，换成紫色垫子就不一定了。自动档按图像中位亮度估，
    # 再补两对固定档兜底，最后按重叠度合并。
    med = float(np.median(gray))
    auto_lo = int(max(5, 0.55 * med))
    auto_hi = int(min(250, max(auto_lo + 30, 1.35 * med)))
    passes = [(auto_lo, auto_hi), (int(lo), int(hi)), (35, 100)]

    h, w = gray.shape
    v = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)[:, :, 2]
    bright_floor = float(np.median(v)) * 0.85   # 相对全图的亮区参考线

    out: list[LabelBox] = []
    for clo, chi in passes:
        edges = cv2.Canny(blur, clo, chi)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < min_area or area > max_ratio * h * w:
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.03 * peri, True)
            if len(approx) != 4:
                # 标签被缩小时 approxPolyDP 会把一条边拆成两段，顶点数变成
                # 6-9 —— 以前这里直接 continue，整张标签就丢了。实测手机退到
                # 2.5 倍距离（整幅缩到 40%）丢 B、Mac 内置摄像头在桌面距离拍
                # 也是这样，都是同一条造成的。改成先救一次，救不回来才丢。
                approx = _approx_quad(c, peri, area, min_side)
            if approx is None:
                if stats is not None:
                    stats.append({"area": int(area), "verts": 0,
                                  "bbox": cv2.boundingRect(c),
                                  "why": "凑不出四边形"})
                continue
            x, y, bw, bh = cv2.boundingRect(approx)
            if stats is not None:
                stats.append({"area": int(area), "verts": len(approx),
                              "bbox": (int(x), int(y), int(bw), int(bh)),
                              "why": ""})
            if min(bw, bh) < min_side:
                if stats is not None:
                    stats[-1]["why"] = f"短边 {min(bw, bh)}<{min_side}"
                continue
            quad = _order_quad(np.asarray(approx, np.float32).reshape(4, 2))

            # 长宽比要像一张纸。实测不过这一关的话，桌面上笔记本纸的横线
            # 会被凑成 679×387 的四边形，拉正后是 96×8 的一根线。
            wq = float(np.linalg.norm(quad[1] - quad[0]))
            hq = float(np.linalg.norm(quad[3] - quad[0]))
            if min(wq, hq) < 1e-3:
                continue
            if not 0.25 <= wq / hq <= 4.0:
                if stats is not None:
                    stats[-1]["why"] = f"长宽比 {wq / hq:.2f} 不像纸"
                continue

            # 内部要亮：这是"纸"而不是积木本体或桌面
            inner = v[y:y + bh, x:x + bw]
            if inner.size == 0 or float(np.median(inner)) < bright_floor:
                if stats is not None:
                    stats[-1]["why"] = "内部不够亮"
                continue
            if stats is not None:
                stats[-1]["why"] = "OK"
            out.append(LabelBox((x, y, bw, bh), quad, area,
                                area / float(max(bw * bh, 1))))

    return _dedupe(out)


def _dedupe(boxes: list[LabelBox], iou_limit: float = 0.55) -> list[LabelBox]:
    """合并同一个标签的重复检测。

    实测两种重复：① 内外两层轮廓各抓一次（面积 49140 / 46884 是同一张纸）；
    ② 标签内部还有个小矩形（B 标签里 97×69 那圈，面积 5878）—— 它的 IoU
    只有 0.12，重叠度过滤挡不住，但它是**被完整包含**在大标签里的，
    这种一定不是独立的一张标签。
    """
    boxes = sorted(boxes, key=lambda b: -b.area_px)
    kept: list[LabelBox] = []
    for b in boxes:
        if any(_iou(b.bbox, k.bbox) > iou_limit or _contained(b.bbox, k.bbox)
               for k in kept):
            continue
        kept.append(b)
    return kept


def _contained(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int],
               ratio: float = 0.85) -> bool:
    """inner 是否基本被 outer 吃掉。"""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    x0, y0 = max(ix, ox), max(iy, oy)
    x1, y1 = min(ix + iw, ox + ow), min(iy + ih, oy + oh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter >= ratio * float(max(iw * ih, 1))


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / float(union) if union else 0.0


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """把 4 个点排成循环顺序（左上/右上/右下/左下），供透视变换用。

    以前用的是"最小 sum 是左上、最小 diff 是右上"这套经典写法，对正着摆的
    矩形没问题，但**积木一转就废**：矩形转到 40° 时最上面的那个角同时也是
    最左的角，argmin(sum) 和 argmin(diff) 会返回同一个点 —— 四个角里有一个
    被选两次、另一个从没被选中，getPerspectiveTransform 拿到退化的四点对，
    变换把整张标签压成一条线。实测转 40° 时 B 的标签明明找到了（面积
    31704px²，和 0° 时的 31374 一样），却提不出一个笔画像素。

    改成按质心角度排序，必然得到一个真循环顺序。再滚一下让 q0→q1 落在长边
    上，这样同一张纸每次归一化出来的长宽方向一致（字可能差 90°，那个交给
    角度搜索）。
    """
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    pts = pts[np.argsort(ang)]
    k = int(np.argmin(pts.sum(axis=1)))       # 起点取最靠左上的角，保证可复现
    pts = np.roll(pts, -k, axis=0)

    def dist(i: int, j: int) -> float:
        return float(np.linalg.norm(pts[j] - pts[i]))

    if dist(0, 1) < dist(1, 2):               # 保证 q0→q1 是长边
        pts = np.roll(pts, -1, axis=0)
    return pts


def normalize(img_bgr: np.ndarray, box: LabelBox, max_side: int = CANON) -> np.ndarray | None:
    """把标签拉正。保持原长宽比，不硬拉到正方形。

    实测那张真实照片里 A 的标签是 338×167 的长条、B 的是 254×221 近方形。
    如果把两者都硬拉成 48×48，A 会被横向压扁一半，字形直接变形 ——
    所以这里只做透视矫正 + 等比缩放，字形归一化交给 character_mask。

    **采样宽度别低于 190**。A 是细笔手写，原图里笔画只有 3-4px 宽；缩到
    96 宽后笔画剩 1.5px，再走开闭运算就碎成噪点了（实测 ink 从 607 掉到
    274，字形断成几段），之后无论怎么比对都认不出来。192 宽是笔试出来
    笔画还完整的下限。
    """
    wq = float(np.linalg.norm(box.quad[1] - box.quad[0]))
    hq = float(np.linalg.norm(box.quad[3] - box.quad[0]))
    if min(wq, hq) < 12:
        return None
    scale = max_side / max(wq, hq)
    tw, th = max(8, int(round(wq * scale))), max(8, int(round(hq * scale)))
    dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]], np.float32)
    M = cv2.getPerspectiveTransform(box.quad, dst)
    return cv2.warpPerspective(img_bgr, M, (tw, th), flags=cv2.INTER_LINEAR)


def character_mask(tile_bgr: np.ndarray, size: int = TILE,
                   keep_rows: bool = False):
    """从拉正的标签图里提出字形，归一化成 size×size 的笔画模板。

    二值化同时跑 Otsu 和自适应两种，取"更像一个字"的那个：
      · 自适应阈值（窗口随图算）对光照不均好，但笔画粗时会把笔画内部挖空，
        手写粗笔 + 手机低码率压缩正好踩这个坑（实测实时流里 B 提取成了一团）。
      · Otsu 对"白底黑字"这种双峰图更稳，但标签一侧亮一侧暗时会漏。
    两种都试，用矩形度和笔画占比挑一个。

    另外**先把标签缩放到固定分辨率再二值化**。这一步不是为了快，是为了稳：
    后面的形态学开闭运算用 3×3 / 2×2 的核对吧，是绝对像素尺寸。输入标签
    一张 96×42、一张 300×150，同一个 3×3 核对细笔画的作用完全不同 ——
    小图里一笔 2px 宽，闭运算直接把它糊成一团；大图里一笔 8px 宽，开运算
    连毛刺都去不掉。实测同一块积木换条链路，笔画数能差 20%，相似度从
    0.9 掉到 0.55。缩到同一尺寸后，笔画粗细变成"相对字形的比例"，跨分辨率
    才可比。

    还有一步**边框必须在二值化之前裁掉**，不能等二值化之后再裁。理由：
    Otsu 是全局阈值，画面里只要多一圈亮边（透视框稍微框大一点就会有，
    边缘几像素而已），它就会把"标签 vs 亮边"当成主要分界 —— 结论是
    "整张标签都是墨"：实测 ink_ratio 0.99、外接框长宽比 2.5，被"整片实心"
    的闸门直接拒掉，退回自适应阈值，而自适应又只抓到 0.13 的笔画。
    两个候选一个被拒一个残缺，分数从 0.83 塌到 0.45，而且**每次框大框小
    结果还不一样** —— 这就是"同一块 B 一会儿认得出、一会儿认不出"的根。
    """
    if tile_bgr is None or tile_bgr.size == 0:
        return None
    th0, tw0 = tile_bgr.shape[:2]
    if max(th0, tw0) != CANON:
        s = CANON / float(max(th0, tw0))
        tile_bgr = cv2.resize(
            tile_bgr, (max(8, int(round(tw0 * s))), max(8, int(round(th0 * s)))),
            interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    tile_bgr = _trim_border(tile_bgr)
    if tile_bgr.size == 0:
        return None
    gray = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2GRAY)
    inv = 255 - gray                      # 白底黑字 → 黑底白字

    cands: list[tuple[str, np.ndarray]] = []
    try:
        otsu_thr, _ = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        # Otsu 是**全局**阈值，它的前提是"画面里只有两类东西：纸和墨"。
        # 这个前提经常不成立：透视框比贴纸大一点、标签翘起一个角、旁边有一块
        # 白纸，画面里就多了一大片亮背景，于是 Otsu 把"亮背景 vs 标签"当成
        # 主分界，判出"整张都是墨"（实测 ink_ratio 0.83-0.90）。
        # 这种候选会被下面的闸门直接拒掉，只剩自适应阈值那个残缺版本，
        # 分数从 0.93 塌到 0.53 —— 这就是"同一块 B 一会儿认得出、一会儿
        # 认不出"的根。
        # 兜底办法：墨量超过 45% 就说明它判歪了（一个字母不可能盖住半张标签），
        # 改用"最暗的 20%"当阈值。字母永远是最暗的那一坨，这个前提比
        # "画面里只有两类东西"可靠得多。
        if float((inv > otsu_thr).mean()) > 0.45:
            otsu_thr = float(np.percentile(inv, 80))
        cands.append(("otsu", ((inv > otsu_thr).astype(np.uint8)) * 255))
    except cv2.error:
        pass
    block = max(15, (min(tile_bgr.shape[:2]) // 3) | 1)   # 跟着标签尺寸走，别拍死 21
    cands.append(("adap", cv2.adaptiveThreshold(inv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                cv2.THRESH_BINARY, block, -8)))

    best = None
    best_name = ""
    otsu_ink = None
    rows: list[dict] = []
    for name, binary in cands:            # cands 里 otsu 在前
        m = _clean_edges(binary)
        crop = _ink_crop(m)
        if crop is None:
            rows.append({"name": name, "ink": None, "ar": None, "ok": False})
            continue
        ch, cw = crop.shape
        ink_ratio = float((crop > 0).sum()) / float(ch * cw)
        # 上下限挡住两种失败：下限是"只抓到一个点/一条线"，上限是"整张标签
        # 都被当成笔画"（那种情况 ink_ratio 接近 1）。上限不能卡太紧：用粗
        # 记号笔写的大字，笔画占比能到 0.74（实测），卡 0.62 会误杀。真正
        # 该挡的"白色积木顶面"由别处负责 —— 它没有笔画结构，二值化出来是
        # 一整块实心，靠 _clean_edges 后的孔洞和下面的检测层一起过滤。
        ar = cw / float(max(ch, 1))
        ok = 0.06 <= ink_ratio <= 0.78 and 0.12 <= ar <= 6.0
        rows.append({"name": name, "ink": round(ink_ratio, 3),
                     "ar": round(ar, 2), "ok": ok})
        if name == "otsu":
            otsu_ink = ink_ratio if ok else None
        if not ok:
            continue
        if best is None:
            # **Otsu 优先**，自适应只做补位。
            # 以前这里按 `min(ink,0.35) - |ar-1|*0.05` 打分挑"更像一个字"的，
            # 那个公式实际上在**奖励笔画多的那个**（0.35 以下越多越好），
            # 而过度分割出来的候选笔画永远更多 —— 于是同一块 B，框得准的时候
            # 选 Otsu（0.93 分），框大一点点就翻成自适应（0.53 分）。
            # 两套二值化风格对应的模板根本不一样，分数当然跳。
            best, best_name = crop, name
        elif (best_name == "otsu" and otsu_ink is not None and otsu_ink < 0.12
              and ink_ratio > otsu_ink * 1.5):
            # 唯一的例外：Otsu 只抓到零星几个像素，多半是光照渐变把一侧的
            # 笔画漏了，这时才让自适应接管。
            best, best_name = crop, name
    if best is None:
        return (None, rows) if keep_rows else None

    out = _fit_canvas(best, size)
    return (out, rows, best_name) if keep_rows else out


def _fit_canvas(crop: np.ndarray, size: int = TILE) -> np.ndarray:
    """把笔画裁片等比塞进 size×size 画布，居中，留白。

    只按最长边等比缩放，不拉成正方形 —— 字形长宽比本身是识别信息
    （A 扁宽、B 近方），拉正了就丢掉一半线索。
    """
    ch, cw = crop.shape
    scale = size / float(max(ch, cw))
    nw, nh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
    resized = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.zeros((size, size), np.uint8)
    out[(size - nh) // 2:(size - nh) // 2 + nh,
        (size - nw) // 2:(size - nw) // 2 + nw] = resized
    return out


def _trim_border(tile_bgr: np.ndarray, px: int = CANON // 24) -> np.ndarray:
    """把标签四周那一圈裁掉（标签边界、贴纸白边、透视框带进来的背景）。

    必须在二值化**之前**做。放到之后做的话，Otsu 已经先被那圈亮边带偏了：
    它会把"标签 vs 亮边"当主要分界，判出"整张都是墨"，这个候选就直接废掉，
    只剩自适应阈值一个残缺候选 —— 分数就塌了。

    裁多少是个**悬崖**，不能随手取比例。实测（A 的 192×84 长条标签）：
      · 裁 0-4px：Otsu 仍被亮边污染，判出整片实心（笔画占比 0.11-0.15）
      · 裁 6px 起：稳定在 806px 笔画 / 占比 0.34
    按短边取比例正好落在 5px 这个悬崖边上，画面稍微一动就翻。所以按 CANON
    取固定像素（192//24 = 8），越过悬崖；这条边框在归一化后本来就是固定
    几个像素宽，和标签长宽比无关。
    """
    h, w = tile_bgr.shape[:2]
    m = max(2, min(int(px), min(h, w) // 6))
    if 2 * m >= min(h, w):
        return tile_bgr
    return tile_bgr[m:h - m, m:w - m]


def _clean_edges(binary: np.ndarray) -> np.ndarray:
    """补断口、去噪点。边框已经在 _trim_border 里处理掉了，这里不再裁。"""
    b = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def _ink_crop(binary: np.ndarray) -> np.ndarray | None:
    ys, xs = np.nonzero(binary)
    if len(xs) < 20:
        return None
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    crop = binary[y0:y1, x0:x1]
    if crop.shape[0] < 6 or crop.shape[1] < 4:
        return None
    return crop


def ink_mask(tile_bgr: np.ndarray) -> np.ndarray | None:
    """兼容旧名字。"""
    return character_mask(tile_bgr)


def similarity(a: np.ndarray, b: np.ndarray, shift: int = 2,
               dilate: int = 3) -> float:
    """两个等尺寸笔画 mask 的相似度 0-1。

    允许上下左右各 shift 像素的平移（登记和识别时手的位置不可能完全一致），
    取最好的那一档。

    两边先各膨胀 dilate 像素再算 Jaccard。实测必要性：同一块 B 的原图和
    手机链路（裁切 + JPEG q82）提出来，笔画数从 610 变成 737 —— 压缩让
    笔画变粗，纯 Jaccard 只有 0.546，刚好卡在阈值下面认不出。膨胀 3px 后
    升到 0.650，而 A 与 B 之间的混淆只从 0.342 升到 0.450，仍在 0.55 之下。
    再放大到 5px 混淆就 0.546 了，太危险。所以取 3。
    """
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    ab = a > 0
    bb = b > 0
    if dilate > 0:
        k = np.ones((dilate, dilate), np.uint8)
        ab = cv2.dilate(ab.astype(np.uint8), k) > 0
        bb = cv2.dilate(bb.astype(np.uint8), k) > 0
    best = 0.0
    for dy in range(-shift, shift + 1):
        for dx in range(-shift, shift + 1):
            sb = np.roll(np.roll(bb, dy, axis=0), dx, axis=1)
            union = int((ab | sb).sum())
            if union == 0:
                continue
            best = max(best, int((ab & sb).sum()) / union)
    return best


ANGLE_STEP = 15  # 连续角度搜索的步长，360/15 = 24 档
# 笔画粗细相对字形大小的两档。试过加 1.15 这一档反大：自比不涨（0.817→0.817），
# 交叉混淆从 0.486 涨到 0.564 —— 放大让笔画糊在一起，反而更容易撞车。
SCALES = (0.85, 1.0)
# 平移搜索。0 就是不搜：模板和查询都是按笔画外接框居中归一化的，平移已经被
# 归一化掉了，再加上 dilate=3 自带约 1px 容差。实测 shift=1 自比只从 0.817
# 升到 0.830，代价是耗时从 4.4ms 涨到 11ms，不值。
SHIFT = 0


def _rotate(mask: np.ndarray, angle: float, size: int = TILE) -> np.ndarray:
    """绕笔画质心把字形转 angle 度，再重新归一化回 size×size。

    两个细节不能省：
      1. **先扩画布再转**。48×48 直接转 45°，四个角会被裁掉，A 的尖顶就没了。
         先贴到对角线长度的方画布中间（约 68×68），转完再裁。
      2. **转完按笔画外接框重新贴一次**。不重贴的话，转 45° 后笔画的外接框
         从 48×24 变成约 53×53，字形在画布里自动"变小"了，和没转的模板
         尺寸对不上，相似度会被尺寸差拖下去。
    """
    if abs(angle % 360.0) < 1e-6:
        return mask
    h, w = mask.shape
    diag = int(np.ceil(np.hypot(h, w))) + 2
    big = np.zeros((diag, diag), np.uint8)
    y0, x0 = (diag - h) // 2, (diag - w) // 2
    big[y0:y0 + h, x0:x0 + w] = mask
    M = cv2.getRotationMatrix2D((diag / 2.0, diag / 2.0), angle, 1.0)
    rot = cv2.warpAffine(big, M, (diag, diag), flags=cv2.INTER_NEAREST)
    crop = _ink_crop(rot)
    if crop is None:
        return mask
    return _fit_canvas(crop, size)


def _scaled(mask: np.ndarray, s: float) -> np.ndarray:
    if abs(s - 1.0) < 1e-6:
        return mask
    h, w = mask.shape
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), 0.0, s)
    return cv2.warpAffine(mask, M, (w, h), flags=cv2.INTER_NEAREST)


def _variants(mask: np.ndarray, rotations: bool = True,
              scales: tuple[float, ...] = SCALES,
              step: int = ANGLE_STEP) -> list[np.ndarray]:
    """排列组合出要比对的形态：先转，再缩。

    顺序不能颠倒。`_rotate` 转完会按笔画外接框重贴（把尺寸归一化掉），
    所以如果先缩放再旋转，缩放会被重贴直接抹平，等于白缩。
    先转后缩，缩放才以"笔画在画布里的留白多少"这个形式留下来。

    为什么要连续角度：积木靠手摆，歪 15°、30° 很常见。只试 0/90/180/270
    的话，歪 20° 的 A 和四个正交朝向都对不齐，分数会掉到阈值下面。

    为什么要多尺度：`_rotate` 的重贴会把"字写小一点"归一化掉，但笔画粗细
    相对字形大小的比例变不了，同一块 B 换条链路（裁切 + JPEG）笔画从
    610 涨到 737 就是这个效应。多试几个缩放能吃掉这部分差异。
    """
    out: list[np.ndarray] = []
    angles = range(0, 360, max(1, int(step))) if rotations else (0,)
    for a in angles:
        base = mask if a == 0 else _rotate(mask, float(a))
        for s in scales:
            out.append(base if abs(s - 1.0) < 1e-6 else _scaled(base, s))
    return out


def _bin(mask: np.ndarray, dilate: int = 3) -> np.ndarray:
    """转 bool 并膨胀。膨胀的用处见 similarity 的注释。"""
    m = mask > 0
    if dilate > 0:
        m = cv2.dilate(m.astype(np.uint8), np.ones((dilate, dilate), np.uint8)) > 0
    return m


def _flat(mask: np.ndarray, dilate: int = 3) -> np.ndarray:
    return _bin(mask, dilate).reshape(-1)


def _shift_stack(mask: np.ndarray, shift: int = 2, dilate: int = 3) -> np.ndarray:
    """一个 mask 的全部平移版本，摊平成 (n_shift, D) 的 float32 矩阵。

    把平移摊成矩阵行是为了后面一次性矩阵乘 —— 逐对比 similarity 时，
    72 个查询形态 × 2 个模板 × 25 个平移 = 3600 次 Jaccard，每次都要
    np.roll 两次，光解释器开销就上百毫秒。矩阵乘把这一坨压成一次 sgemm。
    """
    base = _bin(mask, dilate)
    rows = [np.roll(np.roll(base, dy, 0), dx, 1).reshape(-1)
            for dy in range(-shift, shift + 1)
            for dx in range(-shift, shift + 1)]
    return np.stack(rows).astype(np.float32)


@dataclass
class LetterBank:
    """字母模板库。现场登记，存盘后可复用。"""
    tiles: dict[str, list[np.ndarray]] = field(default_factory=dict)
    path: Path | None = None
    # 预处理的模板矩阵 (D, n)，登记/载入变化时重建
    _cache: tuple | None = field(default=None, repr=False, compare=False)
    last_angle: float = field(default=0.0, repr=False, compare=False)
    # 最近一次 recognize 的按字母排名 [(字母, 分数), ...]，降序
    last_ranking: list = field(default_factory=list, repr=False, compare=False)

    def enroll(self, letter: str, mask: np.ndarray) -> None:
        letter = letter.strip().upper()
        if not letter or mask is None:
            return
        # 同一个字母登记多次是好事：手写两次不可能一样，多存一份让识别更宽容
        self.tiles.setdefault(letter, []).append(mask.copy())
        self._cache = None

    def _prepare(self, dilate: int) -> tuple[list[str], np.ndarray]:
        """把所有模板膨胀+摊平成 (D, n) 矩阵，按字母顺序记下每列属于谁。"""
        key = (dilate, tuple((k, len(v)) for k, v in sorted(self.tiles.items())))
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1], self._cache[2]
        letters: list[str] = []
        cols: list[np.ndarray] = []
        for letter in self.letters():
            for t in self.tiles[letter]:
                # 统一成 float32。_flat 出来是 bool，混着喂 matmul 没必要。
                cols.append(_flat(t, dilate).astype(np.float32))
                letters.append(letter)
        mat = np.stack(cols, axis=1) if cols else np.zeros((TILE * TILE, 0), np.float32)
        self._cache = (key, letters, mat)
        return letters, mat

    def recognize(self, mask: np.ndarray, rotations: bool = True,
                  step: int = ANGLE_STEP, shift: int = SHIFT,
                  dilate: int = 3, scales: tuple[float, ...] = SCALES
                  ) -> tuple[str, float]:
        """返回 (字母, 分数)。库里空的话返回 ("", 0.0)。

        积木靠手摆，横放竖放歪着放都正常，字会跟着转 —— 实测那张样本里
        A 就是顺着长边横着写的，而且整个画面是斜着拍的。所以比对时把查询
        字形绕质心转一圈（每 step 度一档），取最像的那一档。
        只试 0/90/180/270 是不够的：歪 20° 时四个正交朝向全都对不齐
        （实测自比分数 0.45-0.61，15° 步进能到 0.83-0.92）。
        """
        if mask is None or not self.tiles:
            return "", 0.0
        self.last_angle = 0.0
        variants = _variants(mask, rotations, step=step, scales=scales)
        letters, mat = self._prepare(dilate)
        if mat.size == 0:
            return "", 0.0
        tsum = mat.sum(axis=0)                      # (n,)
        best_col = np.zeros(mat.shape[1], np.float32)
        best_var = np.zeros(mat.shape[1], np.int32)
        n_shift = (2 * shift + 1) ** 2

        # 分批算，别一次摊开 72×25 行（48×48 就是 16MB，每帧分配不值）
        batch = 8
        for i in range(0, len(variants), batch):
            chunk = variants[i:i + batch]
            q = np.concatenate([_shift_stack(v, shift, dilate) for v in chunk], 0)
            qsum = q.sum(axis=1, keepdims=True)      # (m,1)
            # errstate 不是掩盖 bug：这台机器上 numpy 链的 BLAS 在做 float32 GEMM
            # 时会留下除零/溢出标志位，numpy 回头一查就把它们报成
            # "divide by zero encountered in matmul"（每帧一屏红字，结果却是对的）。
            # 真正的除零在下面 union 的 maximum(..., 1.0) 里已经兜住了。
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                inter = q @ mat                      # (m,n)
            union = np.maximum(qsum + tsum[None, :] - inter, 1.0)
            # 平移那一维先取最大，剩下 (本批形态数, 模板数)
            sc = (inter / union).reshape(len(chunk), n_shift, -1).max(axis=1)
            col_max = sc.max(axis=0)
            upd = col_max > best_col
            if upd.any():
                best_col[upd] = col_max[upd]
                best_var[upd] = i + sc.argmax(axis=0)[upd]

        # 同一个字母登记了多个样本时，取它最像的那个（不是平均）
        best_letter, best_score, best_k = "", 0.0, 0
        for k, letter in enumerate(letters):
            if best_col[k] > best_score:
                best_letter, best_score, best_k = letter, float(best_col[k]), k
        # 按字母汇总后排序，留给调用方做"第一名比第二名高多少"的判断。
        # 真实字母是"一眼就能看出是 A"：第一名 0.9、第二名 0.5。而桌面上的
        # 花纹、椅子边、人脸局部这些假目标，跟哪个模板都不像也不都不像 ——
        # 第一名 0.62、第二名 0.60，差距很小。只看绝对分数分不开这两种，
        # 看差距可以。
        per_letter: dict[str, float] = {}
        for k, letter in enumerate(letters):
            if best_col[k] > per_letter.get(letter, 0.0):
                per_letter[letter] = float(best_col[k])
        self.last_ranking = sorted(per_letter.items(), key=lambda kv: -kv[1])
        # 记下最像的那个朝向，页面上可以显示"这块是转到 45° 才认出来的"
        per_angle = max(1, len(scales))
        self.last_angle = float((int(best_var[best_k]) // per_angle) * step % 360)
        return best_letter, best_score

    def letters(self) -> list[str]:
        return sorted(self.tiles)

    def forget(self, letter: str) -> int:
        """删掉某个字母的全部模板，返回删掉几份。

        为什么必须有：`enroll` 是追加，登记错一次（比如画面里同时有 A、B，
        把 A 的笔画存成了 D）就**永久污染**了库 —— 之后那块真 D 永远认不出，
        还会被认成 A。现场没有删除手段的话，只能删文件重来，那就没法用了。
        """
        n = len(self.tiles.pop((letter or "").strip().upper(), []))
        if n:
            self._cache = None
        return n

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(p, **{k: np.array(v) for k, v in self.tiles.items()})
        p.with_suffix(".json").write_text(
            json.dumps({"letters": self.letters(),
                        "counts": {k: len(v) for k, v in self.tiles.items()}},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        self.path = p

    @classmethod
    def load(cls, path) -> "LetterBank":
        p = Path(path)
        bank = cls(path=p if p.exists() else None)
        if not p.exists():
            return bank
        with np.load(p) as z:
            for k in z.files:
                bank.tiles[k] = [np.asarray(t) for t in z[k]]
        return bank
