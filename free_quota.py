#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""free_quota.py —— Cline 免费模型的「额度状态」跟踪

设计原则：**不做主动探测**（2026-09-26 用户明确要求）
---------------------------------------------------------------
全量探测 369 个候选会被 Cloudflare 限流，且免费模型只有固定的几个，探它没意义。
免费额度用尽时上游会明确回报恢复时间，直接解析即可 —— 零额外请求：

    HTTP 429
    {"error":{"code":"INFERENCE_CAP_ERROR",
              "message":"Error 429: Daily free limit reached on model
                         deepseek/deepseek-v4.1-flash. Try again in 17h 39m"}}

解析 "Try again in 17h 39m" → 算出恢复时刻 → 到期自动恢复可用，
不需要任何后台轮询。状态落盘 free-quota.json，供菜单栏 App 灰显 + 倒计时。

用法
---------------------------------------------------------------
    q = FreeQuota("/path/to/free-quota.json")
    q.mark_ok("cline-free-gemini38")
    q.mark_exhausted("cline-free-deepseek41", msg)     # 自动解析恢复时间
    q.is_available("cline-free-deepseek41")            # 到期后自动变 True
    q.pick_next(config_raw, current_alias)             # 挑下一个可用的免费模型
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

# 免费额度用尽的识别标记（必须精确，别把普通限流也当成额度用尽）
FREE_LIMIT_MARKERS = ("daily free limit", "free limit reached", "inference_cap_error")

# "Try again in 17h 39m" / "in 2h" / "in 45m" / "in 30s"
_RETRY_RE = re.compile(r"try again in\s*((?:\d+\s*[hms]\s*)+)", re.I)
_UNIT_RE = re.compile(r"(\d+)\s*([hms])", re.I)


def parse_retry_after(msg: str) -> int | None:
    """从上游报文里解析恢复所需秒数；解析不到返回 None。"""
    if not msg:
        return None
    m = _RETRY_RE.search(msg)
    if not m:
        return None
    total = 0
    for num, unit in _UNIT_RE.findall(m.group(1)):
        n = int(num)
        u = unit.lower()
        total += n * 3600 if u == "h" else n * 60 if u == "m" else n
    return total or None


def is_free_limit_error(msg: str) -> bool:
    """判断一条上游错误是否属于「免费额度用尽」。"""
    low = (msg or "").lower()
    return any(k in low for k in FREE_LIMIT_MARKERS)


def fmt_remaining(seconds: int) -> str:
    """3700 → '1h 1m'；300 → '5m'；45 → '45s'"""
    if seconds <= 0:
        return "now"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm" % m
    return "%ds" % s


class FreeQuota:
    """线程安全的免费额度状态表（落盘 JSON）。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    # ---------------- 落盘 ----------------

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("models"), dict):
                return d
        except Exception:
            pass
        return {"updated_at": 0, "models": {}}

    def _save_locked(self):
        self._data["updated_at"] = int(time.time())
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------------- 状态写入 ----------------

    def mark_ok(self, alias: str, model: str = ""):
        """请求成功：清掉欠额度状态。"""
        with self._lock:
            rec = self._data["models"].get(alias) or {}
            # 已经是 ok 就不重复写盘，省 IO
            if rec.get("status") == "ok" and not rec.get("until"):
                return
            self._data["models"][alias] = {
                "model": model or rec.get("model", ""),
                "status": "ok", "until": 0, "msg": "", "ts": int(time.time()),
            }
            self._save_locked()

    def mark_exhausted(self, alias: str, msg: str, model: str = "") -> int | None:
        """额度用尽：解析恢复时间并记录。返回剩余秒数（解析不到返回 None）。"""
        secs = parse_retry_after(msg)
        with self._lock:
            rec = self._data["models"].get(alias) or {}
            self._data["models"][alias] = {
                "model": model or rec.get("model", ""),
                "status": "exhausted",
                "until": int(time.time()) + secs if secs else 0,
                "msg": (msg or "")[:200],
                "ts": int(time.time()),
            }
            self._save_locked()
        return secs

    def mark_blocked(self, alias: str, msg: str, model: str = ""):
        """不可用（如地区受限），无恢复时间。"""
        with self._lock:
            rec = self._data["models"].get(alias) or {}
            self._data["models"][alias] = {
                "model": model or rec.get("model", ""),
                "status": "blocked", "until": 0, "msg": (msg or "")[:200],
                "ts": int(time.time()),
            }
            self._save_locked()

    # ---------------- 状态查询 ----------------

    def state_of(self, alias: str) -> dict:
        """返回 {'status': ok|exhausted|blocked|unknown, 'remaining': 秒, 'msg': ...}"""
        with self._lock:
            rec = self._data["models"].get(alias)
        if not rec:
            return {"status": "unknown", "remaining": 0, "msg": ""}
        status = rec.get("status", "unknown")
        until = int(rec.get("until") or 0)
        now = int(time.time())
        if status == "exhausted":
            if until and now >= until:          # 到期自动恢复
                return {"status": "ok", "remaining": 0, "msg": ""}
            if not until:                        # 没有恢复时间，视为长期不可用
                return {"status": "exhausted", "remaining": 0, "msg": rec.get("msg", "")}
            return {"status": "exhausted", "remaining": until - now,
                    "msg": rec.get("msg", "")}
        return {"status": status, "remaining": 0, "msg": rec.get("msg", "")}

    def is_available(self, alias: str) -> bool:
        return self.state_of(alias)["status"] not in ("exhausted", "blocked")

    def pick_next(self, raw_cfg: dict, current_alias: str) -> str | None:
        """在 free_models 组里挑下一个可用别名（跳过当前这个与不可用的）。"""
        group = raw_cfg.get("free_models") or []
        if not isinstance(group, list) or not group:
            return None
        ordered = [a for a in group if a != current_alias] + [a for a in group if a == current_alias]
        for alias in ordered:
            if alias != current_alias and self.is_available(alias):
                return alias
        return None

    # ---------------- 供面板/菜单栏读取 ----------------

    def snapshot(self, raw_cfg: dict | None = None) -> dict:
        """返回给菜单栏/面板的完整状态（含倒计时文本）。"""
        now = int(time.time())
        out = []
        group = (raw_cfg or {}).get("free_models") or []
        # 别名 → 上游真实模型名
        alias2model = {m.get("id"): m.get("model", "")
                       for m in (raw_cfg or {}).get("models") or [] if isinstance(m, dict)}
        for alias in group:
            st = self.state_of(alias)
            out.append({
                "id": alias,
                "model": alias2model.get(alias, ""),
                "status": st["status"],
                "remaining": st["remaining"],
                "remaining_text": fmt_remaining(st["remaining"]) if st["remaining"] else "",
                "until": now + st["remaining"] if st["remaining"] else 0,
                "msg": st["msg"],
            })
        return {"updated_at": int(self._data.get("updated_at") or 0), "items": out}
