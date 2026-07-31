"""
OpenCode CLI 客户端封装。

通过 ``opencode run --format json`` 启动本地 OpenCode 子进程，并保持与
Hermes harness 相同的 Client / Agent / WorkspaceManager / AgentManager 接口。
OpenCode 的 provider、endpoint 与凭证均由 OpenCode 自身配置读取；本模块不会
向项目配置写入或输出明文凭证。User Simulator 所需参数仅在内存中读取和传递。

公开 API:
  OpenCodeClient / OpenCodeAgent / ExecutionResult / ExecutionOptions / OpenCodeError
  build_opencode_client()
  load_opencode_simulator_config()
  OpenCodeWorkspaceManager / OpenCodeAgentManager
  make_opencode_execute_with_retry / make_opencode_get_agent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60

_CREATE_NO_WINDOW = 0x08000000
_ERROR_TEXT_LIMIT = 4000
_CONFIG_REFERENCE_RE = re.compile(r"\{(env|file):([^{}]+)\}")


def _resolve_config_references(value: Any, config_dir: Path) -> Optional[str]:
    """解析 OpenCode 支持的 ``{env:...}`` / ``{file:...}``，不记录值。"""
    if not isinstance(value, str):
        return None

    def replace(match: re.Match[str]) -> str:
        kind, name = match.groups()
        if kind == "env":
            return os.environ.get(name, "")
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = config_dir / path
        return path.read_text(encoding="utf-8").strip()

    return _CONFIG_REFERENCE_RE.sub(replace, value).strip()


def load_opencode_simulator_config(
    preferred_model: Optional[str] = None,
) -> Optional[AgentModelConfig]:
    """从 OpenCode provider 配置提取 OpenAI 兼容参数，仅供 Simulator 内存使用。"""
    inline_config = os.environ.get("OPENCODE_CONFIG_CONTENT")
    configured_path = os.environ.get("OPENCODE_CONFIG")
    config_path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".config" / "opencode" / "opencode.json"
    )

    try:
        if inline_config:
            data = json.loads(inline_config)
            config_dir = Path.cwd()
        else:
            if not config_path.is_file():
                return None
            data = json.loads(config_path.read_text(encoding="utf-8"))
            config_dir = config_path.parent
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "无法读取 OpenCode 配置，User Simulator 将回退 simulator_config: %s",
            type(exc).__name__,
        )
        return None

    providers = data.get("provider") or data.get("providers")
    if not isinstance(providers, dict):
        return None

    compatible = {
        name: spec
        for name, spec in providers.items()
        if isinstance(spec, dict)
        and spec.get("npm") == "@ai-sdk/openai-compatible"
    }
    if not compatible:
        logger.warning(
            "OpenCode 配置中没有可供 User Simulator 使用的 OpenAI 兼容"
            " provider，继续使用 simulator_config"
        )
        return None

    def match_model(provider: Dict[str, Any], requested: str) -> Optional[str]:
        models = provider.get("models")
        models = models if isinstance(models, dict) else {}
        for model_name, model_options in models.items():
            model_id = (
                model_options.get("id")
                if isinstance(model_options, dict)
                else None
            )
            if requested in (model_name, model_id):
                return str(model_id or model_name)
        return None

    selected_provider = None
    selected_model = None
    if preferred_model:
        provider_hint, separator, requested_model = preferred_model.partition(
            "/"
        )
        preferred_providers = compatible
        if separator and provider_hint in compatible:
            preferred_providers = {
                provider_hint: compatible[provider_hint]
            }
        else:
            requested_model = preferred_model
        matches = [
            (name, match_model(provider, requested_model))
            for name, provider in preferred_providers.items()
        ]
        matches = [(name, model) for name, model in matches if model]
        if len(matches) == 1:
            selected_provider, selected_model = matches[0]

    default_model = data.get("model")
    if not selected_model and isinstance(default_model, str):
        provider_name, separator, model_name = default_model.partition("/")
        if separator and provider_name in compatible:
            selected_provider = provider_name
            selected_model = (
                match_model(compatible[provider_name], model_name) or model_name
            )

    if not selected_model and len(compatible) == 1:
        selected_provider, provider = next(iter(compatible.items()))
        models = provider.get("models")
        models = models if isinstance(models, dict) else {}
        if models:
            selected_model, model_options = next(iter(models.items()))
            if isinstance(model_options, dict):
                selected_model = model_options.get("id") or selected_model

    if not selected_provider or not selected_model:
        logger.warning(
            "OpenCode 中存在多个可用 provider，且无法唯一确定 User Simulator"
            " 模型；继续使用 simulator_config"
        )
        return None

    options = compatible[selected_provider].get("options")
    options = options if isinstance(options, dict) else {}
    try:
        api_key = _resolve_config_references(
            options.get("apiKey") or options.get("api_key"),
            config_dir,
        )
        base_url = _resolve_config_references(
            options.get("baseURL") or options.get("base_url"),
            config_dir,
        )
    except OSError:
        return None

    if (
        not api_key
        or not base_url
        or not base_url.startswith(("http://", "https://"))
    ):
        logger.warning(
            "OpenCode provider 缺少有效的 baseURL/apiKey，"
            "User Simulator 将继续使用 simulator_config"
        )
        return None

    logger.info(
        "User Simulator 将复用 OpenCode provider 配置"
        "（仅在内存中使用，凭证不会输出）"
    )
    return AgentModelConfig(
        model=str(selected_model),
        provider=str(selected_provider),
        api_key=api_key,
        base_url=base_url,
    )


class OpenCodeError(RuntimeError):
    """OpenCode CLI 调用失败。"""


class _OpenCodeNonRetryableError(OpenCodeError):
    """本轮可能已产生副作用，不得自动重复执行。"""


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


def _redact_error_text(text: str) -> str:
    """仅保留有限诊断文本，并遮蔽常见凭证形式。"""
    redacted = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>",
        text,
    )
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|authorization|auth[_-]?token|bearer)"
        r"(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", redacted)
    return redacted[-_ERROR_TEXT_LIMIT:].strip()


def _event_error_message(event: Dict[str, Any]) -> Optional[str]:
    value = event.get("error") or event.get("message")
    part = event.get("part")
    if value is None and isinstance(part, dict):
        value = part.get("error")
    if isinstance(value, dict):
        value = value.get("message") or value.get("name")
    if value is None:
        return None
    return _redact_error_text(str(value))


def _parse_run_output(stdout: str) -> Dict[str, Any]:
    """解析 ``opencode run --format json`` 的 NDJSON 输出。"""
    session_id: Optional[str] = None
    text_order: List[str] = []
    text_by_part: Dict[str, str] = {}
    usage: Optional[Dict[str, Any]] = None
    errors: List[str] = []
    valid_events = 0

    for index, raw_line in enumerate(stdout.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"OpenCode 第 {index + 1} 行不是合法 JSON 事件")
            continue
        if not isinstance(event, dict):
            continue

        valid_events += 1
        if event.get("sessionID"):
            session_id = str(event["sessionID"])

        event_type = event.get("type")
        part = event.get("part")
        part = part if isinstance(part, dict) else {}

        if event_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                part_id = str(part.get("id") or f"event-{index}")
                if part_id not in text_by_part:
                    text_order.append(part_id)
                # OpenCode 同一个 part 可能多次输出累计快照，保留最后一份。
                text_by_part[part_id] = text
        elif event_type == "step_finish":
            tokens = part.get("tokens")
            usage = {
                "tokens": tokens if isinstance(tokens, dict) else {},
                "cost": part.get("cost"),
            }
        elif event_type == "error":
            errors.append(
                _event_error_message(event) or "OpenCode 返回 error 事件"
            )

    if stdout.strip() and valid_events == 0 and not errors:
        errors.append("OpenCode 未返回合法 JSON 事件")

    return {
        "content": "".join(text_by_part[key] for key in text_order).strip(),
        "session_id": session_id,
        "usage": usage,
        "error": "; ".join(errors) if errors else None,
    }


class OpenCodeAgent:
    """一个 ``(agent_name, session_name)`` 对应的 OpenCode 会话句柄。"""

    def __init__(
        self,
        client: "OpenCodeClient",
        agent_name: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        workspace: Optional[Path] = None,
        model_override: Optional[AgentModelConfig] = None,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name
        self._system_prompt = system_prompt
        self._workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self._model_override = model_override
        self._opencode_session_id: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    def _resolved_model(self) -> Optional[str]:
        override = self._model_override
        if override is None or not override.resolved_model:
            return None
        model = override.resolved_model
        if "/" not in model:
            logger.warning(
                "agent=%s 的 OpenCode 模型覆盖缺少 provider，已忽略裸模型名；"
                "继续使用 OpenCode 自身配置",
                self.agent_name,
            )
            return None
        return model

    def _build_command(self) -> List[str]:
        command = [
            self._client.command,
            "run",
            "--format",
            "json",
            "--dir",
            str(self._workspace),
        ]
        model = self._resolved_model()
        if model:
            command.extend(["--model", model])
        if self._opencode_session_id:
            command.extend(["--session", self._opencode_session_id])
        return command

    def _build_prompt(self, query: str) -> str:
        if self._system_prompt and self._opencode_session_id is None:
            return f"{self._system_prompt}\n\n{query}"
        return query

    async def _stop_process(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if process.returncode is not None:
            return

        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW,
                )
                try:
                    await asyncio.wait_for(killer.communicate(), timeout=5)
                except asyncio.TimeoutError:
                    killer.kill()
                    await killer.wait()
            except Exception as exc:
                logger.debug("taskkill 失败，回退 process.kill(): %s", exc)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(
                "OpenCode 子进程未在 5 秒内退出 (pid=%s)", process.pid
            )

    async def close(self) -> None:
        process = self._process
        if process is not None:
            await self._stop_process(process)
        self._process = None

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

        async with self._lock:
            self._workspace.mkdir(parents=True, exist_ok=True)
            command = self._build_command()
            prompt = self._build_prompt(query).encode("utf-8")
            subprocess_kwargs: Dict[str, Any] = {}
            if os.name == "nt":
                subprocess_kwargs["creationflags"] = _CREATE_NO_WINDOW
            else:
                subprocess_kwargs["start_new_session"] = True

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self._workspace),
                    **subprocess_kwargs,
                )
            except Exception as exc:
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message=(
                        "启动 OpenCode CLI 失败: "
                        f"{_redact_error_text(str(exc))}"
                    ),
                )

            self._process = process
            try:
                communicate = process.communicate(input=prompt)
                if timeout is not None:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        communicate, timeout=timeout
                    )
                else:
                    stdout_bytes, stderr_bytes = await communicate
            except asyncio.TimeoutError:
                await self._stop_process(process)
                return ExecutionResult(
                    success=False,
                    stop_reason="timeout",
                    error_message=f"OpenCode run timed out after {timeout}s",
                )
            except asyncio.CancelledError:
                await asyncio.shield(self._stop_process(process))
                raise
            except Exception as exc:
                await self._stop_process(process)
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message=(
                        f"OpenCode run 失败: {_redact_error_text(str(exc))}"
                    ),
                )
            finally:
                self._process = None

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = _redact_error_text(
                stderr_bytes.decode("utf-8", errors="replace")
            )
            parsed = _parse_run_output(stdout)
            if parsed["session_id"]:
                self._opencode_session_id = parsed["session_id"]

            error = parsed["error"]
            if process.returncode != 0:
                error = error or stderr or (
                    f"OpenCode 进程退出码为 {process.returncode}"
                )
            if not parsed["content"]:
                error = error or stderr or "OpenCode 未返回 text 事件"

            if error:
                return ExecutionResult(
                    success=False,
                    content=parsed["content"],
                    stop_reason="error",
                    error_message=error,
                    usage=parsed["usage"],
                )

            return ExecutionResult(
                success=True,
                content=parsed["content"],
                stop_reason="complete",
                usage=parsed["usage"],
            )


class OpenCodeClient:
    """本地 OpenCode CLI 客户端。"""

    def __init__(self, command: str):
        self.command = command
        self._agents: Dict[tuple, OpenCodeAgent] = {}
        self.workspace_manager: Optional[OpenCodeWorkspaceManager] = None

    async def __aenter__(self) -> "OpenCodeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        for agent in self._agents.values():
            await agent.close()
        self._agents.clear()

    def get_agent(
        self,
        agent_name: str,
        session_name: str,
        *,
        system_prompt: Optional[str] = None,
        workspace: Optional[Path] = None,
        model_override: Optional[AgentModelConfig] = None,
    ) -> OpenCodeAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = OpenCodeAgent(
                client=self,
                agent_name=agent_name,
                session_name=session_name,
                system_prompt=system_prompt,
                workspace=workspace,
                model_override=model_override,
            )
        return self._agents[key]


async def build_opencode_client(
    command: str = "opencode",
    **_ignored_legacy_kwargs: Any,
) -> OpenCodeClient:
    if _ignored_legacy_kwargs:
        logger.debug(
            "build_opencode_client: 忽略以下旧的 HTTP 参数: %s",
            sorted(_ignored_legacy_kwargs.keys()),
        )

    configured_command = os.environ.get("OPENCODE_BIN", command)
    resolved_command = shutil.which(configured_command)
    if resolved_command is None and Path(configured_command).is_file():
        resolved_command = str(Path(configured_command).resolve())
    if resolved_command is None:
        raise OpenCodeError(
            "未找到 OpenCode CLI；请先安装并确认 `opencode --version` 可用"
        )

    logger.info(
        "OpenCode 客户端 (本地 CLI / run --format json) 就绪；"
        "模型、provider 与凭证由 OpenCode 自身配置决定。"
    )
    return OpenCodeClient(resolved_command)


_PERSONA_DST: Dict[str, Path] = {
    "SOUL.md": Path("SOUL.md"),
    "USER.md": Path("memories/USER.md"),
    "MEMORY.md": Path("memories/MEMORY.md"),
}


def _normalize_agent_name(agent_name: str) -> str:
    normalized = re.sub(r"[^\w.-]+", "-", agent_name, flags=re.UNICODE)
    normalized = normalized.strip(".-")
    return normalized or "agent-default"


class OpenCodeWorkspaceManager(BaseWorkspaceManager):
    """OpenCode 工作空间管理器: ``<base_dir>/<agent_name>``。"""

    skills_subdir = Path(".opencode/skills")

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir or "~/.opencode/workspace").expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        normalized = _normalize_agent_name(agent_name)
        if normalized != agent_name:
            logger.warning(
                "OpenCode agent_name %r 已规范化为 %r",
                agent_name,
                normalized,
            )
        workspace = self.base_dir / normalized
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "memories").mkdir(exist_ok=True)
        (workspace / self.skills_subdir).mkdir(parents=True, exist_ok=True)
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

        instruction_sections: List[str] = []
        for config_file in config_files:
            src = agent_source / config_file
            if not src.exists():
                logger.warning("Agent 配置文件不存在: %s", src)
                continue
            dst = workspace / _PERSONA_DST.get(config_file, Path(config_file))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logger.info("复制 Agent 配置: %s -> %s", config_file, dst)
            if src.suffix.lower() == ".md":
                try:
                    instruction_sections.append(
                        f"## {config_file}\n\n"
                        f"{src.read_text(encoding='utf-8').strip()}"
                    )
                except UnicodeError:
                    logger.warning("Agent 配置不是 UTF-8，未加入 AGENTS.md: %s", src)

        if instruction_sections:
            (workspace / "AGENTS.md").write_text(
                "# OpenCode Agent Instructions\n\n"
                + "\n\n".join(instruction_sections)
                + "\n",
                encoding="utf-8",
            )


class OpenCodeAgentManager:
    """OpenCode Agent 管理器 — 验证并创建本地 workspace。"""

    def __init__(
        self,
        client: OpenCodeClient,
        workspace_manager: OpenCodeWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides: Dict[str, AgentModelConfig] = (
            agent_overrides or {}
        )
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)
        if agent_config.model:
            logger.info("设置 Agent: %s | model=%s", agent_name, agent_config.model)
        else:
            logger.info("设置 Agent: %s", agent_name)
        self.workspace_manager.get_agent_workspace(agent_name)


def make_opencode_execute_with_retry(
    client: OpenCodeClient,
    workspace_manager: Optional[OpenCodeWorkspaceManager] = None,
):
    """返回 OpenCode 专用 execute_with_retry 闭包，与 Hermes 签名对齐。"""

    async def execute_with_retry(agent, query_text: str, options):
        last_exc: Optional[BaseException] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise OpenCodeError("OpenCode returned None")
                if result.success and result.content:
                    evidence_incomplete = (
                        result.stop_reason or "complete"
                    ) != "complete"
                    return result, evidence_incomplete
                if not result.success:
                    message = (
                        result.error_message or "OpenCode returned error"
                    )
                else:
                    message = "OpenCode returned empty content"

                if (
                    result.stop_reason == "timeout"
                    or getattr(agent, "_opencode_session_id", None)
                ):
                    raise _OpenCodeNonRetryableError(message)
                raise OpenCodeError(message)
            except _OpenCodeNonRetryableError:
                raise
            except (OpenCodeError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= EXECUTION_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "调用失败 (第 %d/%d 次): %s; %ds 后重试",
                    attempt,
                    EXECUTION_MAX_ATTEMPTS,
                    exc,
                    EXECUTION_RETRY_WAIT_SECONDS,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
        if last_exc is not None:
            raise last_exc
        raise OpenCodeError("OpenCode: unknown error after retries")

    return execute_with_retry


def make_opencode_get_agent(
    client: OpenCodeClient,
    workspace_manager: Optional[OpenCodeWorkspaceManager] = None,
    agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    agent_system_prompts: Optional[Dict[str, str]] = None,
):
    """返回 OpenCode 专用 get_agent_fn，注入 workspace 与模型覆盖。"""
    overrides = agent_overrides or {}
    system_prompts = agent_system_prompts or {}

    def get_agent(agent_name: str, session_name: str):
        workspace = (
            workspace_manager.get_agent_workspace(agent_name)
            if workspace_manager is not None
            else None
        )
        return client.get_agent(
            agent_name,
            session_name,
            system_prompt=system_prompts.get(agent_name),
            workspace=workspace,
            model_override=overrides.get(agent_name),
        )

    return get_agent
