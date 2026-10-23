"""OpenClaw TUI 客户端。

通过官方 ``openclaw agent --local --json`` 命令接入，复用 OpenClaw 原生
Agent、模型配置和会话记录，并将结果适配为统一执行器使用的格式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.config import AgentConfigItem, AgentModelConfig, warn_agent_model_conflict
from src.evaluator.trajectory import extract_tool_calls
from src.grok_client import ExecutionOptions, ExecutionResult
from src.opencode_client import _redact_error_text
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


# ============================================================================
# 常量
# ============================================================================

DEFAULT_EXECUTION_TIMEOUT_SECONDS = 3600
DEFAULT_CLI_TIMEOUT_SECONDS = 60
PROCESS_STOP_TIMEOUT_SECONDS = 3

# 消息投递方式按 OpenClaw 版本分流:
#   - 2026.6.6 仅有 ``--message <text>``,整条消息必须塞进单个 argv。
#     Linux MAX_ARG_STRLEN = 32 页 = 131072 字节;取 128000 留余量。
#   - 2026.10.6 新增 ``--message-file <path>``(UTF-8,上限 4MiB),走临时文件不受 argv 限制。
MAX_INLINE_MESSAGE_BYTES = 128_000          # 单参数内核上限 131072,留 3KB 余量
MESSAGE_FILE_MAX_BYTES = 4 * 1024 * 1024    # --message-file 官方上限 4MiB

# 各版本绑定的运行时(node 与 openclaw.mjs 所在目录)
OC_RUNTIME = {
    "default": {
        "node": "/usr/local/node24/bin/node",
        "dir": "/usr/local/node24/lib/node_modules/openclaw",
    },
    # 2026.10.6 要求 Node >=24.22.1,镜像默认的 24.14.1 不满足,必须用 node26
    "2026.10.6": {
        "node": "/usr/local/node26/bin/node",
        "dir": "/usr/local/node26/lib/node_modules/openclaw",
    },
}


def get_openclaw_version(
    command: List[str] = ["openclaw"],
    timeout: float = 3.0,
    fallback: str = "2026.6.6",
) -> str:
    """探测并返回 OpenClaw 版本号（如 '2026.10.6'），失败时返回降级默认版本。"""
    try:
        result = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout + result.stderr
        if match := re.search(r"(\d{4}\.\d+\.\d+)", output):
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass

    return fallback


# 预先探测当前环境的 OpenClaw 版本,后续命令构造据此分支
OC_TUI_VERSION = get_openclaw_version()


_TERMINAL_STOP_REASONS = {"error", "timeout", "aborted", "cancelled"}
_HISTORY_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error)


class OpenclawLocalError(RuntimeError):
    """OpenClaw 原生配置、CLI 或执行结果不可用。"""


# ============================================================================
# OpenclawLocalWorkspaceManager
# ============================================================================

class OpenclawLocalWorkspaceManager(BaseWorkspaceManager):
    """管理 OpenClaw 原生 Agent 已配置的工作空间。"""

    def __init__(self) -> None:
        self.workspaces: Dict[str, Path] = {}

    def get_agent_workspace(self, agent_name: str) -> Path:
        if agent_name not in self.workspaces:
            raise OpenclawLocalError(f"请先在 OpenClaw 原生配置中创建 Agent: {agent_name}")
        workspace = self.workspaces[agent_name]
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
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
            dst = workspace / config_file
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_path(src, dst)
            logger.info("复制 Agent 配置: %s -> %s", config_file, dst)


# ============================================================================
# OpenclawLocalClient
# ============================================================================

class OpenclawLocalClient:
    """封装 OpenClaw ``agent --local --json`` 子进程调用。"""

    def __init__(self, workspace_manager: OpenclawLocalWorkspaceManager) -> None:
        self.command = self._resolve_command()
        self.workspace_manager = workspace_manager
        self.evaluator_names: Set[str] = set()
        self._configs: Dict[str, AgentConfigItem] = {}
        self._models: Dict[str, Optional[str]] = {}
        self._agents: Dict[Tuple[str, str], OpenclawLocalAgent] = {}
        self._processes: Set[asyncio.subprocess.Process] = set()

    def _resolve_command(self) -> List[str]:
        """按 OC_TUI_VERSION 取绑定的 node 与 openclaw 入口。"""
        runtime = OC_RUNTIME.get(OC_TUI_VERSION, OC_RUNTIME["default"])
        node = os.environ.get("OPENCLAW_LOCAL_NODE") or runtime["node"]
        root = os.environ.get("OPENCLAW_DIR") or runtime["dir"]

        entry = Path(root).expanduser() / "openclaw.mjs"
        if not entry.is_file():
            # 绑定的目录不存在时,退回 PATH 反查
            executable = shutil.which("openclaw")
            if executable:
                entry = Path(executable).resolve()
                if entry.name != "openclaw.mjs":
                    entry = entry.parent / "node_modules" / "openclaw" / "openclaw.mjs"

        if not entry.is_file():
            raise OpenclawLocalError(
                "未找到 OpenClaw 入口；请将官方 npm 安装加入 PATH，"
                "或设置 OPENCLAW_DIR 指向包含 openclaw.mjs 的已构建目录"
            )

        logger.info("OpenClaw 版本: %s(node: %s, entry: %s)", OC_TUI_VERSION, node, entry)
        return [node, str(entry)]

    async def __aenter__(self) -> OpenclawLocalClient:
        summaries = await self._run_json(["agents", "list", "--json"])
        if not isinstance(summaries, list):
            raise OpenclawLocalError("OpenClaw agents list 未返回 Agent 列表")

        try:
            self.workspace_manager.workspaces = {
                row["id"]: Path(row["workspace"]).expanduser().resolve()
                for row in summaries
            }
        except (KeyError, TypeError) as exc:
            raise OpenclawLocalError(
                "OpenClaw agents list 返回了无效的 Agent 数据"
            ) from exc
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Any,
    ) -> None:
        await asyncio.gather(
            *(self._stop(process) for process in list(self._processes)),
            return_exceptions=True,
        )

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_STOP_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
        except ProcessLookupError:
            pass

    async def _run_json(
        self,
        args: List[str],
        *,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    ) -> Any:
        process = await asyncio.create_subprocess_exec(
            *self.command,
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        self._processes.add(process)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise OpenclawLocalError(
                f"OpenClaw CLI 执行超时: {timeout:g} 秒"
            ) from exc
        finally:
            await self._stop(process)
            self._processes.discard(process)
        diagnostic = _redact_error_text(stderr.decode("utf-8", errors="replace"))
        if process.returncode:
            raise OpenclawLocalError(
                diagnostic or f"OpenClaw 退出码: {process.returncode}"
            )
        if diagnostic:
            logger.debug("[openclaw-tui stderr] %s", diagnostic)
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpenclawLocalError("OpenClaw CLI 未返回有效 JSON") from exc

    def setup_agent(
        self,
        agent_config: AgentConfigItem,
        override: Optional[AgentModelConfig] = None,
    ) -> None:
        """注册 harness Agent，并解析本次运行使用的模型。"""

        agent_name = agent_config.name
        self.workspace_manager.get_agent_workspace(agent_name)
        self._configs[agent_name] = agent_config
        logger.info("设置 Agent: %s", agent_name)

        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)
            if override.base_url or override.api_key or override.api:
                logger.warning(
                    "OpenClaw TUI Agent '%s' 的服务地址、密钥和协议须配置在"
                    "原生配置中，本后端只接受模型选择",
                    agent_name,
                )

        self._models[agent_name] = (
            override.resolved_model
            if override and override.model
            else agent_config.model
        )

    def get_agent(self, agent_name: str, session_name: str) -> OpenclawLocalAgent:
        if agent_name in self.evaluator_names and session_name.startswith("eval_"):
            return OpenclawLocalAgent(self, agent_name, session_name)
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = OpenclawLocalAgent(
                self, agent_name, session_name
            )
        return self._agents[key]


# ============================================================================
# OpenclawLocalAgent
# ============================================================================

class OpenclawLocalAgent:
    """维护一个 harness 会话与 OpenClaw 原生会话之间的映射。"""

    def __init__(
        self,
        client: OpenclawLocalClient,
        agent_name: str,
        session_name: str,
    ) -> None:
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = f"harness-{uuid.uuid4().hex}"
        self._native_session_id: Optional[str] = None
        self._sqlite_history = False
        self._initialized = False
        self._evidence_incomplete = False

    def _history(self) -> List[Dict[str, Any]]:
        directory = (
            Path.home()
            / ".openclaw"
            / "agents"
            / self.agent_name
            / "sessions"
        )
        if not self._native_session_id:
            raise KeyError("CLI 尚未返回原生会话 ID")

        path = directory / f"{self._native_session_id}.jsonl"
        database = directory.parent / "agent" / "openclaw-agent.sqlite"
        if database.exists():
            try:
                uri = database.as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as connection:
                    events = [
                        row[0]
                        for row in connection.execute(
                            "SELECT event_json FROM transcript_events "
                            "WHERE session_id=? ORDER BY seq",
                            (self._native_session_id,),
                        )
                    ]
                if events:
                    self._sqlite_history = True
                    # 为已有 OpenClaw 统计器保留仅含当前会话的 JSONL 视图。
                    directory.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        "\n".join(events) + "\n", encoding="utf-8"
                    )
            except sqlite3.Error:
                if self._sqlite_history:
                    raise

        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [row for row in rows if row.get("type") == "message"]

    def _build_prompt(self, query: str) -> str:
        config = self._client._configs[self.agent_name]
        if not self._initialized and config.system_prompt:
            return f"任务约束：\n{config.system_prompt}\n\n{query}"
        return query

    def _snapshot_history_ids(self) -> Optional[Set[str]]:
        try:
            ids = {str(row["id"]) for row in self._history()}
            self._evidence_incomplete = False
            return ids
        except _HISTORY_ERRORS:
            previous = None if self._initialized else set()
            self._evidence_incomplete = previous is None
            return previous

    # ------------------------------------------------------------------
    # 命令构造:按全局 OC_TUI_VERSION 分支。
    # ------------------------------------------------------------------

    def _build_command(
        self,
        prompt: str,
        timeout: float,
    ) -> Tuple[List[str], Optional[str]]:
        """按 OC_TUI_VERSION 拼装 ``agent --local`` 命令。

        返回 ``(argv, message_file)``;``message_file`` 非空时由调用方在
        执行结束后删除(见 ``_discard_message_file``)。
        """
        raw = prompt.encode("utf-8")
        args = [
            "agent",
            "--local",
            "--agent",
            self.agent_name,
            "--session-key",
            self.session_key,
            "--timeout",
            f"{timeout:g}",
            "--json",
        ]
        message_file: Optional[str] = None

        if OC_TUI_VERSION == "2026.10.6":
            # 2026.10.6 起支持 --message-file,消息落盘,不受 argv 长度限制(上限 4MiB)
            if len(raw) > MESSAGE_FILE_MAX_BYTES:
                raise OpenclawLocalError(
                    "消息超过 OpenClaw --message-file 上限"
                    f"（{len(raw)} > {MESSAGE_FILE_MAX_BYTES} 字节）"
                )
            fd, message_file = tempfile.mkstemp(prefix="openclaw-msg-", suffix=".txt")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
            except BaseException:
                Path(message_file).unlink(missing_ok=True)
                raise
            args.extend(["--message-file", message_file])
        else:
            # 其余版本仅有 --message,消息内联进 argv,受单参数内核上限 131072 字节约束
            if len(raw) > MAX_INLINE_MESSAGE_BYTES:
                raise OpenclawLocalError(
                    f"OpenClaw {OC_TUI_VERSION} 仅支持 --message，消息过长无法安全放入命令行"
                    f"（{len(raw)} > {MAX_INLINE_MESSAGE_BYTES} 字节）；"
                    "升级到 2026.10.6 可用 --message-file 突破该限制"
                )
            args.extend(["--message", prompt])

        model = self._client._models.get(self.agent_name)
        if model:
            args.extend(["--model", model])
        return args, message_file

    @staticmethod
    def _discard_message_file(message_file: Optional[str]) -> None:
        if message_file:
            Path(message_file).unlink(missing_ok=True)

    @staticmethod
    def _parse_result(data: Any) -> Tuple[str, Dict[str, Any]]:
        if not isinstance(data, dict):
            raise OpenclawLocalError("OpenClaw agent 未返回结果对象")

        meta = data.get("meta") or {}
        payloads = data.get("payloads") or []
        if not isinstance(meta, dict) or not isinstance(payloads, list):
            raise OpenclawLocalError("OpenClaw agent 返回了无效的结果结构")

        content = "\n\n".join(
            payload.get("text") or ""
            for payload in payloads
            if isinstance(payload, dict)
        ).strip()
        payload_error = any(
            payload.get("isError")
            for payload in payloads
            if isinstance(payload, dict)
        )
        error = meta.get("error") or meta.get("aborted") or payload_error
        stop_reason = meta.get("stopReason")
        if error:
            detail = _redact_error_text(f"{error} {content}".strip())
            raise OpenclawLocalError(detail)
        if stop_reason in _TERMINAL_STOP_REASONS or not content:
            raise OpenclawLocalError("OpenClaw 执行中断或回复为空")
        return content, meta

    def _extract_new_tool_calls(
        self,
        previous_ids: Optional[Set[str]],
    ) -> List[Any]:
        if previous_ids is None:
            return []
        try:
            messages = [
                row["message"]
                for row in self._history()
                if str(row["id"]) not in previous_ids
            ]
            return extract_tool_calls(messages)
        except _HISTORY_ERRORS as exc:
            self._evidence_incomplete = True
            logger.debug("OpenClaw 本轮工具证据读取失败: %s", exc)
            return []

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        timeout = float(
            options.timeout_seconds
            if options and options.timeout_seconds
            else DEFAULT_EXECUTION_TIMEOUT_SECONDS
        )
        prompt = self._build_prompt(query)
        args, message_file = self._build_command(prompt, timeout)

        previous_ids = self._snapshot_history_ids()
        try:
            data = await self._client._run_json(
                args,
                cwd=str(
                    self._client.workspace_manager.get_agent_workspace(
                        self.agent_name
                    )
                ),
                timeout=timeout,
            )
        finally:
            self._discard_message_file(message_file)

        content, meta = self._parse_result(data)
        agent_meta = meta.get("agentMeta") or {}
        if not isinstance(agent_meta, dict):
            agent_meta = {}
        self._native_session_id = agent_meta.get("sessionId")
        self._initialized = True

        return ExecutionResult(
            content=content,
            stop_reason=meta.get("stopReason") or "complete",
            session_id=self._native_session_id,
            usage=agent_meta.get("usage"),
            model_provider=self._client._models[self.agent_name],
            tool_calls=self._extract_new_tool_calls(previous_ids),
        )


# ============================================================================
# Client 工厂 + execute_with_retry
# ============================================================================

async def build_openclaw_local_client(
    workspace_manager: OpenclawLocalWorkspaceManager,
) -> OpenclawLocalClient:
    """创建 OpenClaw TUI 客户端。"""

    return OpenclawLocalClient(workspace_manager)


async def execute_openclaw_local(
    agent: OpenclawLocalAgent,
    query_text: str,
    options: Optional[ExecutionOptions],
) -> Tuple[ExecutionResult, bool]:
    """执行单轮查询，并返回结果与工具证据是否不完整。"""

    result = await agent.execute(query_text, options)
    return result, agent._evidence_incomplete
