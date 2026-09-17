"""Openjiuwen 进程内 DeepAgent 客户端。

模型解析优先级(由高到低):
  1. simulator_config[agent_name]
  2. ~/.openjiuwen/openjiuwen.json → default

公开 API(executor / harness_automation 依赖):
  OpenjiuwenClient / OpenjiuwenAgent
  ExecutionResult / ExecutionOptions / OpenjiuwenError
  OpenjiuwenWorkspaceManager / OpenjiuwenAgentManager
  build_openjiuwen_client() / make_openjiuwen_execute_with_retry() / make_openjiuwen_get_agent()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _prequiet_openjiuwen_sdk_logs() -> None:
    """在 import openjiuwen.harness 前收敛 SDK 日志。"""
    level = os.environ.get("OPENJIUWEN_SDK_LOG_LEVEL", "INFO").upper()
    keep_console = os.environ.get("OPENJIUWEN_SDK_LOG_CONSOLE", "0") == "1"
    log_path = str(Path(__file__).resolve().parents[1] / "logs" / "openjiuwen_logs")
    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config
        from openjiuwen.core.common.logging.default.constant import DEFAULT_INNER_LOG_CONFIG
        cfg = dict(DEFAULT_INNER_LOG_CONFIG)
        cfg["level"] = level
        cfg["log_path"] = log_path
        cfg["loggers"] = {**dict(cfg.get("loggers") or {}), "common": {"level": "WARNING"}}
        if not keep_console:
            for key in ("output", "interface_output", "performance_output"):
                outs = cfg.get(key)
                if isinstance(outs, (list, tuple)):
                    cfg[key] = [o for o in outs if o != "console"] or ["file"]
        configure_log_config(cfg)
    except Exception:  # noqa: BLE001
        pass


_prequiet_openjiuwen_sdk_logs()

from openjiuwen.harness import create_deep_agent
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.config import ProviderType
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperation,
    SysOperationCard,
)
from openjiuwen.harness.cli.storage.session_store import SessionStore
from openjiuwen.harness.rails.context_engineer.context_assemble_rail import ContextAssembleRail
from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
from openjiuwen.harness.rails.task_planning_rail import TaskPlanningRail
from openjiuwen.harness.rails.memory.memory_rail import MemoryRail
from openjiuwen.harness.tools import create_web_tools as sdk_tools
from openjiuwen.core.runner import Runner
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")

# ============================================================================
# 常量
# ============================================================================

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 600
DEFAULT_LLM_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_TOKENS = 131072
DEFAULT_MCP_VERSION = "0.0.79"
DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS = 120
DEFAULT_LANGUAGE = "cn"
OPENJIUWEN_HOME_PATH = Path.home() / ".openjiuwen" / "openjiuwen.json"
DEFAULT_SESSION_STORE_DIR = Path.home() / ".openjiuwen" / "sessions"


# ============================================================================
# 异常 / 数据结构
# ============================================================================

class OpenjiuwenError(RuntimeError):
    """Openjiuwen 客户端 / DeepAgent 调用失败。"""


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[float] = None


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    messages: Optional[List[Dict[str, Any]]] = field(default=None)
    files: List[Dict[str, Any]] = field(default_factory=list)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        values = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
            "messages": self.messages,
            "files": list(self.files),
        }
        if update:
            values.update(update)
        return ExecutionResult(**values)


# ============================================================================
# openjiuwen.json → HomeConfig
# ============================================================================

@dataclass
class HomeConfig:
    """openjiuwen.json 解析后的配置快照"""

    model: Optional[AgentModelConfig] = None
    timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: Optional[int] = None
    language: str = DEFAULT_LANGUAGE
    paid_search: bool = True
    embedding: Dict[str, Any] = field(default_factory=dict)
    playwright_mcp_version: str = DEFAULT_MCP_VERSION

    @property
    def agent_kwargs(self) -> Dict[str, Any]:
        """create_deep_agent 支持透传的字段。"""
        kwargs: Dict[str, Any] = {"prompt_mode": "full", "language": self.language}
        if self.max_iterations is not None:
            kwargs["max_iterations"] = self.max_iterations
        return kwargs


def load_home_config(path: Path = OPENJIUWEN_HOME_PATH) -> HomeConfig:
    """一次性读取并解析 openjiuwen.json,后续直接用 cfg.xxx 取值。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取 %s 失败,忽略全局兜底: %s", path, e)
        return HomeConfig()

    default_raw = data.get("default")
    default: Dict[str, Any] = default_raw if isinstance(default_raw, dict) else {}
    return HomeConfig(
        model=AgentModelConfig.model_validate(default),
        timeout=default.get("timeout") or DEFAULT_LLM_TIMEOUT_SECONDS,
        max_tokens=default.get("max_tokens") or DEFAULT_MAX_TOKENS,
        max_iterations=default.get("max_iterations"),
        language=default.get("language") or DEFAULT_LANGUAGE,
        paid_search=bool(default.get("paid_search", True)),
        embedding=data.get("embedding"),
        playwright_mcp_version=(data.get("playwright_mcp") or {}).get("version") or DEFAULT_MCP_VERSION,
    )


# ============================================================================
# Model / SysOperation / MemoryRail 构造
# ============================================================================

def build_model(config: AgentModelConfig, *, timeout: float, max_tokens: int) -> Model:
    """根据 AgentModelConfig 构造 openjiuwen Model。"""
    provider = (config.api or config.provider or "").strip() or "OpenAI"
    if provider not in ProviderType:
        if provider != "OpenAI":
            logger.warning("provider %r 不在 openjiuwen 白名单,回退 OpenAI", provider)
        provider = "OpenAI"

    model_name = config.model
    base_url = config.base_url
    api_key = config.api_key
    if not all((model_name, base_url, api_key)):
        raise OpenjiuwenError(
            "Openjiuwen model configuration requires model, base_url, and api_key"
        )
    assert base_url and api_key and model_name

    client_config: Dict[str, Any] = {
        "client_provider": provider,
        "api_key": api_key,
        "api_base": base_url,
        "verify_ssl": base_url.lower().startswith("https://"),
        "timeout": timeout,
    }
    return Model(
        model_client_config=ModelClientConfig(**client_config),
        model_config=ModelRequestConfig(model=model_name, max_tokens=int(max_tokens)),
    )


def _build_memory_rail(embedding: Dict[str, Any]) -> Optional[MemoryRail]:
    """embedding 三项不全时返回 None(不注册记忆工具)。"""
    cfg = dict(embedding or {})
    model_name = (cfg.get("model_name") or "").strip()
    base_url = (cfg.get("base_url") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    if not (model_name and base_url and api_key):
        return None
    try:
        return MemoryRail(
            embedding_config=EmbeddingConfig(
                model_name=model_name, base_url=base_url, api_key=api_key
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("MemoryRail 构建失败,已跳过记忆工具: %s", e)
        return None


def build_sys_operation(agent_name: str, workspace: Path) -> SysOperation:
    card = SysOperationCard(
        id=f"openjiuwen-{agent_name}",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(
            sandbox_root=[str(workspace)],
            restrict_to_sandbox=False,
            shell_allowlist=None,
        ),
    )
    return SysOperation(card)


def _positive_timeout(options: Optional[ExecutionOptions]) -> Optional[float]:
    if options is None or options.timeout_seconds is None:
        return None
    timeout = float(options.timeout_seconds)
    return timeout if timeout > 0 else None


# ============================================================================
# MCP 注册
# ============================================================================

async def _register_mcp_servers(deep_agent: Any, configs: List[McpServerConfig]) -> None:
    for cfg in configs:
        try:
            await asyncio.wait_for(
                _register_mcp_server_once(deep_agent, cfg),
                timeout=DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("openjiuwen MCP 注册失败 (server=%s): %s", cfg.server_name, e)


async def _register_mcp_server_once(deep_agent: Any, cfg: McpServerConfig) -> None:
    result = await Runner.resource_mgr.add_mcp_server(cfg, tag=deep_agent.card.id)
    if getattr(result, "is_err", None) and result.is_err():
        raise OpenjiuwenError(str(getattr(result, "msg", None) or result))
    deep_agent.ability_manager.add(cfg)


# ============================================================================
# Context 快照 / 消息序列化
# ============================================================================

def _snapshot_context(deep_agent: Any, session_name: str) -> List[Any]:
    getter = getattr(deep_agent, "get_current_context", None)
    if not callable(getter):
        return []

    react_agent = getattr(deep_agent, "react_agent", None)
    context_engine = getattr(react_agent, "context_engine", None)
    get_context = getattr(context_engine, "get_context", None)
    if callable(get_context):
        try:
            context = get_context(session_id=session_name, context_id="default_context_id")
        except Exception as e:  # noqa: BLE001
            logger.debug("检查 Openjiuwen context 失败 (session=%s): %s", session_name, e)
        else:
            if context is None:
                return []

    try:
        msgs = getter(session_id=session_name)
    except Exception as e:  # noqa: BLE001
        if "cannot find context" in str(e).lower():
            return []
        logger.debug("get_current_context 快照失败 (session=%s): %s", session_name, e)
        return []
    return list(msgs or [])


def _dump_message(msg: Any) -> Optional[Dict[str, Any]]:
    if msg is None:
        return None
    if isinstance(msg, dict):
        return msg
    dump = getattr(msg, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception as e:  # noqa: BLE001
            logger.debug("消息 model_dump 失败: %s", e)
            return None
    return None


# ============================================================================
# OpenjiuwenAgent — (agent_name, session_name) 会话句柄
# ============================================================================

class OpenjiuwenAgent:
    """一个 DeepAgent 的会话句柄。main 与 evaluator 共用本类。"""

    def __init__(
        self,
        deep_agent: Any,
        agent_name: str,
        session_name: str,
        client: "OpenjiuwenClient",
    ):
        self._agent = deep_agent
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name
        self._session_store = SessionStore(store_dir=self._client.session_store.store_dir)
        model_name = (
            self._client.cfg.model.resolved_model
            if self._client.cfg.model and self._client.cfg.model.resolved_model
            else agent_name
        )
        self._session_store.new_session(session_name, model_name)

    def _append_tool_call_details(self, raw_messages: List[Dict[str, Any]]) -> None:
        if not raw_messages:
            return
        try:
            path = self._session_store.store_dir / f"{self.session_name}.trajectory.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                for msg in raw_messages:
                    fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "工具调用详情落盘失败 (agent=%s session=%s): %s",
                self.agent_name, self.session_name, e,
            )

    def _collect_new_messages(self, before: List[Any]) -> List[Dict[str, Any]]:
        try:
            after = _snapshot_context(self._agent, self.session_name)
            if len(after) <= len(before):
                return []
            dumped: List[Dict[str, Any]] = []
            for m in after[len(before):]:
                d = _dump_message(m)
                if d is not None:
                    dumped.append(d)
            return dumped
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "抽取 messages 失败 (agent=%s session=%s): %s",
                self.agent_name, self.session_name, e,
            )
            return []

    async def clear_context(self) -> bool:
        """retry 前清掉当前 session 的全部状态(checkpointer + context)。"""
        react_agent = getattr(self._agent, "react_agent", None)
        clear_session_fn = getattr(react_agent, "clear_session", None)
        if not callable(clear_session_fn):
            return False
        try:
            result = clear_session_fn(session_id=self.session_name)
            if asyncio.iscoroutine(result):
                await result
            logger.info(
                "已清空 session 状态(checkpointer+context) (agent=%s session=%s)",
                self.agent_name, self.session_name,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "clear_session 失败 (agent=%s session=%s): %s",
                self.agent_name, self.session_name, e,
            )
            return False

    async def execute(
        self, query: str, options: Optional[ExecutionOptions] = None
    ) -> ExecutionResult:
        timeout = _positive_timeout(options)
        before_msgs = _snapshot_context(self._agent, self.session_name)
        try:
            invocation = self._agent.invoke(
                {"query": query, "conversation_id": self.session_name}
            )
            response = (
                await asyncio.wait_for(invocation, timeout)
                if timeout is not None
                else await invocation
            )
        except asyncio.TimeoutError:
            return ExecutionResult(
                success=False,
                stop_reason="timeout",
                error_message=f"DeepAgent invocation timed out after {timeout}s",
                messages=self._collect_new_messages(before_msgs),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "DeepAgent invoke 异常 (agent=%s session=%s)",
                self.agent_name, self.session_name,
            )
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=str(e),
                messages=self._collect_new_messages(before_msgs),
            )

        messages = self._collect_new_messages(before_msgs)

        if isinstance(response, dict):
            output = response.get("output", "")
        else:
            output = getattr(response, "output", response)
        content = str(output or "").strip()

        try:
            self._session_store.add_message("user", query)
            self._session_store.add_message("assistant", content)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "轨迹落盘失败 (agent=%s session=%s): %s",
                self.agent_name, self.session_name, e,
            )
        self._append_tool_call_details(messages)

        return ExecutionResult(
            success=bool(content),
            content=content,
            stop_reason="complete" if content else "error",
            error_message=None if content else "DeepAgent returned empty output",
            messages=messages,
        )


# ============================================================================
# OpenjiuwenWorkspaceManager
# ============================================================================

class OpenjiuwenWorkspaceManager(BaseWorkspaceManager):
    """Openjiuwen 工作区:main→base_dir,其余→base_dir-<name>。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        workspace = (
            self.base_dir
            if agent_name == "main"
            else self.base_dir.parent / f"{self.base_dir.name}-{agent_name}"
        )
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _copy_agent_configs(
        self, workspace: Path, config_files: List[str], agent_dir: str
    ) -> None:
        agent_source = Path(agent_dir).expanduser()
        if not agent_source.exists():
            logger.warning("Agent 源目录不存在: %s", agent_source)
            return
        for config_file in config_files:
            src = agent_source / config_file
            if not src.exists():
                logger.warning("Agent 配置文件不存在: %s", src)
                continue
            copy_path(src, workspace / config_file)
            copy_path(src, self.base_dir / config_file)
            logger.info("复制 Agent 配置: %s -> %s / %s", config_file, workspace, self.base_dir)
    

    def build_context_md_prompt(self, workspace: Path) -> str:
        """把 workspace 根目录的 AGENT/SOUL/USER/IDENTITY.md 拼进 system prompt。"""
        parts: List[str] = []
        for filename in ("AGENT.md", "SOUL.md", "USER.md", "IDENTITY.md"):
            md_path = workspace / filename
            try:
                text = md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            content = text.strip()
            if not content:
                continue
            parts.append(f"## {filename}\n\n{content}")
        if not parts:
            return ""
        return "# 上下文配置 (Context Files)\n\n" + "\n\n".join(parts) + "\n"

# ============================================================================
# OpenjiuwenClient
# ============================================================================

class OpenjiuwenClient:
    """进程内 Openjiuwen 客户端。"""

    def __init__(self):
        self.cfg = load_home_config()
        self._deep_agents: Dict[str, Any] = {}
        self._system_prompts: Dict[str, Optional[str]] = {}
        self._agents: Dict[tuple, OpenjiuwenAgent] = {}
        self.gateway = None
        self._session_store = SessionStore(store_dir=DEFAULT_SESSION_STORE_DIR)
        self._mcp_configs: List[McpServerConfig] = self._build_mcp_configs()

        if self.cfg.model or self.cfg.embedding:
            logger.info(
                "已加载全局配置: %s (model=%s, timeout=%ss, max_tokens=%s, "
                "max_iterations=%s, language=%s, paid_search=%s, embedding=%s)",
                OPENJIUWEN_HOME_PATH,
                bool(self.cfg.model),
                self.cfg.timeout,
                self.cfg.max_tokens,
                self.cfg.max_iterations,
                self.cfg.language,
                self.cfg.paid_search,
                bool(self.cfg.embedding),
            )

    def _build_mcp_configs(self) -> List[McpServerConfig]:
        configs: List[McpServerConfig] = [
            McpServerConfig(
                server_id="mcp:playwright",
                server_name="playwright",
                server_path="stdio://playwright",
                client_type="stdio",
                params={
                    "command": "npx",
                    "args": [
                        f"@playwright/mcp@{self.cfg.playwright_mcp_version}",
                        "--browser", "chromium",
                    ],
                },
            )
        ]
        github_token = (
            os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
        )
        if github_token:
            configs.append(
                McpServerConfig(
                    server_id="mcp:github",
                    server_name="github",
                    server_path="stdio://github",
                    client_type="stdio",
                    params={
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": github_token},
                    },
                )
            )
        return configs

    @property
    def session_store(self) -> SessionStore:
        return self._session_store

    async def __aenter__(self) -> "OpenjiuwenClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        for agent_name, deep_agent in list(self._deep_agents.items()):
            configured_rails = getattr(deep_agent, "configured_rails", None)
            unregister_rail = getattr(deep_agent, "unregister_rail", None)
            if not callable(configured_rails) or not callable(unregister_rail):
                continue
            try:
                rails = list(configured_rails() or [])
            except Exception as e:  # noqa: BLE001
                logger.debug("读取 Agent %s 的 rails 失败: %s", agent_name, e)
                continue
            for rail in rails:
                try:
                    result = unregister_rail(rail)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:  # noqa: BLE001
                    logger.debug("注销 Agent %s 的 rail 失败: %s", agent_name, e)
        self._agents.clear()
        self._deep_agents.clear()
        self._system_prompts.clear()
        for cfg in self._mcp_configs:
            try:
                await Runner.resource_mgr.remove_mcp_server(
                    server_id=cfg.server_id, ignore_exception=True
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("注销 MCP server %s 失败: %s", cfg.server_name, e)

    def register_agent(self, agent_name: str, deep_agent: Any, system_prompt: Optional[str]) -> None:
        self._deep_agents[agent_name] = deep_agent
        self._system_prompts[agent_name] = system_prompt

    def get_agent(self, agent_name: str, session_name: str) -> OpenjiuwenAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            if agent_name not in self._deep_agents:
                raise OpenjiuwenError(f"Openjiuwen agent is not configured: {agent_name}")
            self._agents[key] = OpenjiuwenAgent(
                self._deep_agents[agent_name], agent_name, session_name, self
            )
        return self._agents[key]


# ============================================================================
# OpenjiuwenAgentManager
# ============================================================================

class OpenjiuwenAgentManager:
    """把 config.agents[] 逐个注册到 OpenjiuwenClient。"""

    def __init__(
        self,
        client: OpenjiuwenClient,
        workspace_manager: OpenjiuwenWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        cfg = self.client.cfg

        override = self.agent_overrides.get(agent_name)
        if override is not None:
            warn_agent_model_conflict(agent_name, agent_config.model, override)

        # 模型:simulator_config[agent] > home.default
        model_cfg = override or cfg.model
        if model_cfg is None:
            raise OpenjiuwenError(
                f"Missing model configuration for agent: {agent_name} "
                f"(not found in simulator_config / {OPENJIUWEN_HOME_PATH.name}#default)"
            )
        source = "simulator_config" if override is not None else f"{OPENJIUWEN_HOME_PATH.name}#default"
        logger.info(
            "agent=%s 模型来源=%s model=%s timeout=%ss max_tokens=%s",
            agent_name, source, model_cfg.resolved_model, cfg.timeout, cfg.max_tokens,
        )

        workspace = self.workspace_manager.get_agent_workspace(agent_name)
        sys_operation = build_sys_operation(agent_name, workspace)

        # web 工具 + rails
        web_tools = sdk_tools(language=cfg.language, include_paid_search=cfg.paid_search)
        rails: List[Any] = [SysOperationRail(), ContextAssembleRail(), TaskPlanningRail()]
        memory_rail = _build_memory_rail(cfg.embedding)
        if memory_rail is not None:
            rails.append(memory_rail)

        deep_agent = create_deep_agent(
            model=build_model(model_cfg, timeout=cfg.timeout, max_tokens=cfg.max_tokens),
            system_prompt=agent_config.system_prompt,
            tools=web_tools or None,
            rails=rails or None,
            workspace=str(workspace),
            sys_operation=sys_operation,
            skills=agent_config.skills or None,
            restrict_to_work_dir=False,
            **cfg.agent_kwargs,
        )

        # AGENT/SOUL/USER/IDENTITY.md 注入
        context_md = self.workspace_manager.build_context_md_prompt(workspace)
        if context_md:
            react_agent = deep_agent.react_agent
            react_agent.add_prompt_builder_section("context_files", context_md, priority=80)
            logger.info("agent=%s .md 已注入 system prompt", agent_name)

        # MCP browser 工具注入
        if self.client._mcp_configs:
            await _register_mcp_servers(deep_agent, self.client._mcp_configs)

        self.client.register_agent(agent_name, deep_agent, agent_config.system_prompt)
        logger.info(
            "设置 Agent: %s | workspace=%s | skills=%s | web_tools=%s | rails=%s | mcp=%s | extra=%s",
            agent_name, workspace, agent_config.skills or [],
            [t.card.name for t in web_tools] or [],
            len(rails),
            [c.server_name for c in self.client._mcp_configs] or [],
            cfg.agent_kwargs,
        )


# ============================================================================
# 工厂 / executor 注入闭包
# ============================================================================

async def build_openjiuwen_client() -> OpenjiuwenClient:
    logger.info("Openjiuwen 客户端(进程内 DeepAgent 模式) 就绪")
    return OpenjiuwenClient()


def make_openjiuwen_execute_with_retry(client: OpenjiuwenClient) -> Callable:
    async def execute_with_retry(agent, query_text: str, options):
        last_error: Optional[OpenjiuwenError] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result.success and result.content:
                    incomplete = (result.stop_reason or "complete") != "complete"
                    return result, incomplete
                last_error = OpenjiuwenError(
                    result.error_message or "Openjiuwen agent returned empty output"
                )
            except (OpenjiuwenError, asyncio.TimeoutError) as e:
                last_error = OpenjiuwenError(str(e))
            if attempt < EXECUTION_MAX_ATTEMPTS:
                logger.warning(
                    "Openjiuwen invocation failed (%d/%d): %s",
                    attempt, EXECUTION_MAX_ATTEMPTS, last_error,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
                clear_fn = getattr(agent, "clear_context", None)
                if callable(clear_fn):
                    clear_result = clear_fn()
                    if asyncio.iscoroutine(clear_result):
                        await clear_result

        raise last_error or OpenjiuwenError("Openjiuwen retry loop exhausted")

    return execute_with_retry