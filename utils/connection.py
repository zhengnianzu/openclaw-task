"""
OpenClaw 连接管理工具

包含:
- ProtocolGateway monkey-patch (自动重连 + WS 心跳)
- build_openclaw_client 工厂函数
- HTTP 健康检查 (/healthz, /readyz)
"""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from openclaw_sdk import OpenClawClient
from openclaw_sdk.core.config import ClientConfig
from openclaw_sdk.core.exceptions import GatewayError
from openclaw_sdk.gateway import protocol as _ocw_protocol
from openclaw_sdk.gateway.protocol import ProtocolGateway

logger = logging.getLogger("openclaw_automation")

# ============================================================================
# 常量
# ============================================================================

DEFAULT_GATEWAY_TIMEOUT_SECONDS = 30
GATEWAY_CONNECT_GRACE_SECONDS = 5.0

# ============================================================================
# Monkey-patch: 给 ProtocolGateway 注入断线自动重连 + 更激进的 WS 心跳
# ============================================================================

_original_reader_loop = ProtocolGateway._reader_loop


async def _reconnecting_reader_loop(self: ProtocolGateway) -> None:
    """带自动重连的 reader loop,替换 SDK 原版。"""
    try:
        await _original_reader_loop(self)
    finally:
        if not self._closed:
            logger.warning("WebSocket 断开,自动重连中...")
            try:
                await self._do_connect()
                logger.info("WebSocket 自动重连成功")
            except Exception as e:
                logger.error("WebSocket 自动重连失败: %s", e)


ProtocolGateway._reader_loop = _reconnecting_reader_loop  # type: ignore[assignment]

try:
    from websockets.asyncio.client import connect as _ws_connect_patched

    async def _patched_open_connection(ws_url: str, timeout: float):
        try:
            return await asyncio.wait_for(
                _ws_connect_patched(
                    ws_url,
                    ping_interval=15,
                    ping_timeout=10,
                    close_timeout=5,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise GatewayError(
                f"Timed out connecting to {ws_url} after {timeout}s"
            ) from exc

    _ocw_protocol._open_connection = _patched_open_connection  # type: ignore[attr-defined]
except Exception as _patch_err:
    logger.warning("未能为 WebSocket 注入心跳参数,使用 SDK 默认值: %s", _patch_err)


# ============================================================================
# HTTP 健康检查
# ============================================================================

def gateway_http_base(gateway) -> Optional[str]:
    """从 gateway 推导 HTTP base URL."""
    base_url = getattr(gateway, "_base_url", None)
    if base_url:
        return base_url.rstrip("/")
    ws_url = getattr(gateway, "_ws_url", None)
    if not ws_url:
        return None
    parsed = urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.hostname}:{parsed.port}"


async def check_http_health(
    http_base: str,
    *,
    timeout: float = 5.0,
) -> tuple[bool, bool, Optional[dict]]:
    """调用 /healthz 和 /readyz,返回 (liveness_ok, readiness_ok, readyz_body_or_None).

    响应格式:
      /healthz → {"ok": true, "status": "live"}
      /readyz  → {"ready": true, "failing": [], "uptimeMs": ...}
    """
    liveness_ok = False
    readiness_ok = False
    readyz_body: Optional[dict] = None

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as sess:
            async with sess.get(f"{http_base}/healthz") as resp:
                try:
                    body = await resp.json(content_type=None)
                    liveness_ok = bool(body.get("ok", False))
                except Exception:
                    liveness_ok = resp.status == 200

            async with sess.get(f"{http_base}/readyz") as resp:
                try:
                    readyz_body = await resp.json(content_type=None)
                    readiness_ok = bool(readyz_body.get("ready", False))
                except Exception:
                    readiness_ok = resp.status == 200
                    readyz_body = {"raw": await resp.text()}
    except Exception as e:
        logger.debug("HTTP 健康检查异常: %s", e)

    return liveness_ok, readiness_ok, readyz_body


# ============================================================================
# Client 工厂 + Gateway 重建
# ============================================================================

async def build_openclaw_client(
    gateway_ws_url: Optional[str] = None,
    api_key: Optional[str] = None,
    gateway_timeout: Optional[int] = None,
) -> OpenClawClient:
    """构造 ProtocolGateway (WebSocket) client,带自动重连 patch。"""
    timeout = float(gateway_timeout or DEFAULT_GATEWAY_TIMEOUT_SECONDS)

    config = ClientConfig(
        mode="protocol" if gateway_ws_url else "auto",
        gateway_ws_url=gateway_ws_url,
        api_key=api_key,
        timeout=int(timeout),
    )

    gateway = ProtocolGateway(
        ws_url=gateway_ws_url or "ws://127.0.0.1:18789/gateway",
        token=api_key,
        connect_timeout=timeout,
        default_timeout=timeout,
        retry_policy=config.retry_policy,
    )
    try:
        await asyncio.wait_for(
            gateway.connect(),
            timeout=timeout + GATEWAY_CONNECT_GRACE_SECONDS,
        )
    except Exception:
        await gateway.close()
        raise
    return OpenClawClient(config=config, gateway=gateway)


async def rebuild_gateway(client: OpenClawClient) -> bool:
    """丢掉旧 ProtocolGateway,重建一个新的并替换 client._gateway。
    仅在 auto-reconnect patch 也无法恢复时使用。
    """
    old_gw = client.gateway
    try:
        await asyncio.wait_for(old_gw.close(), timeout=5)
    except Exception:
        pass

    ws_url = getattr(old_gw, "_ws_url", None)
    token = getattr(old_gw, "_token", None)
    connect_timeout = getattr(old_gw, "_connect_timeout", float(DEFAULT_GATEWAY_TIMEOUT_SECONDS))
    default_timeout = getattr(old_gw, "_default_timeout", float(DEFAULT_GATEWAY_TIMEOUT_SECONDS))
    retry_policy = getattr(old_gw, "_retry_policy", None)

    if not ws_url:
        logger.error("旧 gateway 中拿不到 ws_url,无法重建")
        return False

    new_gw = ProtocolGateway(
        ws_url=ws_url,
        token=token,
        connect_timeout=connect_timeout,
        default_timeout=default_timeout,
        retry_policy=retry_policy,
    )
    try:
        await asyncio.wait_for(
            new_gw.connect(),
            timeout=connect_timeout + GATEWAY_CONNECT_GRACE_SECONDS,
        )
    except Exception as e:
        logger.warning("重建 gateway 连接失败: %s", e)
        try:
            await new_gw.close()
        except Exception:
            pass
        return False

    client._gateway = new_gw  # type: ignore[attr-defined]
    logger.info("gateway 连接已重建: %s", ws_url)
    return True
