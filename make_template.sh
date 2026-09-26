#!/bin/bash
# 从 models.json 生成脱敏模板 models.template.json（所有 api_key / auth_key 一律置空）
#
# 为什么必须用它：模板会被 bar/build.sh 打进 .app、进而进 DMG 分发给别人，
# 手改极易漏掉某个上游的密钥。本脚本生成后自带断言，有残留就非零退出。
#
#   bash make_template.sh
set -e
cd "$(dirname "$0")"

python3 - <<'PY'
import json
import re

cfg = json.load(open("models.json", encoding="utf-8"))

# 上游密钥逐条清空
for _name, up in (cfg.get("upstreams") or {}).items():
    if isinstance(up, dict) and "api_key" in up:
        up["api_key"] = ""
# 本机口令清空
cfg["auth_key"] = ""

with open("models.template.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")

# 自检：模板里不能有任何非空密钥（api_key 与 auth_key 都查）
raw = open("models.template.json", encoding="utf-8").read()
leaks = re.findall(r'"(?:api_key|auth_key)"\s*:\s*"([^"]+)"', raw)
if leaks:
    raise SystemExit("模板仍有 %d 处非空密钥，已中止：%s" % (len(leaks), [s[:4] + "…" for s in leaks]))

print("已生成 models.template.json —— 0 处密钥残留")
print("  上游：%s" % ", ".join((cfg.get("upstreams") or {}).keys()))
print("  对话模型 %d 个 / 图片模型 %d 个" % (len(cfg.get("models") or []), len(cfg.get("images") or [])))
PY
