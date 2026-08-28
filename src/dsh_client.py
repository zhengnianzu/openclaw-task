"""DeepSeek Harness Python SDK 客户端。

每个 ``(agent_name, session_name)`` 维护一个长期运行的官方 JSON-RPC runtime。
模型、endpoint 和凭证由 DeepSeek Harness 配置管理；本模块只负责会话、
workspace、超时和统一执行结果的适配。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from deepseek_harness import DeepSeekHarness, RunResult

from src.deepseek_stream_bridge import DeepSeekNonstreamBridge
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


class DeepSeekHarnessError(RuntimeError):
    """DeepSeek Harness SDK、运行时或返回结果不可用。"""


@dataclass
class ExecutionResult:
    """统一执行器所需的 DeepSeek 单轮执行结果。"""

    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)
    session_id: Optional[str] = None
    model_provider: Optional[str] = None

    def model_copy(
        self, *, update: Optional[Dict[str, Any]] = None
    ) -> "ExecutionResult":
        data = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
            "session_id": self.session_id,
            "model_provider": self.model_provider,
        }
        if update:
            data.update(update)
        return ExecutionResult(**data)


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


@dataclass
class _AgentDefaults:
    system_prompt: Optional[str]
    model: str
    model_provider: str
    cwd: Path


@dataclass
class DeepSeekConfig:
    """传给官方 SDK 和 ``llm-pi-ai`` 的单文件配置。"""

    model: str = "deepseek-v4-flash"
    provider: str = "deepseek-official"
    tools: bool = True
    providers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    nonstream: List[str] = field(default_factory=list)


HarnessFactory = Callable[..., DeepSeekHarness]


def resolve_deepseek_workspace_root(value: Optional[str] = None) -> Path:
    """解析 DeepSeek harness 的 workspace 根目录。"""

    configured = (
        value
        or os.environ.get("DEEPSEEK_HARNESS_WORKSPACE")
        or "~/.deepseek-harness/workspace"
    )
    return Path(configured).expanduser().resolve()


def resolve_deepseek_session_root(value: Optional[str] = None) -> Path:
    """解析 DeepSeek Harness JSONL session 的独立存储目录。"""

    configured = (
        value
        or os.environ.get("DEEPSEEK_HARNESS_SESSION_ROOT")
        or "~/.deepseek-harness/sessions"
    )
    return Path(configured).expanduser().resolve()


def resolve_deepseek_cordis(value: Optional[str] = None) -> Path:
    """解析本项目用于注入 Agent system prompt 的 Cordis 配置。"""

    configured = value or os.environ.get("DEEPSEEK_HARNESS_CORDIS")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().with_name("deepseek_harness.cordis.yml")


def resolve_deepseek_config(value: Optional[str] = None) -> Optional[Path]:
    """解析可选的 DeepSeek Harness 单文件配置。"""

    configured = value or os.environ.get("DEEPSEEK_HARNESS_CONFIG")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise DeepSeekHarnessError(f"DeepSeek Harness 配置不存在: {path}")
        return path

    default_path = Path("~/.deepseek-harness/config.yml").expanduser().resolve()
    return default_path if default_path.is_file() else None


def _safe_name(value: str) -> str:
    """将逻辑名称转换为单个路径片段，避免 ``/`` 等字符改变 session 目录层级。"""

    directory = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not directory:
        raise DeepSeekHarnessError(f"无法生成 session 目录名: {value!r}")
    return directory


def _extract_usage(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """累加同一 turn 各 step 的官方 token usage。"""

    totals: Dict[str, Any] = {}
    for event in events:
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data")
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals or None


def _turn_error(events: List[Dict[str, Any]], finish_reason: Optional[str]) -> str:
    """SDK 结果没有 error 字段，只能从最后一个 ``turn/end`` 事件取得错误详情。"""

    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        if not isinstance(reason, dict):
            break
        for key in ("message", "detail", "error"):
            value = reason.get(key)
            if value:
                return (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
        break
    return f"DeepSeek Harness 未正常完成: finish_reason={finish_reason}"


class DeepSeekAgent:
    """一个逻辑 Agent 会话，对应一个长期复用的官方 SDK runtime。"""

    def __init__(
        self,
        client: "DeepSeekClient",
        agent_name: str,
        session_name: str,
        defaults: _AgentDefaults,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = session_name
        self.session_id = f"{_safe_name(agent_name)}-{_safe_name(session_name)}"
        self._defaults = defaults
        self._harness: Optional[DeepSeekHarness] = None

    def _ensure_session_cwd(self) -> Path:
        session_cwd = self._defaults.cwd / ".sessions" / _safe_name(
            self.session_name
        )
        if not session_cwd.exists():
            session_cwd.mkdir(parents=True)
            for source in self._defaults.cwd.iterdir():
                if source.name == ".sessions":
                    continue
                copy_path(source, session_cwd / source.name)
        if self._client.workspace_manager is not None:
            self._client.workspace_manager.activate_session(
                self.agent_name, session_cwd
            )
        return session_cwd

    def _ensure_harness(self, cwd: Path) -> DeepSeekHarness:
        if self._harness is not None:
            return self._harness

        session_root = (
            self._client.session_root
            / _safe_name(self.agent_name)
            / _safe_name(self.session_name)
        )
        session_root.mkdir(parents=True, exist_ok=True)
        env = {
            "DSH_HARNESS_TOOLS_ENABLED": (
                "1" if self._client.config.tools else "0"
            ),
            "DSH_LLM_PI_AI_PROVIDERS": json.dumps(
                self._client.config.providers,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        if self._defaults.system_prompt:
            env["DSH_SYSTEM_PROMPT"] = self._defaults.system_prompt

        self._harness = self._client._harness_factory(
            provider=self._defaults.model_provider,
            model=self._defaults.model,
            cwd=str(cwd),
            runtime_cwd=str(cwd),
            session_root=str(session_root),
            cordis=str(self._client.cordis_path),
            env=env,
        )
        logger.info(
            "DeepSeek runtime 已创建: agent=%s session=%s provider=%s model=%s cwd=%s",
            self.agent_name,
            self.session_name,
            self._defaults.model_provider,
            self._defaults.model,
            cwd,
        )
        return self._harness

    async def _close_runtime(self) -> None:
        harness = self._harness
        self._harness = None
        if harness is None:
            return
        try:
            await asyncio.to_thread(harness.close)
        except Exception as exc:
            logger.debug(
                "DeepSeek runtime 关闭失败: agent=%s session=%s error=%s",
                self.agent_name,
                self.session_name,
                exc,
            )

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        timeout = (
            float(options.timeout_seconds)
            if options and options.timeout_seconds
            else None
        )

        try:
            cwd = self._ensure_session_cwd()
            harness = self._ensure_harness(cwd)
            run = asyncio.to_thread(
                harness.run, query, session_id=self.session_id
            )
            raw: RunResult = (
                await asyncio.wait_for(run, timeout=timeout)
                if timeout is not None
                else await run
            )
        except asyncio.TimeoutError:
            await self._close_runtime()
            return ExecutionResult(
                success=False,
                stop_reason="timeout",
                error_message=f"DeepSeek Harness turn timed out after {timeout}s",
                session_id=self.session_id,
                model_provider=self._defaults.model_provider,
            )
        except asyncio.CancelledError:
            await self._close_runtime()
            raise
        except Exception as exc:
            await self._close_runtime()
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=str(exc),
                session_id=self.session_id,
                model_provider=self._defaults.model_provider,
            )

        content = raw.final_response.strip()
        finish_reason = raw.finish_reason
        stop_reason = "complete" if finish_reason == "completed" else finish_reason
        failed = finish_reason in {"error", "cancelled", "aborted"}
        error = _turn_error(raw.events, finish_reason) if failed else None
        if not content:
            error = error or "DeepSeek Harness 未返回最终文本"
        if not stop_reason:
            error = error or "DeepSeek Harness 未返回 finish_reason"
            stop_reason = "error"

        return ExecutionResult(
            success=error is None,
            content=content,
            stop_reason=stop_reason,
            error_message=error,
            usage=_extract_usage(raw.events),
            session_id=raw.session_id,
            model_provider=self._defaults.model_provider,
        )

    async def close(self) -> None:
        await self._close_runtime()


class DeepSeekClient:
    """管理多个按 Agent/session 隔离的 DeepSeek SDK runtime。"""

    def __init__(
        self,
        *,
        session_root: Optional[Path] = None,
        cordis_path: Optional[Path] = None,
        harness_factory: HarnessFactory = DeepSeekHarness,
        config: Optional[DeepSeekConfig] = None,
    ):
        self.session_root = (
            session_root or resolve_deepseek_session_root()
        ).expanduser().resolve()
        self.cordis_path = (
            cordis_path or resolve_deepseek_cordis()
        ).expanduser().resolve()
        self._harness_factory = harness_factory
        self.config = config or DeepSeekConfig()
        self._agents: Dict[tuple[str, str], DeepSeekAgent] = {}
        self._agent_defaults: Dict[str, _AgentDefaults] = {}
        self.workspace_manager: Optional[DeepSeekWorkspaceManager] = None
        self._nonstream_bridge: Optional[DeepSeekNonstreamBridge] = None

    async def __aenter__(self) -> "DeepSeekClient":
        if not self.cordis_path.is_file():
            raise DeepSeekHarnessError(
                f"DeepSeek Cordis 配置不存在: {self.cordis_path}"
            )
        await self._activate_nonstream_bridge()
        logger.info(
            "DeepSeek Harness SDK 已就绪: cordis=%s session_root=%s provider=%s model=%s",
            self.cordis_path,
            self.session_root,
            self.config.provider,
            self.config.model,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await asyncio.gather(
            *(agent.close() for agent in self._agents.values()),
            return_exceptions=True,
        )
        self._agents.clear()
        bridge, self._nonstream_bridge = self._nonstream_bridge, None
        if bridge is not None:
            await bridge.close()

    async def _activate_nonstream_bridge(self) -> None:
        if not self.config.nonstream:
            return
        upstreams = {
            provider: self.config.providers[provider]["baseURL"]
            for provider in self.config.nonstream
        }
        bridge = DeepSeekNonstreamBridge(upstreams)
        await bridge.start()
        for provider in self.config.nonstream:
            self.config.providers[provider]["baseURL"] = bridge.base_url(provider)
        self._nonstream_bridge = bridge
        logger.info(
            "DeepSeek nonstream bridge 已启用: providers=%s",
            sorted(upstreams),
        )

    def register_agent_defaults(
        self,
        agent_name: str,
        *,
        system_prompt: Optional[str],
        model: Optional[str],
        model_provider: Optional[str],
        cwd: Path,
    ) -> None:
        self._agent_defaults[agent_name] = _AgentDefaults(
            system_prompt=system_prompt,
            model=model or self.config.model,
            model_provider=model_provider or self.config.provider,
            cwd=cwd,
        )

    def get_agent(self, agent_name: str, session_name: str) -> DeepSeekAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            defaults = self._agent_defaults.get(agent_name)
            if defaults is None:
                raise DeepSeekHarnessError(
                    f"DeepSeek agent 尚未注册: {agent_name}"
                )
            self._agents[key] = DeepSeekAgent(
                self, agent_name, session_name, defaults
            )
        return self._agents[key]


async def build_deepseek_client(
    *,
    session_root: Optional[str] = None,
    cordis_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> DeepSeekClient:
    """使用官方 Python SDK 和可选的单文件配置创建客户端。"""

    resolved_config = resolve_deepseek_config(config_path)
    config = DeepSeekConfig()
    if resolved_config is not None:
        config = DeepSeekConfig(
            **(yaml.safe_load(resolved_config.read_text(encoding="utf-8")) or {})
        )

    return DeepSeekClient(
        session_root=resolve_deepseek_session_root(session_root),
        cordis_path=resolve_deepseek_cordis(cordis_path),
        config=config,
    )


class DeepSeekWorkspaceManager(BaseWorkspaceManager):
    """每个 Agent 独立模板，session 独立 cwd，skills 使用 .agents/skills。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.skills_subdir = Path(".agents/skills")
        self._active_sessions: Dict[str, Path] = {}

    def get_agent_template_workspace(self, agent_name: str) -> Path:
        workspace = self.base_dir / agent_name
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / self.skills_subdir).mkdir(parents=True, exist_ok=True)
        return workspace

    def get_agent_workspace(self, agent_name: str) -> Path:
        return self._active_sessions.get(
            agent_name
        ) or self.get_agent_template_workspace(agent_name)

    def activate_session(self, agent_name: str, workspace: Path) -> None:
        self._active_sessions[agent_name] = workspace

    def get_skills_dst(self, workspace: Path) -> Path:
        return workspace / self.skills_subdir

    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        source_dir = Path(agent_dir).expanduser()
        if not source_dir.is_dir():
            logger.warning("Agent 源目录不存在: %s", source_dir)
            return
        for config_file in config_files:
            source = source_dir / config_file
            if not source.exists():
                logger.warning("Agent 配置文件不存在: %s", source)
                continue
            target = workspace / config_file
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_path(source, target)
            logger.info("复制 Agent 配置: %s -> %s", config_file, target)


class DeepSeekAgentManager:
    """把项目 Agent 配置注册到 DeepSeekClient。"""

    def __init__(
        self,
        client: DeepSeekClient,
        workspace_manager: DeepSeekWorkspaceManager,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        model = agent_config.model
        model_provider = None
        if model and "/" in model:
            model_provider, model = model.split("/", 1)
        if model is None:
            model = self.client.config.model
        if model_provider is None:
            model_provider = self.client.config.provider
        workspace = self.workspace_manager.get_agent_template_workspace(agent_name)
        self.client.register_agent_defaults(
            agent_name,
            system_prompt=agent_config.system_prompt,
            model=model,
            model_provider=model_provider,
            cwd=workspace,
        )
        logger.info(
            "设置 DeepSeek Agent: %s | provider=%s model=%s workspace=%s",
            agent_name,
            model_provider,
            model,
            workspace,
        )


async def execute_deepseek(agent: DeepSeekAgent, query_text: str, options):
    """执行一次请求；可能已有文件副作用，因此失败时不自动重放。"""

    result = await agent.execute(query_text, options=options)
    if result.success and result.content:
        incomplete = (result.stop_reason or "complete") != "complete"
        return result, incomplete
    raise DeepSeekHarnessError(
        result.error_message or "DeepSeek Harness 返回空结果"
    )
