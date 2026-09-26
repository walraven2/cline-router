#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CodeBuddy 官方接口的协议适配层（cline-router 的一种「上游类型」）。

背景：CodeBuddy 官方的服务不是 OpenAI 兼容的，普通 OpenAI 客户端接不上：
  1. 端点是  {base_url}/v2/chat/completions（不是标准的 /chat/completions）
  2. 认证要 Authorization: Bearer <key> 与 X-API-Key: <key> 同时给
  3. 必须带一堆 X-Conversation-* / X-IDE-* / x-stainless-* 请求头
  4. 只支持流式：客户端要非流式时，必须在本层把 SSE 聚合成一个 JSON
  5. messages 只有 1 条 user 时会被拒，需要补一条 system

协议细节参考 https://github.com/Sliverkiss/CodeBuddy2api
（src/codebuddy_api_client.py 的头部构造 + src/codebuddy_router.py 的流式聚合，
 轮换思路来自 src/codebuddy_token_manager.py）。本模块按本项目的零依赖约束重写：
只用标准库，不用 httpx / asyncio。

对外接口刻意做成纯函数 + 模块级轮换表，router.py 在
/v1/chat/completions 与 /api/test 两条路径上复用同一套逻辑。
"""

import json
import secrets
import threading
import time
import uuid
from urllib.parse import urlparse

MODE = "codebuddy"
DEFAULT_CHAT_PATH = "/v2/chat/completions"
DEFAULT_UA = "CLI/1.0.7 CodeBuddy/1.0.7"
FALLBACK_SYSTEM = "You are a helpful assistant."


# ---------------- 请求路径与密钥 ----------------
def chat_path(up):
    """上游对话端点路径（Config 已按 mode 算好默认值，这里只做兜底）。"""
    path = (up.get("path") or "").strip()
    if not path:
        path = DEFAULT_CHAT_PATH if up.get("mode") == MODE else "/chat/completions"
    return path if path.startswith("/") else "/" + path


def effective_keys(up):
    """可用密钥列表：api_keys（多）优先，api_keys 为空时用单个 api_key。去重保序。"""
    keys = []
    for item in (up.get("api_keys") or []):
        item = str(item or "").strip()
        if item:
            keys.append(item)
    single = str(up.get("api_key") or "").strip()
    if single and single not in keys:
        keys.insert(0, single)
    seen, out = set(), []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


class _KeyRing(object):
    """单个上游的密钥轮换器：每 rotation_count 次请求换下一把。"""

    def __init__(self, keys, rotation_count):
        self._lock = threading.Lock()
        self._keys = list(keys)
        self._rotation = max(1, int(rotation_count or 1))
        self._index = 0
        self._calls = 0

    def pick(self, keys, rotation_count):
        with self._lock:
            if keys != self._keys:          # 密钥清单变了（配置热加载）：从头开始计人次
                self._keys = list(keys)
                self._index = 0
                self._calls = 0
            self._rotation = max(1, int(rotation_count or 1))
            if not self._keys:
                return ""
            key = self._keys[self._index % len(self._keys)]
            self._calls += 1
            if self._calls % self._rotation == 0:
                self._index = (self._index + 1) % len(self._keys)
            return key


_RINGS = {}
_RINGS_LOCK = threading.Lock()


def next_api_key(up):
    """取本次请求该用的密钥。只有一把时直接返回（零开销）。"""
    keys = effective_keys(up)
    if not keys:
        return ""
    if len(keys) == 1:
        return keys[0]
    name = up.get("name") or up.get("base_url") or MODE
    with _RINGS_LOCK:
        ring = _RINGS.get(name)
        if ring is None:
            ring = _KeyRing(keys, up.get("rotation_count"))
            _RINGS[name] = ring
    return ring.pick(keys, up.get("rotation_count"))


# ---------------- 请求构造 ----------------
def build_headers(up, api_key, client_ua=None):
    """生成一次请求所需的全部请求头。会话类 ID 每次现生成，避免上游按会话限流。"""
    host = urlparse(up.get("base_url") or "").netloc or "copilot.tencent.com"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "Accept-Encoding": "identity",
        "Host": host,
        "Authorization": "Bearer " + api_key,
        "X-API-Key": api_key,
        "X-Requested-With": "XMLHttpRequest",
        # x-stainless-* 是 OpenAI SDK 的运行时指纹，上游的渠道识别会读它
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Windows",
        "x-stainless-package-version": "5.10.1",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v22.13.1",
        # 会话跟踪三件套 + 请求 ID：每次请求都换，模拟 CLI 的独立请求
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": secrets.token_hex(16),
        "X-Conversation-Message-ID": uuid.uuid4().hex,
        "X-Request-ID": uuid.uuid4().hex,
        "X-Agent-Intent": "craft",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": "1.0.7",
        "X-Domain": host,
        "X-Product": "SaaS",
        "X-User-Id": "anonymous",
        "User-Agent": up.get("user_agent") or client_ua or DEFAULT_UA,
    }
    for key, value in (up.get("headers") or {}).items():   # 用户自定义的头可覆盖上面任何一项
        headers[key] = value
    return headers


def prepare_payload(payload):
    """把 OpenAI 请求体改写成上游能接受的样子。"""
    out = dict(payload or {})
    out["stream"] = True                       # 上游只支持流式，非流式在本层聚合后再返回
    messages = list(out.get("messages") or [])
    # 只有一条用户消息时上游会拒绝，补一条最简 system 即可通过
    if len(messages) == 1 and (messages[0] or {}).get("role") == "user":
        messages = [{"role": "system", "content": FALLBACK_SYSTEM}] + messages
    out["messages"] = messages
    return out


# ---------------- 错误与 SSE 解析 ----------------
def translate_error(status, raw):
    """上游的 {"code":11102,"msg":...} 结构转成 OpenAI 的 {"error":{...}}。"""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    message, code = "", None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            code = obj.get("code")
            message = obj.get("msg") or obj.get("message") or (obj.get("error") or {}).get("message", "")
            tips = obj.get("displayTips") or {}
            tip = tips.get("zh") or tips.get("en")
            if isinstance(tip, dict):
                tip = tip.get("zh") or tip.get("en")
            if tip:
                message = "%s（%s）" % (message, tip) if message else str(tip)
    except Exception:
        message = text
    return {
        "error": {
            "message": message or ("CodeBuddy 上游 HTTP %s" % status),
            "type": "upstream_error",
            "code": code,
            "status": status,
        }
    }


def parse_sse_line(line):
    """解析一行 SSE。返回 ("chunk", dict) / ("done", None) / (None, None)。"""
    line = (line or "").strip()
    if not line.startswith("data:"):
        return None, None
    body = line[5:].strip()
    if not body:
        return None, None
    if body == "[DONE]":
        return "done", None
    try:
        obj = json.loads(body)
    except Exception:
        return None, None
    if isinstance(obj, dict):
        return "chunk", obj
    return None, None


def aggregate_stream(resp, fallback_model=""):
    """把 SSE 流聚合成一个 OpenAI 非流式响应体。

    返回 dict；正常返回是 chat.completion 对象，一条 SSE 都没读到时返回 {"error": {...}}。
    """
    result = {
        "id": None,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": fallback_model,
        "choices": [],
        "usage": None,
    }
    content, reasoning = [], []
    role, finish = "assistant", None
    tool_calls, order = {}, []
    chunks, raw = 0, []

    try:
        for line in resp:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            for piece in line.splitlines():
                if len(raw) < 20:
                    raw.append(piece[:400])
                kind, obj = parse_sse_line(piece)
                if kind != "chunk":
                    continue
                chunks += 1
                if not result["id"] and obj.get("id"):
                    result["id"] = obj.get("id")
                if obj.get("model"):
                    result["model"] = obj.get("model")
                if isinstance(obj.get("usage"), dict):
                    result["usage"] = obj.get("usage")
                for choice in (obj.get("choices") or []):
                    if not isinstance(choice, dict):
                        continue
                    if choice.get("finish_reason"):
                        finish = choice.get("finish_reason")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    if delta.get("role"):
                        role = delta.get("role")
                    if isinstance(delta.get("content"), str) and delta.get("content"):
                        content.append(delta["content"])
                    if isinstance(delta.get("reasoning_content"), str) and delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    for call in (delta.get("tool_calls") or []):
                        _merge_tool_call(tool_calls, order, call)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if not chunks:
        return {
            "error": {
                "message": "上游没返回任何 SSE 数据块：%s" % (" / ".join(raw)[:400] or "(空响应)"),
                "type": "upstream_no_stream",
            }
        }

    message = {"role": role, "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        calls = []
        for seq, key in enumerate(order):
            item = tool_calls[key]
            calls.append({
                "id": item["id"] or ("call_%s" % key),
                "type": item.get("type") or "function",
                "index": seq,
                "function": {
                    "name": item["function"]["name"],
                    "arguments": item["function"]["arguments"],
                },
            })
        message["tool_calls"] = calls

    result["choices"] = [{
        "index": 0,
        "message": message,
        "logprobs": None,
        "finish_reason": finish or ("tool_calls" if tool_calls else "stop"),
    }]
    return result


def _merge_tool_call(store, order, call):
    """工具调用是分帧来的（先 id+name，再一段段 arguments），按 id/index 归并。"""
    if not isinstance(call, dict):
        return
    index = call.get("index")
    key = call.get("id") or ("index:%s" % index)
    item = store.get(key)
    if item is None:
        item = {
            "id": call.get("id") or "",
            "type": call.get("type") or "function",
            "function": {"name": "", "arguments": ""},
        }
        store[key] = item
        order.append(key)
    if call.get("id"):
        item["id"] = call["id"]
    if call.get("type"):
        item["type"] = call["type"]
    fn = call.get("function") or {}
    if isinstance(fn.get("name"), str) and fn.get("name"):
        item["function"]["name"] += fn["name"]
    if isinstance(fn.get("arguments"), str) and fn.get("arguments"):
        item["function"]["arguments"] += fn["arguments"]
