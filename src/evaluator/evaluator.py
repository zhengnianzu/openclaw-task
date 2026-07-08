"""独立第三方 Evaluator(能力: trajectory-evaluation)。

每个 turn 的 agent 回复之后,由一个**独立于执行任务 agent** 的 OC agent 基于
可核验证据(tool_calls + 磁盘真相文件)做评估,产出"完成度/改进点/不符合项/倾向 +
引证",反馈给 user_simulator。

设计要点(见 design.md):
- D1 evaluator 是独立 OC agent,非执行 agent。
- D2 逐轮在环;D3 simulator 仍拍板,evaluator 仅顾问(软反馈,无硬否决)。
- D4 无状态:每轮新开 session,显式投喂(任务 + 历轮全文 + 上轮反馈 + 本轮证据)。
- D5 证据以磁盘真相为准;D6 文本/轨迹拼提示词(a)+ 文件推进 evaluator 工作区(b)。
- D9 结构化输出 + 引证 + 落盘。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Type, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.evaluator.trajectory import Trajectory, TurnRecord

logger = logging.getLogger("harness_automation")

_T = TypeVar("_T", bound=BaseModel)


def _parse_json_as(text: str, model: Type[_T]) -> _T:
    """从模型回复里抽 JSON 并校验:优先 ```json 围栏,其次裸 `{...}`。"""
    m = re.search(r"```json\s*([\s\S]*?)```", text) or re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON found in response: {text[:200]}")
    return model.model_validate(json.loads(m.group(1) if m.lastindex else m.group(0)))


# 评估日志,仿 api_use.log,每行一条 JSON(供离线复核与校准)
eval_logger = logging.getLogger("evaluator_use")
eval_logger.setLevel(logging.INFO)
log_dir = Path(__file__).parent.parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
_eval_handler = logging.FileHandler(log_dir / "evaluator_use.log", encoding="utf-8")
_eval_handler.setFormatter(logging.Formatter("%(message)s"))
eval_logger.addHandler(_eval_handler)
eval_logger.propagate = False


# ============================================================================
# 配置 / 结构化输出
# ============================================================================

class Rubric(BaseModel):
    """单条结构化 rubric 准则。

    向后兼容:纯字符串 rubric 经 `from_raw` 归一为 `when='final'/evaluator='llm_judge'` 的本模型。
    - when: 仅用于识别 gate(参与一票否决);per_turn/final 一视同仁,均为参与加权的 reward 项。
    - evaluator: 判定方式 program/oracle_cmp/llm_judge,投喂给 evaluator-agent 据以判 0/1。
    - formula: 半形式化判据(伪代码 DSL,非可执行代码),供 evaluator 据语义精确比对。
    - gt_ref: oracle 中对应 ground-truth 字段的引用路径(供 oracle_cmp 比对)。
    """
    id: str = Field(..., description="rubric 唯一标识(如 G1/PT2/C3)")
    when: Literal["gate", "per_turn", "final"] = Field("final", description="gate=门禁/per_turn/final;仅 gate 参与一票否决")
    evaluator: str = Field("llm_judge", description="判定方式:program/oracle_cmp/llm_judge")
    text: str = Field("", description="rubric 自然语言描述(判什么)")
    formula: Optional[str] = Field(None, description="半形式化判据(怎么判);非可执行代码")
    gt_ref: Optional[str] = Field(None, description="oracle 中对应 ground-truth 字段的引用路径")

    @classmethod
    def from_raw(cls, raw: Any, idx: int) -> "Rubric":
        """把一条原始 rubric(dict 或 str)归一为 Rubric。str → 旧式自由评估准则。"""
        if isinstance(raw, dict):
            data = dict(raw)
            data.setdefault("id", f"R{idx}")
            return cls(**data)
        # 纯字符串:向后兼容旧格式
        return cls(id=f"R{idx}", when="final", evaluator="llm_judge", text=str(raw))


class BucketSpec(BaseModel):
    """一个评分桶:权重 + 归属的 rubric id 列表。"""
    weight: float = 0.0
    rubric_ids: List[str] = Field(default_factory=list)


class ScoringSpec(BaseModel):
    """评分规格:聚合器(Scorer)的唯一输入,与 JSON 布局解耦。

    解析层负责把 `scoring`(weights/bucket_map)+ rubric 的 when 归一为本模型;
    聚合算法 MUST NOT 直接依赖字段在 JSON 中的位置——满足"权重位置后续会变"的抽象要求。
    """
    gate_ids: List[str] = Field(default_factory=list, description="when==gate 的 rubric id,参与一票否决")
    buckets: Dict[str, BucketSpec] = Field(default_factory=dict, description="bucket 名 → {weight, rubric_ids}")
    gate_zero: bool = Field(True, description="True=任一 gate 判 0 → completion=0")

    @classmethod
    def from_scoring(cls, scoring: Optional[dict], rubrics: List["Rubric"]) -> "ScoringSpec":
        """据 scoring 块与 rubric 列表合成 ScoringSpec。

        无 scoring(旧 config):退回"无 gate、所有非 gate rubric 归单桶等权",completion 仍可算。
        """
        gate_ids = [r.id for r in rubrics if r.when == "gate"]
        if not scoring:
            non_gate = [r.id for r in rubrics if r.when != "gate"]
            buckets = {"default": BucketSpec(weight=1.0, rubric_ids=non_gate)} if non_gate else {}
            return cls(gate_ids=gate_ids, buckets=buckets, gate_zero=bool(gate_ids))
        weights = scoring.get("weights", {}) or {}
        bucket_map = scoring.get("bucket_map", {}) or {}
        buckets = {
            name: BucketSpec(weight=float(weights.get(name, 0.0)), rubric_ids=list(ids))
            for name, ids in bucket_map.items()
        }
        return cls(gate_ids=gate_ids, buckets=buckets, gate_zero=bool(scoring.get("gate_zero", True)))


class Scorer:
    """确定性评分聚合器:completion = (∏gate) × Σ_bucket[ w_bucket·(桶内通过/桶内总) ]。

    权重在非空桶间归一化,保证全过 completion=1.0(空桶 total=0 不参与归一,见 design D2 与样本注脚)。
    completion 取值域 0~1(非百分制)。
    """

    def __init__(self, spec: ScoringSpec):
        self.spec = spec

    def score(self, checks: Dict[str, int]) -> dict:
        """据各条 rubric 的 0/1 判定算出 completion(0~1)与分桶得分。

        checks: {rubric_id: 0|1}。缺失的 id 视为 0(未通过/核验受阻判 0)。
        """
        # gate:一票否决
        gate_status = {gid: int(checks.get(gid, 0)) for gid in self.spec.gate_ids}
        gate_passed = all(v == 1 for v in gate_status.values())

        # 非 gate:按桶取通过比例,权重在非空桶间归一化后加权求和
        active = {n: b for n, b in self.spec.buckets.items() if b.rubric_ids}
        wsum = sum(b.weight for b in active.values()) or 1.0
        bucket_scores: Dict[str, dict] = {}
        weighted_sum = 0.0
        for name, b in self.spec.buckets.items():
            total = len(b.rubric_ids)
            passed = sum(1 for i in b.rubric_ids if int(checks.get(i, 0)) == 1)
            ratio = (passed / total) if total else 0.0
            norm_w = (b.weight / wsum) if total else 0.0
            contrib = norm_w * ratio
            bucket_scores[name] = {
                "passed": passed, "total": total, "ratio": ratio,
                "weight": b.weight, "norm_weight": norm_w, "score": contrib,
            }
            weighted_sum += contrib

        if self.spec.gate_zero and not gate_passed:
            completion = 0.0
        else:
            completion = weighted_sum
        completion = max(0.0, min(1.0, completion))
        return {
            "completion": round(completion, 4),
            "bucket_scores": bucket_scores,
            "gate_status": gate_status,
            "gate_passed": gate_passed,
        }


class EvaluateConfig(BaseModel):
    """Per-query 评估配置(query 内联 `evaluate` 块)。

    出现该块即表示本 query 启用第三方 evaluator(不再有独立 enabled 开关)。
    `agent_name` 须取自顶层 `agents` 列表中的某个已声明 agent,且 ≠ 本 query 的执行 agent;
    其裁判模型在 agent 声明处经 `agents.update` 钉死(见 openclaw_automation._setup_*)。

    字段别名(新名↔历史名,经 AliasChoices 等价):
    - agent_name ↔ evaluator_agent
    - eval_step ↔ evaluate_every_n_turns
    - to_simulator ↔ feedback_to_user
    """
    model_config = ConfigDict(populate_by_name=True)

    agent_name: str = Field(
        "evaluator",
        validation_alias=AliasChoices("agent_name", "evaluator_agent"),
        description="充当 evaluator 的 OC agent 名(须取自 agents 列表,且 ≠ 执行 agent)",
    )
    session_name: Optional[str] = Field(None, description="evaluator 会话名;None=复用 query.session_name 跟随被测会话")
    rubrics: List[str] = Field(default_factory=list, description="旧式字符串验收清单;新式结构化 rubric 走 rubrics_ref")
    eval_step: int = Field(
        1, ge=1,
        validation_alias=AliasChoices("eval_step", "evaluate_every_n_turns"),
        description="评审频率 X:每 X 个 turn 评一次;最近 X 轮投喂窗口的 X 同此值",
    )
    to_simulator: bool = Field(
        False,
        validation_alias=AliasChoices("to_simulator", "feedback_to_user"),
        description="True=评估反馈回流 simulator;False=只评估并落盘、不回流(安全默认,先行观测质量)",
    )
    log_evaluations: bool = Field(True, description="是否把每次评估落盘到 evaluator_use.log")
    review_subdir: str = Field("_under_review", description="推进 evaluator 工作区的被审查文件子目录")
    isolate_eval_files: bool = Field(
        True,
        description="开关:任务执行期间把本 query 的 oracle/rubrics 从磁盘隔离(执行前删除→结束后还原)。"
                    "True=隔离(防被测 agent 读到答案);False=不隔离(调试用,文件保留在盘)",
    )

    # 新增:外部引用(相对 config 文件目录,由 ConfigLoader 解引用 pass 加载并填充运行时字段)
    oracle_ref: Optional[str] = Field(None, description="ground-truth 文件相对路径,供 oracle_cmp 比对")
    rubrics_ref: Optional[str] = Field(None, description="结构化 rubric 的 JSON-Pointer(形如 file.json#/a/b/c)")
    scoring_ref: Optional[str] = Field(None, description="评分块的 JSON-Pointer(形如 file.json#/a/b/c/scoring);解引用后填入运行时 scoring")

    # 运行时字段(不来自 JSON,由解引用 pass 注入;exclude 不参与序列化)
    structured_rubrics: List[Rubric] = Field(default_factory=list, exclude=True)
    oracle_data: Optional[dict] = Field(None, exclude=True)
    # scoring:由 scoring_ref 解引用后填充的评分块(gate_zero/weights/bucket_map),解析为 scoring_spec。
    scoring: Optional[dict] = Field(None, exclude=True)
    scoring_spec: Optional[ScoringSpec] = Field(None, exclude=True)
    # 文件隔离 vault:{绝对路径: 原始文本};解引用时留存,供执行期删除/还原(整文件粒度)。
    file_vault: Dict[str, str] = Field(default_factory=dict, exclude=True)

    def rubric_items(self) -> List[Rubric]:
        """统一返回结构化 rubric:优先 structured_rubrics,否则把旧式字符串 rubrics 归一。"""
        if self.structured_rubrics:
            return self.structured_rubrics
        return [Rubric.from_raw(s, i) for i, s in enumerate(self.rubrics, 1)]

    def resolve_runtime(self) -> None:
        """据已加载的 rubrics_ref/rubrics 与 scoring 装配运行时字段(scoring_spec)。

        oracle_data/structured_rubrics 由 ConfigLoader 在知晓 config 目录时填充;
        本方法只做不依赖磁盘的最终合成(可重复调用)。
        """
        rubrics = self.rubric_items()
        self.scoring_spec = ScoringSpec.from_scoring(self.scoring, rubrics)


class RubricCheck(BaseModel):
    """对单条 rubric 准则的逐条质检结果(0/1 二值)。"""
    rubric_id: str = Field("", description="对应 rubric 的 id(如 C1);用于与 ScoringSpec 关联")
    criterion: str = Field(..., description="被核验的 rubric 准则原文")
    passed: int = Field(..., ge=0, le=1, description="1=通过 / 0=不通过(核验受阻一律判 0)")
    evidence: str = Field("", description="引证:支撑本条裁定的轨迹语句/工具返回/文件内容")


class EvaluationResult(BaseModel):
    """evaluator 的结构化裁决。completion 由 Scorer 算出(非模型自报)。"""
    completion: Optional[float] = Field(
        None,
        description="任务完成度 0~1(非百分制);None=未评估(执行中/无 rubric);已交付时由 Scorer 覆盖",
    )
    inclination: str = Field(..., description="整体倾向:accept(可放行)/reject(应继续)/uncertain")
    improvements: list[str] = Field(default_factory=list, description="改进点")
    violations: list[str] = Field(default_factory=list, description="不符合要求项")
    citations: list[str] = Field(default_factory=list, description="引证:引用轨迹语句/工具返回/文件内容")
    rubric_checks: list[RubricCheck] = Field(
        default_factory=list, description="逐条 rubric 质检结果(0/1);无冻结 rubric 时为空"
    )
    reason: str = Field("", description="总体理由")
    task_declared_complete: bool = Field(
        True,
        description="前置检测:actor 本轮是否声明/呈现已交付姿态(判姿态不判对错)。"
                    "默认 True → 缺省即按现状回流,向后兼容;False=执行中,本轮反馈不回流 simulator",
    )

    # Scorer 注入(非模型输出;默认空,evaluate_turn 中据 rubric_checks 算出后覆盖)
    bucket_scores: dict = Field(default_factory=dict, description="分桶得分(Scorer 算出)")
    gate_status: dict = Field(default_factory=dict, description="各 gate 项 0/1 状态(Scorer 算出)")


# 内置默认评估提示词(evaluator 的角色/铁律/工作区纪律)。写死在此处;
# query 的 evaluator agent 若另配了 system_prompt,会在 Evaluator 处覆盖本默认值。
DEFAULT_EVAL_PROMPT = """你是一个独立、严格的任务评估专家(Evaluator),独立于对话中的"用户"和"执行 agent"。
你的职责:基于**可核验证据**(工具调用记录、磁盘上的真实文件)判断执行 agent 本轮的表现,而非轻信其文本说辞。

评估维度:
1. 任务达成度:相对 Origin_query,本轮(及此前累积)完成到什么程度。
2. 真实性/无幻觉:agent 声称做过的事,是否有**可确证的反证**。**仅当存在可确证反证(如声称生成文件但磁盘上确实不存在、或输出与 oracle 事实冲突)时,才在 violations 中点名。**
3. 约束遵守 / 过程合理性 / 受阻处置 / 不跑题。

铁律:
- 一切以**磁盘真相与工具记录**为准;但"证据缺失"≠"证据为负"。
- **`tool_calls` 为空 MUST NOT 据以推断 agent"未调用工具 / 硬编码 / 造假"**——它常因采集缺口(服务端自主 agent 的工具步骤未被采到)而为空,应与 `evidence_incomplete` 同等对待。判"声称与证据矛盾"必须以可确证反证(磁盘真相 / oracle 冲突)为据,绝不能仅凭"无 tool_calls 记录"。
- 标注为"证据不完整(evidence_incomplete)/核验受阻"的项,MUST NOT 当作负面证据判 agent 未达成(避免冤枉 harness 掉线)。
- 每条关键判断都要在 citations 里引用轨迹中的**具体语句、工具返回或文件内容**作为依据。

前置检测(评估的第一步,只判姿态、不判对错):
- 先判断执行 agent 在**最新一轮**处于哪种姿态,填入 `task_declared_complete`:
  - 执行中(false):明确表示尚未做完("接下来/我先/正在/下一步/稍后")、只给了阶段性进展、
    在向用户提问以继续、或只覆盖了任务的一部分。
  - 已交付(true):给出针对 Origin Query 的完整答复或最终产物且无"还要继续"的信号,或明确声明任务已完成。
- 判定纪律:本步**只看姿态/意图,绝不判对错**——答复内容看着对或不对都不影响它;是否真正达标由 rubric 逐条核验单独决定。
- "松进严出":仅当存在**明确的"执行中"信号**时才判 false;其余一切情况(含看起来完整的最终答复、以及模棱两可)一律判 true——宁可多评一次,绝不漏评真正的终轮。

工作区纪律(评估期间 MUST 严守):
- **禁止新增/创建任何文件**——核验时不得在你的工作区生成临时文件、脚本或任何中间产物。
- 若确因核验需要(如运行脚本)而产生了任何文件,**核验完成后一律删除**,确保评估结束时你的工作区不残留任何由你新生成的文件。
- 本纪律仅约束你(evaluator)自身的产物行为,不影响你对被测 agent 已有产物文件的读取与裁定。

输出 inclination:任务确已达成→accept;尚有缺口/有矛盾→reject;证据不足以判定→uncertain。
"""

# 每轮投喂的 user 消息模板:外置到 evaluator_user_prompt.md,按 `<!-- @section NAME -->`
# 切成命名片段(skeleton/generated_files/oracle/rubric/no_rubric),由 _build_prompt 选片段+replace 装配。
_USER_PROMPT_FILE = Path(__file__).parent / "evaluator_user_prompt.md"
_SECTION_MARKER = re.compile(r"\s*<!--\s*@section\s+(\w+)\s*-->\s*$")


def _load_prompt_sections(path: Path) -> Dict[str, str]:
    """把 user 提示词模板按 `<!-- @section NAME -->` 标记切成 {name: 片段文本}。

    标记行之前的内容(文件头注释)被忽略;每个片段去除首尾空行。
    """
    sections: Dict[str, str] = {}
    name: Optional[str] = None
    buf: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _SECTION_MARKER.match(line)
        if m:
            if name is not None:
                sections[name] = "\n".join(buf).strip("\n")
            name, buf = m.group(1), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        sections[name] = "\n".join(buf).strip("\n")
    return sections


_SECTIONS = _load_prompt_sections(_USER_PROMPT_FILE)


# ============================================================================
# Evaluator
# ============================================================================

class Evaluator:
    """驱动一个**持久** evaluator OC agent,逐评审点评估。

    状态模型:agent 实体在同一 query 内复用(不每轮重建,省建连/初始化开销);
    但**每次评估前 reset 其会话**——因为 OC 会话会持久化并回放 agent 自身上一轮的判词,
    不清空会造成判词自我锚定。历史由 harness 的 trajectory 承载,session 不承担记忆。
    """

    def __init__(
        self,
        config: EvaluateConfig,
        client: Any,
        run_id: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        get_agent_fn: Optional[Callable[[str, str], Any]] = None,
    ):
        self.config = config
        self.client = client
        # get_agent_fn:harness 注入的工厂
        self._get_agent_fn = get_agent_fn
        self.run_id = run_id
        # 同一 query 内固定的 evaluator 会话名(跨 turn 复用、每轮 reset)
        self.session_name = session_name
        # 评估提示词模板:优先用 evaluator agent 配置的 system_prompt(配了就用它),
        # 否则回退内置 DEFAULT_EVAL_PROMPT。注:本网关下 agent 的 system_prompt 不会下发到
        # OC 层,这里把它复用为评估指令模板(作为每轮 user 消息注入),使该配置真正生效。
        self._prompt_template = system_prompt or DEFAULT_EVAL_PROMPT
        # 确定性评分聚合器:由 ScoringSpec 驱动 (∏gate)×Σ桶加权;completion 由它算出而非模型自报
        spec = config.scoring_spec or ScoringSpec.from_scoring(config.scoring, config.rubric_items())
        self.scorer = Scorer(spec)

    @classmethod
    def create(
        cls,
        config: Optional["EvaluateConfig"],
        client: Any,
        run_id: str,
        session_name: str,
        system_prompt: Optional[str] = None,
        get_agent_fn: Optional[Callable[[str, str], Any]] = None,
    ) -> Optional["Evaluator"]:
        """据 query 的 evaluate 块创建 Evaluator;无该块(config=None)则返回 None。

        system_prompt: evaluator agent 在 `agents` 中配置的 system_prompt;非空时作为
        评估提示词模板替代 DEFAULT_EVAL_PROMPT。
        get_agent_fn: harness 注入的 agent 工厂,与被测 agent 使用同一路径,确保
        hermes profile / cwd 等 workspace 设定被应用。
        """
        if config is None:
            return None
        config.resolve_runtime()  # 兜底装配 scoring_spec(ConfigLoader 未调时)
        evaluator = cls(config, client, run_id, session_name, system_prompt, get_agent_fn)
        logger.info(
            "Evaluator 已启用(agent=%s,session=%s,eval_step=%d,to_simulator=%s)",
            config.agent_name, session_name, config.eval_step, config.to_simulator,
        )
        return evaluator

    @property
    def to_simulator(self) -> bool:
        return self.config.to_simulator

    async def evaluate_turn(
        self,
        trajectory: Trajectory,
        current_turn: TurnRecord,
        rubric: Optional[list[Rubric]] = None,
        window: int = 1,
    ) -> Optional[EvaluationResult]:
        """对当前进展做一次评估;失败返回 None(安全降级,不阻断任务)。

        持久 agent + 每轮 reset:先清空会话防判词锚定,再投喂压缩 trajectory
        (origin_query + 结构化 rubric + oracle + 最近 window 轮含 tool_calls + 产物指针)。
        rubric: 随 query 冻结的结构化验收清单;非空时逐条判 0/1 并由 Scorer 算 completion。
        """
        # 持久 agent:同一 query 复用同一会话名(不每轮新建)
        if self._get_agent_fn is not None:
            eval_agent = self._get_agent_fn(self.config.agent_name, self.session_name)
        else:
            eval_agent = self.client.get_agent(self.config.agent_name, self.session_name)

        # D1:评估前 reset 会话,确保自身上一轮判词不被回放(防锚定)
        await self._reset_session(eval_agent)

        # 投递(b):把磁盘真相文件推进 evaluator 自己的工作区,供其用工具就地核验
        await self._push_review_files(current_turn)

        # 投递(a):origin_query + rubrics + 最近 window 轮 + 产物指针(不投全量历史/不投自身旧判词)
        prompt = self._build_prompt(trajectory, rubric, window)
        prompt_chars = len(prompt)  # token 代理量,供 eval_step 实验对比开销

        try:
            # 直接复用各 client 的 agent.execute:追加 schema 后缀 → 解析 JSON
            schema_suffix = (
                f"\n\nRespond with valid JSON matching this schema:\n"
                f"```json\n{json.dumps(EvaluationResult.model_json_schema(), indent=2)}\n```"
            )
            resp = await eval_agent.execute(prompt + schema_suffix)
            result = _parse_json_as(resp.content, EvaluationResult)
        except Exception as e:  # noqa: BLE001
            logger.warning("evaluator 第 %d 轮评估失败: %s", current_turn.turn, e)
            self._log(trajectory, current_turn, None, error=str(e), window=window, prompt_chars=prompt_chars)
            return None

        # 确定性归一:无冻结 rubric、或本轮执行中(未交付)时,不评分——强制清空 rubric_checks
        # 且 completion=None(未评估,区别于"评估后判 0")。兜住模型自拟准则/未交付仍评分的幻觉。
        # 仅归一、不判负/不重试,且置于落盘之前以保证评估日志干净。
        if not rubric or not result.task_declared_complete:
            result.rubric_checks = []
            result.completion = None
        else:
            # 二值化聚合:从逐条 0/1 判定算出 completion(覆盖模型自报值)。
            # checks 按 rubric_id 关联;模型漏填 id 时按顺序回填,缺失项由 Scorer 视为 0。
            checks = self._collect_checks(result.rubric_checks, rubric)
            scored = self.scorer.score(checks)
            result.completion = scored["completion"]
            result.bucket_scores = scored["bucket_scores"]
            result.gate_status = scored["gate_status"]

        self._log(trajectory, current_turn, result, window=window, prompt_chars=prompt_chars)

        # Debug:打印 evaluator 本轮结构化输出,便于在线观测评估质量
        logger.debug(
            "[Evaluator] turn=%d agent=%s 输出:\n%s\n反馈渲染:\n%s",
            current_turn.turn,
            trajectory.agent_name,
            json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
            self.format_feedback(result),
        )

        return result

    @staticmethod
    def _collect_checks(checks_out: list[RubricCheck], rubric: list[Rubric]) -> Dict[str, int]:
        """把模型逐条裁定收敛为 {rubric_id: 0|1}。

        优先按模型回填的 `rubric_id` 关联;模型漏填 id 时按输出顺序回填到 rubric;
        缺失的准则交由 Scorer 视为 0(核验受阻判 0)。
        """
        by_id: Dict[str, int] = {}
        for rc in checks_out:
            if rc.rubric_id:
                by_id[rc.rubric_id] = int(rc.passed)
        if len(by_id) < len(rubric):
            for i, rc in enumerate(checks_out):
                if not rc.rubric_id and i < len(rubric):
                    by_id.setdefault(rubric[i].id, int(rc.passed))
        return by_id

    def format_feedback(self, ev: EvaluationResult) -> str:
        """把结构化裁决转成给 simulator 看的简洁反馈文本。

        边界 X:simulator 不感知 rubric。本函数**只**渲染 evaluator 提炼后的
        未满足项/改进点/引证,**故意不渲染** `ev.rubric_checks`——逐条 rubric
        结果(含准则原文)只进评估日志,不回流 simulator。
        """
        completion_str = "未评估" if ev.completion is None else str(ev.completion)
        lines = [f"完成度: {completion_str} ｜ 倾向: {ev.inclination}"]
        if ev.violations:
            lines.append("不符合要求项:\n- " + "\n- ".join(ev.violations))
        if ev.improvements:
            lines.append("改进点:\n- " + "\n- ".join(ev.improvements))
        if ev.citations:
            lines.append("证据引证:\n- " + "\n- ".join(ev.citations))
        if ev.reason:
            lines.append("理由: " + ev.reason)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #

    async def _reset_session(self, eval_agent: Any) -> None:
        """评估前清空 evaluator 会话(防判词锚定)。失败则降级继续(不阻断任务)。"""
        gateway = getattr(self.client, "gateway", None)
        if gateway is None:
            return
        try:
            await gateway.sessions_reset(eval_agent.session_key)
        except Exception as e:  # noqa: BLE001
            logger.debug("evaluator 会话 reset 失败(降级继续): %s", e)

    async def _push_review_files(self, turn: TurnRecord) -> None:
        gateway = getattr(self.client, "gateway", None)
        if gateway is None:
            return
        for fe in turn.files:
            if not (fe.checked and fe.exists and fe.content is not None):
                continue
            dest = f"{self.config.review_subdir}/{fe.name}"
            try:
                await gateway.agents_files_set(self.config.agent_name, dest, fe.content)
            except Exception as e:  # noqa: BLE001
                logger.debug("推进被审查文件失败 %s: %s", dest, e)

    def _build_prompt(
        self,
        trajectory: Trajectory,
        rubric: Optional[list[Rubric]] = None,
        window: int = 1,
    ) -> str:
        """构建压缩投喂:origin_query + 最近 window 轮(含 tool_calls)+ 产物指针 + rubrics。

        刻意**不投**全量历史、**不投**文件全文、**不投** evaluator 自身上一轮判词(防锚定)。
        进步感知由窗口内 window 轮的证据变化体现。

        文案全部外置到 evaluator_user_prompt.md(_SECTIONS);本方法只做「算占位符值 +
        选片段(无则置空串)+ 一条 replace 链」,不内联成段提示词。
        """
        # 产物文件片段:无产物→空串;有则前置空行与正文隔开。
        file_pointers = trajectory.generated_file_pointers()
        if file_pointers:
            generated_file_lines = "\n".join(
                f"- {p['filename']} (workspace_path={p['workspace_path']})" for p in file_pointers
            )
            generated_files_section = "\n\n" + (
                _SECTIONS["generated_files"]
                .replace("{review_subdir}", self.config.review_subdir)
                .replace("{generated_file_lines}", generated_file_lines)
            )
        else:
            generated_files_section = ""

        # rubric 片段:有→(可选 Oracle 子片段)+ 逐条清单 JSON;无→无清单声明。
        if rubric:
            # Oracle ground-truth(供 oracle_cmp/program 类据 formula 与 gt_ref 精确比对);无则空串。
            oracle = getattr(self.config, "oracle_data", None)
            oracle_section = (
                _SECTIONS["oracle"].replace(
                    "{oracle_json}", json.dumps(oracle, ensure_ascii=False, indent=2)
                ) + "\n\n"
            ) if oracle else ""
            # 统一以 JSON 投喂(与 Oracle 块同构),保留 rubric 原始结构供 agent 精确解析。
            criteria = json.dumps(
                [r.model_dump(exclude_none=True) for r in rubric],
                ensure_ascii=False, indent=2,
            )
            rubric_section = (
                _SECTIONS["rubric"]
                .replace("{oracle_section}", oracle_section)
                .replace("{criteria}", criteria)
            )
        else:
            # 无冻结 rubric:显式声明 rubric_checks 必须为空,避免模型把评估维度当准则自拟。
            rubric_section = _SECTIONS["no_rubric"]

        # 一条 replace 链:结构占位符与已构造好的片段先填,自由文本(可能偶含 `{…}` 字面)最后填,
        # 避免被二次替换(同 user_simulator._render 的既有取舍)。
        return (
            _SECTIONS["skeleton"]
            .replace("{window}", str(window))
            .replace("{generated_files_section}", generated_files_section)
            .replace("{rubric_section}", rubric_section)
            .replace("{system_prompt}", self._prompt_template)
            .replace("{origin_query}", trajectory.query)
            .replace("{recent_evidence}", trajectory.render_recent(window))
        )

    def _log(
        self,
        trajectory: Trajectory,
        turn: TurnRecord,
        result: Optional[EvaluationResult],
        error: Optional[str] = None,
        window: int = 1,
        prompt_chars: int = 0,
    ) -> None:
        if not self.config.log_evaluations:
            return
        win_start = max(1, turn.turn - window + 1)  # 本次投喂窗口的起始轮
        record: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "agent_name": trajectory.agent_name,
            "query": trajectory.query,
            "turn": turn.turn,
            "evidence_incomplete": turn.evidence_incomplete,
            "evaluator_agent": self.config.agent_name,
            "eval_step": self.config.eval_step,
            "window": window,
            "window_turns": [win_start, turn.turn],  # 本次评审覆盖的轮次范围(含端点)
            "prompt_chars": prompt_chars,  # 投喂提示词字符数(token 代理量)
            "to_simulator": self.config.to_simulator,
        }
        if result is not None:
            record["evaluation"] = result.model_dump()
        if error is not None:
            record["error"] = error
        eval_logger.info(json.dumps(record, ensure_ascii=False))


def _isolate_eval_files(evaluate: Optional[EvaluateConfig]) -> None:
    """任务执行前:把该 query 的 oracle/rubrics 文件从磁盘删除(内容已在 file_vault 中)。"""
    if evaluate is None or not evaluate.isolate_eval_files:
        return
    for path in evaluate.file_vault:
        try:
            Path(path).unlink(missing_ok=True)
            logger.debug("[文件隔离] 已删除: %s", path)
        except OSError as e:  # noqa: BLE001
            logger.warning("[文件隔离] 删除失败(忽略): %s (%s)", path, e)


def _restore_eval_files(evaluate: Optional[EvaluateConfig]) -> None:
    """任务结束后:把 file_vault 中的原始字节写回原路径(best-effort,纯调试便利)。"""
    if evaluate is None or not evaluate.isolate_eval_files:
        return
    for path, raw_text in evaluate.file_vault.items():
        try:
            Path(path).write_text(raw_text, encoding="utf-8")
            logger.debug("[文件隔离] 已还原: %s", path)
        except OSError as e:  # noqa: BLE001
            logger.warning("[文件隔离] 还原失败(忽略): %s (%s)", path, e)


# ============================================================================
# 创建 Evaluate
# ============================================================================
def create_evaluator(
    evaluate: Optional[EvaluateConfig],
    client: Any,
    run_id: str,
    query_session: str,
    system_prompt: Optional[str] = None,
    get_agent_fn: Optional[Callable[[str, str], Any]] = None,
) -> Optional[Evaluator]:
    """据 query 的 evaluate 块创建 per-query Evaluator;无该块则返回 None。"""
    if evaluate is None:
        return None
    base = evaluate.session_name or query_session
    session_name = f"eval_{base}_{run_id}"
    return Evaluator.create(evaluate, client, run_id, session_name, system_prompt, get_agent_fn)