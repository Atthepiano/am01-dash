# am01-dash

A content engine for the **Ayaneo AM01S** mini PC built-in 960×400 USB sub-screen on Arch Linux.

Plays turing-smart-screen system monitor themes, static images, animated GIFs, local videos, network streams (HTTP / RTSP / HLS / YouTube via yt-dlp), and arbitrary terminal programs (htop, btop, cava, cmatrix, ...) on the sub-screen, with a Tk GUI for managing a mixed playlist.

[简体中文](#中文说明) · [English](#english)

---

## English

### Background

The AM01S ships with an integrated 960×400 landscape sub-screen driven over USB by a MacroSilicon MS912x chip (USB ID `345f:9133`). The Windows vendor driver works out of the box; on Linux the situation is harder:

- The mainline `ms912x` kernel driver does not know about the 960×400 mode used by the panel.
- The chip exposes a junk EDID (it advertises a 4K display).
- Naively pushing frames at the panel produces flicker, tearing, or a blank screen.

This project addresses each of those and adds a user-space stack on top so the sub-screen can be used for monitoring dashboards, images, video, streams, and TUI programs.

### Repository layout

The project is split across three repositories. Two of them are forks of upstream projects with patches applied on an `am01s` branch.

| Repository | Upstream | Role |
|---|---|---|
| [`Atthepiano/ms912x`](https://github.com/Atthepiano/ms912x/tree/am01s) (`am01s`) | [rhgndf/ms912x](https://github.com/rhgndf/ms912x) | Kernel driver patches: new mode `0xae00` (960×400@60), heartbeat work, modern kernel compatibility |
| [`Atthepiano/turing-smart-screen-python`](https://github.com/Atthepiano/turing-smart-screen-python/tree/am01s) (`am01s`) | [mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python) | Adds an `LcdAm01s` backend bridging PIL images to the ms912x DRM dumb buffer, plus an `AM01S_demo` theme |
| `Atthepiano/am01-dash` (this repository) | — | GUI picker, content runner, and source implementations (image / gif / video / stream / terminal / playlist) |

### Requirements

- Arch Linux with a recent kernel (tested on 7.x)
- An Ayaneo AM01S with the built-in 960×400 sub-screen functional under Windows
- Python 3.11+

### Installation

#### 0. System packages

```bash
sudo pacman -S --needed \
    base-devel git dkms linux-headers \
    python python-pip python-pyserial python-yaml python-babel \
    python-numpy python-pyusb python-requests python-pystray \
    python-pycryptodome python-pillow python-pyte \
    tk sv-ttk tkinter-tooltip \
    ffmpeg yt-dlp \
    psmisc polkit
```

Python packages not in the official repositories:

```bash
sudo python3 -m pip install --break-system-packages \
    uptime ping3 GPUtil pyamdgpuinfo ruamel.yaml setuptools \
    sv-ttk tkinter-tooltip
```

Optional terminal applications used by the picker presets:

```bash
sudo pacman -S --needed \
    htop btop cava cmatrix nvtop fastfetch \
    figlet lolcat cowsay fortune-mod \
    asciiquarium pipes.sh tty-clock peaclock unimatrix
```

#### 1. Kernel driver

```bash
cd ~/Projects                    # or wherever you keep source trees
git clone -b am01s https://github.com/Atthepiano/ms912x.git
cd ms912x
sudo bash install-dkms.sh
sudo modprobe ms912x
lsmod | grep ms912x              # confirm loaded
```

#### 2. udev rule to detach the sub-screen from the desktop compositor

Without this step KWin/Mutter will try to use the sub-screen as an extended display.

```bash
sudo groupadd -f am01dash
sudo usermod -aG am01dash "$USER"        # log out and back in for group to take effect

cd ~/Projects
git clone https://github.com/Atthepiano/am01-dash.git
cd am01-dash
sudo cp udev/99-am01s-isolate.rules /etc/udev/rules.d/
sudo udevadm control --reload

# Trigger a re-bind so the rule applies without a reboot.
# The interface path (1-4.3:1.3 below) may differ on your machine; check with `lsusb -t`.
INTF=$(lsusb -t | grep -B1 ms912x | head -1 | sed 's/.*Port \([0-9]*\).*/1-\1.3/')
sudo sh -c "echo $INTF > /sys/bus/usb/drivers/ms912x/unbind; \
            sleep 1; \
            echo $INTF > /sys/bus/usb/drivers/ms912x/bind"
```

#### 3. turing theme engine

```bash
cd ~/Projects
git clone -b am01s https://github.com/Atthepiano/turing-smart-screen-python.git
```

#### 4. Configure am01-dash

```bash
# Tell am01-dash where the turing fork lives.
# Add this to ~/.profile or ~/.bashrc to persist.
export AM01_DASH_TURING_DIR="$HOME/Projects/turing-smart-screen-python"

cd ~/Projects/am01-dash

# First, start the picker once so it generates the helper scripts under tools/.
python3 am01_dash_picker.py
# (close it)

# Then install the passwordless sudo rule so the picker can apply/stop
# without a password prompt every time.
sudo bash tools/install-nopasswd.sh

# Optional: register a desktop entry in the application menu.
bash desktop/install-desktop.sh
```

### Usage

Launch the picker:

```bash
python3 am01_dash_picker.py
```

Two tabs:

- **Themes (turing)** — browse all 78 turing-smart-screen themes, preview them, apply to the sub-screen.
- **Media** — manage a mixed playlist of any of the supported source types below. Changes are auto-saved to `data/current_playlist.json`.

#### Source types

| Kind | Formats | Notes |
|---|---|---|
| Image | PNG, JPG, WebP, BMP, TIFF | |
| GIF | GIF, animated WebP, APNG | Plays at the encoded per-frame timing |
| Video | Anything ffmpeg can decode | Frame rate is auto-detected via ffprobe and capped at 60 fps |
| Stream | HTTP direct links, RTSP, RTMP, HLS, YouTube, and other yt-dlp-supported sites | YouTube requires cookies (see below) |
| Terminal | Any TUI program | Runs in a hidden PTY, parsed by pyte and rendered with Pillow |

#### Fit modes

Each item can choose one of:

- `contain` (default) — scale uniformly to fit, with black bars on the short side
- `cover` — scale uniformly to fill, cropping the long side
- `stretch` — scale non-uniformly to exactly 960×400

#### YouTube cookies

YouTube requires a logged-in cookie jar. Export one:

```bash
yt-dlp --cookies-from-browser firefox \
       --cookies "$HOME/Projects/am01-dash/data/youtube_cookies.txt" \
       --skip-download 'https://www.youtube.com/'
```

am01-dash automatically uses this file if it exists. Re-export when cookies expire.

### Architecture

```
┌─────────────┐    ┌──────────────────┐
│ picker GUI  │    │ am01_dash.runner │   main loop: pull source.next_frame()
│  (user)     │    │   (root)          │   → fit_to → flip to sub-screen
└──────┬──────┘    └────────┬─────────┘
       │ pkexec helper       │
       ▼                     ▼
┌─────────────────────────────────────┐
│  am01_dash/sources/                 │   image · gif · video · stream
│                                     │   terminal · playlist
└────────────────┬────────────────────┘
                 │ PIL.Image
                 ▼
┌─────────────────────────────────────┐
│ turing-smart-screen-python          │   bridge: PIL image →
│   library/lcd/lcd_am01s.py          │   DRM dumb buffer
└────────────────┬────────────────────┘
                 │ DRM atomic commit
                 ▼
┌─────────────────────────────────────┐
│  ms912x kernel driver (DKMS)        │   heartbeat work, mode 0xae00
└────────────────┬────────────────────┘
                 │ USB bulk OUT (YUV422)
                 ▼
            Ayaneo AM01S 960×400
```

### Acknowledgements

- [rhgndf/ms912x](https://github.com/rhgndf/ms912x) for the original Linux kernel driver.
- [mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python) for the theme rendering engine and the bundled themes.

### License

GPL-3.0. The project inherits this license from turing-smart-screen-python, which it loads at runtime. See [LICENSE](LICENSE).

---

## 中文说明

### 背景

Ayaneo AM01S 自带一块 960×400 横屏的内置副屏，由 MacroSilicon MS912x USB 显示芯片驱动 (`345f:9133`)。Windows 厂商驱动开箱即用；在 Linux 上需要解决几个问题：

- 上游 `ms912x` 内核驱动不认识 960×400 这个非标分辨率
- 芯片暴露的 EDID 是错的（写的是另一台 4K 显示器）
- 直接推帧会导致闪烁、撕裂或黑屏

本项目解决这些底层问题，并在其上提供一个用户态内容栈，支持把副屏用作监控仪表盘 / 图片 / 视频 / 流媒体 / 终端程序的显示设备。

### 仓库结构

| 仓库 | 上游 | 角色 |
|---|---|---|
| [`Atthepiano/ms912x`](https://github.com/Atthepiano/ms912x/tree/am01s) (`am01s`) | [rhgndf/ms912x](https://github.com/rhgndf/ms912x) | 内核驱动补丁：新增 mode `0xae00` (960×400@60)、心跳防熄屏、新内核兼容 |
| [`Atthepiano/turing-smart-screen-python`](https://github.com/Atthepiano/turing-smart-screen-python/tree/am01s) (`am01s`) | [mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python) | 新增 `LcdAm01s` 后端，把 PIL 图像桥接到 ms912x 的 DRM dumb buffer；附带 `AM01S_demo` 主题 |
| `Atthepiano/am01-dash` (本仓库) | — | GUI picker、内容 runner、各类内容源实现（image / gif / video / stream / terminal / playlist） |

### 安装

完整安装步骤、用法、架构图与上面英文版完全一致。中文用户可以直接对照上文操作；命令、路径、参数都不需要本地化。

### License

GPL-3.0。详见 [LICENSE](LICENSE)。
