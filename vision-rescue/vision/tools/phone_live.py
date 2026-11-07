"""手机实时摄像头（HTTPS + getUserMedia），逐帧检测、实时叠加标注。

    python -m vision.tools.phone_live --port 8300

手机浏览器打开 https://<电脑IP>:8300 ，首次会提示"连接不是私密连接"，
选「高级 → 继续前往」即可（自签证书，本地局域网，仅用于比赛演示）。

为什么必须 HTTPS：局域网 http 下浏览器会直接拒绝 getUserMedia，
因为摄像头属于"强隐私权限"，只对安全上下文开放。所以这里自带自签证书。

和 phone_capture.py 的区别：那个是拍照上传（单帧、无稳定性），
这个是连续视频流，稳定性判定（连续 N 帧一致）真正生效。
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from .. import diff as diff_mod
from ..main import VisionApp
from ..snapshot import baseline_blockers
from ..sources import Frame, utc_now_iso

CERT_DIR = Path("vision/certs")
CERT, KEY = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
MAX_UPLOAD = 8 * 1024 * 1024
JPEG_Q = 65                      # 回传给手机的标注图质量：给带宽让路
ANN_MAX_W = 960                  # 标注图回传前缩到这个宽度：
# 1280 宽的标注图 base64 之后 ~110KB，每帧都要在浏览器里解码一次；
# 缩到 960 直接砍掉一半，肉眼几乎看不出差别（积木本来就不靠像素级细节看）。

# 便携版 Git 自带 openssl；系统装了别的也行，这里按顺序找
_OPENSSL_CANDIDATES = [
    Path(r"C:\Users\lenovo\Desktop\WorkBuddy\resources\vendor\PortableGit\usr\bin\openssl.exe"),
    Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
    Path("/usr/bin/openssl"),
]


def ensure_cert(lan_ips: list[str]) -> tuple[Path, Path]:
    """自签证书。SAN 必须带上手机要访问的那个 IP，否则浏览器连警告页都不给过。"""
    if CERT.is_file() and KEY.is_file():
        return CERT, KEY
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    san = ",".join([f"IP:{ip}" for ip in dict.fromkeys(lan_ips + ["127.0.0.1"])]
                   + ["DNS:localhost"])
    err = None
    for exe in _OPENSSL_CANDIDATES:
        if not Path(exe).exists():
            continue
        try:
            subprocess.run(
                [str(exe), "req", "-x509", "-newkey", "rsa:2048",
                 "-keyout", str(KEY), "-out", str(CERT), "-days", "825", "-nodes",
                 "-subj", "/CN=RE-BENCH vision", "-addext", f"subjectAltName={san}"],
                check=True, capture_output=True, timeout=60)
            return CERT, KEY
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            err = exc
    raise RuntimeError(f"生成证书失败，请手动执行 openssl 或安装 cryptography。{err}")


def fit_photo(img: np.ndarray, target) -> np.ndarray:
    """手机画面通常和 16:9 不一致，**居中裁切**再缩放，避免把分区坐标整体扯歪。"""
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


class LiveSession:
    """连续帧检测。每帧只更新状态，只在按钮触发时才落盘。"""

    def __init__(self, config_path: str, output: str | None, min_conf: float) -> None:
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.app = VisionApp(SimpleNamespace(
            camera=0, config=config_path, mode="real", output=output,
            min_conf=min_conf, mock_cycle=False, skip_image_check=False,
            allow_frozen=False, url=None))
        self.app._source = None
        self.lock = threading.Lock()
        self.baseline = None
        self.last_ann: bytes | None = None
        self.last_scene: dict | None = None
        self.last_frame: np.ndarray | None = None
        self.last_ann_img: np.ndarray | None = None
        self.fps_frames = 0
        self.last_diff: dict | None = None
        self.last_msg = "等待手机采集…"

    def frame(self, jpeg: bytes) -> dict:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("这一帧不是可解码的图片")
        img = fit_photo(img, self.cfg.get("resolution", [1280, 720]))
        frame = Frame(image=img, captured_at=utc_now_iso(), source="phone:live",
                      mode="real", index=1)
        with self.lock:
            objects, quality_ok, issues = self.app.observe(frame)
            ann = self.app.annotate(frame, objects, issues)
            # 全尺寸留给快照/标定，回传的那张先缩再编码
            small = ann
            if ann.shape[1] > ANN_MAX_W:
                sc = ANN_MAX_W / float(ann.shape[1])
                small = cv2.resize(ann, (ANN_MAX_W, max(1, int(round(ann.shape[0] * sc)))),
                                   interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q])
            self.last_ann = buf.tobytes() if ok else None
            self.last_ann_img = ann
            self.last_scene = self.app.scene
            self.last_frame = img
            self.fps_frames += 1
            return {
                "ok": True,
                "image": base64.b64encode(self.last_ann).decode() if self.last_ann else None,
                "objects": [{"id": o["object_id"], "zone": o["zone"],
                             "vis": o["visibility"], "conf": o["confidence"]}
                            for o in objects],
                # 配置里应该有几件东西：手机端据此判断"画面里少了谁"
                "expected": self._expected_ids(),
                "quality_ok": quality_ok,
                "issues": issues,
                "has_baseline": self.baseline is not None,
                "baseline_id": (self.baseline or {}).get("snapshot_id"),
                # 诊断：每个标签读到什么字母、多少分。认不出时用户至少能看到
                # "找到了 2 个标签但分数只有 0.4"，而不是干巴巴一句"未检出"。
                # 一个标签框都没找到时，模型对整帧读出的字母（异步、可能滞后）
                "frame_letter": getattr(self.app.detector, "last_global_letter", ""),
                "labels": getattr(self.app.detector, "last_label_info", []),
                "threshold": getattr(self.app.detector, "label_match_threshold", 0.0),
                # 这一帧实际用的读法：模板比对 / 模型+模板 / 模型未就绪。
                # 现场最怕"以为接了模型其实没接上"，所以把它摆在页面上。
                "reader": self.app.detector.reader_name(),
                "vlm": (self.app.detector.vlm.stats()
                        if getattr(self.app.detector, "vlm", None) else None),
            }

    def _expected_ids(self) -> list[str]:
        """配置里应该有几件东西。

        启用字母身份后 object_id 变成 block_A 这种，如果还拿颜色配置里的
        block_red_01 去对，就永远对不上 —— 会一直报"画面里缺目标"。
        """
        return self.app.expected_ids()

    def enroll(self, letter: str) -> dict:
        """把当前画面里那块积木上的字母，登记进模板库。

        现场用法：画面里只放一块积木 → 输入它是哪个字母 → 点登记。
        存盘后下次开机还在，不用重登记。

        **画面里有多块积木时拒绝登记**，这不是谨慎，是必须：以前的做法是
        取"面积最大的那张标签"，于是桌上已经摆着 A、B 的时候去登记 D，
        存进去的是 A 的笔画。之后那块真 D 永远认不出，而且会被认成 A ——
        症状就是"我加了 D，但 D 就是不认"。错的模板一旦进库会一直拖后腿，
        所以宁可拒绝，也不能猜。
        """
        from ..letters import find_labels, ink_mask, normalize

        letter = (letter or "").strip().upper()
        if not letter:
            return {"ok": False, "title": "先填一个字母", "hints": ["比如 A"]}
        with self.lock:
            if self.last_frame is None:
                raise ValueError("还没有收到画面")
            cfg = getattr(self.app.detector, "label_cfg", {}) or {}
            bank = self.app.detector.letter_bank
            if bank is None:
                return {"ok": False, "title": "配置里没启用标签识别"}
            labels = find_labels(self.last_frame, cfg)
            if not labels:
                return {"ok": False, "title": "没找到白色标签",
                        "hints": ["把贴了标签的那一面朝向镜头",
                                  "标签要白底黑字，别用透明胶带"]}

            # 只留下"上面真像有字"的标签
            cands = []
            for lb in labels[:6]:
                mask = ink_mask(normalize(self.last_frame, lb))
                if mask is not None:
                    cands.append((lb, mask))
            if not cands:
                return {"ok": False, "title": "找到了白块，但上面没有字",
                        "hints": ["字要写在白纸/白标签上，笔画粗一点",
                                  "如果积木本身就是白的，直接在白面上写，别贴白标签"]}
            if len(cands) > 1:
                step = int(getattr(self.app.detector, "label_angle_step", 15))
                desc = []
                for lb, m in cands:
                    who, sc = bank.recognize(m, step=step)
                    x, y, w, h = lb.bbox
                    pos = f"{w}×{h}px @({x},{y})"
                    desc.append(f"{pos} → " + (f"像 {who} {sc:.2f}" if who else "读不出"))
                return {"ok": False,
                        "title": f"画面里有 {len(cands)} 张标签，没法确定登记哪一块",
                        "hints": ["只把要登记的那一块积木放进画面，其余的挪出画面",
                                  "或者把其它积木翻到没贴标签的那一面",
                                  *desc]}

            lb, mask = cands[0]
            # 先拿**旧库**看一眼这张字像谁，再入库 —— 入库之后自比必然是
            # 1.00，那时再自检就什么都看不出来了
            step = int(getattr(self.app.detector, "label_angle_step", 15))
            who_before, sc_before = bank.recognize(mask, step=step)
            bank.enroll(letter, mask)
            path = cfg.get("bank")
            if path:
                bank.save(path)

            hints = [f"库里有：{'、'.join(bank.letters())}",
                     "换下一块积木，再登记它的字母"]
            th = float(getattr(self.app.detector, "label_match_threshold", 0.6))
            if who_before and who_before != letter and sc_before >= th:
                hints.insert(0, f"⚠️ 这张字在旧库里最像 {who_before}（{sc_before:.2f}）。"
                                f"如果它其实是 {who_before}，点「删除」把 {letter} 清掉再登记。")
            return {"ok": True, "title": f"已登记 {letter}",
                    "letters": bank.letters(),
                    "letter": letter,
                    "ink_px": int((mask > 0).sum()),
                    "looked_like": [who_before, round(float(sc_before), 3)],
                    "hints": hints}

    def unenroll(self, letter: str) -> dict:
        """删掉某个字母的全部模板（登错时的唯一出口）。"""
        letter = (letter or "").strip().upper()
        if not letter:
            return {"ok": False, "title": "先填要删除的字母"}
        with self.lock:
            bank = self.app.detector.letter_bank
            if bank is None:
                return {"ok": False, "title": "配置里没启用标签识别"}
            n = bank.forget(letter)
            if not n:
                return {"ok": False, "title": f"库里本来就没有 {letter}",
                        "hints": [f"现有：{'、'.join(bank.letters()) or '（空）'}"]}
            cfg = (getattr(self.app.detector, "label_cfg", {}) or {})
            if cfg.get("bank"):
                bank.save(cfg["bank"])
            return {"ok": True, "title": f"已删除 {letter}（{n} 个模板）",
                    "letters": bank.letters(),
                    "hints": [f"现有：{'、'.join(bank.letters()) or '（空）'}",
                              "重新登记时画面里只留一块积木"]}

    def anchor(self) -> dict:
        """把当前画面定为基准 S0。"""
        with self.lock:
            if self.last_scene is None:
                raise ValueError("还没有收到任何画面")

            # 先查"画面里缺谁"。缺件时那一堆 zone_unknown / 边界歧义全是它的
            # 连带症状，直接甩给用户会把人引去调 stability_frames —— 方向就错了。
            # 注意 occluded 也算"在画面里"（只是不完整），别误报成缺失。
            expected = self._expected_ids()
            present = {o["object_id"] for o in self.last_scene["objects"]
                       if o["visibility"] != "unknown" and o["center_px"] is not None}
            missing = [e for e in expected if e not in present]
            if missing:
                names = {"block_red_01": "红色积木", "block_green_01": "绿色积木",
                         "block_blue_01": "蓝色积木"}
                cn = "、".join(names.get(m, m) for m in missing)
                return {"ok": False, "title": "基准被拒绝：画面里缺目标",
                        "issues": [f"{m}:not_detected" for m in missing],
                        "hints": [f"画面里没有{cn}，先把它们完整放进来",
                                  "手机往后移一点，或把积木往画面中间挪，别让它贴边"]}

            blockers = baseline_blockers(self.last_scene, self.app.min_conf)
            if blockers:
                self.app.rejected_store.save(self.last_scene, self.last_frame,
                                             self.last_ann_img)
                return {"ok": False, "title": "基准被拒绝（不合格）",
                        "issues": blockers,
                        "hints": self.app.tuning_hints(self.last_scene["objects"])}
            saved = self.app.store.save(self.last_scene, self.last_frame, self.last_ann_img)
            self.baseline = self.last_scene
            return {"ok": True, "title": "基准已建立",
                    "baseline_id": self.last_scene["snapshot_id"],
                    "saved": str(saved["json"])}

    def compare(self) -> dict:
        """当前画面与基准比较。"""
        with self.lock:
            if self.baseline is None:
                raise ValueError("还没有基准，先点『建立基准』")
            if self.last_scene is None:
                raise ValueError("还没有收到任何画面")
            saved = self.app.store.save(self.last_scene, self.last_frame, self.last_ann_img)
            res = diff_mod.compare(self.baseline, self.last_scene,
                                   min_conf=self.app.min_conf)
            path = diff_mod.save_differences(res, self.app.output_dir)
            self.last_diff = res
            return {"ok": True, "title": "已比较",
                    "status": res["status"],
                    "verdict": diff_mod.gate_verdict(res),
                    "issues": res["issues"],
                    "differences": res["differences"],
                    "saved": str(saved["json"]), "diff": str(path)}

    def clear(self) -> dict:
        with self.lock:
            self.baseline = None
            return {"ok": True, "title": "基准已清除"}


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>RE:BENCH 实时视觉</title>
<style>
:root{--bg:#0d0f13;--panel:#171a20;--line:#2a2f38;--fg:#e9ebef;--dim:#8b929e;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     padding:10px 10px 18px;min-height:100vh}
.hd{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.hd h1{font-size:16px;font-weight:650}
.pill{font-size:11.5px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--ok);border-color:#2a5c40}.pill.bad{color:var(--bad);border-color:#6b2426}
.view{position:relative;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#000}
.view img{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#000}
.view video{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.hud{position:absolute;left:8px;top:8px;font:11px/1.5 ui-monospace,Consolas,monospace;
     color:#cfd6e2;background:#000a;padding:4px 7px;border-radius:5px}
.ov{position:absolute;right:8px;top:8px;font-size:11.5px;padding:3px 8px;border-radius:5px;
    background:#000a;color:var(--blue)}
.warnbar{position:absolute;left:0;right:0;bottom:0;padding:8px 10px;font-size:13.5px;
         background:#6b1f24ee;color:#ffe2e2;text-align:center;display:none;font-weight:600}
.warnbar.on{display:block}
.msg{margin-top:8px;font-size:12.5px;color:var(--dim);min-height:18px}
.msg.bad{color:var(--bad)}.msg.ok{color:var(--ok)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:12px;padding:3px 9px;border-radius:6px;background:#1c2028;border:1px solid var(--line)}
.chip b{font-weight:600}.chip .z{color:var(--blue);font-family:ui-monospace,monospace}
.bar{display:flex;gap:8px;margin-top:12px}
button{flex:1;padding:15px 10px;border-radius:11px;border:1px solid #33507a;background:#1b2536;
       color:var(--fg);font-size:15.5px;font-weight:600}
button.primary{background:#1d4b2e;border-color:#2f7a4a;color:#c9ffdd}
button:disabled{opacity:.4}
.res{margin-top:12px;border:1px solid var(--line);border-radius:11px;padding:11px 13px;
     background:var(--panel);font-size:13.5px}
.res h3{font-size:14px;margin-bottom:6px}
.res table{width:100%;border-collapse:collapse;font-size:13px;margin-top:5px}
.res td,.res th{padding:5px 4px;border-bottom:1px solid #ffffff0c;text-align:left}
.res th{color:var(--dim);font-size:11.5px;font-weight:600}
.res td.m{color:var(--warn)}.res td.u{color:var(--bad)}.res td.k{color:var(--ok)}
.verdict{margin-top:7px;font-weight:700;font-size:14px}
.verdict.READY{color:var(--ok)}.verdict.NOT_READY{color:var(--bad)}.verdict.REVIEW{color:var(--warn)}
.hint{margin-top:6px;font-size:12px;color:var(--dim)}
.lbrow{margin-top:6px;font-size:12px;line-height:1.5;color:#9fd2ff;
       font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
</style></head><body>

<div class="hd">
  <h1>RE:BENCH · 实时视觉</h1>
  <span class="pill" id="st">连接中</span>
</div>

<div class="view">
  <video id="v" playsinline muted autoplay></video>
  <img id="shot" alt="实时标注">
  <div class="hud" id="hud">—</div>
  <div class="ov" id="ov">0 fps</div>
  <div class="warnbar" id="warnbar"></div>
</div>

<div class="msg" id="msg">正在请求摄像头权限…</div>
<div class="chips" id="chips"></div>
<div class="lbrow" id="lbrow" style="display:none"></div>

<div class="bar">
  <button id="anchor" class="primary">建立基准</button>
  <button id="cmp" disabled>与基准比较</button>
</div>

<div class="bar">
  <input id="letter" maxlength="2" placeholder="这块写的字母" inputmode="text"
         autocapitalize="characters" autocomplete="off"
         style="flex:1;padding:14px;border-radius:11px;border:1px solid #2a2f38;
                background:#141821;color:var(--fg);font-size:16px;text-align:center;
                text-transform:uppercase">
  <button id="enroll" style="flex:0 0 30%">登记字母</button>
  <button id="unlabel" style="flex:0 0 22%">删除</button>
</div>

<div class="res" id="res" style="display:none"></div>

<script>
const v=document.getElementById('v'), shot=document.getElementById('shot');
const hud=document.getElementById('hud'), ov=document.getElementById('ov');
const msg=document.getElementById('msg'), chips=document.getElementById('chips');
const st=document.getElementById('st'), res=document.getElementById('res');
const lbRow=document.getElementById('lbrow');
const bAnchor=document.getElementById('anchor'), bCmp=document.getElementById('cmp');
const cv=document.createElement('canvas'); cv.width=1280; cv.height=720;
const cx=cv.getContext('2d');

let busy=false, paused=false, hasBase=false, frames=0, t0=0, missed=0;

function drawCover(){
  const sw=v.videoWidth, sh=v.videoHeight;
  if(!sw||!sh) return false;
  const tr=cv.width/cv.height, sr=sw/sh;
  let cw,ch;
  if(sr>tr){ ch=sh; cw=sh*tr; } else { cw=sw; ch=sw/tr; }
  cx.drawImage(v,(sw-cw)/2,(sh-ch)/2,cw,ch,0,0,cv.width,cv.height);
  return true;
}

function setMsg(t, cls){ msg.textContent=t; msg.className='msg'+(cls?' '+cls:''); }

async function pump(){
  if(busy||paused||v.readyState<2) return;
  busy=true;
  try{
    if(drawCover()){
      const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.82));
      const r=await fetch('/frame',{method:'POST',body:blob});
      const j=await r.json();
      if(j.image) shot.src='data:image/jpeg;base64,'+j.image;
      render(j);
    }
  }catch(e){ missed++; if(missed>3) setMsg('连接中断：'+e.message,'bad'); }
  finally{ busy=false; }
}

// 把 block_red_01:not_detected 这种机器话翻成人话，
// 否则用户看到一堆英文 ID 只会以为是程序坏了
const CN={'block_red_01':'红色积木','block_green_01':'绿色积木','block_blue_01':'蓝色积木'};
const cn=s=>{
  if(CN[s]) return CN[s];
  const m=/^block_([A-Za-z0-9])$/.exec(s);
  if(m) return m[1]+' 块';          // 按字母认身份时 object_id 是 block_A
  return String(s).replace(/^block_/,'').replace(/_01$/,'');
};
function friendly(iss){
  const out=[];
  for(const i of iss){
    // label_pending = 视觉模型还在后台读这一块，属于正常过渡态，不用报给用户
    if(i.startsWith('label_pending:')) continue;
    // 这两条带的是标签框坐标，不是"某个物体"，所以在拆 name:key 之前先拦掉
    if(i.startsWith('label_empty:')){ out.push('有一张标签上没写字（白纸/反光，先跳过）'); continue; }
    if(i.startsWith('label_unread:')){
      out.push('有标签没读出字母（分数偏低）：凑近点、让字正对镜头，或重新登记一次');
      continue;
    }
    const m=/^([A-Za-z0-9_]+):(.+)$/.exec(i);
    if(!m){ out.push(i); continue; }
    const name=cn(m[1]), k=m[2];
    if(k==='not_detected') out.push('看不到'+name);
    else if(k==='zone_unknown') out.push(name+'位置在变');
    else if(k==='possibly_occluded') out.push(name+'被挡或被裁掉一角');
    else if(k==='unrecognized_object') out.push('发现没登记的色块');
    else if(k==='low_confidence') out.push(name+'看不准');
    else if(k==='touching_frame_edge') out.push(name+'贴到画面边');
    else if(k==='label_unreadable') out.push(name+'上的字母认不出来，凑近点、让字正对镜头');
    else if(k.startsWith('label_low_score')) out.push(name+'上的字母太模糊');
    else out.push(name+'：'+k);
  }
  return out;
}

function render(j){
  missed=0;
  frames++; if(!t0) t0=performance.now();
  const fps=frames/((performance.now()-t0)/1000);
  ov.textContent=fps.toFixed(1)+' fps';
  st.textContent=j.has_baseline?'基准已建':'未建基准';
  st.className='pill'+(j.has_baseline?' ok':'');
  hud.textContent='objects='+j.objects.length+'  issues='+(j.issues.length||0)
                 +'  q='+(j.quality_ok?'ok':'bad')
                 +'  tags='+((j.labels||[]).length);
  if(j.labels){
    // 即使这一帧没找到标签也把读法显示出来：现场最怕"以为接了模型其实没接上"，
    // [模板比对] / [模型+模板 qwen-vl-max] 一眼就能看出走的是哪条路。
    lbRow.style.display='block';
    const rd=j.reader?('['+j.reader+'] '):'';
    lbRow.textContent=rd+(j.labels.length
      ? '标签 '+j.labels.length+' 个 · '+j.labels.map(l=>{
          if(!l.letter) return '读不出('+(l.why||'')+')';
          const warn=(l.src==='vlm_conflict')?'⚠':'';   // 模型和模板读到的不一样
          return warn+(l.letter||'')+' '+l.score.toFixed(2)
                 +(l.angle?'@'+l.angle+'°':'')
                 +(l.score>=(j.threshold||0)?'':' (低于'+(j.threshold||0)+')');
        }).join(' · ')
      : '没找到标签：把贴标签那一面朝向镜头');
  }
  chips.innerHTML=j.objects.map(o=>
    '<span class="chip">'+cn(o.id)+' <span class="z">'+o.zone+'</span> <b>'
    +(o.vis!=='unknown'?o.conf.toFixed(2):'未检出')+'</b></span>').join('');

  // 配置里应该有、但这帧没看到的 —— 这才是用户真正要解决的事
  // occluded 不算缺失（它还在画面里，只是被裁了一角）
  const seen=new Set(j.objects.filter(o=>o.vis!=='unknown').map(o=>o.id));
  const miss=(j.expected||[]).filter(id=>!seen.has(id));
  const wb=document.getElementById('warnbar');
  if(miss.length){
    wb.classList.add('on');
    wb.textContent='画面里没有：'+miss.map(cn).join('、')+'  → 把手机往后移/对准桌面';
    setMsg('缺 '+miss.length+' 件：请让它们完整进入画面（现在是出画状态）','bad');
  }else{
    wb.classList.remove('on');
    if(!j.quality_ok){
      setMsg('画面不合格：'+friendly(j.issues).slice(0,2).join('；'),'bad');
    }else{
      setMsg('检测正常 · '+j.objects.filter(o=>o.vis!=='unknown').length+' 件都在画面内','ok');
    }
  }
  hasBase=j.has_baseline;
  bCmp.disabled=!hasBase;
}

function showRes(j){
  res.style.display='block';
  let h='<h3>'+j.title+'</h3>';
  if(j.verdict) h+='<div class="verdict '+j.verdict+'">'+j.verdict+'</div>';
  if(j.differences&&j.differences.length){
    h+='<table><tr><th>目标</th><th>基准</th><th></th><th>当前</th><th>判定</th></tr>';
    for(const d of j.differences){
      const cls=d.status==='unchanged'?'k':(d.status==='uncertain'?'u':'m');
      const lab=d.status==='unchanged'?'原位':(d.status==='misplaced'?'被挪动':'不确定');
      h+='<tr><td>'+cn(d.object_id)+'</td><td>'+d.from_zone+'</td>'
        +'<td>→</td><td>'+d.to_zone+'</td><td class="'+cls+'">'+lab+'</td></tr>';
    }
    h+='</table>';
  }
  if(j.issues&&j.issues.length) h+='<div class="hint">问题：'+friendly(j.issues).join('；')+'</div>';
  if(j.hints&&j.hints.length) h+='<div class="hint">'+j.hints.join('<br>')+'</div>';
  res.innerHTML=h;
}

bAnchor.onclick=async()=>{
  bAnchor.disabled=true;
  try{
    const r=await fetch('/anchor',{method:'POST'});
    const j=await r.json();
    if(j.error){ setMsg(j.error,'bad'); }
    else{ showRes(j); setMsg(j.title, j.ok?'ok':'bad'); if(j.ok){hasBase=true;bCmp.disabled=false;} }
  }finally{ bAnchor.disabled=false; }
};
bCmp.onclick=async()=>{
  bCmp.disabled=true;
  try{
    const r=await fetch('/compare',{method:'POST'});
    const j=await r.json();
    if(j.error) setMsg(j.error,'bad'); else showRes(j);
  }finally{ bCmp.disabled=false; }
};

const inpLetter=document.getElementById('letter'), bEnroll=document.getElementById('enroll');
bEnroll.onclick=async()=>{
  const L=(inpLetter.value||'').trim().toUpperCase();
  if(!L){ setMsg('先填这块积木上写的是哪个字母','bad'); return; }
  bEnroll.disabled=true;
  try{
    const r=await fetch('/label',{method:'POST',headers:{'Content-Type':'application/json'},
                                 body:JSON.stringify({letter:L})});
    const j=await r.json();
    if(j.error){ setMsg(j.error,'bad'); return; }
    const tips=(j.hints||[]).map(h=>'<div class="hint">'+h+'</div>').join('');
    res.style.display='block';
    res.innerHTML='<h3>'+(j.title||'')+'</h3>'+tips;
    if(j.ok){
      const lk=j.looked_like||[];
      const warn=(lk[0]&&lk[0]!==L&&lk[1]>=0.6)?'　⚠️ 这张字更像 '+lk[0]+'（'+lk[1].toFixed(2)+'）':'';
      setMsg('已登记 '+L+'（笔画 '+j.ink_px+'px）　库里：'+(j.letters||[]).join('、')+warn,
             warn?'bad':'ok');
      if(!warn) inpLetter.value='';
    }else{
      setMsg(j.title,'bad');
    }
  }finally{ bEnroll.disabled=false; }
};
inpLetter.addEventListener('keydown',e=>{ if(e.key==='Enter') bEnroll.click(); });

const bUnlabel=document.getElementById('unlabel');
bUnlabel.onclick=async()=>{
  const L=(inpLetter.value||'').trim().toUpperCase();
  if(!L){ setMsg('先填要删除的字母','bad'); return; }
  if(!confirm('把库里 '+L+' 的模板全部删掉？')) return;
  bUnlabel.disabled=true;
  try{
    const r=await fetch('/unlabel',{method:'POST',headers:{'Content-Type':'application/json'},
                                   body:JSON.stringify({letter:L})});
    const j=await r.json();
    if(j.error){ setMsg(j.error,'bad'); return; }
    const tips=(j.hints||[]).map(h=>'<div class="hint">'+h+'</div>').join('');
    res.style.display='block';
    res.innerHTML='<h3>'+(j.title||'')+'</h3>'+tips;
    setMsg(j.ok?j.title:(j.title||'删除失败'), j.ok?'ok':'bad');
    if(j.ok) inpLetter.value='';
  }finally{ bUnlabel.disabled=false; }
};

(async()=>{
  try{
    const stream=await navigator.mediaDevices.getUserMedia({
      video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}},audio:false});
    v.srcObject=stream;
    await v.play();
    setMsg('摄像头已就绪，开始逐帧检测');
    t0=performance.now();
    setInterval(pump,250);
  }catch(e){
    setMsg('拿不到摄像头：'+e.name+' — '+(e.message||''),'bad');
    if(location.protocol!=='https:'){
      setMsg('必须用 https 打开（当前是 '+location.protocol+'），http 下浏览器禁止访问摄像头','bad');
    }
  }
})();
</script></body></html>"""


WATCH = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE:BENCH · 实时监看</title>
<style>
:root{--bg:#0b0d11;--panel:#15181e;--line:#2a2f38;--fg:#eef0f4;--dim:#8b929e;
      --ok:#3ddc84;--warn:#ffb020;--bad:#ff5c5c;--blue:#4aa8ff}
*{box-sizing:border-box;margin:0;padding:0}
/* 用固定高度算，别用 flex:1 —— 父容器高度不确定时 flex 分配会算错，
   stage 撑满把底部栏挤出视口。56 + 78 是头部/底栏的固定高度。 */
body{background:var(--bg);color:var(--fg);height:100vh;overflow:hidden;
     font:16px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.head{position:absolute;top:0;left:0;right:0;height:56px;display:flex;align-items:center;
      gap:16px;padding:0 18px;border-bottom:1px solid var(--line);z-index:5}
.head h1{font-size:20px;font-weight:650;letter-spacing:.3px}
.pill{font-size:13px;padding:4px 12px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--ok);border-color:#2a5c40}.pill.bad{color:var(--bad);border-color:#6b2426}
.spacer{flex:1}
/* 不依赖 calc(100vh) —— 浏览器缩放时 vh 会跟着变，底部栏会被挤出视口。
   用 top/bottom 夹住，几何上必然精确填充中间区域。 */
.stage{position:absolute;top:0;bottom:0;left:0;right:0;background:#000;overflow:hidden}
.main{position:absolute;top:56px;bottom:78px;left:0;right:0}
.stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.hint{position:absolute;top:50%;left:0;right:0;transform:translateY(-50%);
      color:var(--dim);font-size:17px;text-align:center;padding:0 20px;line-height:2;
      box-sizing:border-box}
.hint .sub{display:block;font-size:13.5px;opacity:.65;margin-top:6px}
.bar{position:absolute;left:0;right:0;bottom:0;height:78px;border-top:1px solid var(--line);
     padding:0 18px;display:flex;gap:26px;align-items:center;flex-wrap:wrap;overflow:hidden;
     background:var(--bg);z-index:5}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.chip{font-size:16px;padding:6px 14px;border-radius:9px;background:#1b2028;border:1px solid var(--line)}
.chip b{font-family:ui-monospace,Consolas,monospace}
.chip .z{color:var(--blue);font-weight:700}
.chip.miss{background:#2a1214;border-color:#6b2426;color:#ffb4b4}
.vd{font-size:21px;font-weight:800;letter-spacing:.6px}
.vd.READY{color:var(--ok)}.vd.NOT_READY{color:var(--bad)}.vd.REVIEW{color:var(--warn)}
.btn{padding:9px 18px;border-radius:9px;border:1px solid #33507a;background:#1b2536;
     color:var(--fg);font-size:15px;font-weight:600;cursor:pointer}
.btn.primary{background:#1d4b2e;border-color:#2f7a4a;color:#c9ffdd}
.btn:disabled{opacity:.4}
.diff{font-size:15px;color:var(--dim)}
.diff em{font-style:normal;color:var(--warn);font-weight:700}
</style></head><body>
<div class="head">
  <h1>RE:BENCH · 实时监看</h1>
  <span class="pill" id="st">等待手机画面…</span>
  <span class="spacer"></span>
  <span class="pill" id="base">未建基准</span>
</div>
<div class="main"><div class="stage">
  <img id="shot" alt="" onerror="this.style.visibility='hidden'"
       onload="this.style.visibility='visible';document.getElementById('ph').style.display='none'">
  <div class="hint" id="ph">等待手机开始推流
    <span class="sub">手机浏览器打开 https://192.168.137.1:8300 并允许摄像头</span>
  </div>
</div></div>
<div class="bar">
  <div class="row" id="objs"></div>
  <span class="spacer"></span>
  <div class="vd" id="vd"></div>
  <div class="diff" id="diff"></div>
  <button class="btn primary" id="b1">建立基准</button>
  <button class="btn" id="b2" disabled>与基准比较</button>
</div>
<div id="toast" style="position:absolute;left:50%;bottom:92px;transform:translateX(-50%);
     background:#1b2028;border:1px solid var(--line);padding:9px 18px;border-radius:9px;
     font-size:15px;display:none;z-index:9;max-width:80%"></div>
<script>
const CN={'block_red_01':'红块','block_green_01':'绿块','block_blue_01':'蓝块'};
const cn=s=>CN[s]||s;
const st=document.getElementById('st'), base=document.getElementById('base');
const objs=document.getElementById('objs'), vd=document.getElementById('vd'), diff=document.getElementById('diff');
let fails=0;

function tick(){
  const im=document.getElementById('shot');
  im.src='/shot?t='+Date.now();
  setTimeout(tick, 260);
}
async function poll(){
  try{
    const j=await (await fetch('/state')).json();
    fails=0;
    const seen=new Set(j.objects.filter(o=>o.visibility!=='unknown').map(o=>o.object_id));
    const miss=['block_red_01','block_green_01'].filter(id=>!seen.has(id));
    st.textContent = j.quality_ok ? '画面正常' : '画面异常';
    st.className = 'pill ' + (j.quality_ok?'ok':'bad');
    document.getElementById('b2').disabled = !j.has_baseline;
    base.textContent = j.has_baseline ? '基准已建' : '未建基准';
    base.className = 'pill ' + (j.has_baseline?'ok':'');
    objs.innerHTML = j.objects.map(o=>{
      const bad = o.visibility==='unknown';
      return '<span class="chip'+(bad?' miss':'')+'">'+cn(o.object_id)
        +' <span class="z">'+(bad?'出画':o.zone)+'</span> <b>'
        +(bad?'—':o.confidence.toFixed(2))+'</b></span>';
    }).join('');
    if(j.diff){
      vd.textContent=j.diff.verdict;
      vd.className='vd '+j.diff.verdict;
      diff.innerHTML=j.diff.differences
        .filter(d=>d.status!=='unchanged')
        .map(d=>cn(d.object_id)+' <em>'+d.from_zone+'→'+d.to_zone+'</em>')
        .join('　') || '全部在原位';
    }
  }catch(e){ if(++fails>4){ st.textContent='连不上服务'; st.className='pill bad'; } }
  setTimeout(poll, 700);
}
function toast(t, bad){
  const el=document.getElementById('toast');
  el.textContent=t; el.style.display='block';
  el.style.borderColor = bad ? '#6b2426' : '#2a5c40';
  el.style.color = bad ? '#ffb4b4' : '#c9ffdd';
  clearTimeout(el._t); el._t=setTimeout(()=>el.style.display='none', 3600);
}
async function act(path){
  try{
    const j=await (await fetch(path,{method:'POST'})).json();
    if(j.error){ toast(j.error, true); return; }
    if(j.title) toast(j.title, !j.ok);
    if(j.hints&&j.hints.length) toast(j.title+'｜'+j.hints[0], !j.ok);
    poll();
  }catch(e){ toast('操作失败：'+e.message, true); }
}
document.getElementById('b1').onclick=()=>act('/anchor');
document.getElementById('b2').onclick=()=>act('/compare');

tick(); poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    session: LiveSession | None = None

    def log_message(self, fmt, *args):
        # 只记错误，正常帧流量太吵
        if str(args[1] if len(args) > 1 else "") not in ("200", "304"):
            print(f"[LIVE] {self.client_address[0]} {fmt % args}", flush=True)

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _jpeg(self) -> None:
        buf = self.session.last_ann
        if not buf:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(buf)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(buf)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/shot":
            self._jpeg()
            return
        if path == "/watch":
            body = WATCH.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/state":
            sc = self.session.last_scene or {}
            df = self.session.last_diff
            self._json({
                "quality_ok": sc.get("quality_ok"),
                "objects": [{k: o.get(k) for k in
                             ("object_id", "visibility", "zone", "confidence")}
                            for o in sc.get("objects", [])],
                "has_baseline": self.session.baseline is not None,
                "diff": None if not df else {
                    "status": df["status"], "verdict": diff_mod.gate_verdict(df),
                    "differences": [{k: d[k] for k in
                                     ("object_id", "status", "from_zone", "to_zone")}
                                    for d in df["differences"]]},
            })
            return
        if path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/frame":
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_UPLOAD:
                    self._json({"ok": False, "error": f"帧大小异常（{n} 字节）"}, 400)
                    return
                self._json(self.session.frame(self.rfile.read(n)))
            elif path == "/anchor":
                self._json(self.session.anchor())
            elif path == "/compare":
                self._json(self.session.compare())
            elif path == "/clear":
                self._json(self.session.clear())
            elif path == "/label":
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b"{}"
                try:
                    letter = (json.loads(raw.decode("utf-8")) or {}).get("letter", "")
                except (ValueError, AttributeError):
                    self._json({"ok": False, "error": "登记请求不是合法 JSON"}, 400)
                    return
                self._json(self.session.enroll(letter))
            elif path == "/unlabel":
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b"{}"
                try:
                    letter = (json.loads(raw.decode("utf-8")) or {}).get("letter", "")
                except (ValueError, AttributeError):
                    self._json({"ok": False, "error": "删除请求不是合法 JSON"}, 400)
                    return
                self._json(self.session.unenroll(letter))
            elif path == "/debug":
                sc = self.session.last_scene or {}
                self._json({
                    "quality_ok": sc.get("quality_ok"),
                    "objects": [{k: o.get(k) for k in
                                 ("object_id", "visibility", "zone", "center_px", "confidence")}
                                for o in sc.get("objects", [])],
                })
            else:
                self.send_error(404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


def _lan_ips() -> list[str]:
    import socket
    ips: list[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
            ips.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return ips


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="手机实时摄像头（HTTPS）")
    p.add_argument("--config", default="vision/config/desktop.json")
    p.add_argument("--output", default=None)
    p.add_argument("--min-conf", type=float, default=0.8)
    p.add_argument("--port", type=int, default=8300)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args(argv)

    ips = _lan_ips()
    cert, key = ensure_cert(ips)

    session = LiveSession(args.config, args.output, args.min_conf)
    Handler.session = session
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print("=" * 56, flush=True)
    for ip in ips:
        tag = "  ← 手机连『移动热点』用这个" if ip.startswith("192.168.137.") else ""
        print(f"[LIVE] 手机打开  https://{ip}:{args.port}{tag}", flush=True)
    print("[LIVE] 首次会提示『连接不是私密连接』→ 高级 → 继续前往", flush=True)
    print(f"[LIVE] 电脑自测    https://127.0.0.1:{args.port}", flush=True)
    print("[LIVE] 必须用 https：http 下浏览器禁止访问摄像头", flush=True)
    print("=" * 56, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[LIVE] 退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
