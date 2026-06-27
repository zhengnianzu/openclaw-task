## ADDED Requirements

### Requirement: 逐轮捕获带证据的执行记录

系统 SHALL 在多轮对话执行过程中,为每个 turn 留存 agent 的执行证据:用户输入、agent 文本回复(`content`)、工具调用证据(`tool_calls`,含 `tool`/`input`/`output`/`duration_ms`)、`stop_reason`。系统 MUST NOT 在执行后只保留 `content` 文本而丢弃工具证据。

#### Scenario: 正常返回的 turn 捕获工具证据
- **WHEN** `agent.execute()` 正常返回且 `ExecutionResult` 含非空 `tool_calls`
- **THEN** 该 turn 的记录 SHALL 包含这些 `tool_calls` 的完整内容(含工具入参与返回值),而不仅是 `content` 文本

#### Scenario: 多轮对话按顺序累积
- **WHEN** 一个 query 经历多个 turn 的 agent⇄模拟用户往返
- **THEN** 系统 SHALL 按 turn 顺序保留每一轮的用户输入与 agent 证据,形成可供 evaluator 审阅的运行记录

### Requirement: 以磁盘真相校正文件证据

当需要核验 agent 生成的文件时,系统 SHALL 经网关方法 `agents.files.get(被测 agentId, name)` / `agents.files.list(被测 agentId)` 从**被测 agent 的真实工作区**读取文件,以此作为文件证据的事实来源。系统 MUST NOT 仅采信 `ExecutionResult.files` 的自报载荷。

#### Scenario: 声称生成文件以磁盘为准
- **WHEN** agent 在回复或 `files` 中声称生成了某文件
- **THEN** 系统 SHALL 通过 `agents.files.get(被测 agentId, name)` 核对该文件在被测 agent 工作区是否真实存在及其内容,并以该磁盘结果(而非自报)作为下游评估依据

#### Scenario: 跨 agent 取证不依赖工具沙箱
- **WHEN** 取证方(harness 或独立 evaluator agent)与被测 agent 不是同一个 agent
- **THEN** 系统 SHALL 通过按 `agentId` 寻址的网关文件方法读取被测 agent 文件,而不要求任一 agent 具备跨工作区的执行内工具权限

### Requirement: 标注证据完整性

系统 SHALL 为每个 turn 标注证据是否完整。当某 turn 的结果是经由 `history_fallback`(连接中断或空响应兜底)恢复、只含文本而无工具/文件证据时,系统 SHALL 将该 turn 标记为"证据不完整(`evidence_incomplete`)"。

#### Scenario: 兜底恢复的 turn 标记证据缺失
- **WHEN** 某 turn 的结果来自 `chat.history` 兜底路径,无 `tool_calls`
- **THEN** 该 turn SHALL 被标记为 `evidence_incomplete`,以便 evaluator 区分"agent 真没用工具"与"harness 丢了证据"

#### Scenario: 证据缺失不得被当作负面证据
- **WHEN** 某 turn 被标记为 `evidence_incomplete`
- **THEN** 下游 evaluator MUST NOT 仅因证据缺失而判定 agent 未达成任务(不得制造假阴性)
