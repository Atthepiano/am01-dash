"""
本地视频源 —— 通过 ffmpeg 子进程解码 + 缩放成 raw RGB 帧。

设计：
  * 启动前用 ffprobe 探测源宽高，按"等比缩放到 ≤ hw"算出固定输出尺寸
    -> ffmpeg 输出帧尺寸固定 -> Python read 知道每帧多少字节
  * 关键：保留视频原宽高比！这样 runner 的 fit_to 才有机会做 contain/cover/stretch 区分
  * stderr 写到 /tmp 里方便排错
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

from .base import Source, SourceInfo

logger = logging.getLogger("am01_dash.video")

# AM01S 物理尺寸
DEFAULT_HW_W = 960
DEFAULT_HW_H = 400


def _probe_video_info(path_or_url: str,
                      timeout: float = 10.0) -> Optional[dict]:
    """用 ffprobe 拿视频流的 width/height/fps。失败返回 None。"""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
             "-of", "json", str(path_or_url)],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        w = int(s.get("width") or 0)
        h = int(s.get("height") or 0)
        if w <= 0 or h <= 0:
            return None
        fps = _parse_fps(s.get("r_frame_rate") or s.get("avg_frame_rate"))
        return {"width": w, "height": h, "fps": fps}
    except Exception as e:
        logger.warning("ffprobe %s 失败: %s", path_or_url, e)
        return None


def _parse_fps(value: Optional[str]) -> Optional[float]:
    """把 ffprobe 的 'r_frame_rate' 字符串（如 '30000/1001'）解析成浮点。"""
    if not value or value == "0/0":
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            num, den = float(num), float(den)
            if den == 0:
                return None
            return num / den
        return float(value)
    except Exception:
        return None


# 向后兼容旧函数名
def _probe_video_size(path_or_url: str, timeout: float = 10.0
                      ) -> Optional[Tuple[int, int]]:
    info = _probe_video_info(path_or_url, timeout=timeout)
    if info:
        return (info["width"], info["height"])
    return None


def compute_fit_size(src_w: int, src_h: int,
                     hw_w: int, hw_h: int) -> Tuple[int, int]:
    """
    按"等比缩放使整张画面 ≤ hw"算输出尺寸。
    必然 ≤ hw_w × hw_h，且保留宽高比，且至少有一个边等于对应 hw 维度。
    （= 标准 contain 行为）
    保证偶数（很多视频编码要求）。
    """
    if src_w <= 0 or src_h <= 0:
        return hw_w, hw_h
    scale = min(hw_w / src_w, hw_h / src_h)
    out_w = max(2, int(src_w * scale))
    out_h = max(2, int(src_h * scale))
    # 偶数对齐
    out_w -= out_w % 2
    out_h -= out_h % 2
    return out_w, out_h


class VideoSource(Source):
    """
    本地视频源。

    target_fps: 视频按这个帧率从 ffmpeg 流里读（用 -r 让 ffmpeg 重采样）
                设小一点能省 USB 带宽（ms912x 极限大概 10fps 全帧）
    """
    # 上限（playback 不会超过这个值；硬件 / commit 节流也按这个）
    MAX_FPS = 60
    # 探测失败时的兜底
    FALLBACK_FPS = 30

    target_fps = 30
    is_animated = True

    def __init__(self, path: str | Path, name: Optional[str] = None,
                 duration_ms: Optional[int] = None, loop: bool = True,
                 fit_mode: str = "contain", fps: int = 0,
                 hw_w: int = DEFAULT_HW_W, hw_h: int = DEFAULT_HW_H):
        """
        fps: 强制帧率。
          * 0  -> 自适应：ffprobe 探测源帧率（24/30/60 等），上限 MAX_FPS
          * >0 -> 强制按这个值推（不超过 MAX_FPS）
        """
        path = Path(path)
        super().__init__(
            SourceInfo(
                kind="video",
                name=name or path.stem,
                path=str(path),
                duration_ms=duration_ms,
            ),
            fit_mode=fit_mode,
        )
        self._path = path
        self._loop = loop
        self._configured_fps = fps   # 用户配置原值；0=自适应
        # target_fps 实际值在 start() 里根据 _configured_fps + 探测结果决定
        self.target_fps = self._resolve_fps(fps, fallback=self.FALLBACK_FPS)
        self._hw_w = hw_w
        self._hw_h = hw_h

        # 这两项 start() 时根据探测结果赋值
        self._out_w = hw_w
        self._out_h = hw_h
        self._frame_bytes = hw_w * hw_h * 3

        # subprocess + reader thread
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._latest_frame: Optional[Image.Image] = None
        self._frame_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._last_returned_id = -1
        self._frame_id = 0
        self._exhausted = False   # 源是否已结束（ffmpeg 流读到 EOF）

    @classmethod
    def _resolve_fps(cls, configured: int,
                     fallback: Optional[float] = None) -> int:
        """把用户配置 / 探测结果转成最终 target_fps，并夹在 [1, MAX_FPS]。"""
        if configured and configured > 0:
            return max(1, min(cls.MAX_FPS, int(configured)))
        if fallback is not None and fallback > 0:
            return max(1, min(cls.MAX_FPS, int(round(fallback))))
        return cls.FALLBACK_FPS

    def _resolve_input(self) -> str:
        """子类可覆写以解析 URL 等。默认返回本地文件路径。"""
        if not self._path.is_file():
            raise FileNotFoundError(self._path)
        return str(self._path)

    def _probe_size(self, input_url: str) -> Tuple[int, int]:
        """探测源宽高（+ fps 可用时顺便更新 self.target_fps）。"""
        info = _probe_video_info(input_url, timeout=15.0)
        if info is None:
            logger.warning("[%s] ffprobe 失败，退回到 hw 尺寸（stretch 行为）",
                           self.name)
            return self._hw_w, self._hw_h
        w, h, src_fps = info["width"], info["height"], info.get("fps")
        # fps=0（自适应模式）才用探测值；用户明确指定的 fps 不覆盖
        if (not self._configured_fps or self._configured_fps <= 0) and src_fps:
            new_fps = self._resolve_fps(0, fallback=src_fps)
            if new_fps != self.target_fps:
                logger.info("[%s] 自适应帧率: 源 %.2f fps -> target %d fps",
                            self.name, src_fps, new_fps)
                self.target_fps = new_fps
        logger.info("[%s] 源视频 %dx%d @ %s fps",
                    self.name, w, h,
                    f"{src_fps:.2f}" if src_fps else "?")
        return compute_fit_size(w, h, self._hw_w, self._hw_h)

    def _ffmpeg_input_args(self) -> list[str]:
        """子类可覆写：返回 ffmpeg -i 之前 + -i input 的部分。"""
        args = []
        # -re：按原始时间戳读源（实时速率），防止 ffmpeg 全速解码
        # 把整段视频几秒内"快进"完。这是本地文件 / HTTP MP4 等
        # 离线源必须的；实时流（rtsp/rtmp）本身就是实时的，不需要。
        args += ["-re"]
        if self._loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", self._resolve_input()]
        return args

    def start(self) -> None:
        super().start()
        # 保险：playlist 循环时这里需要确保从干净状态开始
        self._exhausted = False
        with self._frame_lock:
            self._latest_frame = None
            self._frame_id = 0
            self._last_returned_id = -1
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg 不在 PATH 里。请先安装 ffmpeg。")

        input_url = self._resolve_input()
        # 探测源尺寸，算等比缩放后的输出尺寸
        self._out_w, self._out_h = self._probe_size(input_url)
        self._frame_bytes = self._out_w * self._out_h * 3
        logger.info("[%s] ffmpeg 输出 %dx%d (%d bytes/帧)",
                    self.name, self._out_w, self._out_h, self._frame_bytes)

        # 构造 ffmpeg 命令行
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin"]
        cmd += self._ffmpeg_input_args()
        # -vf fps=N: 真正下采样到 N fps（不是 -r 那种"用 N 写时间戳"）
        #            这会丢/复制帧让输出严格在 N fps
        # scale: 缩放到我们要的尺寸
        # -fps_mode passthrough 不行，因为我们要严格匀速；不指定让 ffmpeg 默认 cfr
        cmd += [
            "-an",
            "-vf", f"fps={self.target_fps},scale={self._out_w}:{self._out_h}:flags=bilinear",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "pipe:1",
        ]
        logger.info("[%s] ffmpeg cmd: %s", self.name, " ".join(cmd))

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=self._frame_bytes * 4,
        )

        self._stop_flag.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"VideoSource[{self.name}]",
            daemon=True,
        )
        self._reader_thread.start()
        # 异步抽 stderr，避免 ffmpeg 因为 stderr pipe 满了卡死
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"VideoSource-stderr[{self.name}]",
            daemon=True,
        )
        self._stderr_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1)
            except Exception:
                pass
            self._proc = None
        for t in (self._reader_thread, self._stderr_thread):
            if t is not None:
                t.join(timeout=1.0)
        self._reader_thread = None
        self._stderr_thread = None
        with self._frame_lock:
            self._latest_frame = None
            self._frame_id = 0
            self._last_returned_id = -1
        # 关键：重置 exhausted 标志，让下一次 start() 能正常工作
        # 否则 playlist 循环回到这个源时，runner 立刻看到 is_exhausted=True 又切走
        self._exhausted = False
        super().stop()

    def _reader_loop(self):
        """后台线程：从 ffmpeg stdout 持续 read 整帧。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._exhausted = True
            return
        bytes_per_frame = self._frame_bytes
        size = (self._out_w, self._out_h)
        while not self._stop_flag.is_set():
            # 必须确保读够一整帧，否则 Image.frombytes 会失败
            chunks = []
            remaining = bytes_per_frame
            while remaining > 0 and not self._stop_flag.is_set():
                try:
                    data = proc.stdout.read(remaining)
                except Exception:
                    self._exhausted = True
                    return
                if not data:
                    logger.info("[%s] ffmpeg stdout EOF (源已结束)", self.name)
                    self._exhausted = True
                    return
                chunks.append(data)
                remaining -= len(data)
            if remaining > 0:
                self._exhausted = True
                return
            try:
                img = Image.frombytes("RGB", size, b"".join(chunks))
            except Exception as e:
                logger.warning("[%s] frombytes 失败: %s", self.name, e)
                continue
            with self._frame_lock:
                self._latest_frame = img
                self._frame_id += 1

    def _stderr_loop(self):
        """异步抽 stderr 并写到 logger，避免 buffer 满卡死。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while not self._stop_flag.is_set():
            try:
                line = proc.stderr.readline()
            except Exception:
                return
            if not line:
                return
            try:
                txt = line.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            if txt:
                # ffmpeg info 级别太啰嗦，只 log error / warning
                lower = txt.lower()
                if any(k in lower for k in ("error", "fail", "invalid", "no such")):
                    logger.error("[%s] ffmpeg: %s", self.name, txt)
                elif "warning" in lower:
                    logger.warning("[%s] ffmpeg: %s", self.name, txt)
                else:
                    logger.debug("[%s] ffmpeg: %s", self.name, txt)

    def next_frame(self) -> Optional[Image.Image]:
        with self._frame_lock:
            if self._frame_id == self._last_returned_id:
                return None
            self._last_returned_id = self._frame_id
            return self._latest_frame

    def is_exhausted(self) -> bool:
        return self._exhausted
