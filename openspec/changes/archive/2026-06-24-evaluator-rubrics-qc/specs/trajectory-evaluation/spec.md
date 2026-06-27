## ADDED Requirements

### Requirement: rubric 随 query 传入并在整段对话中冻结

系统 SHALL 支持在 query 上携带一份验收清单(rubric),作为该任务的逐条质检依据。该 rubric SHALL 随用户首次 query 一起传入,并在该 query 的整段多轮对话(全部 turn)中**冻结固定**:系统 MUST NOT 在对话过程中重新生成或修改它。rubric 为可选;当 query 未提供 rubric(空清单)时,系统 SHALL 退回原有的自由维度评估行为,不影响任务推进。

#### Scenario: rubric 随 query 传入并跨轮冻结
- **WHEN** 某 query 携带了非空 rubric 且 evaluator 已启用
- **THEN** 系统 SHALL 在该 query 的每一个 turn 评估中使用同一份 rubric,且各轮所用 rubric 内容 MUST 完全一致(冻结)

#### Scenario: 未提供 rubric 时退回自由评估
- **WHEN** 某 query 未提供 rubric(清单为空)
- **THEN** evaluator SHALL 按原有自由维度方式评估,任务流程 MUST NOT 因缺少 rubric 而中断或报错

### Requirement: evaluator 对 rubric 逐条质检 agent 产物

当存在冻结 rubric 时,evaluator SHALL 把该 rubric 注入评估上下文,并基于本轮可核验证据(`tool_calls` 与磁盘真相文件)对**每一条** rubric 准则逐条裁定。每条裁定 SHALL 给出状态(满足 / 不满足 / 部分满足 / 无法核验)并 SHALL 附带引用本轮证据的具体依据。其中"无法核验"与现有"证据不完整(evidence_incomplete)"同源:核验受阻 MUST NOT 被当作"未满足"据以判负,以免冤枉掉线的 harness。

#### Scenario: 逐条裁定并附引证
- **WHEN** evaluator 在存在冻结 rubric 的情况下完成一轮评估
- **THEN** 其结果 SHALL 为 rubric 中每一条准则给出一个状态(满足/不满足/部分满足/无法核验),且每条 SHALL 引用本轮证据中的具体依据

#### Scenario: 核验受阻不判负
- **WHEN** 某条 rubric 准则因证据缺失/核验受阻而无法判定
- **THEN** evaluator SHALL 将该条标为"无法核验",且 MUST NOT 将其等同于"不满足"来据以判定 agent 未达成

### Requirement: rubric 原文不进入 simulator 且回流受开关控制

rubric 原文 SHALL 仅作用于 evaluator;系统 MUST NOT 将 rubric 准则原文注入 `user_simulator` 的判定上下文。evaluator 基于 rubric 质检后得到的反馈(未满足项/改进点)SHALL 复用既有反馈回流通道交给 `user_simulator`,使其在不感知 rubric 的前提下仍能据反馈调整判定。是否将本轮反馈真正回流给 simulator SHALL 由配置开关 `feedback_to_simulator` 控制:为真时回流,为假时仅评估落盘而不影响 simulator。

#### Scenario: simulator 不感知 rubric 原文
- **WHEN** evaluator 使用 rubric 完成质检并产出反馈
- **THEN** 注入 `user_simulator` 的内容 SHALL 仅为提炼后的反馈(未满足项/改进点),MUST NOT 包含 rubric 准则原文

#### Scenario: 回流开关控制反馈是否影响 simulator
- **WHEN** `feedback_to_simulator` 为假
- **THEN** 系统 SHALL 仍执行评估并落盘,但 MUST NOT 把本轮反馈注入 `user_simulator` 的判定上下文

## MODIFIED Requirements

### Requirement: 评估输出结构化、带引证、可落盘

evaluator 的输出 SHALL 为结构化结果,至少包含:任务完成度、改进点列表、不符合要求项、整体倾向(是否倾向放行)。当存在冻结 rubric 时,该结构化结果 SHALL 额外包含一份**逐条 rubric 质检结果**,每条含准则、状态(满足/不满足/部分满足/无法核验)与引证。其中关键判断 SHALL 引用轨迹中的具体语句、工具返回或文件内容作为引证。系统 SHALL 将每次评估追加到独立评估日志,字段足以离线复现该评估依据,且当存在 rubric 时 SHALL 包含逐条 rubric 质检结果以供离线质检与一致率校准。

#### Scenario: 输出结构化且带引证
- **WHEN** evaluator 完成一轮评估
- **THEN** 其结果 SHALL 包含完成度/改进点/不符合项/整体倾向,且每个关键判断 SHALL 引用本轮证据中的具体依据

#### Scenario: 含 rubric 时输出逐条质检结果
- **WHEN** evaluator 在存在冻结 rubric 的情况下完成一轮评估
- **THEN** 其结构化结果 SHALL 额外包含逐条 rubric 质检结果(准则/状态/引证)

#### Scenario: 评估落盘供校准
- **WHEN** evaluator 完成任意一次评估
- **THEN** 系统 SHALL 追加一条结构化记录到评估日志(含轨迹标识、各项结论、引证、所用裁判模型,以及存在 rubric 时的逐条质检结果),供离线复核与一致率校准
