## Why

当前 evaluator 采用"每轮新开会话、全量重投历史"的**完全无状态**设计:每个 turn 把
`原始任务 + 历轮全文 + 上轮反馈 + 本轮证据`整体拼进提示词喂给评估模型。这带来三个问题:
(1) 复杂任务下 token 开销随轮次近似平方级膨胀,成本不可控;(2) evaluator 模型未被钉死、
且当前 `agents.create` 路径根本不下发模型,导致裁判模型不可控、可能与被测 agent 同源而产生
同构盲点;(3) 评估配置被劈成"全局 `eval_config` + per-query `rubric`"两半,且 `config_session.json`
里 query 内联的 `evaluate` 块当前被静默忽略,无法 per-query 选裁判、控频率。

本次按 0625 检视意见重构:在**不牺牲防判词锚定**的前提下,把 evaluator 改为"**持久 agent +
每轮 reset 会话 + 有界压缩投喂**",钉死一个独立的 flash 级裁判模型,并把评估配置统一成 per-query
可解析的内联块。

## What Changes

- **裁判模型钉死且可配(#1)**:evaluator 必须用一个固定的、刻意区别于被测 agent 的快模型
  (flash 级)。模型经 `agents.update(model/modelProvider/apiKey)` 下发到 evaluator agent;
  其 URL/api-key 写入 `config_session.json`。**BREAKING**:不再依赖 `agents.create` 携带模型
  (该路径本就不下发,现改为 create 后 update 钉死)。
- **配置项重构为 per-query 内联块(#2)**:query 上以 `evaluate` 块声明评估配置;每个 query 可从
  顶层 `agents` 列表中**自选**一个已声明 agent 充当本 query 的 evaluator。**BREAKING**:废弃
  全局 `eval_config`,改为解析 query 内联 `evaluate`(含 `agent_name`/`session_name`/`rubrics`/
  `eval_step`/`feedback_to_simulator`)。
- **持久 agent + 会话仍无状态(#4)**:同一 query 的各 turn 复用**同一个** evaluator agent
  (不每轮重建,省建连/初始化时间);但**每次评估前 `sessions.reset`** 清空该会话(因 OC 会话会
  存自己上一轮判词并回放,reset 防止判词锚定)。历史不再由 session 承载,而记录在 harness 的
  trajectory 里。
- **有界压缩投喂(#4.4/#6)**:每次评估只投喂压缩后的 trajectory——`原始 query + rubrics +
  最近 X 轮(含 tool_calls)+ generated_files{filename, workspace_path} 指针`,不再投全量历史、
  不再内联文件全文。
- **评审频率可配 + 跳过喂空(#5)**:新增 `eval_step` 配置项,每 X 步触发一次评审;**最近 X 轮的
  X 即 eval_step 的 X**(同一参数,保证窗口正好覆盖两次评审间的所有轮次)。被跳过的轮不触发
  evaluator,且 `evaluator_feedback` 给 simulator 喂空。
- **复杂任务评估支撑(#3)**:补充复杂、多轮的任务配置以真正检验评估效果/资源开销/轮数控制;
  "evaluator 是否需要专属独立 skill"作为开放项在实验中验证(本次不锁定为硬需求)。

## Capabilities

### New Capabilities
<!-- 无全新能力;本次为既有评估能力的重构 -->

### Modified Capabilities
- `trajectory-evaluation`: 将"evaluator 无状态且独立投喂"的 Requirement 改写为"持久 agent +
  每轮 reset 会话 + 有界压缩投喂";新增"裁判模型钉死(agents.update)且独立于被测 agent"、
  "评估配置 per-query 内联且可自选 agent"、"评审频率 eval_step 可配且跳过轮喂空"三组 Requirement;
  调整投喂内容口径为"最近 X 轮 + 产物指针",并据此收敛 token 开销。

## Impact

- **代码**:`evaluator.py`(EvaluatorConfig 字段重构、evaluate_turn 改 reset+压缩投喂、模型下发)、
  `trajectory.py`(新增"最近 X 轮压缩渲染"与"产物指针"渲染、`generated_files` 精简为 filename+path)、
  `openclaw_automation.py`(QueryItem 解析内联 `evaluate` 块、process_turn 按 eval_step 节流、
  `_setup_evaluator_agent` 改为 create 后 `agents.update` 钉模型)、`configs/config_session.json`
  (新增模型 URL/api-key 与 per-query `evaluate` 字段)。
- **SDK 接口**:改用 `gateway.agents_update(agentId, model=…, modelProvider=…, apiKey=…)`;评估前调用
  `gateway.sessions_reset(session_key)`。
- **spec**:`openspec/specs/trajectory-evaluation/spec.md` 多条 Requirement 被改写/新增。
- **行为**:启用 evaluator 的 query,默认不再每轮评审(取决于 `eval_step`),simulator 在跳过轮拿到
  空反馈;裁判模型固定为配置中的 flash 模型。
