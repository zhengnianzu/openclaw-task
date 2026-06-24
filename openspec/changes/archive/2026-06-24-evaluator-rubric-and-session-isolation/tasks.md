## 1. Evaluator: 无 rubric 时 rubric_checks 置空

- [x] 1.1 在 `evaluator.py::_build_prompt` 的"无冻结 rubric"分支(rubric 为空时)追加正文祈使:声明 `rubric_checks` 必须返回空数组、不得自拟准则、不得把评估维度当准则
- [x] 1.2 在 `evaluator.py::evaluate_turn` 中,取得 `result` 之后、`self._log(...)` 之前,加入 `if not rubric: result.rubric_checks = []` 的确定性归一(不拒绝/不重试/不改其他字段)
- [x] 1.3 验证:新增离线单测 `test_no_rubric_normalizes_rubric_checks_empty`(伪造会幻觉的裁判,断言归一后 `rubric_checks == []` 且其他字段不变);落盘前归一保证日志同样为空。活网关下的 `evaluator_use.log` 端到端核验留待运行期(见 3.3)

## 2. Simulator: 按 session_name 隔离记忆

- [x] 2.1 在 `openclaw_automation.py` query 主循环外,将全局 `simulator` 改为 `simulators: Dict[str, User_simulator]` 缓存;`execute_queries` 改收 `simulator_factory`,调用点传 `lambda: create_simulator(self.config)`
- [x] 2.2 在循环内以裸 `query.session_name`(经 `base_session`)为键取/建 simulator 实例(首见即建、再见复用);`use_simulator=False` 或工厂返回 None 时不取实例
- [x] 2.3 对取到的实例调用 `update_origin_query(query_text)` 后再进入多轮对话与 `chat(...)`
- [x] 2.4 隔离仅靠分实例,全程未调用 `User_simulator.reset()`(其 API 保留不动)

## 3. 回归验证(以 configs/config_session.json 为场景)

> 需活的网关(`ws://127.0.0.1:18789`)+ LLM API;命令:`python openclaw_automation.py configs/config_session.json`(输出见 `logs/config_session.log`、`evaluator_use.log`)。当前实现环境无网关,以下留待用户在联机环境核验。代码逻辑已由 `python -m py_compile` 与 `test/test_evaluator.py` 离线覆盖(归一保底)。

- [ ] 3.1 跑 test→eval→test 三任务,确认任务 2(`eval`)的 simulator 不再出现"上海",而是据实重述/澄清/或判 `Task_Failed`
- [ ] 3.2 确认任务 3(同 `test`)仍能合法续聊、直接答出上海人口
- [ ] 3.3 确认无 rubric 的任务(2、3)在 `evaluator_use.log` 中 `rubric_checks` 为空;有 rubric 的任务(1)逐条质检结果完整
