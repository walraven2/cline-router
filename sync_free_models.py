#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_free_models.py —— 刷新 Cline 免费模型清单

背景（2026-09-26 实测结论）
---------------------------------------------------------------
Cline 的免费模型**没有任何官方接口可查**：
  * GET /api/v1/models        返回 458 个模型，但只有 id/object/created/owned_by，
                              没有任何免费/价格标记（?free=true 被忽略）
  * GET /api/v1/users/me      只有账号信息，无权益字段
  * GET /api/v1/plans         只有套餐，无模型清单
  * 扩展内置目录 cline:{...}   只硬编码了 4 个（bunny/mimo/deepseek/muse），
                              Gemini、Pixel Canary 是服务端下发的，本地挖不到

免费模型实际分三类，判定方式各不相同：
  ① 前缀免费通道  cline-free/<裸名>      —— 唯一判定方式：探测（本脚本主逻辑）
     例：cline-free/gemini-3.8-flash → 映射到 google/gemini-3.8-flash
       （扩展源码 Yqn() 证实：去清单里找 id 等于或 endswith('/裸名') 的付费同名模型）
  ② 后缀免费      <厂商>/<名>:free       —— 清单里直接带后缀，筛出来即可
  ③ 内置直免      stealth/space-bunny-alpha 等 —— 扩展硬编码，无前缀无后缀

探测判定（对 cline-free/<裸名> 发一个最小请求）：
  200 → OK       免费可用
  429 → LIMIT    免费但今日额度用尽（"Daily free limit reached"）
  403 → BLOCKED  免费但地区受限
  404 → 不是免费通道（绝大多数模型如此）
  402 → 需付费余额

用法
---------------------------------------------------------------
  python3 sync_free_models.py                     # 探测并报告（不改配置）
  python3 sync_free_models.py --apply             # 把新发现的追加进 models.json
  python3 sync_free_models.py --apply --quiet     # 静默模式（定时任务用）
  python3 sync_free_models.py -j 16 -t 30         # 并发数 / 单请求超时

退出码：0 正常；1 出错；2 探测到新模型（便于定时任务判断）
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

APP_SUPPORT = os.path.expanduser("~/Library/Application Support/ClineRouter")
DEFAULT_CFG = os.path.join(APP_SUPPORT, "models.json")

# 扩展硬编码的内置直免模型（无 cline-free/ 前缀，探测不可发现，只能人工维护）
BUILTIN_FREE = [
    "stealth/space-bunny-alpha",
]

# 补充候选：服务端下发但不在 /models 列表里的免费模型裸名
# （如 Pixel Canary 在 458 个 id 里完全不存在，只能靠这里补齐）
EXTRA_BARE_NAMES = [
    "pixel-canary",
    "gemini-3.8-flash",
    "muse-spark-1.3-contributor",
]

STATUS_OK = "OK"          # 200：免费可用
STATUS_LIMIT = "LIMIT"    # 429 + "Daily free limit reached"：免费但今日额度用尽
STATUS_BLOCKED = "BLOCKED"  # 403：免费但地区受限
STATUS_PAID = "PAID"      # 402：需付费余额
STATUS_NO = "NO"          # 404：不是免费通道
STATUS_RATE = "RATE"      # 429 但非"免费额度"语义：被限流，需退避重试（不算结论）
STATUS_ERR = "ERR"

# 免费额度用尽的语义标记（区别于普通限流）
FREE_LIMIT_MARKERS = ("daily free limit", "free limit reached", "inference_cap_error")


# ---------------------------------------------------------------- 配置读取

def load_cfg(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cline_upstream(cfg: dict) -> tuple[str, dict]:
    up = (cfg.get("upstreams") or {}).get("cline")
    if not up:
        raise SystemExit("错误：models.json 里没有 cline 上游")
    headers = {"Content-Type": "application/json"}
    headers.update(up.get("headers") or {})
    if up.get("api_key"):
        headers["Authorization"] = "Bearer " + up["api_key"]
    return up["base_url"].rstrip("/"), headers


# ---------------------------------------------------------------- 网络

def http_json(url: str, headers: dict, timeout: int = 30, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


def fetch_all_models(base: str, headers: dict, timeout: int) -> list[str]:
    code, data = http_json(base + "/models", headers, timeout)
    if code != 200:
        raise SystemExit("拉取模型清单失败 HTTP %s: %s" % (code, str(data)[:200]))
    out = []
    for row in data.get("data") or []:
        mid = row.get("id")
        if isinstance(mid, str) and mid:
            out.append(mid)
    return sorted(set(out))


def classify(code: int, data: dict) -> str:
    """注意：必须区分「免费额度用尽」与「被限流」——两者都是 429。

    实测教训（2026-09-26）：高并发探测时会拿到 429 + HTML 错误页（Cloudflare 限流），
    若一律当作"免费额度用尽"，会把 claude-opus-5.5 这类付费模型误报成免费。
    """
    if code == 200:
        return STATUS_OK
    if code == 404:
        return STATUS_NO
    if code == 402:
        return STATUS_PAID
    if code == 403:
        return STATUS_BLOCKED
    if code == 429:
        blob = json.dumps(data, ensure_ascii=False).lower()
        if any(m in blob for m in FREE_LIMIT_MARKERS):
            return STATUS_LIMIT
        return STATUS_RATE          # 限流，重试后仍如此则不算结论
    return STATUS_ERR


def probe_model(base: str, headers: dict, model: str, timeout: int,
                retries: int = 3) -> dict:
    """发一个 max_tokens=1 的最小请求做探测（成功也仅消耗个位数 token）。

    遇到限流（RATE）时退避重试；重试用尽仍为 RATE 则标记 UNKNOWN 语义（保留 RATE）。
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    t0 = time.time()
    status, code, msg = STATUS_ERR, 0, ""
    for attempt in range(retries + 1):
        code, data = http_json(base + "/chat/completions", headers, timeout, body)
        status = classify(code, data)
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "")[:80]
        elif err:
            msg = str(err)[:80]
        if not msg and isinstance(data.get("raw"), str):
            raw = data["raw"]
            # HTML 错误页（Cloudflare 限流）取标题，别把整页塞进报告
            if raw.lstrip()[:1] == "<":
                import re as _re
                t = _re.search(r"<title[^>]*>(.*?)</title>", raw, _re.S | _re.I)
                msg = ("HTML: " + (t.group(1).strip() if t else raw[:60]))[:80]
            else:
                msg = raw[:80]
        if status != STATUS_RATE:
            break
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))     # 1.5s / 3s / 4.5s 退避
    return {"model": model, "status": status, "code": code,
            "detail": msg, "elapsed": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- 主流程

def build_candidates(model_ids: list[str]) -> tuple[list[str], list[str]]:
    """返回 (待探测的 cline-free/* 候选, 清单里直接带 :free 的模型)"""
    free_suffix = [m for m in model_ids if m.endswith(":free")]
    bare = {m.split("/")[-1] for m in model_ids
            if not m.endswith(":batch") and not m.startswith("~")}
    bare.update(EXTRA_BARE_NAMES)
    bare.update(m.split("/")[-1] for m in BUILTIN_FREE)
    return sorted("cline-free/" + b for b in bare), free_suffix


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新 Cline 免费模型清单")
    ap.add_argument("--config", default=DEFAULT_CFG, help="models.json 路径")
    ap.add_argument("--apply", action="store_true", help="把新发现的写入 models.json")
    ap.add_argument("--quiet", action="store_true", help="静默模式")
    ap.add_argument("-j", "--concurrency", type=int, default=5,
                    help="并发数；实测 >8 会被 Cloudflare 限流，建议 4~6")
    ap.add_argument("-t", "--timeout", type=int, default=30)
    args = ap.parse_args()

    def say(*a):
        if not args.quiet:
            print(*a, flush=True)

    cfg = load_cfg(args.config)
    base, headers = cline_upstream(cfg)

    say("→ 拉取模型清单 ...")
    model_ids = fetch_all_models(base, headers, args.timeout)
    say("  共 %d 个模型" % len(model_ids))

    probes, free_suffix = build_candidates(model_ids)
    say("→ 探测 %d 个 cline-free/ 候选（%d 并发）..." % (len(probes), args.concurrency))

    t0 = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(probe_model, base, headers, p, args.timeout) for p in probes]
        for i, f in enumerate(concurrent.futures.as_completed(futs), 1):
            results.append(f.result())
            if not args.quiet and i % 50 == 0:
                say("    %d/%d" % (i, len(probes)))
    say("  探测完成，耗时 %.1fs" % (time.time() - t0))

    by_status = collections.defaultdict(list)
    for r in results:
        by_status[r["status"]].append(r)

    usable = sorted(by_status[STATUS_OK] + by_status[STATUS_LIMIT] + by_status[STATUS_BLOCKED],
                    key=lambda r: (r["status"], r["model"]))
    direct = sorted(free_suffix)

    # ---------------- 报告 ----------------
    say("")
    say("=" * 68)
    say("免费模型探测结果")
    say("=" * 68)
    say("状态分布: " + ", ".join("%s=%d" % (k, len(v)) for k, v in sorted(by_status.items())))
    say("")
    say("【cline-free/ 通道】可用的 %d 个：" % len(usable))
    for r in usable:
        tag = {"OK": "✅ 可用", "LIMIT": "⏳ 额度用尽", "BLOCKED": "🚫 地区受限"}[r["status"]]
        say("   %-8s %-42s %s" % (tag, r["model"], r["detail"][:44]))
    say("")
    say("【清单自带 :free 后缀】%d 个：" % len(direct))
    for m in direct:
        say("   %s" % m)
    say("")
    say("【内置直免】%d 个：" % len(BUILTIN_FREE))
    for m in BUILTIN_FREE:
        say("   %s" % m)

    if by_status[STATUS_RATE]:
        say("")
        say("⚠️  有 %d 个候选被限流（结果不可信，建议降并发重跑）："
            % len(by_status[STATUS_RATE]))
        for r in by_status[STATUS_RATE][:10]:
            say("   %-8s %s" % (r["code"], r["model"]))
        if len(by_status[STATUS_RATE]) > 10:
            say("   ... 其余 %d 个省略" % (len(by_status[STATUS_RATE]) - 10))

    # ---------------- 与现有配置对比 ----------------
    existing = {m.get("model") for m in cfg.get("models") or []}
    discovered = [r["model"] for r in usable] + direct + BUILTIN_FREE
    new = [m for m in discovered if m not in existing]

    say("")
    say("=" * 68)
    if new:
        say("新发现（配置里还没有）%d 个：" % len(new))
        for m in new:
            say("   + %s" % m)
    else:
        say("没有新发现的免费模型，配置已是最新。")

    if new and args.apply:
        bak = args.config + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(args.config, bak)
        say("")
        say("→ 备份 %s" % os.path.basename(bak))

        raw = load_cfg(args.config)
        models = raw.setdefault("models", [])
        have = {m.get("model") for m in models}
        added = 0
        for m in new:
            if m in have:
                continue
            mid = "cline-free-" + m.replace("cline-free/", "").replace(":free", "") \
                                   .replace("/", "-").replace(".", "")
            base_id, n = mid, 1
            while mid in {x.get("id") for x in models}:
                n += 1
                mid = "%s-%d" % (base_id, n)
            models.append({"id": mid, "upstream": "cline", "model": m})
            added += 1

        tmp = args.config + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, args.config)
        say("→ 已写入 %d 个新模型到 %s" % (added, args.config))
        say("→ 生效方式：POST /api/config 触发路由器热加载（或重启 router）")

    return 2 if new else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
