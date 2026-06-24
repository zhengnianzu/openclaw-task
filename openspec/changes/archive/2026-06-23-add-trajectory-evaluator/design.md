## Context

当前系统(`openclaw_automation.py` 的 `execute_queries`)用 `user_simulator` 既扮演"用户"推进对话,又通过吐 `【Task_Done】`/`【Task_Failed】` 充当成败裁判。这种"运动员兼裁判"导致假阳性(被话术哄过)与假阴性(人设太较真)。且 `user_simulator` 是**单轮 LLM、无工具**,只能读 `result.content` 纯文本,无从核验。

经查证 SDK(`openclaw_sdk`)与本仓库代码:
- `agent.execute()` 返回的 `ExecutionResult` 携带 `tool_calls`(`tool`/`input`/`output`/`duration_ms`)与 `files`;但 `execute_queries`(第 705 行 `agent_reply = result.content`)仅消费 `.content`,证据被丢弃。
- **网关文件方法按 `agentId` 寻址**:`gateway.agents_files_list/get/set(agent_id, ...)`(`gateway/base.py` 691–706)。harness 可传任意 `agentId`,隔着网关直读/写**任何** agent 工作区的真实文件——跨 agent 取证不依赖被测/裁判 agent 自身的工具沙箱。
- `client.get_agent(name, session)` + `AgentManager.setup_agent` 可注册并驱动多个独立 agent;**新建 agent 会触发网关重启**,代码里固定等待 90s(`_wait_gateway_ready`)。同一 agent 换 session 则无此代价。
- 每个 agent 各自独立工作区:`WorkspaceManager.get_agent_workspace` → `base_dir-<name>`。
- `经 history_fallback` 兜底恢复的结果来自 `chat.history`,只有文本,无工具/文件证据。
- `user_simulator.chat(query)` 是单轮 OpenAI 调用,system prompt 由 `system_prompt.md` 用 `{origin_query}/{user_profile}/{user_directory}/{conversation_history}` 渲染;判定 `Task_Done` 等语义写在该 prompt 里。

约束:不改 OpenClaw gateway/SDK;evaluator 用独立 OC agent(非执行 agent);系统是离线评测/跑批,可接受额外延迟与成本换质量。

## Goals / Non-Goals

**Goals:**
- 把"判定/评估"与"扮演用户"两个角色分离:新增独立、带工具的 evaluator 负责评估。
- evaluator 每轮基于**可核验证据**(工具调用 + 磁盘真相文件)评估,产出"完成度/改进点/不符合项 + 引证"反馈给 simulator。
- 形成"执行→评估→反馈→引导"闭环,逐轮提升 agent 的实际完成质量(而非仅事后过滤)。
- 输出结构化、带引证、可落盘的评估,支持离线校准。

**Non-Goals:**
- 不改造 OpenClaw 服务端或 SDK 协议。
- 不让 evaluator 直接夺取判定权:最终 `Task_Done`/`Failed`/继续 仍由 `user_simulator` 拍板(evaluator 是顾问)。
- 不重做 `ResilientGateway` 重连或 fail-fast 整批终止(属另外的已知缺陷)。
- 不实现自动化的人工标注/校准平台(仅留出日志接口)。
- 不依赖 evaluator agent 执行内工具的跨工作区沙箱权限(该网关服务端策略本仓库不可见,故设计绕开)。

## Decisions

**D1 — Evaluator 是独立的 OC agent(非执行 agent)**
新建一个独立 agent(如 `evaluator`)作为裁判,经 `client.get_agent` 驱动、`agent.execute()` 调用,**可使用自身工具**主动核验。MUST NOT 复用执行任务的那个 agent(避免自评自盖章与同源盲点)。
- 备选:用无工具的外部 LLM 当裁判。**否决**:放弃了"用工具主动核验 OC 结果"的能力,退化回纯文本裁判。

**D2 — 逐轮在环评估(per-turn in-loop),而非终局守门**
每个 turn `agent` 回复后即调 evaluator 评估,结论喂回 simulator 决定下一步。目的是**当场引导改进**,而非仅在 `Task_Done` 后做一次采纳/拒收。
- 代价:单条 query 最多 `max_turn` 次 evaluator 执行,比"终局评一次"贵;离线跑批可接受(成本换质量)。

**D3 — simulator 仍拍板,evaluator 仅顾问**
最终 `Task_Done`/`Task_Failed`/继续 由 `user_simulator` 输出;evaluator 只提供证据化反馈。假阳性的根治机制是:evaluator 把"声称 vs 磁盘真相"的矛盾摆给 simulator,simulator 据此收紧判断。
- 备选:evaluator 硬否决、夺取判定权(原 D8)。**否决(已定)**:即便"声称与磁盘铁证矛盾",evaluator 也只给**强反馈**、不硬压 simulator;最终一律由 simulator 终审。理由:与"以 simulator 为对话主体"的现有结构一致,且不把判定权集中到单个裁判(更不脆弱)。

**D4 — Evaluator 无状态:每轮新 session + 显式投喂**
evaluator 每轮新开 session,由 harness 显式投喂:原始任务 + **历轮全文** + 上一轮 evaluator 反馈 + 本轮证据。历轮用全文(不压缩摘要):离线跑批可接受 token 成本,且全文无信息损失、最利于"进步感知"与可审计;若日后 token 成为瓶颈再引入摘要。
- 理由:"进步感知"靠"看到历轮"而非"记得自己说过啥";持久 session 唯一独有的是把**自己上轮判词**带进上下文 → 自我锚定、错误逐轮复利、token 无界膨胀,正是裁判最该避免的偏见。
- 新开 session ≠ 新建 agent,无 90s 重启代价。
- 备选:跨轮持久 session。**否决**:锚定偏见 + 不可审计 + token 膨胀。

**D5 — 证据采集在 harness 侧,以磁盘真相为准**
harness 负责取证:`tool_calls` 从 `ExecutionResult` 内存直取;文件内容经 `agents.files.get(被测 agentId, name)` 从被测 agent **真实磁盘**读取。MUST NOT 仅采信 `ExecutionResult.files` 自报载荷——"声称写了文件但磁盘上空/不存在"正是要拆穿的假阳性。
- 该路径在 SDK 实锤(`agents.files.*` 按 agentId 寻址),不依赖任何 agent 的工具越界。

**D6 — 混合投递:文本/轨迹拼提示词,文件推进 evaluator 工作区**
- 对话文本 + `tool_calls` 轨迹 → 拼进 evaluator 的 `execute()` 提示词(结构化、体量小、只需读)。
- 被测 agent 的生成文件 → 经 D5 取磁盘真相后,用 `agents.files.set(evaluator agentId, "_under_review/<name>", content)` 推进 evaluator **自己的工作区**(命名隔离),让其用自身工具就地核验(开文件、grep、跑校验)。
- 该做法把大/二进制产物挡在 prompt 外,且因写进 evaluator 自己工作区,绕开未知的跨工作区沙箱问题。
- 备选:全量拼提示词。**否决**:大产物爆 token、无法主动核验,evaluator 退化为无工具裁判。

**D7 — 反馈注入 simulator,并改其决策指引**
`system_prompt.md` 新增占位符 `{evaluator_feedback}`,`user_simulator.chat()` 增加 `evaluator_feedback` 入参并在 `_render` 注入;改 prompt 决策段,要求 simulator 在判 `Task_Done`/`Failed`/继续 时**必须参考**该第三方核验结果(尤其"声称与磁盘矛盾"时不得轻易放行)。

**D8 — 证据完整性标记**
凡经 `history_fallback` 恢复的 turn 标 `evidence_incomplete=True`。评估规则强制"证据缺失 ≠ 证据为负",避免把 harness 自身掉线丢证据冤枉成 agent 没做事(防假阴性)。

**D9 — 评估输出结构化 + 引证 + 落盘**
evaluator 输出结构化结果:任务完成度、改进点列表、不符合要求项、整体倾向,且关键判断 SHALL 引用轨迹具体语句/工具返回/文件内容作为引证(压制裁判自身幻觉)。每次评估追加一条 JSON 到独立评估日志,供离线复核与校准。

**D10 — Evaluator 模型:初期对齐 user_simulator**
evaluator 初期复用与 `user_simulator` **相同的模型**,不引入新模型档,降低标定面与配置复杂度。注意通道差异:`user_simulator` 走 OpenAI 兼容客户端,而 evaluator 是 OC agent,其模型经 `AgentConfig`/网关侧配置——实现时将 evaluator agent 的模型设为同名模型;若网关不支持则回退网关默认并记录告警。
- 备选:独立强模型 / 与被测 agent 不同家族以减同源盲点。**推迟**:待校准数据表明需要时再换。

## Risks / Trade-offs

- [裁判也是模型,可能误判 / 自身幻觉] → 强制结构化输出 + 引证;无状态避免错误复利;评估全量落盘留校准接口。
- [假阳性只被"软"拦截(simulator 可能不听劝)] → D7 强化 prompt 决策指引;保留"铁证矛盾硬否决"为 Open Question。
- [逐轮评估成本/延迟上升] → 离线跑批接受;evaluator 可配较省模型;`history_fallback`/超短回复可跳过评估(可选)。
- [证据不完整轨迹被误判] → D8 强制"缺失≠为负"。
- [新增 evaluator agent 触发 90s 网关重启] → 一次性启动代价;同 agent 跨 session 无此代价。
- [推进 evaluator 工作区的文件与其自身文件冲突] → 命名隔离到 `_under_review/`,每轮(或每 query)清理。

## Migration Plan

1. 先落 D5/D8 逐轮证据捕获(纯留存 + 磁盘真相取证,不改判定逻辑,向后兼容)。
2. 注册独立 evaluator agent(D1),打通 `execute()` 驱动与 D6 证据投递,先 **dry-run**:只产出并落盘评估、不回传 simulator,观测评估质量。
3. 接入反馈闭环(D7):`system_prompt.md` + `chat()` 改造,simulator 起用 evaluator 反馈(D2/D3)。
4. 评估结构化与引证(D9)定稿;校准日志字段补齐。
- 回滚:配置开关 `evaluator.enabled=false` 即退回"simulator 自判"旧行为。

## Open Questions

- `evidence_incomplete` 轨迹:evaluator 应输出"挂起/降级"而非直接判负——终态如何呈现给 simulator?
- agent 的 `thinking`(内心思考)要不要给 evaluator 看?利于判意图,但可能误导。
- 是否对 `history_fallback`/超短回复等明显无证据的 turn **跳过** evaluator 调用以省成本?

> 已定(2026-06-18):① 不设硬否决,evaluator 始终软反馈、simulator 终审(D3);② 历轮投喂用全文(D4);③ evaluator 模型初期对齐 user_simulator(D10)。
