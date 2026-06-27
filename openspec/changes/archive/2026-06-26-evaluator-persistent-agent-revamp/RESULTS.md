# 实验与验证结果(连真网关 gemini-3-flash-preview)

## 2.1 探测结论(scripts/probe_agents_update.py + ConfigManager 实连)

- OC 配置结构:`config.agents = {defaults, list}`;全局默认 `agents.defaults.model`,
  per-agent 覆盖 `agents.list[i].model`;provider 的 URL/key 在 `config.models.providers.<provider>`,
  模型串以 `"provider/model"` 引用。
- `agents.update(agentId, model=…)` 是**唯一可靠 per-agent 通道**:只认 `model`,
  但 `model` 可带 provider 前缀 `"provider/model"` → per-agent **选模型 + 选已定义 provider**。
  实测写入 `custom-yibuapi-com/gemini-3-flash-preview` 成功,evaluator 正常回话。
- **不可用**:`config.set/patch` 整份回写被本网关拒(`invalid config`);SDK 的
  `set_agent_model/get_agent_model` 假设 `agents` 为扁平字典,与本网关 `{defaults,list}` 不兼容。
  ⇒ 新 provider 的 baseUrl/apiKey **无法经 harness 下发,须网关侧 `models.providers` 配置**。
- `_pin_model` 据此实现为:组装 `provider/model` 经 agents.update 下发(灵活选模型+provider);
  `base_url/api_key` 字段仅信息性提示。原先发 `model+provider+apiKey` 会被整体拒绝,是真 bug,已修。
- 两 agent 模型隔离实证:`main/main3` 无覆盖 → 继承默认 `deepseek-v3`;`evaluator` 有覆盖 →
  `gemini-3-flash-preview`。**per-agent 覆盖正是"裁判刻意不同于 assistant"的实现**。

## 防锚定 / reset 验证(7.2)

| 步骤 | 结果 |
|---|---|
| turn1 记住暗号"紫罗兰-7" | "记住了" |
| turn2 暗号是?(不 reset) | "紫罗兰-7" ← 同会话确实持久化并回放(锚定风险 real) |
| reset → turn3 暗号是? | "我没有记忆功能…每次会话都是独立的" ← 已抹除(防锚定生效) |

## 反幻觉未退化(7.3,真网关 e2e)

构造一轮含真文件 `report.md` + 幻觉文件 `ghost.md`(声称生成但磁盘无):
- `ghost.md` → violation 拆穿 + rubric **fail**。
- 完成度 50 / 倾向 reject(不轻易放行)。

## 闭环集成(7.1)

- 全流程 `python openclaw_automation.py --config configs/config_session.json` 跑通(3 query 顺序完成)。
- 节流生效:日志 `[Evaluator] turn=1 未达评审点(eval_step=2),跳过并喂空`。
- 模型钉死生效:`已为 agent 'evaluator' 钉死模型名=gemini-3-flash-preview(agents.update 返回 {ok:True})`。
- eval_step=1 的复杂任务跑中,evaluator **在环内 turn1 触发并喂回 simulator**,simulator 仍拍板 Task_Done。
- **观察(印证 #3.1)**:capable agent + 接受型 simulator 对简单/单步任务**1 轮即完成**,
  evaluator 难以触发——故 eval_step 对比改用受控多轮轨迹隔离。

## 6.2 eval_step 对比(scripts/eval_step_sweep.py · 受控 6 轮轨迹 · 真 evaluator)

| eval_step | 触发轮 | 评估次数 | 总时延 | 均时延 | 均投喂chars | 完成度序列 | 倾向 |
|---|---|---|---|---|---|---|---|
| 1 | 1,2,3,4,5,6 | 6 | 79.1s | 13.2s | 1401 | 33,33,66,66,33,100 | reject×5 → accept |
| 2 | 2,4,6 | 3 | 41.9s | 14.0s | 1515 | 67,66,100 | reject,reject → accept |
| 3 | 3,6 | 2 | 25.0s | 12.5s | 1627 | 66,100 | reject → accept |

**结论:**
- **成本**:eval_step 是近线性的开销杠杆——评估次数 ≈ ⌈6/step⌉,总时延 79→42→25s(约 1/step)。
  单次投喂 chars 随窗口仅小幅增长(1401→1627,+16%),远不及次数下降带来的节省;
  总投喂量(次数×单次)≈ 8406→4545→3254,**step 每加 1 大致省一半 token**。
- **效果**:三种取值最终都正确收敛到 accept/100,且**都没有在幻觉轮(turn3 声称测试通过)提前放行**;
  差异在于**反馈密度**——step=1 每轮都给 simulator 反馈(6 次指导),step=3 仅 2 次、其余 4 轮喂空。
- **取舍建议**:对**需要密集纠偏**的复杂任务用小 step(1~2);对**长流程、看重省成本**的任务用大 step(3+),
  靠窗口=step 保证不漏中间轮证据。默认 **eval_step=2** 是成本/反馈密度的折中。

## 6.3 evaluator 专属 skill 是否值得(结论:建议做,有信号)

观察:e2e 中 flash evaluator 对真存在的 `report.md` 判了 `unverifiable`——它**没有主动调工具打开**
已推进到工作区的产物去核验内容,只据指针存在性保守判定。这正是"指针投喂"模式的软肋:
**核验深度依赖模型是否愿意用工具**。

→ 建议为 evaluator 配**专属 skill**,把"必须打开 `_under_review/` 下每个产物文件读其内容再判 rubric"
固化为流程,减少对 flash 模型自发工具调用的依赖,提升核验保真度。属后续增量,非本次必需。
