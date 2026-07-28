"""Openjiuwen 进程内 DeepAgent 客户端。

与 hermes_client / claudecode_client 同构:进程内 SDK,不走网关。
每个 agent(含 evaluator)统一走 create_deep_agent → DeepAgent.invoke;

模型解析优先级(由高到低):
  1. simulator_config[agent_name]                          per-run 覆盖
  2. ~/.openjiuwen/openjiuwen.json → agents.<agent_name>
  3. ~/.openjiuwen/openjiuwen.json → default

公开 API(executor / harness_automation 依赖):
  OpenjiuwenClient / OpenjiuwenAgent
  ExecutionResult / ExecutionOptions / OpenjiuwenError
  OpenjiuwenWorkspaceManager / OpenjiuwenAgentManager
  build_openjiuwen_client() / make_openjiuwen_execute_with_retry() / make_openjiuwen_get_agent()

环境变量:
  OPENJIUWEN_HOME             全局配置目录(默认 ~/.openjiuwen)
  OPENJIUWEN_SDK_LOG_LEVEL    SDK 日志级别(默认 INFO,保留 LLM 轨迹)
  OPENJIUWEN_SDK_LOG_CONSOLE  "1" 时 SDK 日志回到 console(默认关,只落文件)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


_OPENJIUWEN_HOME_ENV = "OPENJIUWEN_HOME"
_OPENJIUWEN_CONFIG_FILENAME = "openjiuwen.json"


def _openjiuwen_home() -> Path:
    override = os.environ.get(_OPENJIUWEN_HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".openjiuwen"


def _prequiet_openjiuwen_sdk_logs() -> None:
    """在 import openjiuwen.harness 前收敛 SDK 日志。
    """
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
from openjiuwen.core.foundation.llm import (
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperation,
    SysOperationCard,
)

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60

# openjiuwen ProviderType 白名单;不在其中的 provider 一律回退 OpenAI
# (OpenAIModelClient 走 /v1/chat/completions,兼容多数 OpenAI-Compatible 后端)。
_KNOWN_PROVIDERS = {
    "OpenAI", "OpenAIAccount", "OpenRouter", "Anthropic",
    "SiliconFlow", "DashScope", "DeepSeek",
    "InferenceAffinity", "intelli_router", "IntelliRouter",
}


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
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        values = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
            "tool_calls": list(self.tool_calls),
            "files": list(self.files),
        }
        if update:
            values.update(update)
        return ExecutionResult(**values)


# ============================================================================
# 全局配置 (~/.openjiuwen/openjiuwen.json) + 三级模型解析
# ============================================================================

def _load_home_config() -> Dict[str, Any]:
    path = _openjiuwen_home() / _OPENJIUWEN_CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取 %s 失败,忽略全局兜底: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_model_config(raw: Any) -> Optional[AgentModelConfig]:
    if not isinstance(raw, dict):
        return None
    try:
        return AgentModelConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("全局配置 model 段解析失败,忽略: %s", e)
        return None


def _resolve_model_config(
    agent_name: str,
    override: Optional[AgentModelConfig],
    home: Dict[str, Any],
) -> tuple[Optional[AgentModelConfig], str]:
    """simulator_config[agent] > home.agents.<name> > home.default > (None, "unresolved")。"""
    if override is not None:
        return override, "simulator_config"
    agents_raw = home.get("agents")
    agents = agents_raw if isinstance(agents_raw, dict) else {}
    scoped = _coerce_model_config(agents.get(agent_name))
    if scoped is not None:
        return scoped, f"{_OPENJIUWEN_CONFIG_FILENAME}#agents.{agent_name}"
    default = _coerce_model_config(home.get("default"))
    if default is not None:
        return default, f"{_OPENJIUWEN_CONFIG_FILENAME}#default"
    return None, "unresolved"


# ============================================================================
# Model / SysOperation 构造
# ============================================================================

def build_model(config: AgentModelConfig) -> Model:
    """根据 AgentModelConfig 构造 openjiuwen Model。

    provider 不在白名单 / 含斜杠 / 是 URL → 回退 OpenAI;
    http:// 端点自动 verify_ssl=False(否则 SDK 要求 ssl_cert);
    model / base_url / api_key 缺一即报错。
    """
    provider = (config.provider or "").strip() or "OpenAI"
    if provider not in _KNOWN_PROVIDERS or provider.startswith(("http://", "https://")):
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
    }
    timeout = getattr(config, "timeout", None)
    if timeout is not None:
        client_config["timeout"] = timeout
    return Model(
        model_client_config=ModelClientConfig(**client_config),
        model_config=ModelRequestConfig(model=model_name),
    )


def build_sys_operation(agent_name: str, workspace: Path, restrict: bool) -> SysOperation:
    """只能在各自 workspace 里读写文件"""
    card = SysOperationCard(
        id=f"openjiuwen-{agent_name}",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(
            sandbox_root=[str(workspace)],
            restrict_to_sandbox=restrict,
        ),
    )
    return SysOperation(card)

def _positive_timeout(options: Optional[ExecutionOptions]) -> Optional[float]:
    if options is None or options.timeout_seconds is None:
        return None
    timeout = float(options.timeout_seconds)
    return timeout if timeout > 0 else None

# ============================================================================
# OpenjiuwenAgent — (agent_name, session_name) 会话句柄
# ============================================================================

class OpenjiuwenAgent:
    """一个 DeepAgent 的会话句柄。main 与 evaluator 共用本类,差异仅在模型配置。"""

    def __init__(self, deep_agent: Any, agent_name: str, session_name: str, client: "OpenjiuwenClient"):
        self._agent = deep_agent
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name

    async def execute(self, query: str, options: Optional[ExecutionOptions] = None) -> ExecutionResult:
        timeout = _positive_timeout(options)
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
            )

        if isinstance(response, dict):
            output = response.get("output", "")
        else:
            output = getattr(response, "output", response)
        content = str(output or "").strip()
        return ExecutionResult(
            success=bool(content),
            content=content,
            stop_reason="complete" if content else "error",
            error_message=None if content else "DeepAgent returned empty output",
        )


# ============================================================================
# OpenjiuwenWorkspaceManager — 每 agent 独立子目录
# ============================================================================

class OpenjiuwenWorkspaceManager(BaseWorkspaceManager):
    """Openjiuwen 工作区:main→base_dir,其余→base_dir-<name>;SOUL/USER 平铺到根。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        candidate = Path(agent_name)
        workspace = (
            self.base_dir
            if agent_name == "main"
            else self.base_dir.parent / f"{self.base_dir.name}-{agent_name}"
        )
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
# OpenjiuwenClient — 持有 DeepAgent 池与会话包装器
# ============================================================================

class OpenjiuwenClient:
    """进程内 Openjiuwen 客户端。gateway=None 让 executor 的 history fallback 走空。"""

    def __init__(self):
        self._home = _load_home_config()
        self._deep_agents: Dict[str, Any] = {}
        self._system_prompts: Dict[str, Optional[str]] = {}
        self._agents: Dict[tuple, OpenjiuwenAgent] = {}
        self.gateway = None
        if self._home:
            logger.info(
                "已加载全局配置: %s (default=%s, agents=%s)",
                _openjiuwen_home() / _OPENJIUWEN_CONFIG_FILENAME,
                bool(self._home.get("default")),
                sorted((self._home.get("agents") or {}).keys()),
            )

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
                rails = list(configured_rails() or [])  # type: ignore[arg-type]
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

    def resolve_model(self, agent_name: str, override: Optional[AgentModelConfig]) -> tuple[Optional[AgentModelConfig], str]:
        return _resolve_model_config(agent_name, override, self._home)

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
# OpenjiuwenAgentManager — 逐 agent 装配 DeepAgent
# ============================================================================

class OpenjiuwenAgentManager:
    """把 config.agents[] 逐个注册到 OpenjiuwenClient。

    workspace 与 skills 的落盘由 harness_automation._setup_workspaces 统一完成;
    本 manager 只负责解析模型 + 建 DeepAgent。所有 agent(含 evaluator)统一走
    create_deep_agent,evaluator 的差异只体现在 simulator_config 覆盖的模型上。
    """

    def __init__(
        self,
        client: OpenjiuwenClient,
        workspace_manager: OpenjiuwenWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
        restrict_to_work_dir: bool = True,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        self.restrict_to_work_dir = restrict_to_work_dir

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override is not None:
            warn_agent_model_conflict(agent_name, agent_config.model, override)

        model_cfg, source = self.client.resolve_model(agent_name, override)
        if model_cfg is None:
            raise OpenjiuwenError(
                f"Missing model configuration for agent: {agent_name} "
                f"(not found in simulator_config / {_OPENJIUWEN_CONFIG_FILENAME}#agents.{agent_name} / #default)"
            )
        logger.info("agent=%s 模型来源=%s model=%s", agent_name, source, model_cfg.resolved_model)

        # workspace + skills 已由 _setup_workspaces 备好;这里取路径 + 绑沙箱 + 建 DeepAgent。
        workspace = self.workspace_manager.get_agent_workspace(agent_name)
        sys_operation = build_sys_operation(agent_name, workspace, self.restrict_to_work_dir)

        deep_agent = create_deep_agent(
            model=build_model(model_cfg),
            system_prompt=agent_config.system_prompt,
            workspace=str(workspace),
            sys_operation=sys_operation,
            skills=agent_config.skills or None,
            restrict_to_work_dir=self.restrict_to_work_dir,
        )
        self.client.register_agent(agent_name, deep_agent, agent_config.system_prompt)
        logger.info(
            "设置 Agent: %s | workspace=%s | skills=%s",
            agent_name, workspace, agent_config.skills or [],
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
        raise last_error or OpenjiuwenError("Openjiuwen retry loop exhausted")

    return execute_with_retry
