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

# ---------- 1) 冻结 Python 路由器（onedir）----------
# 注意：必须是 onedir。onefile 每次启动都要解压内嵌运行时到 /var/folders/，
# 解压出的 .so 是新 inode，macOS 每次都要重做一遍签名校验 → 启动 28~35 秒（实测）。
echo "== 冻结路由器（arch=$BUILD_ARCH, onedir）=="
mkdir -p "$ROOT/build"
PYI_LOG="$ROOT/build/pyi.log"
rm -rf "$ROOT/build/pyi"
if ! (cd "$ROOT" && ROUTER_ARCH="$BUILD_ARCH" "$PYI" router.spec \
        --distpath build/pyi --workpath build/pyi-work --noconfirm >"$PYI_LOG" 2>&1); then
  echo "冻结失败，日志尾部："
  tail -25 "$PYI_LOG"
  exit 1
fi
[ -x "$ROOT/build/pyi/router/router" ] || { echo "冻结未产出 build/pyi/router/router"; exit 1; }
[ -d "$ROOT/build/pyi/router/_internal" ] || { echo "冻结未产出 build/pyi/router/_internal"; exit 1; }
echo "  → build/pyi/router/  ($(du -sh "$ROOT/build/pyi/router" | cut -f1))"

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
# 布局必须跟随 PyInstaller 6 在 macOS 的约定：
#   Contents/MacOS/router     ← launcher（plist / Swift 里写死的路径，不变）
#   Contents/Frameworks/*     ← 运行时（即 onedir 的 _internal 内容）
# 为什么不能把 _internal 放 Contents/MacOS/ 下：launcher 一旦发现自己在
# *.app/Contents/MacOS/ 里，就会把 sys._MEIPASS 定位到 ../Frameworks，
# 实测报 "Failed to load Python shared library .../Contents/Frameworks/Python3"。
rm -rf "$APP/Contents/Frameworks"
mkdir -p "$APP/Contents/Frameworks"
cp -R "$ROOT/build/pyi/router/_internal/." "$APP/Contents/Frameworks/"
cp "$ROOT/build/pyi/router/router" "$APP/Contents/MacOS/router"
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
# 先签 Frameworks 里所有 Mach-O，再签两层可执行，最后签整个 .app。
# onedir 里这一步不能省：未签名（或每次构建签名都变）的 Mach-O 会让 macOS
# 每次 dlopen 都重做完整校验，那正是 onefile 慢的病根；签好并保持文件位置稳定后，
# 系统才能复用校验缓存 → 启动 <1 秒。
if [ -d "$APP/Contents/Frameworks" ]; then
  signed=0
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q "Mach-O"; then
      codesign --force --sign - "$f" >/dev/null 2>&1 && signed=$((signed+1))
    fi
  done < <(find "$APP/Contents/Frameworks" -type f -print0 2>/dev/null)
  echo "  已 ad-hoc 签名 Frameworks 内 Mach-O：$signed 个"
fi
codesign --force --sign - "$APP/Contents/MacOS/router" >/dev/null 2>&1 || true
codesign --force --sign - "$APP/Contents/MacOS/$BIN"   >/dev/null 2>&1 || true
if codesign --force --sign - "$APP" >/dev/null 2>&1; then
  echo "  .app 外层签名 OK"
elif codesign --force --deep --sign - "$APP" >/dev/null 2>&1; then
  echo "  .app 外层签名 OK（--deep）"
else
  echo "  ! .app 外层未签名（内层已逐个签好，不影响 launchd 直接拉起与启动速度）"
fi
echo "== 已生成 $APP =="
echo "   运行时 Frameworks $(du -sh "$APP/Contents/Frameworks" | cut -f1) / 菜单栏 App $(du -h "$APP/Contents/MacOS/$BIN" | cut -f1)"

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
