# OpenClaw 自动化任务系统

> 基于 openclaw-sdk 的配置驱动任务编排框架

一个强大的、配置驱动的自动化系统，用于编排和执行 OpenClaw AI Agent 任务。支持多 Agent 协作、工作空间管理、结果传递等功能。

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
pip install openclaw-sdk pydantic
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
python openclaw_automation.py config.json
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
