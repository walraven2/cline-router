#!/bin/bash
# 把「Cline 路由.app」打成可分发的 DMG（拖进 Applications 即装）
#
# 前置：先构建 .app
#     bash bar/build.sh
# 打包：
#     bash make_dmg.sh
# 打包并自检（挂载 → 校验 → 卸载）：
#     bash make_dmg.sh --verify
#
# 用法参数：
#     --app <路径>   指定 .app（默认 bar/build/Cline 路由.app）
#     --out <路径>   指定输出 dmg（默认 bar/dist/Cline 路由.dmg）
#     --verify       生成后自动挂载校验再卸载
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Cline 路由"
VOL_NAME="Cline 路由"
APP="bar/build/$APP_NAME.app"
OUT="bar/dist/$APP_NAME.dmg"
DO_VERIFY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --app)    APP="$2"; shift 2 ;;
    --out)    OUT="$2"; shift 2 ;;
    --verify) DO_VERIFY=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）"; exit 1 ;;
  esac
done

[ -d "$APP" ] || { echo "找不到 App：$APP"; echo "先跑： bash bar/build.sh"; exit 1; }
[ -x "$APP/Contents/MacOS/router" ] || echo "警告：$APP 内没有路由器二进制，App 将无法拉起服务"

# ---------- 组装 DMG 内容 ----------
STAGE="$(mktemp -d)/stage"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/安装说明.txt" <<'TXT'
Cline 路由 · 安装说明
========================

一、安装
  1. 把左边的「Cline 路由.app」拖进右边的「Applications」文件夹
  2. 首次打开会被 macOS 拦一下（本包未做 Apple 公证，属正常）
     绕过方法：在「应用程序」里右键点它 → 选「打开」→ 再点「打开」
     只需做这一次，之后双击就能开

二、使用
  1. 打开后菜单栏出现「⇄」图标（不占 Dock）
  2. 点图标 →「打开配置面板…」→ 填各上游的 API Key → 保存
  3. 菜单栏「⇄」子菜单里可以切换默认模型、开关开机自启

三、Cline 侧配置（VS Code 扩展）
  Provider : OpenAI Compatible
  Base URL : http://127.0.0.1:4000/v1
  API Key  : 留空即可（本机服务默认不校验；若在面板设了口令则填同值）
  Model ID : auto          ← 固定填 auto，实际用哪个由菜单栏「默认模型」决定

四、数据位置（配置 / 日志 / 生成图片）
  ~/Library/Application Support/ClineRouter/
    models.json        配置（首次启动自动生成，含上游与模型清单；密钥只存在这里，权限 600）
    router.log         服务日志（含 OK/FAIL/AUTH/IMG 打点，排障主要看它）
    router.stderr.log  进程裸输出（崩溃 traceback 会落这里）
    images/            生成图片落盘目录

五、卸载
  1. 菜单栏「⇄」→ 关闭「开机自启」
  2. 菜单栏「⇄」→ 退出
  3. 删除 /Applications/Cline 路由.app
  4. 如需清干净：删掉 ~/Library/Application Support/ClineRouter/

六、常见问题
  · 菜单栏图标显示「⇄ ⏸」= 服务没起来。点「启动路由服务」，
    或看 ~/Library/Application Support/ClineRouter/router.log
  · 服务刚启动要等 2~4 秒（路由器是打包的单文件程序，启动时需自解压）
  · 端口被占：面板里把端口改成别的（默认 4000），保存后重启服务
TXT

# ---------- 生成 DMG ----------
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
echo "== 生成 DMG =="
hdiutil create -quiet -volname "$VOL_NAME" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
rm -rf "$(dirname "$STAGE")"
echo "  → $OUT  ($(du -h "$OUT" | cut -f1))"

# ---------- 自检：挂载 → 校验 → 卸载 ----------
if [ "$DO_VERIFY" = "1" ]; then
  echo "== 自检：挂载 DMG =="
  MNT="$(mktemp -d)"
  hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MNT" "$OUT"
  trap 'hdiutil detach -quiet "$MNT" >/dev/null 2>&1 || true; rmdir "$MNT" 2>/dev/null || true' EXIT

  fail=0
  [ -d "$MNT/$APP_NAME.app" ]                                   || { echo "  ✗ 缺少 $APP_NAME.app"; fail=1; }
  [ -x "$MNT/$APP_NAME.app/Contents/MacOS/router" ]             || { echo "  ✗ 缺少路由器二进制"; fail=1; }
  [ -x "$MNT/$APP_NAME.app/Contents/MacOS/ClineRouterBar" ]     || { echo "  ✗ 缺少菜单栏程序"; fail=1; }
  [ -L "$MNT/Applications" ]                                    || { echo "  ✗ 缺少 Applications 快捷方式"; fail=1; }
  [ -f "$MNT/安装说明.txt" ]                                     || { echo "  ✗ 缺少安装说明"; fail=1; }

  # 签名校验（ad-hoc 也能过，只是不链信任根）
  if codesign -v "$MNT/$APP_NAME.app" >/dev/null 2>&1; then
    echo "  ✓ 签名结构完整"
  else
    echo "  ! 签名校验未通过（ad-hoc 包在部分系统上如此，不影响右键打开）"
  fi

  # 确认模板里没有密钥
  TPL="$MNT/$APP_NAME.app/Contents/Resources/models.json.template"
  if [ -f "$TPL" ]; then
    if grep -qE '"api_key"[[:space:]]*:[[:space:]]*"[^"]+"' "$TPL"; then
      echo "  ✗ 模板里残留了非空 api_key！（不该发生）"; fail=1
    else
      echo "  ✓ 模板已脱敏（无任何 api_key）"
    fi
  fi

  if [ "$fail" = "0" ]; then
    echo "== 自检通过 =="
  else
    echo "== 自检失败 =="
    exit 1
  fi
fi
