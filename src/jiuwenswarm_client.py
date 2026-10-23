"""通过官方 CLI 同款 /tui 协议接入 JiuwenSwarm，不依赖其 Python 运行时。

复用公共工作区、查询循环、OpenCode 返回类型及凭证遮蔽逻辑。
服务端管理模型与原生技能；每个逻辑 Agent/session 复用真实 session_id。
模型由服务端按 alias / model_name 自解析，客户端只透传 agents[].model。
AGENT.md / SOUL.md / USER.md / IDENTITY.md 仍显式注入（服务端
ContextAssembleRail 黑名单这四个文件，不会自动进 system prompt）；
项目声明的技能同样在首轮注入，原生技能库仍由服务端管理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

from src.config import warn_agent_model_conflict
from src.opencode_client import ExecutionOptions, ExecutionResult, _redact_error_text
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60


class JiuwenSwarmHarnessError(RuntimeError):
    """JiuwenSwarm 网关、配置或返回结果不可用。"""


async def build_jiuwenswarm_client() -> "JiuwenSwarmClient":
    """读取部署环境配置，不向公共配置模型增加 Agent 专属字段。"""
    return JiuwenSwarmClient(
        os.environ.get("JIUWENSWARM_GATEWAY_URL", "ws://127.0.0.1:19001/tui"),
        os.environ.get("JIUWENSWARM_MODE", "code.normal"),
    )


class JiuwenSwarmWorkspaceManager(BaseWorkspaceManager):
    """本项目的配置、技能和输入文件按 Agent 隔离，不改服务端全局文件。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.skills_subdir = Path("skills")

    def get_agent_workspace(self, agent_name: str) -> Path:
        """返回 Evaluator 应读取的当前 Agent workspace。"""
        if agent_name == "main":
            workspace = self.base_dir
        else:
            parent = self.base_dir.parent
            base_name = self.base_dir.name
            workspace = parent / f"{base_name}-{agent_name}"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / self.skills_subdir).mkdir(parents=True, exist_ok=True)
        return workspace

    def _copy_agent_configs(self, workspace, config_files, agent_dir) -> None:
        for name in config_files:
            source = Path(agent_dir).expanduser() / name
            if source.exists():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_path(source, target)
            else:
                logger.warning("Agent 配置文件不存在: %s", source)


class JiuwenSwarmClient:
    def __init__(self, gateway_url: str, mode: str):
        self.gateway_url = gateway_url
        self.mode = mode
        self.workspace_manager: Optional[JiuwenSwarmWorkspaceManager] = None
        self._configs = {}
        self._models = {}
        self._agents = {}
        # 兼容只允许一个 TUI 客户端的服务版本；包括 evaluator 在内统一排队。
        self._lock = asyncio.Lock()
        self._http = None

    async def __aenter__(self):
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._http.close()

    def setup_agent(self, config, override=None) -> None:
        self._configs[config.name] = config
        if override:
            warn_agent_model_conflict(config.name, config.model, override)
        # 模型原样透传，服务端按 alias / model_name 自解析。
        self._models[config.name] = (
            override.model if override and override.model else config.model
        )
        if override and (override.base_url or override.api_key or override.api):
            logger.warning(
                "JiuwenSwarm Agent '%s' 的模型选择已生效；"
                "服务地址、密钥和协议请配置在 JiuwenSwarm 的 config.yaml 中",
                config.name,
            )

    def get_agent(self, agent_name: str, session_name: str):
        # 与其他 harness 一致：按 (agent_name, session_name) 键控，
        # 不同元组即独立会话（evaluator 的 eval_ 会话自然与主 agent 分开）。
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = JiuwenSwarmAgent(self, agent_name, session_name)
        return self._agents[key]


class JiuwenSwarmAgent:
    def __init__(self, client: JiuwenSwarmClient, agent_name: str, session_name: str):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = f"harness-{uuid.uuid4().hex}"
        self._initialized = False

    def clear_session(self) -> None:
        """重试前清空历史：换新原生会话并重置首轮注入标记。"""
        self.session_key = f"harness-{uuid.uuid4().hex}"
        self._initialized = False

    def _prompt(self, query: str) -> str:
        if self._initialized:
            return query
        config = self._client._configs[self.agent_name]
        workspace = self._client.workspace_manager.get_agent_workspace(self.agent_name)
        parts = [config.system_prompt] if config.system_prompt else []
        for name in config.config:
            path = workspace / name
            if path.is_file():
                parts.append(f"## {name}\n{path.read_text(encoding='utf-8')}")
        # dev-stable 只自动发现服务端注册的技能目录，显式注入各 Agent 的项目技能。
        for skill in config.skills:
            path = workspace / "skills" / Path(skill).name / "SKILL.md"
            if path.is_file():
                parts.append(f"## 技能 {path.parent.name}（目录：{path.parent}）\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join([*parts, query])

    def _request(self, method: str, **params) -> dict:
        return {
            "type": "req", "id": uuid.uuid4().hex, "method": method,
            "is_stream": method == "chat.send",
            "params": {"session_id": self.session_key, "mode": self._client.mode, **params},
        }

    async def execute(self, query: str, options: ExecutionOptions = None) -> ExecutionResult:
        timeout = getattr(options, "timeout_seconds", None) or 3600
        # 每轮使用独立连接；服务端仍可能转发同会话上一轮的尾随事件。
        async with self._client._lock:
            try:
                async with asyncio.timeout(timeout):
                    async with self._client._http.ws_connect(
                        self._client.gateway_url, max_msg_size=8 * 2**20,
                    ) as ws:
                        try:
                            ack = await ws.receive_json()
                            if ack.get("type") != "event" or ack.get("event") != "connection.ack":
                                raise RuntimeError("未收到 TUI connection.ack")
                            workspace = str(self._client.workspace_manager.get_agent_workspace(self.agent_name))
                            prompt = self._prompt(query)
                            params = dict(
                                content=prompt, query=prompt,
                                cwd=workspace, project_dir=workspace,
                                workspace_dir=workspace, trusted_dirs=[workspace],
                                supports_user_interaction=False,
                                agent_ref={"mode": self._client.mode, "id": "default"},
                            )
                            model_name = self._client._models.get(self.agent_name)
                            if model_name:
                                params["model_name"] = model_name
                            request = self._request("chat.send", **params)
                            await ws.send_json(request)
                            result = await self._receive(ws, request["id"])
                            self._initialized = True
                            return result
                        except (Exception, asyncio.CancelledError):
                            # 先通知服务端停止，单纯断开连接不能保证后台任务已取消。
                            if not ws.closed:
                                try:
                                    await asyncio.wait_for(ws.send_json(self._request("chat.interrupt")), 2)
                                except (Exception, asyncio.CancelledError):
                                    pass
                            raise
            except asyncio.TimeoutError:
                return ExecutionResult(success=False, stop_reason="timeout", error_message=f"JiuwenSwarm 执行超过 {timeout} 秒")
            except (aiohttp.ClientError, OSError, ValueError, TypeError, RuntimeError) as exc:
                return ExecutionResult(success=False, stop_reason="error", error_message=_redact_error_text(str(exc)))

    async def _receive(self, ws, request_id: str) -> ExecutionResult:
        content = ""
        messages = []
        usage = None
        mode = self._client.mode
        team_mode = mode.startswith("team") or mode in ("code.team", "agent.team")
        while True:
            frame = await ws.receive_json()
            if frame.get("type") == "res":
                if frame.get("ok") is False:
                    raise RuntimeError(str(frame.get("error") or frame.get("payload")))
                continue
            if frame.get("type") != "event":
                continue
            payload = frame.get("payload") or {}
            if payload.get("session_id") not in (None, "", self.session_key):
                continue
            event = frame.get("event")
            inner = payload.get("event_type")
            # dev-stable 的结束状态可能在下一轮连接后才到达；旧版无请求编号。
            if event == "chat.processing_status" and payload.get("request_id") not in (None, "", request_id):
                continue
            if event == "chat.error" or inner == "team.error":
                raise RuntimeError(str(payload.get("error") or payload.get("message") or "Agent 执行失败"))
            if event in ("chat.ask_user_question", "plan.approval_required"):
                raise RuntimeError("当前为非交互执行，Agent 请求人工输入或审批；请使用普通模式或在 TUI 中处理")
            if event == "chat.delta":
                content += payload.get("content") or ""
            elif event == "chat.llm_usage" or (event == "chat.final" and inner == "chat.llm_usage"):
                usage = payload
            elif event == "chat.tool_call":
                tool = payload.get("tool_call") or payload
                arguments = tool.get("arguments", tool.get("input", {}))
                messages.append({"role": "assistant", "tool_calls": [{
                    "id": tool.get("tool_call_id") or tool.get("call_id") or tool.get("id"),
                    "function": {
                        "name": tool.get("tool_name") or tool.get("name"),
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                    },
                }]})
            elif event == "chat.tool_result":
                messages.append({
                    "role": "tool", "tool_call_id": payload.get("tool_call_id") or payload.get("call_id"),
                    "content": payload.get("result", payload.get("output", "")),
                })
            elif event == "chat.final" and inner in (None, "", "chat.final"):
                content = payload.get("content") or content
                if not team_mode:
                    break
            elif event == "chat.processing_status" and payload.get("is_processing") is False:
                break
        if not content:
            raise RuntimeError("JiuwenSwarm 未返回回复内容")
        messages.append({"role": "assistant", "content": content})
        return ExecutionResult(content=content, usage=usage, messages=messages)


class JiuwenSwarmAgentManager:
    """将项目 Agent 配置注册到 JiuwenSwarmClient。"""

    def __init__(
        self,
        client: JiuwenSwarmClient,
        workspace_manager: JiuwenSwarmWorkspaceManager,
        agent_overrides=None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        override = self.agent_overrides.get(agent_config.name)
        self.client.setup_agent(agent_config, override)
        self.workspace_manager.get_agent_workspace(agent_config.name)
        logger.info(
            "设置 JiuwenSwarm Agent: %s | model=%s",
            agent_config.name,
            self.client._models.get(agent_config.name),
        )


def make_jiuwenswarm_execute_with_retry(client: JiuwenSwarmClient):
    """返回 JiuwenSwarm 专用 execute_with_retry。

    失败按 EXECUTION_MAX_ATTEMPTS / EXECUTION_RETRY_WAIT_SECONDS 重试；每次
    重试前 clear_session() 换新原生会话、清空上一轮累积的历史，避免脏上下文。
    """

    async def execute_with_retry(agent: JiuwenSwarmAgent, query_text: str, options):
        last_exc = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise JiuwenSwarmHarnessError("JiuwenSwarm returned None")
                if result.success and result.content:
                    evidence_incomplete = (result.stop_reason or "complete") != "complete"
                    return result, evidence_incomplete
                message = (
                    result.error_message if not result.success
                    else "JiuwenSwarm returned empty content"
                )
                raise JiuwenSwarmHarnessError(message)
            except (JiuwenSwarmHarnessError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= EXECUTION_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "JiuwenSwarm 调用失败 (第 %d/%d 次): %s; %ds 后清空历史重试",
                    attempt, EXECUTION_MAX_ATTEMPTS, exc, EXECUTION_RETRY_WAIT_SECONDS,
                )
                agent.clear_session()
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
        if last_exc is not None:
            raise last_exc
        raise JiuwenSwarmHarnessError("JiuwenSwarm: unknown error after retries")

    return execute_with_retry
