"""用视觉大模型认标签上的字母。

为什么要有这一层：模板比对（letters.py）要求**现场把每个字母登记一遍**，
而且对字形差异敏感（手写 D 和 B 就很容易混）。视觉模型不用登记、能认任意
字母、对歪斜和光照更宽容，适合"临时多摆两块没登记过的积木"这种场景。

为什么不用第三方 SDK：这台机器上 requests / openai / torch / 任何 OCR 都没装，
装一个要几分钟还可能下不动模型。整个文件只用标准库 + opencv/numpy，
走 OpenAI 兼容的 `/chat/completions`，所以通义、豆包、智谱、Kimi、本地
vLLM/Ollama 换个 base_url 就能用。

**绝不阻塞实时流**。一次调用 0.5-3 秒，而手机页每秒要出好几帧。所以：
  · `peek(tile)` 只查缓存，立刻返回（没结果就是 None，本帧先用模板结果）；
  · `warm(tile)` 把没见过的图丢进后台队列，worker 慢慢算，算完写缓存；
    同一张图"正在算"时不会重复排队（否则后几块积木永远排不上）；
  · 缓存 key 是图的感知哈希，同一块积木不动就不会重复调用。
净效果：第一帧看到新字形时 VLM 还没答上来（用模板顶着），两三帧之后
VLM 的结果就接管了，而稳态下几乎零调用。

配置（vision/config/desktop.json → label.vlm）：
  base_url  例 https://dashscope.aliyuncs.com/compatible-mode/v1
  model     例 qwen-vl-max / qwen-vl-plus / doubao-1.5-vision-pro / glm-4v-plus
  api_key   "env:DASHSCOPE_API_KEY"（推荐，别把 key 写进仓库）或直接写
  timeout_s 默认 8
  max_side  送进模型前把标签缩到最长边 512（够看清笔画，又省 token）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import cv2
import numpy as np

PROMPT = (
    "这是一张从积木贴纸上裁下来的小图，上面有一个人手写或印刷的英文字母，"
    "摆放方向可能是任意的（躺着、倒着都算）。请判断它是哪个大写字母。\n"
    "只输出一个字符：A-Z 中的一个；确实看不出就输出 ?。不要解释，不要加标点。"
)

_LETTER_RE = re.compile(r"[A-Z]")


@dataclass
class VlmResult:
    letter: str = ""
    raw: str = ""
    error: str = ""
    ms: int = 0


@dataclass
class VlmReader:
    """OpenAI 兼容的视觉模型读取器（异步 + 缓存）。"""

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout_s: float = 8.0
    max_side: int = 512
    cache_size: int = 128
    # 并发读几块。1 个线程会出事：三块积木排在一条队上，一块 1.2 秒，
    # 最后那块要等 2.4 秒才轮到 —— 而它可能早就被挪走了。2 个够用又不烧配额。
    workers: int = 2
    # 出错后的冷却期：端点挂了就每帧重排，等于拿队列当 DoS 工具打自己。
    backoff_s: float = 3.0
    # 连续失败几次就认命停用。key 填错时如果不投降，页面会一直显示
    # "模型+模板"，实际上永远拿不到模型结果 —— 这种"假装在工作"比直接
    # 报错更难查，现场只会觉得"接了模型怎么没变准"。
    max_failures: int = 3
    # 运行期状态
    cache: dict = field(default_factory=dict, repr=False)
    last_error: str = ""
    calls: int = 0
    hits: int = 0
    failures: int = 0
    given_up: bool = False
    _q: queue.Queue | None = field(default=None, repr=False, compare=False)
    _threads: list = field(default_factory=list, repr=False, compare=False)
    _inflight: set = field(default_factory=set, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False,
                                  compare=False)
    _backoff_until: float = field(default=0.0, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.api_key = _resolve_secret(self.api_key)
        self.base_url = (self.base_url or "").rstrip("/")
        if self.base_url and not self.base_url.endswith("/chat/completions"):
            self.base_url += "/chat/completions"

    # ------------------------------------------------------------- 可用性
    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key
                    and not self.given_up)

    def why_unavailable(self) -> str:
        if self.given_up:
            return f"连续失败已停用（{self.last_error[:60]}）"
        if not self.base_url:
            return "没配 base_url"
        if not self.model:
            return "没配 model"
        if not self.api_key:
            return "没配 api_key"
        return ""

    def describe(self) -> str:
        if not self.available:
            return "未配置"
        return f"{self.model}"

    # ------------------------------------------------------------- 缓存
    def key_of(self, tile_bgr: np.ndarray) -> str:
        """图的感知哈希：缩到 16×16 灰度后**和自身均值比**（aHash）。

        不能用原始像素的哈希。手机画面每帧都有 ±2 的传感器噪声、透视拉正的
        插值也每次差一点点，原始哈希一变就是"新图" —— 于是每帧都排一次队、
        每帧调一次模型（一次 0.5-3 秒），队列瞬间积压，缓存等于不存在。
        和均值比之后，只有"笔画落在哪"决定 key：整体亮度偏移因为阈值一起平移，
        算出来完全一样；轻微噪声也只有压在均值附近的少数几个格子会翻。
        """
        g = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (16, 16), interpolation=cv2.INTER_AREA)
        bits = (g > float(g.mean())).astype(np.uint8)
        return hashlib.blake2b(np.packbits(bits).tobytes(), digest_size=8).hexdigest()

    def peek(self, tile_bgr: np.ndarray) -> str | None:
        """查缓存。命中返回字母（"?" 解析成空串），没算过返回 None。"""
        k = self.key_of(tile_bgr)
        if k in self.cache:
            self.hits += 1
            return self.cache[k]
        return None

    def _remember(self, k: str, letter: str) -> None:
        if len(self.cache) >= self.cache_size:
            # 简单粗暴地丢掉最早的一半，现场够用（不值得上 LRU）
            for old in list(self.cache)[: self.cache_size // 2]:
                self.cache.pop(old, None)
        self.cache[k] = letter

    # ------------------------------------------------------------- 异步
    def _start(self) -> None:
        """懒启线程池。第一次 warm 才起，没配 key 时一个线程都不开。"""
        if self._q is not None:
            return
        with self._lock:
            if self._q is not None:
                return
            self._q = queue.Queue(maxsize=16)
            for i in range(max(1, int(self.workers))):
                t = threading.Thread(target=self._worker, name=f"vlm-{i}", daemon=True)
                t.start()
                self._threads.append(t)

    def warm(self, tile_bgr: np.ndarray) -> None:
        """后台排队识别。**同一张图在算的时候不会重复排队**。

        这一条是必需的，不是优化：手机每秒推好几帧，同一块积木会连着出现
        几十次。不记"正在算"的话，每次 warm 都往队列里塞一份，堆满 16 格之后
        第二块、第三块积木永远排不上 —— 实测就是"只认得出第一块"。
        """
        if not self.available:
            return
        if time.time() < self._backoff_until:
            return                    # 刚出错，先歇几秒，别把队列打满
        k = self.key_of(tile_bgr)
        if k in self.cache:
            return
        self._start()
        with self._lock:
            if k in self._inflight:
                return
            self._inflight.add(k)
        try:
            self._q.put_nowait((k, tile_bgr.copy()))
        except queue.Full:
            with self._lock:
                self._inflight.discard(k)

    def _worker(self) -> None:
        while True:
            k, tile = self._q.get()
            try:
                if k in self.cache:
                    continue
                res = self.read(tile)
                if res.error:
                    # 出错**不写缓存**（网络恢复要能重试），但要歇一会儿再试
                    self.failures += 1
                    self._backoff_until = time.time() + self.backoff_s
                    if self.failures >= self.max_failures:
                        self.given_up = True      # 别再无谓地打接口了
                else:
                    self.failures = 0
                    self._remember(k, res.letter)
            except Exception as exc:            # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.failures += 1
                self._backoff_until = time.time() + self.backoff_s
                if self.failures >= self.max_failures:
                    self.given_up = True
            finally:
                with self._lock:
                    self._inflight.discard(k)
                self._q.task_done()

    def wait_idle(self, timeout: float = 15.0) -> bool:
        """等后台队列清空（离线测试/脚本用）。"""
        t0 = time.time()
        while self._q is not None and self._q.unfinished_tasks and \
                time.time() - t0 < timeout:
            time.sleep(0.05)
        return not (self._q and self._q.unfinished_tasks)

    # ------------------------------------------------------------- 同步调用
    def read(self, tile_bgr: np.ndarray) -> VlmResult:
        """同步调一次模型。实时链路请用 peek/warm，别直接调这个。"""
        if not self.available:
            return VlmResult(error="未配置")
        t0 = time.time()
        img = _encode(tile_bgr, self.max_side)
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 8,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{img}"}},
                ],
            }],
        }
        body = json.dumps(payload).encode()
        last = ""
        for attempt in range(2):
            req = urllib.request.Request(
                self.base_url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                self.calls += 1
                text = _extract_text(data)
                letter = _parse_letter(text)
                self.last_error = ""
                return VlmResult(letter=letter, raw=text,
                                 ms=int((time.time() - t0) * 1000))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                last = f"HTTP {exc.code} {detail}"
                if exc.code in (401, 403):
                    break                        # key 不对，重试没意义
            except Exception as exc:             # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(0.4)
        self.last_error = last
        return VlmResult(error=last, ms=int((time.time() - t0) * 1000))

    def stats(self) -> dict:
        return {"model": self.model if self.available else "",
                "available": self.available,
                "why": self.why_unavailable(),
                "calls": self.calls, "hits": self.hits,
                "cached": len(self.cache),
                "queued": self._q.qsize() if self._q else 0,
                "inflight": len(self._inflight),
                "failures": self.failures,
                "given_up": self.given_up,
                "last_error": self.last_error,
                "backoff": round(max(0.0, self._backoff_until - time.time()), 2)}


# ------------------------------------------------------------------ 工具
def _resolve_secret(value: str) -> str:
    """支持 "env:VAR" 写法，别把 key 硬编码进仓库。"""
    v = (value or "").strip()
    if v.startswith("env:"):
        return (os.environ.get(v[4:].strip()) or "").strip()
    return v


def _encode(tile_bgr: np.ndarray, max_side: int) -> str:
    h, w = tile_bgr.shape[:2]
    if max(h, w) > max_side:
        s = max_side / float(max(h, w))
        tile_bgr = cv2.resize(tile_bgr, (max(8, int(w * s)), max(8, int(h * s))),
                              interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", tile_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise RuntimeError("图片编码失败")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _extract_text(data: dict) -> str:
    """尽量从各家略有差异的返回里挖出正文。"""
    try:
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):        # 少数实现返回分块
                return "".join(p.get("text", "") for p in content
                               if isinstance(p, dict))
            if msg.get("reasoning_content"):
                return str(msg["reasoning_content"])
    except (AttributeError, TypeError, IndexError):
        pass
    # 兼容 {"output": {"text": ...}}
    out = data.get("output")
    if isinstance(out, dict) and isinstance(out.get("text"), str):
        return out["text"]
    if isinstance(data.get("text"), str):
        return data["text"]
    return ""


def _parse_letter(text: str) -> str:
    """从模型输出里抠出一个大写字母。"""
    t = (text or "").strip()
    if not t:
        return ""
    # 先看有没有明确说"看不出来"
    if t in {"?", "？"} or "看不出" in t or "无法判断" in t:
        return ""
    m = _LETTER_RE.search(t.upper())
    return m.group(0) if m else ""
