## 1. 数据模型与配置(evaluator.py)

- [x] 1.1 新增 `RubricCheck`(criterion / status: Literal["pass","fail","partial","unverifiable"] / evidence)
- [x] 1.2 `EvaluationResult` 增加 `rubric_checks: list[RubricCheck] = Field(default_factory=list)`
- [x] 1.3 `EvaluatorConfig`:字段 `dry_run` 改名为 `feedback_to_simulator`(bool,默认 `False`),更新 description;同步 `dry_run` property → `feedback_to_simulator` property

## 2. rubric 注入与逐条质检(evaluator.py)

- [x] 2.1 `evaluate_turn` 新增 `rubric: list[str] | None = None` 入参,透传至 `_build_prompt`
- [x] 2.2 `_build_prompt`:存在非空 rubric 时,注入"逐条验收清单"段并要求按每条输出 status+引证;空 rubric 时维持原自由维度提示词
- [x] 2.3 在评估提示词中明确 `unverifiable` 与 `evidence_incomplete` 同源:核验受阻不得判 `fail`
- [x] 2.4 `format_feedback`:渲染"未满足项/改进点"(可由 `rubric_checks` 的 fail/partial 汇总),但 **MUST NOT 输出 rubric 原文**
- [x] 2.5 `_log`:记录中加入 `rubric_checks`(存在时),保持每行一条 JSON

## 3. 编排接入(openclaw_automation.py)

- [x] 3.1 `QueryItem` 新增 `rubric: list[str] = Field(default_factory=list)`
- [x] 3.2 `execute_queries`:在该 query 的 turn 循环**开始前**取出 `query.rubric` 作为冻结 rubric(循环内不改写)
- [x] 3.3 逐轮调用 `evaluator.evaluate_turn(trajectory, turn_record, last_feedback, rubric=frozen_rubric)`
- [x] 3.4 回流判断 `:772` 由 `if not evaluator.dry_run` 改为 `if evaluator.feedback_to_simulator`
- [x] 3.5 确认 `system_prompt.md` 的 `{evaluator_feedback}` 链路不变(X:simulator 不感知 rubric)

## 4. 兼容与回滚

- [x] 4.1 空 rubric → 走原自由维度评估,流程不报错(验证 enabled=True 且 rubric=[] 路径)
- [x] 4.2 `enabled=False` → 完全退回 simulator 自判旧行为(回归验证)
- [x] 4.3 structured output 解析失败 → `evaluate_turn` 返回 None,不阻断任务(沿用现有安全路径)

## 5. 测试(test/test_evaluator.py)

- [x] 5.1 `RubricCheck`/`EvaluationResult.rubric_checks` 结构化解析单测
- [x] 5.2 `_build_prompt` 含 rubric 时注入逐条清单、空 rubric 时不注入的单测
- [x] 5.3 `format_feedback` 不泄漏 rubric 原文的断言(只出未满足项/改进点)
- [x] 5.4 `feedback_to_simulator` 开关:False 不回流、True 回流的行为单测
- [x] 5.5 `unverifiable` 不被当作 fail 判负的单测

## 6. 配置与文档

- [x] 6.1 现有含 `evaluator.dry_run` 的 configs 迁移为 `feedback_to_simulator`(取反)
- [x] 6.2 在示例 config 的某条 query 上补 `rubric: [...]` 示例
- [x] 6.3 `docs/DESIGN.md` 闭环流程一节补充 rubric 质检与回流边界 X
- [x] 6.4 `docs/CONFIG_STRUCTURE.md` 同步 `QueryItem.rubric` 与 `feedback_to_simulator` 字段说明
