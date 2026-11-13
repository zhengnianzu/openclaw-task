"""从 llm-proxy app.py 直接引入的模型 API 处理函数。

来源：https://github.com/zhengnianzu/llm-proxy/blob/0b437714ab34693be2258cf3bbf4195624d2920e/app.py
四个路由保留上游的 HTTP 调用、普通响应分支和 SSE 循环。
网关适配集中在目标地址、请求头、一次尝试和请求级轨迹状态。
"""

import json
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .http_client import get_client, shared_client

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
BAN_EXPLORE = False
TRUST_ENV = False
MAX_RETRIES = 1


def _append_query(url: str, req: "Request") -> str:
    """把客户端原始请求的 query string 原样拼到上游 URL 上。

    实现「原样全部透传」：客户端发 /v1/messages?beta=true，转发到上游时也带上
    ?beta=true。上游 URL 一般不含 query，用 ? 拼接；若已带 query 则用 &。
    """
    q = req.url.query
    if not q:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{q}"


# 逐跳（hop-by-hop）头：只在单跳连接内有意义，转发时必须剔除，否则会破坏转发。
_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    # host / content-length 让 httpx 按目标重新计算；client 鉴权头由上游 key 覆盖
    "host", "content-length",
}


def _filter_forward_headers(headers, upstream_key: str) -> dict:
    """透传客户端请求头，剔除逐跳头，并把鉴权头换成上游 key。"""
    connection_headers = {name.strip().lower() for name in headers.get("connection", "").split(",")}
    out = {}
    for k, v in headers.items():
        if k.lower() in _HOP_BY_HOP_HEADERS or k.lower() in connection_headers:
            continue
        if upstream_key and k.lower() in ("authorization", "x-api-key"):
            continue  # 下面统一用上游 key 覆盖
        out[k] = v
    if upstream_key:
        out["Authorization"] = f"Bearer {upstream_key}"
        out["x-api-key"] = upstream_key
    return out


def _filter_response_headers(headers) -> dict:
    """透传上游响应头，剔除逐跳头。

    我们用 aiter_raw() 原样转发未解压的响应体，所以要**保留** content-encoding
    （如 gzip），让客户端自己解压。但 content-length / transfer-encoding 要剔除：
    流式回传由 Starlette 用 chunked 编码，长度不再固定。
    """
    connection_headers = {name.strip().lower() for name in headers.get("connection", "").split(",")}
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS | connection_headers}


def _resolve_model_name(raw_model: Any) -> str:
    """
    统一处理 model 名：
    - "byenv" 或空值 -> 从环境变量 MODEL_ID 读取
    - 其他值 -> 原样返回
    """
    if raw_model == "byenv" or not raw_model:
        return os.environ.get("MODEL_ID") or "unknown"
    return raw_model


def _strip_task_explore_line(
        tools: Optional[List[Dict[str, Any]]],
        ban_explore: Optional[bool] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    根据 ban_explore 决定是否从 Task 工具描述中移除 "- Explore:" 行。
    - ban_explore 为 None 时，采用全局 BAN_EXPLORE 开关。
    """

    def _remove_explore_from_desc(desc: Any) -> Optional[str]:
        """从工具说明中移除 Explore 行，未发生修改时返回空值。"""
        if not isinstance(desc, str):
            return None
        lines = desc.splitlines()
        filtered_lines = []
        changed = False
        for line in lines:
            if line.lstrip().startswith("- Explore:") or line.lstrip().startswith("- **Explore**:"):
                changed = True
                continue
            filtered_lines.append(line)
        return "\n".join(filtered_lines) if changed else None

    if ban_explore is None:
        ban_explore = BAN_EXPLORE

    if not ban_explore or not tools:
        return tools

    cleaned: List[Any] = []
    for t in tools:
        if not isinstance(t, dict):
            cleaned.append(t)
            continue
        # Anthropic tools: {name, description, input_schema}
        if t.get("name") == "Task":
            new_desc = _remove_explore_from_desc(t.get("description"))
            if new_desc is not None:
                cleaned.append({**t, "description": new_desc})
            else:
                cleaned.append(t)
            continue

        # OpenAI tools: {type:"function", function:{name, description, parameters}}
        if t.get("type") == "function":
            func = t.get("function")
            if isinstance(func, dict) and func.get("name") == "Task":
                new_desc = _remove_explore_from_desc(func.get("description"))
                if new_desc is not None:
                    cleaned.append({**t, "function": {**func, "description": new_desc}})
                else:
                    cleaned.append(t)
                continue

        cleaned.append(t)

    return cleaned


def _has_valid_content(resp_json: dict) -> bool:
    """非流式：content 数组中至少有一个有效内容块（text/thinking/tool_use 非空）。"""
    for block in resp_json.get("content", []):
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            return True
        if btype == "thinking" and block.get("thinking"):
            return True
        if btype == "tool_use" and block.get("input") is not None:
            return True
    return False


def _resp_to_obj(r):  # httpx.Response -> dict
    """记录上游状态码和响应头，响应体优先解析为 JSON。"""
    base = {"status_code": r.status_code, "headers": dict(r.headers)}
    try:
        base["json"] = r.json()
    except Exception:
        base["text"] = r.text
    return base


def _sanitize_messages(messages: Any) -> Any:
    """
    清洗 messages 列表，过滤掉空 text content block。
    Anthropic API 要求 text content blocks 必须非空，否则返回 400。
    - content 为 list 时：过滤 type==text 且 text 为空/空白的块；
      若过滤后 list 为空，用单个空格占位块替代，避免整条消息丢失。
    - content 为字符串时：原样保留（空字符串同理由上游决定，非我们的问题范围）。
    """
    if not isinstance(messages, list):
        return messages
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        # thinking/redacted_thinking blocks in assistant messages must remain exactly
        # as they were in the original response; skip sanitization for such messages.
        if msg.get("role") == "assistant" and any(
            isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
            for b in content
        ):
            cleaned.append(msg)
            continue
        def _is_empty_text_block(b: Any) -> bool:
            """判断内容块是否为仅含空白或缺少文本的文本块。"""
            return isinstance(b, dict) and b.get("type") == "text" and not (b.get("text") or "").strip()

        def _sanitize_block(b: Any) -> Any:
            """对 tool_result 块递归清理其嵌套 content 中的空 text 块。"""
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                return b
            nested = b.get("content")
            if not isinstance(nested, list):
                return b
            filtered_nested = [nb for nb in nested if not _is_empty_text_block(nb)]
            if len(filtered_nested) == len(nested):
                return b
            return {**b, "content": filtered_nested if filtered_nested else [{"type": "text", "text": " "}]}

        new_blocks = [_sanitize_block(b) for b in content if not _is_empty_text_block(b)]
        if len(new_blocks) == len(content) and all(new_blocks[i] is content[i] for i in range(len(content))):
            cleaned.append(msg)
        elif new_blocks:
            cleaned.append({**msg, "content": new_blocks})
        else:
            # 所有块都被过滤掉了，用占位符保留消息结构
            cleaned.append({**msg, "content": [{"type": "text", "text": " "}]})
    return cleaned


def _target_url(req: Request) -> str:
    """将配置中的上游地址与原始请求路径和查询参数直接拼接。"""
    raw_path = req.scope.get("raw_path")
    path = raw_path.decode("ascii") if raw_path is not None else req.url.path
    return _append_query(req.state.proxy_target + path, req)


def _request_headers(req: Request, *, json_body: bool = False) -> dict:
    """生成上游请求头，保留鉴权信息并设置当前请求的会话标识。"""
    headers = _filter_forward_headers(req.headers, "")
    if json_body:
        headers.pop("content-encoding", None)
        headers["content-type"] = "application/json"
        # 模型路由通过 aiter_bytes 读取响应并重新发送，统一请求无压缩字节。
        headers["accept-encoding"] = "identity"
    headers["session-id"] = req.state.proxy_session
    return headers


def _record_failure(req: Request, exc: Exception, *, streaming: bool = False) -> None:
    """将连接错误、超时或流中断的类型写入当前请求的轨迹状态。"""
    if streaming:
        req.state.proxy_error = "upstream_stream_interrupted"
    elif isinstance(exc, httpx.TimeoutException):
        req.state.proxy_error = "upstream_timeout"
    else:
        req.state.proxy_error = "upstream_error"


def _failure_status(req: Request) -> int:
    """将响应开始前的上游超时映射为 504，其他上游错误映射为 502。"""
    return 504 if getattr(req.state, "proxy_error", None) == "upstream_timeout" else 502


async def _stream_response(req: Request, source: AsyncIterator[bytes], **kwargs) -> StreamingResponse:
    """读取上游首块和状态后，创建发往客户端的流式响应。"""
    # 等上游给出状态或首块，再提交下游 HTTP 响应。SSE 的内容仍由原处理函数生成。
    req.state.proxy_stream = source
    first = await anext(source, None)
    upstream_status = getattr(req.state, "proxy_upstream_status", None)
    status = upstream_status or 200
    if getattr(req.state, "proxy_error", None) and (upstream_status is None or upstream_status == 200):
        status = _failure_status(req)
    error_headers = getattr(req.state, "proxy_stream_headers", None)
    if error_headers is not None:
        kwargs["headers"] = error_headers
        kwargs["media_type"] = None

    async def body():
        """先发出已读取的首块，再继续转发剩余响应。"""
        if first is not None:
            yield first
        async for chunk in source:
            yield chunk

    return StreamingResponse(body(), status_code=status, **kwargs)


async def _stream_http_error(req: Request, response: httpx.Response) -> bytes:
    """读取上游 HTTP 错误响应，保存用于透传的响应头。"""
    headers = _filter_response_headers(response.headers)
    headers.pop("content-encoding", None)
    req.state.proxy_stream_headers = headers
    return await response.aread()


@app.post("/v1/messages")
async def anthropic_messages(req: Request):
    """转发 Anthropic Messages 请求并解析普通 JSON 或 SSE 响应。"""
    body = dict(req.state.proxy_body)
    stream = bool(body.get("stream", False))
    body_model = body.get("model")
    ban_explore = BAN_EXPLORE

    model_from_body: Optional[str] = body_model if isinstance(body_model, str) else None
    suffix = "--ban_explore"
    if model_from_body and model_from_body.endswith(suffix):
        # 任何带 "--ban_explore" 后缀的模型名，都强制开启屏蔽 Explore
        ban_explore = True
        base_model = model_from_body[: -len(suffix)]
        model = _resolve_model_name(base_model or "byenv")
    else:
        model = _resolve_model_name(body_model)
    upstream_base = req.state.proxy_target
    upstream_url = _target_url(req)
    upstream_headers = _request_headers(req, json_body=True)
    verify = True
    if "model" in body:
        body["model"] = model

    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    # 清洗空 text content block，避免上游 400 "text content blocks must be non-empty"
    if "messages" in body:
        body["messages"] = _sanitize_messages(body["messages"])

    # ---- non-stream ----
    if not stream:
        r = None
        last_exception = None
        success = False
        try:
            async with shared_client(upstream_base, verify, TRUST_ENV) as client:
                for attempt in range(MAX_RETRIES):
                    try:
                        r = await client.post(upstream_url, headers=upstream_headers, json=body)
                        req.state.proxy_upstream_status = r.status_code
                        if r.status_code == 200:
                            try:
                                resp_json = r.json()
                                if _has_valid_content(resp_json):
                                    success = True
                                    break
                                logging.warning(f"Attempt {attempt} empty content (anthropic non-stream): {r.text[:200]}")
                            except Exception:
                                # JSON 解析失败：透传原始响应
                                success = True
                                break
                    except Exception as e:
                        _record_failure(req, e)
                        last_exception = e

        except Exception as e:
            _record_failure(req, e)
            last_exception = e
            logging.error(f"Failed to create httpx client (anthropic non-stream): {e}")

        if not success:
            if r is not None:
                # 透传上游最后一次错误响应
                if r.status_code == 200:
                    req.state.proxy_error = "upstream_empty_content"
                error_msg = f"HTTP {r.status_code}"
                logging.error(f"Upstream request failed (anthropic non-stream), passing through: {error_msg}")
                req.state.proxy_parsed = _resp_to_obj(r)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                logging.error(f"Upstream request failed (anthropic non-stream): {error_msg}")
                return JSONResponse(
                    status_code=_failure_status(req),
                    content={"type": "error", "error": {"type": "upstream_error", "message": f"上游转发失败: {error_msg}"}},
                )

        req.state.proxy_parsed = _resp_to_obj(r)
        usage = {}
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            req.state.proxy_usage = usage
            if resp_json.get("error"):
                req.state.proxy_error = "upstream_api_error"
        except Exception:
            pass
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (pure pass-through) ----
    async def anthropic_sse_passthrough() -> AsyncIterator[bytes]:
        """在起始事件前缓冲 Anthropic 响应，再逐块转发并解析轨迹。"""
        up_chunks: List[Any] = []
        req.state.proxy_chunks = up_chunks
        log_buf = bytearray()
        connection_established = False
        last_exception = None

        try:
            async with shared_client(upstream_base, verify, TRUST_ENV) as client:
                # 保留上游转发循环，网关只尝试一次。
                for attempt in range(MAX_RETRIES):
                    try:
                        async with client.stream("POST", upstream_url, headers=upstream_headers, json=body) as r:
                            req.state.proxy_upstream_status = r.status_code
                            up_chunks.append({
                                "type": "anthropic_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": dict(r.headers),
                            })

                            if r.status_code >= 400:
                                yield await _stream_http_error(req, r)
                                return

                            # Stream with early commit on message_start:
                            # Buffer until we see message_start, then flush + pass-through directly.
                            # 缺少起始事件时记录失败，由网关返回 502。
                            raw_buf = bytearray()   # pre-commit buffer only
                            committed = False
                            line_buf = ""

                            async for raw in r.aiter_bytes():
                                log_buf.extend(raw)
                                if committed:
                                    yield raw
                                else:
                                    raw_buf.extend(raw)
                                    line_buf += raw.decode("utf-8", errors="replace")
                                    while "\n" in line_buf:
                                        line, line_buf = line_buf.split("\n", 1)
                                        if line.startswith("data:"):
                                            data_part = line[5:].strip()
                                            if data_part and data_part != "[DONE]":
                                                try:
                                                    event = json.loads(data_part)
                                                    if isinstance(event, dict) and event.get("type") in ("message_start", "error"):
                                                        committed = True
                                                        connection_established = True
                                                        yield bytes(raw_buf)
                                                        raw_buf = bytearray()
                                                        break
                                                except (ValueError, RecursionError):
                                                    pass

                            if not committed:
                                req.state.proxy_error = "upstream_missing_message_start"
                                yield bytes(raw_buf)

                            return

                    except Exception as e:
                        _record_failure(req, e, streaming=connection_established)
                        if connection_established:
                            # Already streaming to client, can't retry
                            logging.warning(f"Stream interrupted (anthropic stream): {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (anthropic stream): {e}")

                # Upstream request failed without connecting
                error_msg = str(last_exception) if last_exception else "unknown"
                logging.error(f"Upstream request failed (anthropic stream): {error_msg}")
                err_event = {"type": "error", "error": {"type": "upstream_error", "message": f"上游转发失败: {error_msg}"}}
                yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")

        except Exception as e:
            _record_failure(req, e)
            logging.error(f"Failed to create httpx client (anthropic stream): {e}")
            err_event = {"type": "error", "error": {"type": "connection_error", "message": str(e)}}
            yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")
        finally:
            # Parse full log_buf for logging
            for line in log_buf.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    data_part = line[5:].strip()
                    if data_part and data_part != "[DONE]":
                        try:
                            up_chunks.append(json.loads(data_part))
                        except (ValueError, RecursionError):
                            pass
            _usage_raw = {}
            for _c in up_chunks:
                if isinstance(_c, dict):
                    _message = _c.get("message")
                    _u = (_message.get("usage") if isinstance(_message, dict) else None) or _c.get("usage") or {}
                    if isinstance(_u, dict):
                        _usage_raw.update(_u)
            req.state.proxy_usage = _usage_raw

    return await _stream_response(
        req,
        anthropic_sse_passthrough(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def openai_chat_completions(req: Request):
    """转发 OpenAI Chat Completions 请求，处理普通 JSON 和流式响应。"""
    body = dict(req.state.proxy_body)
    stream = bool(body.get("stream", False))
    body_model = body.get("model")
    ban_explore = BAN_EXPLORE

    model_from_body: Optional[str] = body_model if isinstance(body_model, str) else None
    suffix = "--ban_explore"
    if model_from_body and model_from_body.endswith(suffix):
        # 任何带 "--ban_explore" 后缀的模型名，都强制开启屏蔽 Explore
        ban_explore = True
        base_model = model_from_body[: -len(suffix)]
        model = _resolve_model_name(base_model or "byenv")
    else:
        model = _resolve_model_name(body_model)

    upstream_base = req.state.proxy_target
    upstream_url = _target_url(req)
    upstream_headers = _request_headers(req, json_body=True)
    verify = True
    if "model" in body:
        body["model"] = model
    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    # ---- non-stream ----
    if not stream:
        r = None
        last_exception = None
        success = False
        try:
            async with shared_client(upstream_base, verify, TRUST_ENV) as client:
                for attempt in range(MAX_RETRIES):
                    try:
                        r = await client.post(upstream_url, headers=upstream_headers, json=body)
                        req.state.proxy_upstream_status = r.status_code
                        if r.status_code == 200:
                            success = True
                            break
                        # 非 200 一律重试，最大次数后透传
                    except Exception as e:
                        _record_failure(req, e)
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (openai non-stream): {type(e).__name__}: {e}")

        except Exception as e:
            _record_failure(req, e)
            last_exception = e
            logging.error(f"Failed to create httpx client (openai non-stream): {e}")

        if not success:
            if r is not None:
                # 透传上游最后一次错误响应
                error_msg = f"HTTP {r.status_code}"
                logging.error(f"Upstream request failed (openai non-stream), passing through: {error_msg}")
                req.state.proxy_parsed = _resp_to_obj(r)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                logging.error(f"Upstream request failed (openai non-stream): {error_msg}")
                return JSONResponse(
                    status_code=_failure_status(req),
                    content={"error": {"message": f"上游转发失败: {error_msg}", "type": "upstream_error"}},
                )

        req.state.proxy_parsed = _resp_to_obj(r)
        usage = {}
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            req.state.proxy_usage = usage
            if resp_json.get("error"):
                req.state.proxy_error = "upstream_api_error"
        except Exception:
            pass
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (OpenAI SSE pass-through) ----
    async def sse_passthrough() -> AsyncIterator[bytes]:
        """逐块转发 Chat Completions 流，解析响应事件并收集用量。"""
        up_chunks: List[Any] = []
        req.state.proxy_chunks = up_chunks
        raw_buf = bytearray()
        connection_established = False
        last_exception = None

        try:
            async with shared_client(upstream_base, verify, TRUST_ENV) as client:
                for attempt in range(MAX_RETRIES):
                    try:
                        async with client.stream("POST", upstream_url, headers=upstream_headers, json=body) as r:
                            req.state.proxy_upstream_status = r.status_code
                            up_chunks.append({
                                "type": "openai_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": dict(r.headers),
                            })

                            if r.status_code != 200:
                                yield await _stream_http_error(req, r)
                                return

                            # Pure pass-through: tee raw bytes to client and capture for logging
                            async for raw in r.aiter_bytes():
                                raw_buf.extend(raw)
                                connection_established = True
                                yield raw

                            return

                    except Exception as e:
                        _record_failure(req, e, streaming=connection_established)
                        if connection_established:
                            logging.warning(f"Stream interrupted (openai stream): {type(e).__name__}: {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (openai stream): {type(e).__name__}: {e}")

                # Upstream request failed
                error_msg = str(last_exception) if last_exception else "unknown"
                logging.error(f"Upstream request failed (openai stream): {error_msg}")
                error_data = {"error": {"message": f"上游转发失败: {error_msg}", "type": "upstream_error"}}
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        except Exception as e:
            _record_failure(req, e)
            logging.error(f"Failed to create httpx client (openai stream): {e}")
            error_data = {"error": {"message": str(e), "type": "connection_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            # Parse captured SSE for logging (best-effort)
            for line in raw_buf.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    data_part = line[5:].strip()
                    if data_part and data_part != "[DONE]":
                        try:
                            up_chunks.append(json.loads(data_part))
                        except (ValueError, RecursionError):
                            pass
            # 无论正常/异常/客户端断开，尽最大努力落盘
            # 统计 token（从 usage chunk 提取）
            _usage_raw = {}
            for _c in up_chunks:
                if isinstance(_c, dict):
                    _u = _c.get("usage") or {}
                    if isinstance(_u, dict):
                        _usage_raw.update(_u)
            req.state.proxy_usage = _usage_raw

    return await _stream_response(
        req,
        sse_passthrough(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/responses")
async def openai_responses(req: Request):
    """转发 OpenAI Responses 请求，处理普通 JSON 和命名 SSE 事件。"""
    body = dict(req.state.proxy_body)
    stream = bool(body.get("stream", False))
    body_model = body.get("model")
    ban_explore = BAN_EXPLORE

    model_from_body: Optional[str] = body_model if isinstance(body_model, str) else None
    suffix = "--ban_explore"
    if model_from_body and model_from_body.endswith(suffix):
        ban_explore = True
        base_model = model_from_body[: -len(suffix)]
        model = _resolve_model_name(base_model or "byenv")
    else:
        model = _resolve_model_name(body_model)

    upstream_base = req.state.proxy_target
    upstream_url = _target_url(req)
    upstream_headers = _request_headers(req, json_body=True)
    verify = True
    if "model" in body:
        body["model"] = model

    # ban_explore 处理 tools
    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    # ---- non-stream ----
    if not stream:
        r = None
        last_exception = None
        success = False
        try:
            async with shared_client(upstream_base, verify, TRUST_ENV) as client:
                for attempt in range(MAX_RETRIES):
                    try:
                        r = await client.post(upstream_url, headers=upstream_headers, json=body)
                        req.state.proxy_upstream_status = r.status_code
                        if r.status_code == 200:
                            success = True
                            break
                    except Exception as e:
                        _record_failure(req, e)
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (responses non-stream): {type(e).__name__}: {e}")

        except Exception as e:
            _record_failure(req, e)
            last_exception = e
            logging.error(f"Failed to create httpx client (responses non-stream): {e}")

        if not success:
            if r is not None:
                error_msg = f"HTTP {r.status_code}"
                logging.error(f"Upstream request failed (responses non-stream), passing through: {error_msg}")
                req.state.proxy_parsed = _resp_to_obj(r)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                logging.error(f"Upstream request failed (responses non-stream): {error_msg}")
                return JSONResponse(
                    status_code=_failure_status(req),
                    content={"error": {"message": f"上游转发失败: {error_msg}", "type": "upstream_error"}},
                )

        req.state.proxy_parsed = _resp_to_obj(r)
        usage = {}
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            req.state.proxy_usage = usage
            if resp_json.get("error") or resp_json.get("status") in ("failed", "incomplete"):
                req.state.proxy_error = "upstream_api_error"
        except Exception:
            pass
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (Responses API pass-through) ----
    async def responses_sse_passthrough() -> AsyncIterator[bytes]:
        """逐块转发 Responses 流，解析事件和嵌套的用量字段。"""
        up_chunks: List[Any] = []
        req.state.proxy_chunks = up_chunks
        raw_buf = bytearray()
        connection_established = False
        last_exception = None

        try:
            async with shared_client(upstream_base, verify, TRUST_ENV) as client:
                for attempt in range(MAX_RETRIES):
                    try:
                        async with client.stream("POST", upstream_url, headers=upstream_headers, json=body) as r:
                            req.state.proxy_upstream_status = r.status_code
                            up_chunks.append({
                                "type": "responses_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": dict(r.headers),
                            })

                            if r.status_code != 200:
                                yield await _stream_http_error(req, r)
                                return

                            # Pure pass-through: tee raw bytes to client and capture for logging
                            async for raw in r.aiter_bytes():
                                raw_buf.extend(raw)
                                connection_established = True
                                yield raw

                            return

                    except Exception as e:
                        _record_failure(req, e, streaming=connection_established)
                        if connection_established:
                            logging.warning(f"Stream interrupted (responses stream): {type(e).__name__}: {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (responses stream): {type(e).__name__}: {e}")

                # Upstream request failed
                error_msg = str(last_exception) if last_exception else "unknown"
                logging.error(f"Upstream request failed (responses stream): {error_msg}")
                error_data = {"error": {"message": f"上游转发失败: {error_msg}", "type": "upstream_error"}}
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        except Exception as e:
            _record_failure(req, e)
            logging.error(f"Failed to create httpx client (responses stream): {e}")
            error_data = {"error": {"message": str(e), "type": "connection_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            # Parse captured SSE for logging (best-effort)
            for line in raw_buf.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    data_part = line[5:].strip()
                    if data_part and data_part != "[DONE]":
                        try:
                            up_chunks.append(json.loads(data_part))
                        except (ValueError, RecursionError):
                            pass
            _usage_raw = {}
            for _c in up_chunks:
                if isinstance(_c, dict):
                    _response = _c.get("response")
                    _u = _c.get("usage") or (_response.get("usage") if isinstance(_response, dict) else None) or {}
                    if isinstance(_u, dict):
                        _usage_raw.update(_u)
            req.state.proxy_usage = _usage_raw

    return await _stream_response(
        req,
        responses_sse_passthrough(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def passthrough(full_path: str, req: Request):
    """将其他请求直接转发到配置的上游。"""
    root = req.state.proxy_target
    target = _target_url(req)
    fwd_headers = _request_headers(req)
    body = await req.body()
    verify = True

    _t0 = time.perf_counter()
    try:
        client = await get_client(root, verify, TRUST_ENV)
        upstream_req = client.build_request(
            req.method, target, headers=fwd_headers, content=body or None,
        )
        r = await client.send(upstream_req, stream=True)
        req.state.proxy_upstream_status = r.status_code
        req.state.proxy_upstream_close = r.aclose
    except Exception as ex:
        _record_failure(req, ex)
        logging.warning("passthrough %s %s -> upstream error: %s", req.method, full_path, ex)
        return JSONResponse(
            {"error": {"type": "upstream_error", "message": str(ex)}}, status_code=_failure_status(req),
        )

    async def _body_iter():
        """转发上游原始字节，并在转发结束或异常时关闭上游响应。"""
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            _record_failure(req, exc, streaming=True)
            raise
        finally:
            await r.aclose()
            logging.info(
                "passthrough %s /%s -> %s [%s] %dms",
                req.method, full_path, target, r.status_code,
                int((time.perf_counter() - _t0) * 1000),
            )

    return StreamingResponse(
        _body_iter(),
        status_code=r.status_code,
        headers=_filter_response_headers(r.headers),
    )
