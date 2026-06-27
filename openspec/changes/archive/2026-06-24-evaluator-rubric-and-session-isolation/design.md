## Context

两处问题均在 `logs/config_session.log` 的实跑中暴露:

- `evaluator.py` 用 `StructuredOutput.execute` 取结构化输出。该机制是"schema 文本注入 + pydantic 校验 + 重试"的**软约束**,只保证字段形式合法,不约束语义。`_build_prompt` 在 rubric 为空时仅静默跳过验收清单段,模型遂把 `DEFAULT_EVAL_PROMPT` 里的"评估维度"当准则塞进 `rubric_checks`。
- `openclaw_automation.py` 的 query 主循环复用一个全局 `User_simulator`,每个 query 仅 `update_origin_query`、从不 `reset()`。`User_simulator.chat` 把 `self.messages` 渲染成 `{conversation_history}` 注入 system prompt,导致历史跨 query 单调累积、跨 session 泄露。

约束:`format_feedback` 故意不渲染 `rubric_checks`(evaluator.py "边界 X"),因此 rubric 幻觉只污染 `evaluator_use.log`,不流入 simulator 决策——这界定了问题 1 的爆炸半径,使"prompt 声明 + 轻量保底"足够,无需重试式校验。

## Goals / Non-Goals

**Goals:**
- 无冻结 rubric 时,evaluator 结构化输出与落盘日志中的 `rubric_checks` 恒为空。
- `user_simulator` 的记忆边界与 agent gateway session 边界一一对齐;跨 session 零泄露,同 session 合法续聊。

**Non-Goals:**
- 不引入 token 级受限解码,不改 `StructuredOutput`。
- 不为 rubric 幻觉加"判负/重试式校验";仅做归一保底。
- 不改 `user_simulator` 的对外 API(不依赖 `reset()` 做隔离)。
- 不修改 `config_session.json` 的 query 文本(歧义暴露是预期结果,是否改配置另议)。

## Decisions

### D1:无 rubric 时 `rubric_checks` 置空 —— prompt 声明 + 解析后保底(双层)

- **Prompt 层**:`_build_prompt` 在"无冻结 rubric"分支追加一句正文祈使:`rubric_checks` 必须返回空数组、不得自拟准则。把信号从 schema 注释提升到 prompt 正文(注释已被实测无视)。
- **代码层**:`evaluate_turn` 拿到 `result` 后、`self._log(...)` 之前执行 `if not rubric: result.rubric_checks = []`。确定性、不可被模型无视,保证日志干净。
- **为何双层而非单选**:prompt 层降低无效生成与误导;代码层提供硬保证。两者成本都极低。
- **为何不上重试式校验**:`rubric_checks` 不进 simulator(边界 X),最坏只是脏日志;重试式校验是过度工程。
- **放置点**:保底必须在 `_log` 之前,否则日志仍被污染;`Debug` 打印的 `format_feedback` 本就不含 `rubric_checks`,不受影响。

### D2:`user_simulator` 按 `session_name` 键控多实例

- 主循环将单个 `simulator` 改为 `simulators: dict[str, User_simulator]`,以 `query.session_name`(裸名,**不含** `_RUN_ID`)为键,首见即建、再见复用。
- **为何按 session 而非 per-query reset**:per-query reset 会丢掉同 session 的合法续聊(任务 3 同属 `test`,应记得"上海");按 session 键控同时满足"同 session 续聊"与"跨 session 隔离",与 agent 的 session 语义同构。
- **为何用裸 `session_name` 而非 `_RUN_ID` 全名**:`_RUN_ID` 是物理 run 隔离后缀,逻辑会话身份是 `session_name`;键控取逻辑身份更直观,且同一 run 内两者等价。
- **每个实例仍 `update_origin_query`**:同 session 不同 query 共享历史但各自有 origin_query。
- **构造开销**:每个 session 一个实例,构造仅读模板/profile,开销可忽略。

## Risks / Trade-offs

- [任务 2 行为变化] 隔离后 `eval` session 的 simulator 记忆为空,遇 "这个城市" 无解指代会卡住/重述/判 `Task_Failed` → 这是**预期暴露**而非回归;已在 proposal 中接受。若希望该 query 在隔离下可跑通,应改 query 文本而非恢复泄露。
- [模型仍可能生成多余 rubric_checks] → D1 代码层归一兜底,落盘恒为空;残余仅为一次性无效 token,无下游影响。
- [session 数量膨胀] 大量不同 session 会创建多实例 → 数量与 session 数同阶,内存可控;无需池化。

## Migration Plan

纯内部行为修正,无数据迁移、无外部接口变更。回滚=还原两处代码改动即可。建议以 `config_session.json`(三任务:test/eval/test)作为回归场景,核验:任务 2 不再出现"上海";任务 3 仍直接答上海;`evaluator_use.log` 中无 rubric 的任务其 `rubric_checks` 为空。

## Open Questions

- 无。(已决定:不修改 `config_session.json` 任务 2 的"这个城市";保留歧义暴露,隔离后该 query 在独立 session 卡住/重述/判失败属预期行为。)
