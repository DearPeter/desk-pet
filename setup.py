#!/usr/bin/env python3
"""py2app 打包配置：把 desk_pet_mac.py 打成 macOS .app 应用。

构建：
    ./.venv/bin/python setup.py py2app

产物：dist/杰哥桌宠.app
     ~70-90 MB，包含 numpy / Pillow / PyObjC + 全部 gifs/

未签名/未公证：仅供本机使用。跨机/网络分发会被 Gatekeeper 拦截。
"""

from setuptools import setup
import os

APP = ["desk_pet_mac.py"]
DATA_FILES = [
    ("gifs", [os.path.join("gifs", f) for f in os.listdir("gifs") if f.endswith(".gif")]),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "pet_icon.icns",
    "plist": {
        "CFBundleName": "杰哥桌宠",
        "CFBundleDisplayName": "杰哥桌宠",
        "CFBundleIdentifier": "com.dearpeter.deskpet",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": "",
        # 透明桌宠不应在 Dock 显示独立图标（让 activator policy 由代码控制）
        "LSUIElement": False,
        # 不希望被自动激活抢焦点
        "LSBackgroundOnly": False,
    },
    "packages": [
        "numpy",
        "PIL",
        "objc",
        "AppKit",
        "Foundation",
        "Quartz",
    ],
    "excludes": [
        "tkinter",
        "test",
        "unittest",
        "setuptools",
        "pip",
        "wheel",
        "pkg_resources",
    ],
    # 跳过 numpy/Pillow 的 docs/tests/示例，砍体积
    "strip": True,
    "optimize": 1,
    # 显式 includes：包含 stdlib 模块以及 PyObjCTools 这种 namespace 包
    # （py2app 的 imp.find_module 无法解析没有 __init__.py 的 namespace 包）
    "includes": [
        "urllib.request",
        "urllib.error",
        "json",
        "glob",
        "io",
        "subprocess",
        "threading",
        "time",
        "pathlib",
        "PyObjCTools",
        "PyObjCTools.AppHelper",
    ],
}

setup(
    app=APP,
    name="杰哥桌宠",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app>=0.28"],
)