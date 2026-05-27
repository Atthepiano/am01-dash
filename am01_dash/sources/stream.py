"""
流媒体源 —— HTTP / RTSP / HLS / 任何 ffmpeg 能吃的 URL。

支持：
  * 直链 (HTTP mp4/webm, m3u8, mpd, RTSP, RTMP, UDP, SRT...)
  * 需要解析的网页流 (YouTube, Bilibili 等) —— 通过 yt-dlp

继承 VideoSource，覆写 _resolve_input 和 _ffmpeg_input_args。

YouTube cookies:
  YouTube 要求登录 cookies。优先级:
    1. AM01_DASH_YTDLP_COOKIES 环境变量
    2. <am01_dash>/data/youtube_cookies.txt 固定路径
  导出方法:
    yt-dlp --cookies-from-browser firefox \\
           --cookies /path/to/youtube_cookies.txt \\
           --skip-download 'https://www.youtube.com/watch?v=...'
"""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .base import SourceInfo
from .video import VideoSource

logger = logging.getLogger("am01_dash.stream")


# 默认 cookies 文件路径
_AM01_DASH_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_COOKIES_PATH = _AM01_DASH_DIR / "data" / "youtube_cookies.txt"


def _find_cookies_file() -> Optional[str]:
    """按优先级找 cookies 文件路径，找不到返回 None。"""
    env = os.environ.get("AM01_DASH_YTDLP_COOKIES")
    if env and Path(env).is_file():
        return env
    if DEFAULT_COOKIES_PATH.is_file():
        return str(DEFAULT_COOKIES_PATH)
    return None


_DIRECT_PROTOS = ("rtsp://", "rtmp://", "udp://", "rtp://", "tcp://", "srt://")
_DIRECT_EXTS = (".m3u8", ".mpd", ".mp4", ".webm", ".mkv", ".ts", ".flv",
                ".mov", ".avi", ".m4v")


def _looks_like_direct_url(url: str) -> bool:
    """协议/扩展名启发式判定是不是 ffmpeg 直链。"""
    url_l = url.lower()
    if url_l.startswith(_DIRECT_PROTOS):
        return True
    if url_l.startswith(("http://", "https://")):
        head = url_l.split("?")[0]
        for ext in _DIRECT_EXTS:
            if head.endswith(ext) or ext in head:
                return True
    return False


def _resolve_url_via_ytdlp(url: str, max_height: int = 720,
                           timeout: float = 60.0) -> Optional[str]:
    """
    yt-dlp 把网页流解析成直链。失败返回 None。

    策略：
      * 优先选 "video only 不超过 max_height" —— 我们不要音频，纯 video 直链
        ffmpeg 喂起来最简单
      * 如果没纯 video 流，退回 "video+audio 合一" 格式
    """
    if not shutil.which("yt-dlp"):
        logger.warning("yt-dlp 不在 PATH，无法解析 %s", url[:60])
        return None
    # 用 "best[ext=mp4][height<=N]/best[height<=N]/best" 这种更宽容的格式串
    # 'best' 单流（不需要 ffmpeg 后期 mux）；副屏 400 高没必要 720
    fmt = (
        f"best[ext=mp4][height<={max_height}]/"
        f"best[height<={max_height}]/"
        f"best"
    )
    cmd = ["yt-dlp", "-f", fmt, "-g", "--no-warnings"]
    cookies = _find_cookies_file()
    if cookies:
        cmd += ["--cookies", cookies]
        logger.info("使用 cookies: %s", cookies)
    else:
        logger.warning("没找到 cookies 文件 (env AM01_DASH_YTDLP_COOKIES 或 %s)，"
                       "YouTube 之类需登录的网站可能失败",
                       DEFAULT_COOKIES_PATH)
    cmd.append(url)
    logger.info("yt-dlp 解析: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error("yt-dlp 失败 (rc=%d):\nstderr: %s",
                         r.returncode, r.stderr[-500:])
            return None
        # 输出可能 1-2 行（单流 1 行，video+audio 2 行）
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("http")]
        if not lines:
            logger.warning("yt-dlp 无 http 输出: %s", r.stdout[:300])
            return None
        if len(lines) > 1:
            logger.info("yt-dlp 返回 %d 个直链，取第一个 (video)", len(lines))
        logger.info("yt-dlp 解析成功: %s", lines[0][:100])
        return lines[0]
    except subprocess.TimeoutExpired:
        logger.error("yt-dlp 超时 (%.1fs)", timeout)
        return None
    except Exception as e:
        logger.error("yt-dlp 异常: %s", e)
        return None


class StreamSource(VideoSource):
    """
    远程流媒体源。
    """

    def __init__(self, url: str, name: Optional[str] = None,
                 duration_ms: Optional[int] = None, loop: bool = False,
                 fit_mode: str = "contain", fps: int = 0,
                 max_height: int = 720,
                 hw_w: int = 960, hw_h: int = 400):
        # 父类构造期望 path 是文件，但我们不会真的当文件用 —— 用 url 占位
        super().__init__(
            path=url,
            name=name or url[:64],
            duration_ms=duration_ms,
            loop=loop,
            fit_mode=fit_mode,
            fps=fps,
            hw_w=hw_w, hw_h=hw_h,
        )
        # 覆盖 info.kind
        self.info = SourceInfo(
            kind="stream", name=self.info.name, path=url,
            duration_ms=duration_ms,
        )
        self.fit_mode = fit_mode
        self._url = url
        self._max_height = max_height
        self._resolved_url: Optional[str] = None

    def _resolve_input(self) -> str:
        """覆写：网页流先用 yt-dlp 转直链；直链原样返回。"""
        if self._resolved_url:
            return self._resolved_url
        if _looks_like_direct_url(self._url):
            self._resolved_url = self._url
            logger.info("[%s] 使用直链: %s", self.name, self._url[:80])
        else:
            resolved = _resolve_url_via_ytdlp(self._url, self._max_height)
            if resolved is None:
                raise RuntimeError(
                    f"无法解析流地址: {self._url}\n"
                    f"如果是 YouTube/Bilibili 等网页流，请安装 yt-dlp：\n"
                    f"  sudo pacman -S yt-dlp"
                )
            self._resolved_url = resolved
            logger.info("[%s] yt-dlp 解析: %s -> %s",
                        self.name, self._url[:60], resolved[:80])
        return self._resolved_url

    def _ffmpeg_input_args(self) -> list[str]:
        """覆写：远程流的协议/重连/速率控制。"""
        url = self._resolve_input()
        args = []

        # 实时流（rtsp/rtmp/udp/srt/srt）已经是实时速率，不需要 -re
        # 离线 HTTP（mp4/m3u8 VOD）会被 ffmpeg 全速解码，必须加 -re
        is_live_proto = url.startswith(
            ("rtsp://", "rtmp://", "udp://", "rtp://", "srt://"))
        if not is_live_proto:
            args += ["-re"]

        # 远程协议特殊参数
        if url.startswith("rtsp://"):
            args += ["-rtsp_transport", "tcp"]
        if url.startswith(("http://", "https://")):
            args += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
            ]
        args += ["-rw_timeout", "10000000"]

        # loop 对实时流无意义；对 HTTP VOD 有意义
        if self._loop and not is_live_proto:
            args += ["-stream_loop", "-1"]

        args += ["-i", url]
        return args
