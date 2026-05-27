"""动图源 —— GIF / 动 WebP / APNG，凡是 Pillow 多帧支持的都行。"""
from __future__ import annotations
import time
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageSequence

from .base import Source, SourceInfo


class GifSource(Source):
    """
    一个动图（GIF / animated WebP / APNG）。

    实现策略：
      * start() 时把所有帧解码到内存（[Image, duration_ms]）—— 简单粗暴
        但对于副屏用的几百帧、960×400 以内的小动图完全够用，避免 runner
        和 Pillow 解码并发的麻烦。
      * next_frame() 内部用 wall-clock 算"现在该返回哪帧"，按 GIF 自带
        的 per-frame duration 精确播放（不靠 target_fps）。
      * 默认循环播放；如果用户要单次播放结束后切下一个，runner 用
        info.duration_ms 控制即可。
    """
    target_fps = 30  # 高刷，让 GIF 帧切换平滑（runner 拉取上限）
    is_animated = True

    def __init__(self, path: str | Path, name: Optional[str] = None,
                 duration_ms: Optional[int] = None, loop: bool = True,
                 fit_mode: str = "contain"):
        path = Path(path)
        super().__init__(
            SourceInfo(
                kind="gif",
                name=name or path.stem,
                path=str(path),
                duration_ms=duration_ms,   # None = 永久循环
            ),
            fit_mode=fit_mode,
        )
        self._path = path
        self._loop = loop
        self._frames: List[Tuple[Image.Image, int]] = []  # (frame, duration_ms)
        self._total_ms = 0
        self._start_t = 0.0
        self._last_index = -1

    def start(self) -> None:
        super().start()
        self._frames = []
        with Image.open(self._path) as im:
            for f in ImageSequence.Iterator(im):
                # 拷贝出来（ImageSequence 内部共享缓冲）
                frame = f.convert("RGB").copy()
                dur = f.info.get("duration", 80)   # ms；默认 80ms ≈ 12.5fps
                if dur < 10:
                    dur = 10   # 太短的清理一下，避免 0
                self._frames.append((frame, dur))
        self._total_ms = sum(d for _, d in self._frames) or 1
        self._start_t = time.monotonic()
        self._last_index = -1

    def stop(self) -> None:
        super().stop()
        self._frames = []
        self._last_index = -1

    def next_frame(self) -> Optional[Image.Image]:
        if not self._frames:
            return None

        elapsed_ms = (time.monotonic() - self._start_t) * 1000

        if not self._loop and elapsed_ms >= self._total_ms:
            # 单次播放结束 —— 锁定最后一帧
            idx = len(self._frames) - 1
        else:
            # 在 [0, total_ms) 内取模算当前帧
            pos = elapsed_ms % self._total_ms
            acc = 0
            idx = 0
            for i, (_, d) in enumerate(self._frames):
                acc += d
                if pos < acc:
                    idx = i
                    break

        if idx == self._last_index:
            return None  # 当前还停在同一帧，不重画
        self._last_index = idx
        return self._frames[idx][0]
