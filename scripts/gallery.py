# gallery.py - build a single-file HTML contact sheet from a cams/<timestamp>/ folder.
#
# Usage:  python3 gallery.py cams/20261107-095738
# Output: <dir>/gallery.html  (images embedded as base64, so the file opens anywhere)
#
# Reads index.json (written by robot-cam.ps1), embeds every captured frame, uses the
# *_preview.png for depth (16-bit PNGs do not display usefully in a browser), and lists
# the failed channels with a diagnosis table.

import base64, io, json, os, sys

ORDER = [
    ("HEAD_LEFT_CAMERA",            "头部左目 RGB"),
    ("HEAD_RIGHT_CAMERA",           "头部右目 RGB"),
    ("LEFT_ARM_DEPTH_CAMERA",       "左臂深度（16bit → 8bit 预览）"),
    ("RIGHT_ARM_DEPTH_CAMERA",      "右臂深度（16bit → 8bit 预览）"),
    ("LEFT_FRONT_SURROUND_CAMERA",  "左前环视 RGB"),
    ("RIGHT_FRONT_SURROUND_CAMERA", "右前环视 RGB"),
    ("LEFT_REAR_SURROUND_CAMERA",   "左后环视 RGB"),
    ("RIGHT_REAR_SURROUND_CAMERA",  "右后环视 RGB"),
]

WHY = {
    "LEFT_ARM_CAMERA":         "手臂 RGB 驱动未运行（见页脚结论）",
    "RIGHT_ARM_CAMERA":        "手臂 RGB 驱动未运行（见页脚结论）",
    "LEFT_ARM_DOWN_CAMERA":    "该枚举对应未实装的一路，init 直接被拒",
    "RIGHT_ARM_DOWN_CAMERA":   "该枚举对应未实装的一路，init 直接被拒",
    "LEFT_ARM_INFRA_CAMERA_1": "红外流未启用（需 ir_enabled），且本机无该流发布",
    "LEFT_ARM_INFRA_CAMERA_2": "红外流未启用（需 ir_enabled），且本机无该流发布",
    "RIGHT_ARM_INFRA_CAMERA_1": "红外流未启用（需 ir_enabled），且本机无该流发布",
    "RIGHT_ARM_INFRA_CAMERA_2": "红外流未启用（需 ir_enabled），且本机无该流发布",
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1f23;--dim:#666d76;--line:#e3e6ea;--ok:#1a7f37;--bad:#c62828;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1c2024;--fg:#e8eaed;--dim:#9aa3ad;--line:#2c3238;--ok:#4ade80;--bad:#ff6b6b;--warn:#e0b252}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI",system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:22px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
.pill{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font-size:13px}
.pill b{font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.card img{display:block;width:100%;height:auto;background:#000}
.cap{padding:9px 12px;border-top:1px solid var(--line)}
.cap .n{font-weight:600;font-size:13px}.cap .t{color:var(--dim);font-size:12px}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--dim)}
h2{font-size:15px;margin:30px 0 10px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:12.5px}
th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600}tr:last-child td{border-bottom:0}
.note{margin-top:26px;background:var(--card);border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:8px;padding:14px 16px;font-size:13px}
.note b{color:var(--warn)}ul{margin:8px 0 0 18px;padding:0}li{margin:3px 0}
"""

FOOTNOTE = """<div class="note"><b>结论：手臂 RGB 缺的不是硬件，是驱动进程。</b>
<ul>
<li>探针（09:51）在机器人侧只看到 <span class="mono">gmsl_service_daemon</span>（头部 GMSL 相机）在跑，
<span class="mono">/data/galbot/bin/left_arm_camera_capture</span> 与 <span class="mono">right_arm_camera_capture</span> <b>都不在进程表里</b>。</li>
<li>机器人自带示例文件里就写着 <i>"please confirm that the robot's left arm camera driver left_arm_camera_capture …"</i>，
即手臂 RGB 依赖这个独立驱动进程。</li>
<li>两路手臂相机在系统里是 Intel RealSense（USB xhci-2.2 / 3.1）。深度走 RealSense 直读，
所以驱动没起时<b>深度照样出图、RGB 恒空</b>——这正是本次现象。</li>
<li>因此「别人占用」只是表象：占用者退出时可能顺带把驱动带走；驱动不在，谁都取不到 RGB。</li>
</ul></div>"""


def build(d):
    idx = json.load(io.open(os.path.join(d, "index.json"), encoding="utf-8-sig"))
    cap = {c["camera"]: c for c in idx.get("captured", [])}
    cards = []
    for name, label in ORDER:
        meta = cap.get(name)
        if not meta:
            continue
        fn = os.path.join(d, meta["file"])
        if meta["kind"] == "depth":
            cand = os.path.splitext(fn)[0] + "_preview.png"
            if os.path.exists(cand):
                fn = cand
        if not os.path.exists(fn):
            continue
        b = base64.b64encode(open(fn, "rb").read()).decode()
        mime = "image/png" if fn.lower().endswith(".png") else "image/jpeg"
        cards.append((name, label, meta, "data:%s;base64,%s" % (mime, b)))

    fails = idx.get("errors", [])
    h = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
         '<title>G1 相机抓取 · %s</title><style>%s</style></head><body><div class="wrap">'
         % (idx.get("time", ""), CSS),
         '<h1>Galbot G1 全相机抓取 · %s</h1>' % idx.get("time", ""),
         '<div class="sub">robot %s · ssh exit %s</div>' % (idx.get("robot"), idx.get("sshExit")),
         '<div class="stats">',
         '<span class="pill">成功 <b class="ok">%d</b> 路</span>' % len(cards),
         '<span class="pill">失败 <b class="bad">%d</b> 路</span>' % len(fails),
         '</div><div class="grid">']

    for name, label, meta, uri in cards:
        v = "%dx%d · %s · %.0f KB" % (meta["width"], meta["height"], meta.get("format"), meta["bytes"] / 1024.0)
        h.append('<div class="card"><img src="%s" alt="%s"><div class="cap"><div class="n">%s</div>'
                 '<div class="t">%s</div><div class="mono">%s</div></div></div>'
                 % (uri, name, label, name, v))

    h.append('</div><h2>未取到帧的通道</h2><table><tr><th>相机</th><th>报错</th><th>判读</th></tr>')
    for e in fails:
        cam = e.split(" ")[0]
        h.append('<tr><td class="mono">%s</td><td class="mono">%s</td><td>%s</td></tr>'
                 % (cam, e[len(cam):].strip(), WHY.get(cam, "")))
    h.append('</table>' + FOOTNOTE + '</div></body></html>')

    out = os.path.join(d, "gallery.html")
    open(out, "w", encoding="utf-8").write("\n".join(h))
    print("written %s  %.2f MB  cards=%d fails=%d"
          % (out, os.path.getsize(out) / 1048576.0, len(cards), len(fails)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 gallery.py <cams/timestamp>")
        sys.exit(2)
    build(sys.argv[1])
