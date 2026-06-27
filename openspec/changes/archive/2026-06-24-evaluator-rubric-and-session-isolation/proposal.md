## Why

两个在 `config_session.log` 实跑中暴露的问题:

1. **evaluator 在无 rubric 时自拟 rubric**:当某 query 未传入冻结 rubric 时,结构化输出的 `rubric_checks` 本应为空,但模型把"评估维度"当成准则编进了 `rubric_checks`(任务 2/3 均如此),污染了离线评估日志的语义。`StructuredOutput` 只校验形式(字段/类型合法即放行),管不住这种"形式合法、语义错误"的幻觉。
2. **user_simulator 跨会话信息泄露**:整个 run 复用同一个 `User_simulator` 实例且从不 `reset()`,其 `self.messages` 作为 `{conversation_history}` 注入 system prompt 并跨 query 单调累积。结果是独立 agent session(如 `eval`)里的模拟用户"记得"另一个 session(`test`)的答案——任务 1 答出的"上海"泄露进了任务 2,掩盖了"这个城市"在隔离会话中本无指代的事实。

## What Changes

- **evaluator 无 rubric 时 `rubric_checks` 必须为空**:
  - 在 evaluator prompt 正文中,于"无冻结 rubric"分支显式声明 `rubric_checks` 必须返回空数组、不得自拟准则(从 schema 注释提升为正文祈使句)。
  - 解析后增加确定性保底:`if not rubric: result.rubric_checks = []`,放在落盘日志之前,保证日志干净。该保底只做归一,不拒绝/不重试/不判负。
- **user_simulator 记忆按 `session_name` 隔离**:
  - 由全局单例改为按 `query.session_name` 键控的多实例(裸 `session_name`,不带 `_RUN_ID` 后缀),使"模拟用户记忆边界"与"agent gateway session 边界"一一对齐。
  - 共享同一 session 的 query(如 `test`)合法续聊、保留历史;独立 session(如 `eval`)互不可见,不再泄露。
  - 设计取向:**宁可暴露歧义,也不靠泄露蒙混**——隔离后独立会话若遇到无指代的 query,应诚实卡住/重述/判失败,而非用泄露信息蒙混通过。

## Capabilities

### New Capabilities
- `simulator-session-isolation`: user_simulator 的对话记忆按 agent session 隔离,跨 session 不可见,杜绝跨会话信息泄露。

### Modified Capabilities
- `trajectory-evaluation`: 收紧结构化输出的 rubric 边界——无冻结 rubric 时 `rubric_checks` 必须为空(prompt 显式声明 + 解析后确定性保底)。

## Impact

- `evaluator.py`:`_build_prompt`(无 rubric 分支追加声明)、`evaluate_turn`(解析后保底 `rubric_checks=[]`,置于 `_log` 之前)。
- `openclaw_automation.py`:query 主循环将全局 `simulator` 改为按 `session_name` 键控的实例缓存。
- `user_simulator.py`:基本不动(隔离靠分实例,而非 `reset()`)。
- 行为变化(已接受):任务 2(`eval`)不再答"上海",会因历史为空 + query 含无解指代而卡住/重述/判 `Task_Failed`——属预期暴露,非回归。
