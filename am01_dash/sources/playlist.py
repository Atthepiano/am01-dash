"""轮播队列 —— 把多个 Source 串成一个 Source 给 runner 用。

特性：
  * 每个子源播放 duration_ms 后自动切下一个（用子源的 duration 或 default）
  * 循环到末尾自动回到开头
  * 子源切换时自动 stop()/start()，确保资源被正确管理
"""
from __future__ import annotations
import time
from typing import List, Optional

from PIL import Image

from .base import Source, SourceInfo


class PlaylistSource(Source):
    """
    一个 Source 的有序列表，按时长轮播。

    target_fps 取所有子源的最大值（保证最高刷的子源能跑顺）。
    """
    is_animated = True

    def __init__(self, sources: List[Source], name: str = "Playlist",
                 default_duration_ms: int = 5000, loop: bool = True):
        if not sources:
            raise ValueError("PlaylistSource 至少需要一个子源")
        # PlaylistSource 自己不缩放，由子源各自的 fit_mode 决定
        super().__init__(
            SourceInfo(kind="playlist", name=name, duration_ms=None),
            fit_mode="passthrough",
        )
        self._sources = list(sources)
        self._default_dur = default_duration_ms
        self._loop = loop
        self.target_fps = max(s.target_fps for s in self._sources)
        # 状态
        self._idx = -1
        self._current: Optional[Source] = None
        self._switch_at: float = 0.0   # monotonic 时刻
        self._pending_first_frame = False
        self._pending_dur_ms: Optional[int] = None

    # ---- 内部 ----
    # 涉及 terminal 源切换时插入的"喘息间隔"，让 ms912x heartbeat 完成残留 buffer
    # 的最后一两次推送，避免新源的画面和旧源的尾巴在 USB 链路上挤撞
    _TERMINAL_SWITCH_GAP_S = 0.3

    def _activate(self, idx: int) -> None:
        """切到第 idx 个子源；负责 stop 旧的、start 新的。"""
        prev_kind = getattr(self._current.info, "kind", None) if self._current else None

        if self._current is not None:
            try:
                self._current.stop()
            except Exception:
                pass
            self._current = None

        if idx < 0 or idx >= len(self._sources):
            return

        next_src = self._sources[idx]
        next_kind = getattr(next_src.info, "kind", None)

        # 防御性间隔：只在切入或切出 terminal 源时短暂等待
        if prev_kind == "terminal" or next_kind == "terminal":
            time.sleep(self._TERMINAL_SWITCH_GAP_S)

        self._idx = idx
        next_src.start()
        self._current = next_src
        src = next_src

        # 标记"等首帧"：先把 _switch_at 设到无穷远，等 next_frame 真的拿到
        # 第一帧（说明源完成了启动 + 第一次绘制）才开始算时长。
        # 这能避免 htop/btop 这类 PTY 启动慢的源被"启动时间"吃掉用户设的 duration。
        self._pending_first_frame = True
        self._pending_dur_ms = src.info.duration_ms

        # 决定何时切下一个（_pending_first_frame 标记下，先放无穷远占位）：
        dur_ms = src.info.duration_ms
        if dur_ms is not None and dur_ms > 0:
            # 等到出第一帧才设真正的 switch_at；先放无穷远
            self._switch_at = float("inf")
        else:
            # 没指定时长。看源自己有没有自然结束：
            #   * 静态源（image/gif，is_exhausted 永远 False）→ 用 default_dur 兜底
            #   * 动态源（video/stream，会 is_exhausted）→ 设无穷远，让 next_frame
            #     里的 is_exhausted 检查负责切换
            kind = getattr(src.info, "kind", "")
            if kind in ("video", "stream"):
                self._switch_at = float("inf")
            else:
                self._switch_at = time.monotonic() + self._default_dur / 1000.0

    def _advance(self) -> None:
        nxt = self._idx + 1
        if nxt >= len(self._sources):
            if not self._loop:
                self._activate(-1)   # 全停
                return
            nxt = 0
        self._activate(nxt)

    # ---- 生命周期 ----
    def start(self) -> None:
        super().start()
        if not self._sources:
            return
        self._activate(0)

    def stop(self) -> None:
        if self._current is not None:
            try:
                self._current.stop()
            except Exception:
                pass
            self._current = None
        self._idx = -1
        super().stop()

    # ---- 取帧 ----
    def next_frame(self) -> Optional[Image.Image]:
        if self._current is None:
            return None
        # 子源结束 → 提前切下一个（不等时长到点）
        if self._current.is_exhausted():
            self._advance()
            if self._current is None:
                return None
        # 时间到了就切
        elif time.monotonic() >= self._switch_at:
            self._advance()
            if self._current is None:
                return None
        frame = self._current.next_frame()
        # 拿到第一帧 → 现在才开始算时长
        if frame is not None and self._pending_first_frame:
            self._pending_first_frame = False
            dur_ms = self._pending_dur_ms
            if dur_ms is not None and dur_ms > 0:
                self._switch_at = time.monotonic() + dur_ms / 1000.0
        return frame

    def is_exhausted(self) -> bool:
        # playlist 全跑完且不循环
        return self._current is None and self._started is False

    # ---- 管理 ----
    def add(self, src: Source) -> None:
        self._sources.append(src)
        self.target_fps = max(s.target_fps for s in self._sources)

    def __len__(self) -> int:
        return len(self._sources)
