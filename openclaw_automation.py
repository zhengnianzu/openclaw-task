"""
OpenClaw 自动化任务执行系统

基于 openclaw-sdk 实现的配置驱动的任务自动化框架
支持多 Agent 协作、文件管理、技能安装、查询编排等功能
"""

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
import os
import sys
import tempfile
from user_simulator import User_simulator

from pydantic import BaseModel, Field, validator, field_validator
from openclaw_sdk import OpenClawClient, AgentConfig, ExecutionOptions
from openclaw_sdk.core.types import ExecutionResult
from openclaw_sdk.core.exceptions import GatewayError

from trajectory import Trajectory, build_turn_record, capture_file_evidence
from evaluator import Evaluator, EvaluatorConfig

from utils.connection import (
    build_openclaw_client,
    gateway_http_base,
    check_http_health,
)



def setup_logger(config_file: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("openclaw_automation")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    if config_file:
        log_name = Path(config_file).stem + ".log"
    else:
        log_name = "openclaw_automation.log"

    fh = logging.FileHandler(log_dir / log_name, encoding="utf-8", mode="w")
    fh.setLevel(logging.DEBUG)

    # 控制台流强制 UTF-8,与文件 handler 对齐,避免 Windows GBK 控制台输出中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass  # 某些被重定向/包装的流不支持 reconfigure,退回默认编码
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


logger = logging.getLogger("openclaw_automation")

# 本次 run 的唯一 id,用于 session_name 隔离,避免 gateway 跨 run 复用残留 session
import time as _time
_RUN_ID = _time.strftime("%Y%m%dT%H%M%S")

DEFAULT_GATEWAY_TIMEOUT_SECONDS = 3600
EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60
EXECUTION_HISTORY_FALLBACK_DELAY_SECONDS = 3
EXECUTION_HISTORY_FALLBACK_LIMIT = 50
EXECUTION_HISTORY_FALLBACK_MAX_POLLS = 40
EXECUTION_HISTORY_FALLBACK_POLL_INTERVAL_SECONDS = 30.0


# ============================================================================
# 配置模型定义
# ============================================================================

class SystemConfig(BaseModel):
    """系统配置"""
    platform: List[str] = Field(default=["windows", "linux"])
    python: str = Field(default="3.12")
    tools: List[str] = Field(default_factory=list)


class UserDirConfig(BaseModel):
    """用户目录配置"""
    path: str = Field(..., description="用户数据目录路径")
    map_file: Optional[str] = Field(None, description="映射文件名(相对于 path),如 'MAP_Linux',自动补 .json 后缀")
    profile_file: Optional[str] = Field(None, description="用户画像 JSON 文件名(相对于 path),如 'profile_analyzed.json'")


class InputDirConfig(BaseModel):
    """输入目录配置"""
    skill_dir: Optional[str] = Field(None, description="技能根目录路径,下面每个子目录对应一个技能")
    user_dir: Optional[UserDirConfig] = Field(None, description="用户目录,支持字符串路径或 {path, map_file} 对象")
    agent_dir: Optional[str] = Field(None, description="Agent 源文件目录,包含各 agent 的子目录(如 agent_dir/paper_reader/SOUL.md)")

    @field_validator('skill_dir', mode='before')
    @classmethod
    def coerce_skill_dir(cls, v):
        """兼容旧格式:空 dict {} 转为 None"""
        if isinstance(v, dict):
            return None
        return v

    @field_validator('user_dir', mode='before')
    @classmethod
    def coerce_user_dir(cls, v):
        """兼容旧格式,同时拦截 path 为 null 的情况"""
        # 1. 如果传进来的是普通字符串,转为对象
        if isinstance(v, str):
            return UserDirConfig(path=v)

        # 2. 核心修改:如果传进来的是字典,且 path 为 null (None)
        if isinstance(v, dict) and v.get('path') is None:
            # 分配一个系统的临时空目录作为"虚空地址"
            dummy_path = os.path.join(tempfile.gettempdir(), "openclaw_void_dir")

            # 如果这个虚空目录不存在,顺手建一个,防止后续文件系统操作报错
            if not os.path.exists(dummy_path):
                os.makedirs(dummy_path, exist_ok=True)

            v['path'] = dummy_path
            logging.getLogger("openclaw_automation").warning(
                "检测到 user_dir.path 为空,已自动重定向至虚空地址: %s", dummy_path)
        return v


class AgentConfigItem(BaseModel):
    """单个 Agent 配置"""
    name: str = Field(..., description="Agent 名称")
    config: List[str] = Field(default_factory=list, description="配置文件列表,如 USER.md, SOUL.md")
    skills: List[str] = Field(default_factory=list, description="所需技能列表")
    system_prompt: Optional[str] = Field(None, description="系统提示词")
    model: Optional[str] = Field(None, description="使用的模型")


class QueryItem(BaseModel):
    """查询任务配置"""
    agent_name: str = Field(..., description="执行的 Agent 名称")
    text: str = Field(..., description="查询文本,支持 {result_xxx} 变量替换")
    session_name: Optional[str] = Field("main", description="会话名称")
    timeout: Optional[int] = Field(3600, description="超时时间(秒)")
    use_simulator: bool = Field(True, description="是否启用 user-simulator 进行多轮对话,默认 True")
    rubric: List[str] = Field(default_factory=list, description="验收清单:随本 query 传入,整段多轮对话中冻结,供 evaluator 逐条质检;空=退回自由维度评估")


class AutomationConfig(BaseModel):
    """完整的自动化配置"""
    system: SystemConfig = Field(default_factory=SystemConfig)
    input_dir: InputDirConfig = Field(default_factory=InputDirConfig)
    agents: List[AgentConfigItem] = Field(default_factory=list)
    queries: List[QueryItem] = Field(default_factory=list)

    # OpenClaw 连接配置
    gateway_ws_url: Optional[str] = Field(None, description="WebSocket 网关 URL")
    api_key: Optional[str] = Field(None, description="API Key")
    gateway_timeout: Optional[int] = Field(None, description="Gateway 连接/调用超时(秒)")
    workspace_base: str = Field(r"C:\Users\nianzu\.openclaw\workspace", description="工作空间基础目录")

    # User Simulator 配置
    user_profile: str = Field("", description="用户画像兜底文本,profile_file 不存在时使用")
    simulator_config: Optional[str] = Field(None, description="Simulator 配置 JSON 绝对路径,含 model/api_key/base_url/proxy;不配置则从环境变量读取")
    user_max_turn: int = Field(5, description="多轮对话最大轮次")

    # Evaluator(第三方裁判)配置;默认 enabled=False → 退回 simulator 自判旧行为
    eval_config: EvaluatorConfig = Field(default_factory=EvaluatorConfig, description="第三方 Evaluator 配置段")

    @field_validator("gateway_ws_url", mode="before")
    @classmethod
    def normalize_gateway_ws_url(cls, v):
        """兼容只填 host:port 的写法,自动补全 /gateway。"""
        if not isinstance(v, str):
            return v

        url = v.strip()
        if not url:
            return None

        if url.startswith(("ws://", "wss://")) and "/gateway" not in url:
            return url.rstrip("/") + "/gateway"

        return url


# ============================================================================
# 工作空间管理器
# ============================================================================

class WorkspaceManager:
    """管理 Agent 工作空间和文件"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        """获取 Agent 工作空间路径

        规则:
        - 如果 agent_name 是 "main",返回 base_dir
        - 否则返回 base_dir-agent_name (例如: workspace-paper_reader)
        """
        if agent_name == "main":
            workspace = self.base_dir
        else:
            # 构造 workspace-<agent_name> 格式
            parent = self.base_dir.parent
            base_name = self.base_dir.name
            workspace = parent / f"{base_name}-{agent_name}"

        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def setup_agent_files(
        self,
        agent_name: str,
        config_files: List[str],
        skill_base_dir: Optional[str],
        agent_skills: List[str],
        agent_dir: Optional[str] = None,
        user_dir: Optional[str] = None
    ) -> None:
        """设置 Agent 工作空间文件

        Args:
            agent_name: Agent 名称
            config_files: 配置文件列表(如 SOUL.md, USER.md)
            skill_base_dir: 技能根目录,下面每个子目录对应一个技能
            agent_skills: 该 agent 需要的技能名称列表
            agent_dir: Agent 源文件目录,包含配置文件(如 SOUL.md, USER.md)
            user_dir: 用户数据目录(整体复制到 workspace)
        """
        workspace = self.get_agent_workspace(agent_name)

        logger.info("workspace: %s", workspace)
        if skill_base_dir and agent_skills:
            logger.info("skills_dst: %s", workspace / 'skills')
        if user_dir:
            logger.info("user_dir -> workspace: %s -> %s", Path(user_dir).expanduser(), workspace)

        # 1. 从 agent_dir/ 复制配置文件(SOUL.md, USER.md, TOOLS.md 等)
        if agent_dir and config_files:
            agent_source = Path(agent_dir).expanduser()
            if agent_source.exists():
                for config_file in config_files:
                    src = agent_source / config_file
                    if src.exists():
                        dst = workspace / config_file
                        shutil.copy2(src, dst)
                        logger.info("复制 Agent 配置: %s -> %s", config_file, dst)
                        # 同时复制到 workspace 主目录
                        dst_main = self.base_dir / config_file
                        shutil.copy2(src, dst_main)
                        logger.info("复制 Agent 配置: %s -> %s", config_file, dst_main)
                    else:
                        logger.warning("Agent 配置文件不存在: %s", src)
            else:
                logger.warning("Agent 源目录不存在: %s", agent_source)

        # 2. 复制技能目录:skill_base_dir/<skill_path>/ -> workspace/skills/<skill_name>/
        if skill_base_dir and agent_skills:
            skills_dst = workspace / "skills"
            skills_dst.mkdir(exist_ok=True)
            for skill_path in agent_skills:
                # skill_path 形如 "category/author__skill-name",取最后一段作为目标目录名
                skill_name = Path(skill_path).name
                src = Path(skill_base_dir) / skill_path
                if src.exists() and src.is_dir():
                    dst = skills_dst / skill_name
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    logger.info("复制技能: %s -> %s", skill_path, dst)
                else:
                    logger.warning("技能目录不存在: %s", src)

        # 3. 整体复制 user_dir 到 workspace
        if user_dir:
            user_path = Path(user_dir).expanduser()
            logger.debug("check user_path: %s", user_path)
            if user_path.exists() and user_path.is_dir():
                content_root = user_path / user_path.name

                if not content_root.exists() or not content_root.is_dir():
                    logger.warning("user_dir content root does not exist or is not a directory: %s", content_root)
                    return

                # 复制到 workspace 根目录
                for item in content_root.iterdir():
                    item_dst = workspace / item.name
                    if item_dst.exists():
                        if item_dst.is_dir():
                            shutil.rmtree(item_dst)
                        else:
                            item_dst.unlink()
                    if item.is_dir():
                        shutil.copytree(item, item_dst)
                    else:
                        shutil.copy2(item, item_dst)
                logger.info("复制用户目录: %s -> %s", content_root, workspace)
            else:
                logger.warning("用户目录不存在或不是目录: %s", user_path)

    def setup_from_map(self, map_file: str, base_dir: Optional[str] = None) -> None:
        """根据 map.json 按映射逐条复制文件/目录

        Args:
            map_file: map.json 路径,格式 {"src_path": "dst_path"}
            base_dir: 若提供,map 的 key(源路径)相对于此目录解析;
                      否则 key 视为绝对路径(支持 ~ 展开)
                      dst 路径始终支持 ~ 展开,不存在时自动创建父目录
        """
        map_path = Path(map_file)
        if not map_path.exists():
            logger.warning("map 文件不存在: %s", map_path)
            return

        mapping: Dict[str, str] = json.loads(map_path.read_text(encoding="utf-8"))
        base = Path(base_dir) if base_dir else None
        logger.info("读取 map 文件: %s,共 %d 条映射", map_path, len(mapping))
        if base:
            logger.info("源路径基准目录: %s", base)

        for src_str, dst_str in mapping.items():
            src = (base / src_str) if base else Path(src_str).expanduser()
            dst = Path(dst_str).expanduser()

            if not src.exists():
                logger.warning("源路径不存在,跳过: %s", src)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)

            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

            logger.info("映射复制: %s -> %s", src_str, dst_str)


# ============================================================================
# Simulator 工厂函数
# ============================================================================

def create_simulator(config: "AutomationConfig") -> Optional[User_simulator]:
    """根据配置创建 User_simulator 实例,无配置则返回 None"""
    import os

    user_profile = config.user_profile
    user_dir_cfg = config.input_dir.user_dir
    if user_dir_cfg:
        profile_filename = user_dir_cfg.profile_file or "user_profile.json"
        profile_path = Path(user_dir_cfg.path) / profile_filename
        if profile_path.exists():
            profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
            user_profile = json.dumps(profile_data, ensure_ascii=False, indent=2)
        elif user_dir_cfg.profile_file:
            logger.warning("profile_file 不存在: %s,回退到 config.user_profile", profile_path)

    if not config.simulator_config:
        logger.info("simulator_config 未配置,将跳过多轮对话(仅执行单轮)")
        return None

    proxy_cfg_path = Path(config.simulator_config)
    if proxy_cfg_path.exists():
        proxy_cfg = json.loads(proxy_cfg_path.read_text(encoding="utf-8"))
        logger.info("Simulator 配置来自: %s", proxy_cfg_path)
    else:
        logger.warning("simulator_config 文件不存在: %s,回退到环境变量", proxy_cfg_path)
        proxy_cfg = {}

    model    = proxy_cfg.get("model")    or os.environ.get("SIMULATOR_MODEL", "gpt-4o")
    api_key  = proxy_cfg.get("api_key")  or os.environ.get("SIMULATOR_OPENAI_API_KEY")
    base_url = proxy_cfg.get("base_url") or os.environ.get("SIMULATOR_OPENAI_BASE_URL")
    proxy    = proxy_cfg.get("proxy")    or os.environ.get("SIMULATOR_PROXY")

    user_directory = ""
    if user_dir_cfg:
        root = Path(user_dir_cfg.path)
        if root.exists():
            lines = []
            for p in sorted(root.rglob("*")):
                depth = len(p.relative_to(root).parts) - 1
                indent = "    " * depth
                lines.append(f"{indent}{'└── ' if p.is_file() else ''}{p.name}{'/' if p.is_dir() else ''}")
            user_directory = "\n".join(lines)

    return User_simulator(
        origin_query="",
        user_profile=user_profile,
        user_directory=user_directory,
        model=model,
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
    )


def create_evaluator(config: EvaluatorConfig, client: Any, run_id: str) -> Optional[Evaluator]:
    """根据配置创建 Evaluator 实例,未启用则返回 None"""
    return Evaluator.create(config, client, run_id)


# ============================================================================
# Agent 管理器
# ============================================================================

class AgentManager:
    """管理 Agent 的创建和注册"""

    def __init__(self, client: OpenClawClient, workspace_manager: WorkspaceManager):
        self.client = client
        self.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config: AgentConfigItem) -> None:
        """设置单个 Agent

        Args:
            agent_config: Agent 配置
        """
        agent_name = agent_config.name
        logger.info("设置 Agent: %s", agent_name)

        existing_ids = {a.agent_id for a in await self.client.list_agents()}

        if agent_name not in existing_ids:
            workspace = self.workspace_manager.get_agent_workspace(agent_name)
            # SDK 的 create_agent 不从 AgentConfig 读 workspace,必须显式传 kwarg,
            # 否则 gateway 收到 "." → 解析为其自身 cwd(可能是 system32)→ EPERM。
            await self.client.create_agent(
                AgentConfig(
                    agent_id=agent_name,
                    workspace=str(workspace),
                ),
                workspace=str(workspace),
            )
            logger.info("创建新 Agent: %s,等待 gateway 重启就绪...", agent_name)
            await self._wait_gateway_ready()

    async def _wait_gateway_ready(self, wait: float = 90.0) -> None:
        """创建 agent 后 gateway 会重启,固定等待一段时间让其就绪。"""
        logger.info("等待 gateway 重启就绪,固定等待 %ds ...", int(wait))
        await asyncio.sleep(wait)
        logger.info("gateway 等待完成")


# ============================================================================
# 查询执行
# ============================================================================

def _replace_variables(text: str, results: Dict[str, ExecutionResult]) -> str:
    """替换查询文本中的变量,支持 {result_agent_name}"""
    pattern = r'\{result_(\w+)\}'

    def replacer(match):
        result_key = match.group(0)[1:-1]
        result = results.get(result_key)
        if result is None:
            return f"[Error: {result_key} not found]"
        elif hasattr(result, 'content'):
            return result.content
        return str(result)

    return re.sub(pattern, replacer, text)


async def process_turn(
    client: OpenClawClient,
    query: QueryItem,
    turn: int,
    current_query: str,
    result: ExecutionResult,
    evidence_incomplete: bool,
    trajectory: Trajectory,
    evaluator: Optional[Evaluator],
    last_feedback: Optional[str],
    frozen_rubric: List[str],
) -> tuple[Optional[str], Optional[str]]:
    """逐轮处理(仅多轮 simulator 路径):能力1 捕获带证据轨迹 + 能力2 第三方评估。

    单轮对话不进入本函数(不采集轨迹)。两段逻辑高内聚:能力2 的评估依赖能力1
    刚产出的 turn_record 与累积 trajectory,故合并为一次调用。

    evaluator 未启用时,轨迹无人消费(evaluator 是其唯一 reader),故前置返回——
    既不评估也不采集轨迹。

    Returns:
        (evaluator_feedback, last_feedback)
        - evaluator_feedback: 本轮喂回 simulator 的反馈(feedback_to_simulator=False 时为 None)
        - last_feedback: 更新后的上一轮反馈(供下一轮无状态投喂)
    """
    # evaluator 未启用:轨迹无人消费,既不评估也不采集,直接返回默认
    if evaluator is None or not evaluator.enabled:
        return None, last_feedback

    # 能力1:逐轮捕获带证据的轨迹(tool_calls 内存直取,免费;文件证据升级为磁盘真相 D5)
    turn_record = build_turn_record(turn, current_query, result, evidence_incomplete)
    try:
        await capture_file_evidence(client.gateway, query.agent_name, turn_record)
    except Exception as e:  # noqa: BLE001
        logger.debug("文件证据捕获失败: %s", e)
    trajectory.turns.append(turn_record)

    # 能力2:逐轮第三方评估,反馈喂回 simulator(simulator 仍拍板)
    evaluator_feedback: Optional[str] = None
    try:
        ev = await evaluator.evaluate_turn(
            trajectory, turn_record, last_feedback, rubric=frozen_rubric
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("evaluator 调用异常: %s", e)
        ev = None
    if ev is not None:
        last_feedback = evaluator.format_feedback(ev)
        if evaluator.feedback_to_simulator:
            evaluator_feedback = last_feedback

    return evaluator_feedback, last_feedback


async def execute_queries(
    client: OpenClawClient,
    queries: List[QueryItem],
    simulator_factory: Optional[Callable[[], Optional[User_simulator]]] = None,
    max_turn: int = 5,
    evaluator: Optional[Evaluator] = None,
) -> Dict[str, ExecutionResult]:
    """执行查询任务列表

    外循环遍历每个 query;当 simulator 存在时,内循环进行多轮对话,
    受 max_turn 控制。每轮捕获带证据的轨迹;当 evaluator 启用时,逐轮评估并把
    反馈喂回 simulator(由 simulator 拍板)。

    simulator 记忆按 `session_name` 隔离:共享同一逻辑会话名的 query 复用同一个
    User_simulator 实例(合法续聊);不同会话名互不可见(杜绝跨会话信息泄露)。

    Args:
        client: OpenClaw 客户端
        queries: 查询任务列表
        simulator_factory: 构造 User_simulator 的工厂(每个 session 调用一次);
            返回 None 表示未启用 simulator → 仅单轮
        max_turn: 多轮对话最大轮次
        evaluator: 第三方 Evaluator,None 或 disabled 则退回 simulator 自判

    Returns:
        {result_agent_name: ExecutionResult}
    """
    logger.info("=" * 60)
    logger.info("开始执行查询任务")
    logger.info("=" * 60)

    results: Dict[str, ExecutionResult] = {}

    async def check_readyz() -> None:
        """执行前 HTTP readyz 诊断日志(连接重连由 monkey-patch 自动处理)。"""
        http_base = gateway_http_base(client.gateway)
        if not http_base:
            return
        _, ready, body = await check_http_health(http_base)
        if ready:
            logger.info("gateway readyz OK: %s", body)
        else:
            logger.warning("gateway readyz 未就绪: %s", body)

    async def execute_with_retry(
        agent,
        query_text: str,
        options: Optional[ExecutionOptions],
    ):
        """执行查询,空 final 时先查 history 兜底,再有限重试。

        Returns:
            (ExecutionResult, evidence_incomplete): 第二个值为 True 表示该结果经
            history_fallback 兜底恢复(只剩文本、无工具/文件证据),供轨迹捕获标记。
        """
        max_attempts = EXECUTION_MAX_ATTEMPTS

        def extract_message_text(message: Any) -> str:
            if not isinstance(message, dict):
                return ""
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                return "".join(parts).strip()
            text = message.get("text")
            return text.strip() if isinstance(text, str) else ""

        def is_assistant_message(message: Any) -> bool:
            return (
                isinstance(message, dict)
                and str(message.get("role", "")).lower() == "assistant"
            )

        async def fetch_history() -> List[dict[str, Any]]:
            try:
                return await agent._client.gateway.chat_history(  # type: ignore[attr-defined]
                    agent.session_key,
                    limit=EXECUTION_HISTORY_FALLBACK_LIMIT,
                )
            except Exception as e:
                logger.debug("chat.history 兜底查询失败: %s", e)
                return []

        def find_new_assistant_text(
            before: List[dict[str, Any]],
            after: List[dict[str, Any]],
        ) -> str:
            before_signatures = {
                (
                    str(message.get("role", "")),
                    extract_message_text(message),
                    str(message.get("timestamp", "")),
                    str(message.get("id", "")),
                )
                for message in before
                if isinstance(message, dict)
            }
            new_messages = []
            for message in after:
                if not isinstance(message, dict):
                    continue
                signature = (
                    str(message.get("role", "")),
                    extract_message_text(message),
                    str(message.get("timestamp", "")),
                    str(message.get("id", "")),
                )
                if signature not in before_signatures:
                    new_messages.append(message)
            for message in reversed(new_messages):
                if is_assistant_message(message):
                    text = extract_message_text(message)
                    if text:
                        return text
            return ""

        async def history_fallback(
            before_history: List[dict[str, Any]],
            max_polls: int = EXECUTION_HISTORY_FALLBACK_MAX_POLLS,
            poll_interval: float = EXECUTION_HISTORY_FALLBACK_POLL_INTERVAL_SECONDS,
        ) -> Optional[str]:
            """轮询 chat.history 等待旧 run 完成。

            agent 长任务可能还在后台执行（WS 断开但 run 没停），
            不能只查一次就放弃——需要多轮轮询直到出现新的 assistant 回复。
            """
            for poll in range(1, max_polls + 1):
                await asyncio.sleep(poll_interval)
                try:
                    after_history = await fetch_history()
                except Exception as e:
                    logger.debug("history_fallback 第 %d/%d 次查询失败: %s", poll, max_polls, e)
                    continue
                text = find_new_assistant_text(before_history, after_history)
                if text:
                    logger.info(
                        "execute 返回空内容,但第 %d 次 history 轮询获取到回复 (等待 %.0fs)",
                        poll, poll * poll_interval,
                    )
                    return text
                logger.debug(
                    "history_fallback 第 %d/%d 次轮询,暂无新回复",
                    poll, max_polls,
                )
            return None

        for attempt in range(1, max_attempts + 1):
            before_history = await fetch_history()
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise RuntimeError("Agent returned None")

                if getattr(result, "content", None):
                    return result, False

                fallback_text = await history_fallback(before_history)
                if fallback_text:
                    return result.model_copy(
                        update={
                            "success": True,
                            "content": fallback_text,
                            "stop_reason": result.stop_reason or "complete",
                            "error_message": None,
                        }
                    ), True

                error_message = getattr(result, "error_message", None)
                if error_message and not str(error_message).startswith(
                    "Agent completed with no response"
                ):
                    raise RuntimeError(error_message)

                raise RuntimeError(
                    "Agent returned empty content and chat.history had no new assistant reply"
                )
            except (GatewayError, asyncio.TimeoutError) as e:
                logger.warning(
                    "gateway 连接异常 (第 %d/%d 次): %s，先查 history 看旧 run 是否已完成",
                    attempt, max_attempts, e,
                )
                gw = client.gateway
                if hasattr(gw, "ensure_connected"):
                    try:
                        await gw.ensure_connected(timeout=DEFAULT_GATEWAY_TIMEOUT_SECONDS)
                        logger.info("gateway 重连恢复")
                    except GatewayError:
                        logger.warning("gateway 重连未恢复")

                fallback_text = await history_fallback(before_history)
                if fallback_text:
                    logger.info("WS 断开但 agent 已完成,从 history 获取到回复")
                    return ExecutionResult(
                        success=True,
                        content=fallback_text,
                        stop_reason="complete",
                    ), True

                if attempt >= max_attempts:
                    raise
                logger.warning(
                    "history 也无结果,第 %d/%d 次重试前等待 %d 秒",
                    attempt, max_attempts, EXECUTION_RETRY_WAIT_SECONDS,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
                continue
            except RuntimeError as e:
                if attempt >= max_attempts:
                    logger.error("agent 连续返回空内容 %d 次: %s", attempt, e)
                    raise
                logger.warning(
                    "agent 返回空内容且 history 无兜底,第 %d/%d 次重试前等待 %d 秒: %s",
                    attempt, max_attempts, EXECUTION_RETRY_WAIT_SECONDS, e,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)

    # simulator 按逻辑 session_name 隔离记忆:同会话复用实例、跨会话互不可见
    simulators: Dict[str, User_simulator] = {}

    for idx, query in enumerate(queries, 1):
        logger.info("任务 %d/%d: [%s|%s]", idx, len(queries), query.agent_name, query.session_name)
        logger.info("[Q] %s", query.text)

        query_text = _replace_variables(query.text, results)
        options = ExecutionOptions(timeout_seconds=query.timeout) if query.timeout else None
        base_session = query.session_name or "main"
        session_name = f"{base_session}_{_RUN_ID}"

        await check_readyz()

        # 按逻辑 session_name(裸名,不含 _RUN_ID)取/建 simulator 实例:
        # 首见即建、再见复用 → 同会话续聊保留记忆,跨会话隔离不泄露。
        query_simulator: Optional[User_simulator] = None
        if query.use_simulator and simulator_factory is not None:
            query_simulator = simulators.get(base_session)
            if query_simulator is None:
                query_simulator = simulator_factory()
                if query_simulator is not None:
                    simulators[base_session] = query_simulator

        if query_simulator is not None:
            query_simulator.update_origin_query(query_text)

        current_query = query_text
        last_result = None
        success = False
        retry = 0

        # 能力1:逐轮累积带证据的轨迹
        trajectory = Trajectory(query=query_text, agent_name=query.agent_name)
        last_feedback: Optional[str] = None  # 上一轮 evaluator 反馈(供无状态投喂)
        frozen_rubric = list(query.rubric)  # 随 query 传入并在整段对话中冻结(循环内不改写)

        for turn in range(1, max_turn + 1 if query_simulator else 2):
            logger.debug("[Q%d] %s", turn, current_query)
            agent = client.get_agent(query.agent_name, session_name)

            try:
                result, evidence_incomplete = await execute_with_retry(
                    agent, current_query, options
                )
                last_result = result
                agent_reply = result.content
                logger.info("[A%d] %s", turn, agent_reply)
                if not agent_reply:
                    logger.debug(result)

            except Exception as e:
                import traceback
                logger.error("Agent 执行失败: %s", e)
                logger.debug(traceback.format_exc())
                break

            if not agent_reply:
                retry += 1
                if retry >= 3:
                    logger.error("连续3次未收到回复,任务失败")
                    break
                current_query = "没有看到你的回复,请重新执行。"
                continue

            retry = 0

            # 单轮对话:不采集轨迹进行评估，因为user_simulator不再回复，无需接收评估结果
            if query_simulator is None:
                success = True
                break

            # 能力1+能力2:逐轮捕获带证据轨迹 + 第三方评估,反馈喂回 simulator
            evaluator_feedback, last_feedback = await process_turn(
                client, query, turn, current_query, result, evidence_incomplete,
                trajectory, evaluator, last_feedback, frozen_rubric,
            )

            user_reply = query_simulator.chat(agent_reply, evaluator_feedback=evaluator_feedback)
            logger.debug("[S%d] %s", turn, user_reply)

            if "【Task_Done】" in user_reply:
                logger.info("任务完成(Turn %d)", turn)
                trajectory.outcome = "done"
                try:
                    await execute_with_retry(
                        agent, "真棒", options
                    )
                except Exception:
                    pass
                success = True
                break
            elif "【Task_Failed】" in user_reply:
                logger.error("任务失败(Turn %d):%s", turn, user_reply)
                trajectory.outcome = "failed"
                try:
                    await execute_with_retry(
                        agent, "好吧", options
                    )
                except Exception:
                    pass
                break

            current_query = user_reply
        else:
            if query_simulator is not None:
                trajectory.outcome = "max_turn"
                logger.warning("达到最大轮次 %d,任务未完成", max_turn)

        results[f"result_{query.agent_name}"] = last_result

        if not success:
            logger.error("任务 %d 失败,终止后续 %d 个任务", idx, len(queries) - idx)
            break

    return results



# ============================================================================
# 主执行器
# ============================================================================

class OpenClawAutomation:
    """OpenClaw 自动化任务执行主类"""

    def __init__(self, config: AutomationConfig):
        self.config = config
        self.workspace_manager = WorkspaceManager(config.workspace_base)

    async def run(self) -> Dict[str, ExecutionResult]:
        """运行自动化流程"""
        logger.info("=" * 60)
        logger.info("OpenClaw 自动化任务系统")
        logger.info("=" * 60)

        reconnect_config = {
            "gateway_ws_url": self.config.gateway_ws_url,
            "api_key": self.config.api_key,
            "gateway_timeout": self.config.gateway_timeout,
        }
        logger.debug("reconnect_config: %s", reconnect_config)

        async with await build_openclaw_client(**reconnect_config) as client:
            self.client = client

            # 1. 设置工作空间
            await self._setup_workspaces()

            # 2. 注册 Agents
            await self._setup_agents()
            # 3. 创建 Simulator 工厂(每个 session 构造一个独立实例,隔离记忆)
            simulator_factory = lambda: create_simulator(self.config)
            # 4. 创建 Evaluator
            evaluator = create_evaluator(self.config.eval_config, client, _RUN_ID)
            # 5. 执行查询
            results = await execute_queries(
                client,
                self.config.queries,
                simulator_factory=simulator_factory,
                max_turn=self.config.user_max_turn,
                evaluator=evaluator,
            )

            return results

    async def _setup_workspaces(self) -> None:
        """设置工作空间"""
        logger.info("设置工作空间...")

        # 解析 user_dir:有 map_file 则按映射复制,否则整体复制(旧行为)
        user_dir_config = self.config.input_dir.user_dir
        user_dir_path: Optional[str] = None

        if user_dir_config:
            user_path = Path(user_dir_config.path).expanduser()
            content_root = user_path / user_path.name

            if not content_root.exists() or not content_root.is_dir():
                # env文件夹不存在时,copy_map_not_workspace必须不存在
                assert not user_dir_config.map_file, (
                    "input_dir.user_dir.map_file must be omitted when "
                    "user_path / user_path.name does not exist"
                )
            elif user_dir_config.map_file:
                # env文件夹存在,copy_map_not_workspace是True时,map_file必须存在。
                map_path = self._resolve_map_file(user_dir_config.path, user_dir_config.map_file)
                # 数据子目录 = user_dir.path / user_dir_name(同名子文件夹)
                data_dir = str(content_root)
                self.workspace_manager.setup_from_map(map_path, base_dir=data_dir)
            else:
                # env文件夹存在时,copy_map_not_workspace必须显式设置成True或False
                user_dir_path = user_dir_config.path
                    # copy_map_not_workspace是False时,不会按照map_file的指导复制,此时如果存在map_file,需要给出wraning

        for agent_config in self.config.agents:
            self.workspace_manager.setup_agent_files(
                agent_name=agent_config.name,
                config_files=agent_config.config,
                skill_base_dir=self.config.input_dir.skill_dir,
                agent_skills=agent_config.skills,
                agent_dir=self.config.input_dir.agent_dir,
                user_dir=user_dir_path
            )

    @staticmethod
    def _resolve_map_file(base_path: str, map_file: str) -> str:
        """解析 map_file 路径:相对于 base_path,自动补 .json 后缀"""
        p = Path(base_path) / map_file
        if not p.suffix:
            p = p.with_suffix('.json')
        return str(p)

    async def _setup_agents(self) -> None:
        """注册 Agents 到 gateway"""
        logger.info("设置 Agents...")

        agent_manager = AgentManager(self.client, self.workspace_manager)

        for agent_config in self.config.agents:
            await agent_manager.setup_agent(agent_config)

        await self._setup_evaluator_agent(agent_manager)

    async def _setup_evaluator_agent(self, agent_manager: "AgentManager") -> None:
        """注册独立 evaluator agent(D1:须 ≠ 任一执行 agent;独立工作区)。

        注意:首次创建会触发约 90s 网关重启等待(见 AgentManager)。模型对齐
        (D10)受限——SDK `agents.create` 仅传 name/workspace,不下发 llm_model,
        故此处只记录意图、回退网关默认模型并告警。
        """
        ev_cfg = self.config.eval_config
        if not ev_cfg.enabled:
            return

        exec_agent_names = {a.name for a in self.config.agents}
        if ev_cfg.agent_name in exec_agent_names:
            raise ValueError(
                f"evaluator.agent_name '{ev_cfg.agent_name}' 不得与任一执行 agent 同名(须独立)"
            )

        if ev_cfg.model:
            logger.warning(
                "evaluator 期望模型=%s,但网关 agents.create 不下发模型,实际将用网关默认模型",
                ev_cfg.model,
            )

        await agent_manager.setup_agent(AgentConfigItem(name=ev_cfg.agent_name))
        logger.info("已注册独立 evaluator agent: %s", ev_cfg.agent_name)



# ============================================================================
# 配置加载器
# ============================================================================

class ConfigLoader:
    """配置文件加载器"""

    @staticmethod
    def load_from_file(file_path: str) -> AutomationConfig:
        """从文件加载配置

        支持 JSON 和 YAML 格式
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {file_path}")

        content = path.read_text(encoding="utf-8")

        # 尝试解析 JSON
        if path.suffix.lower() in ['.json']:
            data = json.loads(content)
        elif path.suffix.lower() in ['.yaml', '.yml']:
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                raise ImportError("YAML 支持需要安装 PyYAML: pip install pyyaml")
        else:
            # 默认尝试 JSON
            data = json.loads(content)

        return AutomationConfig(**data)

    @staticmethod
    def load_from_dict(data: Dict[str, Any]) -> AutomationConfig:
        """从字典加载配置"""
        return AutomationConfig(**data)


# ============================================================================
# 主入口函数
# ============================================================================

async def main(config_file: Optional[str] = None, config_dict: Optional[Dict] = None) -> None:
    """主入口函数

    Args:
        config_file: 配置文件路径
        config_dict: 配置字典(直接传入)

    Examples:
        # 从文件加载
        await main(config_file="config.json")

        # 从字典加载
        await main(config_dict={...})
    """
    # 初始化 logger
    setup_logger(config_file)

    # 加载配置
    if config_file:
        config = ConfigLoader.load_from_file(config_file)
    elif config_dict:
        config = ConfigLoader.load_from_dict(config_dict)
    else:
        raise ValueError("必须提供 config_file 或 config_dict")

    # 运行自动化流程
    automation = OpenClawAutomation(config)
    results = await automation.run()

    logger.info("所有任务执行完成!")
    return results


# ============================================================================
# 命令行入口
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw 自动化任务执行系统")
    parser.add_argument(
        "--config",
        help="配置文件路径 (JSON/YAML)"
    )
    parser.add_argument(
        "--workspace",
        default="./workspaces",
        help="工作空间基础目录"
    )

    args = parser.parse_args()

    # 运行
    asyncio.run(main(config_file=args.config))
