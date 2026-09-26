#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cline-router 自检脚本：不需要任何真实 API Key。

流程：起一个假上游（Mock）→ 起 router 指向它 → 断言这些事：
  1) GET /v1/models 能列出配置里的所有模型别名
  2) 非流式请求按别名正确路由、且 model 字段被改写成上游真实模型名、上游收到 Bearer
  3) 流式(SSE)请求逐块透传
  4) model=auto 路由到 default_model；only_auto 热加载后 /v1/models 只剩 auto；
     POST /api/default 切换默认模型后 auto 立即指向新目标
  5) 只读监控端点 /api/fuel、/api/workbuddy 稳定返回 200 + JSON（无凭据时 ok=false 也算通过，
     但不允许 500/挂起/结构缺失）
全部通过打印 PASS 并以 0 退出；任一失败打印 FAIL 并以 1 退出。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK_PORT = 49321
ROUTER_PORT = 49322
ROUTER_KEY = "selftest-key"


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        seen_model = payload.get("model")
        seen_auth = self.headers.get("Authorization")
        seen_ua = self.headers.get("User-Agent")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if payload.get("tools"):
                # 工具调用响应：先开 function_call，再补 arguments，最后 finish
                frames = [
                    {"choices": [{"delta": {"tool_calls": [
                        {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": ""}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": '{"city":"Beijing"}'}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ]
            else:
                frames = [{"choices": [{"delta": {"content": piece}}]} for piece in ["mo", "ck-", str(seen_model)]]
            for fr in frames:
                data = ("data: " + json.dumps(fr) + "\n\n").encode()
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            body = json.dumps({
                "from_mock": True,
                "model_seen_by_upstream": seen_model,
                "auth_seen": seen_auth,
                "ua_seen_by_upstream": seen_ua,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def wait_ready(url, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def post(url, payload, key=None, ua="X-Test-Client/9.9", extra_headers=None):
    headers = {"Content-Type": "application/json", "User-Agent": ua}
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    return urllib.request.urlopen(req, timeout=20)


def main():
    failures = []

    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    config = {
        "host": "127.0.0.1",
        "port": ROUTER_PORT,
        "auth_key": ROUTER_KEY,
        "default_model": "beta",
        "upstreams": {
            "mock": {"base_url": "http://127.0.0.1:%d/v1" % MOCK_PORT, "api_key": "upstream-secret", "timeout": 30}
        },
        "models": [
            {"id": "alpha", "upstream": "mock", "model": "real-alpha"},
            {"id": "beta", "upstream": "mock", "model": "real-beta"},
        ],
    }
    data_dir = tempfile.mkdtemp(prefix="cline-router-selftest-")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(config, f)
        cfg_path = f.name

    proc = subprocess.Popen(
        # --data-dir 指向临时目录：隔离燃料/积分缓存，绝不污染真实数据目录
        [sys.executable, os.path.join(HERE, "router.py"),
         "--config", cfg_path, "--data-dir", data_dir],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % ROUTER_PORT
    try:
        if not wait_ready(base + "/health"):
            print("FAIL: router 未能在 10s 内就绪")
            return 1

        # 1) 模型列表
        with urllib.request.urlopen(base + "/v1/models", timeout=5) as resp:
            ids = sorted(m["id"] for m in json.loads(resp.read())["data"])
        if ids != ["alpha", "beta"]:
            failures.append("模型列表不符：%r" % ids)

        # 2) 非流式路由 + 改写 model + 注入上游 Key
        with post(base + "/v1/chat/completions", {"model": "alpha", "messages": [{"role": "user", "content": "hi"}]}, ROUTER_KEY) as resp:
            got = json.loads(resp.read())
        if got.get("model_seen_by_upstream") != "real-alpha":
            failures.append("model 未被改写成上游名：%r" % got.get("model_seen_by_upstream"))
        if got.get("auth_seen") != "Bearer upstream-secret":
            failures.append("上游 Bearer 未注入：%r" % got.get("auth_seen"))
        if got.get("ua_seen_by_upstream") != "X-Test-Client/9.9":
            failures.append("客户端 UA 未透传：%r" % got.get("ua_seen_by_upstream"))

        # 3) 流式透传：按 SSE 事件解析，把 delta.content 拼起来
        with post(base + "/v1/chat/completions", {"model": "beta", "stream": True, "messages": [{"role": "user", "content": "hi"}]}, ROUTER_KEY) as resp:
            streamed = resp.read().decode("utf-8", "replace")
        content = ""
        events = 0
        for line in streamed.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                content += json.loads(chunk)["choices"][0]["delta"]["content"]
                events += 1
            except Exception:
                pass
        if content != "mock-real-beta":
            failures.append("流式内容不正确：解析后 %r（原文 %r）" % (content, streamed[:200]))
        if events != 3:
            failures.append("流式事件数应为 3，实际 %d（说明没有逐块透传）" % events)

        # 4) 未授权应被拒
        try:
            post(base + "/v1/chat/completions", {"model": "alpha", "messages": []}, "wrong-key")
            failures.append("错误 Key 没有被拒绝")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                failures.append("错误 Key 返回了 %s，期望 401" % exc.code)

        # 5) model=auto 应路由到 default_model（beta）
        with post(base + "/v1/chat/completions", {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, ROUTER_KEY) as resp:
            got = json.loads(resp.read())
        if got.get("model_seen_by_upstream") != "real-beta":
            failures.append("auto 未路由到默认模型 beta：%r" % got.get("model_seen_by_upstream"))

        # 6) 热加载 only_auto=true 后 /v1/models 只暴露 auto
        full_cfg = dict(config)
        full_cfg["only_auto"] = True
        with post(base + "/api/config", full_cfg, extra_headers={"X-Router-UI": "1"}) as resp:
            saved = json.loads(resp.read())
        if not saved.get("ok"):
            failures.append("热加载配置失败：%r" % saved)
        with urllib.request.urlopen(base + "/v1/models", timeout=5) as resp:
            ids = [m["id"] for m in json.loads(resp.read())["data"]]
        if ids != ["auto"]:
            failures.append("only_auto=true 后模型列表应为 ['auto']，实际 %r" % ids)

        # 7) POST /api/default 切换默认模型后，auto 立即指向新目标（alpha）
        with post(base + "/api/default", {"id": "alpha"}, extra_headers={"X-Router-UI": "1"}) as resp:
            switched = json.loads(resp.read())
        if not switched.get("ok") or switched.get("default_model") != "alpha":
            failures.append("切换默认模型失败：%r" % switched)
        with post(base + "/v1/chat/completions", {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, ROUTER_KEY) as resp:
            got = json.loads(resp.read())
        if got.get("model_seen_by_upstream") != "real-alpha":
            failures.append("切默认后 auto 未路由到 alpha：%r" % got.get("model_seen_by_upstream"))

        # 8) 只读监控端点：无凭据也必须稳定 200 + JSON（ok 允许 false，但不许 500/挂起）
        for ep in ("/api/fuel", "/api/workbuddy"):
            try:
                with urllib.request.urlopen(base + ep, timeout=5) as resp:
                    payload = json.loads(resp.read())
                if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
                    failures.append("%s 响应结构不符（缺 bool 型 ok）：%r" % (ep, payload))
            except urllib.error.HTTPError as exc:
                failures.append("%s HTTP %s（期望 200，无凭据时应返回 ok=false）" % (ep, exc.code))
            except Exception as exc:
                failures.append("%s 请求失败：%r" % (ep, exc))

        # 9) 非流式 Responses API：翻译 + 聚合回 response object
        with post(base + "/v1/responses", {"model": "alpha", "input": "hi", "stream": False}, ROUTER_KEY) as resp:
            got = json.loads(resp.read())
        if got.get("object") != "response":
            failures.append("非流式 responses object 不符：%r" % got.get("object"))
        if got.get("status") != "completed":
            failures.append("非流式 responses status 应为 completed：%r" % got.get("status"))
        out0 = (got.get("output") or [{}])[0]
        if out0.get("type") != "message":
            failures.append("非流式 responses 首个 output 应为 message：%r" % out0.get("type"))
        ctext = "".join(b.get("text", "") for b in out0.get("content") or [] if isinstance(b, dict))
        if "real-alpha" not in ctext:
            failures.append("非流式 responses 正文未含上游模型名：%r" % ctext)

        # 10) 流式 Responses API：事件序列 + completed 正文
        with post(base + "/v1/responses", {"model": "beta", "input": "hi", "stream": True}, ROUTER_KEY) as resp:
            ev_stream = resp.read().decode("utf-8", "replace")
        events = []
        for line in ev_stream.splitlines():
            line = line.strip()
            if line.startswith("event:"):
                events.append(line[len("event:"):].strip())
        if not events or events[0] != "response.created":
            failures.append("流式 responses 首个事件应为 response.created，实际 %r" % events[:1])
        if "response.output_text.delta" not in events:
            failures.append("流式 responses 缺少 response.output_text.delta 事件")
        if events[-1] != "response.completed":
            failures.append("流式 responses 末事件应为 response.completed，实际 %r" % events[-1])
        comp = None
        for blk in ev_stream.split("event: "):
            if blk.startswith("response.completed"):
                dseg = blk.split("data: ", 1)[1].strip() if "data: " in blk else ""
                try:
                    comp = json.loads(dseg)
                except Exception:
                    comp = None
        if comp is None:
            failures.append("流式 responses 无法解析 completed 事件 data")
        else:
            # completed 事件结构：{"type":"response.completed","response":{...,"output":[...]}}
            _resp = comp.get("response") if isinstance(comp.get("response"), dict) else comp
            _out = (_resp.get("output") if isinstance(_resp, dict) else None) or [{}]
            ctext = "".join(b.get("text", "") for b in _out[0].get("content", []) if isinstance(b, dict))
            if "real-beta" not in ctext:
                failures.append("流式 responses completed 正文不含上游模型名：%r" % ctext)

        # 11) 工具调用 Responses API：function_call 翻译
        with post(base + "/v1/responses", {"model": "alpha", "input": "hi",
                   "tools": [{"type": "function", "name": "get_weather",
                              "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}],
                   "stream": True}, ROUTER_KEY) as resp:
            tool_stream = resp.read().decode("utf-8", "replace")
        if "response.output_item.added" not in tool_stream:
            failures.append("工具 responses 缺少 response.output_item.added")
        else:
            has_fc = False
            for blk in tool_stream.split("event: response.output_item.added"):
                if "function_call" in blk and "get_weather" in blk:
                    has_fc = True
                    break
            if not has_fc:
                failures.append("工具 responses 的 output_item.added 未含 function_call/get_weather")
        if "response.function_call_arguments.done" not in tool_stream:
            failures.append("工具 responses 缺少 response.function_call_arguments.done")

        # 12) Responses 请求翻译：codex 工具历史（夹 reasoning / web_search_call 等
        #     未知 item）不得破坏「assistant.tool_calls 紧跟其 tool 消息」的邻接要求。
        #     回归背景：2026-09-26 未知类型被捏造成空 user 消息插在 function_call 与
        #     function_call_output 之间 → 火山 ARK 400
        #     "An assistant message with 'tool_calls' must be followed by tool messages"。
        import responses_api as _ra
        _msgs = _ra._input_items_to_messages("sys", [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "运行 pwd"}]},
            {"type": "reasoning", "id": "rs_1"},
            {"type": "function_call", "call_id": "call_a", "name": "shell", "arguments": "{}"},
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            {"type": "local_shell_call", "id": "ls_1", "status": "completed"},
            {"type": "function_call", "call_id": "call_b", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_a", "output": "/tmp"},
            {"type": "function_call_output", "call_id": "call_b", "output": "/tmp2"},
        ])
        _i, _n, _perr = 0, len(_msgs), None
        _calls = sum(len(m.get("tool_calls") or []) for m in _msgs)
        _tools = sum(1 for m in _msgs if m.get("role") == "tool")
        while _i < _n:
            _m = _msgs[_i]
            if _m.get("role") == "assistant" and _m.get("tool_calls"):
                _ids = sorted(c["id"] for c in _m["tool_calls"])
                _j, _got = _i + 1, []
                while _j < _n and _msgs[_j].get("role") == "tool":
                    _got.append(_msgs[_j]["tool_call_id"])
                    _j += 1
                if sorted(_got) != _ids:
                    _perr = "tool_calls %r 后只有 %r" % (_ids, sorted(_got))
                    break
                _i = _j
            else:
                if _m.get("role") == "tool":
                    _perr = "孤立 tool 消息 %r" % _m.get("tool_call_id")
                    break
                _i += 1
        if _perr:
            failures.append("Responses 工具历史配对不合法：%s" % _perr)
        if (_calls, _tools) != (2, 2):
            failures.append("Responses 工具历史上下文丢失：tool_calls=%d tool=%d（期望 2/2）"
                            % (_calls, _tools))
    finally:
        proc.terminate()
        mock.shutdown()
        try:
            os.unlink(cfg_path)
        except OSError:
            pass
        shutil.rmtree(data_dir, ignore_errors=True)

    if failures:
        print("FAIL")
        for item in failures:
            print("  - " + item)
        return 1
    print("PASS  cline-router 自检通过：模型列表 / 路由改写 / 上游密钥注入 / 流式透传 / 鉴权拦截 / auto 默认模型 / only_auto / 切换默认 / 监控端点 / Responses 翻译(非流式·流式·工具·工具历史配对)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
