"""
am01_dash —— Ayaneo AM01S 副屏独占信息屏控制库

底层（裸 DRM）：
- DrmDevice：独占打开 ms912x 的 DRM card，做 modeset/dumb buffer/推帧
- Surface：一个 Pillow Image 风格的 960x400 画布，commit() 推到硬件

高层（内容引擎）：
- Runner：主循环，按 fps 拉 Source 帧，等比缩放 + 推副屏
- Source / ImageSource / GifSource / PlaylistSource：内容源（见 .sources）
"""
from .drm import DrmDevice
from .runner import Runner, fit_to
from .surface import Surface

__all__ = ["DrmDevice", "Surface", "Runner", "fit_to"]
__version__ = "0.2.0"
