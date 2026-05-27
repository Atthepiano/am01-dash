"""
Pillow-friendly 画布封装（双缓冲版本）。

Surface 维护两块 DRM dumb buffer 轮换提交：
  * 每次 commit 写完数据后切到"另一块" framebuffer，
    然后 SETCRTC 绑定新的 fb_id
  * 这样 ms912x 内核驱动会把新 fb_id 当成"新内容"，
    重新读 mmap 数据编码到 USB transfer_buffer
  * 解决 legacy SETCRTC 反复绑定同一 fb 时驱动认为"画面没变"
    而继续推旧帧的问题
"""
from __future__ import annotations
from typing import List

from PIL import Image

from .drm import DrmDevice, DumbBuffer


class Surface:
    """双缓冲 PIL Image 画布。commit() 把 self.image 推到副屏。"""

    NUM_BUFFERS = 2

    def __init__(self, device: DrmDevice):
        if not device.mode:
            raise RuntimeError("DrmDevice 还没 modeset")
        self.device = device
        self.width = device.width
        self.height = device.height

        # 分配 N 块 dumb buffer
        self.buffers: List[DumbBuffer] = [
            device.create_framebuffer() for _ in range(self.NUM_BUFFERS)
        ]
        self._next_buf_idx = 0

        # 用户写画布（runner 把每帧赋值给 self.image）
        self.image: Image.Image = Image.new("RGB",
                                            (self.width, self.height),
                                            (0, 0, 0))

    def clear(self, color=(0, 0, 0)):
        self.image.paste(color, (0, 0, self.width, self.height))

    def commit(self):
        """
        把 self.image 转 XRGB8888 写入"下一块" dumb buffer 并 flip。
        通过轮换 fb_id 强制 ms912x 内核驱动重新读 mmap 数据。
        """
        img = self.image
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))

        # RGB → BGRX (DRM XRGB8888 little-endian = 字节序 B G R X)
        rgba = img.convert("RGBA")
        r, g, b, a = rgba.split()
        bgrx = Image.merge("RGBA", (b, g, r, a))
        raw = bgrx.tobytes()

        # 选下一块 buffer
        buf = self.buffers[self._next_buf_idx]
        self._next_buf_idx = (self._next_buf_idx + 1) % self.NUM_BUFFERS

        # 写入 mmap
        pitch = buf.pitch
        row_bytes = self.width * 4
        mm = buf.mmap_obj
        if pitch == row_bytes:
            mm.seek(0)
            mm.write(raw)
        else:
            for y in range(self.height):
                mm.seek(y * pitch)
                mm.write(raw[y * row_bytes:(y + 1) * row_bytes])

        # 推到硬件 —— 用新的 fb_id，让内核驱动感知到"新帧"
        self.device.flip_to(buf)
