#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""火山方舟 Agent Plan「燃料(AFP)」余额查询 —— daemon + cache.json 形态

端点: POST https://ark.cn-beijing.volcengineapi.com/?Action=GetAFPUsage&Version=2024-01-01
鉴权: 仅支持 Access Key(AK/SK) 的 HMAC-SHA256 V4 签名（Agent Plan 的 ark- API Key 无效）
返回: Result.PlanType + AFPFiveHour/AFPDaily/AFPWeekly/AFPMonthly
      每窗口 {Quota, Used, SubscribeTime, ResetTime}，单位 AFP，时间戳 epoch 毫秒

用法:
    python3 volc_fuel.py                # 打印余额
    python3 volc_fuel.py --json         # 打印原始 JSON
    python3 volc_fuel.py --watch 300    # 守护模式，每 300 秒刷新 cache.json
凭据优先级: --ak/--sk > 环境变量 VOLC_ACCESS_KEY_ID/VOLC_SECRET_ACCESS_KEY > ./config.json
"""
import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HOST, REGION, SERVICE, VERSION = "ark.cn-beijing.volcengineapi.com", "cn-beijing", "ark", "2024-01-01"
ACTION = "GetAFPUsage"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CN_TZ = timezone(timedelta(hours=8))

# 数据落点：.app 模式由 router 传入 data_dir，源码模式回落到模块目录
DATA_DIR = BASE_DIR
CACHE_FILE = os.path.join(BASE_DIR, "fuel-cache.json")
CONFIG_FILE = os.path.join(BASE_DIR, "volc-fuel.json")
LOG_FILE = os.path.join(BASE_DIR, "volc_fuel.log")


def configure(data_dir=None):
    """确定凭据与缓存落点。凭据优先 data_dir，其次模块目录（源码模式）。"""
    global DATA_DIR, CACHE_FILE, CONFIG_FILE, LOG_FILE
    DATA_DIR = data_dir or BASE_DIR
    CACHE_FILE = os.path.join(DATA_DIR, "fuel-cache.json")
    LOG_FILE = os.path.join(DATA_DIR, "volc_fuel.log")
    for cand in (os.path.join(DATA_DIR, "volc-fuel.json"),
                 os.path.join(BASE_DIR, "volc-fuel.json")):
        if os.path.exists(cand):
            CONFIG_FILE = cand
            break
    else:
        CONFIG_FILE = os.path.join(DATA_DIR, "volc-fuel.json")
    return DATA_DIR
WINDOWS = [
    ("five_hour", ("afpfivehour", "fivehour", "5hour", "5h"), "5 小时"),
    ("daily", ("afpdaily", "daily", "oneday", "day"), "近一天"),
    ("weekly", ("afpweekly", "weekly", "week"), "近一周"),
    ("monthly", ("afpmonthly", "monthly", "month"), "近一月"),
]


def _sha256_hex(data):
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _hmac(key, content):
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _norm_query(params):
    parts = []
    for key in sorted(params):
        for v in (params[key] if isinstance(params[key], list) else [params[key]]):
            parts.append("%s=%s" % (urllib.parse.quote(str(key), safe="-_.~"),
                                    urllib.parse.quote(str(v), safe="-_.~")))
    return "&".join(parts).replace("+", "%20")


def sign_request(ak, sk, action=ACTION, body=None, now=None):
    """V4 签名（移植自官方 SDK SignerV4），返回 (url, headers, payload)。"""
    payload = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    x_date = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = _sha256_hex(payload)
    headers = {"Content-Type": "application/json", "Host": HOST,
               "X-Date": x_date, "X-Content-Sha256": payload_hash}
    signed_headers = ";".join(sorted(k.lower() for k in headers))
    canonical_headers = "".join("%s:%s\n" % (k.lower(), headers[k])
                                for k in sorted(headers, key=str.lower))
    canonical_query = _norm_query({"Action": action, "Version": VERSION})
    canonical_request = "\n".join(["POST", "/", canonical_query, canonical_headers,
                                   signed_headers, payload_hash])
    scope = "/".join([short_date, REGION, SERVICE, "request"])
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope, _sha256_hex(canonical_request)])
    k = _hmac(_hmac(_hmac(_hmac(sk.encode("utf-8"), short_date), REGION), SERVICE), "request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = ("HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                                % (ak, scope, signed_headers, signature))
    return "https://%s/?%s" % (HOST, canonical_query), headers, payload


def _hint(raw):
    if "InvalidAuthorization" in raw or "InvalidAccessKey" in raw:
        return "（AK/SK 有误，或误用了 Agent Plan 的 ark- 密钥）"
    if "SignatureDoesNotMatch" in raw:
        return "（签名不匹配：检查 Secret Access Key 是否带首尾空格）"
    return ""


def call_api(ak, sk, action=ACTION, timeout=15):
    url, headers, payload = sign_request(ak, sk, action=action)
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise RuntimeError("HTTP %s %s%s" % (exc.code, _hint(raw), raw[:300])) from None
    except urllib.error.URLError as exc:
        raise RuntimeError("网络不可达: %s" % exc.reason) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("返回非 JSON: %s" % raw[:300]) from None
    err = (data.get("ResponseMetadata") or {}).get("Error")
    if err:
        raise RuntimeError("接口报错 %s: %s %s" % (err.get("Code"), err.get("Message"), _hint(raw)))
    return data


def _nk(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _f(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pick(bucket, *aliases):
    for key, value in bucket.items():
        if _nk(key) in aliases:
            return value
    return None


def _ms(value):
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    return ms * 1000 if 0 < ms < 1e11 else ms


def _cn_time(value):
    ms = _ms(value)
    return datetime.fromtimestamp(ms / 1000.0, CN_TZ).strftime("%Y-%m-%d %H:%M:%S") if ms and ms > 0 else ""


def _left(value):
    ms = _ms(value)
    if not ms or ms <= 0:
        return ""
    secs = ms / 1000.0 - time.time()
    if secs <= 0:
        return "已重置"
    mins = int(secs // 60)
    if mins < 60:
        return "%d 分钟" % mins
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return "%d 小时 %d 分钟" % (hours, mins)
    days, hours = divmod(hours, 24)
    return "%d 天 %d 小时" % (days, hours)


def parse_result(data):
    result = data.get("Result") or {}
    out = {"plan_type": result.get("PlanType") or "", "windows": {}}
    for key, aliases, label in WINDOWS:
        bucket = next((v for k, v in result.items()
                       if isinstance(v, dict) and _nk(k) in set(aliases)), None)
        if bucket is None:
            continue
        quota = _f(_pick(bucket, "quota", "totalquota", "total"))
        used = _f(_pick(bucket, "used", "usedquota", "usage", "usedamount"))
        remaining = _f(_pick(bucket, "remaining", "left", "rest", "balance", "remain"))
        if remaining is None and quota is not None and used is not None:
            remaining = quota - used
        reset = _pick(bucket, "resettime", "resettimestamp", "resetat")
        out["windows"][key] = {
            "label": label, "quota": quota, "used": used, "remaining": remaining,
            "percent": round(used / quota * 100.0, 2) if quota else None,
            "reset_at": _cn_time(reset), "reset_in": _left(reset),
            "subscribe_at": _cn_time(_pick(bucket, "subscribetime", "starttime")),
        }
    return out


def load_credentials(args):
    ak = (args.ak or os.environ.get("VOLC_ACCESS_KEY_ID") or "").strip()
    sk = (args.sk or os.environ.get("VOLC_SECRET_ACCESS_KEY") or "").strip()
    if ak and sk:
        return ak, sk, "参数/环境变量"
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
        ak = ak or str(cfg.get("access_key_id", "")).strip()
        sk = sk or str(cfg.get("secret_access_key", "")).strip()
        if ak and sk:
            return ak, sk, CONFIG_FILE
    return ak, sk, ""


def write_cache(payload):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CACHE_FILE)
    os.chmod(CACHE_FILE, 0o600)


def snapshot_of(ak, sk):
    data = call_api(ak, sk)
    parsed = parse_result(data)
    return {"updated_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_ts": int(time.time()), "ok": True, "error": "",
            "source": ACTION, "plan_type": parsed["plan_type"],
            "windows": parsed["windows"], "raw": data.get("Result") or data}


def fail_snapshot(error):
    return {"updated_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_ts": int(time.time()), "ok": False, "error": error,
            "source": ACTION, "plan_type": "", "windows": {}}


def _fmt(value):
    if value is None:
        return "-"
    return "{:,}".format(int(round(value))) if abs(value - round(value)) < 1e-9 else "{:,.2f}".format(value)


def render_text(snap, cred_source=""):
    lines = ["火山方舟 Agent Plan 燃料(AFP) 余额   更新于 %s" % snap.get("updated_at", "-"),
             "档位: %s" % (snap.get("plan_type") or "-"), "-" * 60]
    for key, _a, label in WINDOWS:
        item = (snap.get("windows") or {}).get(key)
        if not item:
            lines.append("%-8s 无数据" % label)
            continue
        lines.append("%-8s 总额度 %s  已用 %s  剩余 %s  已用 %s%%  %s %s" % (
            label, _fmt(item["quota"]), _fmt(item["used"]), _fmt(item["remaining"]),
            _fmt(item["percent"]), item["reset_at"] or "-",
            ("(%s后重置)" % item["reset_in"]) if item["reset_in"] else ""))
    lines.append("-" * 60)
    if cred_source:
        lines.append("凭据来源: %s" % cred_source)
    return "\n".join(lines)


def log(msg):
    line = "[%s] %s" % (datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def run_watch(ak, sk, interval):
    log("守护启动: 每 %s 秒刷新 %s" % (interval, CACHE_FILE))
    while True:
        try:
            snap = snapshot_of(ak, sk)
            write_cache(snap)
            log("刷新成功: %s" % (", ".join("%s %s/%s" % (k, _fmt(v["used"]), _fmt(v["quota"]))
                                          for k, v in snap["windows"].items()) or "无窗口数据"))
        except Exception as exc:  # noqa: BLE001 失败也要落缓存，供 UI 显示
            write_cache(fail_snapshot(str(exc)))
            log("刷新失败: %s" % exc)
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser(description="火山方舟 Agent Plan 燃料(AFP) 余额查询")
    p.add_argument("--ak"); p.add_argument("--sk")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.add_argument("--no-cache", action="store_true", help="不写 cache.json")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="守护模式间隔")
    p.add_argument("--daemon", action="store_true", help="配合 --watch 后台运行")
    p.add_argument("--data-dir", default=None, help="数据目录（.app 模式由 router 传入）")
    args = p.parse_args()
    configure(args.data_dir)

    ak, sk, source = load_credentials(args)
    if not ak or not sk:
        print("缺少 Access Key。三种方式任选：--ak/--sk；环境变量 VOLC_ACCESS_KEY_ID / "
              "VOLC_SECRET_ACCESS_KEY；写入 %s" % CONFIG_FILE, file=sys.stderr)
        return 2

    if args.watch:
        if args.daemon:
            with open(LOG_FILE, "a") as out:
                subprocess.Popen([sys.executable, os.path.abspath(__file__), "--watch", str(args.watch)],
                                 stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            print("后台已启动，日志: %s" % LOG_FILE)
            return 0
        try:
            run_watch(ak, sk, args.watch)
        except KeyboardInterrupt:
            print("\n已停止。")
        return 0

    try:
        data = call_api(ak, sk)
    except RuntimeError as exc:
        print("查询失败: %s" % exc, file=sys.stderr)
        if not args.no_cache:
            write_cache(fail_snapshot(str(exc)))
        return 1

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    snap = snapshot_of(ak, sk)
    if not args.no_cache:
        write_cache(snap)
    print(render_text(snap, source))
    return 0


class FuelMonitor:
    """常驻刷新器：定期拉取 AFP 余额，缓存到内存 + fuel-cache.json（供菜单栏/面板读）。"""

    def __init__(self, interval=300):
        self.interval = max(60, int(interval or 300))
        self.ak, self.sk, self.source = load_credentials(argparse.Namespace(ak=None, sk=None))
        self._lock = threading.Lock()
        self._snap = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def enabled(self):
        return bool(self.ak and self.sk)

    def start(self):
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return False
        self._thread = threading.Thread(target=self._loop, name="fuel-monitor", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def refresh(self):
        try:
            snap = snapshot_of(self.ak, self.sk)
        except Exception as exc:  # noqa: BLE001 - 失败也落缓存，避免 UI 静默显示旧值
            snap = fail_snapshot(str(exc))
        with self._lock:
            self._snap = snap
        try:
            write_cache(snap)
        except OSError:
            pass
        return snap

    def snapshot(self):
        with self._lock:
            return self._snap

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)


_MONITOR = None


def start_monitor(interval=300, data_dir=None):
    """router 启动时调用；未配置凭据时返回的监视器 enabled=False。"""
    global _MONITOR
    configure(data_dir)
    if _MONITOR is None:
        _MONITOR = FuelMonitor(interval=interval)
    _MONITOR.start()
    return _MONITOR


def current_snapshot():
    return _MONITOR.snapshot() if _MONITOR else None


def stop_monitor():
    if _MONITOR:
        _MONITOR.stop()


if __name__ == "__main__":
    sys.exit(main())
