## Context

现状(见 `evaluator.py` / `trajectory.py` / `openclaw_automation.py`):

- evaluator 为**完全无状态**:`evaluate_turn` 每轮新开会话 `eval_{run_id}_{agent}_{turn}`,
  `_build_prompt` 投喂 `system + render_full(全量历轮) + last_feedback(上轮判词) + 本轮证据`,
  其中产物文件内容内联(`content[:2000]`)、tool_calls 输出内联(`output[:2000]`)。
- 模型未钉:`_setup_evaluator_agent` 仅 `agents.create(name, workspace)`,代码注释已说明
  "网关 agents.create 不下发模型"(`openclaw_automation.py:1005`),`EvaluatorConfig.model` 实际无效。
- 配置割裂:全局 `eval_config`(EvaluatorConfig)+ per-query `QueryItem.rubric`;
  `config_session.json` 里 query 内联的 `evaluate` 块当前被 Pydantic 当额外字段**静默忽略**。
- 评审频率写死为"每 turn 评一次"(`process_turn` 每轮无条件调用)。

SDK 事实(已核验):
- `core/config.py:73` `AgentConfig` 含 `llm_provider/llm_model/llm_api_key`,但 `to_openclaw_agent()`
  **不序列化** llm_* 字段;`client.create_agent` → `gateway.agents_create(name, workspace)` 也只发
  name+workspace。**故 create 路径钉不住模型。**
- `gateway/base.py:683` `agents_update(agent_id, **patch)` → `agents.update`;`config/manager.py:219-242`
  显示网关侧 agent 配置认 camelCase 的 `model / modelProvider / apiKey`。**这是钉模型的可行路径。**
- `gateway/base.py:156` `sessions_reset(key)` → `sessions.reset`,清的是**对话记忆**,不动工作区文件;
  `chat.history` 可取回服务端历史 ⇒ 证实同一 session 会持久化并回放 agent 自身回复(判词锚定来源)。
- per-agent **没有** base_url 槽位:`openai_base_url` 在 ClientConfig(整网关级),URL 基本由
  `modelProvider` 决定 → 自定义 endpoint 的可行性需在落地前确认(见 Open Questions)。

## Goals / Non-Goals

**Goals:**
- 把 evaluator 从"完全无状态全量重投"改为"**持久 agent + 每轮 reset 会话 + 有界压缩投喂**",
  在保住"防判词锚定"的同时把 token/时延开销收敛。
- 用 `agents.update` 钉死一个独立、固定的 flash 级裁判模型。
- 评估配置统一为 per-query 内联 `evaluate` 块,可从 `agents` 列表自选 evaluator。
- 评审频率 `eval_step` 可配,跳过轮给 simulator 喂空;最近 X 轮窗口 X = eval_step。

**Non-Goals:**
- 不改 simulator 仍为最终判定方的定位(evaluator 仍是顾问、软反馈)。
- 不改 rubric 不进 simulator 的边界(`format_feedback` 仍不渲染 rubric_checks)。
- 不在本次锁定"evaluator 专属 skill"为硬需求——作为实验开放项(#3)。
- 不实现 per-agent 自定义 base_url(若网关不支持,退回 provider 默认/全局 URL)。

## Decisions

### D1:持久 agent + 每轮 reset 会话(取代"每轮新建会话")
- **做法**:同一 query 复用一个 evaluator agent 实体;每个评审点先 `sessions.reset(session_key)`
  再投喂。`session_key = agent:{agent_id}:{evaluate.session_name}`。
- **为何**:4.1 的痛点是"每轮重建 agent"的建连/初始化时间——复用 agent 即可消除;4.3/4.4 的
  "防判词锚定"靠 reset 会话即可保证。两件事解耦:**省时间靠不重建,防锚定靠 reset**。
- **替代方案**:(a) 维持"每轮新 session 名"——也无状态,但会堆积一次性 session,且不符合 #4
  "只用一个 agent"的口径;(b) 真有状态跟随(不 reset)——token 最省,但自己的判词必被回放 →
  违反 4.3,否决。
- **关键澄清**:4.2 "历史记录在 session 里" 与 4.4 "每轮 reset" 互斥;以 4.4 为准——
  **历史的唯一权威是 harness 的 trajectory,session 不承担记忆**。

### D2:有界压缩投喂(最近 X 轮 + 产物指针)
- **做法**:新增 trajectory 的"最近 X 轮压缩渲染";投喂 = `origin_query + rubrics + 最近 X 轮
  (含 tool_calls)+ generated_files{filename, workspace_path}`。删去全量历轮、删去文件全文内联。
- **为何**:#6/#4.4 收敛 token。文件改为**指针**,evaluator 用自身工具打开 `workspace_path`
  核验(契合既有 `_push_review_files`:reset 不清工作区,推进的待审文件仍在)。
- **保留底线**:tool_calls **必须留在窗口里**(精简:工具名+关键入参+截断输出)。否则评估退化为
  "听 agent 文本说辞",丢掉反幻觉能力(spec "拆穿话术型假阳性")。这是不可让步项。
- **last_feedback 处置**:不再把 evaluator 自身上一轮判词回投(原 `_build_prompt:231`)——
  那是另一种判词锚定。进步感知改由"窗口内 X 轮证据 delta"承载。

### D3:`agents.update` 钉模型
- **做法**:`_setup_evaluator_agent` 在 `agents.create` 后,调
  `gateway.agents_update(agent_id, model=…, modelProvider=…, apiKey=…)` 下发固定模型。
- **为何**:create 路径不下发模型(已核验);update 是网关认可的 patch 通道。
- **配置**:`config_session.json` 的 agent 声明或 evaluate 块补 `model/modelProvider/apiKey`
  (URL 待 Open Questions 定)。下发失败/缺关键信息 → 明确告警,不静默退回被测模型。

### D4:per-query 内联 `evaluate` 解析 + 自选 agent
- **做法**:`QueryItem` 新增 `evaluate: Optional[EvaluateConfig]`;`EvaluateConfig` 含
  `agent_name/session_name/rubrics/eval_step/feedback_to_simulator`。装配时校验
  `evaluate.agent_name ∈ agents` 且 `≠ query.agent_name`。
- **为何**:#2 配置统一;#2.2 每 query 自选裁判。废弃全局 `eval_config`(BREAKING)。
- **兼容**:`rubric` 从 QueryItem 顶层迁入 `evaluate.rubrics`(同一份,冻结语义不变)。

### D5:`eval_step` 节流 + 跳过喂空 + 窗口对齐
- **做法**:`process_turn` 仍每轮捕获 trajectory;`turn % eval_step == 0` 才触发 evaluator;
  否则 `evaluator_feedback = None/""`。投喂窗口 X 取 `eval_step`(5.3)。
- **为何**:#5 节流;窗口=步长保证两次评审间无上下文空洞(reset 后窗口外不可见)。

## Risks / Trade-offs

- **[reset 与投喂顺序竞态]** reset 必须在投喂/评估之前完成 → 顺序 `reset → push 文件 → send`,
  全程 await 串行;reset 不清工作区,推进文件不受影响。
- **[窗口外上下文丢失]** reset 后 evaluator 只见最近 X 轮;早期建立的约束/产物若超出窗口则不可见。
  → 缓解:`generated_files` 指针给**累积全部产物**(不止最近 X 轮);rubrics/origin_query 每轮必投。
- **[X 取值影响评估质量]** eval_step 偏大→评审稀疏、窗口大→单次 token 高但漏判风险低;偏小→相反。
  → 缓解:#3 实验在 eval_step ∈ {1,3,5,…} 下对比"评估效果 vs token/时延",数据驱动取值。
- **[模型下发失败被静默吞掉]** 若 `agents.update` 不被网关接受 → 退回默认模型却以为钉死了。
  → 缓解:下发后校验 + 失败告警;必要时探测网关是否支持该 patch。
- **[只传文本回复丢证据]** 若压缩误删 tool_calls → 反幻觉退化。→ 缓解:D2 把 tool_calls 列为
  窗口必含项,代码与 spec 双重约束。
- **[per-agent URL 不支持]** 自定义 flash endpoint 可能无 per-agent 槽。→ 缓解:退回 provider 默认
  或全局 `openai_base_url`;落地前先验证(Open Questions)。

## Resolved (探测结论)

- **Q1(URL/模型下发)— 已多轮实测(2026-06,连真网关)**:
  - 本网关 OC 配置结构:`config.agents = {defaults, list}`(**非**按 id 索引的扁平字典);
    全局默认在 `agents.defaults.model`,per-agent 覆盖在 `agents.list[i].model`;
    provider 的 URL/key 定义在 `config.models.providers.<provider>.{baseUrl, apiKey}`,
    模型串以 `"provider/model"` 引用它。
  - `agents.update(agentId, model=…)` 是**唯一可靠的 per-agent 通道**:只认 `model`,但
    `model` **可带 provider 前缀** `"provider/model"`,据此 per-agent 选模型 + 选已定义 provider。
    实测写入 `agents.list[evaluator].model="custom-yibuapi-com/gemini-3-flash-preview"` 成功,evaluator 正常回话。
  - **不可用通道**:`config.set/patch` 整份回写被本网关拒(`invalid config`);SDK 的
    `ConfigManager.set_agent_model/get_agent_model` 假设 `agents` 为扁平字典,与本网关
    `{defaults,list}` 结构**不兼容**(set 会破坏结构 → invalid config;get 返回 unknown)。
    ⇒ **新 provider 的 baseUrl/apiKey 无法经 harness 下发,必须在网关侧 `models.providers` 配置**。
  - 实现取舍:`_pin_model` 组装 `provider/model` 经 agents.update 下发(灵活选模型+provider);
    `base_url/api_key` 字段仅作信息性提示(提醒去网关侧配 provider),不尝试下发。
  - 默认 provider `custom-yibuapi-com` 已能服务 `gemini-3-flash-preview`,故 evaluator 仅需选模型即可,
    无需新建 provider。
  - `sessions.reset` 实测可用且**确实抹除会话记忆**(同会话不 reset 能回忆暗号、reset 后回忆不起),
    防判词锚定机制在 SDK 层得证。

## Open Questions
- **Q2(专属 skill,#3)**:evaluator 是否需要一个专属独立 skill(固化评估职责/人格,替代每轮在
  prompt 里拼 `DEFAULT_EVAL_PROMPT`)?倾向"实验验证后再定",非本次硬需求。
- **Q3(复杂任务集,#3.1)**:需要新增哪些复杂、多轮的任务配置才能真正压测评估效果与开销?
  作为 tasks 的一项产出。
