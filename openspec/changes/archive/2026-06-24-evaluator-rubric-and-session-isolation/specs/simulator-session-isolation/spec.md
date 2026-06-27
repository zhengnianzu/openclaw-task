## ADDED Requirements

### Requirement: user_simulator 对话记忆按 session 隔离

系统 SHALL 使 `user_simulator` 的对话记忆边界与 agent 的 gateway session 边界一一对齐:同一 `session_name` 下的多个 query 复用同一份对话记忆,不同 `session_name` 之间的对话记忆 MUST 相互不可见。系统 MUST NOT 跨 session 把一个 session 累积的对话历史注入到另一个 session 的 `user_simulator` system prompt(`{conversation_history}`)中,从而杜绝跨会话信息泄露。

会话键控 SHALL 使用 query 上携带的逻辑 `session_name`,而非带 run 隔离后缀(如 `_RUN_ID`)的物理会话名。

#### Scenario: 同 session 续聊保留记忆
- **WHEN** 两个 query 携带相同的 `session_name` 且均启用 simulator
- **THEN** 第二个 query 的 `user_simulator` SHALL 能看到第一个 query 累积的对话历史(合法续聊)

#### Scenario: 跨 session 不泄露
- **WHEN** 一个 query 在 session A 中得到了某项信息(如某城市名),随后另一个 query 在独立的 session B 中运行
- **THEN** session B 的 `user_simulator` system prompt 中的对话历史 MUST NOT 包含来自 session A 的任何对话内容

#### Scenario: 用逻辑 session_name 键控
- **WHEN** 系统为某 query 解析其 `user_simulator` 记忆归属
- **THEN** 系统 SHALL 以该 query 的逻辑 `session_name` 作为键,使共享同一逻辑会话的 query 命中同一份记忆,而 run 隔离后缀 MUST NOT 参与该键控

### Requirement: 隔离下暴露歧义而非靠泄露蒙混

当某 query 在独立 session 中运行、其 `user_simulator` 记忆为空,而该 query 文本含有依赖其他会话上下文的无解指代(如"这个城市")时,系统 SHALL 让模拟用户在缺失上下文的前提下据实应对(如重述需求、要求澄清,或在反复无法推进时按既有规则判 `Task_Failed`),而 MUST NOT 通过跨会话泄露得来的信息蒙混推进任务。

#### Scenario: 无解指代时据实应对
- **WHEN** 独立 session 的 query 含有依赖外部会话上下文的指代,且本 session 记忆中无相应信息
- **THEN** `user_simulator` SHALL 在不引用任何跨会话信息的前提下应对(重述/澄清/按规则判失败),MUST NOT 凭借来自其他 session 的内容补全该指代
