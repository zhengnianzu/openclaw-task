"""
Open WebUI HTTP 客户端封装 (OpenAI 兼容接口模式)

把一个已经在运行的 open-webui 服务当作 harness 后端:通过它的
OpenAI 兼容接口 POST /api/chat/completions 驱动被测模型。
鉴权走 Authorization: Bearer <api_key>(open-webui 的 sk- API key 或 JWT)。

多轮会话状态由客户端本地累积 (self._history),每轮把完整 messages 发过去
(与 hermes_client 的做法一致;open-webui 兼容接口本身是无状态的)。

公开 API:
  OpenwebuiClient / OpenwebuiAgent / ExecutionResult / ExecutionOptions / OpenwebuiError
  build_openwebui_client()
  OpenwebuiWorkspaceManager / OpenwebuiAgentManager
  make_openwebui_execute_with_retry / make_openwebui_get_agent
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.workspace import BaseWorkspaceManager, copy_path
from src.config import AgentModelConfig, warn_agent_model_conflict

logger = logging.getLogger("harness_automation")


# ============================================================================
# 连接参数默认值 / 环境变量
# ============================================================================

_DEFAULT_BASE_URL = "http://localhost:8080"
_COMPLETIONS_PATH = "/api/chat/completions"


def _default_base_url() -> str:
    return os.environ.get("OPENWEBUI_BASE_URL", _DEFAULT_BASE_URL)


def _default_api_key() -> Optional[str]:
    return os.environ.get("OPENWEBUI_API_KEY")


def _default_model() -> Optional[str]:
    return os.environ.get("OPENWEBUI_MODEL")


def _completions_url(base_url: str) -> str:
    """把 base_url 归一成 chat completions 端点。

    兼容两类目标(override 可指向任意端点,如 evaluator 直连 OpenAI 兼容 API):
    - 已带完整 .../chat/completions(含 open-webui 的 /api/chat/completions)→ 原样用;
    - 裸 OpenAI 兼容端点(以 /v1 结尾,如 https://x/v1)→ 拼 /chat/completions;
    - 其余视作 open-webui 服务(如 http://localhost:8088)→ 拼 /api/chat/completions。
    """
    url = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):   # 覆盖 /api/chat/completions 与 /chat/completions
        return url
    if url.endswith("/v1"):                 # 裸 OpenAI 兼容端点
        return url + "/chat/completions"
    return url + _COMPLETIONS_PATH          # open-webui 服务


def _is_openwebui_endpoint(base_url: str) -> bool:
    """判断 base_url 是否指向 open-webui 服务(而非裸 OpenAI 兼容端点)。

    只有 open-webui 端点才支持 features / tool_ids;裸 OpenAI(如 yibuapi 的 /v1)不认这些字段。
    """
    url = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")
    if url.endswith("/api/chat/completions"):
        return True
    if url.endswith("/chat/completions"):   # /v1/chat/completions 这类裸端点
        return False
    if url.endswith("/v1"):
        return False
    return True


def _tools_list_url(base_url: str) -> str:
    """open-webui 已注册工具列表端点:{base_url}/api/v1/tools/"""
    return (base_url or _DEFAULT_BASE_URL).strip().rstrip("/") + "/api/v1/tools/"


def _default_features() -> Dict[str, bool]:
    """从环境变量 OPENWEBUI_FEATURES(逗号分隔)读取要开启的内置能力,如 "web_search"。

    这些是布尔开关型能力(非已注册工具),无法自动发现,故用环境变量声明。
    """
    raw = os.environ.get("OPENWEBUI_FEATURES", "").strip()
    return {f.strip(): True for f in raw.split(",") if f.strip()}


def _apply_prompt_vars(text: str) -> str:
    """替换 open-webui 风格的时间模板变量(格式与 open-webui utils/task.py 对齐)。

    纯 API 路径下 open-webui 不做 UI 模板替换,故在客户端每次发送前本地替换,
    保证取的是当下时间(而非部署/引导时刻)。
    """
    if not text or "{{CURRENT_" not in text:
        return text
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    clock = now.strftime("%I:%M:%S %p")
    weekday = now.strftime("%A")
    return (
        text.replace("{{CURRENT_DATE}}", date)
        .replace("{{CURRENT_TIME}}", clock)
        .replace("{{CURRENT_DATETIME}}", f"{date} {clock}")
        .replace("{{CURRENT_WEEKDAY}}", weekday)
    )


# ============================================================================
# 异常类型
# ============================================================================

class OpenwebuiError(RuntimeError):
    """open-webui 调用失败 (HTTP 非 2xx / 超时 / 空响应 / 结构异常)。"""


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        data = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
        }
        if update:
            data.update(update)
        return ExecutionResult(**data)


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


# ============================================================================
# OpenwebuiAgent — 一个 (agent_name, session_name) 句柄,自维护 messages 历史
# ============================================================================

class OpenwebuiAgent:
    """对应一个 agent_name + session_name 句柄。

    open-webui 的 OpenAI 兼容接口无服务端会话概念,故会话历史由本对象累积:
    每轮把 system(若有) + 历史 + 本轮 user 一起发过去。
    """

    def __init__(
        self,
        client: "OpenwebuiClient",
        agent_name: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name
        self._system_prompt = system_prompt
        self._model = model or _default_model()
        self._base_url = base_url or _default_base_url()
        self._api_key = api_key or _default_api_key()
        self._history: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def reset(self) -> None:
        """清空本地会话历史,使下一次 execute 从全新上下文开始。

        供 evaluator 每轮防判词锚定用(openwebui 无服务端会话,历史全在本地)。
        """
        async with self._lock:
            self._history.clear()

    def _build_messages(self, query: str) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        if self._system_prompt:
            # 每次发送前替换时间模板变量({{CURRENT_DATETIME}} 等),取当下时间
            messages.append({"role": "system", "content": _apply_prompt_vars(self._system_prompt)})
        messages.extend(self._history)
        messages.append({"role": "user", "content": query})
        return messages

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        if not self._model:
            return ExecutionResult(
                success=False,
                content="",
                stop_reason="error",
                error_message=(
                    "open-webui: 未指定 model(请在 simulator_config 里给该 agent 配 "
                    "model,或设 OPENWEBUI_MODEL 环境变量)"
                ),
            )

        timeout = (
            float(options.timeout_seconds)
            if options and options.timeout_seconds
            else None
        )

        url = _completions_url(self._base_url)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "model": self._model,
            "messages": self._build_messages(query),
            "stream": False,
        }

        # 工具透传(仅 open-webui 端点):tool_ids 自动发现 + features 由环境变量声明。
        # 都为空则不加字段,保持对裸 OpenAI 端点的纯 chat 兼容。
        if _is_openwebui_endpoint(self._base_url):
            tool_ids = await self._client.discover_tool_ids(self._base_url, self._api_key)
            if tool_ids:
                payload["tool_ids"] = tool_ids
            features = _default_features()
            if features:
                payload["features"] = features

        async with self._lock:
            http = self._client.http
            try:
                resp = await http.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=(httpx.Timeout(timeout) if timeout is not None else None),
                )
            except httpx.TimeoutException:
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="timeout",
                    error_message=f"open-webui 请求超时 (>{timeout}s): {url}",
                )
            except httpx.HTTPError as e:
                logger.exception(
                    "open-webui 请求失败 (agent=%s session=%s)",
                    self.agent_name, self.session_name,
                )
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=f"{type(e).__name__}: {e}",
                )

            if resp.status_code >= 400:
                body_snippet = resp.text[:500]
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=(
                        f"open-webui HTTP {resp.status_code}: {body_snippet}"
                    ),
                )

            try:
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=f"open-webui 响应非 JSON: {e}; body={resp.text[:500]}",
                )

            try:
                choice = data["choices"][0]
                content = (choice["message"]["content"] or "").strip()
            except (KeyError, IndexError, TypeError) as e:
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=(
                        f"open-webui 响应结构异常 ({e}); body={str(data)[:500]}"
                    ),
                )

            finish_reason = choice.get("finish_reason") or "stop"
            usage = data.get("usage")

            if not content:
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message="open-webui 返回空 content",
                    usage=usage,
                )

            # 仅成功且非空才落历史(与 hermes 一致)
            self._history.append({"role": "user", "content": query})
            self._history.append({"role": "assistant", "content": content})

            # OpenAI 的 finish_reason=stop 归一为 "complete";其余(length/content_filter…)原样带出
            stop_reason = "complete" if finish_reason == "stop" else finish_reason
            return ExecutionResult(
                success=True,
                content=content,
                stop_reason=stop_reason,
                usage=usage,
            )


# ============================================================================
# OpenwebuiClient — 进程内 client,缓存 (agent_name, session_name) → Agent
# ============================================================================

class OpenwebuiClient:
    """open-webui HTTP 客户端。

    保存每个 agent 的默认参数 (system_prompt / model / base_url / api_key),
    共享一个 httpx.AsyncClient 连接池。每个 (agent_name, session_name)
    对应一个独立 OpenwebuiAgent(各自维护会话历史)。
    """

    def __init__(self) -> None:
        self._agents: Dict[tuple, OpenwebuiAgent] = {}
        self._agent_defaults: Dict[str, Dict[str, Any]] = {}
        self.http: httpx.AsyncClient = httpx.AsyncClient()
        # 自动发现的工具 id,按 base_url 缓存(每个端点只查一次)
        self._tool_ids_cache: Dict[str, List[str]] = {}

    async def discover_tool_ids(
        self, base_url: str, api_key: Optional[str]
    ) -> List[str]:
        """查询 open-webui 已注册工具 id 列表,按 base_url 缓存;失败/非 open-webui 端点返回 []。

        工具在 open-webui 部署侧注册,本工程零硬编码:运行时自动发现并转发其 tool_ids。
        """
        cache_key = (base_url or "").strip().rstrip("/")
        if cache_key in self._tool_ids_cache:
            return self._tool_ids_cache[cache_key]

        ids: List[str] = []
        if _is_openwebui_endpoint(base_url):
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                resp = await self.http.get(
                    _tools_list_url(base_url),
                    headers=headers,
                    timeout=httpx.Timeout(15.0),
                )
                if resp.status_code < 400:
                    data = resp.json()
                    if isinstance(data, list):
                        ids = [
                            t["id"] for t in data
                            if isinstance(t, dict) and t.get("id")
                        ]
                else:
                    logger.debug(
                        "open-webui 工具列表 HTTP %s: %s",
                        resp.status_code, resp.text[:200],
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("open-webui 工具自动发现失败(降级无工具): %s", e)

        self._tool_ids_cache[cache_key] = ids
        if ids:
            logger.info("open-webui 自动发现工具 (%s): %s", cache_key, ids)
        return ids

    async def __aenter__(self) -> "OpenwebuiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        self._agents.clear()
        try:
            await self.http.aclose()
        except Exception as e:  # noqa: BLE001
            logger.debug("httpx.AsyncClient.aclose 异常 (忽略): %s", e)

    def register_agent_defaults(
        self,
        agent_name: str,
        *,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        """AgentManager 在 setup_agent 时调用,后续 get_agent 可以省参数。"""
        self._agent_defaults[agent_name] = {
            "system_prompt": system_prompt,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
        }

    def get_agent(
        self,
        agent_name: str,
        session_name: str,
        *,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> OpenwebuiAgent:
        key = (agent_name, session_name)
        if key in self._agents:
            return self._agents[key]

        defaults = self._agent_defaults.get(agent_name, {})
        agent = OpenwebuiAgent(
            client=self,
            agent_name=agent_name,
            session_name=session_name,
            system_prompt=system_prompt or defaults.get("system_prompt"),
            model=model or defaults.get("model"),
            base_url=base_url or defaults.get("base_url"),
            api_key=api_key or defaults.get("api_key"),
        )
        self._agents[key] = agent
        return agent


# ============================================================================
# 工厂函数
# ============================================================================

async def build_openwebui_client(**_ignored_legacy_kwargs: Any) -> OpenwebuiClient:
    if _ignored_legacy_kwargs:
        logger.debug(
            "build_openwebui_client: 忽略以下旧的网关参数: %s",
            sorted(_ignored_legacy_kwargs.keys()),
        )
    client = OpenwebuiClient()
    logger.info(
        "open-webui 客户端 (OpenAI 兼容 HTTP 模式) 就绪;base_url 缺省 %s,"
        "需要 open-webui 服务已运行且提供了有效 API key。",
        _default_base_url(),
    )
    return client


# ============================================================================
# 重试常量
# ============================================================================

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60


# ============================================================================
# OpenwebuiWorkspaceManager
# ============================================================================

class OpenwebuiWorkspaceManager(BaseWorkspaceManager):
    """open-webui 工作空间管理器。

    open-webui 是远程服务,没有每-agent 的本地文件系统概念;这里维护一个
    本地目录仅为满足 _setup_workspaces() 契约、暂存 agent 配置文件
    (布局与 claudecode 一致)。
    """

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
        agent_source_root = Path(agent_dir).expanduser()

        if not agent_source_root.exists():
            logger.warning("Agent 源目录不存在: %s", agent_source_root)
            return

        for config_file in config_files:
            src = agent_source_root / config_file
            if not src.exists():
                logger.warning("Agent 配置文件不存在: %s", src)
                continue
            dst = workspace / config_file
            copy_path(src, dst)
            logger.info("复制 Agent 配置: %s -> %s", src, dst)


# ============================================================================
# OpenwebuiAgentManager
# ============================================================================

class OpenwebuiAgentManager:
    """open-webui Agent 管理器: 把 AgentConfigItem → OpenwebuiClient 默认参数。

    没有远程 gateway 概念,setup_agent 只做:
      1. 解析 workspace(暂存配置文件)
      2. 把 system_prompt / model / base_url / api_key 注册到 client 默认表
    """

    def __init__(
        self,
        client: OpenwebuiClient,
        workspace_manager: OpenwebuiWorkspaceManager,
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
        self.workspace_manager.get_agent_workspace(agent_name)

        # simulator_config 命中时优先用其中的 model/base_url/api_key
        effective_model = getattr(agent_config, "model", None)
        base_url: Optional[str] = None
        api_key: Optional[str] = None
        if override:
            if override.model:
                effective_model = override.model
            if override.base_url:
                base_url = override.base_url
            if override.api_key:
                api_key = override.api_key

        self.client.register_agent_defaults(
            agent_name=agent_name,
            system_prompt=getattr(agent_config, "system_prompt", None),
            model=effective_model,
            base_url=base_url,
            api_key=api_key,
        )


# ============================================================================
# make_openwebui_execute_with_retry — 供 src.executor.execute_queries 注入
# ============================================================================

def make_openwebui_execute_with_retry(
    client: OpenwebuiClient,
    workspace_manager: Optional[OpenwebuiWorkspaceManager] = None,
):
    """返回 openwebui 专用的 execute_with_retry 闭包 (简单重试,无 history fallback)。

    返回 `(result, evidence_incomplete)`,签名与其它 harness 对齐:
    - 正常返回 → `(result, False)`;
    - stop_reason 为 timeout/error 但拿到部分 content → `(result, True)`,提示下游
      evaluator:本轮回复可能被截断,证据缺失不得当负面证据(D5)。
    """

    async def execute_with_retry(agent: OpenwebuiAgent, query_text: str, options):
        last_exc: Optional[BaseException] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise OpenwebuiError("open-webui returned None")
                if result.success and result.content:
                    evidence_incomplete = (result.stop_reason or "complete") != "complete"
                    return result, evidence_incomplete
                if not result.success:
                    raise OpenwebuiError(
                        result.error_message or "open-webui returned error"
                    )
                raise OpenwebuiError("open-webui returned empty content")
            except (OpenwebuiError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt >= EXECUTION_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "调用失败 (第 %d/%d 次): %s; %ds 后重试",
                    attempt, EXECUTION_MAX_ATTEMPTS, e, EXECUTION_RETRY_WAIT_SECONDS,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
        if last_exc is not None:
            raise last_exc
        raise OpenwebuiError("open-webui: unknown error after retries")

    return execute_with_retry


def make_openwebui_get_agent(
    client: OpenwebuiClient,
    workspace_manager: Optional[OpenwebuiWorkspaceManager] = None,
):
    """返回 openwebui 专用的 get_agent_fn 闭包 (连接参数已在 client 默认表里)"""

    def get_agent(agent_name: str, session_name: str) -> OpenwebuiAgent:
        return client.get_agent(agent_name, session_name)

    return get_agent
