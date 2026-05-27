#!/bin/bash
# 一次性配置：让 am01_dash_picker 的 helper 脚本无需密码。
# 只授权 am01dash 组对那两个固定路径的 helper 脚本免密；
# 其他任何 sudo 操作都还要密码。
#
# 用法：
#   sudo bash install-nopasswd.sh

set -e

# 通过此脚本所在位置推算 am01_dash 根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AM01_DASH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

HELPER_APPLY="$AM01_DASH_DIR/tools/am01_apply_helper.sh"
HELPER_STOP="$AM01_DASH_DIR/tools/am01_stop_helper.sh"
SUDOERS_FILE="/etc/sudoers.d/am01dash-picker"

if [ "$EUID" -ne 0 ]; then
    echo "❌ 需要 root 跑：sudo bash $0"
    exit 1
fi

# 确认两个 helper 脚本存在；如果不存在，picker 第一次跑时会创建。
if [ ! -f "$HELPER_APPLY" ] || [ ! -f "$HELPER_STOP" ]; then
    echo "⚠️  helper 脚本还没生成（路径: $HELPER_APPLY）"
    echo "    请先跑一次 picker（python3 am01_dash_picker.py）让它创建，"
    echo "    然后再回来跑本脚本。"
    exit 1
fi

# 为安全起见：helper 脚本必须 root 拥有 + 仅 root 可写
echo "🔒 锁定 helper 脚本权限（root:root, 0755）..."
chown root:root "$HELPER_APPLY" "$HELPER_STOP"
chmod 0755     "$HELPER_APPLY" "$HELPER_STOP"

# 写 sudoers 片段
echo "📝 写入 $SUDOERS_FILE..."
cat > "$SUDOERS_FILE" <<EOF
# AM01S Dash Picker 免密：允许 am01dash 组的用户跑这两个 helper 脚本
# 无需密码。helper 接受可选参数（如 theme / playlist <path>）。
# 通配符 "" 允许任意参数；helper 内部会校验合法性。
%am01dash ALL=(root) NOPASSWD: $HELPER_APPLY ""
%am01dash ALL=(root) NOPASSWD: $HELPER_STOP ""
EOF
chmod 0440 "$SUDOERS_FILE"

# 校验 sudoers 语法
echo "🧪 校验 sudoers 语法..."
if visudo -c -f "$SUDOERS_FILE"; then
    echo "✅ sudoers 配置写入并校验通过。"
else
    echo "❌ sudoers 校验失败！正在删除该文件以保护系统..."
    rm -f "$SUDOERS_FILE"
    exit 1
fi

echo ""
echo "🎉 完成。下次 picker 应用/停止时不再弹密码框。"
