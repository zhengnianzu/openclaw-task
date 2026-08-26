"""Grok Build CLI headless harness 客户端。

每个 ``(agent_name, session_name)`` 维护一个 Grok 原生 session ID；每轮通过
``grok --output-format json`` 启动一次子进程，后续轮次使用
``--resume <session-id>`` 恢复上下文。Grok CLI、模型和密钥由部署阶段配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


class GrokHarnessError(RuntimeError):
    """Grok CLI、配置或 headless 返回结果不可用。"""


@dataclass
class ExecutionResult:
    """统一执行器所需的 Grok 单轮执行结果。"""

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
        """复制执行结果，并用指定字段覆盖原值。"""

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
    """统一执行器传入的单轮执行选项。"""

    timeout_seconds: Optional[int] = None


@dataclass
class _AgentDefaults:
    """Agent 各 session 共用的启动参数。"""

    system_prompt: Optional[str]
    model: Optional[str]
    model_provider: Optional[str]
    cwd: Path


def resolve_grok_home(value: Optional[str] = None) -> Path:
    """解析 Grok 配置目录。"""

    configured = value or os.environ.get("GROK_HOME") or "~/.grok"
    return Path(configured).expanduser().resolve()


def resolve_grok_workspace_root(value: Optional[str] = None) -> Path:
    """解析 Grok harness 的 workspace 根目录。"""

    configured = (
        value
        or os.environ.get("GROK_HARNESS_WORKSPACE")
        or "~/.grok-harness/workspace"
    )
    return Path(configured).expanduser().resolve()


def _normalize_stop_reason(value: Any) -> str:
    """将 Grok 的正常结束原因统一为 complete。"""

    if not isinstance(value, str) or not value.strip():
        raise GrokHarnessError("Grok JSON 未返回 stopReason")
    reason = value.strip()
    compact = re.sub(r"[_\s-]+", "", reason).lower()
    if compact in {"complete", "completed", "endturn", "stop"}:
        return "complete"
    return reason


def _parse_headless_output(stdout: str) -> Dict[str, Any]:
    """解析 Grok headless 模式返回的 JSON 对象。"""

    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise GrokHarnessError("Grok 未返回合法的 JSON 最终对象") from exc
    if not isinstance(payload, dict):
        raise GrokHarnessError("Grok JSON 最终结果不是对象")

    is_error = payload.get("type") == "error" or bool(payload.get("error"))
    error_message: Optional[str] = None
    if is_error:
        error_value = payload.get("message") or payload.get("error")
        if isinstance(error_value, dict):
            error_value = (
                error_value.get("message")
                or error_value.get("detail")
                or json.dumps(error_value, ensure_ascii=False)
            )
        error_message = str(error_value or "Grok 返回错误")

    usage = payload.get("usage")
    return {
        "content": str(payload.get("text") or "").strip(),
        "stop_reason": (
            "error"
            if is_error
            else _normalize_stop_reason(payload.get("stopReason"))
        ),
        "session_id": (
            str(payload["sessionId"]) if payload.get("sessionId") else None
        ),
        "usage": usage if isinstance(usage, dict) else None,
        "error": error_message,
    }


class GrokAgent:
    """一个逻辑 Agent 会话，按 Grok session ID 跨进程续接。"""

    def __init__(
        self,
        client: "GrokClient",
        agent_name: str,
        session_name: str,
        defaults: _AgentDefaults,
    ):
        """记录逻辑会话信息，实际进程延迟到执行时创建。"""

        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = session_name
        self._defaults = defaults
        self._grok_session_id: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None

    @property
    def session_id(self) -> Optional[str]:
        """返回 Grok CLI 创建的原生 session ID。"""

        return self._grok_session_id

    def _ensure_session_cwd(self) -> Path:
        """创建当前 session 的独立执行目录。"""

        directory = re.sub(
            r"[^A-Za-z0-9._-]+", "-", self.session_name
        ).strip("-.") or "session"
        session_cwd = self._defaults.cwd / ".sessions" / directory

        # 首次创建时从 Agent 模板复制文件，后续轮次保留已有产物。
        if not session_cwd.exists():
            session_cwd.mkdir(parents=True)
            for source in self._defaults.cwd.iterdir():
                if source.name == ".sessions":
                    continue
                copy_path(source, session_cwd / source.name)

        # Evaluator 需要读取真正执行任务的 session 目录。
        if self._client.workspace_manager is not None:
            self._client.workspace_manager.activate_session(
                self.agent_name, session_cwd
            )
        return session_cwd

    def _build_command(self, prompt_file: Path, cwd: Path) -> List[str]:
        """组装单轮 Grok headless 命令。"""

        command = [
            self._client.command,
            "--cwd",
            str(cwd),
            "--output-format",
            "json",
            "--always-approve",
            "--no-memory",
        ]
        if self._grok_session_id:
            # 恢复原生会话时沿用首轮的模型和系统规则。
            command.extend(["--resume", self._grok_session_id])
        else:
            if self._defaults.model:
                command.extend(["--model", self._defaults.model])
            if self._defaults.system_prompt:
                command.extend(["--rules", self._defaults.system_prompt])
        command.extend(["--prompt-file", str(prompt_file)])
        return command

    async def close(self) -> None:
        """终止并回收当前 Grok CLI 进程。"""

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return

        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        """执行一轮查询并转换为统一结果。"""

        timeout = (
            float(options.timeout_seconds)
            if options and getattr(options, "timeout_seconds", None)
            else None
        )
        cwd = self._ensure_session_cwd()
        prompt_path: Optional[Path] = None
        process: Optional[asyncio.subprocess.Process] = None

        try:
            # 使用 UTF-8 临时文件传递长 prompt，避免命令行长度限制。
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="grok-harness-",
                suffix=".txt",
                delete=False,
            ) as prompt_file:
                prompt_file.write(query)
                prompt_path = Path(prompt_file.name)

            command = self._build_command(prompt_path, cwd)
            env = os.environ.copy()
            env["GROK_HOME"] = str(self._client.grok_home)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )
            self._process = process
            run = process.communicate()
            if timeout is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    run, timeout=timeout
                )
            else:
                stdout_bytes, stderr_bytes = await run
        except asyncio.TimeoutError:
            # 超时和取消都先回收当前 CLI，避免影响下一轮会话。
            await self.close()
            return ExecutionResult(
                success=False,
                stop_reason="timeout",
                error_message=f"Grok headless timed out after {timeout}s",
                session_id=self._grok_session_id,
                model_provider=self._defaults.model_provider,
            )
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=f"启动 Grok CLI 失败: {exc}",
                session_id=self._grok_session_id,
                model_provider=self._defaults.model_provider,
            )
        finally:
            if process is not None and process.returncode is not None:
                self._process = None
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr:
            logger.debug(
                "[grok stderr] agent=%s session=%s %s",
                self.agent_name,
                self.session_name,
                stderr,
            )

        try:
            parsed = _parse_headless_output(stdout)
        except GrokHarnessError as exc:
            detail = f"{exc}: {stderr}" if stderr else str(exc)
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=detail,
                session_id=self._grok_session_id,
                model_provider=self._defaults.model_provider,
            )

        native_session_id = parsed["session_id"]
        if native_session_id:
            self._grok_session_id = native_session_id

        error = parsed["error"]
        if process.returncode != 0:
            error = error or stderr or f"Grok 退出码: {process.returncode}"
        if not native_session_id:
            error = error or "Grok JSON 未返回 sessionId"
        if not parsed["content"]:
            error = error or "Grok 未返回最终文本"

        success = error is None
        return ExecutionResult(
            success=success,
            content=parsed["content"],
            stop_reason=parsed["stop_reason"] if success else "error",
            error_message=error,
            usage=parsed["usage"],
            session_id=native_session_id or self._grok_session_id,
            model_provider=self._defaults.model_provider,
        )


class GrokClient:
    """管理 Grok CLI、原生 session ID 与逻辑 Agent。"""

    def __init__(
        self,
        command: str = "grok",
        grok_home: Optional[Path] = None,
    ):
        """保存 CLI 位置并初始化 Agent 会话缓存。"""

        self.command = command
        self.grok_home = (grok_home or resolve_grok_home()).expanduser().resolve()
        self._agents: Dict[tuple[str, str], GrokAgent] = {}
        self._agent_defaults: Dict[str, _AgentDefaults] = {}
        self.workspace_manager: Optional[GrokWorkspaceManager] = None

    async def __aenter__(self) -> "GrokClient":
        """进入运行上下文前确认 Grok CLI 已安装。"""

        resolved = shutil.which(self.command)
        if resolved is None:
            candidate = Path(self.command).expanduser()
            if candidate.is_file():
                resolved = str(candidate.resolve())
        if resolved is None:
            raise GrokHarnessError(
                "未找到预装的 Grok Build CLI；请先安装并确认 `grok --version` 可用。"
            )
        self.command = resolved
        logger.info(
            "Grok CLI 已就绪: command=%s GROK_HOME=%s",
            self.command,
            self.grok_home,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """退出运行上下文时关闭全部 Grok 会话。"""

        await self.close()

    async def close(self) -> None:
        """并行回收所有 CLI 进程并清空会话缓存。"""

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
        """注册一个 Agent 的模型、规则和 workspace 模板。"""

        self._agent_defaults[agent_name] = _AgentDefaults(
            system_prompt=system_prompt,
            model=model,
            model_provider=model_provider,
            cwd=cwd,
        )

    def get_agent(self, agent_name: str, session_name: str) -> GrokAgent:
        """按 Agent 名和 session 名获取可复用的逻辑会话。"""

        key = (agent_name, session_name)
        if key not in self._agents:
            defaults = self._agent_defaults.get(agent_name)
            if defaults is None:
                raise GrokHarnessError(f"Grok agent 尚未注册: {agent_name}")
            self._agents[key] = GrokAgent(
                self, agent_name, session_name, defaults
            )
        return self._agents[key]


async def build_grok_client(
    grok_home: Optional[str] = None,
) -> GrokClient:
    """使用部署环境已有的 Grok 配置创建客户端。"""

    return GrokClient(grok_home=resolve_grok_home(grok_home))


class GrokWorkspaceManager(BaseWorkspaceManager):
    """每个 Agent 独立模板，session 独立 cwd。"""

    def __init__(self, base_dir: str):
        """创建 workspace 根目录并初始化 session 映射。"""

        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.skills_subdir = Path(".agents/skills")
        self._active_sessions: Dict[str, Path] = {}

    def get_agent_template_workspace(self, agent_name: str) -> Path:
        """返回 Agent 模板目录并创建原生 skills 目录。"""

        workspace = self.base_dir / agent_name
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / self.skills_subdir).mkdir(parents=True, exist_ok=True)
        return workspace

    def get_agent_workspace(self, agent_name: str) -> Path:
        """返回 Evaluator 应读取的当前 Agent workspace。"""

        active = self._active_sessions.get(agent_name)
        return active or self.get_agent_template_workspace(agent_name)

    def activate_session(self, agent_name: str, workspace: Path) -> None:
        """记录 Agent 最近执行任务的 session workspace。"""

        self._active_sessions[agent_name] = workspace

    def get_skills_dst(self, workspace: Path) -> Path:
        """返回 Grok 原生 skills 目录。"""

        return workspace / self.skills_subdir

    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        """按原文件名复制 Agent 配置文件。"""

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


class GrokAgentManager:
    """将项目 Agent 配置注册到 GrokClient。"""

    def __init__(
        self,
        client: GrokClient,
        workspace_manager: GrokWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        """保存客户端、workspace 管理器和模型覆盖配置。"""

        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        """解析模型别名并注册一个 Agent。"""

        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)

        # simulator_config 的同名 Agent 模型优先于任务配置。
        model = override.model if override and override.model else agent_config.model
        workspace = self.workspace_manager.get_agent_template_workspace(agent_name)
        self.client.register_agent_defaults(
            agent_name,
            system_prompt=agent_config.system_prompt,
            model=model,
            model_provider=None,
            cwd=workspace,
        )
        logger.info(
            "设置 Grok Agent: %s | model_alias=%s workspace=%s",
            agent_name,
            model,
            workspace,
        )


def make_grok_get_agent(client: GrokClient):
    """创建符合统一执行器签名的 Agent 获取函数。"""

    def get_agent(agent_name: str, session_name: str) -> GrokAgent:
        """获取指定逻辑会话。"""

        return client.get_agent(agent_name, session_name)

    return get_agent


def make_grok_execute_with_retry(_client: GrokClient):
    """创建统一执行回调，失败时不自动重放请求。"""

    async def execute_with_retry(agent: GrokAgent, query_text: str, options):
        """执行一次查询并标记非正常结束结果。"""

        result = await agent.execute(query_text, options=options)
        if result.success and result.content:
            incomplete = (result.stop_reason or "complete") != "complete"
            return result, incomplete
        raise GrokHarnessError(result.error_message or "Grok 返回空结果")

    return execute_with_retry
