"""
终端源 —— 在伪终端 (PTY) 里跑任意 TUI 程序 (htop / btop / cmatrix...)，
把 ANSI 转义序列经 pyte 渲染成 PIL Image。

设计：
  * pty.fork() 启子进程跑用户指定的命令
  * 子进程以为自己在终端里，输出 ANSI 到 PTY slave
  * Python 端 read PTY master，喂给 pyte.Screen
  * 每帧从 pyte.Screen 取字符 + 颜色矩阵，用 Pillow 等宽字体画到 PIL Image
  * 不污染主屏（PTY 是内核内存里的虚拟设备，没有 GUI）

关键依赖:
  * pyte (sudo pacman -S python-pyte)
"""
from __future__ import annotations
import fcntl
import logging
import os
import pty
import shlex
import signal
import struct
import termios
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .base import Source, SourceInfo

logger = logging.getLogger("am01_dash.terminal")


# 默认等宽字体候选（按顺序找第一个存在的）
_FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
    # turing fork 自带的字体路径（由环境变量定位）
    *([] if not os.environ.get("AM01_DASH_TURING_DIR")
        else [os.path.join(os.environ["AM01_DASH_TURING_DIR"],
                           "res/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf")]),
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/FiraCode-Regular.ttf",
    "/usr/share/fonts/TTF/Hack-Regular.ttf",
]


def _find_mono_font() -> str:
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 终极 fallback：让 PIL 选自带的
    return ""


# ANSI 颜色名 -> RGB
_ANSI_RGB = {
    "black":   (30, 30, 30),
    "red":     (255,  85,  85),
    "green":   ( 80, 250, 123),
    "brown":   (241, 250, 140),    # pyte 把 yellow 叫 brown
    "yellow":  (241, 250, 140),
    "blue":    (139, 233, 253),
    "magenta": (255, 121, 198),
    "cyan":    (139, 233, 253),
    "white":   (248, 248, 242),
    "default": (248, 248, 242),
    "brightblack":   (98, 114, 164),
    "brightred":     (255, 110, 110),
    "brightgreen":   (105, 255, 145),
    "brightyellow":  (255, 255, 153),
    "brightblue":    (180, 240, 255),
    "brightmagenta": (255, 150, 215),
    "brightcyan":    (180, 240, 255),
    "brightwhite":   (255, 255, 255),
}

_DEFAULT_FG = (248, 248, 242)   # near-white
_DEFAULT_BG = (20, 22, 30)      # near-black 微蓝


def _color_to_rgb(name_or_hex: str, fallback: tuple) -> tuple:
    """pyte 给的颜色可能是 'default' / 'red' / 'ff5555' / ..."""
    if not name_or_hex or name_or_hex == "default":
        return fallback
    # 已知名字
    rgb = _ANSI_RGB.get(name_or_hex.lower())
    if rgb is not None:
        return rgb
    # hex (6 hex chars)
    if len(name_or_hex) == 6:
        try:
            r = int(name_or_hex[0:2], 16)
            g = int(name_or_hex[2:4], 16)
            b = int(name_or_hex[4:6], 16)
            return (r, g, b)
        except Exception:
            pass
    return fallback


def _set_winsize(fd: int, rows: int, cols: int):
    """告诉 PTY 终端尺寸。"""
    winsz = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)


class TerminalSource(Source):
    """
    在 PTY 里跑任意终端程序，把渲染结果作为 PIL Image 推送。

    用法：
        TerminalSource("htop")                # 跑 htop
        TerminalSource("btop --utf-force")    # 带参数
        TerminalSource(["btm", "-b"])         # 也可以传 argv list

    参数：
        cols/rows: 字符列数/行数。默认按字体大小算出能容下 960×400 的最大值。
        font_size: 字号 (px)。小字能塞更多内容、可读性差；大字反之。
        bg: 背景色 RGB；fg_default: 默认前景色。
        encoding: 通常 utf-8；btop 等 unicode block 字符必须 utf-8。

    target_fps 决定我们读 pyte 状态 + 重绘的频率。10fps 对 htop/btop 这种
    秒级刷新足够；cmatrix 这种连续动画可以拉高到 30。
    """

    target_fps = 10
    is_animated = True

    def __init__(self, command, name: Optional[str] = None,
                 duration_ms: Optional[int] = None,
                 fit_mode: str = "stretch",
                 cols: Optional[int] = None, rows: Optional[int] = None,
                 font_size: int = 14,
                 fps: int = 10,
                 hw_w: int = 960, hw_h: int = 400,
                 bg: tuple = _DEFAULT_BG, fg: tuple = _DEFAULT_FG,
                 env_extra: Optional[dict] = None):
        # 处理 command 参数：字符串或 list
        if isinstance(command, str):
            argv = shlex.split(command)
            display_name = command
        else:
            argv = list(command)
            display_name = " ".join(argv)
        if not argv:
            raise ValueError("command 不能为空")
        super().__init__(
            SourceInfo(
                kind="terminal",
                name=name or argv[0],
                path=display_name,
                duration_ms=duration_ms,
            ),
            fit_mode=fit_mode,
        )
        self._argv = argv
        self.target_fps = max(1, min(60, fps))
        self._hw_w = hw_w
        self._hw_h = hw_h
        self._bg = bg
        self._fg = fg
        self._env_extra = env_extra or {}

        # 字体 + 字符尺寸
        self._font_path = _find_mono_font()
        self._font_size = font_size
        self._font: Optional[ImageFont.FreeTypeFont] = None
        # 字符宽高（像素），start() 时按字体测量
        self._char_w = 0
        self._char_h = 0

        # 默认按字体大小自动算 cols/rows，使整个网格填满 hw 区域
        self._cols = cols
        self._rows = rows

        # PTY + 子进程
        self._pid: Optional[int] = None
        self._fd: Optional[int] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

        # pyte
        self._screen = None    # pyte.Screen
        self._stream = None    # pyte.ByteStream
        self._screen_lock = threading.Lock()

        # 状态
        self._dirty = True
        self._exhausted = False
        self._last_returned_id = -1
        self._frame_id = 0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        super().start()
        # 保险：playlist 循环时确保从干净状态开始
        self._exhausted = False
        self._dirty = True
        self._last_returned_id = -1
        self._frame_id = 0
        # 1) 加载 pyte
        try:
            import pyte
        except ImportError as e:
            raise RuntimeError(
                "缺少 pyte 模块。请装: sudo pacman -S python-pyte"
            ) from e

        # 2) 加载字体 + 测量字符尺寸
        if self._font_path:
            self._font = ImageFont.truetype(self._font_path, self._font_size)
        else:
            self._font = ImageFont.load_default()
        # 用 "M" 测一个等宽字符的宽高
        bbox = self._font.getbbox("M")
        self._char_w = max(1, bbox[2] - bbox[0])
        self._char_h = max(1, bbox[3] - bbox[1])
        # 行高比 char_h 稍大一点（行间距 = 字号 * 1.25）
        self._line_h = int(self._font_size * 1.25)

        # 3) 算 cols/rows：能容下 hw 区域的最大整数
        if self._cols is None:
            self._cols = max(20, self._hw_w // self._char_w)
        if self._rows is None:
            self._rows = max(8, self._hw_h // self._line_h)
        logger.info("[%s] grid %dcols×%drows, char %dx%d, line_h=%d",
                    self.name, self._cols, self._rows,
                    self._char_w, self._char_h, self._line_h)

        # 4) 创建 pyte Screen + Stream
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)

        # 5) 启 PTY + 子进程
        try:
            pid, fd = pty.fork()
        except OSError as e:
            raise RuntimeError(f"pty.fork 失败: {e}") from e

        if pid == 0:
            # 子进程
            try:
                env = os.environ.copy()
                env.update({
                    "TERM": "xterm-256color",
                    "COLORTERM": "truecolor",
                    "LINES": str(self._rows),
                    "COLUMNS": str(self._cols),
                    "LANG": env.get("LANG") or "en_US.UTF-8",
                    "LC_ALL": env.get("LC_ALL") or "en_US.UTF-8",
                })
                env.update(self._env_extra)
                os.execvpe(self._argv[0], self._argv, env)
            except Exception as e:
                # exec 失败必须立即退出（不然子进程会继续跑 Python）
                os._exit(127)
        # 父进程
        self._pid = pid
        self._fd = fd
        # 设 PTY 窗口尺寸（重要：很多 TUI 通过 SIGWINCH 重新布局）
        _set_winsize(fd, self._rows, self._cols)
        # 非阻塞 read
        flag = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)

        # 6) 读 PTY 的后台线程
        self._stop_flag.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"TerminalSource[{self.name}]",
            daemon=True,
        )
        self._reader_thread.start()
        logger.info("[%s] 启动子进程 pid=%d argv=%s",
                    self.name, pid, self._argv)

    def stop(self) -> None:
        self._stop_flag.set()
        # 杀子进程
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
                # 等会儿，杀不死再 SIGKILL
                for _ in range(20):
                    try:
                        wpid, _ = os.waitpid(self._pid, os.WNOHANG)
                        if wpid != 0:
                            break
                    except ChildProcessError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.kill(self._pid, signal.SIGKILL)
                        os.waitpid(self._pid, 0)
                    except Exception:
                        pass
            except ProcessLookupError:
                pass
            self._pid = None
        # 关 PTY fd
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        self._screen = None
        self._stream = None
        # 关键：重置 exhausted，让 playlist 下次循环回来能正常 start
        self._exhausted = False
        super().stop()

    def is_exhausted(self) -> bool:
        return self._exhausted

    # ---------- 读 PTY 后台 ----------

    def _reader_loop(self):
        """从 PTY master read 字节流喂给 pyte。

        只在子进程"真的退出"时设 _exhausted=True。
        正常 stop() 时主线程会先 _stop_flag.set() 再 close fd，
        此时 read/select 的异常是我们主动关 fd 引起的，**不要**设 exhausted，
        否则下次 start 后还要 reset，且 playlist 可能误判源结束。
        """
        import select
        fd = self._fd
        if fd is None:
            return    # 没 fd 不是 exhausted，是初始化失败，由 start 抛错
        while not self._stop_flag.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError):
                # 主线程 close 了 fd 引发的，不是 EOF
                return
            if not r:
                continue
            try:
                data = os.read(fd, 8192)
            except OSError:
                # 同上，正常 stop
                return
            if not data:
                # 真正 EOF：子进程退出
                logger.info("[%s] PTY EOF (子进程退出)", self.name)
                self._exhausted = True
                return
            with self._screen_lock:
                try:
                    self._stream.feed(data)
                    self._dirty = True
                except Exception as e:
                    logger.warning("[%s] pyte.feed 失败: %s", self.name, e)

    # ---------- 渲染 ----------

    def _render_screen(self) -> Image.Image:
        """把 pyte.Screen 当前状态画到 PIL Image。"""
        cols, rows = self._cols, self._rows
        # 网格实际像素尺寸
        img_w = cols * self._char_w
        img_h = rows * self._line_h
        img = Image.new("RGB", (img_w, img_h), self._bg)
        draw = ImageDraw.Draw(img)

        with self._screen_lock:
            screen = self._screen
            if screen is None:
                return img
            buf = screen.buffer
            # buf 是 {row: {col: pyte.Char}}
            for y in range(rows):
                if y not in buf:
                    continue
                line = buf[y]
                # 性能优化：连续相同色的字符合并 draw.text
                cur_text = []
                cur_x = 0
                cur_fg = self._fg
                cur_bg = self._bg
                cur_bold = False
                cur_start = 0

                def flush():
                    nonlocal cur_text, cur_start, cur_fg, cur_bg, cur_bold
                    if not cur_text:
                        return
                    text = "".join(cur_text)
                    px = cur_start * self._char_w
                    py = y * self._line_h
                    # 背景块
                    if cur_bg != self._bg:
                        draw.rectangle(
                            [px, py, px + len(cur_text) * self._char_w, py + self._line_h],
                            fill=cur_bg,
                        )
                    draw.text((px, py), text, font=self._font, fill=cur_fg)
                    cur_text = []

                for x in range(cols):
                    ch = line.get(x)
                    if ch is None:
                        char = " "
                        fg = self._fg
                        bg = self._bg
                        bold = False
                    else:
                        char = ch.data or " "
                        fg = _color_to_rgb(getattr(ch, "fg", "default"), self._fg)
                        bg = _color_to_rgb(getattr(ch, "bg", "default"), self._bg)
                        if getattr(ch, "reverse", False):
                            fg, bg = bg, fg
                        bold = bool(getattr(ch, "bold", False))
                    if not cur_text:
                        cur_start = x
                        cur_fg, cur_bg, cur_bold = fg, bg, bold
                    elif fg != cur_fg or bg != cur_bg or bold != cur_bold:
                        flush()
                        cur_start = x
                        cur_fg, cur_bg, cur_bold = fg, bg, bold
                    cur_text.append(char)
                flush()
        return img

    # ---------- Source 接口 ----------

    def next_frame(self) -> Optional[Image.Image]:
        if not self._dirty:
            return None
        if self._screen is None:
            return None
        with self._screen_lock:
            self._dirty = False
        img = self._render_screen()
        self._frame_id += 1
        self._last_returned_id = self._frame_id
        return img