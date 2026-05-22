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
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
import os
import tempfile
from user_simulator import User_simulator

from pydantic import BaseModel, Field, validator, field_validator
from openclaw_sdk import OpenClawClient, AgentConfig, ExecutionOptions
from openclaw_sdk.core.config import ClientConfig
from openclaw_sdk.core.types import ExecutionResult
from openclaw_sdk.core.exceptions import GatewayError
from openclaw_sdk.gateway import protocol as _ocw_protocol
from openclaw_sdk.gateway.protocol import ProtocolGateway
from openclaw_sdk.core.exceptions import GatewayError as _GatewayError

# ---- 给底层 websockets 连接显式配置更激进的心跳,以便更快发现死连接 ----
# SDK 默认通过 ws_connect(url) 创建连接,沿用 websockets 库默认 ping_interval=20s。
# 这里覆写 _open_connection,把 ping_interval/ping_timeout 显式设短,
# 避免 NAT/代理静默断开后客户端长时间感知不到。
try:
    from websockets.asyncio.client import connect as _ws_connect_patched

    async def _patched_open_connection(ws_url: str, timeout: float):
        try:
            return await asyncio.wait_for(
                _ws_connect_patched(
                    ws_url,
                    ping_interval=15,
                    ping_timeout=10,
                    close_timeout=5,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise _GatewayError(
                f"Timed out connecting to {ws_url} after {timeout}s"
            ) from exc

    _ocw_protocol._open_connection = _patched_open_connection  # type: ignore[attr-defined]
except Exception as _patch_err:  # pragma: no cover - patch 失败不应阻止启动
    logging.getLogger("openclaw_automation").warning(
        "未能为 WebSocket 注入心跳参数,使用 SDK 默认值: %s", _patch_err
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

    ch = logging.StreamHandler()
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

DEFAULT_GATEWAY_TIMEOUT_SECONDS = 30
GATEWAY_CONNECT_GRACE_SECONDS = 5.0
READINESS_MAX_ATTEMPTS = 4
READINESS_WAIT_SECONDS = 40
EXECUTION_MAX_ATTEMPTS = 3
EXECUTION_RETRY_WAIT_SECONDS = 30


async def build_openclaw_client(
    gateway_ws_url: Optional[str] = None,
    api_key: Optional[str] = None,
    gateway_timeout: Optional[int] = None,
) -> OpenClawClient:
    """手动构造 client,确保 gateway_timeout 作用于 WS 建连超时。"""
    config = ClientConfig(
        mode="protocol" if gateway_ws_url else "auto",
        gateway_ws_url=gateway_ws_url,
        api_key=api_key,
        timeout=gateway_timeout or DEFAULT_GATEWAY_TIMEOUT_SECONDS,
    )
    connect_timeout = float(gateway_timeout or DEFAULT_GATEWAY_TIMEOUT_SECONDS)
    default_timeout = float(gateway_timeout or DEFAULT_GATEWAY_TIMEOUT_SECONDS)

    gateway = ProtocolGateway(
        ws_url=gateway_ws_url or "ws://127.0.0.1:18789/gateway",
        token=api_key,
        connect_timeout=connect_timeout,
        default_timeout=default_timeout,
        retry_policy=config.retry_policy,
    )
    try:
        # SDK 内部 connect() 可能在 backoff 循环里停留很久,这里再包一层总超时。
        await asyncio.wait_for(
            gateway.connect(),
            timeout=connect_timeout + GATEWAY_CONNECT_GRACE_SECONDS,
        )
    except Exception:
        await gateway.close()
        raise
    return OpenClawClient(config=config, gateway=gateway)


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
    timeout: Optional[int] = Field(300, description="超时时间(秒)")


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
            await self.client.create_agent(
                AgentConfig(
                    agent_id=agent_name,
                    workspace=str(workspace),
                )
            )
            logger.info("创建新 Agent: %s", agent_name)


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


async def execute_queries(
    client: OpenClawClient,
    queries: List[QueryItem],
    simulator: Optional[User_simulator] = None,
    max_turn: int = 5,
) -> Dict[str, ExecutionResult]:
    """执行查询任务列表

    外循环遍历每个 query;当 simulator 存在时,内循环进行多轮对话,
    受 max_turn 控制。

    Args:
        client: OpenClaw 客户端
        queries: 查询任务列表
        simulator: 用户模拟器,None 则仅单轮
        max_turn: 多轮对话最大轮次

    Returns:
        {result_agent_name: ExecutionResult}
    """
    logger.info("=" * 60)
    logger.info("开始执行查询任务")
    logger.info("=" * 60)

    results: Dict[str, ExecutionResult] = {}

    async def _rebuild_gateway() -> bool:
        """丢掉旧 ProtocolGateway,重建一个新的并替换 client.gateway。"""
        old_gw = client.gateway
        try:
            await asyncio.wait_for(old_gw.close(), timeout=5)
        except Exception as close_err:
            logger.debug("close 旧 gateway 连接时忽略异常: %s", close_err)

        # 从旧 gateway 中拿连接参数,避免依赖外部 self.config
        ws_url = getattr(old_gw, "_ws_url", None)
        token = getattr(old_gw, "_token", None)
        connect_timeout = getattr(old_gw, "_connect_timeout", float(DEFAULT_GATEWAY_TIMEOUT_SECONDS))
        default_timeout = getattr(old_gw, "_default_timeout", float(DEFAULT_GATEWAY_TIMEOUT_SECONDS))
        retry_policy = getattr(old_gw, "_retry_policy", None)

        if not ws_url:
            logger.error("旧 gateway 中拿不到 ws_url,无法重建")
            return False

        new_gw = ProtocolGateway(
            ws_url=ws_url,
            token=token,
            connect_timeout=connect_timeout,
            default_timeout=default_timeout,
            retry_policy=retry_policy,
        )
        try:
            await asyncio.wait_for(
                new_gw.connect(),
                timeout=connect_timeout + GATEWAY_CONNECT_GRACE_SECONDS,
            )
        except Exception as conn_err:
            logger.warning("重建 gateway 连接失败: %s", conn_err)
            try:
                await new_gw.close()
            except Exception:
                pass
            return False

        client._gateway = new_gw  # type: ignore[attr-defined]
        logger.info("gateway 连接已重建: %s", ws_url)
        return True

    async def wait_for_execution_ready(agent_name: str, session_name: str) -> None:
        """执行前主动探活 + 需要时重建连接,再测试当前 session。"""
        max_attempts = READINESS_MAX_ATTEMPTS
        wait_seconds = READINESS_WAIT_SECONDS

        for attempt in range(1, max_attempts + 1):
            # 1) 不信 _connected,主动探活一次 (使用 SDK 自带的 health 轻量 RPC)
            alive = False
            try:
                health = await asyncio.wait_for(client.gateway.health(), timeout=5)
                alive = bool(getattr(health, "healthy", False))
                if not alive:
                    logger.warning(
                        "gateway health 返回 unhealthy (attempt %d/%d): %s",
                        attempt, max_attempts, health,
                    )
            except Exception as e:
                logger.warning(
                    "gateway liveness probe 失败 (attempt %d/%d): %s",
                    attempt, max_attempts, e,
                )

            if not alive:
                logger.warning(
                    "gateway 未连接/不健康,第 %d/%d 次重建连接",
                    attempt, max_attempts,
                )
                ok = await _rebuild_gateway()
                if not ok:
                    if attempt < max_attempts:
                        await asyncio.sleep(wait_seconds)
                    continue

            # 2) session preview 测试 (同样包外层超时)
            agent = client.get_agent(agent_name, session_name)
            try:
                preview = await asyncio.wait_for(
                    agent.get_memory_status(), timeout=15
                )
                logger.info(
                    "execution ready: agent=%s session=%s attempt=%d preview=%s",
                    agent_name,
                    session_name,
                    attempt,
                    preview,
                )
                return
            except Exception as e:
                logger.warning(
                    "session preview 失败,第 %d/%d 次: agent=%s session=%s err=%s",
                    attempt,
                    max_attempts,
                    agent_name,
                    session_name,
                    e,
                )

            if attempt < max_attempts:
                logger.warning(
                    "等待 %d 秒后继续重连并测试: agent=%s session=%s",
                    wait_seconds,
                    agent_name,
                    session_name,
                )
                await asyncio.sleep(wait_seconds)

        raise RuntimeError(
            f"execution not ready after {max_attempts} attempts: "
            f"agent={agent_name} session={session_name}"
        )

    # base_session(原始 session_name) -> 当前生效 session_name
    # 当某个 session 在 execute_with_reconnect 内部因 max_attempts 用尽而被重命名时,
    # 新名字会写回这里,后续同名 base_session 的 query 直接复用最新值。
    session_overrides: Dict[str, str] = {}
    EXECUTION_MAX_RENAMES = 3

    def _allocate_session_name(base_session: str) -> str:
        suffix = _time.strftime("%H%M%S")
        return f"{base_session}_{_RUN_ID}_r{suffix}"

    async def execute_with_reconnect(
        agent,
        query_text: str,
        options: Optional[ExecutionOptions],
        base_session: Optional[str] = None,
    ):
        """执行查询;遇到 GatewayError 等待 10 秒后重连重试。

        额外处理:
        - 快速空内容(< 5s)→ 调 sessions.reset 清场后再试。
        - max_attempts 用尽时不直接 raise,而是给当前 base_session 分配一个新的
          session_name,写回 session_overrides,拿一个新的 agent 继续重试,最多
          重命名 EXECUTION_MAX_RENAMES 次。
        """
        max_attempts = EXECUTION_MAX_ATTEMPTS
        fast_empty_threshold = 5.0
        already_reset = False
        rename_count = 0

        async def _rename_and_recover(reason: str) -> bool:
            """分配新 session_name 并探活;返回是否成功切换。"""
            nonlocal agent, already_reset
            if base_session is None:
                return False
            new_session = _allocate_session_name(base_session)
            old_session = agent.session_name
            session_overrides[base_session] = new_session
            logger.warning(
                "max_attempts 用尽(%s),切换 session_name: agent=%s %s -> %s",
                reason, agent.agent_id, old_session, new_session,
            )
            agent = client.get_agent(agent.agent_id, new_session)
            already_reset = False
            try:
                await wait_for_execution_ready(agent.agent_id, new_session)
                return True
            except Exception as ready_err:
                logger.warning("切换 session 后探活失败(继续尝试执行): %s", ready_err)
                return True

        while True:
            for attempt in range(1, max_attempts + 1):
                t_start = asyncio.get_event_loop().time()
                try:
                    result = await agent.execute(query_text, options=options)
                    elapsed = asyncio.get_event_loop().time() - t_start
                    if result is None or not getattr(result, "content", None):
                        if elapsed < fast_empty_threshold and not already_reset:
                            logger.warning(
                                "execute 在 %.2fs 内返回空内容,判定为 session 卡死,调用 sessions.reset 清场",
                                elapsed,
                            )
                            try:
                                await asyncio.wait_for(agent.reset_memory(), timeout=10)
                                already_reset = True
                                logger.info(
                                    "session 已重置: agent=%s session=%s",
                                    agent.agent_id, agent.session_name,
                                )
                            except Exception as reset_err:
                                logger.warning("sessions.reset 失败: %s", reset_err)
                        raise RuntimeError("Agent returned empty content")
                    return result
                except GatewayError as e:
                    if attempt >= max_attempts:
                        logger.error(
                            "gateway 连续失败 %d 次 (rename=%d/%d): %s",
                            attempt, rename_count, EXECUTION_MAX_RENAMES, e,
                        )
                        if base_session is None or rename_count >= EXECUTION_MAX_RENAMES:
                            raise
                        rename_count += 1
                        await _rename_and_recover(f"GatewayError: {e}")
                        break  # break inner for; while 会重新进入新一轮 max_attempts

                    logger.warning(
                        "gateway 执行失败,第 %d/%d 次重试前等待 %d 秒: %s",
                        attempt,
                        max_attempts,
                        EXECUTION_RETRY_WAIT_SECONDS,
                        e,
                    )
                    await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
                    await wait_for_execution_ready(agent.agent_id, agent.session_name)
                except RuntimeError as e:
                    if attempt >= max_attempts:
                        logger.error(
                            "agent 连续返回空内容 %d 次 (rename=%d/%d): %s",
                            attempt, rename_count, EXECUTION_MAX_RENAMES, e,
                        )
                        if base_session is None or rename_count >= EXECUTION_MAX_RENAMES:
                            raise
                        rename_count += 1
                        await _rename_and_recover(f"empty content: {e}")
                        break

                    logger.warning(
                        "agent 返回空内容,第 %d/%d 次重试前等待 %d 秒: %s",
                        attempt,
                        max_attempts,
                        EXECUTION_RETRY_WAIT_SECONDS,
                        e,
                    )
                    # 空内容多数是 gateway surface_error 后返回的,ws 本身可能还在;
                    # 但连续多次 empty 很可能是连接已静默断开 - 从第 2 次起主动探活重连。
                    await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
                    if attempt >= 2:
                        logger.warning(
                            "连续 %d 次空内容,触发 readiness 探活/重连", attempt
                        )
                        try:
                            await wait_for_execution_ready(
                                agent.agent_id, agent.session_name
                            )
                        except Exception as ready_err:
                            logger.warning("empty 后重连失败(忽略,下轮重试): %s", ready_err)

    for idx, query in enumerate(queries, 1):
        logger.info("任务 %d/%d: [%s|%s]", idx, len(queries), query.agent_name, query.session_name)
        logger.info("[Q] %s", query.text)

        query_text = _replace_variables(query.text, results)
        options = ExecutionOptions(timeout_seconds=query.timeout) if query.timeout else None
        # 给 session_name 拼上 run-id 时间戳,避免跨 run 复用 gateway 残留 session
        base_session = query.session_name or "main"
        # 如果该 base_session 之前在 execute_with_reconnect 中被重命名过,直接复用最新值
        session_name = session_overrides.get(base_session, f"{base_session}_{_RUN_ID}")
        session_overrides[base_session] = session_name

        await wait_for_execution_ready(query.agent_name, session_name)

        if simulator is not None:
            simulator.update_origin_query(query_text)

        current_query = query_text
        last_result = None
        success = False
        retry = 0

        for turn in range(1, max_turn + 1 if simulator else 2):
            logger.debug("[Q%d] %s", turn, current_query)
            # 每轮重新读取 session_overrides,捕获上一次 execute_with_reconnect 内部可能发生的重命名
            session_name = session_overrides.get(base_session, session_name)
            agent = client.get_agent(query.agent_name, session_name)

            try:
                result = await execute_with_reconnect(
                    agent, current_query, options, base_session=base_session
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

            if simulator is None:
                success = True
                break

            user_reply = simulator.chat(agent_reply)
            logger.debug("[S%d] %s", turn, user_reply)

            if "【Task_Done】" in user_reply:
                logger.info("任务完成(Turn %d)", turn)
                try:
                    await execute_with_reconnect(
                        agent, "真棒", options, base_session=base_session
                    )
                except Exception:
                    pass
                success = True
                break
            elif "【Task_Failed】" in user_reply:
                logger.error("任务失败(Turn %d):%s", turn, user_reply)
                try:
                    await execute_with_reconnect(
                        agent, "好吧", options, base_session=base_session
                    )
                except Exception:
                    pass
                break

            current_query = user_reply
        else:
            if simulator is not None:
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

            # 3. 执行查询
            simulator = create_simulator(self.config)
            results = await execute_queries(
                client,
                self.config.queries,
                simulator=simulator,
                max_turn=self.config.user_max_turn,
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