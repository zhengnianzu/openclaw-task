import re
import os
import json
import httpx
import asyncio
import logging
import base64
import copy
import hashlib
import ipaddress
import math
import socket
from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from uuid import uuid4
from urllib.parse import quote, urlsplit

from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from typing import Any, Dict, List, Optional, AsyncIterator
from fastapi.responses import JSONResponse, StreamingResponse, Response

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from print_stats_summary import TRAJECTORY_DIR, statistic_tokens
from sandbox_identity import get_sandbox_id

load_dotenv(override=True)

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
TRUST_ENV = os.getenv("TRUST_ENV", "true").lower() == "true"
TAICHU_MAAS_COMPAT = os.getenv("TAICHU_MAAS_COMPAT", "").lower() in {"1", "true", "yes"}

# 全局默认：重试次数（不从环境变量读取）
MAX_RETRIES = 20

# 转发、解析及重试基于 llm-proxy-lite 的 app.py（f88838f）。
app = FastAPI(title="模型 API 转发网关")
_GATEWAY_LOGGER = logging.getLogger("llm_proxy_gateway")
_MODEL_PATHS = {
    "/v1/messages": "anthropic",
    "/v1/chat/completions": "openai",
    "/chat/completions": "openai",
    "/v1/responses": "responses",
}
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
}
_SECRET_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
_LOG_LOCK = Lock()
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def get_x_auth_token(request) -> str:
    """沿用 Lite 的 Bearer 提取，并兼容 Anthropic 的原始 x-api-key。"""
    keys = ['authorization', 'x-api-key']
    for key in keys:
        ack = request.headers.get(key)
        if isinstance(ack, str) and ack.startswith('Bearer '):
            ack = ack.split('Bearer ')[1].strip()
            return ack
    return request.headers.get('x-api-key') or ''


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
        """从工具描述中去掉 Explore 项，保留其他文字。"""
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


def _taichu_maas_body(body: dict) -> dict:
    """兼容 MaaS 的工具 schema 和关联 ID 限制，原始请求仍用于轨迹记录。"""
    body = copy.deepcopy(body)
    for tool in body.get("tools", []):
        function = tool.get("function", {})
        parameters = function.get("parameters", {})
        if (function.get("name") == "edit" and "anyOf" in parameters
                and {"old_text", "new_text", "edits"}.issubset(parameters.get("properties", {}))):
            parameters.pop("anyOf")
            function["description"] = (function.get("description", "")
                                       + "\nProvide either old_text with new_text, or edits; do not combine them.")

    def short_id(value):
        if isinstance(value, str) and len(value) > 32:
            return "call_" + hashlib.sha256(value.encode()).hexdigest()[:24]
        return value

    for message in body.get("messages", []):
        for call in message.get("tool_calls", []):
            call["id"] = short_id(call.get("id"))
        if "tool_call_id" in message:
            message["tool_call_id"] = short_id(message["tool_call_id"])
    return body


def _forward_headers(headers, exclude=()) -> list:
    """过滤逐跳请求头和本次转发需要重设的字段。"""
    connection = {name.strip().lower() for name in headers.get("connection", "").split(",")}
    blocked = _HOP_HEADERS | connection | set(exclude)
    return [(name, value) for name, value in headers.items() if name.lower() not in blocked]


def build_upstream_headers(x_auth_token: str, model_id: str, req: Request) -> Dict[str, str]:
    """
    保留 Lite 的鉴权与模型头设置，并带上会话和客户端协议头。
    """
    ack = os.getenv('UPSTREAM_API_KEY') or x_auth_token
    return {
        **dict(_forward_headers(req.headers, (
            "host", "content-length", "content-encoding", "content-type", "authorization",
            "x-api-key", "model-id", "session-id", "accept-encoding", "x-sandbox-id",
        ))),
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ack}",
        "x-api-key": ack,
        "Model-Id": os.environ.get("MODEL_ID") or model_id,
        "session-id": req.state.session_id,
        "X-Sandbox-Id": req.state.sandbox_id,
    }


def _ssl_verify() -> bool:
    """读取上游 TLS 证书校验开关，默认开启。"""
    return os.getenv("SSL_VERIFY", "true").lower() != "false"


# -----------------------------
# Rate limit helpers
# -----------------------------
# 所有需要视为「限流且可重试」的上游状态码，统一维护在这里，便于后续扩展（如再加入 503 等）
RATE_LIMIT_STATUS_CODES = {406, 429}


def is_rate_limit_status(status_code: int) -> bool:
    """
    判断上游响应码是否属于「限流/可重试」错误。
    所有调用处统一依赖本函数，而不是直接写死 (406, 429)。
    """
    return status_code in RATE_LIMIT_STATUS_CODES


def _upstream_url(req: Request) -> str:
    """直接拼接目标基础地址、原始路径和查询参数。"""
    return req.state.target + _request_path(req)


def _request_path(req: Request) -> str:
    """读取转发和轨迹使用的原始路径与查询参数。"""
    raw_path = req.scope.get("raw_path")
    path = raw_path.decode("ascii") if raw_path else quote(req.url.path)
    return path + ("?" + req.url.query if req.url.query else "")


def _json_float(value: str) -> float | str:
    """以字符串保留超出浮点数范围的数字，避免写出非法 JSON。"""
    number = float(value)
    return number if math.isfinite(number) else value


def _parse_json(raw: bytes | str):
    """解析可安全保存为 JSONL 的 JSON 值。"""
    return json.loads(raw, parse_float=_json_float, parse_constant=str)


def _resp_to_obj(r):  # httpx.Response -> dict
    """沿用 Lite 的 JSON 或文本响应解析，保存时隐藏凭据。"""
    base = {"status_code": r.status_code, "headers": _log_headers(r.headers)}
    try:
        base["json"] = r.json()
    except Exception:
        base["text"] = r.text
    return base


def _upstream_failure(req: Request, exc: Exception) -> int:
    """记录连接异常或超时，并返回适当的 HTTP 状态码。"""
    timeout = isinstance(exc, httpx.TimeoutException)
    req.state.error = "upstream_timeout" if timeout else "upstream_error"
    req.state.error_message = _redact_gateway_text(req, f"{type(exc).__name__}: {exc}")
    logging.warning("供应商 %s 转发失败（%s）: %s", req.state.provider, req.state.request_id, type(exc).__name__)
    return 504 if timeout else 502


async def _stream_response(req: Request, source: AsyncIterator[bytes]) -> StreamingResponse:
    """通用转发拿到上游状态后回传响应，并在流结束时落盘。"""
    first = await anext(source, None)
    state = req.state
    status = state.status
    headers = httpx.Headers(state.response_headers)
    headers["session-id"] = state.session_id
    headers["X-Sandbox-Id"] = state.sandbox_id
    state.response_started = True

    async def body():
        """逐块返回原始内容，保留收尾时已读到的响应字节。"""
        raw = bytearray()
        try:
            if first is not None:
                raw.extend(first)
                yield first
            async for chunk in source:
                raw.extend(chunk)
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            state.error = "client_disconnected"
            raise
        finally:
            try:
                await source.aclose()
            finally:
                _record_trajectory(req, status, headers, raw)

    return StreamingResponse(body(), status_code=status, headers=dict(headers))


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/hi")
async def health(req: Request):
    """返回 Lite 的本地健康检查响应。"""
    await _prepare_request(req)
    return _record_response(req, JSONResponse({"LLM_PROXY": "hello !!!"}))


@app.post("/v1/messages")
async def anthropic_messages(req: Request):
    """按 Lite 的普通和 SSE 分支转发 Anthropic Messages。"""
    await _prepare_request(req)
    try:
        body = await req.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return _record_response(req, JSONResponse({"error": {"type": "invalid_json"}}, status_code=400))
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
    upstream_url = _upstream_url(req)
    verify = _ssl_verify()

    x_auth_token = await get_x_auth_token(req)
    upstream_headers = build_upstream_headers(x_auth_token, model, req)
    body["model"] = req.state.model = upstream_headers['Model-Id']

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
        upstream_attempts = 0
        try:
            async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(500.0),
                    trust_env=TRUST_ENV,
            ) as client:
                for attempt in range(MAX_RETRIES):
                    upstream_attempts += 1
                    try:
                        r = await client.post(upstream_url, headers=upstream_headers, json=body)
                        last_exception = None
                        if r.status_code == 200:
                            success = True
                            break
                        _gateway_debug(req, f"Attempt {attempt} non-200 (anthropic non-stream): {r.status_code} {r.text[:200]}")
                    except Exception as e:
                        last_exception = e
                        _gateway_debug(req, f"Attempt {attempt} upstream error (anthropic non-stream): {e}")

                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(0.5)
                        x_auth_token = await get_x_auth_token(req)
                        upstream_headers = build_upstream_headers(x_auth_token, model, req)
        except asyncio.CancelledError:
            req.state.error = "client_disconnected"
            req.state.total_attempts = upstream_attempts
            _record_trajectory(req, 499, httpx.Headers())
            raise
        except Exception as e:
            last_exception = e
            logging.error(f"Failed to create httpx client (anthropic non-stream): {e}")

        req.state.success = success
        req.state.total_attempts = upstream_attempts
        if r is None:
            error_msg = str(last_exception) if last_exception else "unknown"
            req.state.parsed_response = {"error": "max_retries_exceeded", "detail": error_msg}
            return _record_response(req, JSONResponse(
                status_code=502,
                content={"type": "error", "error": {"type": "max_retries_exceeded", "message": f"上游多次失败({MAX_RETRIES}次): {error_msg}"}},
            ))

        req.state.parsed_response = _resp_to_obj(r)
        if success:
            tok_in, tok_out = 0, 0
            try:
                resp_json = r.json()
                usage = resp_json.get("usage", {})
                tok_in = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                tok_out = usage.get("output_tokens") or 0
            except Exception:
                pass
            req.state.tokens = tok_in, tok_out
        return _record_response(req, Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        ))

    # ---- stream SSE (pure pass-through) ----
    response_headers = {**_SSE_HEADERS, "session-id": req.state.session_id,
                        "X-Sandbox-Id": req.state.sandbox_id}
    async def anthropic_sse_passthrough() -> AsyncIterator[bytes]:
        """沿用 Lite 的 SSE 透传和重试循环。"""
        up_chunks: List[Any] = []
        log_buf = bytearray()
        connection_established = False
        upstream_attempts = 0
        last_exception = None
        last_retry_status = None
        retry_headers = upstream_headers
        retry_token = x_auth_token

        try:
            async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(500.0),
                    trust_env=TRUST_ENV,
            ) as client:
                for attempt in range(MAX_RETRIES):
                    upstream_attempts += 1
                    log_buf.clear()
                    try:
                        async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                            req.state.upstream_status = r.status_code
                            up_chunks.append({
                                "type": "anthropic_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": _log_headers(r.headers),
                            })

                            if is_rate_limit_status(r.status_code):
                                err = await r.aread()
                                last_retry_err_text = err.decode("utf-8", errors="replace")
                                last_retry_status = r.status_code
                                up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                                _gateway_debug(req, f"Attempt {attempt} rate limit (anthropic stream): {r.status_code}")
                                if attempt < MAX_RETRIES - 1:
                                    await asyncio.sleep(0.5)
                                    retry_token = await get_x_auth_token(req)
                                    retry_headers = build_upstream_headers(retry_token, model, req)
                                continue

                            connection_established = True

                            if r.status_code >= 400:
                                req.state.error = "upstream_http_error"
                                err = await r.aread()
                                err_text = err.decode("utf-8", errors="replace")
                                up_chunks.append({"type": "error_body", "body": err_text})
                                error_data = {"type": "error", "error": {"type": "api_error", "message": err_text}}
                                payload = f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                                log_buf.extend(payload)
                                yield payload
                                return

                            raw_buf = bytearray()
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
                                                    if json.loads(data_part).get("type") == "message_start":
                                                        committed = True
                                                        yield bytes(raw_buf)
                                                        raw_buf = bytearray()
                                                        break
                                                except json.JSONDecodeError:
                                                    pass

                            if not committed:
                                if attempt < MAX_RETRIES - 1:
                                    _gateway_debug(req, f"Attempt {attempt} no message_start in SSE (anthropic stream), retrying")
                                    connection_established = False
                                    up_chunks.clear()
                                    await asyncio.sleep(0.5)
                                    retry_token = await get_x_auth_token(req)
                                    retry_headers = build_upstream_headers(retry_token, model, req)
                                    continue
                                else:
                                    connection_established = True
                                    yield bytes(raw_buf)

                            return

                    except Exception as e:
                        if connection_established:
                            req.state.error = "upstream_stream_error"
                            _gateway_debug(req, f"Stream interrupted (anthropic stream): {e}")
                            return
                        last_exception = e
                        _gateway_debug(req, f"Attempt {attempt} upstream error (anthropic stream): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            retry_token = await get_x_auth_token(req)
                            retry_headers = build_upstream_headers(retry_token, model, req)

                # All retries exhausted
                req.state.error = "max_retries_exceeded"
                error_msg = str(last_exception) if last_exception else (f"HTTP {last_retry_status}" if last_retry_status else "unknown")
                _gateway_debug(req, f"All retries exhausted (anthropic stream): {error_msg}")
                err_event = {"type": "error", "error": {"type": "max_retries_exceeded", "message": f"上游多次失败({MAX_RETRIES}次): {error_msg}"}}
                payload = f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")
                log_buf.extend(payload)
                yield payload

        except (asyncio.CancelledError, GeneratorExit):
            req.state.error = "client_disconnected"
            raise
        except Exception as e:
            req.state.error = "connection_error"
            logging.error(f"Failed to create httpx client (anthropic stream): {e}")
            err_event = {"type": "error", "error": {"type": "connection_error", "message": str(e)}}
            payload = f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")
            log_buf.extend(payload)
            yield payload
        finally:
            _finalize_stream(req, log_buf, up_chunks, connection_established, upstream_attempts)
            _record_trajectory(req, 200, httpx.Headers(response_headers), log_buf)

    return StreamingResponse(
        anthropic_sse_passthrough(),
        media_type="text/event-stream",
        headers=response_headers,
    )


# ---------- OpenAI Chat Completions ----------
@app.post("/v1/responses")
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def openai_chat_completions(req: Request):
    """
    按 Lite 的普通和 SSE 分支转发 OpenAI Chat Completions 和 Responses。
    """
    await _prepare_request(req)
    try:
        body = await req.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return _record_response(req, JSONResponse({"error": {"type": "invalid_json"}}, status_code=400))
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

    upstream_url = _upstream_url(req)
    verify = _ssl_verify()

    x_auth_token = await get_x_auth_token(req)
    upstream_headers = build_upstream_headers(x_auth_token, model, req)
    body["model"] = req.state.model = upstream_headers['Model-Id']
    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    if TAICHU_MAAS_COMPAT and req.url.path != "/v1/responses":
        body = _taichu_maas_body(body)

    # ---- non-stream ----
    if not stream:
        r = None
        last_exception = None
        success = False
        upstream_attempts = 0
        try:
            async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(500.0),
                    trust_env=TRUST_ENV,
            ) as client:
                for attempt in range(MAX_RETRIES):
                    upstream_attempts += 1
                    try:
                        r = await client.post(upstream_url, headers=upstream_headers, json=body)
                        last_exception = None
                        if r.status_code == 200:
                            success = True
                            break
                        _gateway_debug(req, f"Attempt {attempt} non-200 (openai non-stream): {r.status_code} {r.text[:200]}")
                    except Exception as e:
                        last_exception = e
                        _gateway_debug(req, f"Attempt {attempt} upstream error (openai non-stream): {e}")

                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(0.5)
                        x_auth_token = await get_x_auth_token(req)
                        upstream_headers = build_upstream_headers(x_auth_token, model, req)
        except asyncio.CancelledError:
            req.state.error = "client_disconnected"
            req.state.total_attempts = upstream_attempts
            _record_trajectory(req, 499, httpx.Headers())
            raise
        except Exception as e:
            last_exception = e
            logging.error(f"Failed to create httpx client (openai non-stream): {e}")

        req.state.success = success
        req.state.total_attempts = upstream_attempts
        if r is None:
            error_msg = str(last_exception) if last_exception else "unknown"
            req.state.parsed_response = {"error": "max_retries_exceeded", "detail": error_msg}
            return _record_response(req, JSONResponse(
                status_code=502,
                content={"error": {"message": f"\u4e0a\u6e38\u591a\u6b21\u5931\u8d25({MAX_RETRIES}\u6b21): {error_msg}", "type": "max_retries_exceeded"}},
            ))

        req.state.parsed_response = _resp_to_obj(r)
        if success:
            tok_in, tok_out = 0, 0
            try:
                resp_json = r.json()
                usage = resp_json.get("usage", {})
                tok_in = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                tok_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
            except Exception:
                pass
            req.state.tokens = tok_in, tok_out
        return _record_response(req, Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        ))

    # ---- stream SSE (OpenAI SSE pass-through) ----
    response_headers = {**_SSE_HEADERS, "session-id": req.state.session_id,
                        "X-Sandbox-Id": req.state.sandbox_id}
    async def sse_passthrough() -> AsyncIterator[bytes]:
        """沿用 Lite 的 SSE 透传和重试循环。"""
        up_chunks: List[Any] = []
        log_buf = bytearray()
        connection_established = False
        upstream_attempts = 0
        last_exception = None
        last_retry_status = None
        maas_retries = 0
        retry_headers = upstream_headers
        retry_token = x_auth_token

        try:
            async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(500.0),
                    trust_env=TRUST_ENV,
            ) as client:
                for attempt in range(MAX_RETRIES):
                    upstream_attempts += 1
                    log_buf.clear()
                    try:
                        async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                            req.state.upstream_status = r.status_code
                            up_chunks.append({
                                "type": "openai_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": _log_headers(r.headers),
                            })

                            if is_rate_limit_status(r.status_code):
                                err = await r.aread()
                                last_retry_err_text = err.decode("utf-8", errors="replace")
                                last_retry_status = r.status_code
                                up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                                _gateway_debug(req, f"Attempt {attempt} rate limit (openai stream): {r.status_code}")
                                if attempt < MAX_RETRIES - 1:
                                    await asyncio.sleep(0.5)
                                    retry_token = await get_x_auth_token(req)
                                    retry_headers = build_upstream_headers(retry_token, model, req)
                                continue

                            if r.status_code >= 400:
                                err = await r.aread()
                                err_text = err.decode("utf-8", errors="replace")
                                up_chunks.append({"type": "error_body", "body": err_text})
                                try:
                                    error_data = json.loads(err_text)
                                except ValueError:
                                    error_data = {"error": {"type": "upstream_http_error", "message": f"HTTP {r.status_code}"}}
                                error = error_data.get("error") if isinstance(error_data, dict) else None
                                # 已用真实失败请求验证：MaaS 400001 可能在原样重放后恢复。
                                # 只对这个错误补一次重试，普通 400/401 不重试。
                                if (r.status_code == 400 and isinstance(error, dict)
                                        and str(error.get("code")) == "400001"
                                        and maas_retries < 1
                                        and attempt < MAX_RETRIES - 1):
                                    maas_retries += 1
                                    _GATEWAY_LOGGER.warning("模型上游 HTTP400 code=400001，原样重试 %s/%s request=%s",
                                                           maas_retries, 1, req.state.request_id)
                                    await asyncio.sleep(1)
                                    continue
                                connection_established = True
                                req.state.error = "upstream_http_error"
                                if not error:
                                    error_data = {"error": {"type": "upstream_http_error", "message": f"HTTP {r.status_code}"}}
                                # 流式响应必须返回 SSE 错误事件；裸 JSON 会被 SDK 当成空流。
                                payload = f"data: {json.dumps(error_data, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8")
                                log_buf.extend(payload)
                                yield payload
                                return

                            connection_established = True
                            async for raw in r.aiter_bytes():
                                log_buf.extend(raw)
                                yield raw

                            # Parse log_buf for logging
                            return

                    except Exception as e:
                        if connection_established:
                            req.state.error = "upstream_stream_error"
                            _gateway_debug(req, f"Stream interrupted (openai stream): {e}")
                            return
                        last_exception = e
                        _gateway_debug(req, f"Attempt {attempt} upstream error (openai stream): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            retry_token = await get_x_auth_token(req)
                            retry_headers = build_upstream_headers(retry_token, model, req)

                # All retries exhausted
                req.state.error = "max_retries_exceeded"
                error_msg = str(last_exception) if last_exception else (f"HTTP {last_retry_status}" if last_retry_status else "unknown")
                _gateway_debug(req, f"All retries exhausted (openai stream): {error_msg}")
                error_data = {"error": {"message": f"上游多次失败({MAX_RETRIES}次): {error_msg}", "type": "max_retries_exceeded"}}
                payload = f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                log_buf.extend(payload)
                yield payload
                log_buf.extend(b"data: [DONE]\n\n")
                yield b"data: [DONE]\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            req.state.error = "client_disconnected"
            raise
        except Exception as e:
            req.state.error = "connection_error"
            logging.error(f"Failed to create httpx client (openai stream): {e}")
            error_data = {"error": {"message": str(e), "type": "connection_error"}}
            payload = f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
            log_buf.extend(payload)
            yield payload
            log_buf.extend(b"data: [DONE]\n\n")
            yield b"data: [DONE]\n\n"
        finally:
            _finalize_stream(req, log_buf, up_chunks, connection_established, upstream_attempts)
            _record_trajectory(req, 200, httpx.Headers(response_headers), log_buf)

    return StreamingResponse(
        sse_passthrough(),
        media_type="text/event-stream",
        headers=response_headers,
    )


@app.get("/statistic")
async def statistic_tokens_web(req: Request, model: str = '', date_start: str = '', date_end: str = '', status: str = '全部'):
    """沿用 Lite 的 JSON 统计入口。"""
    await _prepare_request(req)
    res = await asyncio.to_thread(statistic_tokens, model=model, date_start=date_start, date_end=date_end, status=status)
    return _record_response(req, JSONResponse(res))


@app.exception_handler(StarletteHTTPException)
async def record_http_error(req: Request, exc: StarletteHTTPException):
    """为框架返回的 HTTP 错误补齐会话标识并落盘。"""
    if not hasattr(req.state, "session_id"):
        await _prepare_request(req)
    response = await http_exception_handler(req, exc)
    return _record_response(req, response)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def passthrough(req: Request):
    """将其他 HTTP 请求直接发往当前监听地址对应的供应商。"""
    await _prepare_request(req)
    headers = dict(_forward_headers(req.headers, ("host", "content-length", "x-sandbox-id")))
    headers["session-id"] = req.state.session_id
    headers["X-Sandbox-Id"] = req.state.sandbox_id
    url = _upstream_url(req)

    async def forward():
        """保留原始响应字节和内容编码，并在结束时关闭上游连接。"""
        try:
            async with httpx.AsyncClient(
                verify=_ssl_verify(), timeout=httpx.Timeout(500.0, connect=10.0), trust_env=TRUST_ENV,
            ) as client:
                async with client.stream(req.method, url, headers=headers, content=req.state.raw_body) as response:
                    req.state.status = response.status_code
                    req.state.upstream_status = response.status_code
                    req.state.response_headers = dict(_forward_headers(response.headers))
                    async for chunk in response.aiter_raw():
                        yield chunk
        except httpx.RequestError as exc:
            req.state.status = _upstream_failure(req, exc)
            if getattr(req.state, "response_started", False):
                raise
            req.state.response_headers = {"content-type": "application/json"}
            yield json.dumps({"error": {"type": req.state.error}}).encode()

    try:
        return await _stream_response(req, forward())
    except asyncio.CancelledError:
        req.state.error = "client_disconnected"
        _record_trajectory(req, 499, httpx.Headers())
        raise


_COOKIE_HEADERS = {"cookie", "set-cookie"}
_KEY_PREFIXES = ("sk-ant-", "sk-proj-", "sk-")   # 长前缀优先匹配


def _mask_secret(secret: str) -> str:
    """把密钥掩码为「前缀 + 首位 + **** + 后四位」
    - 保留 ``Bearer `` 等认证方案前缀；
    - 识别 sk- / sk-ant- / sk-proj- 前缀，前缀本身原样保留；
    - 去掉沙箱后缀，只取核心key的首位和后四位
    """
    if not secret:
        return secret
    scheme = ""
    head, sep, tail = secret.partition(" ")
    if sep and head.lower() in ("bearer", "basic", "token"):
        scheme, secret = head + " ", tail.strip()
    prefix = next((p for p in _KEY_PREFIXES if secret.startswith(p)), "")
    body = secret[len(prefix):]
    core = body.split("_", 1)[0]
    return f"{scheme}{prefix}{core[0]}****{core[-4:]}"


def _log_headers(headers) -> dict:
    """日志中的鉴权头改为掩码（保留首位与后四位便于对账），Cookie 整体隐藏。"""
    masked = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _COOKIE_HEADERS:
            masked[name] = "[已隐藏]"
        elif lowered in _SECRET_HEADERS:
            masked[name] = _mask_secret(value)
        else:
            masked[name] = value
    return masked


def _parse_body(raw: bytes, headers: httpx.Headers) -> tuple[str, Any]:
    """优先读取 JSON，其次保留文本或 Base64 编码的二进制内容。"""
    if not raw:
        return "json", None
    try:
        raw = httpx.Response(200, headers=headers, content=raw).content
    except httpx.DecodingError:
        return "base64", base64.b64encode(raw).decode("ascii")
    try:
        return "json", _parse_json(raw)
    except (ValueError, RecursionError):
        try:
            return "text", raw.decode("utf-8")
        except UnicodeDecodeError:
            return "base64", base64.b64encode(raw).decode("ascii")


def _session_id(headers, body) -> str:
    """按请求头、请求体、元数据的顺序读取会话标识，缺失时生成 UUID。"""
    candidates = [headers.get("session-id"), headers.get("x-session-id")]
    fields = ("session-id", "session_id", "sessionId")
    if isinstance(body, dict):
        candidates.extend(body.get(key) for key in fields)
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            candidates.extend(metadata.get(key) for key in fields)
            user_id = metadata.get("user_id")
            if isinstance(user_id, str):
                try:
                    user_id = json.loads(user_id)
                except (ValueError, RecursionError):
                    pass
            if isinstance(user_id, dict):
                candidates.extend(user_id.get(key) for key in fields)
            if isinstance(user_id, str):
                match = re.search(r"session_([A-Za-z0-9-]+)", user_id)
                if match:
                    candidates.append(match.group(1))
    for value in candidates:
        if isinstance(value, str):
            value = value.strip()
            if value and value.isascii() and value.isprintable():
                return value
    return uuid4().hex


def _jsonl(value: dict) -> bytes:
    """生成一条 UTF-8 JSONL 记录，供索引按字节定位。"""
    try:
        line = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError:
        # 原版 JSON 解析可读入非有限数，将它们转换为字符串后再写入 JSONL。
        line = json.dumps(_parse_json(json.dumps(value)), ensure_ascii=False, separators=(",", ":"))
    return (line + "\n").encode("utf-8", errors="backslashreplace")


def _append_trajectory(trace: dict, index: dict) -> None:
    """串行追加小时数据与索引，日志目录清空后即写即建。"""
    file = TRAJECTORY_DIR / index["file"]
    line = _jsonl(trace)
    with _LOG_LOCK:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("ab") as output:
            index["offset"] = output.tell()
            index["length"] = len(line)
            output.write(line)
        with (TRAJECTORY_DIR / "index.jsonl").open("ab") as output:
            output.write(_jsonl(index))


def _stream_events(raw: bytes | bytearray) -> tuple[list[dict], bool]:
    events, done = [], False
    for line in bytes(raw).decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        try:
            value = json.loads(data)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events, done


def _finalize_stream(req: Request, raw, chunks, connected: bool, attempts: int) -> None:
    """即使下游提前关闭，也从已转发内容判断流是否已正常结束。"""
    state = req.state
    events, done = _stream_events(raw)
    chunks.extend(events)
    terminal = any(event.get("type") in ("message_stop", "response.completed")
                   or any(choice.get("finish_reason") is not None
                          for choice in (event.get("choices") or []) if isinstance(choice, dict))
                   for event in events)
    error_event = any(event.get("error") or event.get("type") in
                      ("error", "response.failed", "response.incomplete") for event in events)
    upstream_status = getattr(state, "upstream_status", 200)
    if (getattr(state, "error", None) == "client_disconnected" and terminal
            and not error_event and upstream_status < 400):
        del state.error
        state.client_closed_after_completion = True
    if error_event:
        state.error = getattr(state, "error", None) or "upstream_sse_error"
    elif connected and not terminal and not getattr(state, "error", None):
        state.error = "incomplete_stream"
    state.chunks = chunks
    state.stream_completed = terminal
    state.stream_done_marker = done
    state.total_attempts = attempts
    state.success = connected and terminal and upstream_status < 400 and not getattr(state, "error", None)
    tok_in, tok_out = 0, 0
    for event in events:
        usage = event.get("usage") or (event.get("message") or {}).get("usage") or (event.get("response") or {}).get("usage") or {}
        tok_in = max(tok_in, usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        tok_out = max(tok_out, usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    state.tokens = tok_in, tok_out


def _request_kind(req: Request) -> str:
    path = req.url.path
    if path in _MODEL_PATHS:
        return "model"
    if path == "/hi":
        return "health"
    if path in ("/api/v1/models", "/api/tags", "/api/show", "/v1/props", "/props", "/version", "/v1/models", "/models") or path.startswith("/v1/models/"):
        return "probe"
    return "http"


def _redact_gateway_text(req: Request, message: str) -> str:
    secrets = [os.getenv("UPSTREAM_API_KEY", "")]
    for key in _SECRET_HEADERS:
        value = req.headers.get(key, "")
        secrets.extend((value, value.removeprefix("Bearer ")))
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "[已隐藏]")
    message = re.sub(r"\bsk-[\w-]{8,}", "[已隐藏]", message)
    message = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [已隐藏]", message)
    return message


def _gateway_debug(req: Request, message: str) -> None:
    message = _redact_gateway_text(req, message)
    _GATEWAY_LOGGER.debug("[gateway request=%s session=%s] %s", req.state.request_id,
                         req.state.session_id, message)


def _response_record(state, status: int, headers: httpx.Headers, raw: bytes | bytearray) -> dict:
    """合并路由解析结果与实际下发的响应，补充失败状态。"""
    result = getattr(state, "parsed_response", None) or {"headers": _log_headers(headers)}
    result.update(status_code=status, **{"session-id": state.session_id})
    upstream_status = getattr(state, "upstream_status", status)
    if upstream_status != status:
        result["upstream_status_code"] = upstream_status
    if hasattr(state, "stream_completed"):
        result["stream_completed"] = state.stream_completed
    if getattr(state, "client_closed_after_completion", False):
        result["client_closed_after_completion"] = True
    if "json" not in result and "text" not in result:
        kind, parsed = _parse_body(bytes(raw), headers)
        result[kind] = parsed
    chunks = getattr(state, "chunks", None)
    if chunks is not None:
        result["chunks"] = chunks
        if any(isinstance(chunk, dict) and (
            chunk.get("error") or chunk.get("type") in ("error", "response.failed", "response.incomplete")
        ) for chunk in chunks):
            state.error = getattr(state, "error", None) or "upstream_sse_error"
    if getattr(state, "error", None):
        result["error"] = state.error
    if getattr(state, "error_message", None):
        result["error_message"] = state.error_message
    return result


async def _prepare_request(req: Request) -> None:
    """为当前路由确定供应商、请求标识和会话标识。"""
    state = req.state
    state.sandbox_id = get_sandbox_id()
    host, port = req.scope["server"][:2]
    wildcard = "::" if ":" in host else "0.0.0.0"
    listeners = req.app.state.listeners
    state.provider, state.target = listeners.get((host, port)) or listeners[(wildcard, port)]
    state.protocol = _MODEL_PATHS.get(req.url.path, "http") if req.method == "POST" else "http"
    state.request_id = uuid4().hex
    state.started = datetime.now()
    try:
        state.raw_body = await req.body()
    except (ClientDisconnect, asyncio.CancelledError):
        state.body, state.body_kind = None, "json"
        state.session_id = _session_id(req.headers, None)
        state.error = "client_disconnected"
        _record_trajectory(req, 499, httpx.Headers())
        raise

    state.body_kind, state.body = _parse_body(state.raw_body, httpx.Headers(req.headers.raw))
    state.session_id = _session_id(req.headers, state.body)


def _record_response(req: Request, response: Response) -> Response:
    """为普通响应补上会话标识，并在返回前写入轨迹。"""
    response.headers["session-id"] = req.state.session_id
    response.headers["X-Sandbox-Id"] = req.state.sandbox_id
    _record_trajectory(req, response.status_code, httpx.Headers(response.headers.raw), response.body)
    return response


def _failure_text(response: dict) -> str:
    """提取上游失败文本；兼容 Anthropic SSE 中嵌套的 JSON 错误消息。"""
    def message(value, depth=0):
        if depth > 5:
            return ""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            return message(parsed, depth + 1) if isinstance(parsed, (dict, list)) else value
        if isinstance(value, list):
            return "; ".join(filter(None, (message(item, depth + 1) for item in value)))
        if isinstance(value, dict):
            for key in ("message", "detail", "error_message", "msg", "error", "incomplete_details", "reason"):
                text = message(value.get(key), depth + 1)
                if text:
                    return text
            return str(value.get("type") or value.get("code") or "")
        return ""

    text = message(response.get("json"))
    if not text:
        for chunk in reversed(response.get("chunks") or []):
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                text = message(chunk["error"])
            elif chunk.get("type") in ("response.failed", "response.incomplete"):
                text = message(chunk.get("response") or chunk)
            elif chunk.get("type") == "error_body":
                text = message(chunk.get("body"))
            if text:
                break
    if not text:
        text = message(response.get("error_message") or response.get("detail"))
    if not text:
        code = response.get("error")
        text = {"client_disconnected": "客户端在响应完成前断开连接",
                "incomplete_stream": "响应流未收到正常结束事件",
                "upstream_stream_error": "上游响应流传输中断"}.get(code, "")
        status = response.get("upstream_status_code", response.get("status_code"))
        if not text and status and status >= 400:
            text = response.get("text") or f"上游返回 HTTP {status}"
        text = text or str(code or "请求未成功完成")
    return " ".join(text.split())


def _record_trajectory(req: Request, status: int, headers: httpx.Headers,
                       raw: bytes | bytearray = b"") -> None:
    """由各路由收尾直接写入小时数据和索引。"""
    state = req.state
    sandbox_id = getattr(state, "sandbox_id", None) or get_sandbox_id()
    ts = state.started.strftime("%Y%m%d%H%M%S%f")[:-3]
    kind = _request_kind(req)
    elapsed_ms = max(0, int((datetime.now() - state.started).total_seconds() * 1000))
    trace = {
        "ts": ts, "id": state.request_id, "session-id": state.session_id,
        "sandbox_id": sandbox_id,
        "provider": state.provider, "protocol": state.protocol,
        "method": req.method, "path": _request_path(req),
        "headers": {**_log_headers(req.headers), "x-sandbox-id": sandbox_id},
        "req": {"base64": state.body} if state.body_kind == "base64" else state.body,
        "res": _response_record(state, status, headers, raw),
        "request_kind": kind, "duration_ms": elapsed_ms,
    }
    tok_in, tok_out = getattr(state, "tokens", (0, 0))
    success = getattr(state, "success", 200 <= status < 400)
    index = {
        "ts": ts, "id": state.request_id, "session-id": state.session_id, "provider": state.provider,
        "sandbox_id": sandbox_id,
        "model": getattr(state, "model", ""), "tok_in": tok_in, "tok_out": tok_out,
        "status_code": status, "success": success and "error" not in trace["res"],
        "file": f"data/{ts[:10]}.jsonl",
        "request_kind": kind, "duration_ms": elapsed_ms,
    }
    if hasattr(state, "total_attempts"):
        index["total_attempts"] = state.total_attempts
    if not index["success"]:
        index["error_message"] = _redact_gateway_text(req, _failure_text(trace["res"]))[:1000]
        if "upstream_status_code" in trace["res"]:
            index["upstream_status_code"] = trace["res"]["upstream_status_code"]
    try:
        _append_trajectory(trace, index)
    except OSError:
        logging.exception("写入轨迹失败（%s）", state.request_id)
    attempts = getattr(state, "total_attempts", 1)
    success = index["success"]
    level = (logging.DEBUG if kind in ("probe", "health") else
             logging.WARNING if not success or attempts > 1 else logging.INFO)
    label = "接口探测" if kind in ("probe", "health") else ("请求完成" if success else "请求失败")
    _GATEWAY_LOGGER.log(level, "[gateway provider=%s session=%s request=%s] %s kind=%s method=%s path=%s "
                "status=%s upstream=%s attempts=%s 耗时=%.2f秒 tokens=%s/%s error=%s sandbox=%s",
                state.provider, state.session_id, state.request_id, label, kind, req.method,
                _request_path(req), status, getattr(state, "upstream_status", status), attempts,
                elapsed_ms / 1000, tok_in, tok_out, index.get("error_message") or "无", sandbox_id)


def load_config(path: Path) -> list[tuple[str, str, int, str]]:
    """校验端口映射和上游基础地址，并保留配置中的原始 URL。"""
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or not config:
        raise ValueError("配置须包含至少一个供应商")
    providers = []
    addresses = set()
    for name, entry in config.items():
        try:
            local_ip = entry["local_ip"]
            if not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]):[0-9]+", local_ip):
                raise ValueError
            local = urlsplit("//" + local_ip)
            host, port = local.hostname, local.port
            target = entry["target_ip"]
            if not host or not port or local.username is not None or local.path or local.query or local.fragment:
                raise ValueError
            if not re.fullmatch(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?(?:/[A-Za-z0-9._~-]+)*", target):
                raise ValueError
            upstream = urlsplit(target)
            if not upstream.hostname or upstream.port == 0 or {".", ".."}.intersection(upstream.path.split("/")):
                raise ValueError
            if ":" not in upstream.hostname and not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in upstream.hostname.removesuffix(".").split(".")
            ):
                raise ValueError
            labels = upstream.hostname.removesuffix(".").split(".")
            if len(labels) == 4 and all(label.isdecimal() for label in labels):
                ipaddress.IPv4Address(".".join(labels))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"供应商 {name!r} 的 local_ip 或 target_ip 格式无效") from exc
        if (host, port) in addresses:
            raise ValueError(f"供应商 {name!r} 的监听地址重复：{local_ip}")
        addresses.add((host, port))
        providers.append((name, host, port, target))
    return providers


def main() -> None:
    """读取配置，绑定全部端口后启动同一个 Lite 路由应用。"""
    import argparse
    import uvicorn

    global BAN_EXPLORE, TAICHU_MAAS_COMPAT
    parser = argparse.ArgumentParser(description="启动模型 API 转发网关")
    parser.add_argument("--config", type=Path, required=True, help="proxy_config.json 路径")
    parser.add_argument("--ban_explore", action="store_true", help="去掉 Task 工具描述中的 Explore 行")
    parser.add_argument("--taichu-maas-compat", action="store_true", default=TAICHU_MAAS_COMPAT,
                        help="兼容 MaaS 的太初工具 schema 和关联 ID 限制")
    args = parser.parse_args()
    if args.ban_explore:
        BAN_EXPLORE = True
    TAICHU_MAAS_COMPAT = args.taichu_maas_compat
    os.environ["SANDBOX_ID"] = get_sandbox_id()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not _GATEWAY_LOGGER.handlers:
        TRAJECTORY_DIR.parent.mkdir(parents=True, exist_ok=True)
        detail = logging.FileHandler(TRAJECTORY_DIR.parent / "gateway.log", encoding="utf-8")
        detail.setLevel(logging.DEBUG)
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        for handler in (detail, console):
            handler.setFormatter(formatter)
            _GATEWAY_LOGGER.addHandler(handler)
        _GATEWAY_LOGGER.setLevel(logging.DEBUG)
        _GATEWAY_LOGGER.propagate = False
    try:
        providers = load_config(args.config)
        with ExitStack() as stack:
            sockets = []
            listeners = {}
            for name, host, port, target in providers:
                try:
                    family, _, _, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
                    listener = stack.enter_context(socket.socket(family, socket.SOCK_STREAM))
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    else:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    listener.bind(address)
                except OSError as exc:
                    raise OSError(f"供应商 {name!r} 无法监听 {host}:{port}：{exc}") from exc
                sockets.append(listener)
                listeners[listener.getsockname()[:2]] = (name, target)
                logging.info("%s 监听 %s:%s，转发至 %s", name, host, port, target)
            app.state.listeners = listeners
            uvicorn.Server(uvicorn.Config(app, proxy_headers=False, access_log=False)).run(sockets=sockets)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"网关启动失败：{exc}\n")


if __name__ == "__main__":
    main()
