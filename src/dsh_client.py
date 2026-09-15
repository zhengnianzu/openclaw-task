"""DeepSeek Harness (DSH) Python SDK 客户端 —— 薄封装。

- 从 ``~/.dsh/settings.yaml`` 读默认 ``provider``、``model`` 和 ``llm-pi-ai.providers``;
-  .credentials.yaml 复写到 ``settings.yaml.credentials``, 作为 ``apiKeyEnv`` 环境变量注入;
- 每个 ``(agent, session)`` 复用一个 :class:`DeepSeekHarness`
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from deepseek_harness import DeepSeekHarness, RunResult

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.dsh_stream_bridge import DshNonstreamBridge
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


def build_serper_env():
    """
    在沙箱中读取 SERPER_API_KEY 和 SERPER_API_URL 环境变量，
    并写入 ~/.dsh/skills/serper/.env（覆盖写入，仅保留非空变量）。
    """
    skill_dir = Path.home() / ".dsh" / "skills" / "serper"
    env_file = skill_dir / ".env"
    skill_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("SERPER_API_KEY", "")
    api_url = os.environ.get("SERPER_API_URL", "")
    lines = []
    if api_key:
        lines.append(f"SERPER_API_KEY={api_key}")
    if api_url:
        lines.append(f"SERPER_API_URL={api_url}")

    with open(env_file, "w") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")


class DshHarnessError(RuntimeError):
    """DSH SDK、runtime 或返回结果不可用。"""

# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ToolCall:
    """一次 DSH 工具调用及其输入、输出和耗时。"""

    tool: str
    input: Any = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class ExecutionResult:
    """统一执行器所需的 DSH 单轮执行结果。"""

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
    """统一执行器传入 DshAgent 的单轮执行选项。"""
    
    timeout_seconds: Optional[int] = None


@dataclass
class _AgentDefaults:
    """Agent 注册时确定、后续各 session 共用的启动参数。"""

    system_prompt: Optional[str]
    model: str
    model_provider: str
    cwd: Path


# ============================================================================
# ~/.dsh 配置读取
# ============================================================================

def resolve_dsh_home(value: Optional[str] = None) -> Path:
    """DSH 主目录 (默认 ``~/.dsh``, 与 CLI 保持一致)。"""

    configured = value or os.environ.get("DSH_HOME") or "~/.dsh"
    return Path(configured).expanduser().resolve()


def resolve_dsh_cordis(value: Optional[str] = None) -> Path:
    """项目内自定义 Cordis 配置 (注入 llm-pi-ai providers / system prompt)。"""

    configured = value or os.environ.get("DSH_CORDIS_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "deepseek_harness.cordis.yml"
    )


def _load_yaml(path: Path) -> Dict[str, Any]:
    """读取一个 YAML 文件, 缺失或非对象一律返回空 dict。"""

    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_providers(dsh_home: Path) -> Dict[str, Dict[str, Any]]:
    """settings.yaml -> llm-pi-ai.providers, DSH runtime 用来注册 adapter。"""

    settings = _load_yaml(dsh_home / "settings.yaml")
    llm = settings.get("llm-pi-ai") or {}
    providers = llm.get("providers") if isinstance(llm, dict) else None
    if not isinstance(providers, dict):
        return {}
    return {str(k): dict(v) for k, v in providers.items() if isinstance(v, dict)}


def _load_default_route(dsh_home: Path) -> tuple[str, str]:
    """settings.yaml -> agent-default-model.{provider,model}, 用作 fallback。"""

    settings = _load_yaml(dsh_home / "settings.yaml")
    route = settings.get("agent-default-model") or {}
    provider = str(route.get("provider") or "deepseek-official") if isinstance(route, dict) else "deepseek-official"
    model = str(route.get("model") or "deepseek-v4-flash") if isinstance(route, dict) else "deepseek-v4-flash"
    return provider, model


def _load_credentials(dsh_home: Path) -> Dict[str, str]:
    """读 ``{ENV_NAME: value}``, 由 provider.apiKeyEnv 引用。

    优先来源: ``settings.yaml`` 顶层 ``credentials`` 段;
    向后兼容: 若不存在, 回退旧的 ``.credentials.yaml`` 中的 ``refs`` 段。
    """

    settings = _load_yaml(dsh_home / "settings.yaml")
    inline = settings.get("credentials") if isinstance(settings, dict) else None
    legacy_data = _load_yaml(dsh_home / ".credentials.yaml")
    legacy = legacy_data.get("refs") if isinstance(legacy_data, dict) else None

    merged: Dict[str, str] = {}
    for source in (legacy, inline):  # inline 后写, 覆盖 legacy 同名 key
        if not isinstance(source, dict):
            continue
        for k, v in source.items():
            if isinstance(v, (str, int)):
                merged[str(k)] = str(v)
    return merged


def _build_env(
    providers: Dict[str, Dict[str, Any]],
    credentials: Dict[str, str],
    *,
    tools_enabled: bool = True,
    system_prompt: Optional[str] = None,
) -> Dict[str, str]:
    """组装 DSH runtime 子进程环境变量; 直接喂给 ``DeepSeekHarness(env=...)``。"""

    env: Dict[str, str] = {
        "DSH_HARNESS_TOOLS_ENABLED": "1" if tools_enabled else "0",
        "DSH_LLM_PI_AI_PROVIDERS": json.dumps(
            providers, ensure_ascii=False, separators=(",", ":")
        ),
        # apiKeyEnv 里写的名字 -> 对应 credentials.refs 里的值
        **credentials,
    }
    if system_prompt:
        env["DSH_SYSTEM_PROMPT"] = system_prompt
    return env


# ============================================================================
# events 解析
# ============================================================================

def _extract_usage(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """累加同一 turn 各 step 的 token usage。"""

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
    """SDK 没有 error 字段, 从最后一个 ``turn/end`` 事件的 reason 中取详情。"""

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
    return f"DSH 未正常完成: finish_reason={finish_reason}"


def _stringify(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _extract_tool_calls(events: List[Dict[str, Any]]) -> List[ToolCall]:
    """按 tool_use / tool_result 事件配对提取本轮工具调用轨迹。"""

    starts: Dict[str, Dict[str, Any]] = {}
    calls: List[ToolCall] = []
    for event in events:
        etype = event.get("type") or ""
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        call_id = str(
            data.get("id") or data.get("callId") or data.get("toolCallId") or ""
        )

        if etype in ("tool/call/start", "tool/start", "tool/use") and call_id:
            starts[call_id] = {
                "tool": str(data.get("name") or data.get("tool") or ""),
                "input": data.get("input") or data.get("arguments") or "",
            }
        elif etype in ("tool/call/end", "tool/end", "tool/result"):
            start = starts.pop(call_id, {}) if call_id else {}
            calls.append(
                ToolCall(
                    tool=start.get("tool")
                    or str(data.get("name") or data.get("tool") or ""),
                    input=start.get("input", ""),
                    output=_stringify(
                        data.get("output")
                        or data.get("result")
                        or data.get("content")
                    ),
                    duration_ms=(
                        int(data["durationMs"])
                        if isinstance(data.get("durationMs"), (int, float))
                        else None
                    ),
                )
            )
    return calls


# ============================================================================
# DshAgent
# ============================================================================

class DshAgent:
    """一个逻辑 Agent 会话, 对应一个长期复用的 DSH SDK runtime。"""

    def __init__(
        self,
        client: "DshClient",
        agent_name: str,
        session_name: str,
        defaults: _AgentDefaults,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = session_name
        # DSH runtime 用 session_id 承载多轮上下文; 首次 run 时创建, 后续沿用同名 id。
        self.session_id = f"{agent_name}-{session_name}"
        self._defaults = defaults
        self._harness: Optional[DeepSeekHarness] = None

    def _ensure_session_cwd(self) -> Path:
        """首次进入时从模板 workspace 派生本 session 独立 cwd。"""

        session_cwd = self._defaults.cwd / ".sessions" / self.session_name
        if not session_cwd.exists():
            session_cwd.mkdir(parents=True)
            for source in self._defaults.cwd.iterdir():
                if source.name == ".sessions":
                    continue
                copy_path(source, session_cwd / source.name)
        return session_cwd

    def _ensure_harness(self, cwd: Path) -> DeepSeekHarness:
        """按需创建 SDK runtime; 参数直接以 kwargs 传给 :class:`DeepSeekHarness`。"""

        if self._harness is not None:
            return self._harness

        session_root = (
            self._client.session_root
            / self.agent_name
            / self.session_name
        )
        session_root.mkdir(parents=True, exist_ok=True)

        env = _build_env(
            self._client.providers,
            self._client.credentials,
            tools_enabled=self._client.tools_enabled,
            system_prompt=self._defaults.system_prompt,
        )

        kwargs: Dict[str, Any] = {
            "provider": self._defaults.model_provider,
            "model": self._defaults.model,
            "cwd": str(cwd),
            "runtime_cwd": str(cwd),
            "session_root": str(session_root),
            "cordis": str(self._client.cordis_path),
            "env": env,
        }

        self._harness = DeepSeekHarness(**kwargs)
        logger.info(
            "DSH runtime 已创建: agent=%s session=%s provider=%s model=%s cwd=%s",
            self.agent_name,
            self.session_name,
            self._defaults.model_provider,
            self._defaults.model,
            cwd,
        )
        return self._harness

    async def _close_runtime(self) -> None:
        harness, self._harness = self._harness, None
        if harness is None:
            return
        try:
            await asyncio.to_thread(harness.close)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "DSH runtime 关闭失败: agent=%s session=%s error=%s",
                self.agent_name,
                self.session_name,
                exc,
            )

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        """执行一次 prompt, 把超时/取消/异常一律折为 ExecutionResult。"""

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
                error_message=f"DSH turn timed out after {timeout}s",
                session_id=self.session_id,
                model_provider=self._defaults.model_provider,
            )
        except asyncio.CancelledError:
            await self._close_runtime()
            raise
        except Exception as exc:  # noqa: BLE001
            await self._close_runtime()
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=str(exc),
                session_id=self.session_id,
                model_provider=self._defaults.model_provider,
            )

        content = (raw.final_response or "").strip()
        finish_reason = raw.finish_reason
        stop_reason = "complete" if finish_reason == "completed" else finish_reason
        failed = finish_reason in {"error", "cancelled", "aborted"}
        error = _turn_error(raw.events, finish_reason) if failed else None
        if not content and error is None:
            error = "DSH 未返回最终文本"
        if not stop_reason:
            error = error or "DSH 未返回 finish_reason"
            stop_reason = "error"

        return ExecutionResult(
            success=error is None,
            content=content,
            stop_reason=stop_reason,
            error_message=error,
            usage=_extract_usage(raw.events),
            session_id=raw.session_id or self.session_id,
            model_provider=self._defaults.model_provider,
            tool_calls=_extract_tool_calls(raw.events),
        )

    async def close(self) -> None:
        await self._close_runtime()


# ============================================================================
# DshClient
# ============================================================================

class DshClient:
    """管理按 (agent, session) 隔离的 DSH SDK runtime 与共享配置。

    读一次 ``~/.dsh/settings.yaml`` + ``~/.dsh/.credentials.yaml`` 缓存下来,
    后续每个 :class:`DshAgent` 直接把这些值拼进 :class:`DeepSeekHarness` 的 kwargs。
    """

    def __init__(
        self,
        *,
        dsh_home: Optional[Path] = None,
        session_root: Optional[Path] = None,
        cordis_path: Optional[Path] = None,
        tools_enabled: bool = True,
        nonstream_providers: Optional[List[str]] = None,
    ):
        self.dsh_home = (dsh_home or resolve_dsh_home()).expanduser().resolve()
        self.cordis_path = (
            cordis_path or resolve_dsh_cordis()
        ).expanduser().resolve()
        self.session_root = (
            session_root or (self.dsh_home / "sessions")
        ).expanduser().resolve()
        self.session_root.mkdir(parents=True, exist_ok=True)

        # 一次性把 provider/credential 读进内存, DshAgent 直接引用。
        self.providers: Dict[str, Dict[str, Any]] = _load_providers(self.dsh_home)
        self.credentials: Dict[str, str] = _load_credentials(self.dsh_home)
        default_provider, default_model = _load_default_route(self.dsh_home)
        self.default_provider = default_provider
        self.default_model = default_model
        self.tools_enabled = tools_enabled
        self.nonstream_providers: List[str] = list(nonstream_providers or [])

        self._agents: Dict[tuple[str, str], DshAgent] = {}
        self._agent_defaults: Dict[str, _AgentDefaults] = {}
        self.workspace_manager: Optional["DshWorkspaceManager"] = None
        self._nonstream_bridge: Optional[DshNonstreamBridge] = None

    async def __aenter__(self) -> "DshClient":
        if not self.cordis_path.is_file():
            raise DshHarnessError(
                f"DSH Cordis 配置不存在: {self.cordis_path}"
            )
        await self._activate_nonstream_bridge()
        logger.info(
            "DSH SDK 已就绪: cordis=%s session_root=%s provider=%s model=%s",
            self.cordis_path,
            self.session_root,
            self.default_provider,
            self.default_model,
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
        """为指定 provider 起本地 nonstream 桥, 转发 stream -> 非流式上游。"""

        if not self.nonstream_providers:
            return
        upstreams: Dict[str, str] = {}
        for provider in self.nonstream_providers:
            base_url = (self.providers.get(provider) or {}).get("baseURL")
            if not base_url:
                logger.warning(
                    "nonstream provider 缺 baseURL, 跳过: %s", provider
                )
                continue
            upstreams[provider] = str(base_url)
        if not upstreams:
            return
        bridge = DshNonstreamBridge(upstreams)
        await bridge.start()
        for provider in upstreams:
            self.providers[provider]["baseURL"] = bridge.base_url(provider)
        self._nonstream_bridge = bridge
        logger.info(
            "DSH nonstream bridge 已启用: providers=%s", sorted(upstreams)
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
            model=model or self.default_model,
            model_provider=model_provider or self.default_provider,
            cwd=cwd,
        )

    def get_agent(self, agent_name: str, session_name: str) -> DshAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            defaults = self._agent_defaults.get(agent_name)
            if defaults is None:
                raise DshHarnessError(f"DSH agent 尚未注册: {agent_name}")
            self._agents[key] = DshAgent(
                self, agent_name, session_name, defaults
            )
        return self._agents[key]


async def build_dsh_client(
    *,
    dsh_home: Optional[str] = None,
    session_root: Optional[str] = None,
    cordis_path: Optional[str] = None,
    tools_enabled: bool = True,
    nonstream: Optional[List[str]] = None,
) -> DshClient:
    """使用部署阶段已准备好的 ~/.dsh 配置构建客户端。"""

    return DshClient(
        dsh_home=Path(dsh_home).expanduser() if dsh_home else None,
        session_root=Path(session_root).expanduser() if session_root else None,
        cordis_path=Path(cordis_path).expanduser() if cordis_path else None,
        tools_enabled=tools_enabled,
        nonstream_providers=nonstream,
    )


# ============================================================================
# WorkspaceManager
# ============================================================================

class DshWorkspaceManager(BaseWorkspaceManager):
    """每个 Agent 独立模板, session 独立 cwd, skills 落到 ``.agents/skills``。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser().resolve()
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


# ============================================================================
# AgentManager & 执行器闭包
# ============================================================================

class DshAgentManager:
    """把项目 AgentConfigItem 注册到 DshClient (与其他 harness 对齐)。"""

    def __init__(
        self,
        client: DshClient,
        workspace_manager: DshWorkspaceManager,
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

        # simulator_config 优先; 否则用 agents[].model; 都没写就交给 client 默认
        model = override.model if override and override.model else agent_config.model
        model_provider = override.provider if override and override.provider else None
        # 项目沿用的 "provider/model" 简写, 拆成 DSH 的两个独立参数
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
            "设置 DSH Agent: %s | provider=%s model=%s workspace=%s",
            agent_name,
            model_provider or self.client.default_provider,
            model or self.client.default_model,
            workspace,
        )


async def execute_dsh(agent: DshAgent, query_text: str, options):
    result = await agent.execute(query_text, options=options)
    if result.success and result.content:
        incomplete = (result.stop_reason or "complete") != "complete"
        return result, incomplete
    raise DshHarnessError(result.error_message or "DSH 返回空结果")
