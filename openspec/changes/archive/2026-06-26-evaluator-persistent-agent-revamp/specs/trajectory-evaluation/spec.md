## MODIFIED Requirements

### Requirement: 每轮由独立 evaluator 评估并反馈给 simulator

系统 SHALL 在**符合评审频率**(由 `eval_step` 决定)的 turn 调用一个独立的 evaluator 对该评审点之前(含本轮)的执行做评估,并将评估结果反馈给 `user_simulator`。该 evaluator MUST 是一个独立于执行任务 agent 的 OC agent,MUST NOT 复用执行任务的那个 agent。**同一 query 的各 turn SHALL 复用同一个 evaluator agent 实体**(不每轮重建,以省去重复建连/初始化开销);该 evaluator agent SHALL 可由 query 从顶层 `agents` 列表中自选。未到评审点的 turn,系统 SHALL NOT 触发评估,并 SHALL 给 `user_simulator` 喂空反馈。

#### Scenario: 到达评审点触发评估
- **WHEN** 某 turn 满足评审频率(如第 `eval_step` 的整数倍轮),且 evaluator 已启用
- **THEN** 系统 SHALL 调用 evaluator 对当前进展评估,并在 `user_simulator` 决策前把评估结果交给它

#### Scenario: 未到评审点跳过评估并喂空
- **WHEN** 某 turn 未达到评审频率
- **THEN** 系统 SHALL NOT 触发 evaluator,且注入 `user_simulator` 的 `evaluator_feedback` SHALL 为空

#### Scenario: evaluator 不得是执行任务的 agent
- **WHEN** 系统装配 evaluator
- **THEN** evaluator SHALL 使用与执行任务 agent 不同的 OC agent(独立身份/会话),以避免自评自盖章与同源盲点

#### Scenario: 同一 query 各轮复用同一 evaluator agent
- **WHEN** 同一 query 的多个 turn 先后触发评审
- **THEN** 系统 SHALL 复用同一个 evaluator agent 实体,MUST NOT 为每个评审点新建 agent

### Requirement: evaluator 无状态且独立投喂

evaluator 的**会话**SHALL 以无状态方式运行:**evaluator agent 实体在同一 query 内持久复用,但其会话 SHALL 在每次评估之前被 `sessions.reset` 清空**——因为 OC 会话会持久化并回放 agent 自身的历史回复(含上一轮判词),若不清空将造成判词自我锚定。每轮评估所需上下文 SHALL 由系统从 harness 侧记录的 trajectory **显式压缩投喂**,session MUST NOT 承担跨轮记忆职责。

投喂内容 SHALL 为压缩后的 trajectory,仅包含:原始任务(`origin_query`)、冻结 rubrics、**最近 X 轮**的执行记录(含该轮 `tool_calls`)、以及产物文件指针 `generated_files{filename, workspace_path}`;系统 MUST NOT 投喂全量历轮记录,MUST NOT 内联产物文件全文,MUST NOT 把 evaluator 自身上一轮的结构化判词回投给它(防锚定)。"进步感知"SHALL 由最近 X 轮的**证据变化**体现,而非由复用自身会话记忆或回放旧判词得到。

#### Scenario: 每次评估前 reset 会话
- **WHEN** 进入一个评审点、准备调用 evaluator
- **THEN** 系统 SHALL 先对该 evaluator 会话执行 `sessions.reset`,确保其上一轮回复/判词不被回放进本轮评估

#### Scenario: 复用持久 agent 而非每轮重建
- **WHEN** 同一 query 的下一个评审点到来
- **THEN** 系统 SHALL 复用既有 evaluator agent 实体并仅 reset 其会话,MUST NOT 重新创建 agent 或重新初始化连接

#### Scenario: 有界压缩投喂(最近 X 轮 + 产物指针)
- **WHEN** 系统为某次评估构建投喂上下文
- **THEN** 投喂 SHALL 仅含 `origin_query` + rubrics + 最近 X 轮(含 `tool_calls`)+ `generated_files{filename, workspace_path}` 指针,MUST NOT 含全量历轮或产物全文

#### Scenario: 不回放自身历史判词(防锚定)
- **WHEN** evaluator 在某 query 的第二个及以后评审点工作
- **THEN** 其本轮裁定 MUST NOT 受其自身上一轮判词影响——既因 reset 使会话不回放该判词,也因投喂内容不含其上一轮结构化判决

#### Scenario: 进步感知来自证据 delta
- **WHEN** 系统投喂了最近 X 轮记录
- **THEN** evaluator SHALL 据这几轮的证据变化判断"先前问题本轮是否改进",而 MUST NOT 依赖跨轮的自身会话记忆

## ADDED Requirements

### Requirement: 裁判模型钉死且独立于被测 agent

系统 SHALL 为 evaluator agent 钉死一个固定模型,该模型 SHALL 刻意区别于被测执行 agent(建议采用更快的 flash 级模型),并在整段评估过程中保持不变。模型 SHALL 通过 `agents.update(agentId, model=…, modelProvider=…, apiKey=…)` 下发到 evaluator agent;系统 MUST NOT 依赖 `agents.create` 携带模型(该 RPC 仅接收 name/workspace,不下发模型)。evaluator 模型的连接信息(模型名/provider/URL/api-key)SHALL 可在 `config_session.json` 中配置。

#### Scenario: 经 agents.update 钉死模型
- **WHEN** 系统装配 evaluator agent
- **THEN** 系统 SHALL 在创建该 agent 后调用 `agents.update` 下发固定的 `model/modelProvider/apiKey`,使其评估始终使用该模型

#### Scenario: 裁判模型独立于被测 agent
- **WHEN** 配置 evaluator 模型
- **THEN** 该模型 SHALL 不要求与被测 agent 一致,且 SHALL 倾向选用与之不同的快模型,以降低同构盲点

#### Scenario: 模型连接信息可配
- **WHEN** 在 `config_session.json` 中声明 evaluator 的模型连接(model/provider/URL/api-key)
- **THEN** 系统 SHALL 据此下发模型;缺失关键连接信息时 SHALL 明确告警而非静默退回被测模型

### Requirement: 评估配置随 query 内联且可自选 evaluator agent

系统 SHALL 支持在 query 上以内联 `evaluate` 块声明本 query 的评估配置(至少含 `agent_name`、`session_name`、`rubrics`、`eval_step`、`feedback_to_simulator`),并 SHALL 实际解析该块(此前该内联块被静默忽略)。每个 query SHALL 能从顶层 `agents` 列表中**自选**一个已声明 agent 作为本 query 的 evaluator;所选 evaluator agent MUST NOT 与本 query 的执行 agent 同名。

#### Scenario: 解析 query 内联 evaluate 块
- **WHEN** 某 query 携带 `evaluate` 内联块且 evaluator 启用
- **THEN** 系统 SHALL 解析其中的 `agent_name/session_name/rubrics/eval_step/feedback_to_simulator` 并据此驱动评估,MUST NOT 忽略该块

#### Scenario: 自选 agents 列表中的 agent 作为 evaluator
- **WHEN** query 的 `evaluate.agent_name` 指向顶层 `agents` 列表中的某个已声明 agent
- **THEN** 系统 SHALL 用该 agent 充当本 query 的 evaluator

#### Scenario: evaluator 与执行 agent 不同名
- **WHEN** query 的 `evaluate.agent_name` 与该 query 的执行 `agent_name` 相同
- **THEN** 系统 SHALL 报错或拒绝装配,以保证裁判独立

### Requirement: 评审频率可配且跳过轮喂空

系统 SHALL 以配置项 `eval_step` 控制评审频率:每 `eval_step` 个 turn 触发一次评审。**最近 X 轮投喂窗口的 X SHALL 等于 `eval_step`**(同一参数),以保证投喂窗口正好覆盖两次评审之间的全部 turn、不留上下文空洞。系统 SHALL 在**每个 turn** 照常捕获 trajectory(供评审窗口取数),但仅在评审点触发 evaluator。被跳过的 turn,系统 SHALL 给 `user_simulator` 喂空 `evaluator_feedback`。`eval_step` SHALL 可调,以便在不同取值下对比评估效果与资源开销。

#### Scenario: 每 eval_step 轮评审一次
- **WHEN** `eval_step = N`,对话进行到第 N、2N、… 轮
- **THEN** 系统 SHALL 在这些 turn 触发评审,其余 turn 不触发

#### Scenario: 投喂窗口 X 等于 eval_step
- **WHEN** 在某评审点构建投喂
- **THEN** 投喂的"最近 X 轮"中 X SHALL 等于 `eval_step`,使该窗口覆盖自上次评审以来的全部 turn

#### Scenario: 跳过轮仍捕获 trajectory
- **WHEN** 某 turn 未触发评审
- **THEN** 系统 SHALL 仍捕获该轮带证据的 trajectory(tool_calls/产物),以便下个评审点的窗口可取到该轮数据

#### Scenario: 跳过轮喂空反馈
- **WHEN** 某 turn 未触发评审
- **THEN** 注入 `user_simulator` 的 `evaluator_feedback` SHALL 为空,使 simulator 在该轮自主判定
