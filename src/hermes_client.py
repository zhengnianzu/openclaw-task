"""
Hermes Agent 进程内客户端封装 (Python 库模式)

直接 import hermes-agent 的 AIAgent 类,在同一个 Python 进程里跑 agent。

公开 API:
  HermesClient / HermesAgent / ExecutionResult / ExecutionOptions / HermesError
  build_hermes_client()
  HermesWorkspaceManager / HermesAgentManager / hermes_execute_queries()
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import random
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from run_agent import AIAgent as _AIAgent  # noqa: F401

from src.workspace import BaseWorkspaceManager

logger = logging.getLogger("harness_automation")


# ---------------------------------------------------------------------------
# AIAgent 延迟加载
# ---------------------------------------------------------------------------

_HERMES_AGENT_ROOT_ENV = "HERMES_AGENT_ROOT"
_HERMES_AGENT_ROOT_DEFAULT = "/home/ma-user/.hermes/hermes-agent"
_AIAgent_cls = None  # type: ignore[assignment]


def _hermes_agent_root() -> str:
    return os.environ.get(_HERMES_AGENT_ROOT_ENV, _HERMES_AGENT_ROOT_DEFAULT)


def _import_AIAgent():
    global _AIAgent_cls
    if _AIAgent_cls is not None:
        return _AIAgent_cls

    hermes_path = _hermes_agent_root()
    if hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)

    saved_utils = {}
    for key in list(sys.modules):
        if key == "utils" or key.startswith("utils."):
            saved_utils[key] = sys.modules.pop(key)

    try:
        run_agent_mod = importlib.import_module("run_agent")
    except Exception as e:
        sys.modules.update(saved_utils)
        raise HermesError(
            f"无法 import hermes-agent.run_agent (检查 {hermes_path} 是否存在,"
            f"以及 pip 依赖是否齐全): {e}"
        ) from e

    _AIAgent_cls = getattr(run_agent_mod, "AIAgent")
    logger.debug("AIAgent imported from %s", hermes_path)
    return _AIAgent_cls


# ---------------------------------------------------------------------------
# hermes profile API 延迟加载
# ---------------------------------------------------------------------------

_hermes_profiles_mod = None  # type: ignore[assignment]


def _import_hermes_profiles():
    global _hermes_profiles_mod
    if _hermes_profiles_mod is not None:
        return _hermes_profiles_mod
    hermes_path = os.environ.get("HERMES_AGENT_ROOT", _HERMES_AGENT_ROOT_DEFAULT)
    if hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)

    saved_utils = {}
    for key in list(sys.modules):
        if key == "utils" or key.startswith("utils."):
            saved_utils[key] = sys.modules.pop(key)

    try:
        mod = importlib.import_module("hermes_cli.profiles")
    except Exception as e:
        sys.modules.update(saved_utils)
        raise RuntimeError(
            f"无法 import hermes_cli.profiles (检查 {hermes_path}): {e}"
        ) from e
    _hermes_profiles_mod = mod
    return mod


# ============================================================================
# 异常类型
# ============================================================================

class HermesError(RuntimeError):
    """Hermes agent 调用失败。"""


HermesGatewayError = HermesError


# ============================================================================
# config.yaml 读取
# ============================================================================

def _hermes_home_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    return Path(home).expanduser() if home else Path.home() / ".hermes"


def _global_hermes_home() -> Path:
    return Path.home() / ".hermes"


def _load_aiagent_kwargs_from_config(
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    cfg_path = config_path or (_global_hermes_home() / "config.yaml")
    if not cfg_path.is_file():
        logger.warning(
            "config.yaml 不存在: %s — AIAgent 将以全空参数初始化, "
            "请求大概率会 503", cfg_path,
        )
        return {}

    try:
        import yaml
    except ImportError as e:
        raise HermesError(
            "需要 PyYAML 才能读 ~/.hermes/config.yaml: pip install pyyaml"
        ) from e

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise HermesError(f"读 config.yaml 失败 ({cfg_path}): {e}") from e

    model_section = data.get("model") or {}
    if not isinstance(model_section, dict):
        return {}

    model_name = (
        model_section.get("model")
        or model_section.get("default")
        or ""
    )
    provider = model_section.get("provider") or None
    base_url = model_section.get("base_url") or None
    api_key = model_section.get("api_key") or None

    if isinstance(provider, str) and provider.startswith("custom:"):
        pname = provider.split(":", 1)[1].strip()
        cps = data.get("custom_providers") or {}
        cp = cps.get(pname) if isinstance(cps, dict) else None
        if isinstance(cp, dict):
            base_url = base_url or cp.get("base_url")
            api_key = api_key or cp.get("api_key")
            if not model_name:
                ms = cp.get("models")
                if isinstance(ms, list) and ms:
                    model_name = ms[0]

    out: Dict[str, Any] = {}
    if model_name:
        out["model"] = model_name
    if provider:
        out["provider"] = provider
    if base_url:
        out["base_url"] = base_url
    if api_key:
        out["api_key"] = api_key
    return out


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
# HermesAgent
# ============================================================================

class HermesAgent:
    """对应一个 (agent_name, session_name) 句柄,内含独立 AIAgent 实例。"""

    def __init__(
        self,
        client: "HermesClient",
        agent_name: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        hermes_home: Optional[Path] = None,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name
        self._system_prompt = system_prompt
        self.hermes_home: Optional[Path] = Path(hermes_home).expanduser() if hermes_home else None
        self._agent: Optional[Any] = None
        self._history: List[Dict[str, Any]] = []

    def _enter_hermes_home(self):
        if self.hermes_home is None:
            return None
        token = None
        try:
            sys.path.insert(0, _hermes_agent_root())
            from hermes_constants import set_hermes_home_override  # type: ignore
            token = set_hermes_home_override(self.hermes_home)
        except Exception as e:
            logger.debug("set_hermes_home_override 不可用 (忽略): %s", e)
        prev_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        return (token, prev_env)

    def _exit_hermes_home(self, handle) -> None:
        if handle is None:
            return
        token, prev_env = handle
        if token is not None:
            try:
                from hermes_constants import reset_hermes_home_override  # type: ignore
                reset_hermes_home_override(token)
            except Exception as e:
                logger.debug("reset_hermes_home_override 失败 (忽略): %s", e)
        if prev_env is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_env

    def _ensure_agent(self):
        if self._agent is not None:
            return self._agent
        AIAgent = _import_AIAgent()
        ctor_kwargs = _load_aiagent_kwargs_from_config()
        try:
            self._agent = AIAgent(**ctor_kwargs)
        except Exception as e:
            raise HermesError(
                f"AIAgent 初始化失败 (检查 ~/.hermes/config.yaml 的 model 段): {e}"
            ) from e
        logger.debug(
            "AIAgent created for agent=%s session=%s hermes_home=%s "
            "(model=%r provider=%r base_url=%r)",
            self.agent_name, self.session_name, self.hermes_home,
            ctor_kwargs.get("model"), ctor_kwargs.get("provider"),
            ctor_kwargs.get("base_url"),
        )
        return self._agent

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        handle = self._enter_hermes_home()
        try:
            agent = self._ensure_agent()

            def _call() -> Dict[str, Any]:
                return agent.run_conversation(
                    user_message=query,
                    system_message=self._system_prompt,
                    conversation_history=self._history,
                )

            timeout = (
                float(options.timeout_seconds)
                if options and options.timeout_seconds
                else None
            )

            try:
                if timeout is not None:
                    result_dict = await asyncio.wait_for(
                        asyncio.to_thread(_call), timeout=timeout
                    )
                else:
                    result_dict = await asyncio.to_thread(_call)
            except asyncio.TimeoutError:
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="timeout",
                    error_message=f"AIAgent run timed out after {timeout}s",
                )
            except Exception as e:
                logger.exception(
                    "AIAgent.run_conversation 异常 (agent=%s session=%s)",
                    self.agent_name, self.session_name,
                )
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=str(e),
                )

            if not isinstance(result_dict, dict):
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=f"AIAgent returned non-dict: {type(result_dict).__name__}",
                )

            final_text = result_dict.get("final_response", "") or ""

            if final_text:
                self._history.append({"role": "user", "content": query})
                self._history.append({"role": "assistant", "content": final_text})

            return ExecutionResult(
                success=True,
                content=final_text,
                stop_reason=result_dict.get("stop_reason") or "complete",
                usage=result_dict.get("usage"),
            )
        finally:
            self._exit_hermes_home(handle)


# ============================================================================
# HermesClient
# ============================================================================

class HermesClient:
    """进程内 Hermes 客户端。"""

    def __init__(self):
        self._agents: Dict[tuple, HermesAgent] = {}

    async def __aenter__(self) -> "HermesClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        for ag in self._agents.values():
            if ag._agent is not None:
                try:
                    await asyncio.to_thread(ag._agent.close)
                except Exception as e:
                    logger.debug("AIAgent.close 异常 (忽略): %s", e)
        self._agents.clear()

    def get_agent(
        self,
        agent_name: str,
        session_name: str,
        *,
        system_prompt: Optional[str] = None,
        hermes_home: Optional[Path] = None,
    ) -> HermesAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = HermesAgent(
                client=self,
                agent_name=agent_name,
                session_name=session_name,
                system_prompt=system_prompt,
                hermes_home=hermes_home,
            )
        return self._agents[key]


# ============================================================================
# 工厂函数
# ============================================================================

async def build_hermes_client(**_ignored_legacy_kwargs: Any) -> HermesClient:
    if _ignored_legacy_kwargs:
        logger.debug(
            "build_hermes_client: 忽略以下旧的 HTTP 参数: %s",
            sorted(_ignored_legacy_kwargs.keys()),
        )
    client = HermesClient()
    logger.info(
        "Hermes 客户端 (进程内 AIAgent 模式) 就绪;模型与 provider 由 "
        "~/.hermes/config.yaml 决定。"
    )
    return client


# ============================================================================
# 通用重试
# ============================================================================

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER = 0.5

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60


async def with_backoff_retry(
    coro_fn,
    *args,
    max_attempts: int = 5,
    initial_delay: float = _BACKOFF_INITIAL,
    max_delay: float = _BACKOFF_MAX,
    exceptions: tuple = (HermesError, asyncio.TimeoutError),
    **kwargs,
):
    delay = initial_delay
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except exceptions as e:
            last_exc = e
            if attempt >= max_attempts:
                raise
            jitter = random.uniform(-_BACKOFF_JITTER * delay, _BACKOFF_JITTER * delay)
            wait = min(delay + jitter, max_delay)
            logger.warning(
                "调用失败 (第 %d/%d 次): %s; %.1fs 后重试",
                attempt, max_attempts, e, wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, max_delay)
    if last_exc is not None:
        raise last_exc


# ============================================================================
# HermesWorkspaceManager
# ============================================================================

_PERSONA_DST: Dict[str, Path] = {
    "SOUL.md":   Path("SOUL.md"),
    "USER.md":   Path("memories/USER.md"),
    "MEMORY.md": Path("memories/MEMORY.md"),
}


class HermesWorkspaceManager(BaseWorkspaceManager):
    """Hermes 工作空间管理器: 走 hermes profile API,路径为 ~/.hermes/profiles/<name>"""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path.home() / ".hermes" / "profiles"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._ensured: set = set()

    def get_agent_workspace(self, agent_name: str) -> Path:
        if agent_name == "main":
            workspace = Path.home() / ".hermes"
            self._ensured.add("main")
            return workspace

        profiles = _import_hermes_profiles()
        canon = profiles.normalize_profile_name(agent_name)
        if canon == "default":
            canon = "agent-default"
            logger.warning(
                "agent_name 'default' 是 hermes 保留名, 自动改用 profile 'agent-default'",
            )

        workspace: Path = profiles.get_profile_dir(canon)
        if canon in self._ensured and workspace.is_dir():
            return workspace

        if not workspace.is_dir():
            try:
                workspace = profiles.create_profile(
                    name=canon,
                    no_alias=True,
                    no_skills=True,
                )
                logger.info(
                    "创建 hermes profile: %s -> %s",
                    canon, workspace,
                )
            except FileExistsError:
                workspace = profiles.get_profile_dir(canon)
            except Exception as e:
                raise RuntimeError(f"创建 hermes profile '{canon}' 失败: {e}") from e

        (workspace / "memories").mkdir(exist_ok=True)
        (workspace / "skills").mkdir(exist_ok=True)
        self._ensured.add(canon)
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
                    dst = workspace / _PERSONA_DST.get(config_file, Path(config_file))
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    logger.info("复制 Agent 配置: %s -> %s", config_file, dst)
                else:
                    logger.warning("Agent 配置文件不存在: %s", src)
        else:
            logger.warning("Agent 源目录不存在: %s", agent_source)


# ============================================================================
# HermesAgentManager
# ============================================================================

class HermesAgentManager:
    """Hermes Agent 管理器 — 仅验证 profile 存在"""

    def __init__(self, client: HermesClient, workspace_manager: HermesWorkspaceManager):
        self.client = client
        self.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        logger.info("设置 Agent: %s", agent_name)
        self.workspace_manager.get_agent_workspace(agent_name)


# ============================================================================
# make_hermes_execute_with_retry — 供 src.executor.execute_queries 注入
# ============================================================================

def make_hermes_execute_with_retry(client: HermesClient, workspace_manager: Optional[HermesWorkspaceManager] = None):
    """返回 hermes 专用的 execute_with_retry 闭包 (简单重试,无 history fallback)"""

    async def execute_with_retry(agent, query_text: str, options):
        last_exc: Optional[BaseException] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise HermesError("AIAgent returned None")
                if result.success and result.content:
                    return result
                if not result.success:
                    raise HermesError(result.error_message or "AIAgent returned error")
                raise HermesError("AIAgent returned empty content")
            except (HermesError, asyncio.TimeoutError) as e:
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
        raise HermesError("AIAgent: unknown error after retries")

    return execute_with_retry


def make_hermes_get_agent(client: HermesClient, workspace_manager: Optional[HermesWorkspaceManager] = None):
    """返回 hermes 专用的 get_agent_fn 闭包 (含 hermes_home 注入)"""

    def get_agent(agent_name: str, session_name: str):
        hermes_home = (
            workspace_manager.get_agent_workspace(agent_name)
            if workspace_manager is not None else None
        )
        return client.get_agent(agent_name, session_name, hermes_home=hermes_home)

    return get_agent
