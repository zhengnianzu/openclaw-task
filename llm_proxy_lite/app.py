import re
import os
import json
import httpx
import asyncio
import logging
import base64
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
from typing import Any, Dict, List, Optional, AsyncIterator
from fastapi.responses import JSONResponse, StreamingResponse, Response

from starlette.requests import ClientDisconnect

from print_stats_summary import TRAJECTORY_DIR, statistic_tokens

load_dotenv(override=True)

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
TRUST_ENV = os.getenv("TRUST_ENV", "true").lower() == "true"

# 全局默认：重试次数（不从环境变量读取）
MAX_RETRIES = 20

# 转发、解析及重试基于 llm-proxy-lite 的 app.py（f88838f）。
app = FastAPI(title="模型 API 转发网关")
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
            "x-api-key", "model-id", "session-id", "accept-encoding",
        ))),
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ack}",
        "x-api-key": ack,
        "Model-Id": os.environ.get("MODEL_ID") or model_id,
        "session-id": req.state.session_id,
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
    logging.warning("供应商 %s 转发失败（%s）: %s", req.state.provider, req.state.request_id, type(exc).__name__)
    return 504 if timeout else 502


async def _stream_response(req: Request, source: AsyncIterator[bytes]) -> StreamingResponse:
    """等待通用转发拿到上游状态和首块，再返回响应。"""
    req.state.stream = source
    first = await anext(source, None)

    async def body():
        """先回传已读取的首块，再逐块返回余下内容。"""
        if first is not None:
            yield first
        async for chunk in source:
            yield chunk

    response_headers = getattr(req.state, "response_headers", None)
    return StreamingResponse(
        body(), status_code=req.state.status,
        media_type="text/event-stream" if response_headers is None else None,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"} if response_headers is None else response_headers,
    )


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/hi")
async def health():
    """返回 Lite 的本地健康检查响应。"""
    return {"LLM_PROXY": "hello !!!"}


@app.post("/v1/messages")
async def anthropic_messages(req: Request):
    """按 Lite 的普通和 SSE 分支转发 Anthropic Messages。"""
    try:
        body = await req.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": {"type": "invalid_json"}}, status_code=400)
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
                        logging.warning(f"Attempt {attempt} non-200 (anthropic non-stream): {r.status_code} {r.text[:200]}")
                    except Exception as e:
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (anthropic non-stream): {e}")

                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(0.5)
                        x_auth_token = await get_x_auth_token(req)
                        upstream_headers = build_upstream_headers(x_auth_token, model, req)
        except Exception as e:
            last_exception = e
            logging.error(f"Failed to create httpx client (anthropic non-stream): {e}")

        req.state.success = success
        req.state.total_attempts = upstream_attempts
        if not success:
            if r is not None:
                req.state.parsed_response = _resp_to_obj(r)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                req.state.parsed_response = {"error": "max_retries_exceeded", "detail": error_msg}
                return JSONResponse(
                    status_code=502,
                    content={"type": "error", "error": {"type": "max_retries_exceeded", "message": f"上游多次失败({MAX_RETRIES}次): {error_msg}"}},
                )

        req.state.parsed_response = _resp_to_obj(r)
        tok_in, tok_out = 0, 0
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            tok_in = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
            tok_out = usage.get("output_tokens") or 0
        except Exception:
            pass
        req.state.tokens = tok_in, tok_out
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (pure pass-through) ----
    async def anthropic_sse_passthrough() -> AsyncIterator[bytes]:
        """沿用 Lite 的 SSE 透传和重试循环。"""
        up_chunks: List[Any] = []
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
                                logging.warning(f"Attempt {attempt} rate limit (anthropic stream): {r.status_code}")
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
                                yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                                return

                            log_buf = bytearray()
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
                                    logging.warning(f"Attempt {attempt} no message_start in SSE (anthropic stream), retrying")
                                    connection_established = False
                                    up_chunks.clear()
                                    await asyncio.sleep(0.5)
                                    retry_token = await get_x_auth_token(req)
                                    retry_headers = build_upstream_headers(retry_token, model, req)
                                    continue
                                else:
                                    connection_established = True
                                    yield bytes(raw_buf)

                            for line in log_buf.decode("utf-8", errors="replace").splitlines():
                                if line.startswith("data:"):
                                    data_part = line[5:].strip()
                                    if data_part and data_part != "[DONE]":
                                        try:
                                            up_chunks.append(json.loads(data_part))
                                        except json.JSONDecodeError:
                                            pass
                            return

                    except Exception as e:
                        if connection_established:
                            req.state.error = "upstream_stream_error"
                            logging.warning(f"Stream interrupted (anthropic stream): {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (anthropic stream): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            retry_token = await get_x_auth_token(req)
                            retry_headers = build_upstream_headers(retry_token, model, req)

                # All retries exhausted
                req.state.error = "max_retries_exceeded"
                error_msg = str(last_exception) if last_exception else (f"HTTP {last_retry_status}" if last_retry_status else "unknown")
                logging.error(f"All retries exhausted (anthropic stream): {error_msg}")
                err_event = {"type": "error", "error": {"type": "max_retries_exceeded", "message": f"上游多次失败({MAX_RETRIES}次): {error_msg}"}}
                yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")

        except Exception as e:
            req.state.error = "connection_error"
            logging.error(f"Failed to create httpx client (anthropic stream): {e}")
            err_event = {"type": "error", "error": {"type": "connection_error", "message": str(e)}}
            yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")
        finally:
            req.state.chunks = up_chunks
            req.state.success = connection_established
            req.state.total_attempts = upstream_attempts
            _tok_in, _tok_out = 0, 0
            for _c in up_chunks:
                if isinstance(_c, dict):
                    _u = _c.get("message", {}).get("usage") or _c.get("usage") or {}
                    if _u:
                        _tok_in = (_u.get("input_tokens") or 0) + (_u.get("cache_creation_input_tokens") or 0) + (_u.get("cache_read_input_tokens") or 0)
                        _tok_out = _u.get("output_tokens") or 0
            req.state.tokens = _tok_in, _tok_out

    return StreamingResponse(
        anthropic_sse_passthrough(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- OpenAI Chat Completions ----------
@app.post("/v1/responses")
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def openai_chat_completions(req: Request):
    """
    按 Lite 的普通和 SSE 分支转发 OpenAI Chat Completions 和 Responses。
    """
    try:
        body = await req.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": {"type": "invalid_json"}}, status_code=400)
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
                        logging.warning(f"Attempt {attempt} non-200 (openai non-stream): {r.status_code} {r.text[:200]}")
                    except Exception as e:
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (openai non-stream): {e}")

                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(0.5)
                        x_auth_token = await get_x_auth_token(req)
                        upstream_headers = build_upstream_headers(x_auth_token, model, req)
        except Exception as e:
            last_exception = e
            logging.error(f"Failed to create httpx client (openai non-stream): {e}")

        req.state.success = success
        req.state.total_attempts = upstream_attempts
        if not success:
            if r is not None:
                req.state.parsed_response = _resp_to_obj(r)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                req.state.parsed_response = {"error": "max_retries_exceeded", "detail": error_msg}
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": f"\u4e0a\u6e38\u591a\u6b21\u5931\u8d25({MAX_RETRIES}\u6b21): {error_msg}", "type": "max_retries_exceeded"}},
                )

        req.state.parsed_response = _resp_to_obj(r)
        tok_in, tok_out = 0, 0
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            tok_in = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            tok_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        except Exception:
            pass
        req.state.tokens = tok_in, tok_out
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (OpenAI SSE pass-through) ----
    async def sse_passthrough() -> AsyncIterator[bytes]:
        """沿用 Lite 的 SSE 透传和重试循环。"""
        up_chunks: List[Any] = []
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
                                logging.warning(f"Attempt {attempt} rate limit (openai stream): {r.status_code}")
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
                                yield err_text.encode("utf-8")
                                return

                            log_buf = bytearray()
                            async for raw in r.aiter_bytes():
                                log_buf.extend(raw)
                                yield raw

                            # Parse log_buf for logging
                            for line in log_buf.decode("utf-8", errors="replace").splitlines():
                                if line.startswith("data:"):
                                    data_part = line[5:].strip()
                                    if data_part and data_part != "[DONE]":
                                        try:
                                            up_chunks.append(json.loads(data_part))
                                        except json.JSONDecodeError:
                                            pass
                            return

                    except Exception as e:
                        if connection_established:
                            req.state.error = "upstream_stream_error"
                            logging.warning(f"Stream interrupted (openai stream): {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (openai stream): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            retry_token = await get_x_auth_token(req)
                            retry_headers = build_upstream_headers(retry_token, model, req)

                # All retries exhausted
                req.state.error = "max_retries_exceeded"
                error_msg = str(last_exception) if last_exception else (f"HTTP {last_retry_status}" if last_retry_status else "unknown")
                logging.error(f"All retries exhausted (openai stream): {error_msg}")
                error_data = {"error": {"message": f"\u4e0a\u6e38\u591a\u6b21\u5931\u8d25({MAX_RETRIES}\u6b21): {error_msg}", "type": "max_retries_exceeded"}}
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        except Exception as e:
            req.state.error = "connection_error"
            logging.error(f"Failed to create httpx client (openai stream): {e}")
            error_data = {"error": {"message": str(e), "type": "connection_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            req.state.chunks = up_chunks
            req.state.success = connection_established
            req.state.total_attempts = upstream_attempts
            _tok_in, _tok_out = 0, 0
            for _c in up_chunks:
                if isinstance(_c, dict):
                    _u = _c.get("usage") or {}
                    response = _c.get("response")
                    if req.state.protocol == "responses" and isinstance(response, dict):
                        _u = response.get("usage") or _u
                    if _u:
                        _tok_in = _tok_in or (_u.get("input_tokens") or _u.get("prompt_tokens") or 0)
                        _tok_out = _tok_out or (_u.get("output_tokens") or _u.get("completion_tokens") or 0)
            req.state.tokens = _tok_in, _tok_out

    return StreamingResponse(
        sse_passthrough(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/statistic")
def statistic_tokens_web(model: str = '', date_start: str = '', date_end: str = '', status: str = '全部'):
    """沿用 Lite 的 JSON 统计入口。"""
    res = statistic_tokens(model=model, date_start=date_start, date_end=date_end, status=status)
    return JSONResponse(res)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def passthrough(req: Request):
    """将其他 HTTP 请求直接发往当前监听地址对应的供应商。"""
    headers = dict(_forward_headers(req.headers, ("host", "content-length")))
    headers["session-id"] = req.state.session_id
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

    return await _stream_response(req, forward())


def _log_headers(headers) -> dict:
    """仅在日志中隐藏鉴权头和 Cookie 的值。"""
    return {name: "[已隐藏]" if name.lower() in _SECRET_HEADERS else value for name, value in headers.items()}


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


def _response_record(state, status: int, headers: httpx.Headers, raw: bytearray) -> dict:
    """合并路由解析结果与实际下发的响应，补充失败状态。"""
    result = getattr(state, "parsed_response", None) or {"headers": _log_headers(headers)}
    result.update(status_code=status, **{"session-id": state.session_id})
    upstream_status = getattr(state, "upstream_status", status)
    if upstream_status != status:
        result["upstream_status_code"] = upstream_status
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
    return result


class Gateway:
    """按监听端口选择上游，并记录 FastAPI 路由处理的每条 HTTP 请求。"""

    def __init__(self, routes: FastAPI, listeners: dict[tuple[str, int], tuple[str, str]]):
        """绑定路由应用和由本地监听地址查找的供应商配置。"""
        self.routes = routes
        self.listeners = listeners

    async def __call__(self, scope, receive, send):
        """在转发前补齐会话标识，并在请求退出时记录请求和响应。"""
        if scope["type"] != "http":
            await self.routes(scope, receive, send)
            return
        req = Request(scope, receive)
        state = req.state
        host, port = scope["server"][:2]
        wildcard = "::" if ":" in host else "0.0.0.0"
        state.provider, state.target = self.listeners.get((host, port)) or self.listeners[(wildcard, port)]
        state.protocol = _MODEL_PATHS.get(req.url.path, "http") if req.method == "POST" else "http"
        state.request_id = uuid4().hex
        started = datetime.now()
        response_headers = httpx.Headers()
        response_body = bytearray()
        status = 499
        complete = False
        replayed = False
        state.raw_body = b""
        state.body = None

        async def replay():
            """重放已读取的请求体，随后接收下游断开通知。"""
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": state.raw_body, "more_body": False}
            return await receive()

        async def capture(message):
            """在响应头回传同一会话标识，并同步收集实际发送的响应。"""
            nonlocal status, response_headers, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                state.response_started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"session-id"]
                headers.append((b"session-id", state.session_id.encode("ascii")))
                response_headers = httpx.Headers(headers)
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                complete = not message.get("more_body", False)
            await send(message)

        try:
            state.raw_body = await req.body()
            state.body_kind, state.body = _parse_body(state.raw_body, httpx.Headers(req.headers.raw))
            state.session_id = _session_id(req.headers, state.body)
            await self.routes(scope, replay, capture)
        except (ClientDisconnect, asyncio.CancelledError):
            state.error = "client_disconnected"
            raise
        except Exception:
            state.error = getattr(state, "error", None) or "gateway_error"
            raise
        finally:
            if not hasattr(state, "session_id"):
                state.session_id = _session_id(req.headers, state.body)
            stream = getattr(state, "stream", None)
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    state.error = getattr(state, "error", None) or "upstream_stream_error"
            if not complete:
                state.error = getattr(state, "error", None) or "client_disconnected"
            ts = started.strftime("%Y%m%d%H%M%S%f")[:-3]
            trace = {
                "ts": ts, "id": state.request_id, "session-id": state.session_id,
                "provider": state.provider, "protocol": state.protocol,
                "method": req.method, "path": _request_path(req),
                "headers": _log_headers(req.headers),
                "req": {"base64": state.body} if getattr(state, "body_kind", None) == "base64" else state.body,
                "res": _response_record(state, status, response_headers, response_body),
            }
            tok_in, tok_out = getattr(state, "tokens", (0, 0))
            success = getattr(state, "success", 200 <= status < 400)
            index = {
                "ts": ts, "id": state.request_id, "session-id": state.session_id, "provider": state.provider,
                "model": getattr(state, "model", ""), "tok_in": tok_in, "tok_out": tok_out,
                "status_code": status, "success": success and "error" not in trace["res"],
                "file": f"data/{ts[:10]}.jsonl",
            }
            if hasattr(state, "total_attempts"):
                index["total_attempts"] = state.total_attempts
            try:
                await asyncio.to_thread(_append_trajectory, trace, index)
            except OSError:
                logging.exception("写入轨迹失败（%s）", state.request_id)


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

    global BAN_EXPLORE
    parser = argparse.ArgumentParser(description="启动模型 API 转发网关")
    parser.add_argument("--config", type=Path, required=True, help="proxy_config.json 路径")
    parser.add_argument("--ban_explore", action="store_true", help="去掉 Task 工具描述中的 Explore 行")
    args = parser.parse_args()
    if args.ban_explore:
        BAN_EXPLORE = True
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
            gateway = Gateway(app, listeners)
            uvicorn.Server(uvicorn.Config(gateway, proxy_headers=False)).run(sockets=sockets)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"网关启动失败：{exc}\n")


if __name__ == "__main__":
    main()
