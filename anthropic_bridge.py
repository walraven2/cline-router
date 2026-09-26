#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anthropic Messages <-> OpenAI Chat Completions 协议桥接。

用途：Claude Code（CLI / VS Code 插件）只说 Anthropic 协议（POST /v1/messages），
而本机 Cline 路由（默认 127.0.0.1:4000）只接受 OpenAI 协议（/v1/chat/completions）。
本脚本监听一个本地端口（默认 4001），把 Anthropic 请求翻译成 OpenAI 请求打给路由，
再把响应（含 SSE 流式、工具调用）翻译回 Anthropic 格式。

Claude Code 侧配置：
    ANTHROPIC_BASE_URL=http://127.0.0.1:4001/v1
    ANTHROPIC_AUTH_TOKEN=1234
    ANTHROPIC_MODEL=auto

环境变量（可选覆盖）：
    BRIDGE_PORT          监听端口，默认 4001
    BRIDGE_UPSTREAM      上游 chat/completions 地址，默认 http://127.0.0.1:4000/v1/chat/completions
    BRIDGE_UPSTREAM_KEY  上游口令，默认 1234
    BRIDGE_MODEL         上游模型名，默认 auto（Cline 路由的 auto = 当前默认模型）
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("BRIDGE_PORT", "4001"))
UPSTREAM = os.environ.get("BRIDGE_UPSTREAM", "http://127.0.0.1:4000/v1/chat/completions")
UPSTREAM_KEY = os.environ.get("BRIDGE_UPSTREAM_KEY", "1234")
MODEL = os.environ.get("BRIDGE_MODEL", "auto")
UPSTREAM_TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "600"))
# 同时打给上游（router→codebuddy）的并发上限。Claude Code 会一次性并发大量请求，
# 容易触发 CodeBuddy 的并发/速率限流（11128）。限制并发即可少撞窗口；为 1 则完全串行。
MAX_CONCURRENCY = int(os.environ.get("BRIDGE_MAX_CONCURRENCY", "2"))
_UP_SEM = threading.Semaphore(MAX_CONCURRENCY)
# 空正文自愈的 max_tokens 地板。上游模型带隐藏推理，预算太小时推理会把预算吃光、
# 正文一个字不出，上游直接回 500 "empty response content"。低于此值时自动抬到该值
# 重试一次。设为 0 可关闭自愈（退回原来的直接报错）。
EMPTY_MIN_TOKENS = int(os.environ.get("BRIDGE_EMPTY_MIN_TOKENS", "1024"))

FINISH_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 请求转换

def _tool_result_text(content):
    """tool_result 的 content 可能是 str 或 blocks，统一拍平成字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif "text" in b:
                    parts.append(str(b.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _system_text(system):
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _sanitize_tool_pairs(msgs):
    """保证 tool_calls 与 tool 消息严格配对（不配对会被严格上游 400）。

    OpenAI 要求「带 tool_calls 的 assistant 消息」后面紧跟它的**全部** tool 消息。
    Claude Code 的历史里常出现三类不合法形态，火山 ARK 等严格上游会直接 400：
      ① 某个 tool_use 没有对应 tool_result（历史被压缩 / 工具被打断）；
      ② 孤立的 tool 消息（前面没有对应 tool_calls）；
      ③ tool_calls 与 tool 消息数量不匹配。
    这里统一修：删掉没有应答的 tool_call、删掉孤立的 tool 消息。
    """
    out = []
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            following = []
            while j < n and msgs[j].get("role") == "tool":
                following.append(msgs[j])
                j += 1
            answered = set(t.get("tool_call_id") for t in following)
            kept_calls = [c for c in m["tool_calls"] if c.get("id") in answered]
            if kept_calls:
                kept_ids = set(c.get("id") for c in kept_calls)
                nm = dict(m)
                nm["tool_calls"] = kept_calls
                out.append(nm)
                out.extend(t for t in following if t.get("tool_call_id") in kept_ids)
            elif m.get("content"):
                # 没有任何 tool 应答 → 退化成纯文本消息，避免上游 400
                out.append({"role": "assistant", "content": m["content"]})
            i = j
            continue
        if m.get("role") == "tool":
            i += 1   # 孤立 tool 消息 → 丢弃
            continue
        out.append(m)
        i += 1
    return out


def conv_messages(req):
    out = []
    sys_txt = _system_text(req.get("system"))
    if sys_txt:
        out.append({"role": "system", "content": sys_txt})

    for m in req.get("messages") or []:
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = content if isinstance(content, list) else []
        texts, tool_calls, tool_results = [], [], []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                })
            elif t == "tool_result":
                tool_results.append({
                    "tool_call_id": b.get("tool_use_id") or b.get("id") or "",
                    "content": _tool_result_text(b.get("content")),
                })

        text = "".join(texts)
        tool_msgs = [{"role": "tool", "tool_call_id": tr["tool_call_id"],
                      "content": tr["content"] or "(no output)"}
                     for tr in tool_results]
        if role == "assistant" and tool_calls:
            msg = {"role": "assistant", "content": text or ""}
            msg["tool_calls"] = tool_calls
            out.append(msg)
            out.extend(tool_msgs)
        else:
            # tool 消息必须先发：OpenAI 要求「带 tool_calls 的 assistant」后面紧跟它的
            # 全部 tool 消息。Claude Code 有时把 tool_result 和正文塞在同一条 user 消息里，
            # 若先发正文，严格上游（火山 ARK 等）会 400：
            # "An assistant message with 'tool_calls' must be followed by tool messages ..."
            out.extend(tool_msgs)
            if text or not tool_msgs:
                out.append({"role": role, "content": text})
    return _sanitize_tool_pairs(out)


def conv_tools(req):
    tools = req.get("tools")
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def conv_tool_choice(req):
    tc = req.get("tool_choice")
    if not tc:
        return None
    t = tc.get("type") if isinstance(tc, dict) else str(tc)
    if t in ("auto", "none"):
        return t
    if t == "any":
        return "required"
    if t == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return None


def to_openai(req):
    payload = {
        "model": MODEL,
        "messages": conv_messages(req),
        "stream": bool(req.get("stream")),
    }
    mt = req.get("max_tokens")
    payload["max_tokens"] = int(mt) if mt else 8192
    if req.get("temperature") is not None:
        payload["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        payload["top_p"] = req["top_p"]
    if req.get("stop_sequences"):
        payload["stop"] = req["stop_sequences"]
    tools = conv_tools(req)
    if tools:
        payload["tools"] = tools
        tc = conv_tool_choice(req)
        if tc:
            payload["tool_choice"] = tc
    return payload


def _escalate_max_tokens(payload):
    """空正文自愈：把 max_tokens 抬到地板值再打一次。

    已在阈值之上（或地板设为 0）则返回 False，交由调用方如实报错。
    这是有意偏离 Anthropic 语义：宁可多给点正文，也不让请求整个失败。
    """
    cur = int(payload.get("max_tokens") or 0)
    if cur >= EMPTY_MIN_TOKENS:
        return False
    payload["max_tokens"] = EMPTY_MIN_TOKENS
    log("空正文自愈：max_tokens %d -> %d，立即重试" % (cur, EMPTY_MIN_TOKENS))
    return True


def _friendly_upstream_error(code, detail, payload):
    """把上游那句含糊的报错翻成使用者能直接照做的提示。"""
    text = detail.decode("utf-8", "ignore")
    if "empty response content" in text:
        return ("upstream returned empty content (max_tokens=%s): the model spent the whole "
                "budget on hidden reasoning and emitted no text; raise max_tokens to >= %d"
                % (payload.get("max_tokens"), EMPTY_MIN_TOKENS))
    return "upstream %s: %s" % (code, text[:500])


# ---------------------------------------------------------------- 响应转换

def _msg_id():
    return "msg_" + uuid.uuid4().hex[:24]


def _blocks_from_message(msg):
    blocks = []
    text = msg.get("content") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            inp = json.loads(raw)
        except Exception:
            inp = {}
        blocks.append({
            "type": "tool_use",
            "id": call.get("id") or ("toolu_" + uuid.uuid4().hex[:12]),
            "name": fn.get("name", ""),
            "input": inp,
        })
    return blocks


def to_anthropic(openai_resp, model):
    choice = (openai_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = openai_resp.get("usage") or {}
    return {
        "id": openai_resp.get("id") or _msg_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": _blocks_from_message(msg),
        "stop_reason": FINISH_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
        },
    }


# ---------------------------------------------------------------- SSE 流式

def _sse(event, data):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")


class StreamTranslator(object):
    """把 OpenAI 的 chat.completion.chunk 流翻译成 Anthropic 事件流。"""

    def __init__(self, write, model):
        self.write = write
        self.model = model
        self.msg_id = _msg_id()
        self.started_text = False
        self.text_index = 0
        self.tool_index = {}       # openai tool_call index -> anthropic block index
        self.next_index = 1        # 0 号块留给 text
        self.open_index = set()
        self.out_tokens = 0
        self.text_len = 0
        self.tool_calls = 0
        self.finish = None

    def begin(self):
        """只记状态，先不写字节。

        message_start 推迟到第一个真实 token 到达时才发：上游在预算太小时会返回
        一个「200 但零正文」的空流，若提前把 message_start 写出去，就没法中途改主意
        （HTTP 头已发、事件序号已定），只能把空回复原样交给客户端。推迟后空流 =
        一个字节都没写过，可以直接抬 token 重打一次。
        """
        self.started = False

    def _open_text(self):
        self.write(_sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }))
        # 先开一个空的 text 块，保证 index 0 始终是 text
        self.write(_sse("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }))
        self.open_index.add(0)
        self.started = True

    def feed(self, chunk):
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            if not self.started:
                self._open_text()
            self.text_len += len(text)
            self.write(_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            }))
        for call in delta.get("tool_calls") or []:
            idx = call.get("index", 0)
            if idx not in self.tool_index:
                if not self.started:
                    self._open_text()
                self.tool_calls += 1
                bidx = self.next_index
                self.next_index += 1
                self.tool_index[idx] = bidx
                fn = call.get("function") or {}
                self.write(_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": bidx,
                    "content_block": {
                        "type": "tool_use",
                        "id": call.get("id") or ("toolu_" + uuid.uuid4().hex[:12]),
                        "name": fn.get("name", ""),
                        "input": {},
                    },
                }))
                self.open_index.add(bidx)
            bidx = self.tool_index[idx]
            fn = call.get("function") or {}
            partial = fn.get("arguments")
            if partial:
                self.write(_sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": bidx,
                    "delta": {"type": "input_json_delta", "partial_json": partial},
                }))
        usage = chunk.get("usage") or {}
        if usage.get("completion_tokens"):
            self.out_tokens = usage["completion_tokens"]
        if choice.get("finish_reason"):
            self.finish = choice["finish_reason"]
        return self.finish

    def end(self, finish_reason):
        # 全程没来过任何内容（上游空流）→ 补一个空的合法响应，至少不用空回复交差
        if not self.started:
            self._open_text()
        for idx in sorted(self.open_index):
            self.write(_sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
        self.write(_sse("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": FINISH_MAP.get(finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": {"output_tokens": self.out_tokens},
        }))
        self.write(_sse("message_stop", {"type": "message_stop"}))


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_lines(self, resp):
        """把上游 SSE 拆成 json chunk；非 JSON 的心跳行直接跳过。"""
        for line in resp:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                yield json.loads(data)
            except Exception:
                continue

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8"))

    def _upstream_req(self, payload, stream=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % UPSTREAM_KEY,
            "x-api-key": UPSTREAM_KEY,
            "Accept": "text/event-stream" if stream else "application/json",
        }
        return urllib.request.Request(UPSTREAM, data=body, headers=headers, method="POST")

    def _call_upstream(self, payload, stream):
        """打上游，对 CodeBuddy 间歇性 11128/unapproved channel 做退避重试。

        CodeBuddy 网关偶发返回 code=11128（Illegal API invocation from an
        unapproved channel，displayMsg 明确写“请求被拦截，请重新发送”），
        多为并发/风控限流，重试即可恢复。其余错误直接透传不重试。
        """
        max_retries = int(os.environ.get("BRIDGE_RETRIES", "4"))
        last = (0, b"")
        # 并发上限：压住 Claude Code 一次性并发大量请求打爆 CodeBuddy 的限流窗口。
        # 持锁期间包含退避等待，等于自然地把请求节奏降下来。
        with _UP_SEM:
            for attempt in range(max_retries):
                if attempt:
                    # 11128 多为 CodeBuddy 并发/速率限流窗口，退避要够长才能骑过去
                    backoff = min(3 * (2 ** attempt), 30)
                    log("上游重试 %d/%d，退避 %ds" % (attempt + 1, max_retries, backoff))
                    time.sleep(backoff)
                try:
                    return urllib.request.urlopen(
                        self._upstream_req(payload, stream), timeout=UPSTREAM_TIMEOUT), None
                except urllib.error.HTTPError as exc:
                    detail = exc.read()
                    last = (exc.code, detail)
                    transient = b"11128" in detail or b"unapproved channel" in detail
                    log("UPSTREAM HTTP %s (尝试 %d/%d): %s" % (exc.code, attempt + 1, max_retries, detail[:200]))
                    if not transient:
                        # 非限流类错误（参数非法、空正文等）重试必然同样失败，
                        # 别白等退避，直接交给上层处理。
                        return None, last
                except Exception as exc:
                    last = (0, repr(exc).encode("utf-8"))
                    log("UPSTREAM FAIL (尝试 %d/%d) %r" % (attempt + 1, max_retries, exc))
                    if attempt < max_retries - 1:
                        continue
                    return None, last
            return None, last

    # ---- routes

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/models"):
            return self._send_json(200, {
                "object": "list",
                "data": [{"id": MODEL, "object": "model", "owned_by": "cline-router-bridge"}],
            })
        self._send_json(200, {"status": "ok", "bridge": "anthropic->openai", "upstream": UPSTREAM})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            req = self._read_json()
        except Exception as exc:
            return self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                    "message": "bad json: %s" % exc}})

        if path.endswith("/count_tokens"):
            text = json.dumps(req, ensure_ascii=False)
            return self._send_json(200, {"input_tokens": max(1, len(text) // 4)})

        if not path.endswith("/messages"):
            return self._send_json(404, {"type": "error", "error": {"type": "not_found_error",
                                                                    "message": "only /v1/messages"}})

        model = req.get("model") or MODEL
        payload = to_openai(req)
        stream = bool(payload.get("stream"))
        log("REQ model=%s stream=%s msgs=%d tools=%d"
            % (model, stream, len(payload.get("messages") or []), len(payload.get("tools") or [])))

        resp, err = self._call_upstream(payload, stream)
        # 空正文自愈：预算太小导致的 empty response 先抬 token 重打一次，
        # 而不是把「模型没输出」直接甩给用户。
        if err is not None and b"empty response content" in err[1] and _escalate_max_tokens(payload):
            resp, err = self._call_upstream(payload, stream)

        if err is not None:
            code, detail = err
            if code:
                return self._send_json(code, {"type": "error", "error": {
                    "type": "api_error",
                    "message": _friendly_upstream_error(code, detail, payload)}})
            return self._send_json(502, {"type": "error", "error": {"type": "api_error",
                                                                    "message": "upstream unreachable: %s" % detail.decode("utf-8", "ignore")}})

        if stream:
            return self._relay_stream(resp, model, payload)
        try:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8", "ignore"))
        except Exception as exc:
            return self._send_json(502, {"type": "error", "error": {"type": "api_error",
                                                                    "message": "bad upstream json: %s" % exc}})
        return self._send_json(200, to_anthropic(data, model))

    def _relay_stream(self, resp, model, payload):
        # 必须走 chunked：SSE 响应没有 Content-Length，客户端（Claude Code 的
        # Anthropic SDK）是靠「chunked 终止块 0\r\n\r\n」或「连接关闭」来判断
        # 流结束的。之前裸写 body 且声明 Connection: keep-alive，客户端收完
        # message_stop 后仍在等流关闭 → 界面永远停在“回复中”。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        resp_cur = resp
        for attempt in (0, 1):
            tr = StreamTranslator(self._write_chunk, model)
            tr.begin()
            try:
                for chunk in self._sse_lines(resp_cur):
                    tr.feed(chunk)
            except Exception as exc:
                log("STREAM ERR %r" % exc)
            finally:
                try:
                    resp_cur.close()
                except Exception:
                    pass
            # 空流（零正文零工具调用）时，上游既没报错也没内容——多半是预算被隐藏
            # 推理吃光。此时 message_start 还没发出去，可以抬 token 干净重打一次。
            if tr.text_len or tr.tool_calls or not _escalate_max_tokens(payload):
                break
            log("空流重试：max_tokens 已抬至 %d" % payload["max_tokens"])
            resp_cur, err = self._call_upstream(payload, True)
            if err is not None:
                log("空流重试失败：%s" % (err[1][:200],))
                break

        try:
            tr.end(tr.finish)
        except Exception:
            pass
        # 终止块：明确告诉客户端 body 到此为止，随后关闭连接。
        self._end_chunks()
        stop = FINISH_MAP.get(tr.finish, "end_turn")
        empty = " 空正文!" if (not tr.text_len and not tr.tool_calls) else ""
        log("STREAM DONE model=%s stop=%s text=%d tools=%d%s"
            % (model, stop, tr.text_len, tr.tool_calls, empty))

    def _write_chunk(self, data):
        """chunked 编码写一个 SSE 事件块（长度十六进制 + CRLF 包裹）。"""
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise

    def _end_chunks(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log("anthropic bridge listening on http://127.0.0.1:%d  ->  %s" % (PORT, UPSTREAM))
    srv.serve_forever()


if __name__ == "__main__":
    main()
