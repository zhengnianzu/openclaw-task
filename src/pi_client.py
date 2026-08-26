"""Pi CLI RPC harness 客户端。

每个 ``(agent_name, session_name)`` 维护一个长期运行的
``pi --mode rpc --session-dir <dir>`` 子进程，通过 stdin/stdout JSONL 协议交互。
会话历史落盘到 ``_PI_SESSION_DIR``（默认 ``/home/ma-user/.pi/agent/sessions``），
便于事后审计;子进程内多轮上下文仍然由 RPC 内存态维持。
Pi CLI、模型和明文密钥均由部署阶段预先配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")

# 工具结果可能形成较长的单行 JSONL，扩大 asyncio 默认的流读取上限。
_RPC_STREAM_LIMIT = 16 * 1024 * 1024

# Pi 会话落盘目录
_PI_SESSION_DIR = "/home/ma-user/.pi/agent/sessions"


class PiHarnessError(RuntimeError):
    """Pi CLI、RPC 协议或返回结果不可用。"""


@dataclass
class ToolCall:
    """一次 Pi 工具调用及其输入、输出和耗时。"""
    tool: str
    input: Any = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class ExecutionResult:
    """对齐统一执行器所需字段的 Pi 单轮执行结果。"""
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
        """复制当前结果，并用 ``update`` 中的字段覆盖原值。"""

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
    """统一执行器传入 PiAgent 的单轮执行选项。"""
    timeout_seconds: Optional[int] = None


@dataclass
class _AgentDefaults:
    """Agent 注册时确定、后续各 session 共同使用的启动参数。"""
    system_prompt: Optional[str]
    model: Optional[str]
    model_provider: Optional[str]
    cwd: Path


def _content_text(content: Any) -> str:
    """从 Pi 消息 content 中提取并拼接所有文本块。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class PiAgent:
    """一个逻辑 Agent 会话，对应一个长期复用的 Pi RPC 子进程。"""
    def __init__(
        self,
        client: "PiClient",
        agent_name: str,
        session_name: str,
        defaults: _AgentDefaults,
    ):
        """保存逻辑会话信息；RPC 子进程延迟到首次 execute 时启动。"""

        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = session_name
        self.session_id = session_name
        self._defaults = defaults
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_lines: List[str] = []
        self._request_number = 0
        self._lock = asyncio.Lock()

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """持续排空子进程 stderr，并保留最近内容供异常信息使用。"""

        # stderr 若无人读取可能填满管道并阻塞 Pi，因此与 stdout 并行消费。
        while True:
            line = await stream.readline()
            if not line:
                return
            message = line.decode("utf-8", errors="replace").rstrip("\r\n")
            self._stderr_lines.append(message)
            # 仅保留最近 20 行，既能辅助诊断，也避免日志缓存持续增长。
            del self._stderr_lines[:-20]
            logger.debug(
                "[pi stderr] agent=%s session=%s %s",
                self.agent_name,
                self.session_name,
                message,
            )

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        """复用存活的 Pi RPC 子进程，或按 Agent 默认参数首次启动它。"""

        if self._process is not None:
            # 同一 PiAgent 对应同一逻辑会话，复用进程才能保留多轮上下文。
            if self._process.returncode is None:
                return self._process
            raise PiHarnessError(
                f"Pi RPC 子进程已退出: code={self._process.returncode}"
            )

        # 同一 agent 的多个 session 串行执行,无需文件级隔离,直接用模板 workspace
        # 作为 cwd(session 间上下文靠 RPC 子进程内存区分,不靠磁盘目录)。
        session_cwd = self._defaults.cwd
        # 通过 --session-dir 把每次运行的会话历史落盘到统一目录,便于事后审计;
        # pi 会自动生成 session UUID 并在该目录下建 <uuid>/session.jsonl。
        command = [
            self._client.pi_command,
            "--mode",
            "rpc",
            "--session-dir",
            _PI_SESSION_DIR,
            "--approve",
        ]
        if self._defaults.model_provider:
            command.extend(["--provider", self._defaults.model_provider])
        if self._defaults.model:
            command.extend(["--model", self._defaults.model])
        if self._defaults.system_prompt:
            command.extend(
                ["--append-system-prompt", self._defaults.system_prompt]
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(session_cwd),
            limit=_RPC_STREAM_LIMIT,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.terminate()
            raise PiHarnessError("Pi RPC 子进程管道初始化失败")

        self._process = process
        self._stderr_lines.clear()
        # stderr 必须独立消费，stdout 只保留给严格的 RPC JSONL 解析。
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process.stderr)
        )
        logger.info(
            "Pi RPC 已启动: agent=%s session=%s pid=%s provider=%s model=%s cwd=%s",
            self.agent_name,
            self.session_name,
            process.pid,
            self._defaults.model_provider,
            self._defaults.model,
            session_cwd,
        )
        return process

    async def _send_command(self, command: Dict[str, Any]) -> None:
        """将一个 RPC command 编码为 UTF-8 JSONL 并写入 Pi stdin。"""

        process = self._process
        if process is None or process.stdin is None:
            raise PiHarnessError("Pi RPC 子进程尚未启动")
        # Pi RPC 使用 LF 作为唯一记录分隔符，每个 command 必须独占一行。
        payload = json.dumps(command, ensure_ascii=False) + "\n"
        process.stdin.write(payload.encode("utf-8"))
        await process.stdin.drain()

    async def _read_event(self) -> Dict[str, Any]:
        """从 Pi stdout 读取并校验一个非空 JSONL 事件。"""

        process = self._process
        if process is None or process.stdout is None:
            raise PiHarnessError("Pi RPC 子进程尚未启动")

        while True:
            line = await process.stdout.readline()
            if not line:
                # stdout 提前结束时附带最近 stderr，便于定位 CLI 启动或模型错误。
                details = "\n".join(self._stderr_lines[-5:])
                suffix = f": {details}" if details else ""
                raise PiHarnessError(
                    f"Pi RPC 输出提前结束 (code={process.returncode}){suffix}"
                )
            line = line.rstrip(b"\r\n")
            if not line:
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PiHarnessError(f"Pi RPC 返回了无效 JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise PiHarnessError("Pi RPC 事件不是 JSON 对象")
            return value

    async def _run_prompt(self, query: str) -> ExecutionResult:
        """发送一次 prompt，并将 response/event 流收敛为 ExecutionResult。"""

        await self._ensure_process()
        self._request_number += 1
        # response 带请求 ID，而 Agent event 不带，用该 ID 只关联 prompt 接收结果。
        request_id = f"{self.session_name}-{self._request_number}"
        await self._send_command(
            {"id": request_id, "type": "prompt", "message": query}
        )

        accepted = False
        settled = False
        final_message: Optional[Dict[str, Any]] = None
        tool_starts: Dict[str, Dict[str, Any]] = {}
        tool_calls: List[ToolCall] = []

        # response 和 event 可能交错到达，必须同时等到“已接受”和“完全结束”。
        while not (accepted and settled):
            event = await self._read_event()
            event_type = event.get("type")

            if event_type == "response" and event.get("id") == request_id:
                if not event.get("success"):
                    raise PiHarnessError(
                        str(event.get("error") or "Pi 拒绝了 prompt")
                    )
                accepted = True
            elif event_type == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    # 中间工具轮也会产生 assistant 消息，持续覆盖后留下最终一条。
                    final_message = message
                    # 诊断:pi 报 error/aborted 时把整条 message 和最近 stderr 打出
                    if message.get("stopReason") in ("error", "aborted"):
                        logger.error(
                            "Pi 异常 stopReason=%s message=%s stderr_tail=%s",
                            message.get("stopReason"),
                            json.dumps(message, ensure_ascii=False)[:2000],
                            self._stderr_lines[-10:],
                        )
            elif event_type == "tool_execution_start":
                tool_call_id = str(event.get("toolCallId") or "")
                if tool_call_id:
                    tool_starts[tool_call_id] = {
                        "tool": str(event.get("toolName") or ""),
                        "input": event.get("args", ""),
                        "started": time.monotonic(),
                    }
            elif event_type == "tool_execution_end":
                tool_call_id = str(event.get("toolCallId") or "")
                # start/end 按 toolCallId 关联，保留原始入参与最终输出作为轨迹证据。
                start = tool_starts.pop(tool_call_id, {})
                result = event.get("result")
                output = _content_text(
                    result.get("content") if isinstance(result, dict) else None
                )
                if event.get("isError"):
                    output = f"[error] {output}"
                started = start.get("started")
                duration_ms = (
                    round((time.monotonic() - started) * 1000)
                    if isinstance(started, float)
                    else None
                )
                tool_calls.append(
                    ToolCall(
                        tool=start.get("tool")
                        or str(event.get("toolName") or ""),
                        input=start.get("input", ""),
                        output=output,
                        duration_ms=duration_ms,
                    )
                )
            elif event_type == "agent_settled":
                # agent_end 后仍可能自动重试或压缩，只有 agent_settled 才是真正结束。
                settled = True

        if final_message is None:
            raise PiHarnessError("Pi 未返回最终 assistant 消息")

        content = _content_text(final_message.get("content")).strip()
        raw_stop_reason = str(final_message.get("stopReason") or "stop")
        success = bool(content) and raw_stop_reason not in {
            "error",
            "aborted",
            "toolUse",
        }
        stop_reason = "complete" if success and raw_stop_reason == "stop" else raw_stop_reason
        return ExecutionResult(
            success=success,
            content=content,
            stop_reason=stop_reason,
            error_message=(
                None
                if success
                else f"Pi 未正常完成: stopReason={raw_stop_reason}"
            ),
            usage=(
                final_message.get("usage")
                if isinstance(final_message.get("usage"), dict)
                else None
            ),
            session_id=self.session_id,
            model_provider=(
                str(final_message.get("provider"))
                if final_message.get("provider")
                else self._defaults.model_provider
            ),
            tool_calls=tool_calls,
        )

    async def _abort_and_stop(self) -> None:
        """尽力通知 Pi 中止当前任务，随后关闭该会话子进程。"""

        if self._process is not None and self._process.returncode is None:
            try:
                await self._send_command({"type": "abort"})
            except Exception as exc:
                logger.debug("Pi abort 发送失败: %s", exc)
        await self.close()

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        """串行执行一次查询，并把超时、取消和协议错误转换为统一结果。"""

        timeout = (
            float(options.timeout_seconds)
            if options and getattr(options, "timeout_seconds", None)
            else None
        )
        # 一个 RPC stdin/stdout 不能并发消费多条 prompt，同一 session 必须串行。
        async with self._lock:
            try:
                if timeout is not None:
                    return await asyncio.wait_for(
                        self._run_prompt(query), timeout=timeout
                    )
                return await self._run_prompt(query)
            except asyncio.TimeoutError:
                # 超时后关闭进程，避免残留 event 污染下一次 prompt 的读取边界。
                await self._abort_and_stop()
                return ExecutionResult(
                    success=False,
                    stop_reason="timeout",
                    error_message=f"Pi RPC prompt timed out after {timeout}s",
                    session_id=self.session_id,
                    model_provider=self._defaults.model_provider,
                )
            except asyncio.CancelledError:
                await self._abort_and_stop()
                raise
            except Exception as exc:
                await self.close()
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message=str(exc),
                    session_id=self.session_id,
                    model_provider=self._defaults.model_provider,
                )

    async def close(self) -> None:
        """关闭 stdin、回收 Pi 子进程并停止 stderr 消费任务。"""

        process = self._process
        # 先清空引用，确保后续 execute 不会复用正在退出的进程。
        self._process = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    # 正常 terminate 未能及时退出时再强制结束，避免关闭流程悬挂。
                    process.kill()
                    await process.wait()

        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)


class PiClient:
    """管理多个按 Agent/session 隔离的 Pi RPC 子进程。"""

    def __init__(self, pi_command: str = "pi"):
        """记录 Pi 命令，并初始化 Agent 默认参数与会话缓存。"""

        self.pi_command = pi_command
        self._agents: Dict[tuple[str, str], PiAgent] = {}
        self._agent_defaults: Dict[str, _AgentDefaults] = {}
        self.workspace_manager: Optional[PiWorkspaceManager] = None

    async def __aenter__(self) -> "PiClient":
        """进入运行上下文。Pi CLI 由部署阶段预装并加入 PATH;
        若缺失,首次 create_subprocess_exec 会自然抛 FileNotFoundError。"""

        logger.info("Pi client 就绪: pi_command=%s", self.pi_command)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """退出运行上下文时关闭全部 Pi 会话子进程。"""

        await self.close()

    async def close(self) -> None:
        """并行关闭所有已创建的 PiAgent，并清空会话缓存。"""

        await asyncio.gather(
            *(agent.close() for agent in self._agents.values()),
            return_exceptions=True,
        )
        self._agents.clear()

    def register_agent_defaults(
        self,
        agent_name: str,
        *,
        system_prompt: Optional[str],
        model: Optional[str],
        model_provider: Optional[str],
        cwd: Path,
    ) -> None:
        """注册一个 Agent 的系统提示词、模型和 workspace 模板。"""

        self._agent_defaults[agent_name] = _AgentDefaults(
            system_prompt=system_prompt,
            model=model,
            model_provider=model_provider,
            cwd=cwd,
        )

    def get_agent(self, agent_name: str, session_name: str) -> PiAgent:
        """按 ``(agent_name, session_name)`` 获取或创建逻辑会话。"""
        key = (agent_name, session_name)
        if key not in self._agents:
            defaults = self._agent_defaults.get(agent_name)
            if defaults is None:
                raise PiHarnessError(f"Pi agent 尚未注册: {agent_name}")
            # 缓存 PiAgent，确保同一会话后续 prompt 复用原 RPC 子进程。
            self._agents[key] = PiAgent(
                self, agent_name, session_name, defaults
            )
        return self._agents[key]


async def build_pi_client(pi_command: str = "pi") -> PiClient:
    """镜像预装配置env：使用部署阶段已经预装并配置好的 Pi CLI。"""
    return PiClient(pi_command)


class PiWorkspaceManager(BaseWorkspaceManager):
    """每个 Agent 独立 workspace，skill 使用 Pi 原生 .agents/skills。"""

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

    # Pi 自动加载 `AGENTS.md` 和 `CLAUDE.md`；
    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        """按原文件名把 Agent 配置复制到模板 workspace。"""
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


class PiAgentManager:
    """把项目 Agent 配置解析为 PiClient 可复用的启动默认参数。"""

    def __init__(
        self,
        client: PiClient,
        workspace_manager: PiWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        """保存 client、workspace 管理器和按 Agent 设置的模型覆盖。"""
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        """解析模型优先级并向 PiClient 注册一个 Agent。"""
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)

        model = override.model if override and override.model else agent_config.model
        model_provider = override.provider if override else None
        # 沿用项目的 provider/model 简写，拆成 Pi CLI 的两个独立参数。
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
            "设置 Pi Agent: %s | provider=%s model=%s workspace=%s",
            agent_name,
            model_provider,
            model,
            workspace,
        )


def make_pi_get_agent(client: PiClient):
    """创建符合统一执行器签名的 Pi Agent 获取回调。"""
    def get_agent(agent_name: str, session_name: str) -> PiAgent:
        """从指定 client 获取一个逻辑 Pi 会话。"""
        return client.get_agent(agent_name, session_name)

    return get_agent


def make_pi_execute_with_retry(_client: PiClient):
    """创建统一执行器回调；保留接口名称，但失败时不自动重放。"""

    async def execute_with_retry(agent: PiAgent, query_text: str, options):
        """执行一次 prompt，返回结果及轨迹证据是否不完整。"""
        result = await agent.execute(query_text, options=options)
        if result.success and result.content:
            incomplete = (result.stop_reason or "complete") != "complete"
            return result, incomplete
        raise PiHarnessError(result.error_message or "Pi 返回空结果")

    return execute_with_retry