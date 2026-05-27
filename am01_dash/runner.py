"""
Runner —— 把任意 Source 推到 AM01S 副屏的主循环。

设计：
  * 复用 turing-smart-screen-python 的 LcdAm01s 后端做底层渲染
    （它走 atomic commit，会触发 ms912x 内核驱动的 pipe_enable/update
    回调，从而启动心跳；裸 SETCRTC 不会触发，所以不能用）
  * 每 ~1/fps 拉一帧 → DisplayPILImage（带等比缩放）→ commit_loop 节流推送
  * 支持热切换 source、SIGINT/SIGTERM 优雅退出

跑法：
    from am01_dash import Runner
    from am01_dash.sources import GifSource

    runner = Runner()
    runner.set_source(GifSource("/path/to/cat.gif"))
    runner.run()   # 阻塞直到 SIGINT/SIGTERM
"""
from __future__ import annotations
import atexit
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from .sources.base import Source

logger = logging.getLogger("am01_dash.runner")

# 全局日志配置（让所有 am01_dash.* logger 都输出 INFO 到 stderr）
_root = logging.getLogger("am01_dash")
if not _root.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
    _root.addHandler(h)
    _root.setLevel(logging.INFO)


# ============ 工具：等比缩放居中（GUI 预览也会用） ============

def fit_to(image: Image.Image, target_w: int, target_h: int,
           mode: str = "contain", bg=(0, 0, 0)) -> Image.Image:
    """
    把 image 缩放到 target_w × target_h 画布上。

    mode:
        "contain" - 适配长边，等比缩放使整张画面可见，
                    比例不符时四周补 bg 黑边（默认）
        "cover"   - 适配短边，等比缩放铺满画布，
                    比例不符时超出部分被居中裁切
        "stretch" - 非等比拉伸到画布尺寸（会变形）
    """
    sw, sh = image.size
    if (sw, sh) == (target_w, target_h):
        return image if image.mode == "RGB" else image.convert("RGB")

    if mode == "stretch":
        scaled = image.resize((target_w, target_h), Image.LANCZOS)
        return scaled if scaled.mode == "RGB" else scaled.convert("RGB")

    if mode == "cover":
        # 取大的缩放比 → 铺满
        scale = max(target_w / sw, target_h / sh)
        new_w = max(1, int(sw * scale))
        new_h = max(1, int(sh * scale))
        scaled = image.resize((new_w, new_h), Image.LANCZOS)
        if scaled.mode != "RGB":
            scaled = scaled.convert("RGB")
        # 居中裁切到 target
        x0 = (new_w - target_w) // 2
        y0 = (new_h - target_h) // 2
        return scaled.crop((x0, y0, x0 + target_w, y0 + target_h))

    # 默认 / "contain"：等比缩放居中，留黑边
    scale = min(target_w / sw, target_h / sh)
    new_w = max(1, int(sw * scale))
    new_h = max(1, int(sh * scale))
    scaled = image.resize((new_w, new_h), Image.LANCZOS)
    if scaled.mode != "RGB":
        scaled = scaled.convert("RGB")
    out = Image.new("RGB", (target_w, target_h), bg)
    out.paste(scaled, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return out


# 合法 fit_mode 集合
FIT_MODES = ("contain", "cover", "stretch")


# ============ Runner ============

# 通过 turing 的 LcdAm01s 拿底层。需要 turing 在 PYTHONPATH 里。
# 路径用环境变量配置；helper 脚本会在 root 启动时设好。
def _load_lcd():
    """惰性 import LcdAm01s，避免 import 阶段就要求 turing 装好。"""
    turing_dir = os.environ.get("AM01_DASH_TURING_DIR")
    if turing_dir and turing_dir not in sys.path:
        sys.path.insert(0, turing_dir)
    try:
        from library.lcd.lcd_am01s import LcdAm01s
    except ImportError as e:
        raise RuntimeError(
            "找不到 LcdAm01s。请设置 AM01_DASH_TURING_DIR 环境变量指向 "
            "turing-smart-screen-python (am01s 分支) 的目录。\n"
            f"当前 AM01_DASH_TURING_DIR={turing_dir!r}\n"
            f"原始错误: {e}"
        ) from e
    return LcdAm01s


class Runner:
    """
    AM01S 副屏渲染主循环。线程安全：可以在主线程跑 run()，
    其他线程调 set_source() 切源。

    底层用 turing 的 LcdAm01s（走 atomic commit + 心跳）
    """

    # 硬件物理尺寸
    HW_W = 960
    HW_H = 400

    def __init__(self):
        self._lcd = None
        self._source: Optional[Source] = None
        self._source_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._started = False

    # ---- 设备 ----
    def _open_lcd(self) -> None:
        if self._lcd is not None:
            return
        logger.info("初始化 LcdAm01s ...")
        LcdAm01s = _load_lcd()
        # LcdAm01s 默认 400x960 (竖屏 base) → orientation=PORTRAIT 时是 400x960
        # 我们后面 SetOrientation(LANDSCAPE) → get_width=960, get_height=400 ✓
        self._lcd = LcdAm01s(display_width=400, display_height=960)
        # 强制横屏（让 get_width=960, get_height=400）
        from library.lcd.lcd_comm import Orientation
        self._lcd.SetOrientation(Orientation.LANDSCAPE)
        # 黑屏起步
        self._lcd.Clear()
        atexit.register(self._cleanup)
        logger.info("LcdAm01s 初始化完成: %dx%d",
                    self._lcd.get_width(), self._lcd.get_height())

    def _cleanup(self) -> None:
        # 停源
        with self._source_lock:
            src = self._source
            self._source = None
        if src is not None:
            try:
                src.stop()
            except Exception:
                pass
        # 关 LCD（会释放 DRM master + 黑屏）
        if self._lcd is not None:
            try:
                self._lcd.closeSerial()
            except Exception:
                pass
            self._lcd = None

    # ---- 源管理 ----
    def set_source(self, source: Optional[Source]) -> None:
        """热切换内容源。"""
        with self._source_lock:
            old = self._source
            self._source = source
        if old is not None and old is not source:
            try:
                old.stop()
            except Exception as e:
                logger.warning("旧源 stop 异常: %s", e)
        if source is not None:
            try:
                source.start()
                logger.info("切到新源: %s (fps=%d)", source.name, source.target_fps)
            except Exception as e:
                logger.error("新源 start 失败: %s", e)
                with self._source_lock:
                    if self._source is source:
                        self._source = None

    def current_source(self) -> Optional[Source]:
        with self._source_lock:
            return self._source

    # ---- 主循环 ----
    def stop(self) -> None:
        self._stop_flag.set()

    def run(self) -> None:
        """阻塞主循环。"""
        self._open_lcd()
        self._started = True

        def _sig(signum, frame):
            logger.info("收到信号 %d，准备退出", signum)
            self.stop()
        try:
            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)
        except ValueError:
            pass

        last_commit_t = 0.0
        last_frame_id = None

        logger.info("主循环开始")
        try:
            while not self._stop_flag.is_set():
                with self._source_lock:
                    src = self._source
                if src is None:
                    time.sleep(0.1)
                    continue

                interval = 1.0 / max(1, src.target_fps)
                wait = interval - (time.monotonic() - last_commit_t)
                if wait > 0:
                    if self._stop_flag.wait(wait):
                        break

                try:
                    frame = src.next_frame()
                except Exception as e:
                    logger.warning("源 %s next_frame 异常: %s", src.name, e)
                    time.sleep(0.1)
                    continue

                # 源永久结束 → 停止 runner（单源模式）。
                # playlist 模式不会到这里：playlist 在内部自己切下一个，
                # 整个 playlist 跑完才会 is_exhausted() 返回 True。
                if frame is None and src.is_exhausted():
                    logger.info("源 %s 已结束，停止 runner", src.name)
                    self.stop()
                    break

                if frame is None:
                    continue

                fid = id(frame)
                if fid == last_frame_id:
                    continue
                last_frame_id = fid

                # 推到 LCD —— 按 source 的 fit_mode 缩放
                try:
                    # 如果是 playlist，从当前活跃子源拿 fit_mode
                    fit_mode = getattr(src, "fit_mode", "contain")
                    if fit_mode == "passthrough":
                        # playlist 的情况：用其内部当前 source 的 fit_mode
                        cur = getattr(src, "_current", None)
                        if cur is not None:
                            fit_mode = getattr(cur, "fit_mode", "contain")
                        else:
                            fit_mode = "contain"
                    final = fit_to(frame, self.HW_W, self.HW_H, mode=fit_mode)
                    self._lcd.DisplayPILImage(final, 0, 0, self.HW_W, self.HW_H)
                    last_commit_t = time.monotonic()
                except Exception as e:
                    import traceback
                    logger.warning("DisplayPILImage 失败: %s\n%s",
                                   e, traceback.format_exc())
                    time.sleep(0.1)
        finally:
            self._cleanup()
            logger.info("runner 已退出")


# ============ 命令行入口 ============

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tiff")
GIF_EXTS = (".gif", ".webp", ".apng")
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".ts", ".m3u8", ".mpd")


def build_source_for_path(path: Path, duration_ms: int = 5000,
                          fit_mode: str = "contain") -> Optional[Source]:
    """根据文件扩展名自动选 Source 类型；不认识返回 None。"""
    from .sources import ImageSource, GifSource, VideoSource
    ext = path.suffix.lower()
    if ext in GIF_EXTS:
        return GifSource(path, duration_ms=duration_ms, fit_mode=fit_mode)
    if ext in IMAGE_EXTS:
        return ImageSource(path, duration_ms=duration_ms, fit_mode=fit_mode)
    if ext in VIDEO_EXTS:
        return VideoSource(path, duration_ms=duration_ms, fit_mode=fit_mode)
    return None


def build_source_from_dict(d: dict) -> Optional[Source]:
    """
    从 dict（JSON 反序列化）构造一个 Source。
    支持的 schema：
      {"kind": "image"|"gif", "path": "...", "duration_ms": 5000,
       "fit_mode": "contain", "name": "(可选)"}
      {"kind": "video", "path": "...", "duration_ms": 0,
       "fit_mode": "contain", "loop": false, "fps": 10}
      {"kind": "stream", "url": "...", "duration_ms": 0,
       "fit_mode": "contain", "fps": 10}
      {"kind": "playlist", "name": "...", "loop": true, "default_duration_ms": 5000,
       "items": [ <子项>, ... ]}

    约定：duration_ms = 0 或负数表示 "按源自然时长" —— 视频/流跑完为止，
    图片/GIF 在 playlist 里则用 default_duration_ms 兜底。
    """
    from .sources import (ImageSource, GifSource, PlaylistSource,
                          VideoSource, StreamSource)

    def _norm_duration(v):
        """0/负 → None（无限制 / 自然时长）。"""
        if v is None:
            return None
        try:
            v = int(v)
        except Exception:
            return None
        return v if v > 0 else None

    kind = d.get("kind")
    fit = d.get("fit_mode", "contain")
    raw_dur = d.get("duration_ms")
    if kind == "image":
        # 图片没有"自然时长"概念；duration=0/None 时给 5000 ms
        dur = _norm_duration(raw_dur)
        return ImageSource(
            Path(d["path"]).expanduser(),
            name=d.get("name"),
            duration_ms=dur if dur is not None else 5000,
            fit_mode=fit,
        )
    if kind == "gif":
        dur = _norm_duration(raw_dur)
        return GifSource(
            Path(d["path"]).expanduser(),
            name=d.get("name"),
            duration_ms=dur,    # None = 永远循环到下一个切换信号
            loop=bool(d.get("loop", True)),
            fit_mode=fit,
        )
    # fps=0 -> 让 VideoSource 自己用 ffprobe 自适应
    raw_fps = int(d.get("fps", 0) or 0)

    if kind == "video":
        return VideoSource(
            Path(d["path"]).expanduser(),
            name=d.get("name"),
            duration_ms=_norm_duration(raw_dur),
            loop=bool(d.get("loop", False)),
            fit_mode=fit,
            fps=raw_fps,
        )
    if kind == "stream":
        stream_url = d.get("url") or d.get("path")
        if not stream_url:
            raise ValueError(f"stream 源缺 'url' 或 'path': {d}")
        return StreamSource(
            url=stream_url,
            name=d.get("name"),
            duration_ms=_norm_duration(raw_dur),
            loop=bool(d.get("loop", False)),
            fit_mode=fit,
            fps=raw_fps,
            max_height=int(d.get("max_height", 720)),
        )
    if kind == "terminal":
        from .sources import TerminalSource
        cmd = d.get("command") or d.get("path")
        if not cmd:
            raise ValueError(f"terminal 源缺 'command' 或 'path': {d}")
        return TerminalSource(
            command=cmd,
            name=d.get("name"),
            duration_ms=_norm_duration(raw_dur),
            fit_mode=fit,
            fps=raw_fps,
            cols=d.get("cols"),
            rows=d.get("rows"),
            font_size=int(d.get("font_size", 14)),
        )
    if kind == "playlist":
        children = [s for s in
                    (build_source_from_dict(i) for i in d.get("items", []))
                    if s is not None]
        if not children:
            return None
        return PlaylistSource(
            children,
            name=d.get("name", "Playlist"),
            default_duration_ms=int(d.get("default_duration_ms", 5000)),
            loop=bool(d.get("loop", True)),
        )
    return None


def _main_cli():
    import argparse
    import json

    p = argparse.ArgumentParser(prog="am01_dash.runner",
                                description="把图片/GIF/播放列表推到 AM01S 副屏")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("target", nargs="?",
                   help="文件 (PNG/JPG/GIF...) 或目录（自动轮播）")
    g.add_argument("--playlist", "-p", type=str,
                   help="加载 JSON 播放列表文件")
    p.add_argument("--fit", choices=FIT_MODES, default="contain",
                   help="缩放模式：contain=适配长边留黑边(默认), "
                        "cover=适配短边铺满裁切, stretch=拉伸变形")
    p.add_argument("--image-duration", type=int, default=5000,
                   help="目录轮播时每张图持续 ms (默认 5000)")
    p.add_argument("--gif-duration", type=int, default=10000,
                   help="目录轮播时每个 gif 持续 ms (默认 10000)")
    args = p.parse_args()

    from .sources import PlaylistSource

    if args.playlist:
        path = Path(args.playlist).expanduser().resolve()
        if not path.is_file():
            print(f"播放列表文件不存在: {path}")
            sys.exit(1)
        with open(path) as f:
            data = json.load(f)
        src = build_source_from_dict(data)
        if src is None:
            print(f"播放列表 {path} 为空或不合法")
            sys.exit(1)
    elif args.target and args.target.startswith(
            ("http://", "https://", "rtsp://", "rtmp://", "udp://", "srt://")):
        # URL 直接作为流处理
        from .sources import StreamSource
        src = StreamSource(args.target, fit_mode=args.fit)
    else:
        target = Path(args.target).expanduser().resolve()
        if not target.exists():
            print(f"路径不存在: {target}")
            sys.exit(1)
        if target.is_file():
            src = build_source_for_path(target,
                                        duration_ms=args.image_duration,
                                        fit_mode=args.fit)
            if src is None:
                print(f"不支持的文件类型: {target.suffix}")
                sys.exit(1)
        else:
            items = []
            for p_ in sorted(target.iterdir()):
                ext = p_.suffix.lower()
                if ext in GIF_EXTS:
                    dur = args.gif_duration
                elif ext in VIDEO_EXTS:
                    dur = 30000     # 视频默认播 30 秒
                elif ext in IMAGE_EXTS:
                    dur = args.image_duration
                else:
                    continue
                s = build_source_for_path(p_, duration_ms=dur, fit_mode=args.fit)
                if s is not None:
                    items.append(s)
            if not items:
                print(f"目录里没有支持的图片/GIF: {target}")
                sys.exit(1)
            print(f"目录轮播 {len(items)} 个项目")
            src = PlaylistSource(items, name=f"playlist:{target.name}")

    runner = Runner()
    runner.set_source(src)
    runner.run()


if __name__ == "__main__":
    _main_cli()
