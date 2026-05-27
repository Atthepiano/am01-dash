# am01-dash

让 **Ayaneo AM01S** 迷你 PC 自带的 960×400 副屏在 Arch Linux 上跑起来，并把它变成一块可玩的"内容副屏"：能放 turing 系统监控主题、图片/GIF/视频/网络流，甚至直接跑 htop / btop / cmatrix 等终端程序。

![status: working on Arch + kernel 7.x](https://img.shields.io/badge/status-working-brightgreen)
![license: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)

---

## 这是什么

Ayaneo AM01S 自带一块 960×400 横屏的内置小副屏，硬件由 MacroSilicon MS912x USB→显示芯片驱动 (`345f:9133`)，Windows 厂商驱动能用，**Linux 上原本无法正常工作**：

- 内核里的 `ms912x` 驱动不认识 960×400 这个非标分辨率
- 副屏 EDID 是个"假货"（写的是另一台 4K 显示器的 EDID）
- 即使硬塞数据上去也会立刻闪烁、撕裂、黑屏

这个项目把整套问题彻底打通：

1. 给 `ms912x` 内核驱动加了 960×400 mode 支持 + 心跳防熄屏 + 现代内核兼容
2. 让副屏在系统层面**完全独占**（被 udev 隔离出 KWin 控制，避免鼠标飞过去）
3. 在副屏上跑各种内容：
   - turing-smart-screen-python 全部 78 个主题（自适应缩放）
   - 静态图片 / GIF
   - 本地视频 / HTTP 直链 / RTSP / YouTube 等流媒体（自适应帧率 + ffmpeg 解码）
   - 任意终端 TUI 程序（htop / btop / cava / cmatrix / fastfetch ...）
   - 多源轮播 + GUI 编辑

---

## 三个 repo 各干什么

这个项目分成 3 个仓库（每个对应一个上游 fork 或原创）。**安装顺序很重要**：

| Repo | 上游 | 在这个生态里负责 |
|---|---|---|
| **[`Atthepiano/ms912x` (am01s 分支)](https://github.com/Atthepiano/ms912x/tree/am01s)** | [rhgndf/ms912x](https://github.com/rhgndf/ms912x) | Linux 内核驱动：让 ms912x USB 显示芯片支持 AM01S 的 960×400 mode |
| **[`Atthepiano/turing-smart-screen-python` (am01s 分支)](https://github.com/Atthepiano/turing-smart-screen-python/tree/am01s)** | [mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python) | 主题渲染引擎：内含我们写的 `LcdAm01s` 后端 + `AM01S_demo` 主题 |
| **`Atthepiano/am01-dash` (你正在看的)** | （原创） | GUI 主程序 + 内容引擎 + 多源播放器 |

---

## 安装（Arch Linux）

### 0. 系统依赖

```bash
sudo pacman -S --needed \
    base-devel git dkms linux-headers \
    python python-pip python-pyserial python-yaml python-babel \
    python-numpy python-pyusb python-requests python-pystray \
    python-pycryptodome python-pillow python-pyte \
    tk sv-ttk tkinter-tooltip \
    ffmpeg yt-dlp \
    psmisc polkit

# 视频/终端可选增强（按需）
# sudo pacman -S htop btop cava cmatrix nvtop fastfetch \
#                figlet lolcat cowsay fortune-mod \
#                asciiquarium pipes.sh tty-clock peaclock unimatrix
```

```bash
# pip 装 pacman 没有的
sudo python3 -m pip install --break-system-packages \
    uptime ping3 GPUtil pyamdgpuinfo ruamel.yaml setuptools sv-ttk tkinter-tooltip
```

### 1. 装内核驱动（用我们 fork 的 am01s 分支）

```bash
cd ~/Projects   # 或你想放的位置
git clone -b am01s https://github.com/Atthepiano/ms912x.git
cd ms912x
sudo bash install-dkms.sh
# 拔插 USB 或重启让模块加载
sudo modprobe ms912x
lsmod | grep ms912x   # 应该看到
```

### 2. 装 udev 规则（把副屏从 KWin 隔离）

```bash
# 建专用用户组
sudo groupadd -f am01dash
sudo usermod -aG am01dash $USER

cd ~/Projects
git clone https://github.com/Atthepiano/am01-dash.git
cd am01-dash
sudo cp udev/99-am01s-isolate.rules /etc/udev/rules.d/
sudo udevadm control --reload
# 重新插拔副屏 USB 接口让规则生效（或重启）
sudo bash -c 'echo "1-4.3:1.3" > /sys/bus/usb/drivers/ms912x/unbind
              sleep 1
              echo "1-4.3:1.3" > /sys/bus/usb/drivers/ms912x/bind'
# 注意：1-4.3:1.3 这个路径每台机器可能不同，用 lsusb -t 找你这台
```

### 3. 装 turing 主题引擎

```bash
cd ~/Projects
git clone -b am01s https://github.com/Atthepiano/turing-smart-screen-python.git
```

### 4. 配置 + 跑

```bash
# 告诉 am01_dash 在哪找 turing
export AM01_DASH_TURING_DIR="$HOME/Projects/turing-smart-screen-python"
# 永久写到 ~/.profile 或 ~/.bashrc

cd ~/Projects/am01-dash

# 装免密 sudo（picker 的"应用到副屏"按钮要用 root 杀/起 main.py）
sudo bash tools/install-nopasswd.sh
# 第一次跑 picker 会自动生成 helper 脚本，
# 跑完后再跑一次 install-nopasswd.sh 才会生效

# 装应用快捷方式（KDE/GNOME 应用菜单里能搜到）
bash desktop/install-desktop.sh

# 启动！
python3 am01_dash_picker.py
```

---

## 用法

启动 picker 后看到两个标签页：

- **主题 (turing)**：78 个 turing 系统监控主题，点击预览，"应用到副屏"
- **媒体 (图片/GIF)**：图片/GIF/视频/流/终端混合播放列表

### 媒体源支持

| 类型 | 支持格式 | 备注 |
|---|---|---|
| 图片 | PNG / JPG / WebP / BMP / TIFF | |
| GIF | GIF / animated WebP / APNG | 按原 GIF 时序播放 |
| 视频 | 任何 ffmpeg 能解的格式 | 自适应帧率（探测原片 fps，上限 60） |
| 流 | HTTP 直链 / RTSP / RTMP / HLS / YouTube / Bilibili 等 | YouTube 需要 cookies（见下） |
| 终端 | 任意 TUI 程序 | 在隐藏 PTY 里跑，pyte 解析 + Pillow 渲染 |

### 缩放模式

每个媒体源可选三种 fit：

- `contain`：等比缩放铺满长边，留黑边（默认）
- `cover`：等比缩放铺满短边，超出裁切
- `stretch`：拉伸到 960×400（变形）

### YouTube 等需要 cookies

YouTube 反爬要求登录。导出 cookies 一次性即可（之后失效再导一次）：

```bash
yt-dlp --cookies-from-browser firefox \
       --cookies $HOME/Projects/am01-dash/data/youtube_cookies.txt \
       --skip-download 'https://www.youtube.com/'
```

am01-dash 启动时自动检测这个 cookies 文件，找到就用。

### 终端预设

picker → "+ 终端命令"按钮 → 内置 12 个预设：htop / btop / bashtop / nvtop / cava / cmatrix / unimatrix / asciiquarium / pipes.sh / tty-clock / peaclock / fastfetch。每个都已经调好推荐字号。

---

## 架构（一图流）

```
┌─────────────┐   ┌──────────────────┐
│ picker GUI  │   │ am01_dash.runner │ ← 主循环：拉 source.next_frame() → fit_to → 推帧
│  (用户态)   │   │   (root 跑)       │
└──────┬──────┘   └────────┬─────────┘
       │ pkexec helper      │
       ▼                    ▼
┌─────────────────────────────────────┐
│  am01_dash/sources/                 │
│    image / gif / video / stream     │
│    terminal / playlist              │
└────────────────┬────────────────────┘
                 │ PIL.Image
                 ▼
┌─────────────────────────────────────┐
│ turing-smart-screen-python          │
│   library/lcd/lcd_am01s.py          │ ← 桥接：PIL Image → DRM dumb buffer
└────────────────┬────────────────────┘
                 │ DRM atomic commit
                 ▼
┌─────────────────────────────────────┐
│  ms912x kernel driver (DKMS)        │ ← 自定义心跳防熄屏 + 0xAE00 mode
└────────────────┬────────────────────┘
                 │ USB bulk OUT (YUV422)
                 ▼
            [ AM01S 副屏 960x400 ]
```

---

## 致谢

- **[rhgndf/ms912x](https://github.com/rhgndf/ms912x)** —— 原始 Linux 内核驱动作者
- **[mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)** —— 主题渲染引擎和 78 个开箱即用的主题
- 整个开发过程是和 [OpenCode](https://github.com/anomalyco/opencode) AI 协作完成的

---

## License

GPL-3.0 —— 因为运行时依赖 turing-smart-screen-python (GPL-3)。详见 [LICENSE](LICENSE)。
