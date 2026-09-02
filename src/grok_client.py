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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


class GrokHarnessError(RuntimeError):
    """Grok CLI、配置或 headless 返回结果不可用。"""


@dataclass
class ToolCall:
    """一次 Grok 工具调用及其输入、输出和耗时（从 chat_history.jsonl 提取）。"""
    tool: str
    input: Any = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None


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
    requestId: Optional[str] = None
    num_turns: Optional[int] = None
    modelUsage: Optional[Dict[str, Any]] = field(default=None)
    tool_calls: List[ToolCall] = field(default_factory=list)


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
            "requestId": self.requestId,
            "num_turns": self.num_turns,
            "modelUsage": self.modelUsage,
            "tool_calls": self.tool_calls
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


def extract_tool_calls_grok(session_id: str, cwd: Path) -> List[ToolCall]:
    """从 Grok session 的 chat_history.jsonl 提取**本轮新增**的工具调用轨迹。
    
    通过 prompt_index 定位最后一轮对话，只提取该轮的工具调用（避免累积历史轮次）。
    
    Args:
        session_id: Grok 原生 session ID
        cwd: 当前工作目录，用于计算 session 存储路径
        
    Returns:
        本轮新增的工具调用列表（按时间顺序）
    """
    # Grok 使用 URL 编码的 cwd 作为 session 存储的子目录名
    # 例如 /root -> %2Froot
    cwd_encoded = str(cwd).replace("/", "%2F")
    grok_home = resolve_grok_home()
    chat_history_path = grok_home / "sessions" / cwd_encoded / session_id / "chat_history.jsonl"
    
    if not chat_history_path.exists():
        logger.debug("chat_history.jsonl 不存在: %s", chat_history_path)
        return []
    
    # 第一遍扫描：找出最后一个 user 消息的 prompt_index
    last_prompt_index = None
    try:
        with open(chat_history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "user" and "prompt_index" in msg:
                        last_prompt_index = msg["prompt_index"]
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        logger.warning("扫描 prompt_index 失败: %s", exc)
        return []
    
    if last_prompt_index is None:
        logger.debug("未找到 prompt_index，无法确定本轮边界")
        return []
    
    # 第二遍扫描：只提取 last_prompt_index 之后的工具调用
    tool_calls = []
    tool_call_map = {}  # tool_call_id -> ToolCall (待匹配 tool_result)
    current_prompt_index = None
    in_target_turn = False
    
    try:
        with open(chat_history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                msg_type = msg.get("type")
                
                # 跟踪当前所处的 prompt_index
                if msg_type == "user" and "prompt_index" in msg:
                    current_prompt_index = msg["prompt_index"]
                    in_target_turn = (current_prompt_index == last_prompt_index)
                
                # 只处理目标轮次的消息
                if not in_target_turn:
                    continue
                
                # 提取 assistant 消息中的 tool_calls
                if msg_type == "assistant" and "tool_calls" in msg:
                    for tc in msg["tool_calls"]:
                        tool_call_id = tc.get("id")
                        tool_name = tc.get("name", "")
                        # arguments 可能是字符串或字典
                        arguments = tc.get("arguments", "")
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        
                        tool_call_obj = ToolCall(
                            tool=tool_name,
                            input=arguments,
                            output=None,
                            duration_ms=None
                        )
                        tool_calls.append(tool_call_obj)
                        if tool_call_id:
                            tool_call_map[tool_call_id] = tool_call_obj
                
                # 匹配 tool_result 到对应的 tool_call
                elif msg_type == "tool_result":
                    tool_call_id = msg.get("tool_call_id")
                    if tool_call_id and tool_call_id in tool_call_map:
                        tc_obj = tool_call_map[tool_call_id]
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            # 某些工具返回多段内容，合并为字符串
                            content = "\n".join(str(c) for c in content)
                        tc_obj.output = str(content) if content else None
    
    except Exception as exc:
        logger.warning("解析 chat_history.jsonl 失败: %s", exc)
    
    logger.debug(
        "Grok 本轮工具调用: session=%s prompt_index=%s tool_calls=%d",
        session_id, last_prompt_index, len(tool_calls)
    )
    return tool_calls


def resolve_grok_home(value: Optional[str] = None) -> Path:
    """解析 Grok 配置目录。"""

    configured = value or os.environ.get("GROK_HOME") or "~/.grok"
    return Path(configured).expanduser().resolve()


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
        self._defaults = defaults
        self._grok_session_id: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None

    @property
    def session_id(self) -> Optional[str]:
        """返回 Grok CLI 创建的原生 session ID。"""

        return self._grok_session_id

    def _ensure_session_cwd(self) -> Path:
        """创建当前 session 的独立执行目录。"""
        session_cwd = self._defaults.cwd / ".sessions" / self.session_name
        # 首次创建时从 Agent 模板复制文件，后续轮次保留已有产物。
        if not session_cwd.exists():
            session_cwd.mkdir(parents=True)
            for source in self._defaults.cwd.iterdir():
                if source.name == ".sessions":
                    continue
                copy_path(source, session_cwd / source.name)
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

        # 解析 Grok CLI JSON 输出
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=f"无法解析 Grok JSON 输出: {exc}",
                session_id=self._grok_session_id,
                model_provider=self._defaults.model_provider,
            )

        native_session_id = parsed.get("sessionId")
        if native_session_id:
            self._grok_session_id = native_session_id

        # 检查错误：成功响应没有 error 字段
        error_msg = parsed.get("error")
        if process.returncode != 0 and not error_msg:
            error_msg = stderr or f"Grok 退出码: {process.returncode}"

        success = error_msg is None
        stop_reason = parsed.get("stopReason", "end_turn")
        
        # 提取工具调用轨迹
        tool_calls = []
        if success and native_session_id:
            try:
                tool_calls = extract_tool_calls_grok(native_session_id, cwd)
            except Exception as exc:
                logger.debug("提取工具调用失败: %s", exc)
        
        return ExecutionResult(
            success=success,
            content=parsed.get("text", ""),
            stop_reason=stop_reason if success else "error",
            error_message=error_msg,
            usage=parsed.get("usage"),
            session_id=native_session_id or self._grok_session_id,
            model_provider=self._defaults.model_provider,
            requestId=parsed.get("requestId"),
            num_turns=parsed.get("num_turns"),
            modelUsage=parsed.get("modelUsage"),
            tool_calls=tool_calls,
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
        logger.info("Grok CLI 已就绪: command=%s GROK_HOME=%s",self.command,self.grok_home)
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
    """每个 Agent 独立模板，与 Pi/Hermes 保持一致的 workspace 设计。"""

    def __init__(self, base_dir: str):
        """创建 workspace 根目录。"""

        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
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
        workspace = self.workspace_manager.get_agent_workspace(agent_name)
        self.client.register_agent_defaults(
            agent_name,
            system_prompt=agent_config.system_prompt,
            model=model,
            model_provider=None,
            cwd=workspace,
        )
        logger.info(
            "设置 Grok Agent: %s | model=%s workspace=%s",
            agent_name,
            model,
            workspace,
        )


async def execute_grok(agent: GrokAgent, query_text: str, options):
    """执行一次查询并标记非正常结束结果。"""

    result = await agent.execute(query_text, options=options)
    if result.success and result.content:
        incomplete = (result.stop_reason or "end_turn") != "end_turn"
        return result, incomplete
    raise GrokHarnessError(result.error_message or "Grok 返回空结果")