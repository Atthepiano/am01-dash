#!/usr/bin/env python3
"""
最小测试：独占副屏并显示一帧测试图。

运行前提：
  1. ms912x 内核驱动已加载
  2. KWin 没有占用 /dev/dri/card0（要么 udev 规则已生效，要么手动 kscreen-doctor disable）
  3. 当前用户在 am01dash 组里，或临时用 sudo 跑

  sudo python3 examples/01_test_modeset.py
"""
import os
import sys
import time

# 让脚本能 import 同 repo 的 am01_dash 包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw, ImageFont

from am01_dash import DrmDevice, Surface


def make_test_image(w, h) -> Image.Image:
    img = Image.new("RGB", (w, h), (10, 10, 30))
    d = ImageDraw.Draw(img)

    # 渐变色条
    for x in range(w):
        c = int(255 * x / w)
        d.line([(x, 0), (x, 40)], fill=(c, 0, 0))
        d.line([(x, 40), (x, 80)], fill=(0, c, 0))
        d.line([(x, 80), (x, 120)], fill=(0, 0, c))

    # 中央大字
    try:
        font = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Regular.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    text = "AM01S 960x400"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    d.text(((w - tw) / 2, (h - th) / 2), text, fill=(255, 255, 255), font=font)

    # 四角十字标记，方便看是否对齐到边界
    cross = 20
    for cx, cy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        d.line([(cx - cross, cy), (cx + cross, cy)], fill=(255, 255, 0))
        d.line([(cx, cy - cross), (cx, cy + cross)], fill=(255, 255, 0))
    d.rectangle([(0, 0), (w - 1, h - 1)], outline=(255, 255, 0), width=2)
    return img


def main():
    path = "/dev/dri/card0"
    print(f"[*] 打开 {path}")
    with DrmDevice(path) as dev:
        print(f"[*] modeset...")
        dev.modeset()
        print(f"[+] 模式: {dev.width}x{dev.height} (mode name: {dev.mode.name.decode(errors='replace')!r})")
        print(f"[+] connector_id={dev.connector.id} encoder_id={dev.encoder.id} crtc_id={dev.crtc_id}")

        print(f"[*] 创建 Surface 并绘制测试图")
        surf = Surface(dev)
        surf.image = make_test_image(surf.width, surf.height)

        print(f"[*] commit -> 推到屏幕")
        surf.commit()

        print(f"[+] 屏幕上现在应该看到渐变色 + AM01S 字样")
        print(f"[*] 保持 10 秒...")
        time.sleep(10)

    print(f"[+] 已释放设备")


if __name__ == "__main__":
    main()
