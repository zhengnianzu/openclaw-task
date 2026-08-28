"""Codex Python SDK harness 客户端。

一个 ``CodexClient`` 维护一个 ``AsyncCodex``/app-server；每个
``(agent_name, session_name)`` 维护一个真实 Codex thread。provider、模型和
密钥由部署前写好的 ``~/.codex/config.toml`` 管理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


class CodexHarnessError(RuntimeError):
    """Codex SDK、配置或返回结果不可用。"""


@dataclass
class ToolCall:
    """一次 Codex 工具调用及其输入、输出和耗时。"""

    tool: str
    input: Any = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)
    session_id: Optional[str] = None
    model_provider: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)

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
            "tool_calls": list(self.tool_calls),
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
    model: Optional[str]
    model_provider: Optional[str]
    cwd: Path


def _usage_dict(usage: Any) -> Optional[Dict[str, Any]]:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        value = usage.model_dump(mode="json", exclude_none=True)
        return value if isinstance(value, dict) else None
    return None


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _item_dict(item: Any) -> Dict[str, Any]:
    """把 SDK 的 ThreadItem RootModel 解包为当前版本的 snake_case 字典。"""
    item = getattr(item, "root", item)
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    return {}


def _extract_codex_tool_calls(result: Any) -> List[ToolCall]:
    """只从当前 TurnResult.items 提取本轮已完成的工具调用。"""
    calls: List[ToolCall] = []
    for raw_item in getattr(result, "items", None) or []:
        item = _item_dict(raw_item)
        item_type = item.get("type")
        duration_ms = item.get("duration_ms")

        if item_type == "commandExecution":
            calls.append(
                ToolCall(
                    tool=item_type,
                    input={"command": item.get("command"), "cwd": item.get("cwd")},
                    output=_json_text(
                        {
                            "status": item.get("status"),
                            "exit_code": item.get("exit_code"),
                            "output": item.get("aggregated_output"),
                        }
                    ),
                    duration_ms=duration_ms,
                )
            )
        elif item_type == "mcpToolCall":
            tool_name = ".".join(
                part for part in [item.get("server"), item.get("tool")] if part
            )
            result_data = item.get("result") or {}
            calls.append(
                ToolCall(
                    tool=tool_name or item_type,
                    input=item.get("arguments"),
                    output=_json_text(
                        {
                            "status": item.get("status"),
                            "content": result_data.get("content"),
                            "structured_content": result_data.get("structured_content"),
                            "error": (item.get("error") or {}).get("message"),
                        }
                    ),
                    duration_ms=duration_ms,
                )
            )
        elif item_type == "dynamicToolCall":
            tool_name = item.get("tool") or item_type
            if item.get("namespace"):
                tool_name = f"{item['namespace']}.{tool_name}"
            calls.append(
                ToolCall(
                    tool=tool_name,
                    input=item.get("arguments"),
                    output=_json_text(
                        {
                            "status": item.get("status"),
                            "success": item.get("success"),
                            "content": item.get("content_items"),
                        }
                    ),
                    duration_ms=duration_ms,
                )
            )
        elif item_type == "collabAgentToolCall":
            calls.append(
                ToolCall(
                    tool=item.get("tool") or item_type,
                    input={
                        "prompt": item.get("prompt"),
                        "model": item.get("model"),
                        "receiver_thread_ids": item.get("receiver_thread_ids"),
                    },
                    output=_json_text(
                        {
                            "status": item.get("status"),
                            "agents_states": item.get("agents_states"),
                        }
                    ),
                )
            )
        elif item_type == "webSearch":
            calls.append(
                ToolCall(
                    tool=item_type,
                    input={"query": item.get("query"), "action": item.get("action")},
                    output=_json_text({"results": item.get("results")}),
                )
            )
        elif item_type == "fileChange":
            calls.append(
                ToolCall(
                    tool=item_type,
                    input={"changes": item.get("changes")},
                    output=_json_text({"status": item.get("status")}),
                )
            )
        elif item_type == "imageGeneration":
            calls.append(
                ToolCall(
                    tool=item_type,
                    input={"revised_prompt": item.get("revised_prompt")},
                    output=_json_text(
                        {
                            "status": item.get("status"),
                            "result": item.get("result"),
                            "saved_path": item.get("saved_path"),
                        }
                    ),
                )
            )
        elif item_type == "imageView":
            calls.append(
                ToolCall(tool=item_type, input={"path": item.get("path")})
            )
        elif item_type == "sleep":
            calls.append(
                ToolCall(
                    tool=item_type,
                    input={"duration_ms": item.get("duration_ms")},
                    duration_ms=duration_ms,
                )
            )
    return calls


class CodexAgent:
    """一个逻辑 Agent 会话，对应一个长期复用的 Codex thread。"""

    def __init__(
        self,
        client: "CodexClient",
        agent_name: str,
        session_name: str,
        defaults: _AgentDefaults,
    ):
        self._client = client
        self.agent_name = agent_name
        # session_name 是框架的逻辑会话名；Codex SDK 真正的会话标识是 thread.id。
        self.session_name = session_name
        self.session_key = session_name
        self._defaults = defaults
        self._thread = None

    async def _ensure_thread(self):
        if self._thread is not None:
            return self._thread

        # thread_start 不接受业务 session_name。下方把它加入 cwd，只负责隔离
        # 各会话的文件；对话上下文仍由 CodexClient 缓存并复用本 _thread 保留。
        session_cwd = self._defaults.cwd / ".sessions" / self.session_name
        session_cwd.mkdir(parents=True, exist_ok=True)
        # Agent workspace 是会话模板。复制时排除 .sessions，避免递归复制其他会话。
        for source in self._defaults.cwd.iterdir():
            if source.name == ".sessions":
                continue
            copy_path(source, session_cwd / source.name)

        sdk = self._client.sdk
        thread = await sdk.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(session_cwd),
            developer_instructions=self._defaults.system_prompt,
            ephemeral=True,
            model=self._defaults.model,
            model_provider=self._defaults.model_provider,
            sandbox=Sandbox.workspace_write,
            service_name="openclaw_task_harness",
        )
        self._thread = thread
        logger.info(
            "Codex thread 已创建: agent=%s session=%s thread=%s provider=%s model=%s",
            self.agent_name,
            self.session_name,
            thread.id,
            self._defaults.model_provider,
            self._defaults.model,
        )
        return thread

    async def _interrupt_turn(self, turn) -> None:
        """通知 app-server 停止当前 turn；中断失败只记录，不覆盖原始结果。"""
        try:
            await turn.interrupt()
        except Exception as exc:
            logger.warning(
                "Codex turn 中断失败: agent=%s session=%s error=%s",
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
            if options and getattr(options, "timeout_seconds", None)
            else None
        )

        turn = None
        try:
            thread = await self._ensure_thread()

            # thread.run(query) 内部也是先创建 turn 再等待结果。这里显式取得句柄，
            # 唯一目的是在超时或任务取消时调用 interrupt() 停止服务端执行。
            turn = await thread.turn(query)
            result = (
                await asyncio.wait_for(turn.run(), timeout=timeout)
                if timeout is not None
                else await turn.run()
            )
        except asyncio.TimeoutError:
            # wait_for 终止本地等待后，再显式通知 app-server 停止该 turn。
            if turn is not None:
                await self._interrupt_turn(turn)
            return ExecutionResult(
                success=False,
                stop_reason="timeout",
                error_message=f"Codex turn timed out after {timeout}s",
                session_id=getattr(self._thread, "id", None),
                model_provider=self._defaults.model_provider,
            )
        except asyncio.CancelledError:
            if turn is not None:
                await self._interrupt_turn(turn)
            raise
        except Exception as exc:
            # 与 timeout/cancel 保持一致:本地失败后显式通知 server 停止 turn,
            # 避免 network drop / JSON 解析等异常时 server 端 turn 还在跑。
            if turn is not None:
                await self._interrupt_turn(turn)
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=str(exc),
                session_id=getattr(self._thread, "id", None),
                model_provider=self._defaults.model_provider,
            )

        content = (result.final_response or "").strip()
        error = getattr(result, "error", None)
        status = getattr(result, "status", None)
        status_text = getattr(status, "value", None) or str(status or "complete")
        success = bool(content) and error is None
        tool_calls = _extract_codex_tool_calls(result)
        return ExecutionResult(
            success=success,
            content=content,
            stop_reason="complete" if success else status_text,
            error_message=(
                str(error) if error else (None if success else "Codex 未返回最终文本")
            ),
            usage=_usage_dict(getattr(result, "usage", None)),
            session_id=thread.id,
            model_provider=self._defaults.model_provider,
            tool_calls=tool_calls,
        )


class CodexClient:
    """管理一个隔离的 AsyncCodex app-server 和多个 Agent/thread。"""

    def __init__(self, codex_home: Path):
        self.codex_home = codex_home
        self._sdk: Optional[AsyncCodex] = None
        self._agents: Dict[tuple[str, str], CodexAgent] = {}
        self._agent_defaults: Dict[str, _AgentDefaults] = {}
        self.workspace_manager: Optional[CodexWorkspaceManager] = None

    @property
    def sdk(self) -> AsyncCodex:
        if self._sdk is None:
            raise CodexHarnessError("CodexClient 尚未启动")
        return self._sdk

    async def __aenter__(self) -> "CodexClient":
        config = CodexConfig(
            env={"CODEX_HOME": str(self.codex_home.resolve())},
            client_name="openclaw_task_harness",
            client_title="OpenClaw Task Harness",
        )
        sdk = AsyncCodex(config=config)
        try:
            await sdk.__aenter__()
        except Exception:
            # 半启动状态下 close() 自身也可能抛
            try:
                await sdk.close()
            except Exception as close_exc:
                logger.warning("CodexClient 启动失败后 close 又出错: %s", close_exc)
            raise
        self._sdk = sdk
        logger.info("Codex SDK 已启动: CODEX_HOME=%s", self.codex_home)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        self._agents.clear()
        sdk = self._sdk
        self._sdk = None
        if sdk is not None:
            await sdk.close()

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
            model=model,
            model_provider=model_provider,
            cwd=cwd,
        )

    def get_agent(self, agent_name: str, session_name: str) -> CodexAgent:
        # session_name 在这里参与会话管理：同一 (agent, session) 命中同一
        # CodexAgent，其 _thread 会被后续 turn 复用；不同 session 创建独立 thread。
        key = (agent_name, session_name)
        if key not in self._agents:
            defaults = self._agent_defaults.get(agent_name)
            if defaults is None:
                raise CodexHarnessError(f"Codex agent 尚未注册: {agent_name}")
            self._agents[key] = CodexAgent(
                self, agent_name, session_name, defaults
            )
        return self._agents[key]


async def build_codex_client(codex_home: str = "~/.codex") -> CodexClient:
    """使用部署阶段已经准备好的标准 Codex 配置目录启动 SDK。"""
    return CodexClient(Path(codex_home).expanduser())


class CodexWorkspaceManager(BaseWorkspaceManager):
    """每个 Agent 独立 workspace，skill 使用 Codex 原生 .agents/skills。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.skills_subdir = Path(".agents/skills")

    def get_agent_workspace(self, agent_name: str) -> Path:
        if agent_name == "main":
            workspace = self.base_dir
        else:
            parent = self.base_dir.parent
            base_name = self.base_dir.name
            workspace = parent / f"{base_name}-{agent_name}"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / self.skills_subdir).mkdir(parents=True, exist_ok=True)
        return workspace
    
    def get_skills_dst(self, workspace: Path) -> Path:
        return workspace / self.skills_subdir

    # 只认AGENT.md
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


class CodexAgentManager:
    def __init__(
        self,
        client: CodexClient,
        workspace_manager: CodexWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)

        model = override.model if override and override.model else agent_config.model
        model_provider = override.provider if override else None
        # 沿用项目已有的 provider/model 写法，无需给 src.config 增加字段。
        if model_provider is None and model and "/" in model:
            model_provider, model = model.split("/", 1)
        workspace = self.workspace_manager.get_agent_workspace(agent_name)
        self.client.register_agent_defaults(
            agent_name,
            system_prompt=agent_config.system_prompt,
            model=model,
            model_provider=model_provider,
            cwd=workspace,
        )
        logger.info(
            "设置 Codex Agent: %s | provider=%s model=%s workspace=%s",
            agent_name,
            model_provider,
            model,
            workspace,
        )


def make_codex_get_agent(
    client: CodexClient,
):
    def get_agent(agent_name: str, session_name: str) -> CodexAgent:
        return client.get_agent(agent_name, session_name)

    return get_agent


def make_codex_execute_with_retry(_client: CodexClient):
    async def execute_with_retry(agent: CodexAgent, query_text: str, options):
        # Codex turn 可能已产生文件副作用，同一请求不在内部自动重放。
        result = await agent.execute(query_text, options=options)
        if result.success and result.content:
            incomplete = (result.stop_reason or "complete") != "complete"
            return result, incomplete
        raise CodexHarnessError(result.error_message or "Codex 返回空结果")

    return execute_with_retry
