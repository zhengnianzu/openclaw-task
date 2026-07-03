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

from src.evaluator.evaluator import EvaluateConfig, Rubric

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


class AgentModelConfig(BaseModel):
    """按 agent_name 外挂的模型配置。字段名与 user_proxy_model.json 一致。
    命中的 agent 配置(hermes profile / claude settings / openclaw 网关 provider)。
    """
    model_config = {"extra": "ignore"}
    model: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    @property
    def resolved_model(self) -> Optional[str]:
        """返回 provider/model 格式的完整模型串。provider 缺省时原样返回 model。"""
        if not self.model:
            return None
        if self.provider and "/" not in self.model:
            return f"{self.provider}/{self.model}"
        return self.model


def load_agent_model_configs(path: Optional[str]) -> Dict[str, "AgentModelConfig"]:
    """加载 simulator_config JSON,返回 {agent_name: AgentModelConfig}。

    路径为空/不存在 → 返回 {};JSON/格式错误让它自然抛(格式钉死,不应静默降级)。
    """
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        logger.warning("simulator_config 文件不存在: %s", p)
        return {}

    data = json.loads(p.read_text(encoding="utf-8"))
    out = {name: AgentModelConfig.model_validate(spec) for name, spec in data.items()}
    logger.info("已加载 simulator_config: %s (agents=%s)", p, sorted(out))
    return out


def warn_agent_model_conflict(
    agent_name: str,
    configured_model: Optional[str],
    cfg: "AgentModelConfig",
) -> None:
    """agents[].model 与 simulator_config 里同名 agent 的 model 冲突时打 warning。"""
    resolved = cfg.resolved_model
    if configured_model and resolved and configured_model != resolved:
        logger.warning(
            "agent '%s' model 冲突: 配置 '%s' vs simulator_config '%s';采用 simulator_config",
            agent_name, configured_model, resolved,
        )


class QueryItem(BaseModel):
    """查询任务配置"""
    agent_name: str = Field(..., description="执行的 Agent 名称")
    text: str = Field(..., description="查询文本,支持 {result_xxx} 变量替换")
    session_name: Optional[str] = Field("main", description="会话名称")
    timeout: Optional[int] = Field(3600, description="超时时间(秒)")
    use_simulator: bool = Field(True, description="是否启用 user-simulator 进行多轮对话,默认 True")
    evaluate: Optional[EvaluateConfig] = Field(None, description="第三方 evaluator 配置(query 内联块);为空则本 query 不评估。rubric/eval_step 等迁入此块")


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
    simulator_config: Optional[str] = Field(None, description="模型配置 JSON 绝对路径,顶层 model/api_key/base_url 用于 user_simulator;可选嵌套 {agent_name: {model,api_key,base_url}} 用于覆盖 harness agent")
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
def _resolve_json_pointer(data: Any, pointer: str) -> Any:
    """按 RFC6901 JSON-Pointer 解引用(形如 /0/evaluate/0/custom_rubrics)。数字段转列表下标。"""
    cur = data
    for seg in pointer.split("/"):
        if seg == "":
            continue
        seg = seg.replace("~1", "/").replace("~0", "~")  # RFC6901 转义
        if isinstance(cur, list):
            cur = cur[int(seg)]
        else:
            cur = cur[seg]
    return cur


def _resolve_evaluate_refs(config: "AutomationConfig", user_dir: Optional[Path]) -> None:
    """解引用各 query 的 evaluate 块外部引用,以 user_dir目录为相对基准。

    - oracle_ref:加载 ground-truth → ev.oracle_data。
    - rubrics_ref:JSON-Pointer 解引用 → ev.structured_rubrics。
    - scoring_ref:JSON-Pointer 解引用 → ev.scoring(唯一评分来源,无隐式兜底)。
    路径/指针缺失显式报错(不静默退空)。最后 resolve_runtime() 合成 scoring_spec。
    user_dir 为 None 时,若 evaluate 块不含 *_ref 则安全跳过;含 *_ref 则显式报错。
    """
    for q in config.queries:
        ev = q.evaluate
        if ev is None:
            continue

        has_refs = ev.oracle_ref or ev.rubrics_ref or ev.scoring_ref
        if has_refs and user_dir is None:
            raise ValueError(
                f"query '{q.text[:40]}...' 的 evaluate 块包含外部引用"
                f"(oracle_ref/rubrics_ref/scoring_ref),但 input_dir.user_dir 未配置"
            )

        if ev.oracle_ref:
            op = (user_dir / ev.oracle_ref)
            if not op.exists():
                raise FileNotFoundError(f"evaluate.oracle_ref 不存在: {op}")
            oracle_text = op.read_text(encoding="utf-8")
            ev.oracle_data = json.loads(oracle_text)
            # 留存原始字节+绝对路径,供执行期隔离/还原(整文件粒度,逐字节回写)
            ev.file_vault[str(op.resolve())] = oracle_text

        if ev.rubrics_ref:
            file_part, _, ptr = ev.rubrics_ref.partition("#")
            rp = (user_dir / file_part)
            if not rp.exists():
                raise FileNotFoundError(f"evaluate.rubrics_ref 不存在: {rp}")
            rubrics_text = rp.read_text(encoding="utf-8")
            raw = json.loads(rubrics_text)
            arr = _resolve_json_pointer(raw, ptr) if ptr else raw
            ev.structured_rubrics = [Rubric.from_raw(r, i) for i, r in enumerate(arr, 1)]
            # 片段引用也按整文件留存(删除/还原以整文件为单位)
            ev.file_vault[str(rp.resolve())] = rubrics_text

        if ev.scoring_ref:
            # scoring 唯一来源:显式解析 scoring_ref 指针(与 rubrics_ref 对称),无隐式兜底。
            file_part, _, ptr = ev.scoring_ref.partition("#")
            sp = (user_dir / file_part)
            if not sp.exists():
                raise FileNotFoundError(f"evaluate.scoring_ref 不存在: {sp}")
            scoring_text = sp.read_text(encoding="utf-8")
            raw = json.loads(scoring_text)
            resolved = _resolve_json_pointer(raw, ptr) if ptr else raw
            if not isinstance(resolved, dict):
                raise ValueError(f"evaluate.scoring_ref 未解析到 scoring 块(dict): {ev.scoring_ref}")
            ev.scoring = resolved
            # 整文件留存(删除/还原以整文件为单位;与 oracle/rubrics 同构)
            ev.file_vault[str(sp.resolve())] = scoring_text

        ev.resolve_runtime()  # 合成 scoring_spec


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
        
        config = AutomationConfig(**data)
        user_dir_path = Path(config.input_dir.user_dir.path) if config.input_dir.user_dir else None
        _resolve_evaluate_refs(config, user_dir_path)
        return config

    @staticmethod
    def load_from_dict(data: Dict[str, Any]) -> AutomationConfig:
        config = AutomationConfig(**data)
        user_dir_path = Path(config.input_dir.user_dir.path) if config.input_dir.user_dir else None
        _resolve_evaluate_refs(config, user_dir_path)
        return config

