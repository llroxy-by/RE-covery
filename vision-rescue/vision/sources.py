"""取像层：VideoSource / OpenCvSource / MockSource / CameraAdapter。

硬约束（任务书第 4、9 节）：
- 摄像头打开失败必须显式抛错，禁止静默回退 mock；
- 读帧失败必须报错，禁止用上一帧冒充实时画面；
- mock 只能由调用方显式选择（--mode mock），且帧里带 MOCK 水印。
"""

from __future__ import annotations

import abc
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

try:  # cv2 缺失时给出人话提示，而不是 ImportError 栈
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


class SourceError(RuntimeError):
    """取像失败。调用方应终止本次快照，而不是复用旧帧或切 mock。"""


def utc_now_iso() -> str:
    """UTC ISO 8601，毫秒精度，带 Z 后缀（schema 的 date-time 格式）。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _now_iso_from(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _strip_credentials(url: str | None) -> str:
    """日志/JSON 里不写 URL 中的账号密码（IP Webcam 常带 basic auth）。"""
    if not url:
        return ""
    return re.sub(r"//[^/@]*@", "//", url)


@dataclass(frozen=True)
class Frame:
    """一帧图像 + 采集时刻 + 来源描述。全模块只消费这个结构。"""

    image: np.ndarray
    captured_at: str
    source: str
    mode: str
    index: int

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    def describe(self) -> str:
        return f"{self.source} {self.width}x{self.height} @{self.captured_at}"


class VideoSource(abc.ABC):
    """取像源接口。换相机只替换实现，检测与差异逻辑不动。"""

    mode: str = "real"
    source: str = "unknown"

    @abc.abstractmethod
    def read(self) -> Frame:
        """返回新的一帧。失败必须抛 SourceError，绝不返回旧帧。"""

    @abc.abstractmethod
    def close(self) -> None:
        """释放摄像头/文件句柄。可重复调用。"""

    def __enter__(self) -> "VideoSource":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FrozenFrameGuard:
    """冻结画面守卫。

    摄像头被别的程序独占、虚拟摄像头在放静图、驱动卡死时，`read()` 仍会返回 True
    且每帧都一样。这种"假实时"比读帧失败更危险：S1 会与 S0 完全相同，直接被判
    unchanged 而**错误放行**。所以连续多帧像素完全一致必须报错。
    """

    def __init__(self, max_identical: int = 5) -> None:
        self.max_identical = max(1, int(max_identical))
        self._last = None
        self._count = 0

    def check(self, image: np.ndarray) -> None:
        if self._last is not None and np.array_equal(self._last, image):
            self._count += 1
        else:
            self._count = 0
        self._last = image.copy()
        if self._count >= self.max_identical:
            raise SourceError(
                f"画面连续 {self._count + 1} 帧完全一致，疑似冻结画面/虚拟摄像头，"
                "拒绝继续生成快照（避免用旧帧冒充实时画面）"
            )


def fit_to_resolution(frame: np.ndarray, target: tuple[int, int] | None) -> np.ndarray:
    """把流分辨率对齐到配置分辨率：分区坐标是按配置分辨率标的，不对齐就全错位。

    宽高比一致 → 缩放；宽高比不同 → 拒绝（缩放会把分区坐标整体扯歪，
    这种"看起来能跑但位置全偏"的错误比报错危险得多）。
    """
    if target is None:
        return frame
    tw, th = int(target[0]), int(target[1])
    h, w = frame.shape[:2]
    if (w, h) == (tw, th):
        return frame
    if abs(w / h - tw / th) / (tw / th) > 0.02:
        raise SourceError(
            f"视频流是 {w}x{h}（宽高比 {w / h:.2f}），配置是 {tw}x{th}（{tw / th:.2f}），"
            "宽高比不同不能自动缩放：请把 config 的 resolution 改成流的分辨率，"
            "并重新标定五个区域"
        )
    return cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)


class OpenCvSource(VideoSource):
    """本机内置/USB 摄像头，或手机 IP 摄像头的 MJPEG 视频流。

    两种输入：
    - `camera_index=1` → `cv2.VideoCapture(1)`，系统摄像头
    - `url="http://192.168.1.7:8080/video"` → 直接读手机 App 提供的流，
      不用装 Windows 驱动/虚拟摄像头，换手机也不影响代码。
    """

    def __init__(
        self,
        camera_index: int = 0,
        resolution: tuple[int, int] | None = None,
        warmup_frames: int = 5,
        allow_frozen: bool = False,
        url: str | None = None,
    ) -> None:
        if cv2 is None:
            raise SourceError(
                "未安装 opencv-python，无法取像。请执行："
                "python -m pip install -r vision/requirements.txt"
            )
        self.mode = "real"
        self._index = int(camera_index)
        self._url = url
        self.source = f"opencv:url={_strip_credentials(url)}" if url else f"opencv:camera={camera_index}"
        self._frame_index = 0
        self._guard = None if allow_frozen else FrozenFrameGuard()

        self._cap = cv2.VideoCapture(url) if url else cv2.VideoCapture(self._index)
        if not self._cap.isOpened():
            target = f"手机视频流 {_strip_credentials(url)}" if url else f"摄像头 {self._index}"
            raise SourceError(
                f"无法打开{target}："
                + ("检查手机 App 是否已在推流、手机与电脑是否同一 WiFi、URL 端口是否正确"
                   if url else "检查系统摄像头权限、是否被会议软件占用，或换一个编号（0/1/2）")
            )
        if resolution is not None:
            w, h = int(resolution[0]), int(resolution[1])
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            self._target = (w, h)
        else:
            self._target = None
        self._resize_noted = False
        # 丢弃前若干帧：自动曝光/白平衡收敛需要时间。这是"预热"，不是"用旧帧冒充"。
        for _ in range(max(0, int(warmup_frames))):
            self._cap.read()

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        if self._target is None:
            return frame
        h, w = frame.shape[:2]
        tw, th = self._target
        if (w, h) != (tw, th) and not self._resize_noted:
            # 只在真正要缩放时提示一次，避免每帧刷屏
            out = fit_to_resolution(frame, self._target)
            print(f"[SRC] 流 {w}x{h} → 缩放为配置分辨率 {tw}x{th}（分区坐标仍然有效）",
                  flush=True)
            self._resize_noted = True
            return out
        return fit_to_resolution(frame, self._target)

    def read(self) -> Frame:
        ts = time.time()
        ok, frame = self._cap.read()
        if not ok or frame is None or frame.size == 0:
            raise SourceError("读帧失败（手机断流/摄像头掉线），停止生成本次快照（不复用旧帧）")
        if self._guard is not None:
            self._guard.check(frame)
        frame = self._fit(frame)
        self._frame_index += 1
        return Frame(
            image=frame,
            captured_at=_now_iso_from(ts),
            source=self.source,
            mode=self.mode,
            index=self._frame_index,
        )

    def close(self) -> None:
        if getattr(self, "_cap", None) is not None:
            self._cap.release()
            self._cap = None

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


class CameraAdapter(VideoSource):
    """把"任意返回 BGR ndarray 的回调"包装成 VideoSource。

    未来接入 GALBOT 腕部相机时，只需提供 provider（例如 SDK 取图后返回 ndarray），
    检测、快照、差异、界面全部不用改。相机换了必须重标定（分辨率/HSV/区域）。
    """

    def __init__(
        self,
        frame_provider,
        descriptor: str,
        mode: str = "real",
        resolution: tuple[int, int] | None = None,
    ) -> None:
        self._provider = frame_provider
        self.source = descriptor
        self.mode = mode
        self._resolution = resolution
        self._frame_index = 0

    def read(self) -> Frame:
        ts = time.time()
        try:
            image = self._provider()
        except Exception as exc:  # 适配层不吞异常，统一转成取像失败
            raise SourceError(f"相机适配器取图失败：{exc}") from exc
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
            raise SourceError("相机适配器返回的不是 HxWx3 的 BGR ndarray")
        if self._resolution is not None:
            w, h = int(self._resolution[0]), int(self._resolution[1])
            if (image.shape[1], image.shape[0]) != (w, h):
                raise SourceError(
                    f"分辨率与标定不符：实际 {image.shape[1]}x{image.shape[0]}，"
                    f"标定 {w}x{h}。换相机/改分辨率后必须重新标定"
                )
        self._frame_index += 1
        return Frame(image=image, captured_at=_now_iso_from(ts),
                     source=self.source, mode=self.mode, index=self._frame_index)

    def close(self) -> None:
        self._provider = None


# ---------------------------------------------------------------- mock 源

MOCK_SCENARIOS = ("ok", "moved", "swapped", "occluded", "extra", "dim")
_MOCK_BLOCK_COLOR = {
    "red": (0, 0, 220),
    "green": (0, 180, 0),
    "blue": (220, 80, 0),
}
# 橙：配置里没有的颜色。别再用紫 (200,0,200) —— 那张真实桌面就是紫红的
# （H 中位 123），紫色异物会被 background 一起扣掉，测试就成了"因为桌面不是紫
# 才通过"。测试要验的是"能发现多出来的东西"，不是"碰巧桌面颜色不一样"。
_MOCK_UNKNOWN_COLOR = (0, 140, 255)


class MockSource(VideoSource):
    """显式 --mode mock 才能用。只用于在没有摄像头时跑通链路与做回归。"""

    mode = "mock"
    # 注意：source 描述的是"哪一路相机"，不随场景变化。
    # 如果把场景名写进 source，S0/S1 会被判成 source_mismatch，害得每次比较都 uncertain。
    SOURCE_NAME = "mock:desktop"

    def __init__(self, zones: dict, resolution, scenario: str = "ok") -> None:
        if cv2 is None:
            raise SourceError("未安装 opencv-python，mock 源也无法绘制")
        self.source = self.SOURCE_NAME
        self._zones = {k: np.asarray(v, dtype=np.int32) for k, v in zones.items()}
        self._w, self._h = int(resolution[0]), int(resolution[1])
        self._scenario = scenario if scenario in MOCK_SCENARIOS else "ok"
        self._frame_index = 0

    def set_scenario(self, scenario: str) -> None:
        if scenario not in MOCK_SCENARIOS:
            raise SourceError(f"未知 mock 场景：{scenario}（可选 {', '.join(MOCK_SCENARIOS)}）")
        self._scenario = scenario

    def next_scenario(self) -> str:
        i = (MOCK_SCENARIOS.index(self._scenario) + 1) % len(MOCK_SCENARIOS)
        self.set_scenario(MOCK_SCENARIOS[i])
        return self._scenario

    @property
    def scenario(self) -> str:
        return self._scenario

    def describe(self) -> str:
        return f"{self.source} scenario={self._scenario}"

    def _centroid(self, name: str):
        pts = self._zones[name]
        return int(pts[:, 0].mean()), int(pts[:, 1].mean())

    def _block(self, img, zone: str, color, size: int = 96, jitter: int = 3,
               offset: tuple[int, int] = (0, 0)) -> None:
        cx, cy = self._centroid(zone)
        cx += int(offset[0]) + int(np.random.randint(-jitter, jitter + 1))
        cy += int(offset[1]) + int(np.random.randint(-jitter, jitter + 1))
        cv2.rectangle(img, (cx - size // 2, cy - size // 2),
                      (cx + size // 2, cy + size // 2), color, -1)

    def _paint(self) -> np.ndarray:
        # 桌面：中灰 + 轻微噪声，模拟真实纹理
        img = np.full((self._h, self._w, 3), 105, dtype=np.uint8)
        img = cv2.add(img, np.random.randint(0, 7, img.shape, dtype=np.uint8))
        for name, poly in self._zones.items():
            cv2.fillPoly(img, [poly], (128, 128, 128))
            cv2.polylines(img, [poly], True, (70, 70, 70), 2)
            cx, cy = self._centroid(name)
            cv2.putText(img, name, (cx - 14, cy + 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 60), 1, cv2.LINE_AA)

        sc = self._scenario
        if sc == "ok":
            self._block(img, "A", _MOCK_BLOCK_COLOR["red"])
            self._block(img, "B", _MOCK_BLOCK_COLOR["green"])
            self._block(img, "C", _MOCK_BLOCK_COLOR["blue"])
        elif sc == "moved":
            # 只有红块从 A 挪到 B（错开绿块，避免互相遮挡），绿/蓝不动
            self._block(img, "B", _MOCK_BLOCK_COLOR["red"], offset=(0, 90))
            self._block(img, "B", _MOCK_BLOCK_COLOR["green"])
            self._block(img, "C", _MOCK_BLOCK_COLOR["blue"])
        elif sc == "swapped":
            self._block(img, "B", _MOCK_BLOCK_COLOR["red"])
            self._block(img, "A", _MOCK_BLOCK_COLOR["green"])
            self._block(img, "C", _MOCK_BLOCK_COLOR["blue"])
        elif sc == "occluded":
            self._block(img, "A", _MOCK_BLOCK_COLOR["red"])
            self._block(img, "B", _MOCK_BLOCK_COLOR["green"])
            cx, cy = self._centroid("C")  # 蓝块被一块灰色纸板完全盖住
            cv2.rectangle(img, (cx - 80, cy - 80), (cx + 80, cy + 80), (150, 150, 150), -1)
        elif sc == "extra":
            self._block(img, "A", _MOCK_BLOCK_COLOR["red"])
            self._block(img, "B", _MOCK_BLOCK_COLOR["green"])
            self._block(img, "C", _MOCK_BLOCK_COLOR["blue"])
            self._block(img, "TEMP", _MOCK_UNKNOWN_COLOR)
        elif sc == "dim":
            self._block(img, "A", _MOCK_BLOCK_COLOR["red"])
            self._block(img, "B", _MOCK_BLOCK_COLOR["green"])
            self._block(img, "C", _MOCK_BLOCK_COLOR["blue"])
            img = cv2.convertScaleAbs(img, alpha=0.35, beta=10)  # 光线骤暗

        cv2.putText(img, f"MOCK/{sc}", (16, 34), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 0, 255), 2, cv2.LINE_AA)
        return img

    def read(self) -> Frame:
        self._frame_index += 1
        return Frame(image=self._paint(), captured_at=utc_now_iso(),
                     source=self.source, mode=self.mode, index=self._frame_index)

    def close(self) -> None:
        return None


def create_source(mode: str, camera_index: int, config: dict,
                  allow_frozen: bool = False, url: str | None = None) -> VideoSource:
    """按显式 mode 创建取像源。real 失败就报错，绝不自动降级成 mock。"""
    resolution = tuple(config.get("resolution", [1280, 720]))
    if mode == "real":
        return OpenCvSource(camera_index, resolution, allow_frozen=allow_frozen, url=url)
    if mode == "mock":
        return MockSource(config.get("zones", {}), resolution)
    raise SourceError(f"未知的 --mode {mode!r}，只允许 real 或 mock")
