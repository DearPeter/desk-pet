#!/bin/bash
# macOS 启动器：双击即可运行（Finder 中双击 .command 文件）
# 优先使用同目录下的 .venv，其次回退到系统 python3。
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    echo "未找到 Python 3。请先安装 Python 3.9+。"
    read -r -p "按回车键关闭…"
    exit 1
fi

"$PY" - <<'CHECK' 2>/dev/null
import numpy, PIL, AppKit
CHECK
if [ $? -ne 0 ]; then
    echo "缺少依赖。请先执行："
    echo "    python3 -m venv .venv"
    echo "    ./.venv/bin/python -m pip install numpy pillow pyobjc-framework-Cocoa pyobjc-framework-Quartz"
    read -r -p "按回车键关闭…"
    exit 1
fi

exec "$PY" desk_pet_mac.py "$@"
