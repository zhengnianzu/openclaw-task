"""
OpenClaw 自动化任务执行系统

基于 openclaw-sdk 实现的配置驱动的任务自动化框架
支持多 Agent 协作、文件管理、技能安装、查询编排等功能
"""

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, validator
from openclaw_sdk import OpenClawClient, AgentConfig, ExecutionOptions
from openclaw_sdk.core.types import ExecutionResult


# ============================================================================
# 配置模型定义
# ============================================================================

class SystemConfig(BaseModel):
    """系统配置"""
    platform: List[str] = Field(default=["windows", "linux"])
    python: str = Field(default="3.11")
    tools: List[str] = Field(default_factory=list)


class UserDirConfig(BaseModel):
    """用户目录配置"""
    path: str = Field(..., description="用户数据目录路径")
    map_file: Optional[str] = Field(None, description="映射文件名（相对于 path），如 'MAP_Linux'，自动补 .json 后缀")


class InputDirConfig(BaseModel):
    """输入目录配置"""
    skill_dir: Dict[str, str] = Field(default_factory=dict, description="技能目录映射")
    user_dir: Optional[UserDirConfig] = Field(None, description="用户目录，支持字符串路径或 {path, map_file} 对象")
    agent_dir: Optional[str] = Field(None, description="Agent 源文件目录，包含各 agent 的子目录（如 agent_dir/paper_reader/SOUL.md）")

    @validator('user_dir', pre=True)
    def coerce_user_dir(cls, v):
        """兼容旧格式：字符串自动转为 UserDirConfig"""
        if isinstance(v, str):
            return UserDirConfig(path=v)
        return v


class AgentConfigItem(BaseModel):
    """单个 Agent 配置"""
    name: str = Field(..., description="Agent 名称")
    config: List[str] = Field(default_factory=list, description="配置文件列表，如 USER.md, SOUL.md")
    skills: List[str] = Field(default_factory=list, description="所需技能列表")
    system_prompt: Optional[str] = Field(None, description="系统提示词")
    model: Optional[str] = Field(None, description="使用的模型")


class QueryItem(BaseModel):
    """查询任务配置"""
    agent_name: str = Field(..., description="执行的 Agent 名称")
    text: str = Field(..., description="查询文本，支持 {result_xxx} 变量替换")
    session_name: Optional[str] = Field("main", description="会话名称")
    timeout: Optional[int] = Field(300, description="超时时间（秒）")


class AutomationConfig(BaseModel):
    """完整的自动化配置"""
    system: SystemConfig = Field(default_factory=SystemConfig)
    input_dir: InputDirConfig = Field(default_factory=InputDirConfig)
    agents: List[AgentConfigItem] = Field(default_factory=list)
    queries: List[QueryItem] = Field(default_factory=list)

    # OpenClaw 连接配置
    gateway_ws_url: Optional[str] = Field(None, description="WebSocket 网关 URL")
    api_key: Optional[str] = Field(None, description="API Key")
    workspace_base: str = Field(r"C:\Users\nianzu\.openclaw\workspace", description="工作空间基础目录")


# ============================================================================
# 工作空间管理器
# ============================================================================

class WorkspaceManager:
    """管理 Agent 工作空间和文件"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        """获取 Agent 工作空间路径

        规则：
        - 如果 agent_name 是 "main"，返回 base_dir
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
        skill_dirs: Dict[str, str],
        agent_dir: Optional[str] = None,
        user_dir: Optional[str] = None
    ) -> None:
        """设置 Agent 工作空间文件

        Args:
            agent_name: Agent 名称
            config_files: 配置文件列表（如 SOUL.md, USER.md）
            skill_dirs: 技能目录映射 {skill_name: source_path}
            agent_dir: Agent 源文件根目录（包含各 agent 子目录）
            user_dir: 用户数据目录（整体复制到 workspace）
        """
        workspace = self.get_agent_workspace(agent_name)

        # 1. 从 agent_dir/<agent_name>/ 复制配置文件（SOUL.md, USER.md 等）
        if agent_dir and config_files:
            agent_source = Path(agent_dir) / agent_name
            if agent_source.exists():
                for config_file in config_files:
                    src = agent_source / config_file
                    if src.exists():
                        dst = workspace / config_file
                        shutil.copy2(src, dst)
                        print(f"  ✓ 复制 Agent 配置: {config_file}")
                    else:
                        print(f"  ⚠ Agent 配置文件不存在: {src}")
            else:
                print(f"  ⚠ Agent 源目录不存在: {agent_source}")

        # 2. 复制技能目录
        for skill_name, skill_path in skill_dirs.items():
            src = Path(skill_path)
            if src.exists() and src.is_dir():
                dst = workspace / skill_name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                print(f"  ✓ 复制技能目录: {skill_name} -> {dst}")
            else:
                print(f"  ⚠ 技能目录不存在: {src}")

        # 3. 整体复制 user_dir 到 workspace
        if user_dir:
            user_path = Path(user_dir)
            if user_path.exists() and user_path.is_dir():
                # 获取 user_dir 的目录名
                user_dir_name = user_path.name
                dst = workspace / user_dir_name

                # 如果目标已存在，先删除
                if dst.exists():
                    shutil.rmtree(dst)

                # 复制整个目录
                shutil.copytree(user_path, dst)
                print(f"  ✓ 复制用户目录: {user_dir_name}/ -> {dst}")
            else:
                print(f"  ⚠ 用户目录不存在或不是目录: {user_path}")

    def setup_from_map(self, map_file: str, base_dir: Optional[str] = None) -> None:
        """根据 map.json 按映射逐条复制文件/目录

        Args:
            map_file: map.json 路径，格式 {"src_path": "dst_path"}
            base_dir: 若提供，map 的 key（源路径）相对于此目录解析；
                      否则 key 视为绝对路径（支持 ~ 展开）
                      dst 路径始终支持 ~ 展开，不存在时自动创建父目录
        """
        map_path = Path(map_file)
        if not map_path.exists():
            print(f"  ⚠ map 文件不存在: {map_path}")
            return

        mapping: Dict[str, str] = json.loads(map_path.read_text(encoding="utf-8"))
        base = Path(base_dir) if base_dir else None
        print(f"  📄 读取 map 文件: {map_path}，共 {len(mapping)} 条映射")
        if base:
            print(f"  📂 源路径基准目录: {base}")

        for src_str, dst_str in mapping.items():
            src = (base / src_str) if base else Path(src_str).expanduser()
            dst = Path(dst_str).expanduser()

            if not src.exists():
                print(f"  ⚠ 源路径不存在，跳过: {src}")
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)

            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

            print(f"  ✓ 映射复制: {src_str} -> {dst_str}")


# ============================================================================
# Agent 管理器
# ============================================================================

class AgentManager:
    """管理 Agent 的创建、配置和技能安装"""

    def __init__(self, client: OpenClawClient, workspace_manager: WorkspaceManager):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agents: Dict[str, Any] = {}

    async def setup_agent(self, agent_config: AgentConfigItem) -> None:
        """设置单个 Agent

        Args:
            agent_config: Agent 配置
        """
        agent_name = agent_config.name
        print(f"\n📦 设置 Agent: {agent_name}")

        # 获取或创建 Agent
        try:
            agent = self.client.get_agent(agent_name)
            print(f"  ✓ 已存在 Agent: {agent_name}")
        except Exception:
            # 创建新 Agent
            workspace = self.workspace_manager.get_agent_workspace(agent_name)

            # 读取系统提示词（如果有 SOUL.md）
            system_prompt = agent_config.system_prompt
            if not system_prompt and "SOUL.md" in agent_config.config:
                soul_file = workspace / "SOUL.md"
                if soul_file.exists():
                    system_prompt = soul_file.read_text(encoding="utf-8")

            agent = await self.client.create_agent(
                AgentConfig(
                    agent_id=agent_name,
                    system_prompt=system_prompt or f"You are {agent_name} assistant.",
                    workspace=str(workspace),
                )
            )
            print(f"  ✓ 创建新 Agent: {agent_name}")

        self.agents[agent_name] = agent

        # 安装技能
        if agent_config.skills:
            await self._install_skills(agent_name, agent_config.skills)

    async def _install_skills(self, agent_name: str, skills: List[str]) -> None:
        """安装技能到 Agent"""
        print(f"  📚 安装技能...")
        for skill_name in skills:
            try:
                # 注意：实际的技能安装 API 可能需要根据 OpenClaw 的实际实现调整
                # 这里假设有 skills manager
                if hasattr(self.client, 'skills'):
                    await self.client.skills.install(skill_name)
                    print(f"    ✓ 技能安装成功: {skill_name}")
                else:
                    print(f"    ⚠ 技能安装 API 不可用，请手动安装: {skill_name}")
            except Exception as e:
                print(f"    ✗ 技能安装失败 {skill_name}: {e}")

    def get_agent(self, agent_name: str):
        """获取 Agent 实例"""
        return self.agents.get(agent_name)


# ============================================================================
# 查询编排器
# ============================================================================

class QueryOrchestrator:
    """编排和执行查询任务"""

    def __init__(self, agent_manager: AgentManager):
        self.agent_manager = agent_manager
        self.results: Dict[str, ExecutionResult] = {}

    async def execute_queries(self, queries: List[QueryItem]) -> Dict[str, ExecutionResult]:
        """按顺序执行查询任务

        Args:
            queries: 查询任务列表

        Returns:
            执行结果字典 {agent_name: ExecutionResult}
        """
        print("\n" + "="*60)
        print("🚀 开始执行查询任务")
        print("="*60)

        for idx, query in enumerate(queries, 1):
            print(f"\n📝 任务 {idx}/{len(queries)}: {query.agent_name}")
            print(f"   查询: {query.text[:100]}...")

            # 替换变量
            query_text = self._replace_variables(query.text)

            # 获取 Agent
            agent = self.agent_manager.get_agent(query.agent_name)
            if not agent:
                print(f"   ✗ Agent 不存在: {query.agent_name}")
                continue

            # 执行查询
            try:
                # 创建执行选项（注意：字段名是 timeout_seconds）
                options = ExecutionOptions(timeout_seconds=query.timeout) if query.timeout else None

                result = await agent.execute(
                    query_text,
                    options=options
                )

                # 保存结果
                result_key = f"result_{query.agent_name}"
                self.results[result_key] = result

                # 输出结果摘要
                print(f"   ✓ 执行成功")
                print(f"   耗时: {result.latency_ms}ms")
                print(f"   内容: {result.content[:200]}...")

            except Exception as e:
                print(f"   ✗ 执行失败: {e}")
                # 保存错误结果（用于后续查询的变量替换）
                self.results[f"result_{query.agent_name}"] = None

        return self.results

    def _replace_variables(self, text: str) -> str:
        """替换查询文本中的变量

        支持格式：{result_agent_name}
        """
        pattern = r'\{result_(\w+)\}'

        def replacer(match):
            result_key = match.group(0)[1:-1]  # 去掉 {}
            result = self.results.get(result_key)

            if result is None:
                return f"[Error: {result_key} not found]"
            elif hasattr(result, 'content'):
                return result.content
            else:
                return str(result)

        return re.sub(pattern, replacer, text)

    def generate_report(self, output_file: Optional[str] = None) -> str:
        """生成执行报告"""
        report_lines = [
            "\n" + "="*60,
            "📊 执行报告",
            "="*60,
            ""
        ]

        for idx, (key, result) in enumerate(self.results.items(), 1):
            if result is None:
                report_lines.append(f"{idx}. {key}: 执行失败")
            else:
                report_lines.append(f"{idx}. {key}:")
                report_lines.append(f"   状态: {'成功' if result.success else '失败'}")
                report_lines.append(f"   耗时: {result.latency_ms}ms")
                report_lines.append(f"   内容预览: {result.content[:150]}...")
                report_lines.append("")

        report = "\n".join(report_lines)

        # 输出到文件
        if output_file:
            Path(output_file).write_text(report, encoding="utf-8")
            print(f"\n💾 报告已保存到: {output_file}")

        return report


# ============================================================================
# 主执行器
# ============================================================================

class OpenClawAutomation:
    """OpenClaw 自动化任务执行主类"""

    def __init__(self, config: AutomationConfig):
        self.config = config
        self.workspace_manager = WorkspaceManager(config.workspace_base)
        self.client: Optional[OpenClawClient] = None
        self.agent_manager: Optional[AgentManager] = None
        self.query_orchestrator: Optional[QueryOrchestrator] = None

    async def run(self) -> Dict[str, ExecutionResult]:
        """运行自动化流程"""
        print("="*60)
        print("🤖 OpenClaw 自动化任务系统")
        print("="*60)

        # 构建连接参数
        connect_kwargs = {}
        if self.config.gateway_ws_url:
            connect_kwargs['gateway_ws_url'] = self.config.gateway_ws_url
        if self.config.api_key:
            connect_kwargs['api_key'] = self.config.api_key

        # 注意：OpenClawClient.connect() 返回协程，需要先 await
        # 然后返回的 OpenClawClient 实例才支持 async with
        client = await OpenClawClient.connect(**connect_kwargs)

        async with client:
            self.client = client

            # 1. 设置工作空间
            await self._setup_workspaces()

            # 2. 设置 Agents
            await self._setup_agents()

            # 3. 执行查询
            results = await self._execute_queries()

            # 4. 生成报告
            self.query_orchestrator.generate_report("execution_report.txt")

            return results

    async def _setup_workspaces(self) -> None:
        """设置工作空间"""
        print("\n📁 设置工作空间...")

        # 解析 user_dir：有 map_file 则按映射复制，否则整体复制（旧行为）
        user_dir_config = self.config.input_dir.user_dir
        user_dir_path: Optional[str] = None

        if user_dir_config:
            if user_dir_config.map_file:
                map_path = self._resolve_map_file(user_dir_config.path, user_dir_config.map_file)
                # 数据子目录 = user_dir.path / user_dir_name（同名子文件夹）
                data_dir = str(Path(user_dir_config.path) / Path(user_dir_config.path).name)
                self.workspace_manager.setup_from_map(map_path, base_dir=data_dir)
            else:
                user_dir_path = user_dir_config.path

        for agent_config in self.config.agents:
            self.workspace_manager.setup_agent_files(
                agent_name=agent_config.name,
                config_files=agent_config.config,
                skill_dirs=self.config.input_dir.skill_dir,
                agent_dir=self.config.input_dir.agent_dir,
                user_dir=user_dir_path
            )

    @staticmethod
    def _resolve_map_file(base_path: str, map_file: str) -> str:
        """解析 map_file 路径：相对于 base_path，自动补 .json 后缀"""
        p = Path(base_path) / map_file
        if not p.suffix:
            p = p.with_suffix('.json')
        return str(p)

    async def _setup_agents(self) -> None:
        """设置 Agents"""
        print("\n🤖 设置 Agents...")

        self.agent_manager = AgentManager(self.client, self.workspace_manager)

        for agent_config in self.config.agents:
            await self.agent_manager.setup_agent(agent_config)

    async def _execute_queries(self) -> Dict[str, ExecutionResult]:
        """执行查询"""
        self.query_orchestrator = QueryOrchestrator(self.agent_manager)
        return await self.query_orchestrator.execute_queries(self.config.queries)


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
        config_dict: 配置字典（直接传入）

    Examples:
        # 从文件加载
        await main(config_file="config.json")

        # 从字典加载
        await main(config_dict={...})
    """
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

    print("\n✅ 所有任务执行完成！")
    return results


# ============================================================================
# 命令行入口
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw 自动化任务执行系统")
    parser.add_argument(
        "config",
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
