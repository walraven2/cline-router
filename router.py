#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cline-router：一个 Base URL 聚合多个上游 API 的多个模型（对话 + 图像）。

Cline 侧只填一个 OpenAI Compatible 配置：
    Base URL : http://127.0.0.1:4000/v1
    API Key  : models.json 里的 auth_key
    Model ID : 固定填 auto（实际用哪个模型由路由器侧的「默认模型」决定，
               也可在面板/菜单栏随时切换，无需动 Cline）
               开了 only_auto 后 /v1/models 只返回 auto，下拉里就只有它。

保留模型名 auto：请求 model=auto 时自动路由到 default_model（当前默认模型）。

对外端点：
    GET  /ui                       图形化配置面板（含 Seedream 绘图）
    GET  /api/config               读取配置（面板用）
    POST /api/config               保存配置并热加载（面板用，需 X-Router-UI 头）
    POST /api/default              切换默认模型（面板/菜单栏用，需 X-Router-UI 头）
    POST /api/test                 连通性测试单个对话模型（面板用）
    POST /api/image                生成图片并存到本地（面板用）
    GET  /images/<file>            查看已生成的图片
    GET  /api/fuel                 火山方舟 Agent Plan 燃料余额（菜单栏用）
    GET  /api/workbuddy            WorkBuddy / CodeBuddy 积分余额（菜单栏用）
    POST /api/workbuddy/refresh    立即刷新积分（菜单栏用，需 X-Router-UI 头）
    POST /api/workbuddy/checkin    每日签到并刷新积分（菜单栏用，需 X-Router-UI 头）
    GET  /health                   健康检查
    GET  /v1/models                对话模型列表（图像模型不混进来，避免 Cline 误选）
    POST /v1/chat/completions      按 model 路由对话请求（含 SSE 流式透传）
    POST /v1/images/generations    按 model 路由图像生成请求（OpenAI images 协议）

零第三方依赖，Python 3.9+ 可用。仅监听本机地址，不对外暴露。
"""
# 诊断钩子（生产环境零开销）：ROUTER_DEBUG_FAULT=1 时，12 秒后把所有线程的 Python 堆栈
# dump 到 ROUTER_DEBUG_FAULT_FILE（默认 /tmp/router-fault.log）。
# 用于排查「打包（pyinstaller）版启动卡住、源码版正常」这类只在冻结环境复现的问题。
try:
    import os as _os
    if _os.environ.get("ROUTER_DEBUG_FAULT"):
        import faulthandler as _faulthandler
        _fault_file = open(_os.environ.get("ROUTER_DEBUG_FAULT_FILE", "/tmp/router-fault.log"), "a")
        _faulthandler.dump_traceback_later(12, file=_fault_file, exit=False)
except Exception:
    pass

import argparse
import errno
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import admin_ui
import codebuddy
import volc_fuel
import workbuddy_credits

# 路径策略：
#   - 源码模式（python3 router.py 或 bash cline-router.sh start）：HERE 取源码目录，
#     models.json / images/ / router.log 都在源码目录旁边，与旧行为一致。
#   - .app 模式（冻成二进制塞进 Cline 路由.app/Contents/MacOS/router）：__file__ 在 .app 内，
#     该目录用户不可写，必须显式 --data-dir 指向 ~/Library/Application Support/ClineRouter。
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "models.json")
IMAGES_DIR = os.path.join(HERE, "images")
STARTED_AT = time.time()


# 备用日志路径：frozen 模式（pyinstaller 冻成二进制后 __file__ 在 /var/folders/... 不可写），
# 强制写一份到 ~/Library/Application Support/ClineRouter/router.log，便于排障。
# 源码模式这里写失败也无妨，sys.stderr 那份已经够了。
_BACKUP_LOG = os.path.expanduser("~/Library/Application Support/ClineRouter/router.log")


# 备用日志：句柄常驻 + 5MB 轮转。
# 旧实现每写一行都 makedirs + open + close（高频文件 IO），且文件无限增长拖慢菜单栏读日志。
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_LOCK = threading.Lock()
_LOG_FH = None
_LOG_SIZE = 0


def _log_write_backup(line):
    """追加一行到 AppSupport 日志；句柄复用，超过上限滚动为 router.log.1。"""
    global _LOG_FH, _LOG_SIZE
    with _LOG_LOCK:
        try:
            if _LOG_FH is None:
                os.makedirs(os.path.dirname(_BACKUP_LOG), exist_ok=True)
                _LOG_SIZE = os.path.getsize(_BACKUP_LOG) if os.path.exists(_BACKUP_LOG) else 0
                _LOG_FH = open(_BACKUP_LOG, "a", encoding="utf-8")
            if _LOG_SIZE > _LOG_MAX_BYTES:
                try:
                    _LOG_FH.close()
                except Exception:
                    pass
                try:
                    if os.path.exists(_BACKUP_LOG + ".1"):
                        os.remove(_BACKUP_LOG + ".1")
                    os.replace(_BACKUP_LOG, _BACKUP_LOG + ".1")
                except OSError:
                    pass
                _LOG_FH = open(_BACKUP_LOG, "w", encoding="utf-8")
                _LOG_SIZE = 0
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
            _LOG_SIZE += len(line.encode("utf-8")) + 1
        except Exception:
            # 写失败就把句柄丢掉，下次调用重新打开（不抛异常）
            _LOG_FH = None


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    _log_write_backup(line)


class RouterHTTPServer(ThreadingHTTPServer):
    """客户端在读请求行前/写响应中途断开（Cline 取消请求、探针提前关闭连接）时，
    只记一行 CLIENT-DROP；不再让 socketserver 默认 handle_error 打整段 Traceback。"""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            log("CLIENT-DROP %s:%s 连接中断（%s）" % (client_address[0], client_address[1],
                                                    exc.__class__.__name__))
            return
        super().handle_error(request, client_address)


def _expand(value):
    """支持 ${ENV_VAR} 写法：密钥可放环境变量，不必写进配置文件。"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


class Config:
    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.path = path
        self.raw = raw  # 原始配置，配置面板读写的就是它
        self.host = raw.get("host", "127.0.0.1")
        self.port = int(raw.get("port", 4000))
        self.auth_key = _expand(raw.get("auth_key", "") or "")

        self.upstreams = {}
        for name, up in (raw.get("upstreams") or {}).items():
            mode = (up.get("mode") or "openai").strip().lower()
            if mode not in ("openai", codebuddy.MODE):
                raise ValueError("上游 %s 的 mode 只能是 openai 或 %s" % (name, codebuddy.MODE))
            # mode=codebuddy 的上游默认落在/v2/chat/completions，见 codebuddy.chat_path
            path = (up.get("path") or "").strip()
            keys = [_expand(item) for item in (up.get("api_keys") or [])]
            self.upstreams[name] = {
                "name": name,
                "mode": mode,
                "path": path,
                "base_url": (up.get("base_url") or "").rstrip("/"),
                "api_key": _expand(up.get("api_key", "") or ""),
                # 多密钥轮换（CodeBuddy 这类按账号限流的厂子和对流很有用）
                "api_keys": [_expand(k) for k in keys if isinstance(k, str) and k.strip()],
                "rotation_count": int(up.get("rotation_count", 1) or 1),
                "proxy": _expand(up.get("proxy", "") or ""),
                "timeout": int(up.get("timeout", 900)),
                "user_agent": up.get("user_agent") or "",  # 留空=透传客户端 UA，行为与直连一致
                "headers": up.get("headers") or {},
                # 某些网关（如 Cline 官方 API）非流式响应会套一层 {"data": {...}}，开了就自动拆
                "unwrap_data": bool(up.get("unwrap_data", False)),
            }

        self.models = {}
        for item in raw.get("models") or []:
            mid = item.get("id")
            up_name = item.get("upstream")
            if not mid:
                raise ValueError("models 中有条目缺少 id")
            if up_name not in self.upstreams:
                raise ValueError("模型 %r 引用了不存在的 upstream %r" % (mid, up_name))
            self.models[mid] = {"upstream": up_name, "model": item.get("model") or mid}

        self.images = {}
        for item in raw.get("images") or []:
            iid = item.get("id")
            up_name = item.get("upstream")
            if not iid:
                raise ValueError("images 中有条目缺少 id")
            if up_name not in self.upstreams:
                raise ValueError("图片模型 %r 引用了不存在的 upstream %r" % (iid, up_name))
            self.images[iid] = {
                "upstream": up_name,
                "model": item.get("model") or iid,
                "size": item.get("size") or "1024x1024",
                "path": item.get("path") or "/images/generations",
            }

        if not self.models and not self.images:
            raise ValueError("models 和 images 都是空的，至少配一个")

        # auto 保留名路由到这个模型；缺省/非法值回退到配置里的第一个对话模型
        self.default_model = (raw.get("default_model") or "").strip()
        if self.default_model not in self.models:
            self.default_model = next(iter(self.models), "")
        # only_auto=true 时 /v1/models 只暴露 auto（Cline 下拉里就只有它）
        self.only_auto = bool(raw.get("only_auto", False))


# ---------------- 配置读写与校验（配置面板用） ----------------
_CONFIG_LOCK = threading.Lock()


def validate_config(raw):
    """返回错误信息列表，空列表表示合法。"""
    errors = []
    try:
        port = int(raw.get("port", 4000))
    except (TypeError, ValueError):
        errors.append("端口必须是数字")
        port = 0
    if not (0 < port < 65536):
        errors.append("端口需在 1-65535 之间")

    upstreams = raw.get("upstreams") or {}
    if not isinstance(upstreams, dict):
        return errors + ["upstreams 必须是对象"]
    for name, up in upstreams.items():
        up = up or {}
        if not up.get("base_url"):
            errors.append("上游 %s 缺少 Base URL" % name)
        if up.get("headers") is not None and not isinstance(up.get("headers"), dict):
            errors.append("上游 %s 的额外请求头必须是 JSON 对象" % name)
        if up.get("mode") and str(up["mode"]).strip().lower() not in ("openai", codebuddy.MODE):
            errors.append("上游 %s 的 mode 只能是 openai 或 %s" % (name, codebuddy.MODE))
        if up.get("api_keys") is not None and not isinstance(up.get("api_keys"), list):
            errors.append("上游 %s 的密钥池必须是数组（一行一把 Key）" % name)

    seen = set()
    for item in raw.get("models") or []:
        item = item or {}
        mid = (item.get("id") or "").strip()
        if not mid:
            errors.append("有对话模型缺少显示 ID")
            continue
        if mid in seen:
            errors.append("对话模型 ID 重复：%s" % mid)
        seen.add(mid)
        if not (item.get("model") or "").strip():
            errors.append("对话模型 %s 缺少上游真实模型名" % mid)
        if item.get("upstream") not in upstreams:
            errors.append("对话模型 %s 引用了不存在的上游 %r" % (mid, item.get("upstream")))

    default_model = (raw.get("default_model") or "").strip()
    if default_model and default_model not in seen:
        errors.append("默认模型 %s 不在对话模型清单里" % default_model)

    for item in raw.get("images") or []:
        item = item or {}
        iid = (item.get("id") or "").strip()
        if not iid:
            errors.append("有图片模型缺少显示 ID")
            continue
        if iid in seen:
            errors.append("模型 ID 重复（对话与图片不能同名）：%s" % iid)
        seen.add(iid)
        if not (item.get("model") or "").strip():
            errors.append("图片模型 %s 缺少上游真实模型名" % iid)
        if item.get("upstream") not in upstreams:
            errors.append("图片模型 %s 引用了不存在的上游 %r" % (iid, item.get("upstream")))
        image_up = upstreams.get(item.get("upstream")) or {}
        if str(image_up.get("mode") or "openai").strip().lower() == codebuddy.MODE:
            # 适配器只处理对话协议，挂上去会静默失败，所以在保存时拦住
            errors.append("图片模型 %s 不能挂在 %s 上游（CodeBuddy 适配层只做对话）"
                          % (iid, item.get("upstream")))
    return errors


def save_config_file(path, raw):
    """先备份再原子写，避免写一半把配置写坏。"""
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # 配置文件里有上游密钥，权限一律收紧到仅本人可读
    except OSError:
        pass


def reload_config(path):
    """热加载配置；失败时保留旧配置并返回警告。"""
    with _CONFIG_LOCK:
        try:
            new_cfg = Config(path)
        except Exception as exc:  # 配置不合法：保留旧的，进程不退出
            log("配置热加载失败（继续用旧配置）：%s" % exc)
            return ["新配置未生效：%s" % exc]
        Router.config = new_cfg
        log("配置已热加载：%d 个对话模型 / %d 个图片模型 / %d 个上游"
            % (len(new_cfg.models), len(new_cfg.images), len(new_cfg.upstreams)))
    return []


# ---------------- 上游请求构造 ----------------
def build_headers(up, client_ua=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Accept-Encoding": "identity",
        "User-Agent": up["user_agent"] or client_ua or "cline-router/1.0",
    }
    headers.update(up["headers"])
    if up["api_key"]:
        headers["Authorization"] = "Bearer " + up["api_key"]
    return headers


def build_opener(up):
    proxy = up["proxy"]
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )


def upstream_call(up, payload, timeout=None, path=None):
    """向上游发一次普通（非流式）请求，返回 (status, text)。path 缺省用上游的对话路径。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        up["base_url"] + (path or up["path"] or "/chat/completions"),
        data=body, headers=build_headers(up), method="POST"
    )
    try:
        resp = build_opener(up).open(req, timeout=timeout or up["timeout"])
        return resp.status, resp.read(50000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", "replace")
    except Exception as exc:
        return 0, repr(exc)


class Router(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cline-router/1.0"
    config = None  # 由 main() 注入

    # ---------------- 基础设施 ----------------
    def log_message(self, fmt, *args):
        pass  # 屏蔽默认逐请求日志，统一走 log()

    def _send_bytes(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def _send_html(self, html):
        self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _auth_ok(self):
        key = self.config.auth_key
        if not key:
            return True
        got = (self.headers.get("Authorization") or "").strip()
        if got == key or got.lower() == "bearer " + key.lower():
            return True
        if got.lower().startswith("bearer "):
            token = got[7:].strip()
            masked = "Bearer %s…（令牌 %d 字符）" % (
                (token[:4] + "…") if len(token) > 4 else token, len(token))
        else:
            masked = "%s…（%d 字符，无 Bearer 前缀）" % (
                (got[:4] + "…") if len(got) > 4 else got, len(got))
        log("AUTH 拒绝：客户端发来 %s；面板「本机口令」为 %d 个字符。"
            "Cline 的 API Key 需与面板口令一致，或把面板口令清空。" % (masked, len(key)))
        return False

    def _ui_ok(self):
        """防跨站请求：浏览器跨域时无法带自定义头。"""
        return self.headers.get("X-Router-UI") == "1"

    # ---------------- 配置面板接口 ----------------
    def _config_payload(self):
        raw = self.config.raw
        return {
            "ok": True,
            "config_path": self.config.path,
            "uptime_s": int(time.time() - STARTED_AT),
            "host": raw.get("host", "127.0.0.1"),
            "port": raw.get("port", 4000),
            "auth_key": raw.get("auth_key", ""),
            "default_model": self.config.default_model,
            "only_auto": self.config.only_auto,
            "upstreams": raw.get("upstreams") or {},
            "models": raw.get("models") or [],
            "images": raw.get("images") or [],
            "active_models": sorted(self.config.models),
            "active_images": sorted(self.config.images),
        }

    def _handle_save_config(self):
        if not self._ui_ok():
            return self._send_json(403, {"error": {"message": "缺少 X-Router-UI 头（防跨站请求）"}})
        try:
            payload = self._read_json_body()
        except Exception as exc:
            return self._send_json(400, {"error": {"message": "请求体不合法: %s" % exc}})
        try:
            port = int(payload.get("port") or 4000)
        except (TypeError, ValueError):
            return self._send_json(400, {"error": {"message": "端口必须是数字"}})

        new_raw = {
            "host": (payload.get("host") or "127.0.0.1").strip(),
            "port": port,
            "auth_key": payload.get("auth_key", ""),
            "default_model": (payload.get("default_model") or "").strip(),
            "only_auto": bool(payload.get("only_auto", False)),
            "upstreams": payload.get("upstreams") or {},
            "models": payload.get("models") or [],
            "images": payload.get("images") or [],
        }
        try:
            errors = validate_config(new_raw)
        except Exception as exc:
            errors = ["配置校验异常: %s" % exc]
        if errors:
            return self._send_json(400, {"error": {"message": "；".join(errors)}})

        try:
            save_config_file(self.config.path, new_raw)
        except Exception as exc:
            return self._send_json(500, {"error": {"message": "写入配置失败: %s" % exc}})

        warnings = reload_config(self.config.path)
        return self._send_json(200, {
            "ok": True,
            "warnings": warnings,
            "active_models": sorted(Router.config.models),
            "active_images": sorted(Router.config.images),
        })

    def _handle_test_model(self):
        if not self._ui_ok():
            return self._send_json(403, {"error": {"message": "缺少 X-Router-UI 头（防跨站请求）"}})
        try:
            payload = self._read_json_body()
        except Exception as exc:
            return self._send_json(400, {"error": {"message": "请求体不合法: %s" % exc}})
        mid = (payload.get("id") or "").strip()
        entry = self.config.models.get(mid)
        if not entry:
            return self._send_json(404, {"error": {"message": "未知模型 %r（改完配置请先保存再测）" % mid}})
        up = self.config.upstreams[entry["upstream"]]
        started = time.time()
        probe = {
            "model": entry["model"],
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
        if up["mode"] == codebuddy.MODE:
            # CodeBuddy 只出流式，这里强制走适配层，把 SSE 聚合成 JSON 再判定，与真实调用同一路径
            status, resp, err = self._cb_open(up, probe, timeout=min(up["timeout"], 60))
            if status == 0:
                text = "连接上游失败: " + err[:300]
            elif resp is None:
                text = json.dumps(codebuddy.translate_error(status, err), ensure_ascii=False)
            else:
                text = json.dumps(codebuddy.aggregate_stream(resp, entry["model"]), ensure_ascii=False)
        else:
            probe["stream"] = False
            status, text = upstream_call(up, probe, timeout=min(up["timeout"], 60))
        ok = status == 200 and '"choices"' in text
        detail = text if len(text) <= 300 else text[:300] + "…"
        return self._send_json(200, {
            "ok": ok,
            "status": status,
            "ms": int((time.time() - started) * 1000),
            "detail": detail,
        })

    def _handle_set_default(self):
        """切换默认模型（Cline 填 auto 时实际路由到的模型）。"""
        if not self._ui_ok():
            return self._send_json(403, {"error": {"message": "缺少 X-Router-UI 头（防跨站请求）"}})
        try:
            payload = self._read_json_body()
        except Exception as exc:
            return self._send_json(400, {"error": {"message": "请求体不合法: %s" % exc}})
        mid = (payload.get("id") or "").strip()
        if mid not in self.config.models:
            return self._send_json(404, {
                "error": {"message": "未知对话模型 %r；可用: %s" % (mid, ", ".join(sorted(self.config.models)))}
            })
        raw = dict(self.config.raw)
        raw["default_model"] = mid
        try:
            save_config_file(self.config.path, raw)
        except Exception as exc:
            return self._send_json(500, {"error": {"message": "写入配置失败: %s" % exc}})
        warnings = reload_config(self.config.path)
        return self._send_json(200, {
            "ok": True,
            "warnings": warnings,
            "default_model": Router.config.default_model,
        })

    # ---------------- 图像生成 ----------------
    def _handle_image(self, from_ui):
        if from_ui:
            if not self._ui_ok():
                return self._send_json(403, {"error": {"message": "缺少 X-Router-UI 头（防跨站请求）"}})
        elif not self._auth_ok():
            return self._send_json(401, {"error": {"message":
                "invalid api key —— Cline 的 API Key 需与路由器面板里的「本机口令」一致（或在面板里把口令清空）"}})

        try:
            payload = self._read_json_body()
        except Exception as exc:
            return self._send_json(400, {"error": {"message": "请求体不合法: %s" % exc}})

        mid = (payload.get("model") or "").strip()
        entry = self.config.images.get(mid)
        if not entry:
            return self._send_json(404, {
                "error": {"message": "未知图片模型 %r；可用: %s" % (mid, ", ".join(sorted(self.config.images)))}
            })
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            return self._send_json(400, {"error": {"message": "prompt 不能为空"}})

        up = self.config.upstreams[entry["upstream"]]
        body = {"model": entry["model"], "prompt": prompt, "size": payload.get("size") or entry["size"]}
        for key, value in payload.items():  # 其它参数（seed/watermark/guidance_scale 等）原样透传
            if key not in ("model", "prompt", "size"):
                body[key] = value

        started = time.time()
        status, text = upstream_call(up, body, timeout=up["timeout"], path=entry["path"])
        log("IMG %s -> %s [%s] %s %.1fs" % (mid, entry["model"], entry["upstream"], status, time.time() - started))
        if not status:
            return self._send_json(502, {"error": {"message": "连接上游失败: %s" % text[:300]}})
        if status != 200:
            return self._send_json(status, {"error": {"message": text[:600]}})

        image_url = ""
        try:
            obj = json.loads(text)
            inner = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
            items = inner.get("data") if isinstance(inner, dict) else None
            if items:
                image_url = items[0].get("url") or ""
        except Exception:
            pass

        saved_path = ""
        saved_url = ""
        if from_ui and image_url.startswith("http"):
            saved_path = self._download_image(image_url, mid)
            if saved_path:
                saved_url = "/images/" + os.path.basename(saved_path)

        return self._send_json(200, {
            "ok": True,
            "status": status,
            "upstream_url": image_url,
            "saved": saved_path,
            "saved_url": saved_url,
            "detail": "" if image_url else text[:400],
        })

    def _download_image(self, url, mid):
        try:
            os.makedirs(IMAGES_DIR, exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "cline-router/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                head = resp.read(16)
                ext = "png"
                if head.startswith(b"\xff\xd8\xff"):
                    ext = "jpg"
                elif head.startswith(b"RIFF") and b"WEBP" in head:
                    ext = "webp"
                name = "%s-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), mid, ext)
                path = os.path.join(IMAGES_DIR, name)
                with open(path, "wb") as fh:
                    fh.write(head)
                    shutil.copyfileobj(resp, fh)
            log("IMG 已保存 %s" % path)
            return path
        except Exception as exc:
            log("IMG 保存失败：%r" % exc)
            return ""

    def _serve_image_file(self, path):
        name = os.path.basename(path.split("?")[0])
        full = os.path.join(IMAGES_DIR, name)
        if not os.path.isfile(full):
            return self._send_json(404, {"error": {"message": "图片不存在: " + name}})
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        with open(full, "rb") as fh:
            self._send_bytes(200, fh.read(), ctype)

    def _relay_json_unwrapped(self, up, payload, mid, entry):
        """非流式响应的缓冲区模式：必要时拆掉 {"data": {...}} 信封后再返回给客户端。"""
        started = time.time()
        status, text = upstream_call(up, payload, timeout=up["timeout"])
        if not status:
            log("FAIL %s -> %s [%s] 连接上游失败: %s" % (mid, entry["model"], entry["upstream"], text[:200]))
            return self._send_json(502, {"error": {"message": "连接上游失败: %s" % text[:300]}})
        body = text
        try:
            obj = json.loads(text)
            inner = obj.get("data") if isinstance(obj, dict) else None
            # 信封特征：顶层有 data 对象且里面装着 choices（顶层可能还带 success 之类的键）
            if isinstance(inner, dict) and "choices" in inner:
                obj = inner
            body = json.dumps(obj, ensure_ascii=False)
        except Exception:
            body = text
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        log("OK %s -> %s [%s] %s %.1fs %dB%s" % (
            mid, entry["model"], entry["upstream"], status,
            time.time() - started, len(raw), "（信封已拆）" if len(body) != len(text) else ""))

    # ---------------- CodeBuddy 官方协议（详见 codebuddy.py） ----------------
    def _cb_open(self, up, payload, timeout=None):
        """向 CodeBuddy 上游发一次请求。

        返回 (status, resp, error_text)：
          status=0  连接层失败（error_text 是异常/原因）
          resp=None 上游返回非 2xx（error_text 是上游响应体）
          其余      resp 是流对象，**调用方负责关闭**
        """
        body = json.dumps(codebuddy.prepare_payload(payload), ensure_ascii=False).encode("utf-8")
        api_key = codebuddy.next_api_key(up)
        if not api_key:
            return 0, None, "上游 %s 没配密钥（填 api_key 或 api_keys）" % up.get("name", "?")
        headers = codebuddy.build_headers(up, api_key, self.headers.get("User-Agent"))
        url = up["base_url"] + codebuddy.chat_path(up)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            return 200, build_opener(up).open(req, timeout=timeout or up["timeout"]), ""
        except urllib.error.HTTPError as exc:
            return exc.code, None, exc.read(4000).decode("utf-8", "replace")
        except Exception as exc:
            return 0, None, repr(exc)

    def _handle_codebuddy(self, up, payload, mid, entry):
        """CodeBuddy 上游的对话处理：客户端要流式就透传 SSE，要非流式就在本地聚合。"""
        started = time.time()
        status, resp, err = self._cb_open(up, payload)
        upstream_name = up.get("name", "?")
        if status == 0:
            log("FAIL %s -> %s [%s] 连接上游失败: %s" % (mid, entry["model"], upstream_name, err[:200]))
            return self._send_json(502, {"error": {"message": "连接上游失败: %s" % err[:300]}})
        if resp is None:
            log("FAIL %s -> %s [%s] 上游 HTTP %s: %s" % (mid, entry["model"], upstream_name, status, err[:200]))
            return self._send_json(status, codebuddy.translate_error(status, err))
        if payload.get("stream"):
            return self._relay(resp, mid, entry, started)   # 上游本来就是标准 OpenAI SSE，直接透传

        obj = codebuddy.aggregate_stream(resp, entry["model"])
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        code = 200 if "error" not in obj else 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        log("OK %s -> %s [%s] %s %.1fs %dB（流式已聚合）" % (
            mid, entry["model"], upstream_name, code, time.time() - started, len(raw)))

    # ---------------- UI 与只读端点 ----------------
    def _fuel_payload(self):
        """火山方舟 Agent Plan 燃料余额（读常驻刷新器的内存快照，不阻塞）"""
        snap = volc_fuel.current_snapshot()
        if snap is None:
            return {"ok": False, "windows": {}, "plan_type": "", "updated_at": "",
                    "error": "燃料监控未启动（缺 AK/SK，见 volc-fuel.json）"}
        return snap

    def _workbuddy_payload(self):
        """WorkBuddy / CodeBuddy 积分余额（读常驻刷新器的内存快照，不阻塞）"""
        return workbuddy_credits.current_snapshot()

    def _handle_workbuddy_refresh(self):
        """手动触发一次积分刷新（同步等接口返回，约 1~3 秒）。"""
        if not self._ui_ok():
            return self._send_json(403, {"error": {"message": "缺少 X-Router-UI 头（防跨站请求）"}})
        return self._send_json(200, workbuddy_credits.manual_refresh())

    def _handle_workbuddy_checkin(self):
        """每日签到（幂等：已签到返回 status=already），随后顺带刷新余额。"""
        if not self._ui_ok():
            return self._send_json(403, {"error": {"message": "缺少 X-Router-UI 头（防跨站请求）"}})
        result = workbuddy_credits.do_checkin()
        snapshot = workbuddy_credits.manual_refresh()
        return self._send_json(200, {"ok": result.get("status") in ("ok", "already"),
                                     "checkin": result, "snapshot": snapshot})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/ui":
            return self._send_html(admin_ui.HTML)
        if path == "/api/config":
            return self._send_json(200, self._config_payload())
        if path.startswith("/images/"):
            return self._serve_image_file(path)
        if path in ("/v1/models", "/models"):
            ids = ["auto"] if self.config.only_auto else sorted(self.config.models)
            data = [
                {"id": mid, "object": "model", "created": int(STARTED_AT), "owned_by": "cline-router"}
                for mid in ids
            ]
            return self._send_json(200, {"object": "list", "data": data})
        if path == "/api/fuel":
            return self._send_json(200, self._fuel_payload())
        if path == "/api/workbuddy":
            return self._send_json(200, self._workbuddy_payload())
        if path in ("/", "/health"):
            return self._send_json(200, {
                "ok": True,
                "uptime_s": int(time.time() - STARTED_AT),
                "config": self.config.path,
                "default_model": self.config.default_model,
                "models": sorted(self.config.models),
                "images": sorted(self.config.images),
            })
        return self._send_json(404, {"error": {"message": "not found: " + path}})

    # ---------------- 核心：按模型名路由 ----------------
    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/config":
            return self._handle_save_config()
        if path == "/api/default":
            return self._handle_set_default()
        if path == "/api/workbuddy/refresh":
            return self._handle_workbuddy_refresh()
        if path == "/api/workbuddy/checkin":
            return self._handle_workbuddy_checkin()
        if path == "/api/test":
            return self._handle_test_model()
        if path == "/api/image":
            return self._handle_image(from_ui=True)
        if path in ("/v1/images/generations", "/images/generations"):
            return self._handle_image(from_ui=False)
        if not path.endswith("/chat/completions"):
            return self._send_json(404, {"error": {"message": "只支持 /v1/chat/completions 与 /v1/images/generations"}})
        if not self._auth_ok():
            return self._send_json(401, {"error": {"message":
                "invalid api key —— Cline 的 API Key 需与路由器面板里的「本机口令」一致（或在面板里把口令清空）"}})

        try:
            payload = self._read_json_body()
        except Exception as exc:
            return self._send_json(400, {"error": {"message": "请求体不是合法 JSON: %s" % exc}})

        mid = (payload.get("model") or "").strip()
        if mid.lower() == "auto" or not mid:  # 保留名 auto（以及空 model）→ 当前默认模型
            if not self.config.default_model:
                return self._send_json(404, {
                    "error": {"message": "model=auto 但没有任何对话模型可路由，请先在配置里添加对话模型"}
                })
            mid = self.config.default_model
            payload["model"] = mid
        entry = self.config.models.get(mid)
        if not entry:
            hint = ""
            if self.config.images.get(mid):
                hint = "（%s 是图片模型，请走 /v1/images/generations）" % mid
            default_tip = "；填 auto 可用默认模型 %s" % self.config.default_model if self.config.default_model else ""
            return self._send_json(404, {
                "error": {"message": "未知模型 %r%s；可用: %s%s" % (mid, hint, ", ".join(sorted(self.config.models)), default_tip)}
            })

        up = self.config.upstreams[entry["upstream"]]
        payload["model"] = entry["model"]  # 把本地别名换成上游真实模型名

        # CodeBuddy 官方协议不走标准 OpenAI 通道：要专用请求头、强制流式，非流式需本地聚合
        if up["mode"] == codebuddy.MODE:
            return self._handle_codebuddy(up, payload, mid, entry)

        # 非流式 + 该上游开了 unwrap_data：走缓冲区模式，拆掉 {"data": ...} 信封
        if up["unwrap_data"] and not payload.get("stream"):
            return self._relay_json_unwrapped(up, payload, mid, entry)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = build_headers(up, self.headers.get("User-Agent"))
        req = urllib.request.Request(up["base_url"] + (up["path"] or "/chat/completions"),
                                     data=body, headers=headers, method="POST")

        started = time.time()
        try:
            resp = build_opener(up).open(req, timeout=up["timeout"])
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            log("FAIL %s -> %s [%s] 上游 HTTP %s" % (mid, entry["model"], entry["upstream"], exc.code))
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            return
        except Exception as exc:
            log("FAIL %s -> %s [%s] 连接上游失败: %r" % (mid, entry["model"], entry["upstream"], exc))
            return self._send_json(502, {"error": {"message": "连接上游失败: %r" % exc}})

        self._relay(resp, mid, entry, started)

    def _relay(self, resp, mid, entry, started):
        """原样透传上游响应；无 Content-Length 时用 chunked，保证 SSE 逐块抵达。"""
        length = resp.headers.get("Content-Length")
        status = getattr(resp, "status", 200) or 200
        self.send_response(status)
        self.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
        chunked = length is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", length)
        self.end_headers()

        sent = 0
        try:
            while True:
                chunk = resp.read(2048)
                if not chunk:
                    break
                sent += len(chunk)
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("CANCEL %s 客户端中断（已转发 %d 字节）" % (mid, sent))
        finally:
            try:
                resp.close()
            except Exception:
                pass
        log("OK %s -> %s [%s] %s %.1fs %dB" % (mid, entry["model"], entry["upstream"], status, time.time() - started, sent))


def main():
    ap = argparse.ArgumentParser(description="cline-router：一个 Base URL 聚合多个上游模型")
    ap.add_argument("--config", default=None,
                    help="配置文件路径，默认 <HERE>/models.json；.app 模式下由启动器传")
    ap.add_argument("--port", type=int, default=None, help="覆盖配置里的端口")
    ap.add_argument("--host", default=None, help="覆盖配置里的监听地址")
    ap.add_argument("--data-dir", default=None,
                    help="数据目录（包含 models.json 与 images/）。不传则默认 HERE。"
                         ".app 模式由启动器传 ~/Library/Application Support/ClineRouter")
    args = ap.parse_args()

    # 确定数据目录：--data-dir 优先，其次 --config 的父目录，最后 HERE
    data_dir = args.data_dir or HERE
    if args.config is None:
        args.config = os.path.join(data_dir, "models.json")
    if args.data_dir:
        # 数据目录由启动器指定，确保存在；images/ 也归这里
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, "images"), exist_ok=True)

    # 单实例锁（flock）：同一 data_dir 只允许一个 router。
    # 开机时 launchd 的 router agent 与菜单栏 App 的自动拉起可能同时启动两个实例，
    # pyinstaller onefile 同时自解压会互相拖死（表现为进程在跑却不监听端口、不写日志）。
    # 拿不到锁的一方安静等待（不抢端口），直到持锁实例退出再接管。
    _lock_fh = None
    try:
        import fcntl as _fcntl
        _lock_path = os.path.join(data_dir, ".router.lock")
        _lock_fh = open(_lock_path, "w")
        try:
            _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            _lock_fh.close()
            _lock_fh = None
            for _ in range(90):          # 已有实例持锁：等它退出，最多 90 秒
                time.sleep(1)
                try:
                    _lock_fh = open(_lock_path, "w")
                    _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except OSError:
                    if _lock_fh:
                        _lock_fh.close()
                    _lock_fh = None
            if _lock_fh is None:
                log("已有 router 实例在运行（单实例锁被占），本进程退出")
                raise SystemExit(0)
        _lock_fh.write("%d\n" % os.getpid())
        _lock_fh.flush()
    except SystemExit:
        raise
    except Exception:
        _lock_fh = None                  # 异常情况下退化为无锁，绝不阻塞启动

    try:
        cfg = Config(args.config)
    except Exception as exc:
        log("配置无法加载（%s）：%s" % (args.config, exc))
        raise SystemExit(1)
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host
    Router.config = cfg
    # images 也走 data_dir（Config 自带 images 路径计算，复用同一基准）
    global IMAGES_DIR
    IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(args.config)) or HERE, "images")
    os.makedirs(IMAGES_DIR, exist_ok=True)

    # 启动竞态兜底：.app 的 launchd KeepAlive 与手动启动可能短时抢同一端口，
    # 旧实例退出瞬间 bind 会撞 EADDRINUSE —— 只对这一种错误重试 3 次（每次重建 server 对象）。
    srv = None
    for attempt in range(1, 4):
        try:
            srv = RouterHTTPServer((cfg.host, cfg.port), Router)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                log("端口 %d 无法监听（%s）：%s" % (cfg.port, exc.__class__.__name__, exc))
                raise SystemExit(1)
            if attempt >= 3:
                log("端口 %d 连续 3 次被占用，退出。请先退出菜单栏里的旧实例，"
                    "或用 lsof -nP -iTCP:%d -sTCP:LISTEN 查看占用进程" % (cfg.port, cfg.port))
                raise SystemExit(1)
            log("端口 %d 被占用（旧实例可能正在退出），第 %d/3 次重试…" % (cfg.port, attempt))
            time.sleep(1.5)
    log("cline-router 已启动  http://%s:%d/v1" % (cfg.host, cfg.port))
    log("配置 %s" % cfg.path)
    log("对话模型 %d 个: %s" % (len(cfg.models), ", ".join(sorted(cfg.models))))
    log("默认模型（Cline 填 auto 时用它）：%s" % (cfg.default_model or "（无对话模型）"))
    if cfg.only_auto:
        log("only_auto 已开启：/v1/models 只暴露 auto")
    log("图片模型 %d 个: %s" % (len(cfg.images), ", ".join(sorted(cfg.images))))
    log("上游 %d 个: %s" % (len(cfg.upstreams), ", ".join(sorted(cfg.upstreams))))
    if cfg.auth_key:
        log("本机口令：已开启（Cline 的 API Key 需与面板口令一致）")
    else:
        log("本机口令：未设置 → 任何 Key 都通过（服务仅监听 %s，外网访问不到）" % cfg.host)
    log("配置面板 http://%s:%d/ui" % (cfg.host, cfg.port))
    # 火山方舟 Agent Plan 燃料监控（AK/SK 来自 data_dir/volc-fuel.json 或仓库根）
    fuel = volc_fuel.start_monitor(300, data_dir)
    if fuel.enabled:
        log("燃料监控：已启动（每 300s 刷新，凭据 %s）" % fuel.source)
    else:
        log("燃料监控：未启动（缺 %s）" % os.path.join(data_dir, "volc-fuel.json"))
    # WorkBuddy / CodeBuddy 积分监控（登录令牌自动发现：~/.workbuddy-status/config.json
    # 或 macOS 桌面端 CodeBuddyExtension auth/*.info，无需手工配置）
    credits = workbuddy_credits.start_monitor(300, data_dir)
    log("积分监控：已启动（每 300s 刷新，凭据 %s）" % credits.source)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        volc_fuel.stop_monitor()
        workbuddy_credits.stop_monitor()
        log("收到中断，退出")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # pyinstaller freeze 后 stderr 可能不可见,显式写到 AppSupport/router.log 便于排障
        import traceback
        try:
            os.makedirs(os.path.dirname(_BACKUP_LOG), exist_ok=True)
            with open(_BACKUP_LOG, "a", encoding="utf-8") as _f:
                _f.write("\n[FATAL] " + traceback.format_exc())
        except Exception:
            pass
        traceback.print_exc()
        raise SystemExit(1)
