## Context

既有 trajectory-evaluation 能力(`evaluator.py` + `openclaw_automation.py` 的接入)已实现:独立、无状态 evaluator 逐轮基于磁盘真相/`tool_calls` 评估,产出 `EvaluationResult`(completion/inclination/improvements/violations/citations/reason),经 `format_feedback` → `{evaluator_feedback}` 占位符回流给 `user_simulator`,simulator 终审。默认 `enabled=False` 退回旧行为,`dry_run=True` 时只评估不回流。

痛点:评估对着 `DEFAULT_EVAL_PROMPT` 里的**自由维度**打分,没有针对具体任务的逐条验收清单。口径每轮漂移、跨轮/跨任务不可比,"某条具体要求满足没有"无法沉淀为结构化、可审计的判据。

约束(沿用既有设计哲学):evaluator 独立且无状态;以磁盘真相/工具记录为准;核验受阻不判负;simulator 终审、evaluator 只软反馈;新功能须能安全回滚到旧行为。

## Goals / Non-Goals

**Goals:**
- rubric(验收清单)随用户首次 query 传入,在该 query 整段对话中**冻结**,作为逐条质检依据。
- evaluator 对冻结 rubric **逐条质检** agent 产物,每条产出状态 + 引证;结果进结构化输出与评估日志。
- 回流边界 X:rubric 原文只喂 evaluator;simulator 不感知 rubric;evaluator 反馈仍回流;simulator 终审权不变。
- `dry_run` 改名 `feedback_to_simulator`,语义自我说明。
- 全程可回滚:`enabled=False`、空 rubric、生成/解析失败均退回旧行为。

**Non-Goals:**
- 不做 rubric 自动生成(本期 rubric 来自 query 输入,不由 LLM 生成)。
- 不做硬闸:rubric 全 pass 才放行的强约束**不实现**;simulator 仍软参考、终审。
- 不让 simulator 感知 rubric 原文(明确排除)。
- 不改 `system_prompt.md` 的占位符结构。

## Decisions

### D1：用途 = 质检 agent 产物(而非质检评估本身)
rubric 是任务验收清单,evaluator 拿它逐条核验 **agent 的产物**;评估结果照旧回流 simulator。
- 备选:用 rubric 做"对评估裁决的 QA 闸门"(meta-QA)。**否决**:与"回流至 simulator"的目标不符,且复杂度更高;两者未来可叠加,本期不做。

### D2：rubric 来源 = 随 query 传入 + 冻结(不自动生成)
在 `QueryItem` 新增 `rubric: list[str]`;`execute_queries` 在某 query 的 turn 循环**开始前**取出该 rubric,逐轮原样传入 `evaluate_turn`,循环内不改写。
- 为何冻结:evaluator 是无状态的(每轮新 session)。冻结 rubric 提供唯一稳定锚点,使逐条打分跨轮可比,而不重新引入状态。
- 备选:自动生成一次再冻结(省人工、可规模化)→ 本期不做,留作后续;每轮重生成 → 反模式(漂移),排除。

### D3：回流边界 X —— simulator 不感知 rubric
`_build_prompt` 把 rubric 注入 **evaluator** 提示词并要求逐条核验;`format_feedback` 渲染"未满足项/改进点"但**不输出 rubric 原文**;`system_prompt.md` 的 `{evaluator_feedback}` 占位符与 simulator 链路**不变**。simulator 收到的仍是反馈文字,不知背后有 rubric,终审权不变。

### D4：数据模型 —— 新增 `RubricCheck`,扩 `EvaluationResult`
```python
class RubricCheck(BaseModel):
    criterion: str
    status: Literal["pass", "fail", "partial", "unverifiable"]
    evidence: str   # 引证, 复用"磁盘真相"铁律

class EvaluationResult(BaseModel):
    ...existing...
    rubric_checks: list[RubricCheck] = Field(default_factory=list)
```
- `unverifiable` 与现有 `evidence_incomplete` 同源:核验受阻 ≠ 未做,MUST NOT 据以判负(防假阴性)。
- 空 rubric 时 `rubric_checks` 为空,等价旧行为。

### D5：开关改名 `dry_run` → `feedback_to_simulator`(bool,默认 False)
正向命名,字段名即行为:`True`=回流,`False`=只评估落盘不回流。默认 `False` 的**行为**等同原 `dry_run=True`(安全:开了评估也先不影响 simulator)。
- 触点:`openclaw_automation.py:772` `if not evaluator.dry_run` → `if evaluator.feedback_to_simulator`。
- 备选:`observe_only`(默认 True,改动最小)/三态 `feedback_mode`(observe/live,可扩硬闸)。用户拍板用 `feedback_to_simulator` 并接受默认 False。

### D6：落盘
`_log` 在记录中加入 `rubric_checks`(存在时),供离线质检与一致率校准,沿用 `evaluator_use.log` 每行一条 JSON 的格式。

## Risks / Trade-offs

- [rubric 写得差 → 质检失真] → 本期 rubric 由人工随 query 提供,质量归属调用方;evaluator 仍保留自由维度兜底,空 rubric 即旧行为。
- [逐条核验放大评估 prompt / 增加 token] → rubric 通常条目有限;只注入 evaluator 侧,不污染 simulator;可后续按需截断。
- [改名 `dry_run` 为 BREAKING] → 影响现有含 `evaluator.dry_run` 的配置(取反为 `feedback_to_simulator`);迁移说明见下,且 `enabled=False` 时该字段无效。
- [structured output 解析失败] → `StructuredOutput.execute` 已有 max_retries;失败时 `evaluate_turn` 返回 None(现有 dry-run 安全路径),不阻断任务。

## Migration Plan

1. 配置迁移:把 `evaluator.dry_run: true` 改为 `feedback_to_simulator: false`(取反);`dry_run: false` → `feedback_to_simulator: true`。无该字段者默认 `feedback_to_simulator=false`。
2. rubric 为可选:在需要质检的 `queries[]` 上加 `rubric: [...]`;不加即旧行为。
3. 回滚:`evaluator.enabled=false` 立即回退到 simulator 自判旧行为;或移除 `queries[].rubric` 退回自由维度评估。

## Open Questions

- rubric 条目是否需要权重/critical 标记(为将来"硬闸"或加权完成度铺路)?本期不做,先平权。
- 是否需要把逐条质检结果也用于 `completion` 的计算口径(如按 pass 比例)?本期保持 evaluator 自行裁量,不强制公式。
