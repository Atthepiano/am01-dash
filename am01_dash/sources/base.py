"""
Source 抽象基类。

任何"内容源"（静态图、GIF、视频、turing 主题、终端可视化...）
都实现这个接口，再被 runner 调度推到副屏。

设计要点：
  * pull 模型：runner 主循环按 target_fps 节奏调 next_frame()
  * 单源约定：next_frame 必须线程安全；如果源内部用子进程，
    在 start()/stop() 里管理生命周期
  * 帧返回 PIL.Image (任何模式、任何尺寸)，由 LcdAm01s._fit_to_hw 缩放
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image


@dataclass
class SourceInfo:
    """元数据，给 GUI 列表用。"""
    kind: str                       # 'image' / 'gif' / 'video' / 'theme' / 'playlist' / ...
    name: str                       # 用户可见的标签
    path: Optional[str] = None      # 源文件（如果有）
    duration_ms: Optional[int] = None  # 单次播放时长（None = 永久 / 无穷）


class Source(ABC):
    """
    内容源接口。

    子类至少要实现 next_frame()；start/stop 默认空实现。
    """
    # 子类可覆盖
    target_fps: int = 8        # 期望渲染帧率（runner 用来定 sleep）
    is_animated: bool = True   # False = next_frame 返回同一帧多次（如静态图）

    # 缩放模式，runner 用来决定 fit_to 怎么处理这个源的帧。
    # 'contain' = 适配长边、留黑边（默认，原画不变形）
    # 'cover'   = 适配短边、裁切超出（无黑边，但可能裁掉边缘）
    # 'stretch' = 强行拉伸到屏幕（会变形）
    fit_mode: str = "contain"

    def __init__(self, info: SourceInfo, fit_mode: str = "contain"):
        self.info = info
        self.fit_mode = fit_mode
        self._started = False

    # ---- 生命周期 ----
    def start(self) -> None:
        """启动源（开子进程、装载文件等）。runner 在第一次 next_frame 前调用。"""
        self._started = True

    def stop(self) -> None:
        """停止源、释放资源（关子进程、关文件等）。runner 切下一个源前调用。"""
        self._started = False

    # ---- 取帧 ----
    @abstractmethod
    def next_frame(self) -> Optional[Image.Image]:
        """
        返回下一帧 PIL.Image。
        返回 None 表示"本次没有新帧但源仍有效"（runner 会继续轮询）。
        如果源永久结束，is_exhausted() 应该返回 True。
        """
        ...

    def is_exhausted(self) -> bool:
        """
        源是否已经永久结束（不会再有新帧）。
        默认 False；视频/流子类在 EOF 时应返回 True。
        runner 看到 True 会切下一个源（playlist 模式）或停止（单源模式）。
        """
        return False

    # ---- 工具 ----
    @property
    def name(self) -> str:
        return self.info.name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.info.name!r}>"
