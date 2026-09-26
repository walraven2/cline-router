#!/bin/bash
# 编译并打包「Cline 路由.app」= 菜单栏 App(Swift) + 冻结的 Python 路由器(router)
#
#   ./build.sh           仅编译到 bar/build/
#   ./build.sh run       编译并运行
#   ./build.sh install   编译并安装到程序目录（覆盖旧的）
#   ./build.sh clean     清理
#
# 环境变量：
#   BUILD_ARCH=universal   同时构建 x86_64 + arm64（默认 native，只建本机架构）
#   PYINSTALLER=<路径>     指定 pyinstaller（默认自动探测）
#
# 生成物结构：
#   Cline 路由.app/Contents/MacOS/ClineRouterBar   菜单栏 App（Swift）
#   Cline 路由.app/Contents/MacOS/router           路由器（Python 冻结，单文件）
#   Cline 路由.app/Contents/Resources/AppIcon.icns
#   Cline 路由.app/Contents/Resources/models.json.template   首次启动用的脱敏配置模板
set -e
cd "$(dirname "$0")"

APP_NAME="Cline 路由"
BIN="ClineRouterBar"
APP="build/$APP_NAME.app"
ROOT="$(cd .. && pwd)"          # 程序目录（脚本开头已 cd 到 bar/，上一级就是根）
BUILD_ARCH="${BUILD_ARCH:-native}"

if [ "${1:-build}" = "clean" ]; then
  rm -rf build "$ROOT/build/pyi" "$ROOT/build/pyi-work"
  echo "已清理 build/"
  exit 0
fi

# ---------- 0) 定位 pyinstaller ----------
PYI="${PYINSTALLER:-}"
if [ -z "$PYI" ]; then
  if command -v pyinstaller >/dev/null 2>&1; then
    PYI="$(command -v pyinstaller)"
  elif [ -x "$HOME/Library/Python/3.9/bin/pyinstaller" ]; then
    PYI="$HOME/Library/Python/3.9/bin/pyinstaller"
  else
    echo "找不到 pyinstaller。安装： pip3 install --user pyinstaller"
    exit 1
  fi
fi

# ---------- 1) 冻结 Python 路由器 ----------
echo "== 冻结路由器（arch=$BUILD_ARCH）=="
mkdir -p "$ROOT/build"
PYI_LOG="$ROOT/build/pyi.log"
rm -rf "$ROOT/build/pyi"
if ! (cd "$ROOT" && ROUTER_ARCH="$BUILD_ARCH" "$PYI" router.spec \
        --distpath build/pyi --workpath build/pyi-work --noconfirm >"$PYI_LOG" 2>&1); then
  echo "冻结失败，日志尾部："
  tail -25 "$PYI_LOG"
  exit 1
fi
[ -x "$ROOT/build/pyi/router" ] || { echo "冻结未产出 build/pyi/router"; exit 1; }
echo "  → build/pyi/router  ($(du -h "$ROOT/build/pyi/router" | cut -f1))"

# ---------- 2) 编译菜单栏 App ----------
echo "== 编译 $BIN =="
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

compile_swift() {   # $1 = 输出路径；其余参数透传给 swiftc
  local out="$1"; shift
  swiftc -O -o "$out" Sources/*.swift -framework AppKit -framework Foundation "$@"
}

if [ "$BUILD_ARCH" = "universal" ]; then
  compile_swift /tmp/cline-bar-x86_64 -target x86_64-apple-macos11
  compile_swift /tmp/cline-bar-arm64  -target arm64-apple-macos11
  lipo -create -output "$APP/Contents/MacOS/$BIN" /tmp/cline-bar-x86_64 /tmp/cline-bar-arm64
  rm -f /tmp/cline-bar-x86_64 /tmp/cline-bar-arm64
  echo "  → 通用二进制（x86_64 + arm64）"
else
  compile_swift "$APP/Contents/MacOS/$BIN"
fi

# ---------- 3) 组装 .app ----------
# 路由器二进制进 Contents/MacOS/，菜单栏 App 直接拉它（不再依赖源码目录里的 .sh）
cp "$ROOT/build/pyi/router" "$APP/Contents/MacOS/router"
chmod +x "$APP/Contents/MacOS/router"

# 配置模板：首次启动时 App 拷到 ~/Library/Application Support/ClineRouter/models.json
if [ -f "$ROOT/models.template.json" ]; then
  cp "$ROOT/models.template.json" "$APP/Contents/Resources/models.json.template"
fi

# 图标（复用程序目录下的 icon.png）
if [ -f "$ROOT/icon.png" ]; then
  if [ ! -f "$ROOT/AppIcon.icns" ]; then
    bash "$ROOT/make_icon.sh" "$ROOT/icon.png" "$ROOT/AppIcon.icns" >/dev/null
  fi
  cp "$ROOT/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>local.cline-router.bar</string>
    <key>CFBundleExecutable</key>
    <string>$BIN</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# ---------- 4) ad-hoc 签名 ----------
# 先签内层两个可执行，再签整个 .app（--deep 对 ad-hoc 够用；有 Developer ID 时换 --sign "Developer ID Application: ..."）
codesign --force --sign - "$APP/Contents/MacOS/router" >/dev/null 2>&1 || true
codesign --force --sign - "$APP/Contents/MacOS/$BIN"   >/dev/null 2>&1 || true
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "（跳过 ad-hoc 签名）"
echo "== 已生成 $APP =="
echo "   路由器二进制 $(du -h "$APP/Contents/MacOS/router" | cut -f1) / 菜单栏 App $(du -h "$APP/Contents/MacOS/$BIN" | cut -f1)"

case "${1:-build}" in
  run)
    open "$APP"
    echo "已启动，看菜单栏右侧的 ⇄ 图标"
    ;;
  install)
    DEST="$ROOT/$APP_NAME.app"
    rm -rf "$DEST"
    cp -R "$APP" "$DEST"
    xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
    touch "$DEST"
    echo "== 已安装到 $DEST =="
    ;;
esac
