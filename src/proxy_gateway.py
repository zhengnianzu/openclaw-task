from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import logging
import math
import re
import socket
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn
from fastapi import Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from .llm_proxy.app import app
from .llm_proxy.http_client import close_all

_TARGET_URL = re.compile(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?(?:/[A-Za-z0-9._~-]+)*")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_CREDENTIAL_HEADERS = {
    "authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie",
}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provider:
    """一条供应商配置对应的本地监听地址和上游地址。"""

    name: str
    host: str
    port: int
    target: str


def load_config(path: Path) -> list[Provider]:
    """读取供应商配置，校验 URL、端口及监听地址的唯一性。"""
    with path.open(encoding="utf-8-sig") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict) or not config:
        raise ValueError("配置须包含至少一个供应商")

    providers = []
    listeners = set()
    for name, entry in config.items():
        try:
            local = urlsplit(f"//{entry['local_ip'].strip()}")
            if (
                not local.hostname or not local.port or local.path or local.query
                or local.fragment or local.username is not None
            ):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"供应商 {name!r} 的 local_ip 必须为主机和端口") from exc
        try:
            target = entry["target_ip"]
            if not _TARGET_URL.fullmatch(target):
                raise ValueError
            origin = urlsplit(target)
            host = origin.hostname
            if origin.port == 0 or not host or {".", ".."}.intersection(origin.path.split("/")):
                raise ValueError
            if ":" not in host:
                labels = host.removesuffix(".").split(".")
                if not all(_HOST_LABEL.fullmatch(label) for label in labels):
                    raise ValueError
                if len(labels) == 4 and all(label.isdecimal() for label in labels):
                    ipaddress.IPv4Address(".".join(labels))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"供应商 {name!r} 的 target_ip 必须为 http:// 或 https:// 开头的上游基础地址") from exc
        listener = (local.hostname, local.port)
        if listener in listeners:
            raise ValueError(f"监听地址重复：{entry['local_ip']}")
        listeners.add(listener)
        providers.append(Provider(name, *listener, target))
    return providers


def _decode_body(raw: bytes, headers: httpx.Headers) -> bytes:
    """按内容编码解压一份副本，供请求或响应解析。"""
    return httpx.Response(200, headers=headers, content=raw).content


def _parse_body(raw: bytes) -> tuple[str, object]:
    """按 JSON、UTF-8 文本、Base64 的顺序解析请求体或响应体。"""
    if not raw:
        return "json", None
    try:
        return "json", json.loads(raw)
    except (ValueError, RecursionError):
        try:
            return "text", raw.decode("utf-8")
        except UnicodeDecodeError:
            return "base64", base64.b64encode(raw).decode("ascii")


def _log_headers(headers) -> dict:
    """屏蔽日志中的鉴权头和 Cookie，实际转发的请求头仍由原路由处理。"""
    return {
        name: "[已隐藏]" if name.lower() in _CREDENTIAL_HEADERS else value
        for name, value in headers.items()
    }


def _session_id(headers, body) -> str:
    """按优先级提取可写入 HTTP 头的会话标识，缺失时生成 UUID。"""
    candidates = [headers.get("session-id"), headers.get("x-session-id")]
    if isinstance(body, dict):
        fields = ("session-id", "session_id", "sessionId")
        candidates.extend(body.get(field) for field in fields)
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            candidates.extend(metadata.get(field) for field in fields)
            user_id = metadata.get("user_id")
            if isinstance(user_id, str):
                try:
                    user_id = json.loads(user_id)
                except (ValueError, RecursionError):
                    pass
            if isinstance(user_id, str):
                match = re.search(r"session_([A-Za-z0-9-]+)", user_id)
                if match:
                    candidates.append(match.group(1))
            if isinstance(user_id, dict):
                candidates.extend(user_id.get(field) for field in fields)
    for value in candidates:
        if isinstance(value, str):
            value = value.strip()
            if value and value.isascii() and value.isprintable():
                return value
    return uuid4().hex


class TrajectoryLog:
    def __init__(self, root: Path) -> None:
        """建立轨迹数据目录和索引文件。"""
        self.root = root
        self.index = root / "index.jsonl"
        (root / "data").mkdir(parents=True, exist_ok=True)
        self.index.touch(exist_ok=True)

    @staticmethod
    def _line(value: dict) -> bytes:
        """将一条记录编码为带换行的 UTF-8 JSON，便于计算字节位置。"""
        try:
            line = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except ValueError:
            line = json.dumps(_finite_json(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return (line + "\n").encode(
            "utf-8", errors="backslashreplace",
        )

    def append(self, trace: dict, status: int, success: bool) -> None:
        """先追加小时数据，再将该行的偏移和长度写入索引。"""
        relative_path = f"data/{trace['ts'][:10]}.jsonl"
        line = self._line(trace)
        index = {
            "ts": trace["ts"], "id": trace["id"], "session-id": trace["session-id"],
            "provider": trace["provider"], "status_code": status, "success": success,
            "file": relative_path, "length": len(line),
        }
        data_path = self.root / relative_path
        # 运行期间清空日志目录后，下一条请求仍能写入数据和索引。
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("ab") as data:
            index["offset"] = data.tell()
            data.write(line)
        with self.index.open("ab") as output:
            output.write(self._line(index))


def _finite_json(value):
    """递归将非有限浮点数转换为字符串，确保日志符合 JSON 格式。"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    return value


_PROTOCOLS = {
    "/v1/messages": "anthropic",
    "/v1/chat/completions": "openai_chat",
    "/chat/completions": "openai_chat",
    "/v1/responses": "openai_responses",
}


def _result(request: Request, status: int, headers: httpx.Headers, raw: bytearray) -> dict:
    """合并上游解析结果、客户端收到的内容和失败状态，生成响应轨迹。"""
    state = request.state
    upstream_status = getattr(state, "proxy_upstream_status", None)
    result = getattr(state, "proxy_parsed", None) or {"headers": dict(headers)}
    result["headers"] = _log_headers(result["headers"])
    result["status_code"] = status
    if upstream_status is not None and upstream_status != status:
        result["upstream_status_code"] = upstream_status
    error = getattr(state, "proxy_error", None)
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/event-stream":
        try:
            result["text"] = _decode_body(bytes(raw), headers).decode("utf-8", errors="replace")
        except httpx.DecodingError:
            result["base64"] = base64.b64encode(raw).decode("ascii")
            error = error or "upstream_decode_error"
        chunks = getattr(state, "proxy_chunks", [])
        # 上游 SSE 处理函数会把响应头放在首个元信息块中。
        if chunks and isinstance(chunks[0], dict) and isinstance(chunks[0].get("headers"), dict):
            chunks[0]["headers"] = _log_headers(chunks[0]["headers"])
        result["chunks"] = chunks
        if any(line.strip() == "event: error" for line in result.get("text", "").splitlines()) or any(
            isinstance(chunk, dict)
            and (chunk.get("type") in ("error", "response.failed", "response.incomplete") or chunk.get("error"))
            for chunk in chunks
        ):
            error = error or "upstream_sse_error"
    elif "json" not in result and "text" not in result:
        try:
            kind, parsed = _parse_body(_decode_body(bytes(raw), headers))
            result[kind] = parsed
        except httpx.DecodingError:
            result["base64"] = base64.b64encode(raw).decode("ascii")
            error = error or "upstream_decode_error"
    usage = getattr(state, "proxy_usage", None)
    if isinstance(usage, dict) and usage:
        result["usage"] = usage
    if error:
        result["error"] = error
    return result


class Gateway:
    def __init__(self, listeners: dict[tuple[str, int], Provider], logs: TrajectoryLog):
        """保存监听地址映射，并串行写入共享的轨迹文件。"""
        self.listeners = listeners
        self.logs = logs
        self.log_lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """选定供应商和会话，把 HTTP 请求交给复用的路由并记录轨迹。"""
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        host, port = scope["server"][:2]
        wildcard = "::" if ":" in host else "0.0.0.0"
        provider = self.listeners.get((host, port)) or self.listeners[(wildcard, port)]
        request = Request(scope, receive)
        protocol = _PROTOCOLS.get(request.url.path, "http") if request.method == "POST" else "http"
        trace = {
            "ts": datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3],
            "id": uuid4().hex,
            "provider": provider.name,
            "protocol": protocol,
            "method": request.method,
            "path": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            "headers": _log_headers(request.headers),
            "req": None,
        }
        request.state.proxy_target = provider.target
        session_id = None
        status = None
        response_headers = httpx.Headers()
        response_body = bytearray()
        downstream_complete = False
        request_sent = False
        raw_body = b""

        def get_session_id() -> str:
            """首次需要时确定会话标识，后续响应和落盘沿用同一个值。"""
            nonlocal session_id
            if session_id is None:
                session_id = _session_id(request.headers, trace["req"])
            return session_id

        async def replay_request():
            """向上游路由重放已读取的请求体，随后继续接收断开通知。"""
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return await receive()

        async def capture_response(message):
            """写入会话响应头，转发响应消息并保留一份轨迹副本。"""
            nonlocal status, response_headers, downstream_complete
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"session-id"]
                headers.append((b"session-id", get_session_id().encode("ascii")))
                response_headers = httpx.Headers(headers)
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                downstream_complete = not message.get("more_body", False)
            await send(message)

        try:
            raw_body = await request.body()
            incoming = httpx.Headers(scope["headers"])
            try:
                decoded = _decode_body(raw_body, incoming)
            except httpx.DecodingError:
                decoded = raw_body
            kind, parsed = _parse_body(decoded)
            trace["req"] = {"base64": parsed} if kind == "base64" else parsed
            request.state.proxy_body = parsed
            request.state.proxy_session = get_session_id()
            if protocol != "http" and not isinstance(parsed, dict):
                request.state.proxy_error = "invalid_json"
                await JSONResponse({"error": {"type": "invalid_json"}}, status_code=400)(
                    scope, replay_request, capture_response,
                )
            else:
                await app(scope, replay_request, capture_response)
        except (ClientDisconnect, asyncio.CancelledError):
            request.state.proxy_error = "client_disconnected"
            if status is None:
                status = 499
        except Exception:
            if not getattr(request.state, "proxy_error", None):
                request.state.proxy_error = "gateway_error"
            if status is None:
                await JSONResponse({"error": {"type": "gateway_error"}}, status_code=500)(
                    scope, replay_request, capture_response,
                )
            _LOG.exception("处理请求失败（%s）", trace["id"])
            if not downstream_complete:
                raise
        finally:
            try:
                source = getattr(request.state, "proxy_stream", None)
                if source is not None:
                    await source.aclose()
                close_upstream = getattr(request.state, "proxy_upstream_close", None)
                if close_upstream is not None:
                    await close_upstream()
            except Exception:
                if not getattr(request.state, "proxy_error", None):
                    request.state.proxy_error = "gateway_error"
                _LOG.exception("关闭上游响应失败（%s）", trace["id"])
            if status is None:
                status = 499
            if not downstream_complete and not getattr(request.state, "proxy_error", None):
                request.state.proxy_error = "client_disconnected"
            trace["session-id"] = get_session_id()
            trace["res"] = _result(request, status, response_headers, response_body)
            try:
                async with self.log_lock:
                    write = asyncio.create_task(asyncio.to_thread(
                        self.logs.append, trace, status, 200 <= status < 400 and "error" not in trace["res"],
                    ))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        await write
                        raise
            except OSError:
                _LOG.exception("写入轨迹失败（%s）", trace["id"])


async def serve(providers: list[Provider], log_root: Path) -> None:
    """绑定所有供应商端口，然后启动 HTTP 服务并管理连接池的生命周期。"""
    with ExitStack() as stack:
        sockets = []
        listeners = {}
        for provider in providers:
            try:
                family, _, _, _, address = socket.getaddrinfo(
                    provider.host, provider.port, type=socket.SOCK_STREAM,
                )[0]
                listener = stack.enter_context(socket.socket(family, socket.SOCK_STREAM))
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind(address)
            except OSError as exc:
                raise OSError(f"供应商 {provider.name!r} 绑定 {provider.host}:{provider.port} 失败：{exc}") from exc
            sockets.append(listener)
            listeners[listener.getsockname()[:2]] = provider
            _LOG.info("%s 监听 %s:%s，转发至 %s", provider.name, provider.host, provider.port, provider.target)
        gateway = Gateway(listeners, TrajectoryLog(log_root))
        server = uvicorn.Server(uvicorn.Config(gateway, proxy_headers=False))
        try:
            await server.serve(sockets=sockets)
        finally:
            await close_all()


def main() -> None:
    """解析启动参数，读取配置并启动转发网关。"""
    parser = argparse.ArgumentParser(description="启动供应商模型 API 转发网关")
    parser.add_argument("--config", type=Path, required=True, help="proxy_config.json 的路径")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        providers = load_config(args.config)
        asyncio.run(serve(providers, Path("logs_proxy/traj")))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"网关启动失败：{exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
