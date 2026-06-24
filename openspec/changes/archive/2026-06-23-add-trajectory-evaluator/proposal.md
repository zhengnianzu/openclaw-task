## Why

任务是否完成,目前完全由 `user_simulator` 自己吐 `【Task_Done】` 决定。它既是对话里的"用户"(运动员),又是判定成败的"裁判"——角色冲突导致两类系统性误判:

- **假阳性**:agent 其实没做好,但话术漂亮,把模拟用户哄过,产出被错误采纳。
- **假阴性**:模拟用户人设太较真,任务已完成却死活不给过,优质轨迹被白白丢弃。

更深的问题:当前 `user_simulator` 是**单轮 LLM、无工具**,只能看到 `result.content`(纯文本说辞),无从核验 agent 到底做没做。而经查证:`agent.execute()` 返回的 `ExecutionResult` 早已携带 `tool_calls`(含工具入参与返回值);网关方法 `agents.files.get/list` **按 `agentId` 寻址**,harness 可隔着网关直读**任意 agent** 工作区的真实磁盘文件。可核验证据近在手边,却被 `execute_queries` 丢弃。

## What Changes

- 新增**逐轮证据捕获**:在多轮对话循环中,把每个 turn 的证据(`tool_calls`、`stop_reason`、`content`)留存;文件证据经 `agents.files.get(被测 agentId)` 从被测 agent 真实工作区取**磁盘真相**,而非采信 `ExecutionResult.files` 自报。标记经 `history_fallback` 兜底、只剩文本的 turn 为"证据不完整"。
- 新增**独立 Evaluator(OC agent)**:新建一个**独立于执行任务 agent** 的 OC agent 作为裁判,**可使用工具主动核验** OC 操作结果。它在**每个 turn** agent 回复后出场,产出"任务完成度 / 改进点 / 不符合要求项 + 引证",**反馈给 `user_simulator`**。
- 改 `user_simulator`**接收反馈再决策**:`system_prompt.md` 新增占位符注入 evaluator 的核验结果,`chat()` 增加反馈入参。**判定权仍归 simulator**(`Task_Done`/引导下一轮/`Task_Failed`/超限退出),但其判断须参考 evaluator 的证据化反馈——以此根治话术型假阳性,并通过"评估→反馈→引导"闭环逐轮提升完成质量。
- evaluator **无状态**:每轮新开 session,由 harness 显式投喂(任务 + 历轮全文 + 上轮反馈 + 本轮证据),避免裁判自我锚定。
- 评估结论**结构化落盘**(仿 `api_use.log`),供离线复核与校准。

## Capabilities

### New Capabilities
- `trajectory-capture`: 在多轮对话执行中,逐轮留存 agent 的工具调用证据,并以网关直读的磁盘真相校正"生成文件"证据;标注证据完整性。
- `trajectory-evaluation`: 由独立、带工具、无状态的 evaluator(OC agent)在每轮对 agent 执行做基于真实证据的评估,产出可追溯的反馈给 `user_simulator`,驱动其逐轮引导与最终判定。

### Modified Capabilities
<!-- 暂无既有 spec,无既有能力的需求被修改。 -->

## Impact

- **代码**:`openclaw_automation.py` 的 `execute_queries`(逐轮证据捕获、磁盘真相取证、evaluator 接入、反馈回传 simulator);新增 evaluator 组件(独立 OC agent 的注册与驱动、评分/反馈 prompt、结构化输出解析);`user_simulator.chat()` 新增反馈入参;`system_prompt.md` 新增 evaluator 反馈占位符与决策指引。
- **配置**:`AutomationConfig` 新增 evaluator 配置(是否启用、evaluator agent 名/工作区、评估 prompt、是否落盘等)。
- **依赖**:复用 SDK `client.get_agent`/`agent.execute` 驱动 evaluator;复用 `gateway.agents_files_get/list/set` 跨 agent 取/推证据;裁判结构化输出可用 `openclaw_sdk.output.structured`。
- **数据/产物**:新增评估结论日志,供校准与离线复核。
- **成本**:每个 turn 多一次 evaluator agent 执行(逐轮在环,比"仅终局评一次"更贵,离线跑批可接受);新增 evaluator agent 注册会触发一次网关重启等待(约 90s)。
