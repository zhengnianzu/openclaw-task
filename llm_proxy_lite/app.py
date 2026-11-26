import os
import json
import httpx
import asyncio
import logging
import sys
import traceback
import socket
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from typing import Any, Dict, List, Optional, AsyncIterator
from fastapi.responses import JSONResponse, StreamingResponse, Response

from print_stats_summary import TRAJECTORY_DIR, statistic_tokens

# 控制台和重定向输出统一使用 UTF-8，兼容 Linux 和 Windows。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

load_dotenv(override=True)

# 同一网关中的所有任务共用，启动时可通过 --sandbox-id 指定。
SANDBOX_ID = uuid4().hex

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
TRUST_ENV = os.getenv("TRUST_ENV", "true").lower() == "true"

# 全局默认：重试次数（不从环境变量读取）
MAX_RETRIES = 20

app = FastAPI(title="Anthropic+OpenAI Proxy (FastAPI)")




async def get_x_auth_token(request) -> str:
    keys = ['authorization', 'x-api-key']
    for key in keys:
        ack = request.headers.get(key)
        if isinstance(ack, str) and ack.startswith('Bearer '):
            ack = ack.split('Bearer ')[1].strip()
            return ack
    return ''


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


def build_upstream_headers(x_auth_token: str, model_id: str, req: Request) -> Dict[str, str]:
    """
    你网关/后端需要的 headers 全放这。
    如果你还需要额外的（比如 X-ID / X-APPKEY），也在这里加。
    """
    ack = os.getenv('UPSTREAM_API_KEY') or x_auth_token
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ack}",
        "x-api-key": ack,
        "Model-Id": os.environ.get("MODEL_ID") or model_id,
        "X-Sandbox-Id": SANDBOX_ID,
        **{key: value for key, value in req.headers.items() if key in ("session-id", "x-session-id")},
    }


def _ssl_verify() -> bool:
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


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/hi")
async def health():
    return {"LLM_PROXY": "hello !!!"}


# ---------- Anthropic Messages ----------
def _dump_json(path: str, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "ab") as f:
        offset = f.tell()
        f.write(line)
    return offset, len(line)


def _resp_to_obj(r):  # httpx.Response -> dict
    base = {"status_code": r.status_code, "headers": dict(r.headers)}
    try:
        base["json"] = r.json()
    except Exception:
        base["text"] = r.text
    return base


def _append_index(req: Request, protocol: str, model: str, response: dict,
                  tok_in: int = 0, tok_out: int = 0, success: bool = True,
                  total_attempts: int = 1, error_message: Any = None,
                  stream_done: Optional[bool] = None):
    """将本次请求、响应写入小时 JSONL，并追加字节位置索引。"""
    state = req.state
    ts = state.started.strftime("%Y%m%d%H%M%S%f")[:-3]
    status = response.get("status_code")
    # 只对流式请求按结束标记和关闭异常分类，明确的 HTTP/SSE 错误仍优先判失败。
    stream_status = None
    if stream_done is not None:
        cancelled = isinstance(error_message, (asyncio.CancelledError, GeneratorExit))
        if (status or 0) < 400 and (error_message is None or cancelled):
            if stream_done:
                stream_status = "SUCCESS_CLIENT_CLOSED" if cancelled else "SUCCESS"
            else:
                stream_status = "STREAM_CANCELLED" if cancelled else "INCOMPLETE_STREAM"
        success = success and stream_status in ("SUCCESS", "SUCCESS_CLIENT_CLOSED")
    trace = {
        "ts": ts, "id": state.request_id,
        "sandbox_id": SANDBOX_ID,
        "provider": state.provider, "protocol": protocol,
        "method": req.method, "path": req.url.path + ("?" + req.url.query if req.url.query else ""),
        "headers": dict(req.headers), "req": state.body, "res": response,
    }
    index = {
        "ts": ts, "id": state.request_id,
        "sandbox_id": SANDBOX_ID,
        "provider": state.provider, "model": model, "tok_in": tok_in, "tok_out": tok_out,
        "status_code": status, "success": success,
        "file": f"data/{ts[:10]}.jsonl", "total_attempts": total_attempts,
    }
    if stream_status:
        index["stream_status"] = stream_status
    if not success:
        index["error_message"] = str(error_message) if error_message is not None else stream_status
        trace["error_message"] = index["error_message"]
        if isinstance(error_message, BaseException):
            index["error_type"] = type(error_message).__name__
            trace["exception"] = traceback.format_exception(error_message)
    elif stream_status == "SUCCESS_CLIENT_CLOSED":
        trace["exception"] = traceback.format_exception(error_message)
    index["offset"], index["length"] = _dump_json(TRAJECTORY_DIR / index["file"], trace)
    _dump_json(TRAJECTORY_DIR / "index.jsonl", index)


@app.post("/v1/messages")
async def anthropic_messages(req: Request):
    """anthropic透传"""
    body = await req.json()
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
    req.state.request_id = uuid4().hex
    req.state.started = datetime.now()
    req.state.body = dict(body)
    host, port = req.scope["server"][:2]
    listeners = req.app.state.listeners
    req.state.provider, target = listeners.get((host, port)) or listeners[("0.0.0.0", port)]
    upstream_url = target + req.url.path + ("?" + req.url.query if req.url.query else "")
    verify = _ssl_verify()

    x_auth_token = await get_x_auth_token(req)
    upstream_headers = build_upstream_headers(x_auth_token, model, req)
    body["model"] = upstream_headers['Model-Id']

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

        if not success:
            if r is not None:
                _append_index(req, "anthropic", model, _resp_to_obj(r), 0, 0, success=False, total_attempts=upstream_attempts, error_message=last_exception if last_exception is not None else r.text)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                _append_index(req, "anthropic", model, {"error": "max_retries_exceeded", "detail": error_msg}, 0, 0, success=False, total_attempts=upstream_attempts, error_message=last_exception)
                return JSONResponse(
                    status_code=502,
                    content={"type": "error", "error": {"type": "max_retries_exceeded", "message": f"上游多次失败({MAX_RETRIES}次): {error_msg}"}},
                )

        tok_in, tok_out = 0, 0
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            tok_in = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
            tok_out = usage.get("output_tokens") or 0
        except Exception:
            pass
        _append_index(req, "anthropic", model, _resp_to_obj(r), tok_in, tok_out, success=True, total_attempts=upstream_attempts)
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (pure pass-through) ----
    async def anthropic_sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        log_buf = bytearray()
        connection_established = False
        upstream_status = None
        log_error = None
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
                    upstream_status = None
                    log_error = None
                    log_buf = bytearray()
                    try:
                        async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                            upstream_status = r.status_code
                            up_chunks.append({
                                "type": "anthropic_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": dict(r.headers),
                            })

                            if is_rate_limit_status(r.status_code):
                                err = await r.aread()
                                last_retry_err_text = err.decode("utf-8", errors="replace")
                                last_retry_status = r.status_code
                                log_error = last_retry_err_text
                                up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                                logging.warning(f"Attempt {attempt} rate limit (anthropic stream): {r.status_code}")
                                if attempt < MAX_RETRIES - 1:
                                    await asyncio.sleep(0.5)
                                    retry_token = await get_x_auth_token(req)
                                    retry_headers = build_upstream_headers(retry_token, model, req)
                                continue

                            connection_established = True

                            if r.status_code >= 400:
                                err = await r.aread()
                                err_text = err.decode("utf-8", errors="replace")
                                log_error = err_text
                                up_chunks.append({"type": "error_body", "body": err_text})
                                error_data = {"type": "error", "error": {"type": "api_error", "message": err_text}}
                                yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
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

                            return

                    except Exception as e:
                        log_error = e
                        if connection_established:
                            logging.warning(f"Stream interrupted (anthropic stream): {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (anthropic stream): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            retry_token = await get_x_auth_token(req)
                            retry_headers = build_upstream_headers(retry_token, model, req)

                # All retries exhausted
                error_msg = str(last_exception) if last_exception else (f"HTTP {last_retry_status}" if last_retry_status else "unknown")
                logging.error(f"All retries exhausted (anthropic stream): {error_msg}")
                err_event = {"type": "error", "error": {"type": "max_retries_exceeded", "message": f"上游多次失败({MAX_RETRIES}次): {error_msg}"}}
                yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")

        except Exception as e:
            logging.error(f"Failed to create httpx client (anthropic stream): {e}")
            last_exception = e
            log_error = e
            err_event = {"type": "error", "error": {"type": "connection_error", "message": str(e)}}
            yield f"event: error\ndata: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")
        finally:
            log_error = sys.exc_info()[1] or log_error

            # 按完整 SSE 事件合并多行 data；LF、CRLF、CR 均支持，末尾半条事件不解析。
            sse_text = log_buf.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            for event in sse_text.split("\n\n")[:-1]:
                data_part = "\n".join(line[5:].removeprefix(" ") for line in event.split("\n")
                                      if line == "data" or line.startswith("data:"))
                if data_part and data_part != "[DONE]":
                    try:
                        chunk = json.loads(data_part)
                        up_chunks.append(chunk)
                        if isinstance(chunk, dict) and chunk.get("type") == "error":
                            log_error = data_part
                    except json.JSONDecodeError:
                        log_error = data_part

            # 同次遍历读取上游 usage 和 Anthropic 的 message_stop，不估算 Token。
            stream_done = False
            _tok_in, _tok_out = 0, 0
            for _c in up_chunks:
                if isinstance(_c, dict):
                    stream_done = stream_done or _c.get("type") == "message_stop"
                    _u = _c.get("message", {}).get("usage") or _c.get("usage") or {}
                    if _u:
                        _tok_in = ((_u.get("input_tokens") or 0) + (_u.get("cache_creation_input_tokens") or 0) + (_u.get("cache_read_input_tokens") or 0)) or _tok_in
                        _tok_out = _u.get("output_tokens") or _tok_out

            # 统一分类时保留关闭异常原文；成功与否由协议完成状态决定。
            _append_index(req, "anthropic", model, {
                "type": "anthropic_passthrough_sse_capture", "chunks": up_chunks,
                "headers": {"content-type": "text/event-stream"},
                "status_code": upstream_status,
            }, _tok_in, _tok_out, success=connection_established,
                total_attempts=upstream_attempts, error_message=log_error, stream_done=stream_done)

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
    OpenAI-compatible endpoint:
      - non-stream: upstream JSON pass-through
      - stream: upstream OpenAI SSE pass-through
    """
    body = await req.json()
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

    req.state.request_id = uuid4().hex
    req.state.started = datetime.now()
    req.state.body = dict(body)
    host, port = req.scope["server"][:2]
    listeners = req.app.state.listeners
    req.state.provider, target = listeners.get((host, port)) or listeners[("0.0.0.0", port)]
    upstream_url = target + req.url.path + ("?" + req.url.query if req.url.query else "")
    verify = _ssl_verify()

    x_auth_token = await get_x_auth_token(req)
    upstream_headers = build_upstream_headers(x_auth_token, model, req)
    body["model"] = upstream_headers['Model-Id']
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

        if not success:
            if r is not None:
                _append_index(req, "openai", model, _resp_to_obj(r), 0, 0, success=False, total_attempts=upstream_attempts, error_message=last_exception if last_exception is not None else r.text)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )
            else:
                error_msg = str(last_exception) if last_exception else "unknown"
                _append_index(req, "openai", model, {"error": "max_retries_exceeded", "detail": error_msg}, 0, 0, success=False, total_attempts=upstream_attempts, error_message=last_exception)
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": f"\u4e0a\u6e38\u591a\u6b21\u5931\u8d25({MAX_RETRIES}\u6b21): {error_msg}", "type": "max_retries_exceeded"}},
                )

        tok_in, tok_out = 0, 0
        try:
            resp_json = r.json()
            usage = resp_json.get("usage", {})
            tok_in = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            tok_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        except Exception:
            pass
        _append_index(req, "openai", model, _resp_to_obj(r), tok_in, tok_out, success=r.status_code < 400, total_attempts=upstream_attempts)
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (OpenAI SSE pass-through) ----
    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        log_buf = bytearray()
        is_responses = req.url.path.endswith("/responses")
        connection_established = False
        upstream_status = None
        log_error = None
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
                    upstream_status = None
                    log_error = None
                    log_buf = bytearray()
                    try:
                        async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                            upstream_status = r.status_code
                            up_chunks.append({
                                "type": "openai_passthrough_sse_meta",
                                "status_code": r.status_code,
                                "headers": dict(r.headers),
                            })

                            if is_rate_limit_status(r.status_code):
                                err = await r.aread()
                                last_retry_err_text = err.decode("utf-8", errors="replace")
                                last_retry_status = r.status_code
                                log_error = last_retry_err_text
                                up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                                logging.warning(f"Attempt {attempt} rate limit (openai stream): {r.status_code}")
                                if attempt < MAX_RETRIES - 1:
                                    await asyncio.sleep(0.5)
                                    retry_token = await get_x_auth_token(req)
                                    retry_headers = build_upstream_headers(retry_token, model, req)
                                continue

                            connection_established = True

                            if r.status_code >= 400:
                                err = await r.aread()
                                err_text = err.decode("utf-8", errors="replace")
                                log_error = err_text
                                up_chunks.append({"type": "error_body", "body": err_text})
                                yield err_text.encode("utf-8")
                                return

                            async for raw in r.aiter_bytes():
                                log_buf.extend(raw)
                                yield raw

                            return

                    except Exception as e:
                        log_error = e
                        if connection_established:
                            logging.warning(f"Stream interrupted (openai stream): {e}")
                            return
                        last_exception = e
                        logging.warning(f"Attempt {attempt} upstream error (openai stream): {e}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(0.5)
                            retry_token = await get_x_auth_token(req)
                            retry_headers = build_upstream_headers(retry_token, model, req)

                # All retries exhausted
                error_msg = str(last_exception) if last_exception else (f"HTTP {last_retry_status}" if last_retry_status else "unknown")
                logging.error(f"All retries exhausted (openai stream): {error_msg}")
                error_data = {"error": {"message": f"\u4e0a\u6e38\u591a\u6b21\u5931\u8d25({MAX_RETRIES}\u6b21): {error_msg}", "type": "max_retries_exceeded"}}
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        except Exception as e:
            logging.error(f"Failed to create httpx client (openai stream): {e}")
            last_exception = e
            log_error = e
            error_data = {"error": {"message": str(e), "type": "connection_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            log_error = sys.exc_info()[1] or log_error

            # 按完整事件合并 data；Chat 用 [DONE]，Responses 用 response.completed。
            sse_text = log_buf.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            stream_done = False
            for event in sse_text.split("\n\n")[:-1]:
                data_part = "\n".join(line[5:].removeprefix(" ") for line in event.split("\n")
                                      if line == "data" or line.startswith("data:"))
                if data_part == "[DONE]":
                    if not is_responses:
                        stream_done = True
                elif data_part:
                    try:
                        chunk = json.loads(data_part)
                        up_chunks.append(chunk)
                        if isinstance(chunk, dict):
                            if is_responses and chunk.get("type") == "response.completed":
                                stream_done = True
                            if chunk.get("error") or chunk.get("type") in ("error", "response.failed", "response.incomplete"):
                                log_error = data_part
                    except json.JSONDecodeError:
                        log_error = data_part

            # 只读取上游已返回的 usage；客户端关闭时也保留已收到的 Token 用量。
            _tok_in, _tok_out = 0, 0
            for _c in up_chunks:
                if isinstance(_c, dict):
                    _u = _c.get("usage") or (_c.get("response") or {}).get("usage") or {}
                    if _u:
                        _tok_in = _tok_in or (_u.get("input_tokens") or _u.get("prompt_tokens") or 0)
                        _tok_out = _tok_out or (_u.get("output_tokens") or _u.get("completion_tokens") or 0)

            # 统一分类，正常收尾取消也保留原始诊断信息。
            _append_index(req, "openai", model, {
                "type": "openai_passthrough_sse_capture", "chunks": up_chunks,
                "headers": {"content-type": "text/event-stream"},
                "status_code": upstream_status,
            }, _tok_in, _tok_out, success=connection_established,
                total_attempts=upstream_attempts, error_message=log_error, stream_done=stream_done)

    return StreamingResponse(
        sse_passthrough(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ===============================================
# 以下为新增的统计功能

@app.get("/statistic")
def statistic_tokens_web(model: str = '', date_start: str = '', date_end: str = '', status: str = '全部'):
    res = statistic_tokens(model=model, date_start=date_start, date_end=date_end, status=status)
    return JSONResponse(res)


def load_config(path: Path) -> list[tuple[str, str, int, str]]:
    """读取端口映射并检查上游基础地址。"""
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or not config:
        raise ValueError("配置须包含至少一个供应商")
    providers = []
    addresses = set()
    for name, entry in config.items():
        try:
            local_ip = entry["local_ip"]
            local = urlsplit("//" + local_ip)
            host, port = local.hostname, local.port
            target = entry["target_ip"]
            if (not isinstance(target, str) or not host or not port or ":" in host
                    or local.username is not None or local.path or local.query or local.fragment):
                raise ValueError
            target = target.rstrip("/")
            upstream = urlsplit(target)
            if (upstream.scheme not in ("http", "https") or not upstream.hostname or upstream.port == 0
                    or upstream.username is not None or upstream.query or upstream.fragment):
                raise ValueError
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

    global BAN_EXPLORE, SANDBOX_ID
    parser = argparse.ArgumentParser(description="启动模型 API 转发网关")
    parser.add_argument("--config", type=Path, required=True, help="proxy_config.json 路径")
    parser.add_argument("--ban_explore", action="store_true", help="去掉 Task 工具描述中的 Explore 行")
    parser.add_argument("--sandbox-id", default=SANDBOX_ID, help="沙箱 ID，未指定时自动生成，同一网关共用")
    args = parser.parse_args()
    SANDBOX_ID = args.sandbox_id
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
                    family, _, _, _, address = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)[0]
                    listener = stack.enter_context(socket.socket(family, socket.SOCK_STREAM))
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    else:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(address)
                except OSError as exc:
                    raise OSError(f"供应商 {name!r} 无法监听 {host}:{port}：{exc}") from exc
                sockets.append(listener)
                listeners[listener.getsockname()[:2]] = (name, target)
                logging.info("%s 监听 %s:%s，转发至 %s", name, host, port, target)
            app.state.listeners = listeners
            uvicorn.Server(uvicorn.Config(app, proxy_headers=False)).run(sockets=sockets)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"网关启动失败：{exc}\n")


if __name__ == "__main__":
    main()
