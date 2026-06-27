## 1. 配置模型重构(#2)

- [x] 1.1 新增 `EvaluateConfig`(per-query):字段 `agent_name/session_name/rubrics/eval_step/feedback_to_simulator`,并校验 `eval_step >= 1`
- [x] 1.2 `QueryItem` 新增 `evaluate: Optional[EvaluateConfig]`,真正解析 `config_session.json` 里 query 内联的 `evaluate` 块(此前被静默忽略)
- [x] 1.3 废弃全局 `AutomationConfig.eval_config`(BREAKING);把原 `EvaluatorConfig` 中仍需的装配级项(model/provider/api_key/prompt_file/log/review_subdir)归并到 agent 声明或 `evaluate` 块
- [x] 1.4 `rubric` 从 `QueryItem` 顶层迁入 `evaluate.rubrics`,保持"随 query 冻结"语义
- [x] 1.5 装配期校验:`evaluate.agent_name` 必须存在于顶层 `agents` 列表,且 `≠ query.agent_name`,否则报错
- [x] 1.6 更新 `configs/config_session.json` 示例:per-query `evaluate` 块 + evaluator agent 的 `model/modelProvider/apiKey`(URL 视 Q1 结论)

## 2. 钉死裁判模型(#1)

- [x] 2.1 探测确认:用一次 `agents.update` 验证网关是否接受 `model/modelProvider/apiKey`,以及是否支持 per-agent base_url(落实 design Q1)
- [x] 2.2 `_setup_evaluator_agent` 改为:`agents.create(name, workspace)` 后调用 `gateway.agents_update(agent_id, model=…, modelProvider=…, apiKey=…)` 下发固定 flash 模型
- [x] 2.3 下发后校验生效;缺关键连接信息或下发失败时**明确告警**,MUST NOT 静默退回被测模型
- [x] 2.4 移除/更新 `openclaw_automation.py:1003-1007` 处"create 不下发模型"的旧告警逻辑

## 3. 持久 agent + 每轮 reset 会话(#4)

- [x] 3.1 evaluator 改为同一 query 复用一个 agent 实体(跨 turn 不重建);会话键固定为 `agent:{agent_id}:{evaluate.session_name}`
- [x] 3.2 `evaluate_turn` 在投喂/评估**之前**调用 `gateway.sessions_reset(session_key)`,确保不回放自身上一轮判词
- [x] 3.3 确认顺序 `reset → _push_review_files(推进待审产物)→ StructuredOutput.execute`,并验证 reset 不清工作区文件
- [x] 3.4 移除 `_build_prompt` 中 `last_feedback`(上轮判词)回投;进步感知改由窗口内证据 delta 承载

## 4. 有界压缩投喂(#4.4 / #6)

- [x] 4.1 `trajectory.py` 新增"最近 X 轮压缩渲染"(含 `tool_calls`:工具名 + 关键入参 + 截断输出)
- [x] 4.2 产物改为指针:渲染/投喂 `generated_files{filename, workspace_path}`,删去文件全文内联;指针覆盖**累积全部产物**而非仅最近 X 轮
- [x] 4.3 `_build_prompt` 重写为:`origin_query + rubrics + 最近 X 轮(含 tool_calls)+ 产物指针`;删去 `render_full(全量历轮)`
- [x] 4.4 保证 tool_calls 为窗口必含项(代码层硬约束,呼应 spec 反幻觉底线)

## 5. 评审频率节流(#5)

- [x] 5.1 `process_turn`/主循环:每轮仍捕获 trajectory,但仅当 `turn % eval_step == 0` 触发 evaluator
- [x] 5.2 投喂窗口 X 取 `eval_step`(5.3),保证窗口覆盖两次评审间全部 turn
- [x] 5.3 被跳过的 turn:`evaluator_feedback` 喂空给 simulator(5.2)
- [x] 5.4 评估日志补记 `eval_step`、本次窗口轮次范围,便于离线复核

## 6. 复杂任务与实验支撑(#3)

- [x] 6.1 新增复杂、多轮的任务配置(多文件产物、需追问/纠错的多轮交互),替代当前过简任务
- [x] 6.2 在 `eval_step ∈ {1,3,5,…}` 下跑实验,记录评估效果、token 消耗、时延、轮数控制并对比
- [x] 6.3 评估"evaluator 专属独立 skill"是否值得(对比固化 skill vs 每轮拼 prompt 的稳定性/成本),给结论(落实 Q2)

## 7. 校验与文档

- [x] 7.1 端到端跑通:启用 evaluator 的 query 在 reset + 压缩投喂 + eval_step 节流下正常完成,simulator 仍拍板
- [x] 7.2 验证防锚定:连续评审点之间 evaluator 不受自身旧判词影响(reset 生效 + 投喂不含旧判词)
- [x] 7.3 验证反幻觉未退化:对"声称生成但磁盘无此文件"的用例,evaluator 仍能据指针+工具核验拆穿
- [x] 7.4 `openspec validate evaluator-persistent-agent-revamp --strict` 通过;更新 README/示例配置说明
