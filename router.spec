# -*- mode: python ; coding: utf-8 -*-
"""pyinstaller 规格：把 router.py（含 admin_ui）冻成单文件可执行 `router`。

由 bar/build.sh 调用：
    pyinstaller router.spec --distpath build/pyi --workpath build/pyi-work --noconfirm

环境变量：
    ROUTER_ARCH = native（默认）| universal2 | x86_64 | arm64

产物：build/pyi/router —— 直接嵌进 Cline 路由.app/Contents/MacOS/router。
"""
import os

_arch = os.environ.get("ROUTER_ARCH", "native")
target_arch = None if _arch in ("", "native") else _arch

a = Analysis(
    ["router.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["admin_ui", "volc_fuel", "workbuddy_credits"],   # router.py 的本地依赖，显式声明保证一定收进来
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
    a.binaries,
    a.datas,
    [],
    name="router",
    debug=False,
    bootloader_ignore_signals=False,   # 让 launchd 的 SIGTERM 能正常传到 Python
    strip=False,
    upx=False,
    runtime_tmpdir=None,               # onefile：解压到系统临时目录再跑
    console=True,                      # 保留 stdout/stderr（launchd 重定向到日志用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
)
