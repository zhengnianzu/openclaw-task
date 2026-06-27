## Why

当前 evaluator 只对着 `DEFAULT_EVAL_PROMPT` 里几条**自由维度**(达成度/真实性/约束遵守)打分,缺少一份针对具体任务的、逐条可核验的**验收清单(rubric)**。这导致评估口径每轮漂移、跨轮/跨任务不可比,且无法把"这条具体要求满足了没有"沉淀成结构化、可审计的判据。引入随 query 传入并冻结的 rubric,让 evaluator 逐条质检 agent 产物,可显著提升评估一致性与回流反馈的精准度。

## What Changes

- 在 `QueryItem` 上新增 `rubric: list[str]` 字段:验收清单随用户首次 query 一起传入,在该 query 的整段多轮对话中**冻结固定**,不每轮重生成。
- evaluator 评估时把冻结的 rubric 注入提示词,**逐条核验** agent 本轮产物,每条产出 `pass / fail / partial / unverifiable` + 引证;`unverifiable` 与现有 `evidence_incomplete` 同源语义(核验受阻 ≠ 未做,不据此判负)。
- `EvaluationResult` 新增 `rubric_checks` 字段;每轮评估的逐条结果落入 `evaluator_use.log` 供离线质检/校准。
- 回流边界(决策 X):rubric **原文只喂给 evaluator**,`user_simulator` **不感知 rubric**;evaluator 按 rubric 打分后的反馈(未满足项/改进点)照旧回流,simulator 仍为最终判定方且终审权不变。
- 将 `EvaluatorConfig.dry_run` 改名为 `feedback_to_simulator`(bool,默认 `False`):语义为"是否把反馈真正回流给 simulator",默认不回流(等价于原 `dry_run=True` 的安全行为)。**BREAKING**:配置字段 `dry_run` 移除,改用 `feedback_to_simulator`(极性相反)。
- 兜底:rubric 缺省(空清单)或核验失败时,evaluator 退回原有自由维度评估,不影响任务推进。

## Capabilities

### New Capabilities
<!-- 无新增能力;本变更扩展既有 trajectory-evaluation 能力 -->

### Modified Capabilities
- `trajectory-evaluation`: 评估在自由维度之外新增"对冻结 rubric 的逐条质检";结构化输出新增逐条 rubric 结果;明确 rubric 原文只作用于 evaluator、simulator 不感知;反馈回流由 `feedback_to_simulator` 开关控制。

## Impact

- 代码:
  - `openclaw_automation.py`:`QueryItem` 新增 `rubric`;`EvaluatorConfig` 字段 `dry_run → feedback_to_simulator`;`execute_queries` 在 turn 循环前取出该 query 的冻结 rubric 并逐轮传入 `evaluate_turn`;`:772` 回流判断由 `if not evaluator.dry_run` 改为 `if evaluator.feedback_to_simulator`。
  - `evaluator.py`:`EvaluatorConfig` 改名字段;`EvaluationResult` 新增 `rubric_checks: list[RubricCheck]`;`evaluate_turn` 接收 rubric 入参;`_build_prompt` 注入冻结 rubric 并要求逐条核验;`format_feedback` 渲染"未满足项/改进点"但**不输出 rubric 原文**;`_log` 落 `rubric_checks`。
- 配置:现有含 `evaluator.dry_run` 的配置需改为 `feedback_to_simulator`(取反);`queries[].rubric` 为可选,缺省即旧行为。
- 文档:`docs/DESIGN.md` 闭环流程一节补充 rubric 质检与回流边界;`docs/CONFIG_STRUCTURE.md` 同步字段。
- 不变:`system_prompt.md` 的 `{evaluator_feedback}` 占位符不变(X 决策:simulator 无感)。
