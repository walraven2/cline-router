#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI Responses API <-> Chat Completions 双向翻译。

用途：新版 OpenAI Codex CLI 只认 Responses API（wire_api="responses"），而 cline-router
只实现 /v1/chat/completions。本模块把 Responses 请求翻成 Chat Completions 打给上游，
再把上游的 Chat Completions 流/响应翻回 Responses 格式（含 function_call 工具调用）。

Codex 实际使用的事件名（从 codex 二进制 strings 提取的最小集）：
  response.created / response.in_progress / response.output_item.added / response.output_item.done
  response.content_part.added / response.content_part.done
  response.output_text.delta / response.output_text.done
  response.function_call_arguments.delta / response.function_call_arguments.done
  response.completed / response.incomplete（max_output_tokens 时）
  response.reasoning_summary_text.delta（推理摘要，chat 协议无对应物 → 跳过）
"""
import json
import time
import uuid


def _rid(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:24])


def log_skip(ttype):
    try:
        import sys
        sys.stderr.write("[responses] 丢弃非 function 工具类型 %r（上游 chat 端点不支持）\n" % ttype)
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- 请求：Responses -> Chat

def _item_text(item):
    """从 message/function_call item 的 content 里抽纯文本（用于 assistant 伴随文本）。"""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("input_text", "output_text") and b.get("text"):
                parts.append(b["text"])
        return "\n".join(parts)
    return ""


def _message_content(content):
    """message item 的 content：str 原样；parts 数组拍平成 str / image_url part。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt in ("input_text", "output_text") and b.get("text"):
            texts.append(b["text"])
        elif bt == "input_image":
            url = (b.get("image_url") or {}).get("url") if isinstance(b.get("image_url"), dict) else b.get("image_url")
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        # 其余（input_file 等）忽略
    text = "\n".join(texts)
    if parts and not text:
        return parts  # 纯多图
    if parts and text:
        return [{"type": "text", "text": text}] + parts
    return text


def _output_text(out):
    """function_call_output 的 output：str 原样；parts 数组抽 text；其余 JSON 化。"""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for b in out:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("text"):
                    parts.append(str(b["text"]))
        if parts:
            return "\n".join(parts)
        return ""
    if out is None:
        return ""
    return json.dumps(out, ensure_ascii=False)


def _sanitize_tool_pairs(msgs):
    """保证 tool_calls 与 tool 消息严格配对（不配对会被严格上游 400）。

    OpenAI 要求「带 tool_calls 的 assistant 消息」后面紧跟它的**全部** tool 消息。
    codex 的历史里可能出现：某个 function_call 没有对应 function_call_output
    （工具被打断 / 被裁剪）、孤立 tool 消息、数量不匹配。这里统一修：
    删掉没有应答的 tool_call、删掉孤立的 tool 消息。
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
                out.append({"role": "assistant", "content": m["content"]})
            i = j
            continue
        if m.get("role") == "tool":
            i += 1   # 孤立 tool 消息 → 丢弃
            continue
        out.append(m)
        i += 1
    return out


def _input_items_to_messages(instructions, inpt):
    """把 Responses 的 input（string 或 item 数组）翻成 chat messages。

    只翻三类 item：message / function_call / function_call_output。其余类型
    （reasoning、item_reference、local_shell_call、web_search_call、
    custom_tool_call、mcp_call ...）**一律跳过**——chat 协议无对应物。

    关键：绝不能把未知类型捏造成（空的）user 消息。codex 的工具历史里
    function_call 与 function_call_output 之间常夹着 reasoning / web_search_call，
    一旦被翻成消息插进去，就破坏了「assistant.tool_calls 必须紧跟其 tool 消息」
    的邻接要求，严格上游（火山 ARK 等）会直接 400：
      An assistant message with 'tool_calls' must be followed by tool messages ...
    """
    messages = []
    if instructions and isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    if inpt is None:
        return messages
    if isinstance(inpt, str):
        messages.append({"role": "user", "content": inpt})
        return messages
    if not isinstance(inpt, list):
        messages.append({"role": "user", "content": str(inpt)})
        return messages

    for item in inpt:
        if not isinstance(item, dict):
            continue
        t = item.get("type") or ("message" if item.get("role") else None)
        if t == "function_call":
            name = item.get("name", "")
            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)
            call = {
                "id": item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            # codex 的并行工具调用是**多个独立 function_call item**，但它们属于同一次
            # assistant 回合。必须合并进同一条 assistant 消息，否则会变成多条相邻的
            # assistant(tool_calls)，各自都配不齐 tool 应答 → 被净化丢弃（丢上下文）。
            prev = messages[-1] if messages else None
            if prev and prev.get("role") == "assistant" and prev.get("tool_calls"):
                prev["tool_calls"].append(call)
                if not prev.get("content"):
                    prev["content"] = _item_text(item) or ""
            else:
                messages.append({"role": "assistant", "content": _item_text(item) or "",
                                 "tool_calls": [call]})
        elif t == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": _output_text(item.get("output")) or "(no output)",
            })
        elif t == "message" or item.get("role"):
            role = item.get("role") or "user"
            content = _message_content(item.get("content"))
            if content:
                messages.append({"role": role, "content": content})
        # else: 未知 item 类型 → 跳过（见上方说明，绝不能捏造成消息）
    return _sanitize_tool_pairs(messages)


def _chat_tools(tools):
    """展开 Responses tools；非 function 类型（web_search / local_shell 等 codex 内置工具）
    上游 chat 端点大多不支持 → 丢弃并告警，避免上游 400。"""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            log_skip(t.get("type"))
            continue
        fn = t.get("function") or {}
        params = fn.get("parameters") or t.get("parameters") or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": (fn.get("name") or t.get("name") or ""),
                "description": fn.get("description") or t.get("description") or "",
                "parameters": params if isinstance(params, dict) else {"type": "object", "properties": {}},
            },
        })
    return out or None


def _chat_tool_choice(tc):
    if not tc:
        return None
    if isinstance(tc, str):
        return tc if tc in ("auto", "none", "required") else None
    t = tc.get("type")
    if t == "function":
        name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
        return {"type": "function", "function": {"name": name}}
    return None


def to_chat_request(req, model):
    """Responses 请求 → Chat Completions 请求（model 已是上游真实名）。"""
    payload = {
        "model": model,
        "messages": _input_items_to_messages(req.get("instructions"), req.get("input")),
        "stream": True,  # 上游一律走流式，非流式在 router 端聚合（codebuddy 也只支持流式）
        "stream_options": {"include_usage": True},
    }
    tools = _chat_tools(req.get("tools"))
    if tools:
        payload["tools"] = tools
        tc = _chat_tool_choice(req.get("tool_choice"))
        if tc:
            payload["tool_choice"] = tc
    if req.get("parallel_tool_calls") is not None:
        payload["parallel_tool_calls"] = bool(req.get("parallel_tool_calls"))
    if req.get("temperature") is not None:
        payload["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        payload["top_p"] = req["top_p"]
    mt = req.get("max_output_tokens")
    if mt:
        payload["max_tokens"] = int(mt)
    effort = (req.get("reasoning") or {}).get("effort") if isinstance(req.get("reasoning"), dict) else None
    if effort:
        payload["reasoning_effort"] = effort
    return payload


# ---------------------------------------------------------------- SSE 行读取

def iter_sse_lines(resp):
    """把上游 SSE 流拆成 json chunk；跳过心跳与 [DONE]。"""
    for line in resp:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "ignore")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except Exception:
            continue


# ---------------------------------------------------------------- 响应：Chat -> Responses（非流式）

def aggregate_chat_stream(chunks):
    """把 Chat Completions 的 SSE chunk 列表聚合为单个 chat.completion dict。"""
    content = ""
    tool_calls = {}  # index -> {id, name, arguments}
    finish = None
    usage = {}
    for ch in chunks:
        choice = (ch.get("choices") or [{}])[0]
        d = choice.get("delta") or {}
        if d.get("content"):
            content += d["content"]
        for tc in d.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            i = tc.get("index", 0)
            slot = tool_calls.setdefault(i, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        if ch.get("usage"):
            usage = ch["usage"]
    msg = {"role": "assistant", "content": content}
    tcs = [{"id": t["id"] or ("call_" + uuid.uuid4().hex[:12]),
            "type": "function",
            "function": {"name": t["name"], "arguments": t["arguments"]}}
           for t in tool_calls.values() if t["name"]]
    if tcs:
        msg["tool_calls"] = tcs
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "choices": [{"message": msg, "finish_reason": finish}]}, usage


def to_response_object(chat_resp, model):
    """非流式 chat.completion → Responses object。"""
    choice = (chat_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = chat_resp.get("usage") or {}
    finish = choice.get("finish_reason")
    output = []
    text = msg.get("content") or ""
    if text:
        output.append({
            "type": "message", "id": _rid("msg"), "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call", "id": _rid("fc"),
            "call_id": tc.get("id") or ("call_" + uuid.uuid4().hex[:12]),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments") or "{}",
            "status": "completed",
        })
    if not output:
        output.append({
            "type": "message", "id": _rid("msg"), "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": "", "annotations": []}],
        })
    status = "incomplete" if finish == "length" else "completed"
    r = {
        "id": _rid("resp"), "object": "response",
        "created_at": int(time.time()), "model": model,
        "status": status, "output": output,
        "tool_choice": "auto", "parallel_tool_calls": True,
    }
    if finish == "length":
        r["incomplete_details"] = {"reason": "max_output_tokens"}
    r["usage"] = {
        "input_tokens": usage.get("prompt_tokens", 0) or 0,
        "output_tokens": usage.get("completion_tokens", 0) or 0,
        "total_tokens": usage.get("total_tokens", 0) or 0,
    }
    return r


# ---------------------------------------------------------------- SSE：Chat -> Responses（流式）

def _sse(event, data):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")


class ResponsesStreamTranslator(object):
    """把 Chat Completions 的 SSE chunk 流翻译成 Responses 事件流。"""

    def __init__(self, write, model):
        self.write = write
        self.model = model
        self.resp_id = _rid("resp")
        self.items = []          # 完整 item 结构（end 时拼进 completed.response.output）
        self.item_index = 0
        self.text_item = None    # {id, full, output_index}
        self.tool_items = {}     # chat index -> {id, call_id, name, args, output_index}
        self.started = False
        self.out_tokens = 0
        self.text_len = 0
        self.tool_calls = 0
        self.finish = None

    def begin(self):
        base = {"id": self.resp_id, "object": "response", "created_at": int(time.time()),
                "model": self.model, "output": [], "tool_choice": "auto",
                "parallel_tool_calls": True, "status": "in_progress"}
        self.write(_sse("response.created", {"type": "response.created", "response": base}))
        self.write(_sse("response.in_progress", {"type": "response.in_progress", "response": dict(base)}))
        self.started = True

    def _open_text(self):
        iid = _rid("msg")
        self.text_item = {"id": iid, "full": "", "output_index": self.item_index}
        self.write(_sse("response.output_item.added", {
            "type": "response.output_item.added", "output_index": self.item_index,
            "item": {"type": "message", "id": iid, "status": "in_progress",
                     "role": "assistant", "content": []},
        }))
        self.write(_sse("response.content_part.added", {
            "type": "response.content_part.added", "output_index": self.item_index,
            "item_id": iid, "content_index": 0, "part": {"type": "output_text", "text": ""},
        }))
        self.item_index += 1

    def _open_tool(self, idx, call_id, name):
        iid = _rid("fc")
        self.tool_items[idx] = {"id": iid, "call_id": call_id, "name": name,
                                "args": "", "output_index": self.item_index}
        self.write(_sse("response.output_item.added", {
            "type": "response.output_item.added", "output_index": self.item_index,
            "item": {"type": "function_call", "id": iid, "call_id": call_id,
                     "name": name, "arguments": "", "status": "in_progress"},
        }))
        self.item_index += 1
        return iid

    def feed(self, chunk):
        if not self.started:
            self.begin()
        choice = (chunk.get("choices") or [{}])[0]
        d = choice.get("delta") or {}
        text = d.get("content")
        if text:
            if self.text_item is None:
                self._open_text()
            self.text_item["full"] += text
            self.text_len += len(text)
            self.write(_sse("response.output_text.delta", {
                "type": "response.output_text.delta",
                "output_index": self.text_item["output_index"], "item_id": self.text_item["id"],
                "content_index": 0, "delta": text,
            }))
        for tc in d.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            call_id = tc.get("id") or ""
            name = fn.get("name") or ""
            slot = self.tool_items.get(idx)
            if slot is None:
                iid = self._open_tool(idx, call_id, name)
            else:
                if call_id and not slot["call_id"]:
                    slot["call_id"] = call_id
                if name and not slot["name"]:
                    slot["name"] = name
                iid = slot["id"]
            partial = fn.get("arguments")
            if partial:
                self.tool_items[idx]["args"] += partial
                self.write(_sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "output_index": self.tool_items[idx]["output_index"], "item_id": iid,
                    "call_id": self.tool_items[idx]["call_id"],
                    "name": self.tool_items[idx]["name"],
                    "delta": partial,
                }))
        usage = chunk.get("usage") or {}
        if usage.get("completion_tokens"):
            self.out_tokens = usage["completion_tokens"]
        if choice.get("finish_reason"):
            self.finish = choice["finish_reason"]
        return self.finish

    def end(self, finish_reason):
        if not self.started:
            self.begin()
        # 收尾文本 item
        if self.text_item is not None:
            iid = self.text_item["id"]
            oi = self.text_item["output_index"]
            full = self.text_item["full"]
            self.write(_sse("response.output_text.done", {
                "type": "response.output_text.done", "output_index": oi, "item_id": iid,
                "content_index": 0, "text": full,
            }))
            self.write(_sse("response.content_part.done", {
                "type": "response.content_part.done", "output_index": oi, "item_id": iid,
                "content_index": 0, "part": {"type": "output_text", "text": full},
            }))
            self.write(_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": oi,
                "item": {"type": "message", "id": iid, "status": "completed",
                         "role": "assistant",
                         "content": [{"type": "output_text", "text": full, "annotations": []}]},
            }))
            self.items.append({"type": "message", "id": iid, "status": "completed",
                               "role": "assistant",
                               "content": [{"type": "output_text", "text": full, "annotations": []}]})
        # 收尾工具 items
        for idx, slot in sorted(self.tool_items.items()):
            oi = slot["output_index"]
            self.write(_sse("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "output_index": oi,
                "item_id": slot["id"], "call_id": slot["call_id"], "name": slot["name"],
                "arguments": slot["args"],
            }))
            self.write(_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": oi,
                "item": {"type": "function_call", "id": slot["id"], "call_id": slot["call_id"],
                         "name": slot["name"], "arguments": slot["args"], "status": "completed"},
            }))
            self.items.append({"type": "function_call", "id": slot["id"], "call_id": slot["call_id"],
                               "name": slot["name"], "arguments": slot["args"], "status": "completed"})
        # 全程零内容兜底
        if not self.items:
            iid = _rid("msg")
            self.write(_sse("response.output_item.added", {
                "type": "response.output_item.added", "output_index": 0,
                "item": {"type": "message", "id": iid, "status": "in_progress",
                         "role": "assistant", "content": []},
            }))
            self.write(_sse("response.content_part.added", {
                "type": "response.content_part.added", "output_index": 0, "item_id": iid,
                "content_index": 0, "part": {"type": "output_text", "text": ""},
            }))
            self.write(_sse("response.content_part.done", {
                "type": "response.content_part.done", "output_index": 0, "item_id": iid,
                "content_index": 0, "part": {"type": "output_text", "text": ""},
            }))
            self.write(_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0,
                "item": {"type": "message", "id": iid, "status": "completed",
                         "role": "assistant", "content": [{"type": "output_text", "text": "", "annotations": []}]},
            }))
            self.items.append({"type": "message", "id": iid, "status": "completed",
                               "role": "assistant", "content": [{"type": "output_text", "text": "", "annotations": []}]})

        status = "incomplete" if (finish_reason or self.finish) == "length" else "completed"
        resp = {"id": self.resp_id, "object": "response", "created_at": int(time.time()),
                "model": self.model, "status": status, "output": self.items,
                "tool_choice": "auto", "parallel_tool_calls": True,
                "usage": {"input_tokens": 0, "output_tokens": self.out_tokens,
                          "total_tokens": self.out_tokens}}
        if status == "incomplete":
            resp["incomplete_details"] = {"reason": "max_output_tokens"}
        self.write(_sse("response.completed", {"type": "response.completed", "response": resp}))
