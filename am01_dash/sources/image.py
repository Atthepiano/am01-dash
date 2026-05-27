"""静态图片源 —— PNG/JPG/WebP 等任何 Pillow 支持的格式。"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from PIL import Image

from .base import Source, SourceInfo


class ImageSource(Source):
    """
    单张静态图片。
    next_frame() 只在首次返回图，之后返回 None（runner 收到 None 也不会切走，
    硬件画面会保持最后一帧不变）。

    runner 的轮播逻辑会按 info.duration_ms 在主循环里切下一个源，
    不依赖本源主动结束。
    """
    target_fps = 1       # 静态图不需要高帧率
    is_animated = False

    def __init__(self, path: str | Path, name: Optional[str] = None,
                 duration_ms: int = 5000, fit_mode: str = "contain"):
        path = Path(path)
        super().__init__(
            SourceInfo(
                kind="image",
                name=name or path.stem,
                path=str(path),
                duration_ms=duration_ms,
            ),
            fit_mode=fit_mode,
        )
        self._path = path
        self._cached: Optional[Image.Image] = None
        self._delivered = False

    def start(self) -> None:
        super().start()
        # 提前 load，避免第一次 next_frame 卡顿
        with Image.open(self._path) as im:
            self._cached = im.convert("RGB").copy()
        self._delivered = False

    def stop(self) -> None:
        super().stop()
        self._cached = None
        self._delivered = False

    def next_frame(self) -> Optional[Image.Image]:
        if self._cached is None:
            return None
        if self._delivered:
            return None        # 已经送过了，硬件保持上一帧
        self._delivered = True
        return self._cached
