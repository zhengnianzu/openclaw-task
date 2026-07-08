"""
Claude Code (claude_agent_sdk) 进程内客户端封装

公开 API:
  ClaudecodeClient / ClaudecodeAgent / ExecutionResult / ExecutionOptions / ClaudecodeError
  build_claudecode_client()
  ClaudecodeWorkspaceManager / ClaudecodeAgentManager
  make_claudecode_execute_with_retry / make_claudecode_get_agent
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from src.workspace import BaseWorkspaceManager, copy_path
from src.config import AgentModelConfig, warn_agent_model_conflict

logger = logging.getLogger("harness_automation")


# ============================================================================
# 异常类型
# ============================================================================

class ClaudecodeError(RuntimeError):
    """Claude Code SDK 调用失败 (含底层 ClaudeSDKError / 超时 / 空响应)。"""



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
    session_id: Optional[str] = None
    total_cost_usd: Optional[float] = None

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        data = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
            "session_id": self.session_id,
            "total_cost_usd": self.total_cost_usd,
        }
        if update:
            data.update(update)
        return ExecutionResult(**data)


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


# ============================================================================
# Helper: 把流式 Message 收敛成 (text, result_msg)
# ============================================================================

def _extract_assistant_text(msg: AssistantMessage) -> str:
    """从一个 AssistantMessage 里抽取所有 TextBlock 拼接 (忽略 thinking / tool 块)。"""
    parts: List[str] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts)


# ============================================================================
# ClaudecodeAgent — 一个 (agent_name, session_name) 句柄,内含一个 ClaudeSDKClient
# ============================================================================

class ClaudecodeAgent:
    """对应一个 agent_name + session_name 句柄。

    底层用 ClaudeSDKClient 跑 streaming 模式,这样可以在同一个 session 里多轮交互
    (Claude Code 的会话状态由 CLI 子进程负责维护)。
    """

    def __init__(
        self,
        client: "ClaudecodeClient",
        agent_name: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[Path] = None,
        permission_mode: str = "bypassPermissions",
        extra_options: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name
        self._system_prompt = system_prompt
        self._model = model
        self._cwd: Optional[Path] = Path(cwd).expanduser() if cwd else None
        self._permission_mode = permission_mode
        self._extra_options = dict(extra_options or {})
        self._env = dict(env or {})

        self._sdk: Optional[ClaudeSDKClient] = None
        self._lock = asyncio.Lock()

    def _build_options(self) -> ClaudeAgentOptions:
        kwargs: Dict[str, Any] = {
            "permission_mode": self._permission_mode,
            # 显式告诉 SDK:让 claude CLI 子进程去读 settings.json。
            "setting_sources": ["user", "project"],
        }
        if self._system_prompt:
            kwargs["system_prompt"] = self._system_prompt
        if self._model:
            kwargs["model"] = self._model
        if self._cwd is not None:
            kwargs["cwd"] = str(self._cwd)
        if self._env:
            # claude CLI 子进程环境变量;ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/
            # ANTHROPIC_MODEL 用于按 agent 隔离模型端点。
            kwargs["env"] = dict(self._env)
        # 让上层可以塞 allowed_tools / disallowed_tools / mcp_servers 等
        kwargs.update(self._extra_options)
        return ClaudeAgentOptions(**kwargs)

    async def _ensure_connected(self) -> ClaudeSDKClient:
        if self._sdk is not None:
            return self._sdk
        options = self._build_options()
        sdk = ClaudeSDKClient(options=options)
        try:
            # connect() 不传 prompt → 自动用空 stream,保持连接以便后续 .query()
            await sdk.connect()
        except Exception as e:
            raise ClaudecodeError(
                f"ClaudeSDKClient.connect 失败 (agent={self.agent_name} "
                f"session={self.session_name}): {e}"
            ) from e
        self._sdk = sdk
        logger.debug(
            "ClaudeSDKClient connected: agent=%s session=%s model=%r cwd=%s",
            self.agent_name, self.session_name, self._model, self._cwd,
        )
        return sdk

    async def close(self) -> None:
        if self._sdk is None:
            return
        sdk = self._sdk
        self._sdk = None
        await self._force_kill_subprocess(sdk)

    async def _force_kill_subprocess(self, sdk: ClaudeSDKClient) -> None:
        """SIGKILL CLI 子进程,3s 内等回收,否则交给 OS。"""
        try:
            transport = getattr(sdk, "_transport", None)
            proc = getattr(transport, "_process", None) if transport else None
            if proc is None or getattr(proc, "returncode", None) is not None:
                return
            try:
                proc.kill()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "kill 兜底:proc.wait 仍未返回 (agent=%s),交给 OS",
                    self.agent_name,
                )
        except BaseException as e:  # noqa: BLE001
            logger.debug("kill 异常 (忽略): %s: %s", type(e).__name__, e)

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

        async with self._lock:
            try:
                sdk = await self._ensure_connected()
            except ClaudecodeError as e:
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=str(e),
                )

            async def _run() -> ExecutionResult:
                await sdk.query(query, session_id=self.session_name)

                text_parts: List[str] = []
                last_result: Optional[ResultMessage] = None
                last_assistant_error: Optional[str] = None

                async for msg in sdk.receive_response():
                    if isinstance(msg, AssistantMessage):
                        text_parts.append(_extract_assistant_text(msg))
                        if msg.error:
                            last_assistant_error = msg.error
                    elif isinstance(msg, ResultMessage):
                        last_result = msg
                    elif isinstance(msg, SystemMessage):
                        logger.debug(
                            "[claudecode system] %s %s",
                            msg.subtype, msg.data,
                        )

                content = "".join(text_parts).strip()

                if last_result is None:
                    return ExecutionResult(
                        success=False,
                        content=content,
                        stop_reason="error",
                        error_message=(
                            last_assistant_error
                            or "ClaudeSDKClient 流提前结束,没有收到 ResultMessage"
                        ),
                    )

                if last_result.is_error:
                    return ExecutionResult(
                        success=False,
                        content=content,
                        stop_reason=last_result.subtype or "error",
                        error_message=(
                            last_assistant_error
                            or last_result.result
                            or "Claude Code 返回 is_error=True"
                        ),
                        usage=last_result.usage,
                        session_id=last_result.session_id,
                        total_cost_usd=last_result.total_cost_usd,
                    )

                if not content and last_result.result:
                    content = last_result.result.strip()

                return ExecutionResult(
                    success=True,
                    content=content,
                    stop_reason=last_result.subtype or "complete",
                    usage=last_result.usage,
                    session_id=last_result.session_id,
                    total_cost_usd=last_result.total_cost_usd,
                )

            try:
                if timeout is not None:
                    return await asyncio.wait_for(_run(), timeout=timeout)
                return await _run()
            except asyncio.TimeoutError:
                # 超时后这个 SDK 连接基本废了,主动关掉以便下一次重连
                await self.close()
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="timeout",
                    error_message=f"ClaudeSDKClient.execute timed out after {timeout}s",
                )
            except ClaudeSDKError as e:
                logger.exception(
                    "ClaudeSDKClient 调用失败 (agent=%s session=%s)",
                    self.agent_name, self.session_name,
                )
                await self.close()
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=f"{type(e).__name__}: {e}",
                )
            except Exception as e:
                logger.exception(
                    "ClaudeSDKClient 未知异常 (agent=%s session=%s)",
                    self.agent_name, self.session_name,
                )
                await self.close()
                return ExecutionResult(
                    success=False,
                    content="",
                    stop_reason="error",
                    error_message=str(e),
                )


# ============================================================================
# ClaudecodeClient — 进程内 client,缓存 (agent_name, session_name) → Agent
# ============================================================================

class ClaudecodeClient:
    """Claude Code 进程内客户端。

    保存每个 agent 的默认参数 (system_prompt / model / cwd / extra_options),
    每个 (agent_name, session_name) 持有一个独立的 ClaudeSDKClient 子进程。
    """

    def __init__(self) -> None:
        self._agents: Dict[tuple, ClaudecodeAgent] = {}
        self._agent_defaults: Dict[str, Dict[str, Any]] = {}

    async def __aenter__(self) -> "ClaudecodeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        """逐个关闭所有 agent(单个失败不打断其他)。"""
        for ag in list(self._agents.values()):
            try:
                await ag.close()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:
                logger.debug(
                    "Agent.close 异常 (忽略): agent=%s session=%s %s: %s",
                    ag.agent_name, ag.session_name, type(e).__name__, e,
                )
        self._agents.clear()

    def register_agent_defaults(
        self,
        agent_name: str,
        *,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[Path] = None,
        permission_mode: str = "bypassPermissions",
        extra_options: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """AgentManager 在 setup_agent 时调用,后续 get_agent 可以省参数。"""
        self._agent_defaults[agent_name] = {
            "system_prompt": system_prompt,
            "model": model,
            "cwd": cwd,
            "permission_mode": permission_mode,
            "extra_options": extra_options or {},
            "env": env or {},
        }

    def get_agent(
        self,
        agent_name: str,
        session_name: str,
        *,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[Path] = None,
        permission_mode: Optional[str] = None,
        extra_options: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> ClaudecodeAgent:
        key = (agent_name, session_name)
        if key in self._agents:
            return self._agents[key]

        defaults = self._agent_defaults.get(agent_name, {})
        merged_extra = dict(defaults.get("extra_options") or {})
        if extra_options:
            merged_extra.update(extra_options)
        merged_env = dict(defaults.get("env") or {})
        if env:
            merged_env.update(env)

        agent = ClaudecodeAgent(
            client=self,
            agent_name=agent_name,
            session_name=session_name,
            system_prompt=system_prompt or defaults.get("system_prompt"),
            model=model or defaults.get("model"),
            cwd=cwd or defaults.get("cwd"),
            permission_mode=(
                permission_mode
                or defaults.get("permission_mode")
                or "bypassPermissions"
            ),
            extra_options=merged_extra,
            env=merged_env,
        )
        self._agents[key] = agent
        return agent


# ============================================================================
# 工厂函数
# ============================================================================

async def build_claudecode_client(**_ignored_legacy_kwargs: Any) -> ClaudecodeClient:
    if _ignored_legacy_kwargs:
        logger.debug(
            "build_claudecode_client: 忽略以下旧的 HTTP 参数: %s",
            sorted(_ignored_legacy_kwargs.keys()),
        )
    client = ClaudecodeClient()
    logger.info(
        "Claude Code 客户端 (claude_agent_sdk / SubprocessCLI) 就绪;"
        "需要 claude CLI 已安装并能拉起 (claude --version)。"
    )
    return client


# ============================================================================
# 重试常量
# ============================================================================

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60


class ClaudecodeWorkspaceManager(BaseWorkspaceManager):
    """Claude Code 工作空间管理器: workspace 直接给 ClaudeAgentOptions.cwd 用。"""

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
        agent_name: str,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        agent_source_root = Path(agent_dir).expanduser()
        per_agent_dir = agent_source_root / agent_name

        if not agent_source_root.exists():
            logger.warning("Agent 源目录不存在: %s", agent_source_root)
            return

        merged_sections: List[str] = []

        for config_file in config_files:
            src = per_agent_dir / config_file
            if not src.exists():
                legacy = agent_source_root / config_file
                if legacy.exists():
                    logger.warning(
                        "未找到 %s,回退到旧布局 %s — 建议把文件放到 <agent_dir>/<agent_name>/ 下",
                        src, legacy,
                    )
                    src = legacy
                else:
                    logger.warning(
                        "Agent 配置文件不存在: %s (也不在旧布局 %s)",
                        src, legacy,
                    )
                    continue

            dst = workspace / config_file
            copy_path(src, dst)
            logger.info("复制 Agent 配置: %s -> %s", src, dst)

# ============================================================================
# ClaudecodeAgentManager
# ============================================================================

class ClaudecodeAgentManager:
    """Claude Code Agent 管理器: 把 AgentConfigItem → ClaudecodeClient 默认参数。

    没有远程 gateway 概念,setup_agent 只做:
      1. 解析 workspace
      2. 把 system_prompt / model / cwd 注册到 client._agent_defaults
    """

    def __init__(
        self,
        client: ClaudecodeClient,
        workspace_manager: ClaudecodeWorkspaceManager,
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
        workspace = self.workspace_manager.get_agent_workspace(agent_name)

        # 有 override 的 agent (如 evaluator): setting_sources=["project"] → 跳过 ~/.claude/settings.json
        # 没 override 的 agent (如 assistant1): 走原路径 (SDK 默认 ["user", "project"] 或类似) → 继续继承 ~/.claude/settings.json 里的根目录配置
        env: Dict[str, str] = {}
        extra_options: Dict[str, Any] = {}
        effective_model = getattr(agent_config, "model", None)
        if override:
            if override.model:
                effective_model = override.model
                env["ANTHROPIC_MODEL"] = override.model
            if override.base_url:
                env["ANTHROPIC_BASE_URL"] = override.base_url
            if override.api_key:
                env["ANTHROPIC_AUTH_TOKEN"] = override.api_key
            # 只读 project 段(通常不存在) → user 段完全跳过 → env 注入生效
            extra_options["setting_sources"] = ["project"]

        self.client.register_agent_defaults(
            agent_name=agent_name,
            system_prompt=getattr(agent_config, "system_prompt", None),
            model=effective_model,
            cwd=workspace,
            permission_mode="bypassPermissions",
            extra_options=extra_options or None,
            env=env or None,
        )


# ============================================================================
# make_claudecode_execute_with_retry — 供 src.executor.execute_queries 注入
# ============================================================================

def make_claudecode_execute_with_retry(
    client: ClaudecodeClient,
    workspace_manager: Optional[ClaudecodeWorkspaceManager] = None,
):
    """返回 claudecode 专用的 execute_with_retry 闭包 (简单重试,无 history fallback)。

    返回 `(result, evidence_incomplete)`,签名与 OpenClaw 对齐:
    - 本地直连正常返回 → `(result, False)`;
    - stop_reason 为 timeout/error 但能拿到部分 content → `(result, True)`,提示下游
      evaluator:本轮回复可能被截断,证据缺失不得当负面证据(D5)。
    """

    async def execute_with_retry(agent: ClaudecodeAgent, query_text: str, options):
        last_exc: Optional[BaseException] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise ClaudecodeError("ClaudeSDKClient returned None")
                if result.success and result.content:
                    # stop_reason 非 "complete" 说明本轮被截断/异常返回,标 incomplete
                    evidence_incomplete = (result.stop_reason or "complete") != "complete"
                    return result, evidence_incomplete
                if not result.success:
                    raise ClaudecodeError(
                        result.error_message or "ClaudeSDKClient returned error"
                    )
                raise ClaudecodeError("ClaudeSDKClient returned empty content")
            except (ClaudecodeError, asyncio.TimeoutError) as e:
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
        raise ClaudecodeError("ClaudeSDKClient: unknown error after retries")

    return execute_with_retry


def make_claudecode_get_agent(
    client: ClaudecodeClient,
    workspace_manager: Optional[ClaudecodeWorkspaceManager] = None,
):
    """返回 claudecode 专用的 get_agent_fn 闭包 (cwd 由 workspace_manager 注入)"""

    def get_agent(agent_name: str, session_name: str) -> ClaudecodeAgent:
        cwd = (
            workspace_manager.get_agent_workspace(agent_name)
            if workspace_manager is not None
            else None
        )
        return client.get_agent(agent_name, session_name, cwd=cwd)

    return get_agent

