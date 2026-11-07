# -*- coding: utf-8 -*-
"""把 rebench/perception.py 渲染成逐行注解的 HTML 讲义。"""
import html
import pathlib

SRC = pathlib.Path("rebench/perception.py")
OUT = pathlib.Path("rebench/docs/perception_walkthrough.html")

SECTIONS = {
    1: "模块说明：这份代码的契约",
    17: "数据结构：Slot 与 SceneModel",
    42: "核心数学：反投影与坐标系变换",
    59: "台面高度估计",
    68: "两个小工具：颜色分类与主轴方向",
    89: "主函数 observe_slots",
    147: "适配层：槽位结果 → 物体结果",
    169: "自测：合成一张深度图",
}

A = {
1: "模块文档字符串。这三条前提是设计契约：改代码前先确认它们仍然成立，否则整段逻辑的前提就没了。",
2: "空行。Python 用缩进表达层级，不用花括号。",
3: "设计前提之一：相机固定、台面高度已知。",
4: "前提之二：身份靠颜色，位置靠槽位，不做 6D 位姿估计。",
5: "前提之三：输出是结构化数据，不是图像。",
8: "自测入口。写清楚怎么跑，明天的你才不用猜。",
9: "文档字符串结束。",
11: "从标准库导入 dataclass（自动生成构造函数）和 field（用于可变默认值）。",
12: "typing 里的类型标注：只影响可读性和编辑器提示，运行时没有开销。",
14: "numpy 是唯一的第三方依赖。全部计算靠它向量化。",
16: "两个空行分隔顶层定义，PEP 8 惯例。",
17: "@dataclass 装饰器：自动为你生成 __init__、__repr__、__eq__。不用手写 self.cx = cx。",
18: "class Slot 定义一个矩形区域，即“槽位”。",
19: "槽位名，会出现在日志和报告里，取名要能一眼认出来。",
20: "中心 x，单位米，相对机器人 base 坐标系。单位写进注释，这是最容易出错的地方。",
21: "中心 y，单位同上。",
22: "half_x 是半边长，不是全长。用半边长做区间判断更省事：abs(x-cx) <= half_x。",
23: "half_y 同理。",
26: "SceneModel 把所有可调阈值收在一个对象里。调参只改这里，不散落在函数内部。",
27: "类定义。",
28: "table_z 默认 0，表示“不指定，运行时从深度图自动估计”；给正数则直接用你标定好的值。",
29: "高度阈值 8mm：高于台面这么多才算物体。低于 5mm 会把桌面纹理当物体，高于 15mm 会漏掉薄片。",
30: "面积阈值 120 像素。注意它和分辨率绑定——换相机或改分辨率必须重调。",
31: "有效深度比例阈值。低于 0.4 说明这个槽位大半个被挡住了，直接判“不可观测”，不猜。",
32: "D405 量程有限，超过这个距离的点不可信，参与计算会污染结果。",
33: "palette：参考色表。field(default_factory=lambda: {...}) 是可变默认值的正确写法。",
34: "红色参考值。这些是“典型值”，现场最好用实测中位数替换。",
35: "蓝色参考值。",
36: "绿色参考值。",
37: "黄色参考值。",
38: "lambda 返回一个新 dict，保证每个实例拿到独立副本。若直接写 = {} 或 = {...}，所有实例会共享同一个对象，改一个全变。",
39: "color_tol：RGB 欧氏距离阈值。超过就返回 None，宁可说“不确定”，不硬凑一个颜色。",
42: "deproject：把深度图的每个像素还原成三维点。这是整条流水线的第一步。",
43: "文档字符串，写明单位是米、无效值的约定。",
44: "解包出高度和宽度。",
45: "meshgrid 生成两张与图像同尺寸的坐标网格：u 是列号，v 是行号。这一步是“不用 for 循环”的关键。",
46: "转成 float64。如果原图是 uint16（毫米），不转会溢出或整除截断。",
47: "小孔成像逆运算：x = (u - cx) * z / fx。K[0,2] 是主点横坐标，K[0,0] 是 x 方向焦距。",
48: "y 方向同理：用行号 v、主点纵坐标 K[1,2]、焦距 K[1,1]。",
49: "沿最后一维堆叠成 H×W×3，每个像素一个 (x, y, z)。",
52: "to_base：把相机坐标系下的点转到机器人 base 坐标系。感知模块内部一律用 base 系。",
53: "T 是 4×4 齐次变换矩阵，相机 → base。",
54: "左上 3×3 是旋转矩阵 R。",
55: "右上 3×1 是平移向量 t。",
56: "先 reshape 成 N×3，再做 p·Rᵀ + t。矩阵乘比 Python 循环快两个数量级以上，这是 numpy 的核心用法。",
59: "estimate_table_z：估计台面高度。",
60: "为什么用分位数：均值会被桌面上的物体抬高，导致后面高度算错。",
61: "取出所有点的 z 分量。",
62: "只要有限、正数、且在量程内的点。np.isfinite 同时过滤 NaN 和 inf，比 x == x 更清晰。",
63: "有效点太少说明相机没数据或内参错了。",
64: "直接抛异常，不返回一个假的高度。静默降级是调试的噩梦。",
65: "取 15% 分位数：桌面上多数像素是台面本身，低分位能稳定落在台面上。",
68: "下划线开头的函数是“内部使用”的约定，外部不该直接调。",
69: "best_d 初始化为一个足够大的数，任何真实距离都会更小。",
70: "遍历参考色表。.items() 同时拿到名字和 RGB。",
71: "平方欧氏距离。只比较大小，不必开方，省一次运算。",
72: "更近就更新。",
73: "记录当前最优。",
74: "超过阈值返回 None。注意 None 在后面的判据里会被当成“未知色”，而不是“没有物体”。",
77: "_yaw_deg：由点集估算矩形朝向。",
78: "矩形有 180 度对称，所以结果折到 0-90 度。",
79: "先减去均值，把点云平移到原点附近，避免数值误差。",
80: "协方差矩阵描述点集的分布方向。np.cov(c.T) 要求每行是一个变量。",
81: "点太少或全重合时协方差会出现 NaN，提前返回 0，避免后面崩掉。",
83: "eigh 专用于对称矩阵，比通用 eig 更稳定，且返回的特征值已排序。",
84: "最大特征值对应的特征向量就是点集伸展最长的方向，也就是矩形长边。",
85: "用 arctan2 求角度再转成度。%180 折到半圆。",
86: "再 %90 折到四分之一圆，对应矩形的对称性。",
89: "主函数。六个参数，全部有类型标注，读签名就知道要准备什么。",
90: "depth：单通道深度图，米。",
91: "color：RGB 彩色图，与深度同分辨率。",
92: "K：3×3 相机内参。",
93: "T_cam2base：4×4 外参。",
94: "slots：槽位列表。",
95: "model：阈值集合。",
96: "返回 dict：槽位名 → 该槽位的观测结果。",
97: "一句话说明返回值不含图像，避免下游误用。",
98: "两步链式调用：先反投影到相机系，再转到 base 系。括号嵌套读法是从里往外。",
99: "把 N×3 的点云拆成三个一维数组，后面全是布尔运算。",
100: "彩色图摊平成 N×3，保证第 i 行对应的就是点云第 i 行（同分辨率、同遍历顺序）。",
102: "valid 是一个与点云等长的布尔数组：有限、不太近（>5cm）、且不超量程。",
103: "如果 model.table_z 是 0（未指定），就从有效点中自动估计；否则用你给的值。",
105: "out 先建空字典，循环里逐个填。",
106: "遍历每个槽位。",
107: "inbox 开始：一个布尔掩码，标记“属于这个槽位的有效点”。",
108: "x 方向在 [cx-half_x, cx+half_x] 内。abs 简化了区间判断。",
109: "y 方向同理。",
110: "再与 valid 做逻辑与，只有深度有效的点才参与统计。",
111: "括号闭合。三个条件用 & 连接，不能用 and（and 对数组会报错）。",
112: "count_nonzero 数 True 的个数。转 int 是为了后面做除法。",
113: "一个点都没落在区域内。",
114: "记为不可见。注意 valid_ratio 写 0.0，原因是 out_of_view 而不是 occluded，便于区分。",
115: "continue 跳过本次循环剩余部分，处理下一个槽位。",
117: "计算有效比例：分母是“几何上落在矩形内的所有点”，分子是其中深度有效的。",
118: "分母：只做几何判断，不加 valid。",
119: "max(..., 1) 防止分母为 0 触发除零错误。",
120: "比例低于阈值。",
121: "判为被遮挡。这一条是“诚实性”的技术落点：看不见就说看不见，不猜。",
122: "continue 到下一个槽位。",
124: "高度 = 台面 z − 点的 z，乘 1000 转成毫米。相机在上方，物体越近 z 越小，所以高度为正。",
125: "on_obj：布尔掩码，标记“高于阈值”的点，即物体表面。",
126: "数物体像素。",
127: "面积太小。",
128: "判为空槽位。注意这和 occluded 是两种完全不同的事：一个是“看清楚了没有东西”，一个是“没看清”。",
129: "continue。",
131: "把物体像素的 (x, y) 堆成 N×2，供求质心和主轴用。",
132: "均值即质心。mean(axis=0) 对每一列分别求平均。",
133: "颜色取中位数而不是均值：中位数抗高光和阴影，均值会被一小块反光拉偏。",
134: "组装返回字典。",
135: "found=True 表示这个槽位里确实有东西。",
136: "保留有效比例，方便事后判断这次观测有多可信。",
137: "dx = 质心 x − 槽位中心 x，转毫米并保留一位小数。这就是下游要的“偏了多少”。",
138: "dy 同理。",
139: "yaw 走 PCA，结果同样保留一位小数。",
140: "颜色分类，可能是 None（未知色）。",
141: "像素数保留，用于排查阈值是否合适。",
142: "高度取中位数，可用于判断“单个”还是“叠了两层”。",
143: "字典闭合。",
144: "返回全部槽位的结果。",
147: "适配层：把“按槽位组织”的结果，转成 state.judge() 需要的“按物体组织”的格式。",
148: "身份靠颜色匹配：槽位里出现红色，就认为红块在那里。",
149: "空字典起步。",
150: "遍历 S0 里登记的每个物体。",
151: "hit 用来记第一个匹配到的槽位。",
152: "遍历所有槽位结果。",
153: "同时满足“找到了”且“颜色等于该物体的颜色”。颜色为 None 时不会误匹配。",
154: "记下槽位名和结果。",
155: "找到第一个就够了，跳出内层循环。",
156: "一个都没匹配上。",
157: "slot 给 None。judge 会据此判为缺失或不可观测——这正是我们希望的语义。",
158: "匹配上了。",
159: "解包。",
160: "按 judge 需要的字段组装。",
161: "物体所在槽位。",
162: "横向偏移。",
163: "纵向偏移。",
164: "朝向偏差。",
165: "字典闭合。",
166: "返回物体级观测，可以直接喂给 state.judge()。",
169: "自测函数：不接真机也能验证逻辑。这是最省时间的投资。",
170: "合成场景：台面 0.60 米，两个 45mm 高的块。",
171: "图像大小 240×320。",
172: "焦距（像素单位）。真实值请用 get_camera_intrinsic() 取。",
173: "组装 3×3 内参矩阵。",
175: "整张深度图填 0.60，即台面。",
176: "整张彩色图填浅灰 (235)。dtype 必须写 uint8，否则存图像时会出问题。",
178: "说明：这里把相机与 base 设为重合，方便手算验证。",
179: "np.eye(4) 生成 4×4 单位矩阵，即“不旋转不平移”。",
181: "内部小函数：在图上“画”一块矩形。",
182: "世界坐标 → 像素坐标的左边界：u = 主点 + x * f / z。",
183: "右边界同理。",
184: "上边界，用行坐标 v。",
185: "下边界。",
186: "把这块区域的深度改成 0.555，比台面近 45mm，模拟一块高 45mm 的物体。",
187: "同时把颜色改成该物体的颜色。",
189: "红块放在 x=-0.10，比 tray 中心偏左 5mm。",
190: "蓝块放在 y=+0.02，比 box_A 中心偏上 20mm。",
192: "定义三个槽位。",
193: "tray 中心 -0.095，所以红块会产生约 -5mm 的 dx。",
194: "box_A 中心 0.10。",
195: "box_B 放在 y=0.15，画面里没有东西，应判为空。",
196: "列表闭合。",
197: "跑一次观测。table_z 显式给 0.60，跳过自动估计。",
198: "遍历结果。",
199: "f-string 格式化：{k:8s} 表示左对齐占 8 个字符，{v} 直接打印字典。",
200: "打印预期，方便一眼对比实测和期望。",
203: "只有直接运行本文件时才执行；被 import 时不执行。",
204: "调用自测。",
205: "文件结束。",
}

CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#e4e8ee;--code:#f6f8fa;--accent:#1F3864;--hl:#fff8e6}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 64px;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.65}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:24px;font-weight:600;color:var(--accent);margin:0 0 6px}
.sub{color:var(--muted);font-size:14px;margin:0 0 26px}
h2{font-size:16px;font-weight:600;margin:34px 0 10px;padding-left:10px;border-left:4px solid var(--accent);color:var(--accent)}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0 8px}
.card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fbfcfe}
.card b{display:block;font-size:13px;color:var(--accent);margin-bottom:4px}
.card span{font-size:13px;color:#333}
table{width:100%;border-collapse:collapse;margin:6px 0 4px;table-layout:fixed}
td{padding:3px 8px;vertical-align:top;border-bottom:1px solid #f2f4f7}
td.ln{width:44px;text-align:right;color:#9aa0a6;font-family:Consolas,monospace;font-size:12px;user-select:none}
td.code{width:52%;background:var(--code);font-family:Consolas,"Courier New",monospace;font-size:12.5px;white-space:pre;overflow-x:auto;color:#0d1117;border-radius:4px}
td.note{font-size:13px;color:#24292f}
tr.key td.code{background:var(--hl)}
tr.key td.note{font-weight:500}
.sec td{background:var(--accent);color:#fff;font-size:13px;font-weight:600;padding:6px 10px;border:0;border-radius:5px}
.foot{margin-top:26px;font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:12px}
"""

_FIX = {
112: "count_nonzero 数 True 的个数，统计落在该槽位内的有效点数。",
113: "以下四行把槽位的世界坐标换算成像素范围：u = fx·x/z + cx。",
114: "为什么必须换算：深度无效的像素会反投影到原点，若原点恰在槽位内，",
115: "用“矩形内点数”当分母会被撑大，把空槽位误判成遮挡。这个坑真踩过。",
116: "u0：槽位左边界的像素坐标。",
117: "u1：右边界。",
118: "v0 / v1：上下边界。",
119: "整块槽位都投到画面外 → 这个槽位根本没进视野。",
120: "记 out_of_view。和 occluded 是两件事：一个是没拍到，一个是拍到了但被挡。",
121: "continue 到下一个槽位。",
122: "有效比例 = 实测有效点数 ÷ 该槽位应占的像素数。",
123: "比例低于阈值 → 遮挡。这是“诚实性”在代码里的落点：看不见就说看不见。",
124: "记为 occluded，交给上层换视角或转人工。",
125: "continue。",
}
_depth, _remap = {}, {}
for _k, _v in A.items():
    if _k >= 124:
        _depth[_k + 3] = _v
    elif 113 <= _k <= 122:
        continue
    else:
        _depth[_k] = _v
_depth.update(_FIX)
A = _depth
KEY = {45, 46, 56, 62, 65, 74, 83, 84, 102, 110, 123, 128, 136}

lines = SRC.read_text(encoding="utf-8").split("\n")
rows = []
for i, raw in enumerate(lines, start=1):
    if i in SECTIONS:
        rows.append(f'<tr class="sec"><td colspan="3">{SECTIONS[i]}</td></tr>')
    cls = ' class="key"' if i in KEY else ""
    code = html.escape(raw) if raw.strip() else "&nbsp;"
    note = html.escape(A.get(i, "·"))
    rows.append(
        f'<tr{cls}><td class="ln">{i}</td><td class="code">{code}</td><td class="note">{note}</td></tr>'
    )

cards = [
    ("向量化", "所有计算都是“整张图一起算”，没有一个 for 循环遍历像素。慢的写法会卡住主循环。"),
    ("布尔掩码", "inbox / valid / on_obj 都是布尔数组，用 & 连接，再用它去“挑”数据，这是 numpy 的核心套路。"),
    ("分位数与中位数", "台面高度用 15% 分位，颜色用中位数。两者都是为了让少量异常像素不影响结论。"),
    ("小孔成像逆运算", "x=(u-cx)·z/fx。深度图转点云就靠这一个公式，记住它比记住函数名有用。"),
    ("PCA 求主轴", "协方差矩阵最大特征值的方向就是物体长边，折到 0–90° 得到 yaw。"),
    ("宁可返回 None", "看不清就返回 occluded，颜色不确定就返回 None。不猜，是这套代码的设计底线。"),
]
card_html = "".join(f'<div class="card"><b>{t}</b><span>{d}</span></div>' for t, d in cards)

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>perception.py 逐行讲解</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>perception.py 逐行讲解</h1>
<p class="sub">槽位级视觉观测模块 · 共 {len(lines)} 行 · 六条主线在前，逐行注解在后</p>
<div class="cards">{card_html}</div>
<h2>逐行注解</h2>
<table>{''.join(rows)}</table>
<p class="foot">黄色底纹为最该记住的行。若某行注解与代码不一致，以代码为准——这份讲义会随代码演进而过期。</p>
</div></body></html>""",
    encoding="utf-8",
)
print("written", OUT, OUT.stat().st_size, "bytes")
