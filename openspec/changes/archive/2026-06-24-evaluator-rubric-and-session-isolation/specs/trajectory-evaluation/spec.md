## MODIFIED Requirements

### Requirement: 评估输出结构化、带引证、可落盘

evaluator 的输出 SHALL 为结构化结果,至少包含:任务完成度、改进点列表、不符合要求项、整体倾向(是否倾向放行)。当存在冻结 rubric 时,该结构化结果 SHALL 额外包含一份**逐条 rubric 质检结果**,每条含准则、状态(满足/不满足/部分满足/无法核验)与引证。**当不存在冻结 rubric(空清单)时,该结构化结果中的逐条 rubric 质检结果 SHALL 为空;系统 MUST NOT 让 evaluator 自拟 rubric 准则,也 MUST NOT 把评估维度当作 rubric 准则填入逐条质检结果。** 其中关键判断 SHALL 引用轨迹中的具体语句、工具返回或文件内容作为引证。系统 SHALL 将每次评估追加到独立评估日志,字段足以离线复现该评估依据,且当存在 rubric 时 SHALL 包含逐条 rubric 质检结果以供离线质检与一致率校准。

为保证"无 rubric 即为空"在模型偶发幻觉下仍然成立,系统 SHALL 在两个层面共同约束:在评估提示词中显式声明无 rubric 时逐条质检结果必须为空;并在解析评估输出后做确定性归一——当本轮无冻结 rubric 时,将逐条 rubric 质检结果强制置空。该归一 SHALL 在评估落盘日志之前完成,且 MUST NOT 改变完成度/倾向等其他裁定,也 MUST NOT 据此判 agent 未达成。

#### Scenario: 输出结构化且带引证
- **WHEN** evaluator 完成一轮评估
- **THEN** 其结果 SHALL 包含完成度/改进点/不符合项/整体倾向,且每个关键判断 SHALL 引用本轮证据中的具体依据

#### Scenario: 含 rubric 时输出逐条质检结果
- **WHEN** evaluator 在存在冻结 rubric 的情况下完成一轮评估
- **THEN** 其结构化结果 SHALL 额外包含逐条 rubric 质检结果(准则/状态/引证)

#### Scenario: 无 rubric 时逐条质检结果为空
- **WHEN** 某 query 未提供冻结 rubric(清单为空),evaluator 完成一轮评估
- **THEN** 其结构化结果中的逐条 rubric 质检结果 SHALL 为空数组,且落盘日志中该字段亦 SHALL 为空——即便模型在生成时自拟了准则,系统也 SHALL 在落盘前将其归一为空

#### Scenario: 评估落盘供校准
- **WHEN** evaluator 完成任意一次评估
- **THEN** 系统 SHALL 追加一条结构化记录到评估日志(含轨迹标识、各项结论、引证、所用裁判模型,以及存在 rubric 时的逐条质检结果),供离线复核与一致率校准
