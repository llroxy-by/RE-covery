"""遮挡记忆演示 · 手机摄像头版（点选目标，不用标签）

    python -m rebench.tools.occlusion_live --port 8302
    手机打开 https://<电脑IP>:8302 → 高级 → 继续前往 → 允许摄像头

现场三步：
  1. **用手指点一下桌上的积木** → 页面锁定它，显示「已记忆 block_1 (x, y)」
  2. 拿一个盒子挡在手机与积木之间 → 积木看不见了
     → 页面切到「遮挡中」，记忆位置不动，并给出走廊判定与决策
  3. 把盒子拿开 → 目标重新出现 → 「记忆校验通过」

为什么不用标签/字母：这条链路要演的是"遮挡后还记不记得"，身份怎么来不重要；
现场随便拿一块积木就能演，不必先写字母、先登记。锁定用模板匹配，不依赖颜色
阈值，也不假设桌布颜色。

跑的是 `rebench/occlusion.py` 里真正的判据（WorldModel / check_vertical_approach
/ pick_strategy）。唯一"演"的地方是几何：手机没有深度，相机位姿是**固定假设值**
（大致等于机器人腕部俯视桌面），三维坐标由像素反投影到桌面平面得到。
页面上标了「演示几何」，不要拿这些数当标定结果。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import ssl
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2
import numpy as np

from ..occlusion import (Box3D, OCCLUDED, WorldModel,
                         check_vertical_approach, pick_strategy)
from vision.tools.phone_live import _lan_ips, ensure_cert, fit_photo

MAX_UPLOAD = 8 * 1024 * 1024
JPEG_Q = 68

# ---------------------------------------------------------------- 演示用几何
# 相机位姿是**假设值**：当作一台装在腕部、俯视桌面的 1280×720 相机。
# 内参取自任务书里 D405 的实测值（fx 657.4 / fy 656.9 / cx 636.4 / cy 356.1）。
CAM_POS = np.array([0.30, 0.0, 1.05])
CAM_LOOK = np.array([0.72, 0.0, 0.71])
TABLE_Z = 0.71
K = dict(fx=657.4, fy=656.9, cx=636.4, cy=356.1)
OBJ_WIDTH = 0.040
OBJ_HALF_H = 0.020

# 跟踪参数
TPL = 72                 # 锁定用的模板边长（像素）
MATCH_SCALE = 0.5        # 匹配时画面缩一半，快一倍
SEARCH_PAD = 150         # 每帧只在上一位置附近搜，避免匹配到别处
MIN_SCORE = 0.42         # 低于这个相关系数就算"看不见了"
PATCH_DIFF_OCCLUDED = 12.0   # 区域灰度变化超过这个 → 那里被东西占了
PATCH_DIFF_EMPTY = 6.0       # 低于这个 → 那里变回空桌面，东西真被拿走
# 障碍物进深按"薄板"估。单目看不出盒子有多厚：用宽度去推厚度是错的
# （宽盒子会被算成很深，于是永远判"挡手"）。要精确厚度只能用腕部深度相机，
# 这也是文档里写"真机必须用深度"的原因之一。
BOX_DEPTH_EST = 0.05


def _basis(pos: np.ndarray, look: np.ndarray) -> np.ndarray:
    """世界 → 相机 的旋转（相机轴：x 右、y 下、z 前）。"""
    f = look - pos
    f = f / np.linalg.norm(f)
    up = np.array([0.0, 0.0, 1.0])
    r = np.cross(f, up)
    r = r / np.linalg.norm(r)
    d = np.cross(f, r)
    return np.stack([r, d, f], axis=1)      # 列向量


R_CAM = _basis(CAM_POS, CAM_LOOK)


def pixel_to_table(u: float, v: float) -> tuple[float, float] | None:
    """像素 → 桌面平面上的 (x, y)（base_link 米）。没有交点就返回 None。"""
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
    """目标框 → 落点三维位置。

    用**底边中点**而不是框中心：从上方垂直下爪，关心的是"它坐在桌面的哪一点"，
    框中心会系统性偏几厘米。
    """
    x, y, w, h = bbox
    hit = pixel_to_table(x + w / 2.0, y + h)
    if hit is None:
        return None
    return (hit[0], hit[1], TABLE_Z + OBJ_HALF_H)


def bbox_to_box(bbox, height_m: float = 0.18) -> Box3D | None:
    """把障碍物框反投影成桌面上的盒子。

    **只用底边，不用整块 bbox。** 单目图像里"盒子挡在前面"和"盒子压在目标
    正上方"投影几乎一样，用整块 bbox 反投影一定盖住目标，走廊永远判"被占"，
    演示就变成"什么都不敢抓"。而物体与桌面的**接触线**（底边）才是它真正的
    落脚位置：挡在前面的盒子接触线在目标之前，压在正上方的盒子接触线就在
    目标自己的位置 —— 这两种情况的正确处置本来就相反，也只能靠这条线分开。
    """
    x, y, w, h = bbox
    n1 = pixel_to_table(x + 0.10 * w, y + h)
    n2 = pixel_to_table(x + 0.90 * w, y + h)
    if n1 is None or n2 is None:
        return None
    depth = BOX_DEPTH_EST
    xs = [n1[0], n2[0]]
    ys = [n1[1], n2[1]]
    return Box3D(min(xs), max(xs) + depth,
                 min(ys) - 0.02, max(ys) + 0.02,
                 TABLE_Z, TABLE_Z + height_m)


class Track:
    """点选锁定后的一路模板跟踪。"""

    def __init__(self, oid: str, frame: np.ndarray, u: float, v: float) -> None:
        self.id = oid
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        half = TPL // 2
        x0 = int(max(0, min(gray.shape[1] - TPL, round(u) - half)))
        y0 = int(max(0, min(gray.shape[0] - TPL, round(v) - half)))
        self.tpl = gray[y0:y0 + TPL, x0:x0 + TPL].copy()
        self.bbox = (x0, y0, TPL, TPL)
        self.score = 1.0

    def match(self, frame: np.ndarray) -> tuple[tuple[int, int, int, int], float]:
        """在上一位置附近找模板。返回 (bbox, 相关系数)。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x, y, w, h = self.bbox
        sx0 = max(0, x - SEARCH_PAD)
        sy0 = max(0, y - SEARCH_PAD)
        sx1 = min(gray.shape[1], x + w + SEARCH_PAD)
        sy1 = min(gray.shape[0], y + h + SEARCH_PAD)
        if sx1 - sx0 < TPL or sy1 - sy0 < TPL:
            return self.bbox, 0.0

        small = cv2.resize(gray[sy0:sy1, sx0:sx1], None, fx=MATCH_SCALE,
                           fy=MATCH_SCALE, interpolation=cv2.INTER_AREA)
        tpl = cv2.resize(self.tpl, None, fx=MATCH_SCALE, fy=MATCH_SCALE,
                         interpolation=cv2.INTER_AREA)
        if small.shape[0] < tpl.shape[0] or small.shape[1] < tpl.shape[1]:
            return self.bbox, 0.0
        res = cv2.matchTemplate(small, tpl, cv2.TM_CCOEFF_NORMED)
        _, best, _, loc = cv2.minMaxLoc(res)
        self.score = float(best)
        if best >= MIN_SCORE:
            self.bbox = (int(loc[0] / MATCH_SCALE) + sx0,
                         int(loc[1] / MATCH_SCALE) + sy0, w, h)
        return self.bbox, float(best)


class OcclusionSession:
    """一帧一帧地推：看见 → 记忆；看不见 → 判遮挡 → 决策。"""

    def __init__(self, resolution=(1280, 720), fresh_s: float = 30.0) -> None:
        self.res = tuple(resolution)
        self.world = WorldModel(camera_pose=tuple(CAM_POS), table_z=TABLE_Z,
                                fresh_s=fresh_s)
        self.lock = threading.Lock()
        self.tracks: dict[str, Track] = {}
        self.last_jpeg: bytes | None = None
        self.last_clean: np.ndarray | None = None
        self.last_patches: dict[str, np.ndarray] = {}
        self.last_bbox: dict[str, tuple] = {}
        # 上次看见这个目标时的整帧灰度图。遮挡发生时要靠它差分出
        # **障碍物自己的范围** —— 拿目标自己的 bbox 当障碍物是错的，
        # 那会永远盖住目标，于是永远判"走廊被占"。
        self.clean_gray: dict[str, np.ndarray] = {}
        self.events: deque = deque(maxlen=8)
        self.frames = 0
        self._next_id = 1

    # ------------------------------------------------------------ 交互
    def lock_on(self, u: float, v: float) -> dict:
        """用户点了画面上的某个点 → 在那里锁定一个目标。"""
        with self.lock:
            if self.last_clean is None:
                return {"ok": False, "error": "还没有画面"}
            h, w = self.last_clean.shape[:2]
            u = float(min(max(u, TPL), w - TPL))
            v = float(min(max(v, TPL), h - TPL))
            oid = f"block_{self._next_id}"
            self._next_id += 1
            self.tracks[oid] = Track(oid, self.last_clean, u, v)
            self._ev(f"锁定 {oid} @ 画面 ({u:.0f}, {v:.0f})")
            return {"ok": True, "id": oid}

    def reset(self) -> dict:
        with self.lock:
            self.tracks.clear()
            self.world.objects.clear()
            self.world.set_obstacles([])
            self.last_patches.clear()
            self.last_bbox.clear()
            self.clean_gray.clear()
            self.events.clear()
            self._next_id = 1
            return {"ok": True}

    def _ev(self, text: str) -> None:
        self.events.appendleft({"t": self.frames, "text": text})

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

            seen: dict[str, dict] = {}
            for oid, tr in self.tracks.items():
                bbox, score = tr.match(img)
                if score >= MIN_SCORE:
                    seen[oid] = {"bbox": bbox, "score": score}

            # ① 看见的 → 刷新记忆（这是唯一会改位置的地方）
            for oid, info in seen.items():
                pose = bbox_pose_table(info["bbox"])
                if pose is None:
                    continue
                was = self.world.objects.get(oid)
                if was is not None and was.state == OCCLUDED:
                    self._ev(f"{oid} 重新出现，记忆校验：位置一致")
                self.world.see(oid, pose)
                self.last_patches[oid] = _patch(gray, info["bbox"])
                self.last_bbox[oid] = info["bbox"]
                self.clean_gray[oid] = cv2.resize(gray, (640, 360),
                                                  interpolation=cv2.INTER_AREA)
                if was is None:
                    self._ev(f"记住 {oid} @ ({pose[0]:.2f}, {pose[1]:.2f})")

            # ② 看不见的 → 判遮挡（记忆位置一律不动）
            obstacles: list[Box3D] = []
            occluded_now: dict[str, dict] = {}
            for oid in list(self.tracks):
                if oid in seen or oid not in self.last_bbox:
                    continue
                bbox = self.last_bbox[oid]
                p_now = _patch(gray, bbox)
                p_old = self.last_patches[oid]
                diff = float(np.mean(np.abs(p_now.astype(np.int16)
                                           - p_old.astype(np.int16))))
                # 三证据里的第二条：这片区域被别的东西占了（像素变了）；
                # 低于阈值说明那里变回空桌面 —— 东西是真被拿走的，不是被挡。
                reason = "occluded" if diff >= PATCH_DIFF_OCCLUDED else "empty"
                state = self.world.resolve(oid, slot_reason=reason)
                if state == OCCLUDED:
                    occluded_now[oid] = {"diff": round(diff, 1), "bbox": bbox}
                    occ_bbox = self._occluder_bbox(gray, oid, bbox)
                    if occ_bbox is not None:
                        box = bbox_to_box(occ_bbox)
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
        变化最大的连通域才是那个挡上来的东西；它的**底边**才是接触线。

        找不到任何变化连通域 → 那里是空桌面（东西真被拿走了），返回 None。
        """
        prev = self.clean_gray.get(oid)
        if prev is None:
            return None
        cur = cv2.resize(gray, (640, 360), interpolation=cv2.INTER_AREA)
        d = cv2.absdiff(cur, prev)
        _, m = cv2.threshold(d, 30, 255, cv2.THRESH_BINARY)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        x, y, w, h = bbox
        cx, cy = (x + w / 2.0) / 2.0, (y + h / 2.0) / 2.0   # 半分辨率坐标
        best = None
        for i in range(1, n):
            sx, sy, sw, sh, area = stats[i]
            if area < 400:
                continue
            inside = sx <= cx <= sx + sw and sy <= cy <= sy + sh
            if not inside:
                continue
            # 取把目标盖住的最大的那块（障碍物常常和目标连成一片）
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
            objs.append({
                "id": oid,
                "state": o.state,
                "visible": oid in seen,
                "pose": [round(v, 3) for v in o.pose] if o.pose else None,
                "age_s": None if tgt is None else tgt["age_s"],
                "uncertainty_mm": (None if not tgt or "uncertainty_m" not in tgt
                                   else round(tgt["uncertainty_m"] * 1000)),
                "score": round(seen.get(oid, {}).get("score", 0.0), 2),
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
                "tracked": len(self.tracks),
                "events": list(self.events),
                "frames": self.frames}

    # ------------------------------------------------------------ 标注
    def _annotate(self, img, seen, obstacles) -> np.ndarray:
        out = img.copy()
        for oid, info in seen.items():
            x, y, w, h = info["bbox"]
            cv2.rectangle(out, (x, y), (x + w, y + h), (0, 210, 90), 2)
            cv2.putText(out, f"{oid} {info['score']:.2f}", (x, max(18, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 210, 90), 2, cv2.LINE_AA)
        # 记忆中的目标：黄色虚线框，表示"我记得它在这儿"
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


def _patch(gray: np.ndarray, bbox) -> np.ndarray:
    x, y, w, h = bbox
    hh, ww = gray.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(ww, x + w), min(hh, y + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return np.zeros((16, 16), np.uint8)
    return cv2.resize(gray[y0:y1, x0:x1], (16, 16), interpolation=cv2.INTER_AREA)


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
.tip{position:absolute;left:0;right:0;bottom:0;padding:7px 10px;font-size:12.5px;
     background:#000a;color:#cfe6ff;text-align:center}
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

<div class="hd"><h1>遮挡记忆 · 手机演示</h1><span class="pill" id="st">连接中</span></div>

<div class="view">
  <video id="v" playsinline muted autoplay></video>
  <img id="shot" alt="实时">
  <div class="hud" id="hud">—</div>
  <div class="tip" id="tip">点一下画面里的积木，把它锁定</div>
</div>

<div class="msg" id="msg">正在请求摄像头权限…</div>
<div id="cards"></div>
<div class="bar"><button id="clr">清除记忆，重新开始</button></div>
<div class="note">几何说明：手机没有深度，位置由像素反投影到桌面平面得到，
相机位姿为演示用的固定假设值（≈ 机器人腕部俯视桌面），不是标定结果。</div>

<script>
const v=document.getElementById('v'), shot=document.getElementById('shot');
const hud=document.getElementById('hud'), msg=document.getElementById('msg');
const st=document.getElementById('st'), cards=document.getElementById('cards');
const tip=document.getElementById('tip');
const cv=document.createElement('canvas'); cv.width=1280; cv.height=720;
const cx=cv.getContext('2d');
let busy=false, frames=0;

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

// 点选：把点击位置换算到 1280×720 的画面坐标
shot.onclick=async (ev)=>{
  const r=shot.getBoundingClientRect();
  const u=(ev.clientX-r.left)/r.width*1280, vv=(ev.clientY-r.top)/r.height*720;
  try{
    const j=await (await fetch('/lock',{method:'POST',headers:{'Content-Type':'application/json'},
                                       body:JSON.stringify({u:u,v:vv})})).json();
    if(j.ok) setMsg('已锁定 '+j.id+'，现在拿盒子挡上去','ok');
    else setMsg(j.error||'锁定失败','bad');
  }catch(e){ setMsg('锁定失败：'+e.message,'bad'); }
};

const EVID={visible:'看得见',occluded:'被判遮挡',missing:'按丢失处理'};
function render(j){
  frames++;
  st.textContent = !j.objects.length ? '等待点选'
                 : (j.objects.some(o=>o.state==='occluded')? '遮挡中':'跟踪中');
  hud.textContent='objects='+j.objects.length+'  obstacles='+j.obstacles+'  frames='+j.frames;
  tip.style.display = j.tracked? 'none':'block';

  cards.innerHTML = j.objects.map(o=>{
    const ev1=o.pose?1:0, ev2=(o.state==='occluded')?1:0, ev3=(o.age_s!==null&&o.age_s<300)?1:0;
    const dec=j.decisions[o.id];
    const decHtml = dec? `<div class="dec ${dec.action}"><b>${
        dec.action==='blind_pick'?'可以盲抓':dec.action==='refuse'?'拒绝抓取':'先重看一眼'
      }</b> · ${dec.message}</div>` : '';
    return `<div class="card">
      <h3>${o.id}<span class="st ${o.state}">${EVID[o.state]||o.state}</span></h3>
      <div class="row"><span>记忆位置 (base_link)</span><b>${
        o.pose? o.pose.map(v=>v.toFixed(2)).join(', ') : '—'}</b></div>
      <div class="row"><span>这条记忆多久了</span><b>${o.age_s===null?'—':o.age_s.toFixed(0)+' s'}</b></div>
      <div class="row"><span>位置不确定度</span><b>${o.uncertainty_mm===null?'—':'±'+o.uncertainty_mm+' mm'}</b></div>
      <div class="row"><span>当前帧</span><b>${o.visible?('匹配 '+o.score.toFixed(2)):(o.diff!==null?'区域变化 '+o.diff:'—')}</b></div>
      <div class="ev">
        <span class="${ev1?'on':'off'}">① 有历史位置</span>
        <span class="${ev2?'on':'off'}">② 区域被占</span>
        <span class="${ev3?'on':'off'}">③ 记忆未过期</span>
      </div>
      ${decHtml}
    </div>`;
  }).join('') || '<div class="card"><div class="row"><span>点一下画面里的积木，先把它锁定</span></div></div>';

  const occ=j.objects.filter(o=>o.state==='occluded').length;
  if(!j.tracked) setMsg('点一下画面里的积木，把它锁定');
  else if(!j.objects.length) setMsg('已锁定，正在等第一帧建立记忆…');
  else if(occ) setMsg('目标被挡住了 —— 记忆位置保持不动，正在用记忆位姿决策','ok');
  else setMsg('已记忆位置（现在可以拿盒子挡上去）','ok');
}
async function loop(){ await pump(); setTimeout(loop, 250); }

document.getElementById('clr').onclick=async()=>{
  await fetch('/reset',{method:'POST'}); setMsg('记忆已清除，点一下积木重新锁定','');
};

(async()=>{
  try{
    const stream=await navigator.mediaDevices.getUserMedia({
      video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}},audio:false});
    v.srcObject=stream; await v.play();
    setMsg('摄像头已就绪，点一下画面里的积木'); loop();
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
            elif path == "/lock":
                d = json.loads(self._body().decode("utf-8") or "{}")
                self._json(self.session.lock_on(float(d.get("u", 640)),
                                                float(d.get("v", 360))))
            elif path == "/reset":
                self._json(self.session.reset())
            else:
                self.send_error(404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="遮挡记忆演示（手机摄像头，点选目标）")
    p.add_argument("--port", type=int, default=8302)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--res", default="1280x720")
    p.add_argument("--fresh-s", type=float, default=30.0,
                   help="记忆新鲜期：这个时间内允许直接盲抓")
    args = p.parse_args(argv)

    res = tuple(int(v) for v in args.res.lower().split("x"))
    ips = _lan_ips()
    cert, key = ensure_cert(ips)
    session = OcclusionSession(resolution=res, fresh_s=args.fresh_s)
    Handler.session = session
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print("=" * 60, flush=True)
    for ip in ips:
        tag = "  ← 手机连『移动热点』用这个" if ip.startswith("192.168.137.") else ""
        print(f"[OCC] 手机打开  https://{ip}:{args.port}{tag}", flush=True)
    print("[OCC] 首次会提示『连接不是私密连接』→ 高级 → 继续前往", flush=True)
    print("[OCC] 流程：点一下积木 → 记住位置 → 拿盒子挡住 → 看它还能不能决策",
          flush=True)
    print("=" * 60, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[OCC] 退出", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
