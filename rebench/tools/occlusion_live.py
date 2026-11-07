"""遮挡记忆演示 · 手机摄像头版（自主识别，无需人工点选）

    python -m rebench.tools.occlusion_live --port 8302
    手机打开 https://<电脑IP>:8302 → 高级 → 继续前往 → 允许摄像头

现场流程（全程不需要点屏幕）：
  1. 打开页面时桌面尽量空着，它用前 1 秒自动记下"空桌长什么样"
  2. 把积木放上去 → 自动识别成一个物体，**框住整块积木**，并记下三维位置
  3. 拿盒子挡在积木前面 → 积木看不见了 → 页面切「遮挡中」，位置不动，给出决策
  4. 把盒子拿开 → 目标重新出现 → 「记忆校验通过」

两条检测来源，都是自动的：
  · **颜色**：`vision.detector.Detector` 按 `vision/config/desktop.json` 的调色板
    找积木（红/绿…），ID 天然稳定（block_red_01），框取轮廓外接矩形 → 整块物体
  · **前景**：相对"空桌基线"做差分，凡是新出现的整块区域都算一个物体，
    任意颜色都行；用模板跟踪保持 ID（模板就是整块物体，不是固定小方格）

判据用的是 `rebench/occlusion.py` 里真正的逻辑（WorldModel /
check_vertical_approach / pick_strategy），不是另写一套演示代码。

几何仍是**演示用假设值**（手机没有深度）：相机位姿固定，位置由像素反投影到
桌面平面得到。所以屏幕上的坐标只能用来演示"记忆没变"，不能当标定结果。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import ssl
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np

from ..occlusion import (Box3D, OCCLUDED, WorldModel,
                         check_vertical_approach, pick_strategy)

# 功能分支把视觉源码放在 vision-rescue/vision/，主分支则放在 vision/。
# 在首次 import 前选好路径：若先让顶层空目录变成 namespace package，再补 sys.path，
# Python 可能仍缓存错误的包路径，随后继续报“没有 vision.detector”。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VISION_ROOT = _REPO_ROOT / "vision"
if not (_VISION_ROOT / "detector.py").is_file():
    _VISION_ROOT = _REPO_ROOT / "vision-rescue" / "vision"
    sys.path.insert(0, str(_VISION_ROOT.parent))
from vision.detector import Detector
from vision.tools.phone_live import _lan_ips, ensure_cert, fit_photo

MAX_UPLOAD = 8 * 1024 * 1024
JPEG_Q = 68

# ---------------------------------------------------------------- 演示用几何
CAM_POS = np.array([0.30, 0.0, 1.05])
CAM_LOOK = np.array([0.72, 0.0, 0.71])
TABLE_Z = 0.71
K = dict(fx=657.4, fy=656.9, cx=636.4, cy=356.1)
OBJ_WIDTH = 0.040
OBJ_HALF_H = 0.020

# ---------------------------------------------------------------- 检测参数
BASELINE_FRAMES = 12       # 用前 12 帧（约 1 秒）学空桌基线
FG_DIFF = 26               # 与空桌的灰度差超过这个算"有东西"
FG_MIN_AREA = 2500         # 小于这个面积的斑点当噪声
TPL_MAX_SIDE = 160         # 模板最长边（整块物体的裁剪）
MATCH_MIN = 0.50           # 模板匹配低于这个算"看不见了"
SEARCH_PAD_RATIO = 0.6     # 在上一位置多大范围里找
PATCH_DIFF_OCCLUDED = 12.0 # 区域灰度变化超过这个 → 被别的东西占了
PATCH_DIFF_EMPTY = 6.0     # 低于这个 → 变回空桌面，东西真被拿走了
BOX_DEPTH_EST = 0.05       # 障碍物进深按薄板估（单目看不出厚度）


# ---------------------------------------------------------------- 几何
def _basis(pos: np.ndarray, look: np.ndarray) -> np.ndarray:
    f = look - pos
    f = f / np.linalg.norm(f)
    up = np.array([0.0, 0.0, 1.0])
    r = np.cross(f, up)
    r = r / np.linalg.norm(r)
    d = np.cross(f, r)
    return np.stack([r, d, f], axis=1)


R_CAM = _basis(CAM_POS, CAM_LOOK)


def pixel_to_table(u: float, v: float) -> tuple[float, float] | None:
    d_cam = np.array([(u - K["cx"]) / K["fx"], (v - K["cy"]) / K["fy"], 1.0])
    d_world = R_CAM @ (d_cam / np.linalg.norm(d_cam))
    if abs(d_world[2]) < 1e-6:
        return None
    t = (TABLE_Z - CAM_POS[2]) / d_world[2]
    if t <= 0:
        return None
    p = CAM_POS + t * d_world
    return float(p[0]), float(p[1])


def bbox_pose_table(bbox) -> tuple[float, float, float] | None:
    """物体框 → 落点三维位置。用底边中点：从上方垂直下爪，关心它坐在哪一点。"""
    x, y, w, h = bbox
    hit = pixel_to_table(x + w / 2.0, y + h)
    if hit is None:
        return None
    return (hit[0], hit[1], TABLE_Z + OBJ_HALF_H)


def bbox_to_box(bbox, height_m: float = 0.18) -> Box3D | None:
    """障碍物框 → 桌面上的盒子。

    只用**底边**（接触线）：单目图像里"挡在前面"和"压在正上方"投影几乎一样，
    用整块 bbox 反投影一定盖住目标，走廊永远判"被占"，系统就变成什么都不敢抓。
    而物体与桌面的接触线才是它真正的落脚位置，这两种情况也只有它能分开。
    """
    x, y, w, h = bbox
    n1 = pixel_to_table(x + 0.10 * w, y + h)
    n2 = pixel_to_table(x + 0.90 * w, y + h)
    if n1 is None or n2 is None:
        return None
    xs = [n1[0], n2[0]]
    ys = [n1[1], n2[1]]
    return Box3D(min(xs), max(xs) + BOX_DEPTH_EST,
                 min(ys) - 0.02, max(ys) + 0.02,
                 TABLE_Z, TABLE_Z + height_m)


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _covers(outer, inner, ratio: float = 0.5) -> bool:
    """inner 有多大比例落在 outer 里。用来判断"这块区域是不是把某个目标盖住了"。"""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    x0, y0 = max(ox, ix), max(oy, iy)
    x1, y1 = min(ox + ow, ix + iw), min(oy + oh, iy + ih)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter >= ratio * float(max(iw * ih, 1))


def _patch(gray: np.ndarray, bbox) -> np.ndarray:
    x, y, w, h = bbox
    hh, ww = gray.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(ww, x + w), min(hh, y + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return np.zeros((16, 16), np.uint8)
    return cv2.resize(gray[y0:y1, x0:x1], (16, 16), interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------- 物体
class Obj:
    """一个被跟踪的物体：**整块外接框** + 外观模板。"""

    def __init__(self, oid: str, frame: np.ndarray, bbox, source: str) -> None:
        self.id = oid
        self.bbox = tuple(int(v) for v in bbox)
        self.source = source            # "color" | "fg"
        self.tpl: np.ndarray | None = None
        self.tpl_size = (0, 0)
        self.set_template(frame)

    def set_template(self, frame: np.ndarray) -> None:
        """模板 = 整个物体框里的灰度图（按最长边缩放，保留长宽比）。"""
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x, y, w, h = self.bbox
        hh, ww = g.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(ww, x + w), min(hh, y + h)
        if x1 - x0 < 12 or y1 - y0 < 12:
            self.tpl = None
            return
        crop = g[y0:y1, x0:x1]
        s = TPL_MAX_SIDE / float(max(crop.shape[:2]))
        if s < 1.0:
            crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        self.tpl = crop.copy()
        self.tpl_size = (x1 - x0, y1 - y0)

    def locate(self, frame: np.ndarray) -> tuple[tuple | None, float]:
        """在上一位置附近找这块物体。返回 (bbox, 相关系数)。"""
        if self.tpl is None:
            return None, 0.0
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x, y, w, h = self.bbox
        pad_x, pad_y = int(w * SEARCH_PAD_RATIO), int(h * SEARCH_PAD_RATIO)
        sx0, sy0 = max(0, x - pad_x), max(0, y - pad_y)
        sx1, sy1 = min(g.shape[1], x + w + pad_x), min(g.shape[0], y + h + pad_y)
        win = g[sy0:sy1, sx0:sx1]
        if (win.shape[0] < self.tpl.shape[0] + 1
                or win.shape[1] < self.tpl.shape[1] + 1):
            return None, 0.0
        res = cv2.matchTemplate(win, self.tpl, cv2.TM_CCOEFF_NORMED)
        _, best, _, loc = cv2.minMaxLoc(res)
        if best < MATCH_MIN:
            return None, float(best)
        tw, th = self.tpl_size
        return (sx0 + loc[0], sy0 + loc[1], tw, th), float(best)


class OcclusionSession:
    def __init__(self, config: str | None = None,
                 resolution=(1280, 720), fresh_s: float = 30.0,
                 use_color: bool = True) -> None:
        self.res = tuple(resolution)
        cfg_path = (Path(config) if config is not None
                    else _VISION_ROOT / "config" / "desktop.json")
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.detector = None
        if use_color and self.cfg.get("colors"):
            c = dict(self.cfg)
            c["label"] = {"enabled": False}      # 颜色检测，不走标签路径
            try:
                self.detector = Detector(c)
            except Exception as exc:             # noqa: BLE001
                print(f"[OCC] 颜色检测不可用：{exc}", flush=True)

        self.world = WorldModel(camera_pose=tuple(CAM_POS), table_z=TABLE_Z,
                                fresh_s=fresh_s)
        self.lock = threading.Lock()
        self.objs: dict[str, Obj] = {}
        self.last_jpeg: bytes | None = None
        self.last_clean: np.ndarray | None = None
        self.last_patches: dict[str, np.ndarray] = {}
        self.last_bbox: dict[str, tuple] = {}
        self.clean_gray: dict[str, np.ndarray] = {}
        self.events: deque = deque(maxlen=8)
        self.frames = 0
        self._next_id = 1
        # 空桌基线：前 BASELINE_FRAMES 帧的逐像素中值
        self._base_buf: list[np.ndarray] = []
        self.baseline: np.ndarray | None = None

    # ------------------------------------------------------------ 事件
    def _ev(self, text: str) -> None:
        self.events.appendleft({"t": self.frames, "text": text})

    def reset(self) -> dict:
        """清空所有物体并重新学习空桌。"""
        with self.lock:
            self.objs.clear()
            self.world.objects.clear()
            self.world.set_obstacles([])
            self.last_patches.clear()
            self.last_bbox.clear()
            self.clean_gray.clear()
            self.events.clear()
            self._base_buf.clear()
            self.baseline = None
            self._next_id = 1
            return {"ok": True, "note": "已清空，重新学习空桌"}

    # ------------------------------------------------------------ 检测
    def _foreground(self, gray: np.ndarray) -> list[tuple]:
        """与空桌基线差分，返回新出现的整块区域（任意颜色都能找到）。"""
        if self.baseline is None:
            return []
        d = cv2.absdiff(gray, self.baseline)
        _, m = cv2.threshold(d, FG_DIFF, 255, cv2.THRESH_BINARY)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        # 大核闭运算：把一块积木上的高光/阴影/贴纸碎片连成一个整体。
        # 不做这一步，一个物体会被拆成好几块小框 —— 这正是"框没框住整个物体"的根源。
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        h, w = gray.shape[:2]
        for c in cnts:
            area = float(cv2.contourArea(c))
            if area < FG_MIN_AREA or area > 0.35 * h * w:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            if not 0.25 <= bw / float(max(bh, 1)) <= 4.0:
                continue
            out.append((x, y, bw, bh))
        return out

    def _color_boxes(self, img: np.ndarray) -> list[tuple[str, tuple]]:
        """按调色板找积木（有配置才用）。返回 [(稳定ID, 整块外接框)]。"""
        if self.detector is None:
            return []
        try:
            dets, _ = self.detector.detect(img)
        except Exception:                        # noqa: BLE001
            return []
        return [(d.object_id, tuple(int(v) for v in d.bbox))
                for d in dets if not d.is_unknown]

    def _update_objects(self, img: np.ndarray, gray: np.ndarray) -> dict[str, tuple]:
        """把这一帧的检测并进物体表，返回 {id: 当前框}（只列看得见的）。"""
        seen: dict[str, tuple] = {}

        # ① 颜色物体：ID 稳定，每帧直接更新框
        for oid, bbox in self._color_boxes(img):
            o = self.objs.get(oid)
            if o is None:
                self.objs[oid] = Obj(oid, img, bbox, "color")
                self._ev(f"识别到 {oid}（颜色）")
            else:
                o.bbox = bbox
                o.set_template(img)
            seen[oid] = bbox

        # ② 已有的前景物体：靠模板跟住
        for oid, o in list(self.objs.items()):
            if oid in seen or o.source != "fg":
                continue
            bbox, _score = o.locate(img)
            if bbox is not None:
                o.bbox = bbox
                seen[oid] = bbox

        # ③ 新出现的前景区域 → 建新物体（框住整块）
        for bbox in self._foreground(gray):
            if any(_iou(bbox, b) > 0.25 for b in seen.values()):
                continue
            # **盖住已有目标的区域是障碍物，不是新物体。**
            # 不做这个区分的话，盒子挡住积木时那一大块前景会被当成"新出现的
            # 物体"建一条记忆；盒子拿走后它又匹配到别处，留下一个幽灵对象，
            # 页面上会一直挂着一个不存在的东西。
            if any(_iou(bbox, o.bbox) > 0.25 or _covers(bbox, o.bbox, 0.5)
                   or _covers(bbox, self.last_bbox.get(oid, o.bbox), 0.5)
                   for oid, o in list(self.objs.items())):
                continue
            oid = f"obj_{self._next_id}"
            self._next_id += 1
            self.objs[oid] = Obj(oid, img, bbox, "fg")
            seen[oid] = bbox
            self._ev(f"识别到 {oid}（新出现）")
        return seen

    # ------------------------------------------------------------ 主流程
    def frame(self, jpeg: bytes) -> dict:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("这一帧不是可解码的图片")
        img = fit_photo(img, self.res)

        with self.lock:
            self.frames += 1
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            self.last_clean = img

            # 学空桌基线：前 1 秒取中值，之后固定不动
            if self.baseline is None:
                self._base_buf.append(gray)
                if len(self._base_buf) >= BASELINE_FRAMES:
                    self.baseline = np.median(np.stack(self._base_buf), axis=0
                                              ).astype(np.uint8)
                    self._base_buf.clear()
                    self._ev("空桌基线已就绪")

            seen = self._update_objects(img, gray)

            # ① 看见的 → 刷新记忆（唯一会改位置的地方）
            for oid, bbox in seen.items():
                pose = bbox_pose_table(bbox)
                if pose is None:
                    continue
                was = self.world.objects.get(oid)
                if was is not None and was.state == OCCLUDED:
                    self._ev(f"{oid} 重新出现，记忆校验：位置一致")
                self.world.see(oid, pose)
                self.last_patches[oid] = _patch(gray, bbox)
                self.last_bbox[oid] = bbox
                self.clean_gray[oid] = cv2.resize(gray, (640, 360),
                                                  interpolation=cv2.INTER_AREA)
                if was is None:
                    self._ev(f"记住 {oid} @ ({pose[0]:.2f}, {pose[1]:.2f})")

            # ② 看不见的 → 判遮挡（记忆位置一律不动）
            obstacles: list[Box3D] = []
            occluded_now: dict[str, dict] = {}
            for oid in list(self.objs):
                if oid in seen or oid not in self.last_bbox:
                    continue
                bbox = self.last_bbox[oid]
                current_patch = _patch(gray, bbox).astype(np.int16)
                diff = float(np.mean(np.abs(
                    current_patch - self.last_patches[oid].astype(np.int16))))
                # “和目标上次的外观不同”不等于“被挡住”：积木被拿走后，空桌面
                # 与积木本身的差异同样很大。必须再对照空桌基线，否则会对空位盲抓。
                if self.baseline is not None:
                    empty_diff = float(np.mean(np.abs(
                        current_patch - _patch(self.baseline, bbox).astype(np.int16))))
                else:
                    empty_diff = float("inf")
                if empty_diff <= PATCH_DIFF_EMPTY:
                    reason = "empty"           # 区域已经恢复成空桌：目标真被拿走
                elif diff >= PATCH_DIFF_OCCLUDED:
                    reason = "occluded"        # 区域非空，且内容已不是原来的目标
                else:
                    reason = "unchanged"       # 检测漏帧但区域没变化，不能据此盲抓
                state = self.world.resolve(oid, slot_reason=reason)
                if state == OCCLUDED:
                    occluded_now[oid] = {"diff": round(diff, 1), "bbox": bbox}
                    occ = self._occluder_bbox(gray, oid, bbox)
                    if occ is not None:
                        box = bbox_to_box(occ)
                        if box is not None:
                            obstacles.append(box)

            self.world.set_obstacles(obstacles, camera_pose=tuple(CAM_POS))

            # ③ 决策：能不能用记忆位置盲抓
            decisions = {}
            for oid, o in self.world.objects.items():
                if o.state != OCCLUDED:
                    continue
                tgt = self.world.target(oid)
                feas = (check_vertical_approach(tgt["pose"], obstacles, OBJ_WIDTH)
                        if tgt and tgt.get("pose")
                        else {"ok": False, "reason": "no_pose",
                              "message": "没有可用位姿"})
                decisions[oid] = {"decision": pick_strategy(tgt, feas),
                                  "feasibility": feas, "target": tgt}

            ann = self._annotate(img, seen, obstacles)
            ok, buf = cv2.imencode(".jpg", ann, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q])
            self.last_jpeg = buf.tobytes() if ok else None
            return self._payload(seen, occluded_now, decisions)

    # ------------------------------------------------------------ 障碍物范围
    def _occluder_bbox(self, gray: np.ndarray, oid: str, bbox) -> tuple | None:
        """现在占住目标那块地方的**东西自己的**包围盒。

        拿目标自己的 bbox 当障碍物是错的 —— 它必然盖住目标，走廊永远判被占，
        系统就变成"什么都不敢抓"。这里用"上次看见时的整帧"做差分，
        变化最大的连通域才是挡上来的东西；它的**底边**才是接触线。
        """
        prev = self.clean_gray.get(oid)
        if prev is None:
            return None
        cur = cv2.resize(gray, (640, 360), interpolation=cv2.INTER_AREA)
        d = cv2.absdiff(cur, prev)
        _, m = cv2.threshold(d, 30, 255, cv2.THRESH_BINARY)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        n, _lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        x, y, w, h = bbox
        cx, cy = (x + w / 2.0) / 2.0, (y + h / 2.0) / 2.0
        best = None
        for i in range(1, n):
            sx, sy, sw, sh, area = stats[i]
            if area < 400:
                continue
            if not (sx <= cx <= sx + sw and sy <= cy <= sy + sh):
                continue
            if best is None or area > best[4]:
                best = (sx, sy, sw, sh, area)
        if best is None:
            return None
        sx, sy, sw, sh, _ = best
        return (sx * 2, sy * 2, sw * 2, sh * 2)

    # ------------------------------------------------------------ 输出
    def _payload(self, seen, occluded_now, decisions) -> dict:
        objs = []
        for oid, o in self.world.objects.items():
            tgt = self.world.target(oid)
            src = self.objs.get(oid).source if oid in self.objs else "?"
            objs.append({
                "id": oid,
                "state": o.state,
                "visible": oid in seen,
                "source": src,
                "pose": [round(v, 3) for v in o.pose] if o.pose else None,
                "age_s": None if tgt is None else tgt["age_s"],
                "uncertainty_mm": (None if not tgt or "uncertainty_m" not in tgt
                                   else round(tgt["uncertainty_m"] * 1000)),
                "bbox": list(self.last_bbox.get(oid) or []),
                "diff": occluded_now.get(oid, {}).get("diff"),
            })
        return {"ok": True,
                "image": (base64.b64encode(self.last_jpeg).decode()
                          if self.last_jpeg else None),
                "objects": objs,
                "decisions": {oid: {"action": d["decision"]["action"],
                                    "message": d["decision"]["message"],
                                    "reason": d["decision"].get("reason"),
                                    "corridor": d["feasibility"].get("reason"),
                                    "yaw": d["feasibility"].get("gripper_yaw_deg"),
                                    "obstacle": d["feasibility"].get("obstacle")}
                              for oid, d in decisions.items()},
                "obstacles": len(self.world.obstacles),
                "baseline_ready": self.baseline is not None,
                "baseline_seen": len(self._base_buf),
                "events": list(self.events),
                "frames": self.frames}

    # ------------------------------------------------------------ 标注
    def _annotate(self, img, seen, obstacles) -> np.ndarray:
        out = img.copy()
        for oid, bbox in seen.items():
            x, y, w, h = bbox
            cv2.rectangle(out, (x, y), (x + w, y + h), (0, 210, 90), 2)
            cv2.putText(out, oid, (x, max(18, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 210, 90), 2, cv2.LINE_AA)
        # 记忆中的目标：黄色虚线框（我记得它在这儿）
        for oid, o in self.world.objects.items():
            if oid in seen or oid not in self.last_bbox:
                continue
            x, y, w, h = self.last_bbox[oid]
            for seg in range(0, w, 14):
                cv2.line(out, (x + seg, y), (x + min(seg + 7, w), y), (0, 190, 255), 2)
                cv2.line(out, (x + seg, y + h),
                         (x + min(seg + 7, w), y + h), (0, 190, 255), 2)
            for seg in range(0, h, 14):
                cv2.line(out, (x, y + seg), (x, y + min(seg + 7, h)), (0, 190, 255), 2)
                cv2.line(out, (x + w, y + seg),
                         (x + w, y + min(seg + 7, h)), (0, 190, 255), 2)
            cv2.putText(out, f"{oid} MEMORY", (x, max(18, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 190, 255), 2, cv2.LINE_AA)

        cv2.putText(out, f"obstacles={len(obstacles)}  frames={self.frames}",
                    (12, out.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (230, 230, 230), 2, cv2.LINE_AA)
        return out


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>遮挡记忆演示</title>
<style>
:root{--bg:#0d0f13;--panel:#171a20;--line:#2a2f38;--fg:#e9ebef;--dim:#8b929e;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     padding:10px 10px 20px;min-height:100vh}
.hd{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.hd h1{font-size:16px;font-weight:650;flex:1}
.pill{font-size:11.5px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.view{position:relative;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#000}
.view img{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#000}
.view video{position:absolute;width:1px;height:1px;opacity:0}
.hud{position:absolute;left:8px;top:8px;font:11px/1.5 ui-monospace,Consolas,monospace;
     color:#cfd6e2;background:#000a;padding:4px 7px;border-radius:5px}
.msg{margin-top:8px;font-size:13px;color:var(--dim);min-height:20px}
.msg.bad{color:var(--bad)}.msg.ok{color:var(--ok)}
.card{margin-top:10px;border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:12px 14px}
.card h3{font-size:14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center}
.st{font-size:12px;padding:2px 9px;border-radius:20px;font-weight:700}
.st.visible{background:#12351f;color:var(--ok)}
.st.occluded{background:#3a2a08;color:var(--warn)}
.st.missing{background:#3a1416;color:var(--bad)}
.row{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;color:var(--dim)}
.row b{color:var(--fg);font-family:ui-monospace,Consolas,monospace;font-weight:600}
.ev{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.ev span{font-size:11.5px;padding:3px 8px;border-radius:6px;border:1px solid var(--line)}
.ev span.on{background:#12351f;color:var(--ok);border-color:#2a5c40}
.ev span.off{color:#6b7280}
.dec{margin-top:10px;padding:9px 11px;border-radius:9px;font-size:13px;line-height:1.5}
.dec.blind_pick{background:#12351f;border:1px solid #2f7a4a;color:#c9ffdd}
.dec.refuse{background:#3a1416;border:1px solid #6b2426;color:#ffb4b4}
.dec.reobserve{background:#3a2a08;border:1px solid #6b5320;color:#ffe2b0}
.bar{display:flex;gap:8px;margin-top:12px}
button{flex:1;padding:14px 10px;border-radius:11px;border:1px solid #33507a;background:#1b2536;
       color:var(--fg);font-size:15px;font-weight:600}
.note{margin-top:10px;font-size:11.5px;color:#6b7280;line-height:1.6}
</style></head><body>

<div class="hd"><h1>遮挡记忆 · 自主识别</h1><span class="pill" id="st">连接中</span></div>

<div class="view">
  <video id="v" playsinline muted autoplay></video>
  <img id="shot" alt="实时">
  <div class="hud" id="hud">—</div>
</div>

<div class="msg" id="msg">正在请求摄像头权限…</div>
<div id="cards"></div>
<div class="bar"><button id="clr">重新学习空桌</button></div>
<div class="note">全程不需要点屏幕：开页面时桌面尽量空着，它用前 1 秒记住空桌，
之后放上去的东西会自动被识别。<br>
几何说明：手机没有深度，位置由像素反投影到桌面平面得到，相机位姿为演示用的
固定假设值（≈ 机器人腕部俯视桌面），不是标定结果。</div>

<script>
const v=document.getElementById('v'), shot=document.getElementById('shot');
const hud=document.getElementById('hud'), msg=document.getElementById('msg');
const st=document.getElementById('st'), cards=document.getElementById('cards');
const cv=document.createElement('canvas'); cv.width=1280; cv.height=720;
const cx=cv.getContext('2d');
let busy=false;

function drawCover(){
  const sw=v.videoWidth, sh=v.videoHeight; if(!sw||!sh) return false;
  const tr=cv.width/cv.height, sr=sw/sh; let cw,ch;
  if(sr>tr){ ch=sh; cw=sh*tr; } else { cw=sw; ch=sw/tr; }
  cx.drawImage(v,(sw-cw)/2,(sh-ch)/2,cw,ch,0,0,cv.width,cv.height);
  return true;
}
function setMsg(t,cls){ msg.textContent=t; msg.className='msg'+(cls?' '+cls:''); }

async function pump(){
  if(busy||v.readyState<2) return; busy=true;
  try{
    if(drawCover()){
      const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.82));
      const j=await (await fetch('/frame',{method:'POST',body:blob})).json();
      if(j.image) shot.src='data:image/jpeg;base64,'+j.image;
      render(j);
    }
  }catch(e){ setMsg('连接中断：'+e.message,'bad'); }
  finally{ busy=false; }
}

const EVID={visible:'看得见',occluded:'被判遮挡',missing:'按丢失处理'};
function render(j){
  st.textContent = !j.baseline_ready ? '正在记忆空桌'
                 : (!j.objects.length ? '等待目标'
                    : (j.objects.some(o=>o.state==='occluded')? '遮挡中':'跟踪中'));
  hud.textContent='objects='+j.objects.length+'  obstacles='+j.obstacles
                 +'  frames='+j.frames+(j.baseline_ready?'':'  base='+j.baseline_seen);

  cards.innerHTML = j.objects.map(o=>{
    const ev1=o.pose?1:0, ev2=(o.state==='occluded')?1:0, ev3=(o.age_s!==null&&o.age_s<300)?1:0;
    const dec=j.decisions[o.id];
    const decHtml = dec? `<div class="dec ${dec.action}"><b>${
        dec.action==='blind_pick'?'可以盲抓':dec.action==='refuse'?'拒绝抓取':'先重看一眼'
      }</b> · ${dec.message}</div>` : '';
    return `<div class="card">
      <h3>${o.id}<span class="st ${o.state}">${EVID[o.state]||o.state}</span></h3>
      <div class="row"><span>来源</span><b>${o.source==='color'?'颜色检测':'新出现的前景'}</b></div>
      <div class="row"><span>记忆位置 (base_link)</span><b>${
        o.pose? o.pose.map(v=>v.toFixed(2)).join(', ') : '—'}</b></div>
      <div class="row"><span>这条记忆多久了</span><b>${o.age_s===null?'—':o.age_s.toFixed(0)+' s'}</b></div>
      <div class="row"><span>位置不确定度</span><b>${o.uncertainty_mm===null?'—':'±'+o.uncertainty_mm+' mm'}</b></div>
      <div class="row"><span>框 (x,y,w×h)</span><b>${o.bbox&&o.bbox.length? o.bbox.join(', ') : '—'}</b></div>
      <div class="ev">
        <span class="${ev1?'on':'off'}">① 有历史位置</span>
        <span class="${ev2?'on':'off'}">② 区域被占</span>
        <span class="${ev3?'on':'off'}">③ 记忆未过期</span>
      </div>
      ${decHtml}
    </div>`;
  }).join('') || `<div class="card"><div class="row"><span>${
      j.baseline_ready? '画面里还没有物体：把积木放上去，会自动识别'
                      : '正在学习空桌，请让桌面空着（约 1 秒）'}</span></div></div>`;

  const occ=j.objects.filter(o=>o.state==='occluded').length;
  if(!j.baseline_ready) setMsg('正在学习空桌，请让桌面空着…');
  else if(!j.objects.length) setMsg('把积木放上去，会自动识别（不用点屏幕）');
  else if(occ) setMsg('目标被挡住了 —— 记忆位置保持不动，正在用记忆位姿决策','ok');
  else setMsg('已识别 '+j.objects.length+' 个物体并记住位置（现在可以拿盒子挡上去）','ok');
}
async function loop(){ await pump(); setTimeout(loop, 250); }

document.getElementById('clr').onclick=async()=>{
  await fetch('/reset',{method:'POST'});
  setMsg('已清空，请让桌面空着，正在重新学习…','');
};

(async()=>{
  try{
    const stream=await navigator.mediaDevices.getUserMedia({
      video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}},audio:false});
    v.srcObject=stream; await v.play();
    setMsg('摄像头已就绪，正在学习空桌'); loop();
  }catch(e){
    setMsg('拿不到摄像头：'+e.name+' — '+(e.message||''),'bad');
    if(location.protocol!=='https:') setMsg('必须用 https 打开（当前 '+location.protocol+'）','bad');
  }
})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    session: OcclusionSession | None = None

    def log_message(self, fmt, *args):
        if str(args[1] if len(args) > 1 else "") not in ("200", "304"):
            print(f"[OCC] {self.client_address[0]} {fmt % args}", flush=True)

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_UPLOAD:
            raise ValueError(f"请求体大小异常（{n} 字节）")
        return self.rfile.read(n)

    def do_GET(self):                       # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/shot":
            if not self.session or not self.session.last_jpeg:
                self.send_error(404)
                return
            self._send(self.session.last_jpeg, "image/jpeg")
        else:
            self.send_error(404)

    def do_POST(self):                      # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/frame":
                self._json(self.session.frame(self._body()))
            elif path == "/reset":
                self._json(self.session.reset())
            else:
                self.send_error(404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="遮挡记忆演示（手机摄像头，自主识别）")
    p.add_argument("--port", type=int, default=8302)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--config", default=None,
                   help="视觉配置；默认自动选择仓库里的 vision/config/desktop.json")
    p.add_argument("--res", default="1280x720")
    p.add_argument("--fresh-s", type=float, default=30.0,
                   help="记忆新鲜期：这个时间内允许直接盲抓")
    p.add_argument("--no-color", action="store_true",
                   help="不用调色板，只靠空桌差分找新出现的物体")
    args = p.parse_args(argv)

    res = tuple(int(v) for v in args.res.lower().split("x"))
    ips = _lan_ips()
    cert, key = ensure_cert(ips)
    session = OcclusionSession(args.config, resolution=res,
                               fresh_s=args.fresh_s, use_color=not args.no_color)
    Handler.session = session
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print("=" * 62, flush=True)
    for ip in ips:
        tag = "  ← 手机连『移动热点』用这个" if ip.startswith("192.168.137.") else ""
        print(f"[OCC] 手机打开  https://{ip}:{args.port}{tag}", flush=True)
    print("[OCC] 首次会提示『连接不是私密连接』→ 高级 → 继续前往", flush=True)
    print("[OCC] 开页面时桌面尽量空着 → 放积木（自动识别）→ 拿盒子挡住 → 看决策",
          flush=True)
    print("=" * 62, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[OCC] 退出", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
