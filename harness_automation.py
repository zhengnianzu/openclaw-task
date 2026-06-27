"""
统一自动化任务执行系统 (Harness Automation)

通过配置文件中的 harness_type 字段切换 openclaw / hermes 两种 harness 实现。
共享部分: Simulator 工厂、main/CLI 入口。
特有部分: WorkspaceManager / AgentManager 由 src/ 下的模块提供。
统一查询执行器: src.executor.execute_queries (回调注入差异)。
配置模型: src.config (AutomationConfig / ConfigLoader 等)。
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from user_simulator import User_simulator

from src.config import AutomationConfig, ConfigLoader

import time as _time
_RUN_ID = _time.strftime("%Y%m%dT%H%M%S")

PROJECT_ROOT = Path(__file__).parent.resolve()


# ============================================================================
# Logger
# ============================================================================

def setup_logger(config_file: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("harness_automation")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    if config_file:
        log_name = Path(config_file).stem + ".log"
    else:
        log_name = "harness_automation.log"

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


logger = logging.getLogger("harness_automation")


# ============================================================================
# Simulator 工厂函数
# ============================================================================

def create_simulator(config: AutomationConfig) -> Optional[User_simulator]:
    """根据配置创建 User_simulator 实例,无配置则返回 None"""

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
# 主执行器
# ============================================================================

class HarnessAutomation:
    """统一自动化任务执行主类,根据 harness_type 选择实现"""

    def __init__(self, config: AutomationConfig):
        self.config = config
        self.harness_type = config.harness_type

        if self.harness_type == "hermes":
            from src.hermes_client import HermesWorkspaceManager
            self.workspace_manager = HermesWorkspaceManager(config.workspace_base)
        else:
            from src.openclaw_client import OpenclawWorkspaceManager
            self.workspace_manager = OpenclawWorkspaceManager(config.workspace_base)

    async def run(self) -> Dict[str, Any]:
        """运行自动化流程"""
        logger.info("=" * 60)
        logger.info("自动化任务系统 (harness_type=%s)", self.harness_type)
        logger.info("=" * 60)

        if self.harness_type == "hermes":
            return await self._run_hermes()
        else:
            return await self._run_openclaw()

    async def _run_openclaw(self) -> Dict[str, Any]:
        from src.openclaw_client import (
            build_openclaw_client,
            OpenclawAgentManager,
            make_openclaw_execute_with_retry,
            openclaw_check_readyz,
        )
        from src.executor import execute_queries

        reconnect_config = {
            "gateway_ws_url": self.config.gateway_ws_url,
            "api_key": self.config.api_key,
            "gateway_timeout": self.config.gateway_timeout,
        }
        logger.debug("reconnect_config: %s", reconnect_config)

        async with await build_openclaw_client(**reconnect_config) as client:
            self.client = client

            await self._setup_workspaces()

            agent_manager = OpenclawAgentManager(client, self.workspace_manager)
            for agent_config in self.config.agents:
                await agent_manager.setup_agent(agent_config)

            simulator = create_simulator(self.config)
            results = await execute_queries(
                queries=self.config.queries,
                get_agent_fn=lambda name, session: client.get_agent(name, session),
                execute_with_retry_fn=make_openclaw_execute_with_retry(client),
                simulator=simulator,
                max_turn=self.config.user_max_turn,
                run_id=_RUN_ID,
                pre_query_hook=lambda: openclaw_check_readyz(client),
            )
            return results

    async def _run_hermes(self) -> Dict[str, Any]:
        from src.hermes_client import (
            build_hermes_client,
            HermesAgentManager,
            make_hermes_execute_with_retry,
            make_hermes_get_agent,
        )
        from src.executor import execute_queries

        legacy_oc = {}
        if self.config.gateway_ws_url:
            legacy_oc["gateway_ws_url"] = self.config.gateway_ws_url
        if self.config.gateway_timeout is not None:
            legacy_oc["gateway_timeout"] = self.config.gateway_timeout
        if self.config.api_key:
            legacy_oc["api_key"] = "<redacted>"
        if legacy_oc:
            logger.info(
                "[跨框架兼容] 接受到 openclaw 风格字段 %s; hermes 已全部忽略",
                legacy_oc,
            )

        async with await build_hermes_client() as client:
            self.client = client

            await self._setup_workspaces()

            agent_manager = HermesAgentManager(client, self.workspace_manager)
            for agent_config in self.config.agents:
                await agent_manager.setup_agent(agent_config)

            simulator = create_simulator(self.config)
            results = await execute_queries(
                queries=self.config.queries,
                get_agent_fn=make_hermes_get_agent(client, workspace_manager=self.workspace_manager),
                execute_with_retry_fn=make_hermes_execute_with_retry(client, workspace_manager=self.workspace_manager),
                simulator=simulator,
                max_turn=self.config.user_max_turn,
                run_id=_RUN_ID,
            )
            return results

    async def _setup_workspaces(self) -> None:
        """设置工作空间"""
        logger.info("设置工作空间...")

        user_dir_config = self.config.input_dir.user_dir
        user_dir_path: Optional[str] = None

        if user_dir_config:
            user_path = Path(user_dir_config.path).expanduser()
            content_root = user_path / user_path.name

            if not content_root.exists() or not content_root.is_dir():
                assert not user_dir_config.map_file, (
                    "input_dir.user_dir.map_file must be omitted when "
                    "user_path / user_path.name does not exist"
                )
            elif user_dir_config.map_file:
                map_path = self._resolve_map_file(user_dir_config.path, user_dir_config.map_file)
                data_dir = str(content_root)
                self.workspace_manager.setup_from_map(map_path, base_dir=data_dir)
            else:
                user_dir_path = user_dir_config.path

        for agent_config in self.config.agents:
            self.workspace_manager.setup_agent_files(
                agent_name=agent_config.name,
                config_files=agent_config.config,
                skill_base_dir=self.config.input_dir.skill_dir,
                agent_skills=agent_config.skills,
                agent_dir=self.config.input_dir.agent_dir,
                user_dir=user_dir_path,
            )

    @staticmethod
    def _resolve_map_file(base_path: str, map_file: str) -> str:
        p = Path(base_path) / map_file
        if not p.suffix:
            p = p.with_suffix('.json')
        return str(p)


# ============================================================================
# 主入口函数
# ============================================================================

async def main(config_file: Optional[str] = None, config_dict: Optional[Dict] = None) -> None:
    """主入口函数"""
    setup_logger(config_file)

    if config_file:
        config = ConfigLoader.load_from_file(config_file)
    elif config_dict:
        config = ConfigLoader.load_from_dict(config_dict)
    else:
        raise ValueError("必须提供 config_file 或 config_dict")

    automation = HarnessAutomation(config)
    results = await automation.run()

    logger.info("所有任务执行完成!")
    return results


# ============================================================================
# 命令行入口
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="自动化任务执行系统")
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

    asyncio.run(main(config_file=args.config))
