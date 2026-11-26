"""通过官方 CLI 同款 /tui 协议接入 JiuwenSwarm，不依赖其 Python 运行时。

复用公共工作区、查询循环、OpenCode 返回类型及凭证遮蔽逻辑。
服务端管理模型与原生技能；每个逻辑 Agent/session 复用真实 session_id。
模型由服务端从 config.yaml 的对应 Agent 配置解析，客户端不透传模型配置。
首轮仅注入配置的 system_prompt 与运行约束；工作区 Markdown 不手动
注入正文，项目声明的技能由服务端按 workspace 的 skills 目录发现。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta
import aiohttp
from dataclasses import dataclass, field

from src.workspace import BaseWorkspaceManager, copy_path

from src.agent_logging import logged_execution, log_native, log_tool_start, log_tool_complete

logger = logging.getLogger("harness_automation")

FIRST_TURN_SYSTEM_REMINDER = """======<system-reminder>
以下是本会话的运行约束，不是用户任务正文；实际权限与工具参数以运行时为准。

## 网络搜索

- 如需互联网搜索，必须优先使用当前已启用、可用的搜索技能。
- 通过已注册的技能接口获取所选搜索技能的使用说明，并实际执行其搜索流程。只读取说明或准备调用，不算已经尝试搜索。
- 执行搜索技能脚本前，先确认该技能在当前运行时中的真实绝对路径；必须在同一次 Shell 调用中使用 cd "<技能目录绝对路径>" && <执行命令>，从技能根目录运行脚本。不得在当前项目目录直接执行技能的相对路径脚本，不依赖前一次调用中的 cd 状态，也不得猜测或写死其他环境的技能路径。
- 只有该搜索技能已经尝试并执行失败，或当前未提供、不可用时，才依次尝试其他可用搜索技能及已注册的 web_search、paid_search 等方案；不能跳过可用的搜索技能直接降级。只读取说明或计划执行不算失败。

## 工具使用约束

- read_file 仅用于读取文本文件。每次调用前，先检查目标路径的扩展名；扩展名不明或无法确定为文本时，先确认文件类型。
- 禁止使用 read_file 直接读取 PDF、任何图片或其他二进制文件，包括 .pdf、.png、.jpg、.jpeg、.webp、.gif、.bmp、.tif、.tiff、.heic、.svg 等，扩展名检查不区分大小写。
- 处理 PDF 时，使用当前可用的 PDF 查看、解析、转换或文本提取能力，再读取提取出的文本；不得用 read_file 直接读取原始 PDF。
- 所有图像查看和理解场景，必须优先调用已注册的 image_reading MCP 工具，不能用 read_file 读取图片代替图像理解。
- 如 image_reading 未注册或调用失败，应说明限制，再使用其他确实可用的图像理解能力；仍然禁止用 read_file 直接读取图片。
</system-reminder>==="""


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[float] = None


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    messages: Optional[List[Dict[str, Any]]] = field(default=None)
    files: List[Dict[str, Any]] = field(default_factory=list)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        values = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
            "messages": self.messages,
            "files": list(self.files),
        }
        if update:
            values.update(update)
        return ExecutionResult(**values)


def _resolve_work_mode(mode: str, work_mode: Optional[str] = None) -> str:
    """解析网关工作模式；Agent 请求对应 work，Code 请求对应 code。"""
    configured = (work_mode or "").strip().lower()
    if configured == "agent":
        configured = "work"
    if configured:
        if configured not in {"code", "work"}:
            raise ValueError(
                "JIUWENSWARM_WORK_MODE 仅支持 code/work（agent 会自动映射为 work）"
            )
        return configured

    mode_name = (mode or "").strip().lower().split(".", 1)[0]
    return "work" if mode_name == "agent" else "code"


# jiuwenswarm 的 app.py / server app 均以 load_dotenv(override=True) 读取
# ~/.jiuwenswarm/config/.env, 该文件里 SERPER_API_KEY 等是空行, 会把沙箱注入的
# 环境变量清空(日志表现为「SERPER_API_KEY 存在但为空」)。故在建会话前把当前
# 进程的所有非空环境变量回填进 .env。空值不回填, 保住模板自带的端口等运行期默认值。
def sync_jiuwenswarm_env(env_file: Optional[Path] = None) -> Optional[Path]:
    """把当前进程的非空环境变量回填到 ~/.jiuwenswarm/config/.env。

    服务端每次 create_instance 都会以 override=True 重新加载该文件, 所以在
    建立会话前写入即可生效。文件不存在时不新建(交给产品自身初始化), 返回 None。
    已存在的键原地覆盖, 新增的键追加到文件末尾。
    """
    env_path = Path(env_file) if env_file else Path.home() / ".jiuwenswarm" / "config" / ".env"
    if not env_path.exists():
        logger.warning("jiuwenswarm .env not found, skip env sync: %s", env_path)
        return None

    overrides = {k: v for k, v in os.environ.items() if v.strip()}
    if not overrides:
        logger.info("no jiuwenswarm env overrides in os.environ, .env unchanged")
        return env_path

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        seen = set()
        out = []
        for line in lines:
            stripped = line.strip()
            key = ""
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
            if key and key in overrides:
                out.append(f"{key}={overrides[key]}")
                seen.add(key)
            else:
                out.append(line)
        # 环境里给了、模板里没有的键, 追加到末尾
        out.extend(f"{k}={v}" for k, v in overrides.items() if k not in seen)
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("write jiuwenswarm .env failed: %s: %s", type(e).__name__, e)
        return env_path

    logger.info("synced jiuwenswarm .env keys: %s", sorted(overrides))
    return env_path


def build_serper_env(
    workspace_manager: "JiuwenSwarmWorkspaceManager",
) -> Path:
    """把 Serper 环境变量写入默认 workspace 的 ``skills/serper/.env``。

    dev/xiaoyi 的 ``workspace_manager.base_dir`` 已分别指向各自默认 workspace。
    调用方必须在技能复制完成后、Agent 建立会话前调用，避免文件被覆盖。
    """
    api_key = os.environ.get("SERPER_API_KEY", "")
    api_url = os.environ.get("SERPER_API_URL", "")
    lines = []
    if api_key:
        lines.append(f"SERPER_API_KEY={api_key}")
    if api_url:
        lines.append(f"SERPER_API_URL={api_url}")

    content = "\n".join(lines)
    if lines:
        content += "\n"

    skill_dir = workspace_manager.base_dir / "skills" / "serper"
    skill_dir.mkdir(parents=True, exist_ok=True)
    env_path = skill_dir / ".env"
    env_path.write_text(content, encoding="utf-8")
    logger.info(
        "wrote JiuwenSwarm serper env: path=%s keys=%s",
        env_path,
        [line.split("=", 1)[0] for line in lines],
    )
    return env_path


class JiuwenSwarmHarnessError(RuntimeError):
    """JiuwenSwarm 网关、配置或返回结果不可用。"""


def _llm_log_candidates() -> list:
    """服务端 llm.log 的候选路径(相对当前工作目录,即 harness 运行目录)。"""
    return [
        Path.cwd() / "logs" / "llm.log",
        Path.home() / ".jiuwenswarm" / "logs" / "llm.log",
    ]


_LLM_LOG_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _parse_llm_log_ts(line: str) -> Optional[datetime]:
    """llm.log 每行以「YYYY-MM-DD HH:MM:SS」开头, 取出来做时间窗匹配。

    该文件里的 JSON 体不带 session_id / request_id（trace 是字面量
    default_trace_id），所以无法按 ID 关联, 唯一可靠的关联维度是时间。
    """
    m = _LLM_LOG_LINE_TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _llm_exception_in_window(started_at: Optional[datetime],
                             ended_at: Optional[datetime],
                             max_bytes: int = 1024 * 1024) -> tuple:
    """取落在 [started_at, ended_at] 时间窗内的异常原文, 返回 (原文, 日志时间)。
    时间窗取不到匹配时返回 ("", "") —— 宁可只报原始错误, 也不给一条错的根因。
    """
    if started_at is None:
        return "", ""
    for path in _llm_log_candidates():
        try:
            if not path.exists():
                continue
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                chunk = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        # 倒序找时间窗内最近的一条
        for line in reversed(chunk.splitlines()):
            if '"exception"' not in line:
                continue
            line_ts = _parse_llm_log_ts(line)
            if line_ts is None:
                continue
            if line_ts < started_at:
                continue
            if ended_at is not None and line_ts > ended_at:
                continue
            try:
                record = json.loads(line[line.index("{"):])
            except (ValueError, KeyError):
                continue
            exc = record.get("exception")
            if exc:
                return str(exc), line_ts.strftime("%Y-%m-%d %H:%M:%S")
    return "", ""


def _with_real_cause(message: str,
                     started_at: Optional[datetime] = None,
                     ended_at: Optional[datetime] = None,
                     request_id: str = "") -> str:
    """把包装后的错误文案补上本次请求时间窗内的真实原因。

    request_id 是本端 chat.send 的请求号, 一并带出 —— llm.log 里没有它,
    但 gateway.log / agent_server.log 有, 拿到它才能串起完整链路。
    """
    cause, cause_ts = _llm_exception_in_window(started_at, ended_at)
    parts = [message]
    if cause and cause not in message:
        parts.append(f"真实原因({cause_ts}): {cause}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return " (".join(parts) + (")" if len(parts) > 1 else "")



async def build_jiuwenswarm_client(
    harness_type: Optional[str] = None,
) -> "JiuwenSwarmClient":
    """读取部署环境配置，不向公共配置模型增加 Agent 专属字段。"""
    # 建连(= 建会话)前把环境变量回填进 .env, 服务端 create_instance 会重新加载。
    sync_jiuwenswarm_env()
    # 所有分支统一按通用 Agent 跑: 网关默认走 code(原生 CLI 不写 work_mode),
    # 这里显式钉死 mode=agent + work_mode=work, 与该 harness 的评测语义一致。
    # 不读 JIUWENSWARM_MODE / JIUWENSWARM_WORK_MODE, 避免环境残留导致两个分支行为不一致。
    return JiuwenSwarmClient(
        os.environ.get("JIUWENSWARM_GATEWAY_URL", "ws://127.0.0.1:19001/tui"),
        "agent",
        harness_type=harness_type,
        work_mode="work",
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
    def __init__(
        self,
        gateway_url: str,
        mode: str,
        harness_type: Optional[str] = None,
        work_mode: Optional[str] = None,
    ):
        self.gateway_url = gateway_url
        self.mode = mode
        self.work_mode = _resolve_work_mode(mode, work_mode)
        self.harness_type = harness_type
        self.workspace_manager: Optional[JiuwenSwarmWorkspaceManager] = None
        self._configs = {}
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

    def setup_agent(self, config) -> None:
        """注册 Agent 的非模型配置。

        模型选择不走这里：网关只认 chat.send params 里的 model_name(见
        JiuwenSwarmAgent.execute)，config.yaml 的 agents.<name>.model 在
        现网网关版本中不参与模型解析。
        """
        self._configs[config.name] = config

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
        self._session_created = False
        self._initialized = False

    def _agent_ref(self) -> dict:
        """返回 JiuwenSwarm config.yaml 中对应的 Agent 身份引用。

        agent_ref 只决定 agent 身份/会话归属，**不参与模型选择**(实测
        2026-11-13：网关 _resolve_model_for_request 只读 params.model_name)。
        具名 Agent 要用自己的模型，靠 execute() 里透传的
        model_name=<agent_name> 命中 models.defaults 的 alias 条目。
        """
        agent_id = "default" if self.agent_name == "main" else self.agent_name
        return {"mode": self._client.mode, "id": agent_id}

    def clear_session(self) -> None:
        """清空会话状态。

        **当前调用方已移除**：JiuwenSwarm 的重试机制整体取消，两个 harness_type
        分支都不再重试，因此不会有代码在 execute 之间调这个函数。保留它只为将来
        需要时重新接入。

        重新接入时注意两点：

        1. xiaoyi 分支必须**沿用原有 session_key**。换新会话要再走一次
           session.create，而 TUI 单次 RPC 上限只有 25s（gateway
           agent_request_timeout._TUI_DEFAULT_UNARY_TIMEOUT_SECONDS）；上一轮被
           中断的 turn 在服务端没停干净时 session.create 必然超时，重试会全部卡
           在 session.create、根本到不了 chat.send（实测间隔 85s = 60s 等待
           + 25s 超时）。沿用原会话可直接发 chat.send（流式路径没有总超时）。
        2. 别把 _initialized 置回 False —— 那会让下一次 execute 重新拼接首轮
           system_prompt/运行约束并重发整段任务正文，服务端会把同一个任务当成
           新的一轮再跑一遍。实测 task_source1_mermaid-0101 的 history.jsonl 里
           因此堆了 5 份完全相同的任务正文（仅 <claw_context> 时间戳不同）。
        """
        if self._client.harness_type == "jiuwenswarm-xiaoyi":
            return
        self.session_key = f"harness-{uuid.uuid4().hex}"
        self._session_created = False
        self._initialized = False

    def _prompt(self, query: str) -> str:
        if self._client.harness_type == "jiuwenswarm-xiaoyi":
            tz = timezone(timedelta(hours=8))
            now = datetime.now(tz)
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            payload = json.dumps({"timestamp": ts}, separators=(",", ":"))
            query = f"<claw_context>{payload}</claw_context>\n\n{query}"
        return self._with_system_prompt(query)

    def _with_system_prompt(self, query: str) -> str:
        """首轮把 system_prompt 和运行约束拼到 query 前面。

        chat.send 只有 content/query 两个文本入口，所以只能作为首轮消息的一部分
        发送；后续轮次沿用既有上下文，只发 query。
        """
        if self._initialized:
            return query
        parts = []
        config = self._client._configs.get(self.agent_name)
        system_prompt = (getattr(config, "system_prompt", None) or "").strip()
        if system_prompt:
            parts.append(system_prompt)
        parts.append(FIRST_TURN_SYSTEM_REMINDER)
        return "\n\n".join([*parts, query])

    def _request(self, method: str, **params) -> dict:
        return {
            "type": "req", "id": uuid.uuid4().hex, "method": method,
            "is_stream": method == "chat.send",
            "params": {
                "session_id": self.session_key,
                "mode": self._client.mode,
                "work_mode": self._client.work_mode,
                **params,
            },
        }

    @staticmethod
    async def _receive_response(ws, request_id: str) -> dict:
        """读取指定 RPC 的响应。

        两个目标分支都使用 legacy TUI ``res`` 帧；按 id 匹配可避免把连接上
        延迟到达的其他响应误认为 session.create 的结果。
        """
        while True:
            frame = await ws.receive_json()
            log_native("TUI消息", frame)
            if frame.get("type") != "res" or frame.get("id") != request_id:
                continue
            if frame.get("ok") is False:
                error = frame.get("error") or frame.get("payload") or "RPC 执行失败"
                if isinstance(error, dict):
                    error = error.get("message") or error.get("error") or error
                raise RuntimeError(str(error))
            payload = frame.get("payload")
            return payload if isinstance(payload, dict) else {}

    async def _ensure_session(self, ws, workspace: str) -> None:
        """为需要前置建会话的分支建立 JiuwenSwarm 原生会话。

        xiaoyi_0.2.4.beta3 会保留传入 ID，后续请求采用服务端返回的权威 ID。

        dev-stable 不走这里（见下方 early-return）：它的 chat.send 直接接受客户端
        session_id，不需要 session.create；
        """
        if self._session_created:
            return
        if self._client.harness_type != "jiuwenswarm-xiaoyi":
            # dev-stable: 不建会话，chat.send 自带 session_id 即可（对齐 v1024 已验证行为）
            self._session_created = True
            return
        requested_session_id = self.session_key
        request = self._request(
            "session.create",
            cwd=workspace,
            project_dir=workspace,
            workspace_dir=workspace,
            trusted_dirs=[workspace],
            supports_user_interaction=False,
            agent_ref=self._agent_ref(),
        )
        await ws.send_json(request)
        payload = await self._receive_response(ws, request["id"])
        returned_session_id = payload.get("session_id") or payload.get("sessionId")
        if not returned_session_id:
            raise RuntimeError("JiuwenSwarm session.create 未返回 session_id")
        self.session_key = str(returned_session_id)
        if (
            self._client.harness_type == "jiuwenswarm-xiaoyi"
            and self.session_key != requested_session_id
        ):
            raise RuntimeError(
                "jiuwenswarm-xiaoyi 要求 session.create 保留客户端 session_id，"
                f"但服务端返回了 {self.session_key}"
            )
        self._session_created = True
        logger.info(
            "JiuwenSwarm 会话已建立: harness_type=%s session_id=%s",
            self._client.harness_type,
            self.session_key,
        )

    @logged_execution("jiuwenswarm")
    async def execute(self, query: str, options: ExecutionOptions = None) -> ExecutionResult:
        timeout = getattr(options, "timeout_seconds", None) or 3600
        # 记录本次 chat.send 的起止时刻: llm.log 里没有 request/session id,
        # 只能靠时间窗把「本次失败」和「半小时前的旧异常」区分开。
        started_at = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        request_id = ""
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
                            await self._ensure_session(ws, workspace)
                            prompt = self._prompt(query)
                            params = dict(
                                content=prompt, query=prompt,
                                cwd=workspace, project_dir=workspace,
                                workspace_dir=workspace, trusted_dirs=[workspace],
                                supports_user_interaction=False,
                                agent_ref=self._agent_ref(),
                            )
                            # 具名 agent 透传 model_name=<agent_name>。网关侧
                            # _resolve_model_for_request 只认 params.model_name,
                            # 命中 models.defaults 中 alias=<agent_name> 的条目后
                            # 才会用该 agent 自己的 model_client_config(含 api_key);
                            # 不传则一律落 is_default 条目 —— agent_ref 不参与
                            # 模型选择, agents.<name>.model 也是死配置(实测
                            # 2026-11-13: evaluator 带 alias 却全程跑 default 模型)。
                            # main 沿用服务端 default, 不传。
                            if self.agent_name != "main":
                                params["model_name"] = self.agent_name
                            request = self._request("chat.send", **params)
                            request_id = request["id"]
                            await ws.send_json(request)
                            result = await self._receive(ws, request["id"])
                            self._initialized = True
                            return result
                        except (Exception, asyncio.CancelledError):
                            # 先通知服务端停止，单纯断开连接不能保证后台任务已取消。
                            # 2s 太短：interrupt 未送达时服务端会继续跑完整个 turn，
                            # 后续重试的 session.create 便一直排在它后面直至超时。
                            if not ws.closed:
                                try:
                                    await asyncio.wait_for(ws.send_json(self._request("chat.interrupt")), 10)
                                except (Exception, asyncio.CancelledError):
                                    pass
                            raise
            except asyncio.TimeoutError:
                return ExecutionResult(success=False, stop_reason="timeout", error_message=f"JiuwenSwarm 执行超过 {timeout} 秒")
            except (aiohttp.ClientError, OSError, ValueError, TypeError, RuntimeError) as exc:
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message=_with_real_cause(
                        str(exc),
                        started_at=started_at,
                        ended_at=datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None),
                        request_id=request_id,
                    ),
                )

    async def _receive(self, ws, request_id: str) -> ExecutionResult:
        content = ""
        messages = []
        usage = None
        mode = self._client.mode
        team_mode = mode.startswith("team") or mode in ("code.team", "agent.team")
        active_tools = {}
        while True:
            frame = await ws.receive_json()
            log_native("TUI事件", frame)
            if frame.get("type") == "res":
                if frame.get("ok") is False:
                    raise RuntimeError(str(frame.get("error") or frame.get("payload")))
                continue
            if frame.get("type") != "event":
                continue
            payload = frame.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("session_id") not in (None, "", self.session_key):
                continue
            event = frame.get("event")
            inner = payload.get("event_type")
            # 两个分支都可能在下一轮连接后才送达上一轮的终态；旧事件无请求编号。
            if event in ("chat.processing_status", "chat.final", "chat.error") and (
                payload.get("request_id") or payload.get("requestId")
            ) not in (None, "", request_id):
                continue
            if event == "chat.error" or inner == "team.error":
                raise RuntimeError(str(payload.get("error") or payload.get("message") or "Agent 执行失败"))
            if event in ("chat.ask_user_question", "plan.approval_required"):
                raise RuntimeError("当前为非交互执行，Agent 请求人工输入或审批；请使用普通模式或在 TUI 中处理")
            if event == "chat.delta":
                content += payload.get("content") or ""
            elif event in ("chat.llm_usage", "chat.usage_metadata", "chat.usage_summary") or (
                event == "chat.final"
                and inner in ("chat.llm_usage", "chat.usage_metadata", "chat.usage_summary")
            ):
                usage = (
                    payload.get("usage")
                    or payload.get("usage_metadata")
                    or payload.get("metadata")
                    or payload
                )
            elif event == "chat.tool_call":
                tool = payload.get("tool_call") or payload
                arguments = tool.get("arguments", tool.get("input", {}))
                call_id = tool.get("tool_call_id") or tool.get("call_id") or tool.get("id")
                if call_id not in active_tools:
                    name = tool.get("tool_name") or tool.get("name") or "unknown"
                    active_tools[call_id] = (name, arguments)
                    log_tool_start(call_id, name, arguments)
                messages.append({"role": "assistant", "tool_calls": [{
                    "id": tool.get("tool_call_id") or tool.get("call_id") or tool.get("id"),
                    "function": {
                        "name": tool.get("tool_name") or tool.get("name"),
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                    },
                }]})
            elif event == "chat.tool_result":
                tool_result = payload.get("tool_result")
                tool_result = tool_result if isinstance(tool_result, dict) else payload
                call_id = tool_result.get("tool_call_id") or tool_result.get("call_id") or tool_result.get("id")
                name, arguments = active_tools.pop(call_id, ("unknown", {}))
                log_tool_complete(call_id, name, arguments, tool_result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_result.get("tool_call_id") or tool_result.get("call_id") or tool_result.get("id"),
                    "content": tool_result.get("result", tool_result.get("output", tool_result.get("content", ""))),
                })
            elif event == "chat.final" and inner in (None, "", "chat.final"):
                final_content = payload.get("content")
                message = payload.get("message")
                if not final_content and isinstance(message, dict):
                    final_content = message.get("content") or message.get("text")
                content = final_content or content
                if not team_mode:
                    break
            elif event == "chat.processing_status" and (
                payload.get("is_processing") is False
                or str(payload.get("status") or "").lower() in ("complete", "completed", "done", "idle")
            ):
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
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        self.client.setup_agent(agent_config)
        self.workspace_manager.get_agent_workspace(agent_config.name)
        logger.info("设置 JiuwenSwarm Agent: %s | model=config.yaml", agent_config.name)


def make_jiuwenswarm_execute_with_retry(client: JiuwenSwarmClient):
    """返回 JiuwenSwarm 专用 execute_with_retry。

    重试机制已整体取消，单次 execute 的结果就是最终结果，失败直接抛。
    """

    async def execute_with_retry(agent: JiuwenSwarmAgent, query_text: str, options):
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
        # 服务端 chat.error 只给包装后的文案，真实原因补在这里
        raise JiuwenSwarmHarnessError(_with_real_cause(message))

    return execute_with_retry
