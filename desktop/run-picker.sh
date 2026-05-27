#!/bin/bash
# AM01S Dash Picker 启动脚本（由 .desktop 调用）
# 准备环境后启动 GUI

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AM01_DASH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 通过环境变量定位 turing-smart-screen-python 副本（用户安装时可设）
# 默认按常见位置猜
if [ -z "$AM01_DASH_TURING_DIR" ]; then
    for d in \
        "$(dirname "$AM01_DASH_DIR")/turing-smart-screen-python" \
        "$HOME/turing-smart-screen-python" \
        /opt/turing-smart-screen-python; do
        if [ -f "$d/main.py" ]; then
            export AM01_DASH_TURING_DIR="$d"
            break
        fi
    done
fi

export PYTHONPATH="${AM01_DASH_DIR}${PYTHONPATH:+:$PYTHONPATH}"

# 如果已经有一个 picker 在跑，激活已有窗口而非开第二个
if pgrep -u "$USER" -f 'python3 .*am01_dash_picker.py' >/dev/null 2>&1; then
    if command -v wmctrl >/dev/null 2>&1; then
        wmctrl -a "AM01S Dash Picker" 2>/dev/null && exit 0
    fi
    notify-send "AM01S Dash Picker" "已在运行中" 2>/dev/null || true
    exit 0
fi

cd "$AM01_DASH_DIR" || exit 1
exec python3 am01_dash_picker.py
