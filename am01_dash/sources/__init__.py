"""
am01_dash 内容源集合。

支持的源类型：
  * ImageSource    - 静态图片 (PNG/JPG/WebP/...)
  * GifSource      - 动图 (GIF / animated WebP / APNG)
  * VideoSource    - 本地视频文件 (mp4/webm/mkv/...)
  * StreamSource   - 网络流 (HTTP / RTSP / HLS / YouTube 等)
  * TerminalSource - 任意 TUI 程序 (htop / btop / cmatrix / ...)
  * PlaylistSource - 多源轮播
"""
from .base import Source, SourceInfo
from .gif import GifSource
from .image import ImageSource
from .playlist import PlaylistSource
from .stream import StreamSource
from .terminal import TerminalSource
from .video import VideoSource

__all__ = [
    "Source", "SourceInfo",
    "ImageSource", "GifSource", "VideoSource", "StreamSource",
    "TerminalSource", "PlaylistSource",
]
