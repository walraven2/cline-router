#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy / CodeBuddy「积分」余额查询 —— daemon + cache.json 形态

端点（默认 https://copilot.tencent.com，可被 ~/.workbuddy-status/config.json 覆盖）:
    POST /billing/meter/get-user-resource-summary   积分包余额（Packages[]）
    POST /billing/meter/checkin-activity-status     签到活动状态
    POST /billing/meter/daily-checkin               每日签到（幂等，10001=已签到）
鉴权: Authorization: Bearer <桌面端登录令牌(JWT)>，另带 X-User-Id

凭据发现（越靠前越优先）:
    1. ~/.workbuddy-status/config.json 的 accessToken / 环境变量 WORKBUDDY_ACCESS_TOKEN
    2. macOS 桌面端登录文件:
       ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info
       （取 auth.accessToken + account.uid，新→旧逐一验证）
    3. ~/.config/CodeBuddy CN/automations/automations.db 里的 JWT（含 WAL 兜底）

用法:
    python3 workbuddy_credits.py              # 打印积分
    python3 workbuddy_credits.py --json       # 输出快照 JSON
    python3 workbuddy_credits.py --checkin    # 执行每日签到
    python3 workbuddy_credits.py --watch 300  # 守护模式，每 300 秒刷新 cache.json

与 WorkBuddyStatus 小工具共享 ~/.workbuddy-status/ 下的偏好令牌指纹与签到标记，
保证「菜单栏 / 桌面小工具」两处显示的签到状态一致。
"""
import argparse
import base64
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".workbuddy-status")
SHARED_CONFIG = os.path.join(STATE_DIR, "config.json")
SHARED_PREF = os.path.join(STATE_DIR, ".token-fingerprint")
SHARED_CHECKIN_MARK = os.path.join(STATE_DIR, ".checkin-mark")

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
CHECKIN_STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"
CHECKIN_ALREADY_CODE = 10001
EXPLICIT_SOURCE = "配置文件 / 环境变量"

# macOS 桌面端登录信息目录（与 WorkBuddyStatus 小工具一致）
AUTH_DIRS = [
    os.path.join(HOME, "Library/Application Support/CodeBuddyExtension/Data/Public/auth"),
    os.path.join(HOME, "Library/Application Support/CodeBuddyExtension/Data/Public"),
    os.path.join(HOME, ".workbuddy/auth"),
    os.path.join(HOME, ".codebuddy/auth"),
]
# CodeBuddy CN 的 SQLite 登录库（Linux / 部分版本）
AUTOMATION_DBS = [os.path.join(HOME, ".config/CodeBuddy CN/automations/automations.db")]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CN_TZ = timezone(timedelta(hours=8))

# 数据落点：.app 模式由 router 传入 data_dir，源码模式回落到模块目录
DATA_DIR = BASE_DIR
CACHE_FILE = os.path.join(BASE_DIR, "workbuddy-cache.json")
LOG_FILE = os.path.join(BASE_DIR, "workbuddy_credits.log")

_JWT = r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}"
JWT_RE = re.compile(_JWT)
JWT_RE_BYTES = re.compile(_JWT.encode())

# 套餐代码 -> 中文名（与 WorkBuddyStatus 内置表一致）
PACKAGE_NAMES = {
    "TCACA_code_001_PqouKr6QWV": "免费版",
    "TCACA_code_002_AkiJS3ZHF5": "专业版（月）",
    "TCACA_code_003_FAnt7lcmRT": "专业版（年）",
    "TCACA_code_005_maRGyrHhw1": "专业版 Plus（月）",
    "TCACA_code_006_DbXS0lrypC": "专业版试用",
    "TCACA_code_007_nzdH5h4Nl0": "成长计划（活动）",
    "TCACA_code_008_cfWoLwvjU4": "专业版（按日）",
    "TCACA_code_009_0XmEQc2xOf": "积分加油包",
    "TCACA_code_023_4xbGhMrE6q": "青春版",
    "TCACA_code_026_BaESVICNoi": "高级版",
    "TCACA_code_027_0FCGVA6vSa": "旗舰版",
    "TCACA_code_028_NtpWi0jzXs": "奖励积分 A",
    "TCACA_code_029_6wCGEWquYy": "奖励积分 B",
    "TCACA_code_030_BjSt89qTvr": "奖励积分 C",
    "TCACA_code_035_ArVxJcGDsm": "专业版（国际）",
    "TCACA_code_036_lupO5WgNdG": "积分包（国际）",
    "TCACA_code_037_WxOD3MpI2o": "奖励积分（国际）",
    "TCACA_code_038_OhvqZtiPKr": "积分包 D",
    "TCACA_code_039_KRcQj7wUat": "专业版试用（月）",
    "TCACA_code_040_mi9rCYg46x": "专业版试用（年）",
}


def configure(data_dir=None):
    """确定缓存/日志落点。"""
    global DATA_DIR, CACHE_FILE, LOG_FILE
    DATA_DIR = data_dir or BASE_DIR
    CACHE_FILE = os.path.join(DATA_DIR, "workbuddy-cache.json")
    LOG_FILE = os.path.join(DATA_DIR, "workbuddy_credits.log")
    return DATA_DIR


def log(msg):
    line = "[%s] %s" % (datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def package_name(code):
    if code in PACKAGE_NAMES:
        return PACKAGE_NAMES[code]
    parts = str(code or "").split("_")
    if "code" in parts:
        i = parts.index("code")
        if i + 1 < len(parts):
            return "套餐 %s" % parts[i + 1]
    return code or "未知套餐"


# ---------------- 基础工具 ----------------
def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value):
    if value is None:
        return "-"
    return "{:,}".format(int(round(value))) if abs(value - round(value)) < 0.005 else "{:,.2f}".format(value)


def _cn_now():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def jwt_payload(token):
    """本地解析 JWT payload（不校验签名）。"""
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad).decode("utf-8", "replace"))
    except Exception:
        return {}


def jwt_expiry(token):
    exp = jwt_payload(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


# ---------------- 配置 / 凭据发现 ----------------
class Settings(object):
    def __init__(self):
        self.endpoint = DEFAULT_ENDPOINT
        self.access_token = ""
        self.user_id = ""


def load_settings():
    s = Settings()
    try:
        with open(SHARED_CONFIG, encoding="utf-8") as fh:
            root = json.load(fh)
        if root.get("endpoint"):
            s.endpoint = str(root["endpoint"])
        if root.get("accessToken"):
            s.access_token = str(root["accessToken"]).strip()
        if root.get("userId"):
            s.user_id = str(root["userId"]).strip()
    except (OSError, ValueError):
        pass
    if os.environ.get("WORKBUDDY_ENDPOINT"):
        s.endpoint = os.environ["WORKBUDDY_ENDPOINT"]
    if os.environ.get("WORKBUDDY_ACCESS_TOKEN"):
        s.access_token = os.environ["WORKBUDDY_ACCESS_TOKEN"].strip()
    if os.environ.get("WORKBUDDY_USER_ID"):
        s.user_id = os.environ["WORKBUDDY_USER_ID"].strip()
    return s


def _tokens_in_sqlite(path):
    """从 SQLite 库里捞出所有 JWT（含原始字节兜底，覆盖未 checkpoint 的 WAL）。"""
    found = set()
    if not os.path.exists(path):
        return found
    try:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f")
        con = sqlite3.connect(uri, uri=True)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for (table,) in cur.fetchall():
            try:
                cur.execute("SELECT * FROM %s" % table)
                for row in cur.fetchall():
                    for value in row:
                        if isinstance(value, str):
                            found.update(JWT_RE.findall(value))
                        elif isinstance(value, bytes):
                            found.update(m.decode() for m in JWT_RE_BYTES.findall(value))
            except sqlite3.Error:
                continue
        con.close()
    except sqlite3.Error as exc:
        log("查询 %s 失败：%s" % (path, exc))
    for extra in (path, path + "-wal"):
        try:
            with open(extra, "rb") as fh:
                found.update(m.decode() for m in JWT_RE_BYTES.findall(fh.read()))
        except OSError:
            pass
    return found


def _info_file_credentials():
    """扫描桌面端 auth/*.info，新→旧返回 [(token, uid, 来源名)]。"""
    out = []
    for directory in AUTH_DIRS:
        files = []
        for pattern in ("*.info", "*.json"):
            files.extend(glob.glob(os.path.join(directory, pattern)))
        files = sorted(set(files),
                       key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                       reverse=True)
        for path in files:
            try:
                with open(path, encoding="utf-8") as fh:
                    root = json.load(fh)
            except (OSError, ValueError):
                continue
            auth = root.get("auth") or {}
            token = str(auth.get("accessToken") or "").strip()
            # 新版本桌面端可能把令牌加密成 {$wbEncrypted: ...} 信封，这里只收明文 JWT
            if not token or token.count(".") != 2 or not token.startswith("eyJ"):
                continue
            account = root.get("account") or {}
            uid = str(account.get("uid") or account.get("userId") or "").strip()
            nickname = account.get("nickname") or account.get("userName") or ""
            if not isinstance(nickname, str):
                nickname = ""
            label = nickname.strip() or os.path.basename(path)
            if uid:
                label = "%s（%s）" % (label, uid[:8])
            out.append((token, uid, label))
    return out


def _current_account_uid():
    """桌面端「当前登录账号」的 uid：取最新 .info 的 account.uid。

    新版桌面端会把 accessToken 加密成 $wbEncrypted 信封（无法直接使用），
    但 account.uid 仍是明文——用它来挑出与桌面端同一账号的明文令牌。
    """
    newest_file, newest_mtime = "", 0.0
    for directory in AUTH_DIRS:
        for pattern in ("*.info", "*.json"):
            for path in glob.glob(os.path.join(directory, pattern)):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_file, newest_mtime = path, mtime
    if not newest_file:
        return ""
    try:
        with open(newest_file, encoding="utf-8") as fh:
            root = json.load(fh)
    except (OSError, ValueError):
        return ""
    account = root.get("account") or {}
    return str(account.get("uid") or account.get("userId") or "").strip()


def read_preferred_fingerprint():
    for path in (SHARED_PREF, os.path.join(DATA_DIR, "workbuddy-token.txt")):
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            pass
    return ""


def write_preferred_fingerprint(fingerprint):
    for path in (SHARED_PREF, os.path.join(DATA_DIR, "workbuddy-token.txt")):
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(fingerprint)
            return
        except OSError:
            continue


def _today_str():
    return time.strftime("%Y-%m-%d")


def _checkin_mark_paths():
    return [SHARED_CHECKIN_MARK, os.path.join(DATA_DIR, "workbuddy-checkin-mark")]


def locally_checked_in():
    """今天是否已成功签到过（本地标记，服务端 today_checked_in 不可靠）。"""
    today = _today_str()
    for path in _checkin_mark_paths():
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read().strip() == today:
                    return True
        except OSError:
            continue
    return False


def mark_checked_in_today():
    for path in _checkin_mark_paths():
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_today_str())
            return
        except OSError:
            continue


def candidate_credentials(settings):
    """按「越可能有效」排序返回候选：偏好指纹 > 新签发 > 长令牌。"""
    items = []
    seen = set()
    now = int(time.time())

    def push(token, source, uid="", strict_iss=False):
        token = (token or "").strip()
        if not token or token in seen:
            return
        payload = jwt_payload(token)
        exp = payload.get("exp")
        exp = int(exp) if isinstance(exp, (int, float)) else 0
        if exp and exp < now:
            return
        if strict_iss:
            iss = str(payload.get("iss") or "")
            if "codebuddy" not in iss and "copilot" not in iss:
                return
        seen.add(token)
        items.append({
            "token": token,
            "uid": uid or str(payload.get("sub") or ""),
            "source": source,
            "explicit": source == EXPLICIT_SOURCE,
            "exp": exp,
            "iat": int(payload.get("iat") or 0),
            "fingerprint": hashlib.md5(token.encode()).hexdigest(),
        })

    if settings.access_token:
        push(settings.access_token, EXPLICIT_SOURCE, settings.user_id)

    for token, uid, source in _info_file_credentials():
        push(token, source, uid)  # .info 一定是桌面端自己的令牌，不做 iss 过滤

    for db in AUTOMATION_DBS:
        for token in _tokens_in_sqlite(db):
            push(token, db, strict_iss=True)

    preferred = read_preferred_fingerprint()
    current_uid = _current_account_uid()
    items.sort(key=lambda it: (
        not it["explicit"],                                # 手工配置 > 自动发现
        it["fingerprint"] != preferred,                    # 上次验证通过的令牌
        bool(current_uid) and it["uid"] != current_uid,    # 与桌面端当前登录账号同 uid
        -it["iat"],                                        # 新签发优先
        -len(it["token"]),
    ))
    return items


# ---------------- 接口调用 ----------------
class AuthError(RuntimeError):
    """令牌不可用（HTTP 401/403 或业务码 401/40301）——可以换下一个候选。"""


def api_post(settings, path, token, uid, timeout=20):
    url = settings.endpoint.rstrip("/") + path
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Language", "zh")
    req.add_header("User-Agent", "WorkBuddyStatus/1.0 (ClineRouter)")
    req.add_header("Authorization", "Bearer %s" % token)
    if uid:
        req.add_header("X-User-Id", uid)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code in (401, 403):
            raise AuthError("HTTP %s（令牌不可用）：%s" % (exc.code, body[:200])) from None
        raise RuntimeError("HTTP %s：%s" % (exc.code, body[:300] or exc.reason)) from None
    except urllib.error.URLError as exc:
        raise RuntimeError("网络不可达: %s" % exc.reason) from None
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise RuntimeError("返回非 JSON: %s" % raw[:300]) from None
    code = data.get("code")
    if code in (401, 40301):
        raise AuthError("接口错误 %s：%s" % (code, data.get("msg") or ""))
    if code not in (0, None):
        raise RuntimeError("接口错误 %s：%s" % (code, data.get("msg")))
    return data


def _post_checkin(settings, token, uid, timeout=20):
    """POST 签到接口，返回 (payload, http_status)。

    注意：服务端用 HTTP 400 承载业务码 10001（今天已签到），4xx 不能直接当失败——
    必须读响应体里的业务码，否则「今日已签」会被误报成签到失败。
    """
    url = settings.endpoint.rstrip("/") + CHECKIN_PATH
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Language", "zh")
    req.add_header("User-Agent", "WorkBuddyStatus/1.0 (ClineRouter)")
    req.add_header("Authorization", "Bearer %s" % token)
    if uid:
        req.add_header("X-User-Id", uid)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        status = exc.code
    try:
        return json.loads(raw or "{}"), status
    except ValueError:
        return {"code": None, "msg": (raw or "")[:200]}, status


def fetch_summary(settings, token, uid, timeout=20):
    """拉取积分包 + 签到状态，返回统一快照 dict。"""
    payload = api_post(settings, SUMMARY_PATH, token, uid, timeout)
    data = payload.get("data") or {}
    packages = []
    for item in data.get("Packages") or []:
        code = item.get("PackageCode") or ""
        packages.append({
            "code": code,
            "name": package_name(code),
            "total": _num(item.get("CycleTotalCapacity")),
            "remain": _num(item.get("CycleRemainCapacity")),
            "used": _num(item.get("CycleUsedCapacity")),
            "frozen": _num(item.get("CycleFrozenCapacity")),
            "unit": item.get("CapacityUnit") or "credits",
        })
    packages.sort(key=lambda p: -p["remain"])

    checkin = None
    try:
        cdata = (api_post(settings, CHECKIN_STATUS_PATH, token, uid, timeout).get("data")) or {}
        checkin = {
            "active": bool(cdata.get("active")),
            "today": bool(cdata.get("today_checked_in")),
            "streak": int(_num(cdata.get("streak_days"))),
            "daily": _num(cdata.get("daily_credit")),
            "week": int(_num(cdata.get("week_checkin_days"))),
            "season": cdata.get("season"),
            "activity": cdata.get("activity_name") or "",
        }
        if locally_checked_in():  # 服务端标记不可靠：本地标记优先
            checkin["today"] = True
            checkin["local_mark"] = True
    except AuthError:
        raise
    except Exception as exc:
        log("签到状态接口失败：%s" % exc)

    total_remain = sum(p["remain"] for p in packages)
    total_cap = sum(p["total"] for p in packages)
    return {
        "ok": True,
        "updated_at": int(time.time()),
        "updated_at_text": _cn_now(),
        "token_expires_at": jwt_expiry(token),
        "is_paid": bool(data.get("IsPaidUser")),
        "packages": packages,
        "checkin": checkin,
        "total_remain": total_remain,
        "total_capacity": total_cap,
        "total_used": sum(p["used"] for p in packages),
        "ratio": (total_remain / total_cap) if total_cap > 0 else 0.0,
        "error": "",
    }


def fetch_with_fallback(settings=None, timeout=20):
    """依次尝试候选凭据，返回 (快照, 命中凭据)。全部失败则抛最后一个错误。"""
    settings = settings or load_settings()
    candidates = candidate_credentials(settings)
    if not candidates:
        raise RuntimeError("未找到 WorkBuddy 登录令牌，请先登录 WorkBuddy / CodeBuddy 桌面端")
    last_error = None
    for cred in candidates[:12]:
        try:
            data = fetch_summary(settings, cred["token"], cred["uid"], timeout)
        except AuthError as exc:
            last_error = exc
            log("令牌 %s… 不可用（%s），换下一个" % (cred["fingerprint"][:8], exc))
            continue
        data["cred_source"] = cred["source"]
        data["cred_fingerprint"] = cred["fingerprint"][:8]
        data["cred_expires_at"] = cred["exp"]
        write_preferred_fingerprint(cred["fingerprint"])
        return data, cred
    if last_error is not None:
        raise last_error
    raise RuntimeError("所有候选令牌都不可用")


def do_checkin(timeout=20):
    """执行每日签到（幂等）。返回 dict：status in ok/already/error。"""
    settings = load_settings()
    candidates = candidate_credentials(settings)
    if not candidates:
        return {"status": "error", "message": "未找到 WorkBuddy 登录令牌，请先登录桌面端"}
    last_error = None
    for cred in candidates[:12]:
        try:
            payload, http_status = _post_checkin(settings, cred["token"], cred["uid"], timeout)
        except Exception as exc:  # 网络类错误换令牌没有意义，直接返回
            return {"status": "error", "message": "签到请求失败：%s" % exc}
        code = payload.get("code")
        data = payload.get("data") or {}
        if code == 0:
            write_preferred_fingerprint(cred["fingerprint"])
            mark_checked_in_today()
            return {
                "status": "ok",
                "message": "签到成功",
                "credit": _num(data.get("credit") or payload.get("credit")),
                "streak": int(_num(data.get("streak_days") or data.get("streakDays"))),
            }
        if code == CHECKIN_ALREADY_CODE or "已签" in str(payload.get("msg") or ""):
            write_preferred_fingerprint(cred["fingerprint"])
            mark_checked_in_today()
            return {
                "status": "already",
                "message": payload.get("msg") or "今天已签到",
                "streak": int(_num(data.get("streak_days") or data.get("streakDays"))),
            }
        if code in (401, 40301) or http_status in (401, 403):
            last_error = AuthError("接口错误 %s：%s" % (code, payload.get("msg") or ""))
            log("令牌 %s… 不可用（%s），换下一个" % (cred["fingerprint"][:8], last_error))
            continue
        return {"status": "error", "message": "接口错误 %s：%s" % (code, payload.get("msg") or "")}
    return {"status": "error", "message": str(last_error or "所有候选令牌都不可用")}


# ---------------- 缓存 / 快照 ----------------
def write_cache(payload):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        log("写入缓存失败：%s" % exc)


def read_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def fail_snapshot(error):
    return {
        "ok": False,
        "updated_at": int(time.time()),
        "updated_at_text": _cn_now(),
        "error": str(error),
        "packages": [],
        "checkin": None,
        "total_remain": 0,
        "total_capacity": 0,
        "total_used": 0,
        "ratio": 0.0,
    }


def snapshot_of(payload):
    """截取给菜单栏的字段（包含错误态）。"""
    if not payload:
        return fail_snapshot("尚未查询")
    if not payload.get("ok"):
        return payload
    fields = ("ok", "updated_at", "updated_at_text", "token_expires_at", "is_paid",
              "packages", "checkin", "total_remain", "total_capacity", "total_used",
              "ratio", "cred_source", "cred_fingerprint", "cred_expires_at", "error")
    return {key: payload.get(key) for key in fields}


def refresh_once():
    """同步刷新一次并落盘，返回快照。"""
    try:
        data, _cred = fetch_with_fallback()
        write_cache(data)
        log("刷新成功：剩余 %s / %s（%s）" % (_fmt(data["total_remain"]),
                                              _fmt(data["total_capacity"]),
                                              data.get("cred_source") or "-"))
        return snapshot_of(data)
    except Exception as exc:
        snap = fail_snapshot(exc)
        write_cache(snap)
        log("刷新失败：%s" % exc)
        return snap


def render_text(payload):
    """命令行输出。"""
    if not payload or not payload.get("ok"):
        return "WorkBuddy 积分: 查询失败：%s" % ((payload or {}).get("error") or "未知错误")
    lines = ["WorkBuddy 积分（更新于 %s）" % (payload.get("updated_at_text") or "-")]
    lines.append("剩余 %s / %s（%s）" % (_fmt(payload["total_remain"]),
                                        _fmt(payload["total_capacity"]),
                                        "%.1f%%" % (100 * payload.get("ratio", 0))))
    for p in payload.get("packages") or []:
        lines.append("  · %s  %s / %s" % (p["name"], _fmt(p["remain"]), _fmt(p["total"])))
    checkin = payload.get("checkin")
    if checkin:
        lines.append("签到：%s，连续 %s 天，本周 %s 天%s" % (
            "今日已签" if checkin.get("today") else "今日未签",
            checkin.get("streak", 0), checkin.get("week", 0),
            "（每日 %s 分）" % _fmt(checkin.get("daily")) if checkin.get("daily") else ""))
    exp = payload.get("token_expires_at")
    if exp:
        left = exp - int(time.time())
        if left < 3 * 86400:
            lines.append("令牌：%s后过期（请打开桌面端刷新登录）" % (
                "已过期" if left <= 0 else "%.1f 天" % (left / 86400.0)))
    return "\n".join(lines)


# ---------------- 守护 / monitor ----------------
class CreditsMonitor(object):
    """后台刷新线程：与 FuelMonitor 同款接口，供 router 内嵌使用。"""

    def __init__(self, interval=300):
        self.interval = max(60, int(interval))
        self.source = "未发现令牌"
        self._lock = threading.Lock()
        self._snapshot = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        cached = read_cache()
        if cached:
            with self._lock:
                self._snapshot = snapshot_of(cached)
        try:
            candidates = candidate_credentials(load_settings())
            if candidates:
                self.source = candidates[0]["source"]
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            return self

        def loop():
            log("WorkBuddy 积分监控启动，间隔 %ss" % self.interval)
            while not self._stop.is_set():
                snap = refresh_once()
                with self._lock:
                    self._snapshot = snap
                if snap.get("ok") and snap.get("cred_source"):
                    self.source = snap["cred_source"]
                if self._stop.wait(self.interval):
                    break
            log("WorkBuddy 积分监控停止")

        self._thread = threading.Thread(target=loop, name="workbuddy-credits", daemon=True)
        self._thread.start()
        return self

    def refresh(self):
        """手动触发一次同步刷新（返回新快照）。"""
        snap = refresh_once()
        with self._lock:
            self._snapshot = snap
        return snap

    def current(self):
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
        cached = read_cache()
        if cached:
            with self._lock:
                self._snapshot = snapshot_of(cached)
            return self._snapshot
        return None

    def stop(self):
        self._stop.set()


_MONITOR = None
_MONITOR_LOCK = threading.Lock()


def start_monitor(interval=300, data_dir=None):
    global _MONITOR
    if data_dir:
        configure(data_dir)
    with _MONITOR_LOCK:
        if _MONITOR is None:
            _MONITOR = CreditsMonitor(interval)
            _MONITOR.start()
        return _MONITOR


def stop_monitor():
    global _MONITOR
    with _MONITOR_LOCK:
        if _MONITOR is not None:
            _MONITOR.stop()
            _MONITOR = None


def current_snapshot():
    monitor = _MONITOR
    if monitor is not None:
        snap = monitor.current()
        if snap is not None:
            return snap
    cached = read_cache()
    if cached:
        return snapshot_of(cached)
    return fail_snapshot("尚未查询")


def manual_refresh():
    monitor = _MONITOR
    if monitor is not None:
        return monitor.refresh()
    return refresh_once()


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy / CodeBuddy 积分查询")
    parser.add_argument("--json", action="store_true", help="输出快照 JSON")
    parser.add_argument("--check", action="store_true", help="仅检查凭据发现")
    parser.add_argument("--checkin", action="store_true", help="执行每日签到")
    parser.add_argument("--refresh", action="store_true", help="强制刷新一次并打印")
    parser.add_argument("--watch", type=int, nargs="?", const=300, metavar="SECONDS",
                        help="守护模式（默认 300 秒）")
    parser.add_argument("--data-dir", help="缓存/日志目录（默认脚本目录）")
    args = parser.parse_args()

    configure(args.data_dir)
    settings = load_settings()

    if args.check:
        candidates = candidate_credentials(settings)
        print("端点: %s" % settings.endpoint)
        print("候选令牌: %d 个" % len(candidates))
        for cred in candidates:
            exp = cred.get("exp")
            when = datetime.fromtimestamp(exp, CN_TZ).strftime("%Y-%m-%d %H:%M") if exp else "-"
            print("  · %s…  来源: %s  过期: %s" % (cred["fingerprint"][:8], cred["source"], when))
        return 0

    if args.checkin:
        result = do_checkin()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("[%s] %s%s" % (result.get("status"), result.get("message", ""),
                                 "（+%s 分）" % _fmt(result.get("credit")) if result.get("credit") else ""))
        snapshot = refresh_once()
        if not args.json:
            print(render_text(snapshot))
        return 0 if result.get("status") in ("ok", "already") else 1

    if args.watch:
        data = refresh_once()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(render_text(data))
        stop = threading.Event()
        try:
            while not stop.wait(args.watch):
                refresh_once()
        except KeyboardInterrupt:
            return 0
        return 0

    data = refresh_once()
    if args.json or args.refresh:
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render_text(data))
    else:
        print(render_text(data))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
