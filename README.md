# AI AGENT 自动化任务系统
一个强大的、配置驱动的自动化系统，用于编排和执行 AI Agent 任务。支持多 Agent 协作、工作空间管理、结果传递等功能。目前支持框架如下：
- OpenClaw
- Hermes

## OpenClaw 
> 基于 openclaw-sdk 的配置驱动任务编排框架
| `openclaw_automation.py` | OpenClaw 网关 | WebSocket → `openclaw-sdk` | 已有 OpenClaw 实例 (`ws://127.0.0.1:18789`) |
详见 `src/openclaw_client.py`。

## Hermes
> from run_agent import AIAgent
| `hermes_automation.py`   | 进程内 AIAgent | 直接 `from run_agent import AIAgent` | 想跑 hermes-agent、无网关、最少依赖 |
详见 `src/hermes_client.py`。

## ClaudeCode
> 基于 `claude_agent_sdk` 的进程内 harness — 直接复用官方 `ClaudeSDKClient` / `ClaudeAgentOptions` / `query()`等。
| `from claude_agent_sdk import ClaudeSDKClient` | 需要 `claude` CLI 已安装 (`claude --version`) |
详见 `src/claudecode_client.py`。

## 特性

- ✅ **配置驱动** - 通过 JSON/YAML 配置定义所有任务
- ✅ **多 Agent 协作** - 支持多个 Agent 顺序或并行工作
- ✅ **工作空间管理** - 自动管理文件和配置
- ✅ **结果传递** - 后续任务可以引用前面任务的结果
- ✅ **技能管理** - 自动安装和配置 Agent 技能
- ✅ **执行报告** - 自动生成详细的执行报告
- ✅ **类型安全** - 使用 Pydantic 进行配置验证

## 快速开始

### 1. 安装依赖

```bash
# 一份 requirements.txt 涵盖两个后端 (openclaw + hermes)
pip install -r requirements.txt
```

### 2. 确保 OpenClaw 运行

```bash
# 检查 OpenClaw 状态
curl http://127.0.0.1:18789/health
```

### 3. 创建配置文件

创建 `config.json`:

```json
{
  "agents": [
    {
      "name": "assistant",
      "system_prompt": "You are a helpful AI assistant."
    }
  ],
  "queries": [
    {
      "agent_name": "assistant",
      "text": "Hello! Please introduce yourself."
    }
  ]
}
```

### 4. 运行

```bash
# 同一份 config.json,两个后端任挑
python openclaw_automation.py --config configs/config_simple.json
python hermes_automation.py   --config configs/config_simple.json
```

## 文档

- **[快速开始](QUICKSTART.md)** - 5 分钟入门指南
- **[设计文档](DESIGN.md)** - 详细的架构和 API 文档
- **[示例代码](examples.py)** - 10 个实用示例

## 项目文件

| 文件 | 说明 |
|------|------|
| `openclaw_automation.py` | 主程序 - 核心自动化引擎 |
| `examples.py` | 示例集合 - 10 个使用示例 |
| `test_automation.py` | 测试脚本 - 验证系统功能 |
| `DESIGN.md` | 设计文档 - 完整的技术文档 |
| `QUICKSTART.md` | 快速开始 - 入门指南 |
| `README.md` | 本文件 - 项目概述 |

### 配置示例

| 文件 | 说明 |
|------|------|
| `example_config.json` | 完整示例 - 研究+写作流水线 |
| `config_simple.json` | 简单示例 - 基础问答 |
| `config_code_review.json` | 高级示例 - 代码审查流程 |

### Hermes 端文件 (一体化新增)

| 文件 | 说明 |
|------|------|
| `hermes_automation.py` | 进程内 AIAgent 前端，与 `openclaw_automation.py` 等价 |
| `hermes_utils/hermes_client.py` | `HermesClient` / `HermesAgent` 库封装 (lazy import `run_agent.AIAgent`) |
| `hermes_utils/__init__.py` | 独立包,跟 `utils/` 并列;避免跟 hermes-agent 顶层 `utils.py` 重名 |
| `docs/README_HERMES.md` | Hermes 后端的详细说明、配置示例、跨框架兼容字段 |
| `test/smoke_test.py` | Hermes 离线冒烟（imports / 配置校验 / 变量替换） |
| `test/test_hermes_client.py` | Hermes 端到端: 真实拉起 AIAgent 跑一条 query |

## 测试矩阵（3 Harness × 3 配置）

系统支持 3 种 harness 后端和 3 种配置模式的自由组合，共 9 种场景。核心配置文件为：

| 配置文件 | 模式 | `use_simulator` | `evaluate` |
|----------|------|:-:|:-:|
| `config_simple.json` | 单轮测试 | `false` | 无 |
| `config_user.json` | + User Simulator | `true` | 无 |
| `config_simple_eval.json` | + Evaluator | `true` | 有 |

### 3 × 3 矩阵

| | `config_simple.json` | `config_user.json` | `config_simple_eval.json` |
|---|---|---|---|
| **OpenClaw** | 单轮问答 | 多轮 + simulator | 多轮 + simulator + evaluator |
| **Hermes** | 单轮问答 | 多轮 + simulator | 多轮 + simulator + evaluator |
| **ClaudeCode** | 单轮问答 | 多轮 + simulator | 多轮 + simulator + evaluator |

### 运行方式

```bash
# OpenClaw（默认 harness，不需要 --harness 参数）
python harness_automation.py --config configs/config_simple.json
python harness_automation.py --config configs/config_user.json
python harness_automation.py --config configs/config_simple_eval.json

# Hermes — CLI --harness 覆盖 config 内的 harness_type
python harness_automation.py --harness hermes --config configs/config_simple.json
python harness_automation.py --harness hermes --config configs/config_user.json
python harness_automation.py --harness hermes --config configs/config_simple_eval.json

# ClaudeCode
python harness_automation.py --harness claudecode --config configs/config_simple.json
python harness_automation.py --harness claudecode --config configs/config_user.json
python harness_automation.py --harness claudecode --config configs/config_simple_eval.json
```

### 三种配置模式

| 模式 | 说明 |
|------|------|
| **单轮测试** | 单轮问答，无 simulator 无 evaluator |
| **+ User Simulator** | 多轮对话，simulator 扮演用户并仲裁 Task_Done/Failed |
| **+ Evaluator** | 多轮对话 + 第三方 evaluator 逐轮评估，反馈回流 simulator |

### 模型配置差异

| Harness | 模型路由 | 模型串格式 | `provider` 字段 |
|---|---|---|---|
| **OpenClaw** | 网关 `agents_update` | `provider/model`（如 `api-proxy-deepseek/deepseek-v4-flash`） | 必填 |
| **Hermes** | `AIAgent` 构造参数 | 裸模型名（如 `deepseek-v4-flash`） | 不需要 |
| **ClaudeCode** | 环境变量 `ANTHROPIC_MODEL` | 裸模型名 | 不需要 |

`user_proxy_model.json` 统一调配各 agent 的模型。OpenClaw 场景需要配 `provider` 字段拼接 `provider/model`，其他 harness 忽略该字段：

```json
{
  "evaluator": {
    "model": "deepseek-v4-flash",
    "provider": "api-proxy-deepseek",
    "base_url": "http://...",
    "api_key": "sk-..."
  }
}
```

## 使用示例

### 基础使用

```python
import asyncio
from openclaw_automation import main

# 从配置文件运行
asyncio.run(main(config_file="config.json"))
```

### 编程方式

```python
from openclaw_automation import OpenClawAutomation, AutomationConfig, AgentConfigItem, QueryItem

config = AutomationConfig(
    agents=[
        AgentConfigItem(
            name="writer",
            system_prompt="You are a professional writer."
        )
    ],
    queries=[
        QueryItem(
            agent_name="writer",
            text="Write a short story about AI"
        )
    ]
)

automation = OpenClawAutomation(config)
results = await automation.run()
```

### 多 Agent 协作

```json
{
  "agents": [
    {"name": "researcher", "system_prompt": "You research topics."},
    {"name": "writer", "system_prompt": "You write articles."},
    {"name": "editor", "system_prompt": "You edit content."}
  ],
  "queries": [
    {"agent_name": "researcher", "text": "Research: AI trends"},
    {"agent_name": "writer", "text": "Write article: {result_researcher}"},
    {"agent_name": "editor", "text": "Edit: {result_writer}"}
  ]
}
```

## 运行示例

```bash
# 查看所有示例
python examples.py

# 运行特定示例
python examples.py 1   # 简单使用
python examples.py 4   # 内容创作流水线
python examples.py 9   # 并行执行
```

## 运行测试

```bash
# 运行所有测试
python test_automation.py

# 运行特定测试
python test_automation.py "配置模型"
python test_automation.py "工作空间"
```

## 配置结构

### 完整配置示例

```json
{
  "system": {
    "platform": ["windows", "linux"],
    "python": "3.11",
    "tools": []
  },
  "input_dir": {
    "skill_dir": {
      "skill_name": "/path/to/skill"
    },
    "user_dir": "/path/to/user/configs"
  },
  "agents": [
    {
      "name": "agent_name",
      "config": ["USER.md", "SOUL.md"],
      "skills": ["skill_name"],
      "system_prompt": "System prompt text",
      "model": "claude-3-5-sonnet"
    }
  ],
  "queries": [
    {
      "agent_name": "agent_name",
      "text": "Query text with {result_other_agent} variables",
      "session_name": "main",
      "timeout": 300
    }
  ],
  "gateway_ws_url": "ws://127.0.0.1:18789/gateway",
  "api_key": null,
  "workspace_base": "./workspaces"
}
```

### 最小配置

```json
{
  "agents": [
    {"name": "bot", "system_prompt": "You are helpful."}
  ],
  "queries": [
    {"agent_name": "bot", "text": "Hello"}
  ]
}
```

## 核心组件

### ConfigLoader

加载和验证配置文件

```python
config = ConfigLoader.load_from_file("config.json")
config = ConfigLoader.load_from_dict({...})
```

### WorkspaceManager

管理 Agent 工作空间

```python
workspace_mgr = WorkspaceManager("./workspaces")
workspace = workspace_mgr.get_agent_workspace("agent_name")
```

### AgentManager

创建和管理 Agents

```python
agent_mgr = AgentManager(client, workspace_mgr)
await agent_mgr.setup_agent(agent_config)
agent = agent_mgr.get_agent("agent_name")
```

### QueryOrchestrator

编排查询执行

```python
orchestrator = QueryOrchestrator(agent_mgr)
results = await orchestrator.execute_queries(queries)
orchestrator.generate_report("report.txt")
```

### OpenClawAutomation

主控制器

```python
automation = OpenClawAutomation(config)
results = await automation.run()
```

### Evaluator(第三方裁判)

为某个 query 配置独立 evaluator，逐评审点基于**可核验证据**(tool_calls + 磁盘真相文件)评估
agent 表现，并把反馈喂回 user_simulator（simulator 仍拍板）。

#### 架构概览

```
execute_queries() 每轮循环
  |
  +-- agent.execute(query) --> agent_reply
  |
  +-- process_turn()
  |     +-- evaluator.evaluate_turn() --> EvaluationResult
  |           +-- Scorer.score(rubric_checks) --> completion (确定性算出，非模型自报)
  |           +-- format_feedback(result) --> evaluator_feedback (文本)
  |
  +-- simulator.chat(agent_reply, evaluator_feedback) --> 下一轮 user_query 或 Task_Done/Failed
```

**User Simulator** 和 **Evaluator** 的关键区别：

| | User Simulator | Evaluator |
|---|---|---|
| API 通道 | 自建 OpenAI client，独立于 harness | 走 harness 的 agent 通道（openclaw/hermes/claudecode） |
| 配置位置 | `simulator_config` JSON 的 `user_simulator` 段 | `agents[]` 声明 + `queries[].evaluate` 块 |
| 角色 | 最终仲裁者（拍板 Task_Done/Failed） | 顾问（软反馈，无硬否决权） |

#### 配置方式

在 query 上加 `evaluate` 块，并在顶层 `agents` 声明裁判 agent：

```json
{
  "agents": [
    { "name": "main" },
    { "name": "evaluator", "model": "deepseek-v4-flash" }
  ],
  "queries": [{
    "agent_name": "main",
    "text": "...",
    "evaluate": {
      "agent_name": "evaluator",
      "eval_step": 1,
      "to_simulator": true,
      "rubrics": ["准则1", "准则2"]
    }
  }]
}
```

#### evaluate 块字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `agent_name` | str | `"evaluator"` | 评估器 agent 名称（须在 agents 列表中声明，且不等于执行 agent） |
| `session_name` | str? | null | 评估器会话名；null 则复用 query 的 session_name |
| `eval_step` | int | `1` | 每 N 轮评估一次；同时也是投喂窗口大小 |
| `to_simulator` | bool | `false` | 是否将评估反馈回流给 simulator |
| `isolate_eval_files` | bool | `true` | 执行期间是否从磁盘隔离 oracle/rubrics 文件（防 agent 读到答案） |

#### Rubric 配置：两种方式

**方式一：内联字符串 `rubrics`**（简单场景，不依赖外部文件）

```json
"evaluate": {
  "agent_name": "evaluator",
  "rubrics": [
    "[gate] 回答包含核心概念",
    "[final] 给出了代码实现",
    "[per_turn] 使用中文回答"
  ]
}
```

内联 rubrics 会自动归一化：id 补为 R1/R2/...，evaluator 设为 `llm_judge`，所有条目按 `when=final` 处理（文本中的 `[gate]` 等标记仅作语义提示，不自动解析）。Scorer 将所有非 gate rubric 归入单桶等权。

**方式二：外部引用 `rubrics_ref` + `scoring_ref`**（精细控制，需要 `user_dir`）

```json
"input_dir": {
  "user_dir": { "path": "configs" }
},
"queries": [{
  "evaluate": {
    "agent_name": "evaluator",
    "rubrics_ref": "eval_rubrics.json#/rubrics",
    "scoring_ref": "eval_rubrics.json#/scoring",
    "oracle_ref": "oracle.json"
  }
}]
```

引用的 rubrics 文件示例（如 `eval_rubrics.json`）：

```json
{
  "rubrics": [
    {
      "id": "G1",
      "when": "gate",
      "evaluator": "llm_judge",
      "text": "回答包含核心概念"
    },
    {
      "id": "C1",
      "when": "final",
      "evaluator": "oracle_cmp",
      "text": "数值结果与标准答案一致",
      "formula": "|agent_value - oracle_value| <= 0.01",
      "gt_ref": "derived.mean_wage"
    },
    {
      "id": "PT1",
      "when": "per_turn",
      "evaluator": "llm_judge",
      "text": "使用中文回答"
    }
  ],
  "scoring": {
    "gate_zero": true,
    "weights": {
      "correctness": 0.7,
      "presentation": 0.3
    },
    "bucket_map": {
      "correctness": ["C1"],
      "presentation": ["PT1"]
    }
  }
}
```

**两种方式互斥**：`structured_rubrics`（来自 `rubrics_ref`）优先；为空才回退到内联 `rubrics`。

#### Rubric 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str | 唯一标识，如 G1、C1、PT1 |
| `when` | `gate` / `final` / `per_turn` | 评分角色（见下方说明） |
| `evaluator` | str | 判定方式：`llm_judge`（LLM 判断）/ `oracle_cmp`（与标准答案比对）/ `program`（公式判定） |
| `text` | str | 自然语言描述 |
| `formula` | str? | 半形式化判据（伪代码 DSL，供 evaluator 参考） |
| `gt_ref` | str? | oracle 中对应 ground-truth 字段的引用路径 |

#### `when` 的三种类型

| `when` | 含义 | 评分行为 |
|--------|------|---------|
| `gate` | 门禁 / 一票否决 | 任何一条 gate 判 0 -> **整体 completion 直接归零** |
| `final` | 最终加权项 | 归入对应 bucket，按桶内通过比例 x 权重计算得分 |
| `per_turn` | 每轮加权项 | 评分逻辑与 final 一致，语义上提示 evaluator 每轮都检查 |

#### Scoring 评分机制

评分公式：

```
completion = (所有gate的乘积) x SUM[ bucket权重 x (桶内通过数 / 桶内总数) ]
```

示例（3 gate + 4 桶）：

```
G1=1, G2=1, G3=0  -->  completion = 0（gate 一票否决）

G1=1, G2=1, G3=1 的情况下：
  correctness(w=0.6): C1=1, C2=1, C3=0  --> 0.6 x 2/3 = 0.4
  provenance(w=0.2):  P1=1              --> 0.2 x 1/1 = 0.2
  process(w=0.2):     PT1=1, PT2=0      --> 0.2 x 1/2 = 0.1
  --> completion = 0.4 + 0.2 + 0.1 = 0.7
```

空桶（`rubric_ids` 为空）不参与归一化。

#### oracle_ref（可选）

指向 ground-truth JSON 文件（相对于 `user_dir.path`），内容会注入 evaluator prompt 供 `oracle_cmp` 类 rubric 比对：

```json
{
  "derived": {
    "mean_wage": 8060.0,
    "r_edu_wage": 0.9483,
    "edu_wage_significant_at_05": true
  }
}
```

#### 配置示例

| 配置文件 | 场景 | rubric 方式 |
|----------|------|------------|
| `configs/config_simple_eval.json` | 快排算法题，快速验证 | `rubrics_ref` 引用 `simple_eval_rubrics.json` |
| `configs/config_evaluator.json` | 经济中心问答，内联 rubrics | 内联 `rubrics` 字符串 |
| `configs/config_eval.json` | 科研数据分析，完整评估 | `rubrics_ref` + `scoring_ref` + `oracle_ref` |

#### 测试

```bash
# Scorer 纯逻辑测试（不调 API）
python test/test_evaluator.py --mode scorer

# 端到端测试（调 evaluator 模型 API）
python test/test_evaluator_e2e.py --mock-reply good
python test/test_evaluator_e2e.py --mock-reply bad
```

#### 日志

每次评估落盘到 `logs/evaluator_use.log`（JSON lines），包含：
- rubric 逐条 0/1 结果、evidence 引证
- Scorer 算出的 completion / gate_status / bucket_scores
- 投喂窗口范围（window_turns）、prompt 字符数
- 轨迹落盘到 `logs/trajectories/{run_id}/{session_name}.json`

#### 设计要点

- **持久 agent + 每轮 reset 会话**：同一 query 各轮复用同一 evaluator agent，但每次评估前 reset 清空会话防判词自我锚定
- **有界压缩投喂**：每次只投 `origin_query + rubrics + 最近 eval_step 轮 + 产物指针`，不投全量历史
- **completion 由 Scorer 算出**（确定性聚合），不信任模型自报的 completion 值
- **文件隔离**：执行前删除 oracle/rubrics（防 agent 作弊），执行后还原

## 变量替换

在查询文本中使用 `{result_<agent_name>}` 引用之前的结果：

```json
{
  "queries": [
    {"agent_name": "agent1", "text": "Do task 1"},
    {"agent_name": "agent2", "text": "Continue from: {result_agent1}"},
    {"agent_name": "agent3", "text": "Combine: {result_agent1} and {result_agent2}"}
  ]
}
```

## 适用场景

### 内容创作

研究 → 写作 → 编辑 → SEO 优化

### 代码审查

代码分析 → 测试生成 → 文档编写

### 数据分析

数据收集 → 分析 → 报告生成

### 翻译本地化

翻译 → 审校 → 文化适配

### 自动化工作流

任意多步骤的 AI 任务编排

## 系统架构

```
┌─────────────────────────────────────────┐
│          配置文件 (JSON/YAML)            │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│         ConfigLoader (配置加载)          │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│    OpenClawAutomation (主控制器)        │
└────────────────┬────────────────────────┘
                 │
        ┌────────┼────────┐
        │        │        │
        ▼        ▼        ▼
  ┌─────────┬─────────┬─────────┐
  │Workspace│ Agent   │ Query   │
  │ Manager │ Manager │Orchestr.│
  └─────────┴─────────┴─────────┘
        │        │        │
        └────────┼────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│      openclaw-sdk (OpenClawClient)      │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│          OpenClaw Instance              │
└─────────────────────────────────────────┘
```

## 最佳实践

### 1. 配置管理

- ✅ 使用版本控制管理配置
- ✅ 敏感信息使用环境变量
- ✅ 为不同环境创建不同配置

### 2. Agent 设计

- ✅ 职责单一明确
- ✅ 使用描述性名称
- ✅ 提供清晰的系统提示词

### 3. 查询编排

- ✅ 拆分复杂任务
- ✅ 合理使用结果传递
- ✅ 设置适当超时

### 4. 工作空间

- ✅ 保持整洁
- ✅ 有意义的文件名
- ✅ 定期清理

### 5. 错误处理

- ✅ 捕获和记录异常
- ✅ 提供明确错误信息
- ✅ 实现优雅降级

## 故障排查

### 连接失败

```bash
# 检查 OpenClaw
curl http://127.0.0.1:18789/health

# 检查配置
cat config.json | grep gateway_ws_url
```

### Agent 创建失败

确保 Agent 名称唯一，或先删除已存在的 Agent。

### 技能安装失败

手动安装技能或检查 SDK 版本。

### 执行超时

增加 `timeout` 配置值。

## 环境要求

- Python 3.11+
- openclaw-sdk
- pydantic >= 2.0
- 运行中的 OpenClaw 实例

## 可选依赖

```bash
pip install pyyaml  # YAML 配置支持
```

## 开发

### 启用调试日志

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### 运行测试

```bash
python test_automation.py
```

### 贡献

欢迎提交 Issue 和 Pull Request！

## 性能优化

### 1. 并行执行

对独立任务使用并行执行：

```python
results = await asyncio.gather(*tasks)
```

### 2. 使用缓存

```python
from openclaw_sdk.cache import InMemoryCache
cache = InMemoryCache(ttl=3600)
```

### 3. 优化工作空间

只复制必要的文件和技能。

## 扩展开发

### 自定义组件

可以继承和扩展核心组件：

```python
class CustomWorkspaceManager(WorkspaceManager):
    def setup_agent_files(self, ...):
        # 自定义逻辑
        super().setup_agent_files(...)
```

### 添加钩子

在关键点添加自定义逻辑：

```python
class CustomOrchestrator(QueryOrchestrator):
    async def execute_queries(self, queries):
        # 执行前钩子
        await self.before_execution()

        results = await super().execute_queries(queries)

        # 执行后钩子
        await self.after_execution(results)

        return results
```

## 版本历史

- **v1.0.0** (2026-03-05)
  - 初始版本
  - 基础配置加载
  - 多 Agent 支持
  - 查询编排
  - 工作空间管理
  - 结果传递
  - 执行报告

## 许可证

MIT License

## 致谢

基于 [openclaw-sdk](https://github.com/masteryodaa/openclaw-sdk) 构建。

## 链接

- OpenClaw: https://github.com/openclaw
- openclaw-sdk: https://github.com/masteryodaa/openclaw-sdk
- 文档: 查看 `DESIGN.md` 和 `QUICKSTART.md`

## 支持

如有问题，请：

1. 查看 `DESIGN.md` 中的故障排查部分
2. 运行 `python test_automation.py` 检查环境
3. 查看示例代码 `examples.py`
4. 提交 Issue

---

**祝您使用愉快！** 🚀
