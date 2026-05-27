#!/usr/bin/env python3
"""
AM01S Dash Picker —— turing 主题切换 / 预览 / 一键应用 GUI（高速版）。

设计要点：
  * 预览：进程内直接调 turing 的 config.load_theme + display 渲染，
          复用 theme-editor.py 的做法，无子进程、无文件 IO。
          切主题 ≈ 200ms，无需任何等待。
  * 应用到副屏：用 pkexec 弹密码框，root 跑 helper 脚本。
  * 停止：同样走 pkexec 杀 root 进程。

启动：
  python3 am01_dash_picker.py
"""
from __future__ import annotations
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Optional

from PIL import Image, ImageTk

# ============ 路径 ============
# am01_dash 包所在目录（仓库根）由文件位置自动推导
AM01_DASH_DIR = Path(__file__).resolve().parent
# turing-smart-screen-python 路径优先取环境变量，否则猜常见位置
def _resolve_turing_dir() -> Path:
    env = os.environ.get("AM01_DASH_TURING_DIR")
    if env:
        return Path(env).expanduser().resolve()
    # 常见安装位置
    candidates = [
        AM01_DASH_DIR.parent / "turing-smart-screen-python",
        Path.home() / "turing-smart-screen-python",
        Path("/opt/turing-smart-screen-python"),
    ]
    for c in candidates:
        if (c / "main.py").is_file():
            return c
    # 兜底（picker 启动时会再次校验）
    return AM01_DASH_DIR.parent / "turing-smart-screen-python"

TURING_DIR = _resolve_turing_dir()
THEMES_DIR = TURING_DIR / "res" / "themes"
CONFIG_YAML = TURING_DIR / "config.yaml"

# AM01S 物理尺寸
AM01_W, AM01_H = 960, 400

# GUI 预览尺寸
PREVIEW_DISPLAY_W = 720
PREVIEW_DISPLAY_H = int(PREVIEW_DISPLAY_W * AM01_H / AM01_W)  # 300

# ============ 让 turing 的库被 import ============
sys.path.insert(0, str(TURING_DIR))
sys.path.insert(0, str(AM01_DASH_DIR))
os.chdir(str(TURING_DIR))  # turing 内部用很多相对路径

# 关掉 turing 自己的日志（避免污染我们的终端）
import library.log  # noqa: E402
library.log.logger.setLevel(logging.WARNING)

# 把 turing 配成 STATIC + SIMU，仅用于预览
from library import config  # noqa: E402
config.CONFIG_DATA["config"]["HW_SENSORS"] = "STATIC"
config.CONFIG_DATA["display"]["REVISION"] = "SIMU"

# display 必须在 config 改完后 import
# 第一次 import 时 config 还没 load_theme，需要先放一个安全默认 theme
config.CONFIG_DATA["config"]["THEME"] = "3.5inchTheme2"
config.load_theme()

from library.display import display  # noqa: E402
import library.stats as stats  # noqa: E402


# ============ 辅助 ============

_THEME_SIZE_RE = re.compile(r'^\s*DISPLAY_SIZE:\s*"?(.+?)"?\s*$', re.MULTILINE)
_THEME_ORIENT_RE = re.compile(r'^\s*DISPLAY_ORIENTATION:\s*"?(.+?)"?\s*$', re.MULTILINE)


def list_themes() -> list[dict]:
    out = []
    if not THEMES_DIR.is_dir():
        return out
    for d in sorted(THEMES_DIR.iterdir()):
        if not d.is_dir():
            continue
        ty = d / "theme.yaml"
        if not ty.is_file():
            continue
        info = {"name": d.name, "size": "?", "orientation": "?", "path": str(d)}
        try:
            text = ty.read_text(encoding="utf-8", errors="replace")
            m = _THEME_SIZE_RE.search(text)
            if m:
                info["size"] = m.group(1).strip()
            m = _THEME_ORIENT_RE.search(text)
            if m:
                info["orientation"] = m.group(1).strip()
        except Exception:
            pass
        out.append(info)
    return out


def read_current_theme_from_yaml() -> str | None:
    try:
        text = CONFIG_YAML.read_text(encoding="utf-8")
        m = re.search(r'^\s*THEME:\s*"?(.+?)"?\s*$', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def patch_config(theme_name: str, revision: str):
    """安全地把 config.yaml 的 THEME 和 REVISION 字段改掉，并验证结果合法。"""
    import yaml
    text = CONFIG_YAML.read_text(encoding="utf-8")

    # 关键修复：re.sub replacement 字符串里的 \1 是 backreference，
    # 但 theme_name 里如果含 '\' 或某些字符会被 re 误解。用 lambda 避开。
    def _repl_theme(m):
        return m.group(1) + theme_name
    def _repl_rev(m):
        return m.group(1) + revision

    text = re.sub(r'^(\s*THEME:\s*).*$', _repl_theme,
                  text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^(\s*REVISION:\s*).*$', _repl_rev,
                  text, count=1, flags=re.MULTILINE)

    # 校验：新文本必须是合法 YAML 且能找到 THEME / REVISION 字段
    try:
        d = yaml.safe_load(text)
        actual_theme = d["config"]["THEME"]
        actual_rev = d["display"]["REVISION"]
        if str(actual_theme).strip() != theme_name.strip():
            raise ValueError(
                f"patch 后 THEME 不一致: 期望 {theme_name!r} 实际 {actual_theme!r}"
            )
        if str(actual_rev).strip() != revision.strip():
            raise ValueError(
                f"patch 后 REVISION 不一致: 期望 {revision!r} 实际 {actual_rev!r}"
            )
    except Exception as e:
        raise RuntimeError(
            f"修改 config.yaml 失败（已回滚未写盘）: {e}\n"
            f"建议手动检查 {CONFIG_YAML}"
        ) from e

    tmp = CONFIG_YAML.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(CONFIG_YAML)


# ============ root helper ============

HELPER_PATH = AM01_DASH_DIR / "tools" / "am01_apply_helper.sh"
HELPER_STOP_PATH = AM01_DASH_DIR / "tools" / "am01_stop_helper.sh"


PIDFILE = "/run/am01_dash_main.pid"

_KILL_BLOCK = (
    "# 杀进程：优先用 pidfile，pgrep 兜底，最后用 fuser 看谁占着 /dev/dri/card0\n"
    f"PIDFILE='{PIDFILE}'\n"
    "PIDS=''\n"
    "if [ -f \"$PIDFILE\" ]; then\n"
    "  PIDS=$(cat \"$PIDFILE\" 2>/dev/null)\n"
    "  echo \"[helper] from pidfile: $PIDS\" >> \"$LOG_FILE\"\n"
    "fi\n"
    "# 兜底1：fuser 找正持有 card0 的进程\n"
    "FUSER_PIDS=$(fuser /dev/dri/card0 2>/dev/null | tr -s ' ' '\\n' | grep -E '^[0-9]+$')\n"
    "if [ -n \"$FUSER_PIDS\" ]; then\n"
    "  echo \"[helper] from fuser card0: $FUSER_PIDS\" >> \"$LOG_FILE\"\n"
    "  PIDS=\"$PIDS $FUSER_PIDS\"\n"
    "fi\n"
    "# 兜底2：pgrep main.py（命令行不一定含完整路径，但试一下）\n"
    "PGREP_PIDS=$(pgrep -f 'python3 main.py' 2>/dev/null)\n"
    "if [ -n \"$PGREP_PIDS\" ]; then\n"
    "  PIDS=\"$PIDS $PGREP_PIDS\"\n"
    "fi\n"
    "# 去重 + 杀\n"
    "PIDS=$(echo \"$PIDS\" | tr ' ' '\\n' | sort -u | grep -E '^[0-9]+$')\n"
    "if [ -n \"$PIDS\" ]; then\n"
    "  echo \"[helper] killing PIDs: $(echo $PIDS | tr '\\n' ' ')\" >> \"$LOG_FILE\"\n"
    "  for pid in $PIDS; do kill -TERM \"$pid\" 2>/dev/null || true; done\n"
    "  sleep 2\n"
    "  for pid in $PIDS; do kill -KILL \"$pid\" 2>/dev/null || true; done\n"
    "  sleep 1\n"
    "fi\n"
    "rm -f \"$PIDFILE\"\n"
    "# 再次确认 card0 已释放\n"
    "if fuser /dev/dri/card0 >/dev/null 2>&1; then\n"
    "  echo \"[helper] card0 仍被占用！\" >> \"$LOG_FILE\"\n"
    "  fuser -v /dev/dri/card0 >> \"$LOG_FILE\" 2>&1 || true\n"
    "fi\n"
)


def _safe_write_helper(path: Path, content: str):
    """只有当现有内容与目标不一致时才覆写，避免破坏 root 拥有权 / 免密配置。"""
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return  # 已经是最新版，不动
        except Exception:
            pass
    try:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    except PermissionError:
        # helper 已被 root 拥有（install-nopasswd 之后），普通用户改不了。
        # 这是预期情况：内容只要还能用就 OK；如果代码升级要更新 helper，
        # 用户需要重跑 sudo bash tools/install-nopasswd.sh
        print(f"[picker] 警告：无权覆写 {path}（已被 root 拥有）。"
              f"如果 helper 行为异常，请重跑 install-nopasswd.sh。",
              file=sys.stderr)


def _ensure_helpers():
    HELPER_PATH.parent.mkdir(exist_ok=True)
    # Unified apply helper：根据第 1 个参数选不同启动逻辑
    #   $1 == "theme"             跑 turing main.py（用 config.yaml 里设的 THEME）
    #   $1 == "playlist" $2=path  跑 am01_dash.runner --playlist $2
    apply_content = (
        "#!/bin/bash\n"
        "# AM01S 副屏统一启动器\n"
        "# 用法:\n"
        "#   am01_apply_helper.sh theme\n"
        "#   am01_apply_helper.sh playlist /path/to/playlist.json\n"
        f"TURING_DIR='{TURING_DIR}'\n"
        f"AM01_DASH_DIR='{AM01_DASH_DIR}'\n"
        "LOG_FILE=\"$TURING_DIR/main.am01s.log\"\n"
        "MODE=\"${1:-theme}\"\n"
        "PLAYLIST_PATH=\"${2:-}\"\n"
        + _KILL_BLOCK +
        "echo '' >> \"$LOG_FILE\"\n"
        "echo \"===== $(date '+%F %T') am01_apply_helper start (mode=$MODE) =====\" >> \"$LOG_FILE\"\n"
        "export PYTHONPATH=\"${AM01_DASH_DIR}${PYTHONPATH:+:$PYTHONPATH}\"\n"
        "case \"$MODE\" in\n"
        "  theme)\n"
        "    cd \"$TURING_DIR\"\n"
        "    setsid python3 main.py >> \"$LOG_FILE\" 2>&1 < /dev/null &\n"
        "    ;;\n"
        "  playlist)\n"
        "    if [ -z \"$PLAYLIST_PATH\" ] || [ ! -f \"$PLAYLIST_PATH\" ]; then\n"
        "      echo \"[helper] 缺少 playlist 路径或文件不存在: $PLAYLIST_PATH\" >> \"$LOG_FILE\"\n"
        "      exit 2\n"
        "    fi\n"
        "    cd \"$AM01_DASH_DIR\"\n"
        "    setsid python3 -m am01_dash.runner --playlist \"$PLAYLIST_PATH\" "
        ">> \"$LOG_FILE\" 2>&1 < /dev/null &\n"
        "    ;;\n"
        "  *)\n"
        "    echo \"[helper] 未知 MODE: $MODE\" >> \"$LOG_FILE\"\n"
        "    exit 3\n"
        "    ;;\n"
        "esac\n"
        "NEW_PID=$!\n"
        "disown\n"
        "echo \"$NEW_PID\" > \"$PIDFILE\"\n"
        "chmod 0666 \"$PIDFILE\" 2>/dev/null || true\n"
        "echo \"started pid=$NEW_PID mode=$MODE (detached)\" >> \"$LOG_FILE\"\n"
        "exit 0\n"
    )
    stop_content = (
        "#!/bin/bash\n"
        f"TURING_DIR='{TURING_DIR}'\n"
        "LOG_FILE=\"$TURING_DIR/main.am01s.log\"\n"
        + _KILL_BLOCK +
        "echo \"===== $(date '+%F %T') am01_stop_helper done =====\" >> \"$LOG_FILE\"\n"
        "exit 0\n"
    )
    _safe_write_helper(HELPER_PATH, apply_content)
    _safe_write_helper(HELPER_STOP_PATH, stop_content)


def _run_root_script(script: Path, *extra_args: str) -> tuple[bool, str]:
    """
    以 root 跑 helper 脚本（带可选参数）。优先 sudo -n（免密），
    失败 fallback 到 pkexec 弹密码框。
    """
    cmd_base = [str(script)] + list(extra_args)

    # sudo -n（免密路径）
    if shutil.which("sudo"):
        try:
            r = subprocess.run(
                ["sudo", "-n"] + cmd_base,
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                return True, "ok (sudo -n)"
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            return False, "sudo -n 超时"

    # pkexec（弹密码框）
    if shutil.which("pkexec"):
        try:
            r = subprocess.run(
                ["pkexec"] + cmd_base,
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return True, "ok (pkexec)"
            return False, (
                f"pkexec 退出码 {r.returncode}\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}\n\n"
                f"提示：跑 `sudo bash tools/install-nopasswd.sh` 可永久免密。"
            )
        except subprocess.TimeoutExpired:
            return False, "pkexec 超时"
        except Exception as e:
            return False, f"pkexec 调用失败：{e}"

    return False, "既找不到 sudo 也没有 pkexec"


def apply_to_subscreen(mode: str = "theme",
                       playlist_path: Optional[str] = None) -> tuple[bool, str]:
    """
    mode='theme'    -> 按 config.yaml 跑 turing 主题
    mode='playlist' -> 跑 am01_dash.runner --playlist <path>
    """
    _ensure_helpers()
    if mode == "theme":
        return _run_root_script(HELPER_PATH, "theme")
    if mode == "playlist":
        if not playlist_path:
            return False, "playlist 模式需要 playlist_path"
        return _run_root_script(HELPER_PATH, "playlist", playlist_path)
    return False, f"未知 mode: {mode}"


def stop_subscreen() -> tuple[bool, str]:
    _ensure_helpers()
    return _run_root_script(HELPER_STOP_PATH)


# ============ 渲染：进程内直接调 turing ============

# turing 各 stat 模块的"渲染一次"入口
def _render_all_stats_once():
    """复用 theme-editor.py 的做法。"""
    td = config.THEME_DATA.get('STATS', {})

    def safe(fn):
        try:
            fn()
        except Exception as e:
            # 不让任何 stat 的异常阻断预览
            print(f"[picker] stat 渲染失败 (忽略): {e}", file=sys.stderr)

    if td.get('CPU', {}).get('PERCENTAGE', {}).get("INTERVAL", 0) > 0:
        safe(stats.CPU.percentage)
    if td.get('CPU', {}).get('FREQUENCY', {}).get("INTERVAL", 0) > 0:
        safe(stats.CPU.frequency)
    if td.get('CPU', {}).get('LOAD', {}).get("INTERVAL", 0) > 0:
        safe(stats.CPU.load)
    if td.get('CPU', {}).get('TEMPERATURE', {}).get("INTERVAL", 0) > 0:
        safe(stats.CPU.temperature)
    if td.get('CPU', {}).get('FAN_SPEED', {}).get("INTERVAL", 0) > 0:
        safe(stats.CPU.fan_speed)
    if td.get('GPU', {}).get("INTERVAL", 0) > 0:
        safe(stats.Gpu.stats)
    if td.get('MEMORY', {}).get("INTERVAL", 0) > 0:
        safe(stats.Memory.stats)
    if td.get('DISK', {}).get("INTERVAL", 0) > 0:
        safe(stats.Disk.stats)
    if td.get('NET', {}).get("INTERVAL", 0) > 0:
        safe(stats.Net.stats)
    if td.get('DATE', {}).get("INTERVAL", 0) > 0:
        safe(stats.Date.stats)
    if td.get('UPTIME', {}).get("INTERVAL", 0) > 0:
        safe(stats.SystemUptime.stats)
    if td.get('CUSTOM', {}).get("INTERVAL", 0) > 0:
        safe(stats.Custom.stats)
    if td.get('WEATHER', {}).get("INTERVAL", 0) > 0:
        safe(stats.Weather.stats)
    if td.get('PING', {}).get("INTERVAL", 0) > 0:
        safe(stats.Ping.stats)


# 全局：当前正在 picker 内"预览"的 LcdSimulated 实例。
# 每次切换主题都要重建（display 是 module-level singleton，但是 lcd 可以替换）。
def render_theme_in_process(theme_name: str) -> tuple[Image.Image | None, str | None]:
    """
    完全在 picker 进程内渲染一次主题，返回 (PIL Image, error)。
    速度: ~100-500ms（视主题复杂度）。
    """
    try:
        # 1) 改 config 内存值（不写盘）
        config.CONFIG_DATA["config"]["THEME"] = theme_name
        config.CONFIG_DATA["display"]["REVISION"] = "SIMU"
        config.CONFIG_DATA["config"]["HW_SENSORS"] = "STATIC"
        # 2) 重新 load theme
        config.load_theme()

        # 3) 重建 display.lcd 用新主题尺寸的 SIMU 后端
        #    导入新尺寸用的工具函数
        from library.display import _get_theme_size, _get_theme_orientation
        from library.lcd.lcd_simulated import LcdSimulated
        from library.lcd.lcd_comm import Orientation

        # turing 的 LcdSimulated.__init__ 会启 HTTP server，多次创建会端口冲突。
        # 我们 monkey-patch 一下：只在首次启 server；后续重用 mock。
        if not getattr(display, "_picker_patched", False):
            orig_init = LcdSimulated.__init__

            def _picker_init(self, com_port="AUTO", display_width=320, display_height=480,
                             update_queue=None):
                from library.lcd.lcd_comm import LcdComm
                LcdComm.__init__(self, com_port, display_width, display_height, update_queue)
                self.screen_image = Image.new("RGB",
                                              (self.get_width(), self.get_height()),
                                              (0, 0, 0))
                self.orientation = Orientation.PORTRAIT
                # 不启 webserver，也不写 screencap.png
                self.webServer = None

            def _picker_close(self):
                pass

            def _picker_setorient(self, orientation=Orientation.PORTRAIT):
                self.orientation = orientation
                with self.update_queue_mutex:
                    self.screen_image = Image.new("RGB",
                                                  (self.get_width(), self.get_height()),
                                                  (0, 0, 0))

            def _picker_displaypil(self, image, x=0, y=0, image_width=0, image_height=0):
                if not image_height:
                    image_height = image.size[1]
                if not image_width:
                    image_width = image.size[0]
                if image.size[1] > self.get_height():
                    image_height = self.get_height()
                if image.size[0] > self.get_width():
                    image_width = self.get_width()
                if image_width != image.size[0] or image_height != image.size[1]:
                    image = image.crop((0, 0, image_width, image_height))
                with self.update_queue_mutex:
                    self.screen_image.paste(image, (x, y))

            LcdSimulated.__init__ = _picker_init
            LcdSimulated.closeSerial = _picker_close
            LcdSimulated.SetOrientation = _picker_setorient
            LcdSimulated.DisplayPILImage = _picker_displaypil
            display._picker_patched = True

        # 4) 关掉旧 lcd，新建一个匹配主题尺寸的
        w, h = _get_theme_size()
        display.lcd = LcdSimulated(display_width=w, display_height=h)

        # 5) 跑一遍渲染（init + static images + static text + 所有 stats）
        display.initialize_display()
        display.display_static_images()
        display.display_static_text()
        _render_all_stats_once()

        # 6) 抓画面
        img = display.lcd.screen_image.copy()
        return img, None
    except Exception as e:
        import traceback
        return None, f"{e}\n{traceback.format_exc()[-600:]}"


# ============ 媒体源预览（picker 进程内快速生成） ============

def _preview_image(path: str) -> tuple[Image.Image | None, str | None]:
    try:
        im = Image.open(path).convert("RGB")
        return im, None
    except Exception as e:
        return None, f"打开图片失败: {e}"


def _preview_gif(path: str) -> tuple[Image.Image | None, str | None]:
    try:
        im = Image.open(path)
        # 取第一帧
        im.seek(0)
        return im.convert("RGB"), None
    except Exception as e:
        return None, f"打开 GIF 失败: {e}"


def _preview_video(path: str) -> tuple[Image.Image | None, str | None]:
    """用 ffmpeg 截 1 秒位置的一帧。"""
    import shutil as _sh
    import subprocess as _sp
    import tempfile
    if not _sh.which("ffmpeg"):
        return None, "ffmpeg 不在 PATH"
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp_path = tf.name
        # -ss 1 跳到 1 秒（避开纯黑开头），-vframes 1 取一帧
        r = _sp.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "1", "-i", path, "-vframes", "1",
             "-vf", "scale=480:-2",
             tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not os.path.isfile(tmp_path):
            return None, f"ffmpeg 截帧失败: {r.stderr[-200:]}"
        im = Image.open(tmp_path).convert("RGB").copy()
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return im, None
    except Exception as e:
        return None, f"截帧异常: {e}"


def _find_mono_font(size: int) -> ImageFont.FreeTypeFont:
    """按优先级找一个等宽 TTF 字体。"""
    candidates = [
        TURING_DIR / "res/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
        Path("/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf"),
        Path("/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSansMono.ttf"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                return ImageFont.truetype(str(p), size)
        except Exception:
            pass
    return ImageFont.load_default()


def _preview_stream(url: str) -> tuple[Image.Image | None, str | None]:
    """流的预览：占位卡片（实时拉太重，picker 不做）。"""
    img = Image.new("RGB", (480, 200), (40, 50, 70))
    d = ImageDraw.Draw(img)
    font = _find_mono_font(16)
    font_s = _find_mono_font(12)
    d.text((20, 30), "🌐 流媒体", fill=(120, 200, 255), font=font)
    short = url if len(url) <= 60 else url[:57] + "..."
    d.text((20, 70), short, fill=(220, 220, 220), font=font_s)
    d.text((20, 110), "应用到副屏后实时拉取播放", fill=(160, 160, 160), font=font_s)
    return img, None


def _preview_terminal(item: dict) -> tuple[Image.Image | None, str | None]:
    """终端的预览：实际跑命令 ~1.5 秒，让 TUI 自绘后截图。"""
    try:
        from am01_dash.sources.terminal import TerminalSource
        src = TerminalSource(
            command=item["path"],
            fps=item.get("fps", 10),
            font_size=item.get("font_size", 14),
            hw_w=960, hw_h=400,
        )
    except Exception as e:
        return None, f"创建 TerminalSource 失败: {e}"
    try:
        src.start()
    except Exception as e:
        return None, f"启动失败: {e}"
    try:
        # 等 TUI 完成初次绘制
        time.sleep(1.5)
        # 强制 dirty 拿一帧
        src._dirty = True
        img = src.next_frame()
        if img is None:
            return None, "终端没有输出（命令可能不存在或立即退出）"
        return img.convert("RGB"), None
    finally:
        try:
            src.stop()
        except Exception:
            pass


def render_media_preview(item: dict) -> tuple[Image.Image | None, str | None]:
    """根据 item.kind 分发到对应的预览生成函数。"""
    kind = item.get("kind")
    path = item.get("path", "")
    if kind == "image":
        return _preview_image(path)
    if kind == "gif":
        return _preview_gif(path)
    if kind == "video":
        return _preview_video(path)
    if kind == "stream":
        return _preview_stream(path)
    if kind == "terminal":
        return _preview_terminal(item)
    return None, f"未知类型: {kind}"


# ============ GUI ============

class PickerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("AM01S Dash Picker —— turing 主题预览 / 应用")
        root.geometry("1100x540")
        root.minsize(900, 480)

        self.themes = list_themes()
        self.current_theme = read_current_theme_from_yaml()
        self._photo = None

        self._build_ui()
        self._refresh_theme_list()

    def _build_ui(self):
        # 顶层 Notebook：主题 / 媒体 / ...
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # === Tab 1: 主题 ===
        theme_tab = ttk.Frame(notebook)
        notebook.add(theme_tab, text="主题 (turing)")
        self._build_theme_tab(theme_tab)

        # === Tab 2: 媒体（图片/GIF 播放列表）===
        media_tab = ttk.Frame(notebook)
        notebook.add(media_tab, text="媒体 (图片/GIF)")
        self._build_media_tab(media_tab)

        # 共享底部状态栏
        self.lbl_status = ttk.Label(self.root, text="就绪",
                                     relief="sunken", anchor="w")
        self.lbl_status.pack(side="bottom", fill="x")

    def _build_theme_tab(self, parent):
        main = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        # 左
        left = ttk.Frame(main)
        main.add(left, weight=1)
        ttk.Label(left, text="主题列表（点击预览）",
                  font=("sans-serif", 11, "bold")).pack(anchor="w", pady=(0, 4))
        sb = ttk.Scrollbar(left, orient="vertical")
        self.lst = tk.Listbox(left, yscrollcommand=sb.set,
                              activestyle="dotbox", height=20)
        sb.config(command=self.lst.yview)
        self.lst.pack(side="left", fill=tk.BOTH, expand=True)
        sb.pack(side="right", fill=tk.Y)
        self.lst.bind("<<ListboxSelect>>", self._on_select)

        # 右
        right = ttk.Frame(main)
        main.add(right, weight=3)

        self.lbl_current = ttk.Label(right, text="", font=("sans-serif", 10))
        self.lbl_current.pack(anchor="w", pady=(0, 6))

        canvas_frame = tk.Frame(right, bg="#222",
                                width=PREVIEW_DISPLAY_W + 8,
                                height=PREVIEW_DISPLAY_H + 8)
        canvas_frame.pack(pady=(0, 8))
        canvas_frame.pack_propagate(False)
        self.canvas = tk.Canvas(canvas_frame,
                                width=PREVIEW_DISPLAY_W,
                                height=PREVIEW_DISPLAY_H,
                                bg="black", highlightthickness=0)
        self.canvas.pack(padx=4, pady=4)
        self.canvas_text = self.canvas.create_text(
            PREVIEW_DISPLAY_W // 2, PREVIEW_DISPLAY_H // 2,
            text="← 选一个主题", fill="#888", font=("sans-serif", 14),
        )

        self.lbl_info = ttk.Label(right, text="", font=("monospace", 9))
        self.lbl_info.pack(anchor="w", pady=(0, 8))

        btn_row = ttk.Frame(right)
        btn_row.pack(anchor="w", pady=(4, 0))
        ttk.Button(btn_row, text="重新预览",
                   command=self._do_preview).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="应用到副屏 ▶",
                   command=self._do_apply).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="停止副屏程序",
                   command=self._do_stop).pack(side="left")

    # ============ 媒体选项卡 ============

    def _build_media_tab(self, parent):
        """
        媒体 tab 三栏布局：
          左：类型快捷栏（5 种 + 全部）
          中：当前 + 按钮区（添加按钮 + 列表 + 操作）
          右：预览（点列表行触发）
        """
        # 媒体列表内部状态（在 build 之前初始化好，避免列加载阶段引用未定义）
        self.media_items: list[dict] = []
        self.media_default_json = AM01_DASH_DIR / "data" / "current_playlist.json"
        self._media_photo = None    # 预览图引用
        self._media_filter_kind = "all"

        # 顶部：全局动作
        topbar = ttk.Frame(parent)
        topbar.pack(fill=tk.X, pady=(0, 6))
        self.media_loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(topbar, text="循环整个列表", variable=self.media_loop_var,
                        command=self._auto_save_playlist
                        ).pack(side="left", padx=(0, 16))
        ttk.Button(topbar, text="应用到副屏 ▶", command=self._media_apply
                   ).pack(side="left", padx=(0, 6))
        ttk.Button(topbar, text="停止副屏", command=self._do_stop
                   ).pack(side="left", padx=(0, 16))
        ttk.Label(topbar, text="（修改自动保存）",
                  font=("sans-serif", 9), foreground="#888").pack(side="left")

        # 主分栏
        body = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        # ============ 左栏：类型快捷过滤 + 添加按钮 ============
        left = ttk.Frame(body, padding=(2, 4))
        body.add(left, weight=0)
        ttk.Label(left, text="添加内容",
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(0, 4))
        for icon, text, cmd in [
            ("🖼", " 图片/GIF",    self._media_add_files),
            ("🎬", " 视频文件",    self._media_add_video),
            ("🌐", " 流 / URL",    self._media_add_url),
            ("⌨ ", " 终端命令",    self._media_add_terminal),
            ("📂", " 目录(递归)",  self._media_add_dir),
        ]:
            ttk.Button(left, text=f"{icon}{text}", command=cmd, width=14
                       ).pack(anchor="w", pady=2)

        ttk.Separator(left, orient="horizontal").pack(fill=tk.X, pady=10)

        ttk.Label(left, text="筛选类型",
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(0, 4))
        self._media_filter_var = tk.StringVar(value="all")
        for kind, label in [
            ("all",      "全部"),
            ("image",    "图片"),
            ("gif",      "GIF"),
            ("video",    "视频"),
            ("stream",   "流"),
            ("terminal", "终端"),
        ]:
            ttk.Radiobutton(left, text=label, variable=self._media_filter_var,
                            value=kind, command=self._media_refresh_tree
                            ).pack(anchor="w")

        ttk.Separator(left, orient="horizontal").pack(fill=tk.X, pady=10)
        ttk.Button(left, text="🗑 清空全部",
                   command=self._media_clear, width=14
                   ).pack(anchor="w", pady=2)

        # ============ 中栏：当前播放列表 + 行内操作 ============
        center = ttk.Frame(body, padding=(4, 4))
        body.add(center, weight=3)

        # 列表 + 滚动条
        list_frame = ttk.Frame(center)
        list_frame.pack(fill=tk.BOTH, expand=True)
        cols = ("path", "kind", "fit", "duration", "fps")
        self.media_tree = ttk.Treeview(list_frame, columns=cols,
                                       show="headings", height=14,
                                       selectmode="browse")
        for c, t, w, anchor in [
            ("path", "文件 / URL / 命令", 360, "w"),
            ("kind", "类型", 70, "center"),
            ("fit",  "缩放", 80, "center"),
            ("duration", "时长", 80, "center"),
            ("fps",  "fps", 60, "center"),
        ]:
            self.media_tree.heading(c, text=t)
            self.media_tree.column(c, width=w, anchor=anchor)
        vsb = ttk.Scrollbar(list_frame, orient="vertical",
                            command=self.media_tree.yview)
        self.media_tree.configure(yscrollcommand=vsb.set)
        self.media_tree.pack(side="left", fill=tk.BOTH, expand=True)
        vsb.pack(side="right", fill="y")
        self.media_tree.bind("<Double-1>", self._media_on_dblclick)
        self.media_tree.bind("<<TreeviewSelect>>", self._media_on_select)

        # 行内动作
        op = ttk.Frame(center)
        op.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(op, text="↑ 上移", command=lambda: self._media_move(-1)
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(op, text="↓ 下移", command=lambda: self._media_move(+1)
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(op, text="✎ 编辑", command=self._media_edit_selected
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(op, text="🗑 删除", command=self._media_remove_selected
                   ).pack(side="left", padx=(0, 12))
        ttk.Label(op, text="（双击行 = 编辑）",
                  font=("sans-serif", 9), foreground="#888").pack(side="left")

        # ============ 右栏：预览 ============
        right = ttk.Frame(body, padding=(4, 4))
        body.add(right, weight=2)
        ttk.Label(right, text="预览",
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(0, 4))

        # 副屏比例 960×400 = 12:5，缩到 480×200 显示
        prev_w, prev_h = 480, 200
        cf = tk.Frame(right, bg="#222", width=prev_w + 8, height=prev_h + 8)
        cf.pack(pady=(0, 4))
        cf.pack_propagate(False)
        self.media_preview_canvas = tk.Canvas(cf, width=prev_w, height=prev_h,
                                              bg="black", highlightthickness=0)
        self.media_preview_canvas.pack(padx=4, pady=4)
        self._media_preview_canvas_text = self.media_preview_canvas.create_text(
            prev_w // 2, prev_h // 2,
            text="← 点列表行预览",
            fill="#888", font=("sans-serif", 12),
        )
        self._media_preview_size = (prev_w, prev_h)

        # 当前选中的详情
        self.media_detail_lbl = ttk.Label(right, text="",
                                           font=("monospace", 9),
                                           justify="left", anchor="w")
        self.media_detail_lbl.pack(fill=tk.X, pady=(4, 0))

        # 启动时尝试加载上次的列表
        if self.media_default_json.exists():
            try:
                self._media_load_from_file(self.media_default_json)
            except Exception:
                pass

    # ---- media helpers ----

    @staticmethod
    def _media_kind_for(path: str) -> Optional[str]:
        ext = Path(path).suffix.lower()
        if ext in (".gif", ".webp", ".apng"):
            return "gif"
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
            return "image"
        if ext in (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v",
                   ".flv", ".ts", ".m3u8", ".mpd"):
            return "video"
        return None

    @staticmethod
    def _default_duration_for(kind: str) -> int:
        """每种源的默认 duration_ms。

        * 图片/GIF/terminal：没有"自然时长"，需要明确设秒数否则 playlist 卡住
        * 视频/流：默认 0 = 按自然时长（跑完自动切下一个）
        """
        return {
            "image": 5000,
            "gif": 10000,
            "video": 0,
            "stream": 0,
            "terminal": 30000,   # 终端无自然结束，默认 30 秒后切下一个
        }.get(kind, 5000)

    def _media_refresh_tree(self):
        for iid in self.media_tree.get_children():
            self.media_tree.delete(iid)
        # 过滤
        filter_kind = getattr(self, "_media_filter_var", None)
        filter_kind = filter_kind.get() if filter_kind is not None else "all"
        for i, it in enumerate(self.media_items):
            if filter_kind != "all" and it["kind"] != filter_kind:
                continue
            dur = it["duration_ms"]
            dur_display = "自然" if dur is None or dur <= 0 else str(dur)
            fps_val = it.get("fps", 0)
            if it["kind"] in ("video", "stream"):
                fps_display = "自动" if not fps_val else str(fps_val)
            elif it["kind"] == "terminal":
                fps_display = str(fps_val or 10)
            else:
                fps_display = "-"
            # iid 用真实索引（即使过滤了也保留原 index，便于改/删时定位回 media_items）
            self.media_tree.insert("", "end", iid=str(i), values=(
                it["path"], it["kind"], it["fit_mode"], dur_display, fps_display
            ))

    # ---- 选中/预览 ----

    def _media_on_select(self, _evt=None):
        sel = self.media_tree.selection()
        if not sel:
            return
        try:
            i = int(sel[0])
        except ValueError:
            return
        if i < 0 or i >= len(self.media_items):
            return
        it = self.media_items[i]
        self._media_show_detail(it)
        # 预览（异步避免卡 UI）
        self.media_preview_canvas.delete("img")
        self.media_preview_canvas.itemconfig(
            self._media_preview_canvas_text,
            text=f"渲染中：{it['kind']}", fill="#aaa",
        )
        self.root.update_idletasks()

        def _run():
            t0 = time.monotonic()
            img, err = render_media_preview(it)
            dt = (time.monotonic() - t0) * 1000
            self.root.after(0, lambda: self._media_show_preview(it, img, err, dt))

        threading.Thread(target=_run, daemon=True).start()

    def _media_show_detail(self, it: dict):
        path = it.get("path", "")
        if len(path) > 70:
            path = "..." + path[-67:]
        lines = [
            f"path: {path}",
            f"kind: {it['kind']}     fit: {it['fit_mode']}     "
            f"duration: {it['duration_ms']}     fps: {it.get('fps', 0)}",
        ]
        if it.get("name"):
            lines.append(f"name: {it['name']}")
        if it.get("loop") is not None:
            lines.append(f"loop: {it.get('loop')}")
        if it.get("font_size"):
            lines.append(f"font_size: {it.get('font_size')}")
        self.media_detail_lbl.config(text="\n".join(lines))

    def _media_show_preview(self, it, pil_img, err, dt_ms):
        canvas = self.media_preview_canvas
        canvas.delete("img")
        if pil_img is None:
            canvas.itemconfig(self._media_preview_canvas_text,
                              text=f"预览失败：{it['kind']}\n{(err or '')[:80]}",
                              fill="#f55")
            return
        # 等比缩放到 prev 区
        pw, ph = self._media_preview_size
        tw, th = pil_img.size
        ratio = min(pw / tw, ph / th)
        nw, nh = max(1, int(tw * ratio)), max(1, int(th * ratio))
        img = pil_img.resize((nw, nh), Image.LANCZOS)
        self._media_photo = ImageTk.PhotoImage(img)
        x = (pw - nw) // 2
        y = (ph - nh) // 2
        canvas.itemconfig(self._media_preview_canvas_text, text="")
        canvas.create_image(x, y, image=self._media_photo, anchor="nw", tags="img")
        self._set_status(
            f"预览：{it['kind']}  {tw}x{th} → {nw}x{nh}  {dt_ms:.0f}ms"
        )

    def _media_edit_selected(self):
        """触发"编辑"按钮 = 等价于双击当前选中行。"""
        sel = self.media_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一行")
            return
        # 构造一个假的 event，复用 _media_on_dblclick
        # 用 identify_row 路径：直接拿 selection
        # 偷个懒：直接调内部逻辑
        try:
            i = int(sel[0])
        except ValueError:
            return
        if i < 0 or i >= len(self.media_items):
            return
        self._media_open_edit_dialog(i)

    def _media_add_files(self):
        from tkinter import filedialog
        files = filedialog.askopenfilenames(
            title="选择图片/GIF 文件",
            filetypes=[
                ("图片和动图", "*.png *.jpg *.jpeg *.bmp *.tiff *.gif *.webp *.apng"),
                ("所有文件", "*.*"),
            ],
        )
        added = 0
        for f in files:
            kind = self._media_kind_for(f)
            if kind is None:
                continue
            self.media_items.append({
                "path": f, "kind": kind,
                "fit_mode": "contain",
                "duration_ms": self._default_duration_for(kind),
            })
            added += 1
        self._media_refresh_tree()
        self._auto_save_playlist()
        self._set_status(f"已添加 {added} 个文件 (共 {len(self.media_items)} 项)")

    def _media_add_video(self):
        from tkinter import filedialog
        files = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[
                ("视频", "*.mp4 *.webm *.mkv *.mov *.avi *.m4v *.flv *.ts"),
                ("HLS/DASH", "*.m3u8 *.mpd"),
                ("所有文件", "*.*"),
            ],
        )
        added = 0
        for f in files:
            self.media_items.append({
                "path": f, "kind": "video",
                "fit_mode": "contain",
                "duration_ms": self._default_duration_for("video"),
                "loop": True,
                "fps": 0,    # 0 = 用 ffprobe 探测的原片帧率
            })
            added += 1
        self._media_refresh_tree()
        self._auto_save_playlist()
        self._set_status(f"已添加 {added} 个视频 (共 {len(self.media_items)} 项)")

    def _media_add_url(self):
        """弹窗输入流/URL"""
        dlg = tk.Toplevel(self.root)
        dlg.title("添加流 / URL")
        dlg.transient(self.root)
        dlg.geometry("700x360")
        dlg.minsize(560, 320)
        dlg.update_idletasks()
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass

        # 用 pack 自顶向下排，不容易出"小窗口"问题
        main = ttk.Frame(dlg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(main, text="URL (HTTP/HTTPS 直链, RTSP, m3u8, YouTube 等)",
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(0, 2))
        url_var = tk.StringVar()
        ttk.Entry(main, textvariable=url_var).pack(fill=tk.X, pady=(0, 12))

        # 名称
        row1 = ttk.Frame(main)
        row1.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row1, text="名称（可选）：", width=14).pack(side="left")
        name_var = tk.StringVar()
        ttk.Entry(row1, textvariable=name_var).pack(side="left", fill=tk.X, expand=True)

        # 时长 + 缩放模式同一行
        row2 = ttk.Frame(main)
        row2.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row2, text="时长 (ms)：", width=14).pack(side="left")
        dur_var = tk.StringVar(value="0")
        ttk.Entry(row2, textvariable=dur_var, width=14).pack(side="left", padx=(0, 8))
        ttk.Label(row2, text="(0 = 自然时长)",
                  font=("sans-serif", 9), foreground="#888"
                  ).pack(side="left", padx=(0, 16))

        # 循环复选框
        row_loop = ttk.Frame(main)
        row_loop.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row_loop, text="", width=14).pack(side="left")
        loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_loop, text="循环播放（流跑完自动从头开始）",
                        variable=loop_var).pack(side="left")
        ttk.Label(row2, text="缩放：").pack(side="left")
        fit_var = tk.StringVar(value="contain")
        ttk.Combobox(row2, textvariable=fit_var,
                     values=("contain", "cover", "stretch"),
                     state="readonly", width=12).pack(side="left")

        # 提示文字
        tip = ttk.Label(main,
            text=(
                "提示：\n"
                "  • 直链 mp4 / m3u8 / mpd / rtsp / rtmp 直接喂\n"
                "  • YouTube / Bilibili 等需要先安装 yt-dlp:\n"
                "        sudo pacman -S yt-dlp\n"
                "  • 时长用于轮播切换；纯播单源时可不设"
            ), justify="left", font=("sans-serif", 9), foreground="#888")
        tip.pack(anchor="w", pady=(4, 0))

        # 底部按钮（fill_x + side=bottom 保证永远可见）
        btn_row = ttk.Frame(dlg)
        btn_row.pack(side="bottom", fill=tk.X, padx=10, pady=10)

        def _ok():
            url = url_var.get().strip()
            if not url:
                messagebox.showerror("错误", "URL 不能为空")
                return
            try:
                dur = int(dur_var.get())
            except Exception:
                dur = 0
            self.media_items.append({
                "path": url, "kind": "stream",
                "fit_mode": fit_var.get(),
                "duration_ms": dur,    # 0 = 自然时长
                "name": name_var.get().strip() or None,
                "loop": loop_var.get(),
                "fps": 0,   # 流也默认自适应
            })
            self._media_refresh_tree()
            self._auto_save_playlist()
            self._set_status(f"已添加流: {url[:50]}")
            dlg.destroy()

        ttk.Button(btn_row, text="取消", command=dlg.destroy
                   ).pack(side="right", padx=(8, 0))
        ttk.Button(btn_row, text="确定", command=_ok
                   ).pack(side="right")

    def _media_add_terminal(self):
        """弹窗：输入要在副屏跑的 TUI 命令（htop / btop / cmatrix...）。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("添加终端命令")
        dlg.transient(self.root)
        dlg.geometry("680x440")
        dlg.minsize(560, 380)
        dlg.update_idletasks()
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass

        main = ttk.Frame(dlg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(main, text="命令（在副屏的 PTY 里跑，主屏不可见）",
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(0, 2))
        cmd_var = tk.StringVar(value="htop")
        ttk.Entry(main, textvariable=cmd_var).pack(fill=tk.X, pady=(0, 8))

        # 快速预设（点击同时设置命令 + 推荐字号）
        ttk.Label(main, text="预设（点击自动填命令和推荐字号）：",
                  font=("sans-serif", 9), foreground="#888"
                  ).pack(anchor="w", pady=(0, 2))
        preset_frame = ttk.Frame(main)
        preset_frame.pack(fill=tk.X, pady=(0, 8))

        # 每个预设 = (label, command, recommended_font_size)
        presets = [
            ("htop",        "htop",                       14),
            ("btop",        "btop",                       11),
            ("bashtop",     "bashtop",                    12),
            ("nvtop",       "nvtop",                      13),
            ("cava",        "cava",                       14),
            ("cmatrix",     "cmatrix -ab",                12),
            ("unimatrix",   "unimatrix -s 96 -l o",       12),
            ("asciiquarium","asciiquarium",               12),
            ("pipes.sh",    "pipes.sh",                   14),
            ("tty-clock",   "tty-clock -c -t -C 4",       14),
            ("peaclock",    "peaclock",                   12),
            ("fastfetch",   "watch -n2 -t fastfetch",     12),
        ]

        # 4 列网格排布
        for idx, (label, cmd, font_sz) in enumerate(presets):
            r, c = divmod(idx, 4)
            ttk.Button(
                preset_frame, text=label, width=12,
                command=lambda c_=cmd, fs_=font_sz: (
                    cmd_var.set(c_), font_var.set(str(fs_))
                ),
            ).grid(row=r, column=c, padx=2, pady=2, sticky="we")
        for c in range(4):
            preset_frame.columnconfigure(c, weight=1)

        # 名称
        row1 = ttk.Frame(main)
        row1.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row1, text="名称：", width=12).pack(side="left")
        name_var = tk.StringVar()
        ttk.Entry(row1, textvariable=name_var).pack(side="left", fill=tk.X, expand=True)

        # 时长 + fps
        row2 = ttk.Frame(main)
        row2.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row2, text="时长 (ms)：", width=12).pack(side="left")
        dur_var = tk.StringVar(value="30000")
        ttk.Entry(row2, textvariable=dur_var, width=10).pack(side="left", padx=(0, 16))
        ttk.Label(row2, text="fps：").pack(side="left")
        fps_var = tk.StringVar(value="10")
        ttk.Entry(row2, textvariable=fps_var, width=8).pack(side="left")

        # 字号 + 缩放模式
        row3 = ttk.Frame(main)
        row3.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row3, text="字号 (px)：", width=12).pack(side="left")
        font_var = tk.StringVar(value="14")
        ttk.Entry(row3, textvariable=font_var, width=8).pack(side="left", padx=(0, 16))
        ttk.Label(row3, text="缩放：").pack(side="left")
        fit_var = tk.StringVar(value="stretch")
        ttk.Combobox(row3, textvariable=fit_var,
                     values=("stretch", "contain", "cover"),
                     state="readonly", width=10).pack(side="left")

        # 提示
        ttk.Label(main, justify="left", foreground="#888",
                  font=("sans-serif", 9),
                  text=(
                      "说明：\n"
                      "  • 命令必须能在终端直接跑（PATH 里的可执行）\n"
                      "  • 字号小 = 能显示更多内容；副屏 960×400 推荐 12-16 px\n"
                      "  • 时长 = 0 是无限（playlist 中没意义，终端没自然结束）\n"
                      "  • fps：htop/btop 刷新本就慢，10 fps 足够；cmatrix 可设 30"
                  )).pack(anchor="w", pady=(8, 0))

        # 底部按钮
        btn_row = ttk.Frame(dlg)
        btn_row.pack(side="bottom", fill=tk.X, padx=10, pady=10)

        def _ok():
            cmd = cmd_var.get().strip()
            if not cmd:
                messagebox.showerror("错误", "命令不能为空")
                return
            try:
                dur = int(dur_var.get())
            except Exception:
                dur = 30000
            try:
                fps = int(fps_var.get())
            except Exception:
                fps = 10
            try:
                font_size = int(font_var.get())
            except Exception:
                font_size = 14
            self.media_items.append({
                "path": cmd, "kind": "terminal",
                "fit_mode": fit_var.get(),
                "duration_ms": dur,
                "fps": fps,
                "font_size": font_size,
                "name": name_var.get().strip() or None,
            })
            self._media_refresh_tree()
            self._auto_save_playlist()
            self._set_status(f"已添加终端: {cmd[:60]}")
            dlg.destroy()

        ttk.Button(btn_row, text="取消", command=dlg.destroy
                   ).pack(side="right", padx=(8, 0))
        ttk.Button(btn_row, text="确定", command=_ok
                   ).pack(side="right")

    def _media_add_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择包含图片/GIF/视频 的目录")
        if not d:
            return
        added = 0
        for root, _, files in os.walk(d):
            for fn in sorted(files):
                p = os.path.join(root, fn)
                kind = self._media_kind_for(p)
                if kind is None:
                    continue
                self.media_items.append({
                    "path": p, "kind": kind,
                    "fit_mode": "contain",
                    "duration_ms": self._default_duration_for(kind),
                })
                added += 1
        self._media_refresh_tree()
        self._auto_save_playlist()
        self._set_status(f"递归添加 {added} 个文件 (共 {len(self.media_items)} 项)")

    def _media_clear(self):
        if not self.media_items:
            return
        if not messagebox.askyesno("清空", f"清空 {len(self.media_items)} 项播放列表？"):
            return
        self.media_items.clear()
        self._media_refresh_tree()
        self._auto_save_playlist()
        self._set_status("播放列表已清空")

    def _media_remove_selected(self):
        sel = list(self.media_tree.selection())
        if not sel:
            return
        idxs = sorted([int(i) for i in sel], reverse=True)
        for i in idxs:
            self.media_items.pop(i)
        self._media_refresh_tree()
        self._auto_save_playlist()
        self._set_status(f"删除 {len(idxs)} 项")

    def _media_move(self, delta: int):
        sel = self.media_tree.selection()
        if not sel:
            return
        i = int(sel[0])
        j = i + delta
        if j < 0 or j >= len(self.media_items):
            return
        self.media_items[i], self.media_items[j] = self.media_items[j], self.media_items[i]
        self._media_refresh_tree()
        self._auto_save_playlist()
        self.media_tree.selection_set(str(j))
        self.media_tree.see(str(j))

    def _media_on_dblclick(self, event):
        """双击行 → 弹窗改 fit_mode / duration_ms"""
        iid = self.media_tree.identify_row(event.y)
        if not iid:
            sel = self.media_tree.selection()
            if not sel:
                return
            iid = sel[0]
        try:
            i = int(iid)
        except ValueError:
            return
        if i < 0 or i >= len(self.media_items):
            return
        self._media_open_edit_dialog(i)

    def _media_open_edit_dialog(self, i: int):
        it = self.media_items[i]

        # 小弹窗
        dlg = tk.Toplevel(self.root)
        dlg.title(f"编辑：{Path(it['path']).name}")
        dlg.transient(self.root)
        dlg.geometry("560x340")
        dlg.resizable(False, False)
        # 注意：grab_set 必须在窗口可见之后；否则报 "window not viewable"。
        # 先 update_idletasks 让窗口被映射，再 grab。
        dlg.update_idletasks()
        try:
            dlg.wait_visibility()    # 等窗口真正显示出来
            dlg.grab_set()
        except tk.TclError:
            # 某些 WM 下 wait_visibility 也会失败 —— 放弃 modal，凑活用
            pass
        dlg.columnconfigure(0, weight=0)
        dlg.columnconfigure(1, weight=1)
        # 路径可能很长，截断显示
        short_path = it["path"]
        if len(short_path) > 60:
            short_path = "..." + short_path[-57:]
        ttk.Label(dlg, text=f"文件: {short_path}").grid(row=0, column=0, columnspan=2,
                                                       sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text=f"类型: {it['kind']}").grid(row=1, column=0, columnspan=2,
                                                       sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="缩放模式 (fit):").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        fit_var = tk.StringVar(value=it["fit_mode"])
        ttk.Combobox(dlg, textvariable=fit_var,
                     values=("contain", "cover", "stretch"),
                     state="readonly", width=14).grid(row=2, column=1, padx=8, pady=4)

        ttk.Label(dlg, text="时长 (ms):").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        dur_var = tk.StringVar(value=str(it["duration_ms"]))
        ttk.Entry(dlg, textvariable=dur_var, width=14
                  ).grid(row=3, column=1, sticky="w", padx=8, pady=4)
        # 提示：什么情况下能用 0
        if it["kind"] in ("video", "stream"):
            hint = "0 = 按视频/流自然时长（跑完自动切下一个）"
        else:
            hint = "图片/GIF 没有自然时长，建议 >= 1000"
        ttk.Label(dlg, text=hint, font=("sans-serif", 9), foreground="#888"
                  ).grid(row=3, column=1, sticky="w", padx=(140, 8), pady=4)

        # 循环 + fps（仅视频/流有意义）
        loop_var = tk.BooleanVar(value=bool(it.get("loop", False)))
        fps_var = tk.StringVar(value=str(it.get("fps", 0)))
        if it["kind"] in ("video", "stream"):
            ttk.Label(dlg, text="循环:").grid(row=4, column=0, sticky="w", padx=8, pady=4)
            ttk.Checkbutton(dlg, variable=loop_var,
                            text="循环播放（视频/流跑完自动从头开始）"
                            ).grid(row=4, column=1, sticky="w", padx=8, pady=4)

            ttk.Label(dlg, text="帧率 (fps):").grid(row=5, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(dlg, textvariable=fps_var, width=14
                      ).grid(row=5, column=1, sticky="w", padx=8, pady=4)
            ttk.Label(dlg, text="0 = 自动（用源原始帧率，上限 60）",
                      font=("sans-serif", 9), foreground="#888"
                      ).grid(row=5, column=1, sticky="w", padx=(140, 8), pady=4)

        def _ok():
            try:
                dur = int(dur_var.get())
            except Exception as e:
                messagebox.showerror("错误", f"时长不是数字: {e}")
                return
            # 0 / 负 → 自然时长（仅视频/流有意义；静态源用 0 会卡住）
            if dur < 0:
                dur = 0
            if dur == 0 and it["kind"] not in ("video", "stream"):
                if not messagebox.askyesno(
                    "提示",
                    "图片/GIF 没有自然结束。设 0 会让 playlist 永远停在这一项。\n确定？"
                ):
                    return
            it["fit_mode"] = fit_var.get()
            it["duration_ms"] = dur
            if it["kind"] in ("video", "stream"):
                it["loop"] = loop_var.get()
                try:
                    fps_v = int(fps_var.get())
                    it["fps"] = max(0, fps_v)
                except Exception:
                    it["fps"] = 0
            self._media_refresh_tree()
            self._auto_save_playlist()
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.grid(row=6, column=0, columnspan=2, pady=10)
        ttk.Button(btn_row, text="确定", command=_ok).pack(side="left", padx=4)
        ttk.Button(btn_row, text="取消", command=dlg.destroy).pack(side="left", padx=4)

    def _media_to_playlist_dict(self) -> dict:
        items = []
        for it in self.media_items:
            d = {
                "kind": it["kind"], "path": it["path"],
                "fit_mode": it["fit_mode"], "duration_ms": it["duration_ms"],
            }
            if it["kind"] in ("video", "stream"):
                d["loop"] = bool(it.get("loop", True))
                d["fps"] = int(it.get("fps", 0) or 0)
            if it["kind"] == "terminal":
                d["fps"] = int(it.get("fps", 10))
                d["font_size"] = int(it.get("font_size", 14))
                # runner build_source_from_dict 既认 command 也认 path
            if it.get("name"):
                d["name"] = it["name"]
            items.append(d)
        return {
            "kind": "playlist",
            "name": "picker-playlist",
            "loop": self.media_loop_var.get(),
            "default_duration_ms": 5000,
            "items": items,
        }

    def _media_load_from_file(self, path: Path):
        import json
        with open(path) as f:
            data = json.load(f)
        items = data.get("items", [])
        self.media_loop_var.set(bool(data.get("loop", True)))
        loaded = []
        for it in items:
            kind = it.get("kind") or self._media_kind_for(it["path"]) or "image"
            entry = {
                "path": it["path"],
                "kind": kind,
                "fit_mode": it.get("fit_mode", "contain"),
                "duration_ms": int(it.get("duration_ms", 5000)),
            }
            if kind in ("video", "stream"):
                entry["loop"] = bool(it.get("loop", True))
                entry["fps"] = int(it.get("fps", 0) or 0)
            if kind == "terminal":
                entry["fps"] = int(it.get("fps", 10))
                entry["font_size"] = int(it.get("font_size", 14))
            if it.get("name"):
                entry["name"] = it["name"]
            loaded.append(entry)
        self.media_items = loaded
        self._media_refresh_tree()

    def _auto_save_playlist(self):
        """改完播放列表立刻写盘到 default_json，供 helper / 下次启动用。"""
        import json
        try:
            self.media_default_json.parent.mkdir(exist_ok=True)
            tmp = self.media_default_json.with_suffix(".json.tmp")
            with open(tmp, "w") as fh:
                json.dump(self._media_to_playlist_dict(), fh,
                          indent=2, ensure_ascii=False)
            tmp.replace(self.media_default_json)
        except Exception as e:
            self._set_status(f"自动保存失败：{e}")

    def _media_apply(self):
        if not self.media_items:
            messagebox.showinfo("提示", "播放列表为空")
            return
        import json
        # 保存到固定路径让 helper 能 root 身份读到
        self.media_default_json.parent.mkdir(exist_ok=True)
        with open(self.media_default_json, "w") as fh:
            json.dump(self._media_to_playlist_dict(), fh, indent=2, ensure_ascii=False)

        if not messagebox.askyesno(
            "确认应用",
            f"应用 {len(self.media_items)} 项媒体播放列表到副屏？\n\n"
            f"循环: {'是' if self.media_loop_var.get() else '否'}\n"
            f"JSON: {self.media_default_json}"
        ):
            return

        self._set_status("正在 pkexec 应用播放列表...")
        self.root.update_idletasks()
        ok, msg = apply_to_subscreen("playlist", str(self.media_default_json))
        if not ok:
            messagebox.showerror("应用失败", msg)
            self._set_status(f"应用失败：{msg[:120]}")
            return
        self._set_status(f"已应用播放列表 ({len(self.media_items)} 项) → 副屏")

    def _refresh_theme_list(self):
        self.current_theme = read_current_theme_from_yaml()
        self.lst.delete(0, tk.END)
        for i, t in enumerate(self.themes):
            mark = "● " if t["name"] == self.current_theme else "  "
            label = f"{mark}{t['name']}  [{t['size']}, {t['orientation']}]"
            self.lst.insert(tk.END, label)
            if t["name"] == self.current_theme:
                self.lst.selection_set(i)
                self.lst.see(i)
        self.lbl_current.config(
            text=f"config.yaml 当前主题：{self.current_theme or '(未知)'}"
        )

    def _selected_theme(self) -> dict | None:
        sel = self.lst.curselection()
        if not sel:
            return None
        return self.themes[sel[0]]

    def _on_select(self, _evt):
        t = self._selected_theme()
        if not t:
            return
        self.lbl_info.config(
            text=f"path: {t['path']}\nsize: {t['size']}  orientation: {t['orientation']}"
        )
        self._do_preview()

    def _set_status(self, text: str):
        self.lbl_status.config(text=text)

    def _do_preview(self):
        t = self._selected_theme()
        if not t:
            return
        self._set_status(f"渲染中：{t['name']}...")
        self.root.update_idletasks()

        # 同步渲染（足够快），不开线程，避免 turing 内部线程不安全
        t0 = time.monotonic()
        img, err = render_theme_in_process(t["name"])
        dt = (time.monotonic() - t0) * 1000

        if img is None:
            self.canvas.delete("img")
            self.canvas.itemconfig(self.canvas_text,
                                   text=f"渲染失败：{t['name']}",
                                   fill="#f55")
            self._set_status(f"预览失败：{err[:120] if err else '?'}")
            return

        # 缩到 PREVIEW_DISPLAY，保比例
        tw, th = img.size
        ratio = min(PREVIEW_DISPLAY_W / tw, PREVIEW_DISPLAY_H / th)
        new_size = (max(1, int(tw * ratio)), max(1, int(th * ratio)))
        img = img.resize(new_size, Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        x = (PREVIEW_DISPLAY_W - new_size[0]) // 2
        y = (PREVIEW_DISPLAY_H - new_size[1]) // 2
        self.canvas.delete("img")
        self.canvas.itemconfig(self.canvas_text, text="")
        self.canvas.create_image(x, y, image=self._photo, anchor="nw", tags="img")
        self._set_status(
            f"预览：{t['name']}  {tw}x{th} → {new_size[0]}x{new_size[1]}  耗时 {dt:.0f}ms"
        )

    def _do_apply(self):
        t = self._selected_theme()
        if not t:
            messagebox.showwarning("提示", "请先选一个主题")
            return

        # 提示：非 AM01S 原生尺寸主题会被等比缩放 + 居中 + 黑边补全
        warn = ""
        if t["size"] != "AM01S":
            warn = (
                f"\n\n💡 主题原生尺寸 = '{t['size']}'，"
                f"会被等比缩放到 960×400 居中显示（四周可能有黑边）。"
            )

        if not messagebox.askyesno(
            "确认应用",
            f"应用 {t['name']} 到副屏？\n\n"
            f"  config.yaml: THEME={t['name']}, REVISION=AM01S\n"
            f"  pkexec（弹密码框）杀旧 main.py 并后台启动新进程。"
            f"{warn}"
        ):
            return

        # 必须先写盘，因为 helper 是另一个进程读 config.yaml 的
        try:
            patch_config(t["name"], "AM01S")
        except Exception as e:
            messagebox.showerror("失败", f"改 config.yaml 失败：{e}")
            return

        self._set_status("正在 pkexec 应用到副屏...")
        self.root.update_idletasks()
        ok, msg = apply_to_subscreen()
        if not ok:
            messagebox.showerror("应用失败", msg)
            self._set_status(f"应用失败：{msg[:120]}")
            return

        self._refresh_theme_list()
        self._set_status(f"已应用 {t['name']} → 副屏")
        messagebox.showinfo(
            "应用成功",
            f"已切到 {t['name']}。\n\n"
            f"日志: {TURING_DIR / 'main.am01s.log'}\n\n"
            "副屏几秒后应该刷新到新主题。"
        )

    def _do_stop(self):
        if not messagebox.askyesno(
            "确认停止",
            "杀掉副屏 main.py 进程（副屏会黑屏）。\n\n继续？"
        ):
            return
        self._set_status("正在 pkexec 停止副屏程序...")
        self.root.update_idletasks()
        ok, msg = stop_subscreen()
        if not ok:
            messagebox.showerror("停止失败", msg)
            self._set_status(f"停止失败：{msg[:120]}")
            return
        self._set_status("已停止副屏 main.py")


def main():
    root = tk.Tk()
    try:
        try:
            import sv_ttk
            sv_ttk.set_theme("dark")
        except Exception:
            pass
        app = PickerApp(root)
        root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
