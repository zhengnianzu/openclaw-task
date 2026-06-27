## 1. 逐轮证据捕获(向后兼容,先行落地)

- [x] 1.1 新增轻量证据模型:`TurnRecord{user_input, agent_content, tool_calls, stop_reason, files, evidence_incomplete}`,在 query 内逐轮累积
- [x] 1.2 在 `execute_queries` 内层 turn 循环中,把每个 turn 的 `result.tool_calls`/`content`/`stop_reason` 留存进 `TurnRecord`(不再只取 `.content`)
- [x] 1.3 文件证据取磁盘真相:经 `gateway.agents_files_get/list(被测 agentId, ...)` 读取被测 agent 真实工作区文件,作为 `files` 证据来源(不采信 `ExecutionResult.files` 自报)
- [x] 1.4 标记证据完整性:凡经 `history_fallback` 恢复的 turn 置 `evidence_incomplete=True`
- [x] 1.5 单测:正常返回 turn 捕获到 `tool_calls`;声称的文件以磁盘核对为准;兜底 turn 被标 `evidence_incomplete`

## 2. 独立 evaluator agent 装配

- [x] 2.1 `AutomationConfig` 新增 evaluator 配置段(`enabled`、evaluator agent 名/工作区、模型连接[初期对齐 user_simulator 同名模型]、评估 prompt 路径、是否落盘)
- [x] 2.2 在 `_setup_agents` 注册独立 evaluator agent(独立工作区;校验其 agent 名 ≠ 任一执行 agent);留意首次创建触发约 90s 网关重启
- [x] 2.3 实现 evaluator 驱动:每轮**新开 session**调用 `evaluator_agent.execute(评估提示词)`,无状态

## 3. 证据投递与评估提示词

- [x] 3.1 投递(a):把原始任务 + 历轮全文 + 上一轮反馈 + 本轮 `tool_calls`/对话文本 拼进 evaluator 提示词
- [x] 3.2 投递(b):把被测产物经 `agents_files_set(evaluator agentId, "_under_review/<name>", content)` 推进 evaluator 自身工作区(命名隔离),供其用工具就地核验;每轮/每 query 清理
- [x] 3.3 编写评估 system prompt:输出任务完成度 / 改进点 / 不符合要求项 / 整体倾向;关键判断须**引证**(引轨迹语句、工具返回、文件内容)
- [x] 3.4 用 `openclaw_sdk.output.structured` 定义评估结构化输出并解析
- [x] 3.5 单测:话术型假阳性(声称 vs 磁盘矛盾)被反馈点名;`evidence_incomplete` 样本不被判负

## 4. 反馈闭环接入 simulator

- [x] 4.1 `system_prompt.md` 新增占位符 `{evaluator_feedback}`;`User_simulator._render`/`chat()` 增加 `evaluator_feedback` 入参并注入
- [x] 4.2 改 `system_prompt.md` 决策段:要求 simulator 判 `Task_Done`/`Failed`/继续 时参考 evaluator 反馈;证据矛盾时不轻易放行
- [x] 4.3 在 `execute_queries` 每轮:agent 回复 → 取证 → 调 evaluator → 把反馈传入 `simulator.chat(agent_reply, evaluator_feedback)`
- [x] 4.4 dry-run 开关:可只产出/落盘评估而不回传 simulator,用于先行观测评估质量

## 5. 评估落盘与可观测

- [x] 5.1 新增独立评估日志(仿 `api_use.log`,每行 JSON:轨迹标识/完成度/改进点/不符合项/倾向/引证/裁判模型)
- [x] 5.2 预留校准接口:日志字段足以离线复现评估依据,便于后续与人工标注比对一致率

## 6. 收尾验证

- [x] 6.1 端到端跑通一个含工具调用与文件产出的真实任务,确认"执行→取证→评估→反馈→引导"闭环与日志正确
  - 实测(deepseek-v3 执行 / gemini-3-flash-preview 模拟):闭环与 `evaluator_use.log` 均正确;evaluator 引证到磁盘真相 `openclaw_report.md: 存在 ✓ [工作区发现 size=197]` + 文件内容,判定建立在证据而非文本说辞上。
  - 取证修正:本网关(2026.2.26)`agents.files.list/get/set` **仅暴露脚手架白名单**,看不到用户新建文件;`capture_file_evidence` 改为经 `files.list` 取 workspace 路径后**直扫本地工作区目录**读盘(同机)。投递(b)推 `_under_review` 因 `files.set` 白名单不可用,投递(a)拼提示词仍送达内容。
- [x] 6.2 把 evaluator 从 dry-run 切换为实际回传 simulator(确认反馈质量后)
  - 实测 `dry_run=false`:evaluator"completion=100/accept + 磁盘已核验"反馈回传后,simulator 当轮(Turn1)即判 `Task_Done`;对比 dry_run 时其需追问、拖到 Turn2。反馈闭环生效,引导更高效。
- [x] 6.3 更新 `docs/`(CONFIG_STRUCTURE、DESIGN)说明 evaluator 流程与配置;回填回滚开关(`evaluator.enabled=false`)说明
