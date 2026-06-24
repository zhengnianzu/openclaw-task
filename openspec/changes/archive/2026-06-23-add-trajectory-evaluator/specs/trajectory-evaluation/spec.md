## ADDED Requirements

### Requirement: 每轮由独立 evaluator 评估并反馈给 simulator

系统 SHALL 在每个 turn 的 agent 回复之后,调用一个独立的 evaluator 对本轮执行做评估,并将评估结果反馈给 `user_simulator`。该 evaluator MUST 是一个独立于执行任务 agent 的 OC agent,MUST NOT 复用执行任务的那个 agent。

#### Scenario: 每轮 agent 回复后触发评估
- **WHEN** 某 turn 中 agent 产生了回复(且 evaluator 已启用)
- **THEN** 系统 SHALL 调用 evaluator 对该轮执行评估,并在 `user_simulator` 决策前把评估结果交给它

#### Scenario: evaluator 不得是执行任务的 agent
- **WHEN** 系统装配 evaluator
- **THEN** evaluator SHALL 使用与执行任务 agent 不同的 OC agent(独立身份/会话),以避免自评自盖章与同源盲点

### Requirement: 基于真实证据并可用工具核验

evaluator SHALL 基于本轮的可核验证据(`tool_calls` 与经磁盘真相校正的文件)进行评估,而非仅凭 agent 的文本说辞。evaluator SHALL 能使用自身工具对被审查产物做主动核验(如打开文件、检索、运行校验)。

#### Scenario: 拆穿话术型假阳性
- **WHEN** agent 声称完成了某操作,但本轮证据(`tool_calls` 与磁盘上的文件)中没有任何支撑
- **THEN** evaluator SHALL 在反馈中明确指出"声称与证据矛盾",据以阻止该轮被判为完成

#### Scenario: 工具核验产物
- **WHEN** 被审查的产物文件已按约定推送至 evaluator 自身工作区
- **THEN** evaluator SHALL 可用自身工具读取/核验该产物,并将核验所得作为评估依据

### Requirement: evaluator 无状态且独立投喂

evaluator SHALL 以无状态方式运行:每轮新开会话,由系统显式投喂所需上下文(原始任务、历轮全文记录、上一轮 evaluator 反馈、本轮证据)。evaluator MUST NOT 依赖跨轮累积的自身会话记忆。

#### Scenario: 每轮独立评估
- **WHEN** 进入新一轮评估
- **THEN** 系统 SHALL 为 evaluator 提供本轮所需的全部上下文,且该轮评估结果 SHALL 仅由所投喂的输入决定,不受 evaluator 上一轮内部状态影响

#### Scenario: 具备进步感知而不自我锚定
- **WHEN** 系统投喂了历轮记录与上一轮 evaluator 反馈
- **THEN** evaluator SHALL 能据此判断"上轮指出的问题本轮是否改进",同时其判断 MUST NOT 因复用自身历史会话而被旧判词锚定

### Requirement: simulator 据反馈决策且仍为判定方

最终的 `【Task_Done】`/`【Task_Failed】`/继续下一轮 SHALL 仍由 `user_simulator` 输出;evaluator 为顾问角色。`user_simulator` 在做出该判定时 SHALL 参考 evaluator 的证据化反馈。

#### Scenario: 反馈注入 simulator 决策
- **WHEN** evaluator 产出本轮评估反馈
- **THEN** 系统 SHALL 把该反馈注入 `user_simulator` 的判定上下文(经 system prompt 占位符),使其在判 `Task_Done`/`Failed`/继续 时据此调整

#### Scenario: 证据矛盾时不轻易放行
- **WHEN** evaluator 反馈指出 agent 的声称与磁盘真相/工具证据矛盾
- **THEN** `user_simulator` SHALL NOT 仅凭 agent 的文本说辞判定 `Task_Done`,而应据该反馈继续追问或判失败

### Requirement: 评估输出结构化、带引证、可落盘

evaluator 的输出 SHALL 为结构化结果,至少包含:任务完成度、改进点列表、不符合要求项、整体倾向(是否倾向放行)。其中关键判断 SHALL 引用轨迹中的具体语句、工具返回或文件内容作为引证。系统 SHALL 将每次评估追加到独立评估日志,字段足以离线复现该评估依据。

#### Scenario: 输出结构化且带引证
- **WHEN** evaluator 完成一轮评估
- **THEN** 其结果 SHALL 包含完成度/改进点/不符合项/整体倾向,且每个关键判断 SHALL 引用本轮证据中的具体依据

#### Scenario: 评估落盘供校准
- **WHEN** evaluator 完成任意一次评估
- **THEN** 系统 SHALL 追加一条结构化记录到评估日志(含轨迹标识、各项结论、引证、所用裁判模型),供离线复核与一致率校准
