"""
自动化任务配置模型 + 配置加载器

所有 Pydantic 配置模型和 ConfigLoader 集中在此,
harness_automation.py 和 src/ 模块均从这里导入。
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("harness_automation")


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
        if isinstance(v, dict):
            return None
        return v

    @field_validator('user_dir', mode='before')
    @classmethod
    def coerce_user_dir(cls, v):
        if isinstance(v, str):
            return UserDirConfig(path=v)
        if isinstance(v, dict) and v.get('path') is None:
            dummy_path = os.path.join(tempfile.gettempdir(), "harness_void_dir")
            if not os.path.exists(dummy_path):
                os.makedirs(dummy_path, exist_ok=True)
            v['path'] = dummy_path
            logger.warning(
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


class AutomationConfig(BaseModel):
    """完整的自动化配置"""
    harness_type: str = Field("openclaw", description="harness 类型: openclaw 或 hermes")

    system: SystemConfig = Field(default_factory=SystemConfig)
    input_dir: InputDirConfig = Field(default_factory=InputDirConfig)
    agents: List[AgentConfigItem] = Field(default_factory=list)
    queries: List[QueryItem] = Field(default_factory=list)

    # 网关连接配置 — openclaw 使用,hermes 忽略
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
        if not isinstance(v, str):
            return v
        url = v.strip()
        if not url:
            return None
        if url.startswith(("ws://", "wss://")) and "/gateway" not in url:
            return url.rstrip("/") + "/gateway"
        return url


# ============================================================================
# 配置加载器
# ============================================================================

class ConfigLoader:
    """配置文件加载器"""

    @staticmethod
    def load_from_file(file_path: str) -> AutomationConfig:
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {file_path}")

        content = path.read_text(encoding="utf-8")

        if path.suffix.lower() in ['.json']:
            data = json.loads(content)
        elif path.suffix.lower() in ['.yaml', '.yml']:
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                raise ImportError("YAML 支持需要安装 PyYAML: pip install pyyaml")
        else:
            data = json.loads(content)

        return AutomationConfig(**data)

    @staticmethod
    def load_from_dict(data: Dict[str, Any]) -> AutomationConfig:
        return AutomationConfig(**data)
