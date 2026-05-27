#!/bin/bash
# 把 .desktop 文件装到用户应用菜单
#
# 用法：
#   bash install-desktop.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AM01_DASH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DESKTOP_DST_DIR="$HOME/.local/share/applications"
ICON_DST_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

mkdir -p "$DESKTOP_DST_DIR" "$ICON_DST_DIR"

# 由模板生成最终 .desktop（替换占位符路径）
DESKTOP_OUT="$DESKTOP_DST_DIR/am01-dash-picker.desktop"
sed "s|@AM01_DASH_DIR@|$AM01_DASH_DIR|g" \
    "$SCRIPT_DIR/am01-dash-picker.desktop.in" > "$DESKTOP_OUT"
chmod 644 "$DESKTOP_OUT"
echo "📝 wrote $DESKTOP_OUT"

# 拷贝图标
cp -v "$SCRIPT_DIR/am01-dash-picker.png" "$ICON_DST_DIR/am01-dash-picker.png"

# 刷新桌面数据库
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DST_DIR" 2>/dev/null || true
fi

if command -v desktop-file-validate >/dev/null 2>&1; then
    echo "🧪 校验 .desktop 文件..."
    desktop-file-validate "$DESKTOP_OUT" && \
        echo "✅ desktop 文件校验通过"
fi

echo ""
echo "🎉 安装完成。打开应用菜单搜索 'AM01S' 应该能看到。"
