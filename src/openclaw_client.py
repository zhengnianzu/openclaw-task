"""
OpenClaw 客户端模块

包含:
- ResilientGateway: ProtocolGateway 子类，断线自动重连、WS 心跳、指数退避
- build_openclaw_client 工厂函数
- HTTP 健康检查 (/healthz, /readyz)
- OpenclawWorkspaceManager: openclaw 工作空间管理
- OpenclawAgentManager: openclaw agent 注册管理
- openclaw_execute_queries: openclaw 查询执行(含 history fallback)
"""

import asyncio
import logging
import random
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from websockets.asyncio.client import connect as ws_connect

from openclaw_sdk import OpenClawClient, AgentConfig, ExecutionOptions
from openclaw_sdk.core.config import ClientConfig
from openclaw_sdk.core.exceptions import GatewayError
from openclaw_sdk.core.types import ExecutionResult
from openclaw_sdk.gateway.protocol import ProtocolGateway

from src.workspace import BaseWorkspaceManager, copy_path
from src.config import AgentModelConfig, warn_agent_model_conflict

logger = logging.getLogger("harness_automation")

# ============================================================================
# 常量
# ============================================================================

DEFAULT_GATEWAY_TIMEOUT_SECONDS = 3600
GATEWAY_CONNECT_GRACE_SECONDS = 30.0

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_JITTER = 0.5
_TICK_WATCH_INTERVAL = 30
_TICK_WATCH_HEALTH_TIMEOUT = 30
_ENSURE_CONNECTED_TIMEOUT = 3600

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60
EXECUTION_HISTORY_FALLBACK_DELAY_SECONDS = 3
EXECUTION_HISTORY_FALLBACK_LIMIT = 50
EXECUTION_HISTORY_FALLBACK_MAX_POLLS = 40
EXECUTION_HISTORY_FALLBACK_POLL_INTERVAL_SECONDS = 30.0


# ============================================================================
# ResilientGateway: ProtocolGateway 子类
# ============================================================================

class ResilientGateway(ProtocolGateway):
    """ProtocolGateway 子类，补齐 TUI 级连接韧性。"""

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

    async def _do_connect(self) -> None:
        from openclaw_sdk.gateway.protocol import _load_token

        self._state = "connecting"
        if self._token is None:
            self._token = _load_token()

        self._handshake_done.clear()
        self._connect_req_id = None

        self._ws = await asyncio.wait_for(
            ws_connect(
                self._ws_url,
                ping_interval=20,
                ping_timeout=60,
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

    async def _reader_loop(self) -> None:
        try:
            await super()._reader_loop()
        finally:
            if not self._closed:
                self._reconnect_event.clear()
                self._state = "reconnecting"
                await self._ensure_reconnect_task()

    async def _ensure_reconnect_task(self) -> asyncio.Task[None]:
        async with self._reconnect_lock:
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_event.clear()
                self._state = "reconnecting"
                self._reconnect_task = asyncio.create_task(
                    self._reconnect_with_backoff(),
                    name="openclaw-ws-reconnect",
                )
            return self._reconnect_task

    def _fail_pending(self, exc: Exception) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if future.done():
                continue
            future.set_exception(exc)
            future.add_done_callback(self._consume_future_exception)

    @staticmethod
    def _consume_future_exception(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        try:
            future.exception()
        except Exception:
            pass

    async def _reconnect_with_backoff(self) -> None:
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
            except (GatewayError, Exception) as exc:
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

    async def _tick_watch(self) -> None:
        while not self._closed:
            await asyncio.sleep(_TICK_WATCH_INTERVAL)
            if not self._connected:
                continue
            try:
                status = await asyncio.wait_for(
                    self.health(), timeout=_TICK_WATCH_HEALTH_TIMEOUT
                )
                if status.healthy:
                    self._last_activity = time.monotonic()
                else:
                    logger.warning("Tick watch: health 返回 unhealthy，主动断开触发重连")
                    if self._ws:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
            except Exception:
                logger.warning("Tick watch: health 调用失败，主动断开触发重连")
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

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await self.ensure_connected(timeout=_ENSURE_CONNECTED_TIMEOUT)
        self._last_activity = time.monotonic()
        return await super().call(
            method, params, timeout=timeout, idempotency_key=idempotency_key, **kwargs
        )

    async def subscribe(self, event_types: list[str] | None = None):
        await self.ensure_connected(timeout=_ENSURE_CONNECTED_TIMEOUT)
        self._last_activity = time.monotonic()
        return await super().subscribe(event_types=event_types)

    async def ensure_connected(self, timeout: float = _ENSURE_CONNECTED_TIMEOUT) -> None:
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

    def _on_reconnected(self, attempt: int) -> None:
        logger.info("WebSocket 自动重连成功 (第 %d 次尝试)", attempt)
        self._state = "degraded" if self._degraded else "ready"

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
# Client 工厂
# ============================================================================

async def build_openclaw_client(
    gateway_ws_url: Optional[str] = None,
    api_key: Optional[str] = None,
    gateway_timeout: Optional[int] = None,
) -> OpenClawClient:
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
    client._gateway = new_gw
    logger.info("gateway 连接已重建: %s", ws_url)
    return True


# ============================================================================
# OpenclawWorkspaceManager
# ============================================================================

class OpenclawWorkspaceManager(BaseWorkspaceManager):
    """OpenClaw 工作空间管理器: workspace = base_dir-agent_name"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        if agent_name == "main":
            workspace = self.base_dir
        else:
            parent = self.base_dir.parent
            base_name = self.base_dir.name
            workspace = parent / f"{base_name}-{agent_name}"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        agent_source = Path(agent_dir).expanduser()
        if agent_source.exists():
            for config_file in config_files:
                src = agent_source / config_file
                if src.exists():
                    dst = workspace / config_file
                    copy_path(src, dst)
                    logger.info("复制 Agent 配置: %s -> %s", config_file, dst)
                    dst_main = self.base_dir / config_file
                    copy_path(src, dst_main)
                    logger.info("复制 Agent 配置: %s -> %s", config_file, dst_main)
                else:
                    logger.warning("Agent 配置文件不存在: %s", src)
        else:
            logger.warning("Agent 源目录不存在: %s", agent_source)


# ============================================================================
# OpenclawAgentManager
# ============================================================================

class OpenclawAgentManager:
    """OpenClaw Agent 注册管理器"""

    def __init__(
        self,
        client: OpenClawClient,
        workspace_manager: OpenclawWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides: Dict[str, AgentModelConfig] = agent_overrides or {}

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)
        if agent_config.model:
            logger.info("设置 Agent: %s | model=%s", agent_name, agent_config.model)
        else:
            logger.info("设置 Agent: %s", agent_name)

        # 预设Agent:evaluator
        existing_ids = {a.agent_id for a in await self.client.list_agents()}

        if agent_name not in existing_ids:
            workspace = self.workspace_manager.get_agent_workspace(agent_name)
            await self.client.create_agent(
                AgentConfig(
                    agent_id=agent_name,
                    workspace=str(workspace),
                ),
                workspace=str(workspace),
            )
            logger.info("创建新 Agent: %s,等待 gateway 重启就绪...", agent_name)
            await self._wait_gateway_ready()

        # 钉死模型:agents.create 不下发模型,改用 agents.update 下发(网关侧
        # baseUrl/apiKey 整份回写被拒,只能网关侧 `config.models.providers.*` 配)。
        # override.resolved_model(provider/model) 优先;否则用 agent_config.model。
        model = (override.resolved_model if override and override.model else agent_config.model)
        if model:
            await self._pin_model(agent_name, model, has_endpoint_info=bool(override))

    async def _pin_model(
        self,
        agent_name: str,
        model: str,
        has_endpoint_info: bool = False,
    ) -> None:
        """经 agents.update 下发 model 串(本网关唯一可靠的 per-agent 模型通道)。

        has_endpoint_info=True 表示 override 里带了 baseUrl/apiKey,本 harness 无法
        下发,只是提示用户去网关侧配好 provider。
        """
        if has_endpoint_info:
            logger.info(
                "agent '%s' 的 baseUrl/apiKey 不经 harness 下发(本网关整份回写被拒);"
                "请在网关侧 `config.models.providers.*` 配置,本 agent 仅引用模型串 '%s'",
                agent_name, model,
            )
        try:
            resp = await self.client.gateway.agents_update(agent_name, model=model)
            logger.info(
                "已为 agent '%s' 钉死模型=%s (agents.update 返回: %s)",
                agent_name, model, resp,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "为 agent '%s' 下发模型=%s 失败: %s",
                agent_name, model, e,
            )

    async def _wait_gateway_ready(self, wait: float = 90.0) -> None:
        logger.info("等待 gateway 重启就绪,固定等待 %ds ...", int(wait))
        await asyncio.sleep(wait)
        logger.info("gateway 等待完成")


# ============================================================================
# OpenClaw execute_with_retry + check_readyz
# ============================================================================

async def openclaw_check_readyz(client: OpenClawClient) -> None:
    http_base = gateway_http_base(client.gateway)
    if not http_base:
        return
    _, ready, body = await check_http_health(http_base)
    if ready:
        logger.info("gateway readyz OK: %s", body)
    else:
        logger.warning("gateway readyz 未就绪: %s", body)


def make_openclaw_execute_with_retry(client: OpenClawClient):
    """返回 openclaw 专用的 execute_with_retry 闭包 (含 history fallback + gateway 重连)"""

    async def execute_with_retry(agent, query_text: str, options):
        max_attempts = EXECUTION_MAX_ATTEMPTS

        def extract_message_text(message: Any) -> str:
            if not isinstance(message, dict):
                return ""
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                return "".join(parts).strip()
            text = message.get("text")
            return text.strip() if isinstance(text, str) else ""

        def is_assistant_message(message: Any) -> bool:
            return (
                isinstance(message, dict)
                and str(message.get("role", "")).lower() == "assistant"
            )

        async def fetch_history() -> List[dict[str, Any]]:
            try:
                return await agent._client.gateway.chat_history(
                    agent.session_key,
                    limit=EXECUTION_HISTORY_FALLBACK_LIMIT,
                )
            except Exception as e:
                logger.debug("chat.history 兜底查询失败: %s", e)
                return []

        def find_new_assistant_text(
            before: List[dict[str, Any]],
            after: List[dict[str, Any]],
        ) -> str:
            before_signatures = {
                (
                    str(message.get("role", "")),
                    extract_message_text(message),
                    str(message.get("timestamp", "")),
                    str(message.get("id", "")),
                )
                for message in before
                if isinstance(message, dict)
            }
            new_messages = []
            for message in after:
                if not isinstance(message, dict):
                    continue
                signature = (
                    str(message.get("role", "")),
                    extract_message_text(message),
                    str(message.get("timestamp", "")),
                    str(message.get("id", "")),
                )
                if signature not in before_signatures:
                    new_messages.append(message)
            for message in reversed(new_messages):
                if is_assistant_message(message):
                    text = extract_message_text(message)
                    if text:
                        return text
            return ""

        async def history_fallback(
            before_history: List[dict[str, Any]],
            max_polls: int = EXECUTION_HISTORY_FALLBACK_MAX_POLLS,
            poll_interval: float = EXECUTION_HISTORY_FALLBACK_POLL_INTERVAL_SECONDS,
        ) -> Optional[str]:
            for poll in range(1, max_polls + 1):
                await asyncio.sleep(poll_interval)
                try:
                    after_history = await fetch_history()
                except Exception as e:
                    logger.debug("history_fallback 第 %d/%d 次查询失败: %s", poll, max_polls, e)
                    continue
                text = find_new_assistant_text(before_history, after_history)
                if text:
                    logger.info(
                        "execute 返回空内容,但第 %d 次 history 轮询获取到回复 (等待 %.0fs)",
                        poll, poll * poll_interval,
                    )
                    return text
                logger.debug(
                    "history_fallback 第 %d/%d 次轮询,暂无新回复",
                    poll, max_polls,
                )
            return None

        for attempt in range(1, max_attempts + 1):
            before_history = await fetch_history()
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise RuntimeError("Agent returned None")
                if query_text == "真棒" or query_text == "好吧":
                    print(f"openclaw res={result}")
                if getattr(result, "content", None):
                    return result, False

                fallback_text = await history_fallback(before_history)
                if fallback_text:
                    return result.model_copy(
                        update={
                            "success": True,
                            "content": fallback_text,
                            "stop_reason": result.stop_reason or "complete",
                            "error_message": None,
                        }
                    ), True

                error_message = getattr(result, "error_message", None)
                if error_message and not str(error_message).startswith(
                    "Agent completed with no response"
                ):
                    raise RuntimeError(error_message)

                raise RuntimeError(
                    "Agent returned empty content and chat.history had no new assistant reply"
                )
            except (GatewayError, asyncio.TimeoutError) as e:
                logger.warning(
                    "gateway 连接异常 (第 %d/%d 次): %s，先查 history 看旧 run 是否已完成",
                    attempt, max_attempts, e,
                )
                gw = client.gateway
                if hasattr(gw, "ensure_connected"):
                    try:
                        await gw.ensure_connected(timeout=DEFAULT_GATEWAY_TIMEOUT_SECONDS)
                        logger.info("gateway 重连恢复")
                    except GatewayError:
                        logger.warning("gateway 重连未恢复")

                fallback_text = await history_fallback(before_history)
                if fallback_text:
                    logger.info("WS 断开但 agent 已完成,从 history 获取到回复")
                    return ExecutionResult(
                        success=True,
                        content=fallback_text,
                        stop_reason="complete",
                    ), True

                if attempt >= max_attempts:
                    raise
                logger.warning(
                    "history 也无结果,第 %d/%d 次重试前等待 %d 秒",
                    attempt, max_attempts, EXECUTION_RETRY_WAIT_SECONDS,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
                continue
            except RuntimeError as e:
                if attempt >= max_attempts:
                    logger.error("agent 连续返回空内容 %d 次: %s", attempt, e)
                    raise
                logger.warning(
                    "agent 返回空内容且 history 无兜底,第 %d/%d 次重试前等待 %d 秒: %s",
                    attempt, max_attempts, EXECUTION_RETRY_WAIT_SECONDS, e,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)

    return execute_with_retry

