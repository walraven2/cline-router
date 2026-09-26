#!/bin/bash
# 把一张方形 PNG 打包成 macOS 的 .icns
# 用法: ./make_icon.sh <源PNG> <输出.icns>
set -e
SRC="${1:?用法: make_icon.sh <源PNG> <输出.icns>}"
OUT="${2:?用法: make_icon.sh <源PNG> <输出.icns>}"
SET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$SET"

gen() { sips -z "$1" "$1" "$SRC" --out "$SET/$2" >/dev/null; }

gen 16   icon_16x16.png
gen 32   icon_16x16@2x.png
gen 32   icon_32x32.png
gen 64   icon_32x32@2x.png
gen 128  icon_128x128.png
gen 256  icon_128x128@2x.png
gen 256  icon_256x256.png
gen 512  icon_256x256@2x.png
gen 512  icon_512x512.png
gen 1024 icon_512x512@2x.png

mkdir -p "$(dirname "$OUT")"
iconutil -c icns "$SET" -o "$OUT"
echo "已生成 $OUT"
