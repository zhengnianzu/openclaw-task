"""TaiChu CLI eval-json 客户端。

只做三件事：按 taichu 原生约定组织目录、把 CLI 跑起来、读回本次证据。

- 配置：全部来自预注入的 ``$TAICHU_HOME/configs/.taichu.json``。provider、model、
  ``max_tokens`` 都在那里定义，客户端不派生、不改写配置。
- 目录：所有 Agent 共用同一个 ``--user-data-root``，只在 ``user/agent_instances/``
  下按 persona 分目录；``main`` 用内置分身 ``sys_celia_main``。
- system prompt：不做特殊处理。taichu 原生按 ``meta_data/*.md`` 组装，文件放进去即可。

目录布局（``home`` 即 ``harness_dir``）::

    {home}/
    ├── configs/.taichu.json                  # 预注入，唯一模型配置来源
    ├── evidence/<agent>/                     # 固定证据目录，每次调用前清空
    └── user/                                 # --user-data-root（所有 Agent 共用）
        ├── history/                          # taichu 原生会话库
        ├── interaction/
        └── agent_instances/
            └── {persona_id}/
                ├── agent_config/persona_policy.json
                ├── meta_data/                # IDENTITY.md / SOUL.md / … + skills/
                ├── workspace/                # --workspace-root
                ├── session_workspaces/       # taichu 为每个 session 自动创建
                └── memory/

不在客户端重试：单轮失败直接上报，taichu 自身的续接/恢复预算由 ``--max-turns`` 控制。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import stat
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")

DEFAULT_PERSONA = "sys_celia_main"          # 0.2.0 内置默认分身
_EVIDENCE_PROTOCOL = "eval-json/v1"
_IS_WINDOWS = os.name == "nt"
DEFAULT_TAICHU_HOME = Path(r"C:\Users\win11admin\taichu")  if _IS_WINDOWS else Path("/home/ma-user/taichu")

# 本地兜底强杀留给 taichu 自己走完 deadline 并落证据的余量。
_TIMEOUT_GRACE_SECONDS = int(os.environ.get("TAICHU_TIMEOUT_GRACE", "60"))
_DEFAULT_MAX_TURNS = int(os.environ.get("TAICHU_MAX_TURNS", "999999"))


class TaichuHarnessError(RuntimeError):
    """CLI、配置或执行证据不可用。"""


def persona_for(agent_name: str) -> str:
    """Agent → persona id。``main`` 用内置默认分身，其余以自身名称建独立分身。"""
    return DEFAULT_PERSONA if agent_name == "main" else agent_name


# ---------------------------------------------------------------------------
# 平台适配
# ---------------------------------------------------------------------------

async def kill_process_tree(process) -> None:
    """结束 CLI 进程及它派生的工具子进程。

    POSIX 靠 ``start_new_session`` + ``killpg`` 一次带走整组；Windows 没有进程组，
    ``process.kill()`` 只会杀掉父进程，taichu 派生的工具 exe 会留在后台，
    所以用 ``taskkill /T`` 按进程树清理。
    """
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    elif process.returncode is None:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except (OSError, NotImplementedError):
            process.kill()


def remove_tree(path: Path) -> None:
    """删除目录树。

    POSIX 下直接删——root 不受文件权限限制，只要父目录可写即可。Windows 上
    带只读属性的文件会让 ``DeleteFile`` 对管理员也失败，先清只读位再删。
    """
    if not _IS_WINDOWS:
        shutil.rmtree(path)
        return

    def _clear_readonly(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        func(target)

    shutil.rmtree(path, onexc=lambda func, target, exc: _clear_readonly(func, target, exc))


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaichuPaths:
    """沙箱固定布局。``home`` 即 hive 的 ``harness_dir``。"""

    home: Path
    tools: Optional[Path] = None        # 仅 docker 启动壳挂载用
    cli: Optional[str] = None

    @classmethod
    def resolve(
        cls,
        home: Optional[str] = None,
        tools: Optional[str] = None,
        cli: Optional[str] = None,
    ) -> "TaichuPaths":
        home_path = Path(home or os.environ.get("TAICHU_HOME")
                         or DEFAULT_TAICHU_HOME).expanduser().resolve()
        explicit_tools = tools or os.environ.get("TAICHU_TOOLS")
        tools_path = Path(explicit_tools).expanduser().resolve() if explicit_tools else None
        if tools_path is not None and not tools_path.is_dir():
            raise TaichuHarnessError(f"TaiChu 工具包目录不存在: {tools_path}")
        return cls(home_path, tools_path, cli or os.environ.get("TAICHU_CLI") or "taichu-cli")

    # -- 宿主侧路径 ---------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.home / "configs" / ".taichu.json"

    @property
    def user_root(self) -> Path:
        """``--user-data-root``：所有 Agent 共用，history/interaction 天然共享。"""
        return self.home / "user"

    @property
    def evidence_root(self) -> Path:
        return self.home / "evidence"

    def agent_root(self, agent_name: str) -> Path:
        """Agent 之间只在这里分叉：``user/agent_instances/{persona}``。"""
        return self.user_root / "agent_instances" / persona_for(agent_name)

    def shared_workspace(self, agent_name: str) -> Path:
        return self.agent_root(agent_name) / "workspace"

    def meta_data(self, agent_name: str) -> Path:
        return self.agent_root(agent_name) / "meta_data"

    def persona_config_dir(self, agent_name: str) -> Path:
        return self.agent_root(agent_name) / "agent_config"

    # -- CLI 侧路径（启动壳可覆盖）------------------------------------------
    def cli_config(self) -> str:
        return str(self.config_file)

    def cli_user_root(self) -> str:
        return str(self.user_root)

    def cli_workspace(self, agent_name: str) -> str:
        return str(self.shared_workspace(agent_name))

    def cli_path(self, host_path: Path) -> str:
        return str(Path(host_path).resolve())

    def ensure(self) -> None:
        for path in (self.config_file.parent, self.user_root / "agent_instances",
                     self.evidence_root):
            path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    tool: str
    input: Any = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = 3600
    max_turns: Optional[int] = None


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    persona_id: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    evidence_incomplete: bool = False
    evidence_dir: Optional[str] = None

    def model_copy(self, *, update=None):
        return replace(self, **(update or {}))


@dataclass
class TraceSummary:
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    assistant_texts: list[str] = field(default_factory=list)
    assistant_rounds: int = 0
    plain_rounds: int = 0
    complete: bool = True
    provider_errors: list[str] = field(default_factory=list)   # model_output 里的模型报错原文


# ---------------------------------------------------------------------------
# 原生日志解析
# ---------------------------------------------------------------------------

def read_taichu_ledger(path: Path, *, run_id=None, session_id=None) -> TraceSummary:
    """解析单次运行的 ledger；续接会话的 ledger 含历史前缀，按 ``run_id`` 切片。

    只统计一次 tool_call/tool_result，避免 operation_checkpoint 等旁路事实重复计数。
    """
    summary = TraceSummary()
    pending: dict[str, ToolCall] = {}
    rows: list[dict] = []
    try:
        with path.open("rb") as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except (ValueError, UnicodeDecodeError):
                    summary.complete = False
    except OSError:
        summary.complete = False
        return summary

    if session_id:
        rows = [row for row in rows if row.get("session_id") in (None, session_id)]

    # 续接会话的 ledger 会带上前面轮次的事件；从本轮 run_bootstrap/agent_start 起切。
    start = 0
    if run_id:
        matched = [
            index for index, row in enumerate(rows)
            if row.get("event") in ("run_bootstrap", "agent_start")
            and (row.get("payload") or {}).get("run_id") == run_id
        ]
        if matched:
            start = matched[0]
        else:
            summary.complete = False
            starts = [i for i, row in enumerate(rows) if row.get("event") == "run_bootstrap"]
            start = starts[-1] if starts else 0
    rows = rows[start:]

    for row in rows:
        payload = row.get("payload") or {}
        event = row.get("event")
        if event == "tool_call":
            call = ToolCall(payload.get("tool_name", ""), payload.get("args", {}))
            pending[payload.get("tool_call_id")] = call
            summary.tool_calls.append(call)
        elif event == "tool_result":
            call = pending.pop(payload.get("tool_call_id"), None)
            if call is None:
                summary.complete = False
                continue
            call.output = "\n".join(
                block.get("text", "") for block in payload.get("content", [])
                if block.get("type") == "text"
            )
            if payload.get("is_error"):
                call.output = "[tool error] " + call.output
            call.duration_ms = (payload.get("timing") or {}).get("total_elapsed_ms")
        elif event == "model_output":
            assistant = payload.get("assistant") or {}
            blocks = assistant.get("content") or []
            summary.assistant_rounds += 1
            summary.plain_rounds += not any(block.get("type") == "toolCall" for block in blocks)
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            if text:
                summary.assistant_texts.append(text)
            if assistant.get("error_message"):
                summary.provider_errors.append(str(assistant["error_message"]))
            for key, value in (assistant.get("usage") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    summary.usage[key] = summary.usage.get(key, 0) + value

    summary.complete = summary.complete and not pending and summary.assistant_rounds > 0
    return summary


def _parse_cli_json(stdout: bytes) -> dict:
    """CLI 会在 JSON 之前打印 plugin diagnostics，取最后一行合法 JSON 对象。"""
    text = stdout.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and (
            payload.get("protocol") == _EVIDENCE_PROTOCOL or payload.get("schema_version") == 1
        ):
            return payload
    raise TaichuHarnessError(f"TaiChu CLI 输出中没有可解析的 JSON: {text[:400]}")


def _preflight_error(result: dict, returncode) -> Optional[TaichuHarnessError]:
    """CLI 在写出证据前就失败时，把错误码翻译成明确异常。

    没有 ``artifacts`` 就不会有 receipt；此时必须按错误码上报，
    而不是让调用方去读一个不存在的文件、报成「receipt 不可读」。
    """
    if result.get("artifacts"):
        return None
    error = result.get("error") or {}
    code = error.get("code") or "UNKNOWN"
    detail = error.get("message") or "无错误详情"
    return TaichuHarnessError(
        f"TaiChu 在执行前失败（exit={returncode}, code={code}）：{detail}"
    )


# ---------------------------------------------------------------------------
# Workspace / Persona
# ---------------------------------------------------------------------------

class TaichuWorkspaceManager(BaseWorkspaceManager):
    """persona 目录与共享 workspace。

    0.2.0 的分身就是一个目录，``persona list`` 以目录为准，因此无需 ``persona create``。
    meta_data 下的 md 文件由 taichu 原生 bootstrap 生成，这里只补目录结构。
    """

    def __init__(self, paths: Optional[TaichuPaths] = None):
        self.paths = paths or TaichuPaths.resolve()
        self.paths.ensure()

    def get_agent_workspace(self, agent_name: str) -> Path:
        """补齐 persona 目录，返回共享 workspace（所有 session 共用一份）。"""
        root = self.paths.agent_root(agent_name)
        (root / "agent_config").mkdir(parents=True, exist_ok=True)
        (root / "meta_data" / "skills").mkdir(parents=True, exist_ok=True)
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        policy = root / "agent_config" / "persona_policy.json"
        if not policy.is_file():
            # 空对象表示继承 .taichu.json 里预注入的 provider/model。
            policy.write_text("{}\n", encoding="utf-8")
        return workspace

    def get_skills_dst(self, workspace: Path) -> Path:
        # workspace 与 meta_data 同级：.../agent_instances/<persona>/{workspace,meta_data}
        return workspace.parent / "meta_data" / "skills"

    def _copy_agent_configs(self, workspace, config_files, agent_dir):
        meta_data = workspace.parent / "meta_data"
        for name in config_files:
            source = Path(agent_dir).expanduser() / name
            if not source.is_file():
                raise FileNotFoundError(f"TaiChu Agent 配置不存在: {source}")
            copy_path(source, meta_data / source.name)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class TaichuAgent:
    def __init__(self, client, agent_name, session_name):
        self.client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.persona_id = persona_for(agent_name)
        self.session_id: Optional[str] = None
        self._process = None
        self._unusable = False

        self.workspace = client.workspace_manager.get_agent_workspace(agent_name)
        client.paths.evidence_root.mkdir(parents=True, exist_ok=True)

    # -- 生命周期 -----------------------------------------------------------
    async def close(self):
        process = self._process
        if process is not None:
            await kill_process_tree(process)
            await process.wait()
        await self._cleanup_launch()

    async def _cleanup_launch(self) -> None:
        """启动壳的善后钩子；沙箱内直接起进程，无需额外处理。"""
        return None

    # -- 命令构造 -----------------------------------------------------------
    def _new_evidence_dir(self) -> Path:
        """固定的证据目录：沙箱本身已隔离，每次调用前清空即可满足「已存在且为空」。

        路径固定为 ``evidence/<agent>/``，便于按沙箱直接取用。
        """
        path = self.client.paths.evidence_root / self.agent_name
        if path.exists():
            remove_tree(path)
        path.mkdir(parents=True)
        return path

    def _launch_prefix(self) -> list[str]:
        """CLI 之前的启动壳。沙箱内就是 CLI 本身；测试用的 docker 壳在 test/ 里覆盖。"""
        return [self.client.paths.cli or "taichu-cli"]

    def _build_command(self, query_text: str, evidence_dir: Path, options: ExecutionOptions) -> list[str]:
        paths = self.client.paths
        # 优先用 hive prepare_config 预生成的 per-agent 配置（.taichu.<persona>.json），
        # 不存在则退回默认 .taichu.json。
        cfg_override = self.client._agent_configs.get(self.agent_name)
        config_arg = str(cfg_override) if cfg_override else paths.cli_config()
        command = self._launch_prefix() + [
            "--config", config_arg,
            "--user-data-root", paths.cli_user_root(),
            "--no-color",
        ]
        # --agent-instance-id 仅在非默认分身时下发；默认分身由 CLI 自动保证存在。
        if self.persona_id != DEFAULT_PERSONA:
            command += ["--agent-instance-id", self.persona_id]
        command += ["--output", "eval-json", "run"]
        command += ["--session", self.session_id] if self.session_id else ["--new-session"]
        command += [
            "--workspace-root", paths.cli_workspace(self.agent_name),
            "--evidence-dir", paths.cli_path(evidence_dir),
            "--timeout-seconds", str(int(options.timeout_seconds or 3600)),
            "--max-turns", str(_DEFAULT_MAX_TURNS),
            "--message", query_text,
        ]
        return command

    # -- 执行 ---------------------------------------------------------------
    async def execute(self, query_text: str, options=None) -> ExecutionResult:
        async with self.client.execution_lock():
            if self._unusable:
                raise TaichuHarnessError("此 TaiChu 会话曾被中断，不能自动恢复或重放请求")
            if not query_text.strip():
                raise ValueError("TaiChu 查询不能为空")
            opts = options or ExecutionOptions()
            if opts.timeout_seconds is not None and opts.timeout_seconds <= 0:
                raise ValueError("timeout_seconds 必须大于零")

            evidence_dir = self._new_evidence_dir()
            command = self._build_command(query_text, evidence_dir, opts)

            try:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.client.paths.home),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                )
            except OSError as error:
                self._unusable = True
                raise TaichuHarnessError(
                    "无法启动 TaiChu（taichu-cli 是否在 PATH 中？用 TAICHU_CLI 指定绝对路径）: "
                    f"{error}"
                ) from error

            hard_deadline = float(opts.timeout_seconds or 3600) + _TIMEOUT_GRACE_SECONDS
            try:
                stdout, stderr = await asyncio.wait_for(self._process.communicate(), hard_deadline)
                returncode = self._process.returncode
            except (asyncio.TimeoutError, asyncio.CancelledError) as error:
                self._unusable = True
                await self.close()
                if isinstance(error, asyncio.CancelledError):
                    raise
                return ExecutionResult(
                    success=False, stop_reason="timeout",
                    error_message=f"TaiChu 执行超过 {hard_deadline:.0f}s，已终止进程",
                    session_id=self.session_id, persona_id=self.persona_id,
                    evidence_dir=str(evidence_dir),
                )
            finally:
                self._process = None

            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.debug("[taichu stderr] agent=%s session=%s %s",
                             self.agent_name, self.session_name, stderr_text)

            result = _parse_cli_json(stdout)
            returned_session = result.get("session_id")
            run_id = result.get("run_id")

            problem = _preflight_error(result, returncode)
            if problem is not None:
                raise problem

            if self.session_id and returned_session != self.session_id:
                self._unusable = True
                raise TaichuHarnessError("TaiChu 返回了不同的 session_id")
            self.session_id = returned_session

            receipt, summary, gaps = self._collect_evidence(evidence_dir, result, run_id)
            execution = receipt.get("execution") or {}
            candidate = receipt.get("candidate") or {}
            success = (
                result.get("status") == "completed"
                and execution.get("status") == "completed"
                and bool(self.session_id)
            )
            error = result.get("error") or {}
            failure_message = None
            if not success:
                failure_message = (
                    f"TaiChu 执行失败（exit={returncode}, "
                    f"code={error.get('code') or execution.get('errorCode') or 'unknown'}）"
                )
                # 真实原因在 ledger 的 model_output.error_message；stderr 多是 plugin diagnostics 噪声。
                if summary.provider_errors:
                    failure_message += f": {summary.provider_errors[-1][:400]}"
                elif stderr_text:
                    noise = ("taichu-cli plugin diagnostics",)
                    lines = [line for line in stderr_text.splitlines()
                             if line.strip() and not line.startswith(noise)]
                    if lines:
                        failure_message += f": {lines[-1][:300]}"

            return ExecutionResult(
                success=success,
                content=execution.get("assistantText") or "",
                stop_reason="complete" if success else (result.get("status") or "error"),
                error_message=failure_message,
                session_id=self.session_id,
                run_id=run_id,
                persona_id=candidate.get("agentInstanceId") or self.persona_id,
                usage=summary.usage or None,
                tool_calls=summary.tool_calls,
                evidence_incomplete=bool(gaps) or not summary.complete,
                evidence_dir=str(evidence_dir),
            )

    def _collect_evidence(self, evidence_dir: Path, result: dict, run_id):
        """读取本次运行封存的 receipt 与 ledger，返回 (receipt, 轨迹摘要, 缺口列表)。"""
        artifacts = result.get("artifacts") or {}
        receipt_path = evidence_dir / (artifacts.get("receipt_path") or "taichu-cli-eval-receipt.json")
        ledger_path = evidence_dir / (artifacts.get("native_ledger_path")
                                      or "native/taichu-session-ledger.jsonl")

        for path in (receipt_path, ledger_path):
            if not path.resolve().is_relative_to(evidence_dir.resolve()):
                raise TaichuHarnessError(f"证据路径超出本次证据目录: {path}")

        gaps: list[str] = []
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            self._unusable = True
            raise TaichuHarnessError(f"TaiChu 证据 receipt 不可读: {receipt_path}") from error

        native = receipt.get("nativeEvidence") or {}
        gaps = list(native.get("evidenceGaps") or [])
        if (native.get("coverage") or {}).get("status") != "canonical":
            gaps.append("coverage 非 canonical")

        summary = read_taichu_ledger(ledger_path, run_id=run_id, session_id=self.session_id)
        return receipt, summary, gaps


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TaichuClient:
    """沙箱客户端。子类可替换 ``paths_class`` / ``agent_class`` 换启动壳。"""

    paths_class = TaichuPaths
    agent_class = TaichuAgent
    workspace_class = TaichuWorkspaceManager

    def __init__(self, paths: Optional[TaichuPaths] = None, **kwargs):
        self.paths = paths or self.paths_class.resolve(**kwargs)
        self.workspace_manager = self.workspace_class(self.paths)
        self._agents: dict[tuple[str, str], TaichuAgent] = {}
        self._registered: set[str] = set()
        self._agent_configs: dict[str, Optional[Path]] = {}
        self._lock = asyncio.Lock()
        self.base_config: dict = {}

    async def __aenter__(self):
        self.paths.ensure()
        await self.check_runtime()
        self.base_config = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
        return self

    def _resolve_agent_config(self, agent_name: str) -> Optional[Path]:
        """返回该 agent 的专属配置文件；不存在则返回 None（用默认 .taichu.json）。

        hive ``prepare_config._prepare_taichu`` 按 user_proxy_model.json 里的 agent 名
        生成 ``.taichu.<agent>.json``，这里按同样的命名查找。
        """
        directory = self.paths.config_file.parent
        for name in (agent_name, persona_for(agent_name)):
            candidate = directory / f".taichu.{name}.json"
            if candidate.is_file():
                return candidate
        return None

    async def __aexit__(self, *args):
        await self.close()

    async def check_runtime(self) -> None:
        """启动前尽早失败：确认 taichu-cli 可执行。"""
        cli = self.paths.cli or "taichu-cli"
        resolved = shutil.which(cli) or (cli if os.path.isfile(cli) else None)
        if not resolved:
            raise TaichuHarnessError(
                f"找不到可执行的 taichu-cli: {cli}（用 TAICHU_CLI 指定绝对路径）"
            )
        self.paths = replace(self.paths, cli=resolved)

    def execution_lock(self) -> asyncio.Lock:
        """所有 Agent 共用同一个 user-data-root（history/interaction 同库），串行执行。"""
        return self._lock

    async def close(self):
        await asyncio.gather(*(agent.close() for agent in self._agents.values()),
                             return_exceptions=True)

    def get_agent(self, agent_name, session_name):
        key = (agent_name, session_name)
        if key not in self._agents:
            if agent_name not in self._registered:
                raise TaichuHarnessError(f"TaiChu Agent 未注册: {agent_name}")
            self._agents[key] = self.agent_class(self, agent_name, session_name)
        return self._agents[key]


async def build_taichu_client(home=None, **kwargs) -> TaichuClient:
    # Windows 上 asyncio 默认 SelectorEventLoop 不支持子进程（RuntimeError），
    # 切到 ProactorEventLoop 才能 create_subprocess_exec。
    if _IS_WINDOWS:
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except (AttributeError, NotImplementedError):
            pass
    return TaichuClient(home=home, **kwargs)


# ---------------------------------------------------------------------------
# Agent 注册
# ---------------------------------------------------------------------------

class TaichuAgentManager:
    """只注册 persona 目录。

    provider/model/max_tokens 全部来自预注入的 ``.taichu.json``，这里不派生配置、
    不改写 provider，也不把 system_prompt 写进 meta_data。
    """

    def __init__(self, client: TaichuClient, workspace_manager, agent_overrides=None):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        client.workspace_manager = workspace_manager
        # 复用 harness 已解析的路径，避免两处各自 resolve 出不一致的 home。
        if isinstance(workspace_manager, TaichuWorkspaceManager):
            client.paths = workspace_manager.paths

    async def setup_agent(self, agent_config):
        workspace = self.workspace_manager.get_agent_workspace(agent_config.name)
        self.client._registered.add(agent_config.name)
        cfg_path = self.client._resolve_agent_config(agent_config.name)
        self.client._agent_configs[agent_config.name] = cfg_path
        base_config = self.client.base_config or {}
        model_id = base_config.get("default_model")
        if cfg_path is not None:
            try:
                model_id = json.loads(cfg_path.read_text(encoding="utf-8")).get(
                    "default_model") or model_id
            except (OSError, ValueError):
                pass
        logger.info(
            "设置 TaiChu Agent: %s | persona=%s | model=%s | config=%s | workspace=%s",
            agent_config.name, persona_for(agent_config.name),
            model_id,
            cfg_path or self.client.paths.cli_config(), workspace,
        )


# ---------------------------------------------------------------------------
# 统一执行器接入
# ---------------------------------------------------------------------------

def make_taichu_execute(_client):
    """适配统一执行器：失败直接抛出，不做客户端重放。

    taichu 自身带 completion-recovery 轮次，其预算由 ``--max-turns`` 控制；
    在适配器里再重试会把有副作用的工具调用重复执行，因此这里只执行一次。
    """
    async def execute(agent, query_text, options):
        result = await agent.execute(query_text, options)
        if not result.success:
            raise TaichuHarnessError(result.error_message or "TaiChu 执行失败")
        if not result.content:
            raise TaichuHarnessError("TaiChu 返回空结果")
        return result, result.evidence_incomplete
    return execute
