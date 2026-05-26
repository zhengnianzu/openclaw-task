"""
OpenClaw 连接管理工具

包含:
- ResilientGateway: ProtocolGateway 子类，提供断线自动重连、WS 心跳、指数退避
- build_openclaw_client 工厂函数
- HTTP 健康检查 (/healthz, /readyz)
"""

import asyncio
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
from websockets.asyncio.client import connect as ws_connect

from openclaw_sdk import OpenClawClient
from openclaw_sdk.core.config import ClientConfig
from openclaw_sdk.core.exceptions import GatewayError
from openclaw_sdk.gateway.protocol import ProtocolGateway

logger = logging.getLogger("openclaw_automation")

# ============================================================================
# 常量
# ============================================================================

DEFAULT_GATEWAY_TIMEOUT_SECONDS = 30
GATEWAY_CONNECT_GRACE_SECONDS = 5.0

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER = 0.5
_TICK_WATCH_INTERVAL = 30
_TICK_WATCH_HEALTH_TIMEOUT = 10


# ============================================================================
# ResilientGateway: ProtocolGateway 子类，替代 monkey-patch
# ============================================================================

class ResilientGateway(ProtocolGateway):
    """ProtocolGateway 子类，补齐 TUI 级连接韧性。

    增强能力:
    1. _do_connect override: 注入 WS 心跳参数 (ping_interval/ping_timeout)
    2. _reader_loop override: 断线后自动触发指数退避重连
    3. _tick_watch: 定期检测连接活性，超时主动断开触发重连
    4. call override: 断线期间等待重连完成再发请求
    5. _on_reconnected: 重连后日志 + 序号丢失警告
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._reconnect_event = asyncio.Event()
        self._reconnect_event.set()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_lock = asyncio.Lock()
        self._tick_task: asyncio.Task[None] | None = None
        self._last_activity: float = 0.0
        self._last_seq: int = 0
        self._state: str = "disconnected"
        self._degraded: bool = False

    # ------------------------------------------------------------------ #
    # 1. 注入 WS 心跳参数
    # ------------------------------------------------------------------ #

    async def _do_connect(self) -> None:
        """Open WS with ping/pong heartbeat, then complete handshake."""
        from openclaw_sdk.gateway.protocol import _load_token

        self._state = "connecting"
        if self._token is None:
            self._token = _load_token()

        self._handshake_done.clear()
        self._connect_req_id = None

        self._ws = await asyncio.wait_for(
            ws_connect(
                self._ws_url,
                ping_interval=15,
                ping_timeout=10,
                close_timeout=5,
            ),
            timeout=self._connect_timeout,
        )

        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="openclaw-ws-reader"
        )

        try:
            await asyncio.wait_for(
                self._handshake_done.wait(), timeout=self._connect_timeout
            )
        except asyncio.TimeoutError as exc:
            await self._cleanup_ws()
            raise GatewayError(
                "Timed out waiting for connect handshake with gateway"
            ) from exc

        self._connected = True
        self._last_activity = time.monotonic()
        self._state = "ready"

    # ------------------------------------------------------------------ #
    # 2. 断线自动重连
    # ------------------------------------------------------------------ #

    async def _reader_loop(self) -> None:
        """在父类 reader loop 退出后，自动触发重连。"""
        try:
            await super()._reader_loop()
        finally:
            if not self._closed:
                self._reconnect_event.clear()
                self._state = "reconnecting"
                await self._ensure_reconnect_task()

    async def _ensure_reconnect_task(self) -> asyncio.Task[None]:
        """确保任意时刻最多只有一个重连任务。"""
        async with self._reconnect_lock:
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_event.clear()
                self._state = "reconnecting"
                self._reconnect_task = asyncio.create_task(
                    self._reconnect_with_backoff(),
                    name="openclaw-ws-reconnect",
                )
            return self._reconnect_task

    async def _reconnect_with_backoff(self) -> None:
        """指数退避重连，对标 TUI 的 scheduleReconnect。"""
        delay = _BACKOFF_INITIAL
        attempt = 0
        while not self._closed:
            attempt += 1
            try:
                await self._cleanup_ws()
                await self._do_connect()
                self._on_reconnected(attempt)
                self._reconnect_event.set()
                return
            except GatewayError:
                logger.error("重连认证失败，停止重连")
                self._state = "disconnected"
                self._reconnect_event.set()
                return
            except Exception as exc:
                if self._closed:
                    self._state = "closed"
                    self._reconnect_event.set()
                    return
                jitter = random.uniform(-_BACKOFF_JITTER * delay, _BACKOFF_JITTER * delay)
                wait = min(delay + jitter, _BACKOFF_MAX)
                logger.warning(
                    "WebSocket 重连第 %d 次失败 (%s)；%.1f 秒后重试",
                    attempt, exc, wait,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, _BACKOFF_MAX)
        self._state = "closed" if self._closed else "disconnected"

    # ------------------------------------------------------------------ #
    # 3. Tick watch — 定期检测连接活性
    # ------------------------------------------------------------------ #

    async def _tick_watch(self) -> None:
        """轻量 idle watchdog，长时间无活动时主动断开触发重连。"""
        while not self._closed:
            await asyncio.sleep(_TICK_WATCH_INTERVAL)
            if not self._connected:
                continue
            elapsed = time.monotonic() - self._last_activity
            if elapsed < _TICK_WATCH_INTERVAL * 2:
                continue
            logger.warning(
                "Tick watch 检测到连接空闲 %.1f 秒，主动断开触发重连",
                elapsed,
            )
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    def _start_tick_watch(self) -> None:
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(
                self._tick_watch(), name="openclaw-tick-watch"
            )

    # ------------------------------------------------------------------ #
    # 4. 断线时等待重连
    # ------------------------------------------------------------------ #

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """断线时等待重连完成再发请求，而不是直接抛异常。"""
        await self.ensure_connected(timeout=60)
        self._last_activity = time.monotonic()
        return await super().call(
            method, params, timeout=timeout, idempotency_key=idempotency_key, **kwargs
        )

    async def ensure_connected(self, timeout: float = 60.0) -> None:
        """等待现有重连完成，必要时主动拉起单飞重连。"""
        deadline = time.monotonic() + timeout
        while True:
            if self._closed:
                raise GatewayError("Gateway closed")
            if self._connected:
                return

            await self._ensure_reconnect_task()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError(f"等待重连超时 ({timeout:.0f}s)")
            try:
                await asyncio.wait_for(self._reconnect_event.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise GatewayError(f"等待重连超时 ({timeout:.0f}s)") from exc

            if self._connected:
                return

            if self._reconnect_task is not None and self._reconnect_task.done():
                exc = self._reconnect_task.exception()
                if exc is not None:
                    raise GatewayError(f"重连失败: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 5. 重连后回调
    # ------------------------------------------------------------------ #

    def _on_reconnected(self, attempt: int) -> None:
        logger.info("WebSocket 自动重连成功 (第 %d 次尝试)", attempt)
        self._state = "degraded" if self._degraded else "ready"

    # ------------------------------------------------------------------ #
    # Lifecycle overrides
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        self._closed = False
        await super().connect()
        self._reconnect_event.set()
        self._start_tick_watch()

    async def close(self) -> None:
        self._closed = True
        self._state = "closed"
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        self._reconnect_event.set()
        await super().close()

    async def _route_message(self, msg: dict[str, Any]) -> None:
        self._last_activity = time.monotonic()
        seq = msg.get("seq")
        if seq is not None and isinstance(seq, int):
            if self._last_seq and seq > self._last_seq + 1:
                logger.warning(
                    "事件序号跳跃: %d → %d，可能丢失 %d 个事件",
                    self._last_seq, seq, seq - self._last_seq - 1,
                )
                self._degraded = True
                self._state = "degraded"
            self._last_seq = seq
        await super()._route_message(msg)

    @property
    def state(self) -> str:
        return self._state

    @property
    def degraded(self) -> bool:
        return self._degraded


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
    """调用 /healthz 和 /readyz，返回 (liveness_ok, readiness_ok, readyz_body_or_None)."""
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
    """构造 ResilientGateway (WebSocket) client，带自动重连 + 心跳。"""
    timeout = float(gateway_timeout or DEFAULT_GATEWAY_TIMEOUT_SECONDS)

    config = ClientConfig(
        mode="protocol" if gateway_ws_url else "auto",
        gateway_ws_url=gateway_ws_url,
        api_key=api_key,
        timeout=int(timeout),
    )

    gateway = ResilientGateway(
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
    """丢掉旧 gateway，重建一个新的 ResilientGateway 并替换 client._gateway。"""
    old_gw = client.gateway
    if isinstance(old_gw, ResilientGateway):
        if old_gw._pending:
            logger.warning("gateway 仍有 %d 个 pending 请求，跳过重建", len(old_gw._pending))
            return False
        if old_gw._subscribers:
            logger.warning("gateway 仍有 %d 个活动订阅，跳过重建", len(old_gw._subscribers))
            return False
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
        logger.error("旧 gateway 中拿不到 ws_url，无法重建")
        return False

    new_gw = ResilientGateway(
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
