"""生成自带完整源码的运行手册 docs/GRASP_HANDBOOK.md。

把代码本体内嵌进 markdown，队友只看这一份文档就能跑起来，
不需要去仓库里翻文件。

用法： python tools/build_handbook.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "GRASP_HANDBOOK.md"

# (路径, 语言, 一句话说明)
FILES = [
    ("robot_exec.py", "python", "机器人侧常驻执行器。一次 init，之后走 JSONL 命令。所有抓取脚本都基于它。"),
    ("tools/plan_grasp.py", "python", "主流程：头部全局观察 -> 规划落点 -> 移动一次 -> 腕部深度精扫 -> 一次下降抓取。"),
    ("tools/oneshot_grasp.py", "python", "精简版：直接深度定位 -> 单次垂直下降 -> 合爪。适合已知有目标时快速跑。"),
    ("tools/drive_and_grasp.py", "python", "带底盘：目标超出可达区时，小步前进把它拉进来再抓。"),
    ("robot_link.py", "python", "本地侧 SSH 拉起代理并按 JSONL 收发的封装（观察台用）。"),
    ("robot_agent.py", "python", "只读代理：取深度图、框选返回三维点与 IK 可达性。观察台后端。"),
    ("start.command", "bash", "观察台启动脚本（会加载 vision/config/.env.local 里的模型密钥）。"),
]

HEAD = """# 机器人 3D 抓取 · 运行手册（自带全部源码）

> 这份文档把**所有代码本体**都内嵌进来了，不用去仓库翻文件，照着做就能跑。
> 由 `tools/build_handbook.py` 自动生成，代码改了重新生成即可。

---

## 一、这套东西能干什么

1. 用**头部彩色相机**做全局观察，粗定位桌面上的物体
2. 规划一次机械臂移动，把**腕部深度相机**送到目标上方
3. 用深度点云算物体的精确三维位置和顶面高度
4. **一次垂直下降**到抓取高度，合爪，抬升

不是"一格一格试探"。落点由深度直接算：

```
腕部高度 = 物体顶面 z + 指长(0.189 m) − 抓深(0.028 m)
```

---

## 二、环境

| 项 | 值 |
|---|---|
| 机器人 | `galbot-echo`（Galbot G1） |
| 地址 | `192.168.167.46`，用户 `galbot` |
| SDK | `PYTHONPATH=/data/galbot/lib`，Python 3.8 |
| 本地 | macOS，任何装了 python3 + numpy + opencv 的环境都行 |

### 首次连接（装公钥，只需一次）

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub galbot@192.168.167.46
```

### 建立持久主连接（强烈建议，后面所有操作复用）

```bash
ssh -o ControlMaster=yes -o ControlPath=/tmp/kitt-console-live.sock \\
    -o ControlPersist=1800 galbot@192.168.167.46 'echo ok'
```

所有脚本默认走 `/tmp/kitt-console-live.sock` 这个主连接，没有会自动新建。

---

## 三、怎么跑

### 方式 1：一键抓取（最常用）

```bash
cd vision-rescue
python3 tools/oneshot_grasp.py
```

它会：抬臂 -> 张开夹爪 -> 深度扫描 -> 找物体 -> 逆解校验 -> 一次下降 -> 合爪 -> 抬升。

### 方式 2：带头部观察的完整流程

```bash
python3 tools/plan_grasp.py
```

先用头部彩色看全局，决定往哪边移动，再让腕部深度精扫。延迟实测：
头部观察 268–350 ms，移动 255 ms–2 s，深度精扫 617–824 ms。

### 方式 3：目标太远时用底盘

```bash
python3 tools/drive_and_grasp.py
```

目标 x 超过 0.85 m 时，左右臂垂直抓取够不到，它会小步前进把目标拉进可达区。

### 方式 4：观察台（人工核对）

```bash
cd vision-rescue
./start.command
# 浏览器打开 http://127.0.0.1:8410/
```

⚠️ 观察台会占用腕部深度相机。**跑抓取脚本前必须先关掉它**，否则执行器拿不到深度数据。

---

## 四、`robot_exec.py` 命令协议

机器人上常驻一个进程，**只 init 一次**（省掉每次 3–5 秒的 SDK 初始化），
之后本地按行发 JSON，机器人按行回 JSON。

| 命令 | 作用 | 主要参数 |
|---|---|---|
| `state` | 关节角 + 腕部位姿 + 夹爪宽度/力矩 | — |
| `cart` | 逆解并移动到笛卡尔位姿 | `x,y,z,tilt,speed` |
| `joints` | 直接关节角移动 | `q[7],speed` |
| `grip` | 夹爪 | `w,v,eff,blocking,settle` |
| `depth` | 深度统计（不传图） | — |
| `find` | 深度找物体，返回连通域清单 | `frames` |
| `target` | 高度图求峰找最高物体（更稳） | `frames,peaks,xmin..ymax` |
| `ik` | 只算逆解，不动 | `x,y,z,tilt` |
| `overview` | 头部彩色全局观察 | `table_z` |
| `base` | 底盘小步移动 | `vx,vy,wz,dur` |
| `quit` | 退出 | — |

### ⚠️ 协议必须按 id 匹配

SDK 会往 **stdout** 打日志（`[info] Resolved robot type...`）。
如果客户端"发一行读一行"，会把日志当响应，导致**结果错位**（表现为数据每次都不一样、耗时显示 0 ms）。

正确写法（`tools/*.py` 里都是这个模式）：

```python
_ID = 0
def call(p, obj):
    global _ID
    _ID += 1
    obj = dict(obj, id=_ID)
    p.stdin.write(json.dumps(obj) + "\\n"); p.stdin.flush()
    for _ in range(300):
        line = p.stdout.readline().strip()
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        if r.get("id") == _ID:
            return r
```

---

## 五、关键标定值（都是真机实测，别改）

| 量 | 值 | 说明 |
|---|---|---|
| 工具轴 | 安装帧 `left_arm_end_effector_mount_link` 的 **+X** | 不是 +Z |
| 指长 | **0.189 m** | 安装帧到指尖，按 0.176 算会一直偏低 1.3 cm |
| 腕部相机偏离工具轴 | **0.07 m** | 画面中心 ≠ 夹爪正下方 |
| 桌面高度 | base 下 **z ≈ 0.707 m** | 会随腿部姿态浮动 |
| 夹爪开度 | 0 – 0.12 m，手指厚约 2 cm | |
| 深度相机 | RealSense D405，1280×720，`depth_scale=10000` | 最佳 7–50 cm |
| 头部彩色 | 1280×960，`fx=412.7  cx=668.2  cy=485.2` | **鱼眼**，反投影要用等距模型 `θ=r/f` |

### 左臂垂直抓取可达区（实测）

```
     y=-0.05 +0.00 +0.05 +0.10 +0.15 +0.20 +0.25 +0.30
x=0.60  O   O   O   O   O   O   O   O
x=0.65  O   O   O   O   O   O   O   O
x=0.70  O   O   O   O   O   O   O   O
x=0.75  O   O   O   O   O   O   O   O
x=0.80  .   O   O   O   O   O   O   O
x=0.85  .   .   O   O   O   O   O   O
x=0.90  .   .   .   .   .   .   .   .
```

**目标要放在 x 0.62–0.80、y 0.00–0.28 这个矩形里。**

---

## 六、安全红线（踩过，都是真的）

### 1. 逆解返回 SUCCESS ≠ 解是对的

逆解可能返回**另一个构型分支**的解。真发生过一次：目标只挪了 6 cm，
逆解却给了一个让手臂横向甩出 **43 cm** 的解，执行后手臂直接扫过去，差点打到人。

**必须做的校验：**

```python
ik = call(p, {"cmd":"ik", "x":x, "y":y, "z":z})
assert ik["max_delta_deg"] <= 45, "关节增量过大，拒绝执行"
# 执行后立刻核对实际位置
r = call(p, {"cmd":"cart", "x":x, "y":y, "z":z, "speed":0.05})
assert r["err_mm"] <= 40, "实际位置与目标差太多，疑似跳变，立刻停"
```

### 2. 绝对不要用"直线关节插值"大幅回位

从偏姿态回预设姿态，直线插值会让末端**先下沉**（实测降到离桌面 2 cm）。
要用**笛卡尔小步爬行**：分几个路点，每个路点先算逆解，增量超限就缩小步长。

参考 `/tmp/crawl.py` 的思路（本次实操 4 个路点，增量 26–41°，误差 ≤1.9 mm）。

### 3. `request_shutdown()` 会关掉整机

不是退出 SDK 实例。收尾只调 `r.destroy()`。

### 4. 夹爪阻塞模式会空等

`set_gripper_command(is_blocking=True)` 夹住东西时实测等 **8.8 秒**才返回。
判定是否夹住要用**宽度 + 力矩双判据**：

- 夹空：宽度到限位（约 0.010），力矩约 9.5 Nm
- 夹住实物：宽度卡住（0.021–0.023），力矩 30–39 Nm

### 5. 不要低空横移

离桌面 2 cm 高度横着挪，会把积木直接扫飞。正确顺序：**高空横移对准 → 原地垂直下降**。

### 6. 黑色桌布是深度杀手

有效像素从 79% 掉到 13%，点云稀疏导致聚类碎裂、每次扫描结果都不一样。
**垫一张白纸立刻解决**（实测有效像素 13% → 53~79%）。

### 7. 同一进程不要二次 `init()`

第二次 `init(enable_sensor_set={...})` 不会真正启用传感器，`get_depth_data` 会返回没有 `width` 的字典。
一次 init 时把要用的相机都带上（头部彩色 + 腕部深度可以同时启用）。

---

## 七、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 拿不到深度（没有 `width` 字段） | 相机被别的进程占了 | 关掉观察台（8410），或同进程二次 init |
| 扫描结果每次都不一样 | 桌面太黑 / 没按 id 匹配响应 | 垫白纸；检查 id 匹配 |
| `motion_plan` / `check_collision` 全部 FAULT | WBC 读不到整机状态，规划器坏了 | 不用规划器，走 `set_joint_positions` 直接控制 |
| 夹爪不动、力矩 9.5 Nm 顶着 | 指腹压死在桌面上 | 先把手臂抬高再合爪 |
| 逆解 FAULT | 目标超出可达区 | 看上面的可达区表，或用底盘小步前进 |
| 手臂位置乱跑 | 逆解跳构型分支 | 加关节增量上限 + 执行后核对误差 |

---

## 八、完整源码

以下全部文件按原样内嵌。
"""


def fence_lang(lang: str) -> str:
    return {"python": "python", "bash": "bash"}.get(lang, "")


def main() -> None:
    parts = [HEAD]
    for idx, (rel, lang, desc) in enumerate(FILES, 1):
        path = ROOT / rel
        if not path.exists():
            parts.append(f"\n### {idx}. `{rel}`\n\n> ⚠️ 文件不存在\n")
            continue
        code = path.read_text(encoding="utf-8")
        lines = code.count("\n") + 1
        parts.append(f"\n### {idx}. `{rel}`（{lines} 行）\n\n{desc}\n\n")
        parts.append(f"```{fence_lang(lang)}\n{code.rstrip()}\n```\n")

    parts.append("""
---

## 九、一键自检（跑之前先确认链路通）

```python
import base64, json, subprocess
from pathlib import Path

payload = base64.b64encode(Path("robot_exec.py").read_bytes()).decode()
cmd = ["ssh","-o","BatchMode=yes","-T",
       "-o","ControlPath=/tmp/kitt-console-live.sock","galbot@192.168.167.46",
       "PYTHONPATH=/data/galbot/lib python3 -u -c "
       "\\"import base64;exec(base64.b64decode('%s'))\\"" % payload]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

for _ in range(40):
    line = p.stdout.readline().strip()
    if line.startswith('{"ok"'):
        print("启动:", line); break

p.stdin.write(json.dumps({"cmd":"state","id":1}) + "\\n"); p.stdin.flush()
for _ in range(300):
    line = p.stdout.readline().strip()
    if line.startswith("{"):
        r = json.loads(line)
        if r.get("id") == 1:
            print("腕部:", r["mount"], "夹爪:", r["grip"]["width"]); break

p.stdin.write(json.dumps({"cmd":"depth","id":2}) + "\\n"); p.stdin.flush()
for _ in range(300):
    line = p.stdout.readline().strip()
    if line.startswith("{"):
        r = json.loads(line)
        if r.get("id") == 2:
            print("深度有效像素:", r.get("valid_pct"), "%"); break

p.stdin.write(json.dumps({"cmd":"quit","id":99}) + "\\n"); p.stdin.flush(); p.wait(timeout=10)
```

正常输出示例：

```
启动: {"ok": true, "ready": true, "boot_ms": 3345, "depth_cam": true}
腕部: [0.47, 0.23, 1.1] 夹爪: 0.105
深度有效像素: 70.5 %
```
""")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")
    total = OUT.stat().st_size
    print("WROTE %s  %.1f KB  %d 个源码文件" % (OUT, total / 1024.0, len(FILES)))


if __name__ == "__main__":
    main()
