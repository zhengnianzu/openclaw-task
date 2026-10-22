"""通过 OpenClaw agent --local --json 接入，复用原生配置和会话。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from src.config import warn_agent_model_conflict
from src.evaluator.trajectory import extract_tool_calls
from src.grok_client import ExecutionOptions, ExecutionResult
from src.opencode_client import _redact_error_text
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


class OpenclawLocalError(RuntimeError):
    """原生配置、CLI 或执行结果不可用。"""


class OpenclawLocalWorkspaceManager(BaseWorkspaceManager):
    def __init__(self):
        self.workspaces = {}

    def get_agent_workspace(self, agent_name: str) -> Path:
        if agent_name not in self.workspaces:
            raise OpenclawLocalError(f"请先在 OpenClaw 原生配置中创建 Agent: {agent_name}")
        workspace = self.workspaces[agent_name]
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _copy_agent_configs(self, workspace, config_files, agent_dir):
        for name in config_files:
            source = Path(agent_dir).expanduser() / name
            if source.exists():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_path(source, target)
            else:
                logger.warning("Agent 配置文件不存在: %s", source)


class OpenclawLocalClient:
    def __init__(self, workspace_manager):
        root = os.environ.get("OPENCLAW_DIR")
        executable = shutil.which("openclaw") if not root else None
        entry = Path(root).expanduser().resolve() / "openclaw.mjs" if root else (
            Path(executable).resolve() if executable else None
        )
        if not root and entry is not None and entry.name != "openclaw.mjs":
            entry = entry.parent / "node_modules" / "openclaw" / "openclaw.mjs"
        if entry is None or not entry.is_file():
            raise OpenclawLocalError(
                "未找到 OpenClaw 入口；请将官方 npm 安装加入 PATH，"
                "或设置 OPENCLAW_DIR 指向包含 openclaw.mjs 的已构建目录"
            )
        self.command = [os.environ.get("OPENCLAW_LOCAL_NODE", "node"), str(entry)]
        self.workspace_manager = workspace_manager
        self.evaluator_names = set()
        self._configs, self._models, self._agents = {}, {}, {}
        self._processes = set()

    async def __aenter__(self):
        summaries = await self._run_json(["agents", "list", "--json"])
        if not isinstance(summaries, list):
            raise OpenclawLocalError("OpenClaw agents list 未返回 Agent 列表")
        self.workspace_manager.workspaces = {
            row["id"]: Path(row["workspace"]).expanduser().resolve() for row in summaries
        }
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for process in list(self._processes):
            await self._stop(process)

    @staticmethod
    async def _stop(process):
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
        except ProcessLookupError:
            pass

    async def _run_json(self, args, *, cwd=None, timeout=60):
        process = await asyncio.create_subprocess_exec(
            *self.command, *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
        )
        self._processes.add(process)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            raise OpenclawLocalError(f"OpenClaw CLI 执行超时: {timeout} 秒") from exc
        finally:
            await self._stop(process)
            self._processes.discard(process)
        diagnostic = _redact_error_text(stderr.decode("utf-8", errors="replace"))
        if process.returncode:
            raise OpenclawLocalError(diagnostic or f"OpenClaw 退出码: {process.returncode}")
        if diagnostic:
            logger.debug("[openclaw-local stderr] %s", diagnostic)
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpenclawLocalError("OpenClaw CLI 未返回有效 JSON") from exc

    def setup_agent(self, config, override=None):
        self.workspace_manager.get_agent_workspace(config.name)
        self._configs[config.name] = config
        if override:
            warn_agent_model_conflict(config.name, config.model, override)
            if override.base_url or override.api_key or override.api:
                logger.warning(
                    "OpenClaw Local Agent '%s' 的服务地址、密钥和协议须配置在原生配置中，"
                    "本后端只接受模型选择", config.name,
                )
        self._models[config.name] = (
            override.resolved_model if override and override.model else config.model
        )

    def get_agent(self, agent_name, session_name):
        if agent_name in self.evaluator_names and session_name.startswith("eval_"):
            return OpenclawLocalAgent(self, agent_name, session_name)
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = OpenclawLocalAgent(self, agent_name, session_name)
        return self._agents[key]


class OpenclawLocalAgent:
    def __init__(self, client, agent_name, session_name):
        self._client = client
        self.agent_name, self.session_name = agent_name, session_name
        self.session_key = f"harness-{uuid.uuid4().hex}"
        self._native_session_id = None
        self._sqlite_history = False
        self._initialized = False
        self._evidence_incomplete = False

    def _history(self):
        directory = Path.home() / ".openclaw" / "agents" / self.agent_name / "sessions"
        if not self._native_session_id:
            raise KeyError("CLI 尚未返回原生会话 ID")
        path = directory / f"{self._native_session_id}.jsonl"
        database = directory.parent / "agent" / "openclaw-agent.sqlite"
        if database.exists():
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    events = [row[0] for row in connection.execute(
                        "SELECT event_json FROM transcript_events WHERE session_id=? ORDER BY seq",
                        (self._native_session_id,),
                    )]
                if events:
                    self._sqlite_history = True
                    # 为已有 OpenClaw 统计器保留仅含当前会话的 JSONL 视图。
                    directory.mkdir(parents=True, exist_ok=True)
                    path.write_text("\n".join(events) + "\n", encoding="utf-8")
            except sqlite3.Error:
                if self._sqlite_history:
                    raise
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if row.get("type") == "message"]

    async def execute(self, query, options=None):
        client = self._client
        config = client._configs[self.agent_name]
        timeout = options.timeout_seconds if options and options.timeout_seconds else 3600
        prompt = query
        if not self._initialized and config.system_prompt:
            prompt = f"任务约束：\n{config.system_prompt}\n\n{query}"
        if len(prompt.encode("utf-8")) > 4 * 1024 * 1024:
            raise OpenclawLocalError("OpenClaw 消息文件不能超过 4 MiB")
        try:
            previous = {row["id"] for row in self._history()}
            self._evidence_incomplete = False
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            previous = None if self._initialized else set()
            self._evidence_incomplete = previous is None
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as file:
            file.write(prompt)
            prompt_path = Path(file.name)
        try:
            args = [
                "agent", "--local", "--agent", self.agent_name,
                "--session-key", self.session_key, "--timeout", str(timeout),
                "--json", "--message-file", str(prompt_path),
            ]
            model = client._models[self.agent_name]
            if model:
                args.extend(["--model", model])
            data = await client._run_json(
                args, cwd=str(client.workspace_manager.get_agent_workspace(self.agent_name)), timeout=timeout,
            )
        finally:
            prompt_path.unlink(missing_ok=True)
        if not isinstance(data, dict):
            raise OpenclawLocalError("OpenClaw agent 未返回结果对象")
        meta = data.get("meta") or {}
        payloads = data.get("payloads") or []
        content = "\n\n".join(p.get("text") or "" for p in payloads).strip()
        error = meta.get("error") or meta.get("aborted") or any(p.get("isError") for p in payloads)
        if error or meta.get("stopReason") in {"error", "timeout", "aborted", "cancelled"} or not content:
            raise OpenclawLocalError(_redact_error_text(str(error) + " " + content) if error else "OpenClaw 执行中断或回复为空")
        agent_meta = meta.get("agentMeta") or {}
        self._native_session_id = agent_meta.get("sessionId")
        self._initialized = True
        calls = []
        try:
            if previous is not None:
                calls = extract_tool_calls([row["message"] for row in self._history() if row["id"] not in previous])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self._evidence_incomplete = True
            logger.debug("OpenClaw 本轮工具证据读取失败: %s", exc)
        return ExecutionResult(
            content=content, stop_reason=meta.get("stopReason") or "complete",
            session_id=agent_meta.get("sessionId"), usage=agent_meta.get("usage"),
            model_provider=client._models[self.agent_name], tool_calls=calls,
        )


async def build_openclaw_local_client(workspace_manager):
    return OpenclawLocalClient(workspace_manager)


async def execute_openclaw_local(agent, query_text, options):
    result = await agent.execute(query_text, options)
    return result, agent._evidence_incomplete
