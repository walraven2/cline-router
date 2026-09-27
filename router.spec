# -*- mode: python ; coding: utf-8 -*-
"""pyinstaller 规格：把 router.py（含本地模块）冻成「onedir」目录形态。

为什么是 onedir 而不是 onefile（2026-09-27 实测）：
    onefile 每次启动都要先把内嵌运行时解压到 /var/folders/.../_MEIxxxx/，
    解压出的 .so / dylib 每次都是全新 inode，macOS 的代码签名校验（amfid）
    无法复用缓存，于是每次启动都要把几十个二进制重新校验一遍。
    实测：`router --help` 要 28.7 秒（CPU 仅 0.3 秒，几乎全是等待），
    完整启动到 /health 可响应要 35 秒；同一份代码用系统 python3 跑只要 0.43 秒。
    onedir 不做解压、文件位置固定，校验结果可被系统缓存 → 启动 <1 秒。

产物：build/pyi/router/            ← 目录（launcher 可执行）
      build/pyi/router/_internal/  ← 全部运行时依赖
由 bar/build.sh 整体铺进 Cline 路由.app/Contents/MacOS/。

环境变量：
    ROUTER_ARCH = native（默认）| universal2 | x86_64 | arm64
"""
import os

_arch = os.environ.get("ROUTER_ARCH", "native")
target_arch = None if _arch in ("", "native") else _arch

a = Analysis(
    ["router.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["admin_ui", "codebuddy", "free_quota", "responses_api",
                   "volc_fuel", "workbuddy_credits"],   # router.py 的本地依赖，显式声明保证一定收进来
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 路由器只用标准库，把常见的大块第三方排除掉，产物更小
    excludes=[
        "numpy", "playwright", "yt_dlp", "PIL", "matplotlib",
        "pandas", "scipy", "tkinter", "IPython", "pytest", "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,             # onedir：二进制不打进 launcher，交给 COLLECT
    name="router",
    debug=False,
    bootloader_ignore_signals=False,   # 让 launchd 的 SIGTERM 能正常传到 Python
    strip=False,
    upx=False,
    console=True,                      # 保留 stdout/stderr（launchd 重定向到日志用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="router",
)
