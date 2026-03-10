# OpenClaw 自动化任务系统 - 设计文档

## 目录

1. [系统概述](#系统概述)
2. [核心特性](#核心特性)
3. [架构设计](#架构设计)
4. [配置详解](#配置详解)
5. [使用指南](#使用指南)
6. [高级用法](#高级用法)
7. [最佳实践](#最佳实践)
8. [故障排查](#故障排查)

---

## 系统概述

OpenClaw 自动化任务系统是一个配置驱动的任务编排框架，基于 `openclaw-sdk` 构建，提供：

- **多 Agent 协作**：支持多个 AI Agent 协同工作
- **工作空间管理**：自动管理文件和配置
- **任务编排**：顺序执行查询，支持结果传递
- **技能管理**：自动安装和配置 Agent 技能
- **灵活配置**：JSON/YAML 配置文件驱动

### 适用场景

- 内容创作流水线（研究 → 写作 → 审校）
- 数据分析工作流（收集 → 分析 → 报告）
- 多步骤自动化任务
- Agent 协作场景

---

## 核心特性

### 1. 配置驱动

所有任务通过配置文件定义，无需修改代码：

```json
{
  "agents": [...],
  "queries": [...],
  "input_dir": {...}
}
```

### 2. 工作空间隔离

每个 Agent 拥有独立工作空间，避免文件冲突：

```
workspaces/
├── main/
│   ├── USER.md
│   ├── SOUL.md
│   └── research_skill/
└── writer/
    ├── USER.md
    ├── SOUL.md
    └── writing_skill/
```

### 3. 结果传递

后续任务可以引用前面任务的结果：

```json
{
  "agent_name": "writer",
  "text": "Based on: {result_main}, write an article"
}
```

### 4. 自动化报告

执行完成后自动生成详细报告：

```
📊 执行报告
====================================
1. result_main:
   状态: 成功
   耗时: 2340ms
   内容预览: Research findings...
```

---

## 架构设计

### 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                     配置层 (Config)                       │
│  - AutomationConfig                                      │
│  - SystemConfig / InputDirConfig / AgentConfigItem      │
│  - QueryItem                                            │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                  编排层 (Orchestration)                   │
│  - OpenClawAutomation (主控制器)                         │
│  - ConfigLoader (配置加载)                               │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
        ▼            ▼            ▼
┌──────────┐  ┌──────────┐  ┌──────────┐
│Workspace │  │  Agent   │  │  Query   │
│ Manager  │  │ Manager  │  │Orchestr. │
└──────────┘  └──────────┘  └──────────┘
      │            │            │
      └────────────┼────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────┐
│                  SDK 层 (openclaw-sdk)                    │
│  - OpenClawClient                                        │
│  - Agent                                                │
│  - Gateway (WebSocket/HTTP/Local)                       │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                OpenClaw Instance                         │
└─────────────────────────────────────────────────────────┘
```

### 核心组件详解

#### 1. ConfigLoader

**职责**：加载和验证配置

```python
config = ConfigLoader.load_from_file("config.json")
# 或
config = ConfigLoader.load_from_dict({...})
```

**支持格式**：
- JSON (.json)
- YAML (.yaml, .yml)
- Python 字典

#### 2. WorkspaceManager

**职责**：管理 Agent 工作空间

```python
workspace_mgr = WorkspaceManager("./workspaces")

# 创建工作空间
workspace = workspace_mgr.get_agent_workspace("main")

# 设置文件
workspace_mgr.setup_agent_files(
    agent_name="main",
    config_files=["USER.md", "SOUL.md"],
    skill_dirs={"skill1": "/path/to/skill1"},
    user_dir="/path/to/user/files"
)
```

**文件操作**：
- ✅ 复制配置文件到工作空间
- ✅ 复制技能目录
- ✅ 复制用户文件
- ✅ 自动创建目录结构

#### 3. AgentManager

**职责**：创建和管理 Agents

```python
agent_mgr = AgentManager(client, workspace_mgr)

# 设置 Agent
await agent_mgr.setup_agent(agent_config)

# 获取 Agent
agent = agent_mgr.get_agent("main")
```

**功能**：
- ✅ 创建新 Agent 或获取已存在的 Agent
- ✅ 自动安装技能
- ✅ 设置工作空间路径
- ✅ 配置系统提示词

#### 4. QueryOrchestrator

**职责**：编排和执行查询任务

```python
orchestrator = QueryOrchestrator(agent_mgr)

# 执行所有查询
results = await orchestrator.execute_queries(queries)

# 生成报告
orchestrator.generate_report("report.txt")
```

**特性**：
- ✅ 按顺序执行查询
- ✅ 变量替换 (`{result_xxx}`)
- ✅ 错误处理
- ✅ 结果收集

#### 5. OpenClawAutomation

**职责**：主控制器，协调所有组件

```python
automation = OpenClawAutomation(config)
results = await automation.run()
```

**执行流程**：
1. 连接 OpenClaw 网关
2. 设置工作空间
3. 创建/配置 Agents
4. 执行查询任务
5. 生成报告
6. 清理资源

---

## 配置详解

### 完整配置示例

```json
{
  "system": {
    "platform": ["windows", "linux"],
    "python": "3.11",
    "tools": ["git", "npm"]
  },
  "input_dir": {
    "skill_dir": {
      "research_skill": "./skills/research",
      "writing_skill": "./skills/writing",
      "editing_skill": "./skills/editing"
    },
    "user_dir": "./user_configs"
  },
  "agents": [
    {
      "name": "researcher",
      "config": ["USER.md", "SOUL.md", "CONTEXT.md"],
      "skills": ["research_skill", "web_search"],
      "system_prompt": "You are an expert researcher.",
      "model": "claude-3-5-sonnet"
    },
    {
      "name": "writer",
      "config": ["USER.md", "SOUL.md"],
      "skills": ["writing_skill"],
      "system_prompt": "You are a professional writer.",
      "model": "claude-3-5-sonnet"
    },
    {
      "name": "editor",
      "config": ["USER.md"],
      "skills": ["editing_skill"],
      "system_prompt": "You are an experienced editor.",
      "model": "claude-3-opus"
    }
  ],
  "queries": [
    {
      "agent_name": "researcher",
      "text": "Research AI developments in 2026",
      "session_name": "main",
      "timeout": 300
    },
    {
      "agent_name": "writer",
      "text": "Write an article based on: {result_researcher}",
      "session_name": "main",
      "timeout": 600
    },
    {
      "agent_name": "editor",
      "text": "Review and improve: {result_writer}",
      "session_name": "main",
      "timeout": 300
    }
  ],
  "gateway_ws_url": "ws://127.0.0.1:18789/gateway",
  "api_key": null,
  "workspace_base": "./workspaces"
}
```

### 配置字段说明

#### system (系统配置)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `platform` | `List[str]` | ❌ | 支持的平台，默认 `["windows", "linux"]` |
| `python` | `str` | ❌ | Python 版本，默认 `"3.11"` |
| `tools` | `List[str]` | ❌ | 所需工具列表 |

#### input_dir (输入目录配置)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skill_dir` | `Dict[str, str]` | ❌ | 技能目录映射 `{skill_name: path}` |
| `user_dir` | `str` | ❌ | 用户配置文件目录 |

#### agents (Agent 配置列表)

每个 Agent 配置项：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | `str` | ✅ | Agent 唯一标识符 |
| `config` | `List[str]` | ❌ | 配置文件列表（相对于 user_dir） |
| `skills` | `List[str]` | ❌ | 所需技能列表 |
| `system_prompt` | `str` | ❌ | 系统提示词 |
| `model` | `str` | ❌ | 使用的模型 |

#### queries (查询任务列表)

每个查询配置项：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `agent_name` | `str` | ✅ | 执行的 Agent 名称 |
| `text` | `str` | ✅ | 查询文本，支持变量替换 |
| `session_name` | `str` | ❌ | 会话名称，默认 `"main"` |
| `timeout` | `int` | ❌ | 超时时间（秒），默认 300 |

#### 连接配置

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `gateway_ws_url` | `str` | ❌ | WebSocket 网关 URL |
| `api_key` | `str` | ❌ | API 密钥 |
| `workspace_base` | `str` | ❌ | 工作空间基础目录，默认 `"./workspaces"` |

### 变量替换语法

在 `queries[].text` 中可以使用变量引用之前的执行结果：

```
{result_<agent_name>}
```

**示例**：

```json
{
  "queries": [
    {
      "agent_name": "researcher",
      "text": "Research topic X"
    },
    {
      "agent_name": "writer",
      "text": "Write about: {result_researcher}"
    },
    {
      "agent_name": "reviewer",
      "text": "Review: {result_writer} based on: {result_researcher}"
    }
  ]
}
```

---

## 使用指南

### 安装依赖

```bash
# 安装 openclaw-sdk
pip install openclaw-sdk

# 安装可选依赖
pip install pyyaml  # 如需 YAML 配置支持
```

### 基础使用

#### 1. 准备配置文件

创建 `config.json`：

```json
{
  "agents": [
    {
      "name": "assistant",
      "system_prompt": "You are a helpful assistant."
    }
  ],
  "queries": [
    {
      "agent_name": "assistant",
      "text": "Hello, introduce yourself"
    }
  ]
}
```

#### 2. 运行任务

**方式 1：命令行**

```bash
python openclaw_automation.py config.json
```

**方式 2：Python 脚本**

```python
import asyncio
from openclaw_automation import main

asyncio.run(main(config_file="config.json"))
```

**方式 3：代码中直接使用**

```python
import asyncio
from openclaw_automation import OpenClawAutomation, AutomationConfig

config = AutomationConfig(
    agents=[
        {"name": "assistant", "system_prompt": "You are helpful."}
    ],
    queries=[
        {"agent_name": "assistant", "text": "Hello"}
    ]
)

async def run():
    automation = OpenClawAutomation(config)
    results = await automation.run()
    return results

asyncio.run(run())
```

### 目录结构建议

```
project/
├── config.json              # 配置文件
├── openclaw_automation.py   # 主程序
├── workspaces/              # 工作空间（自动创建）
│   ├── agent1/
│   └── agent2/
├── user_configs/            # 用户配置文件
│   ├── USER.md
│   ├── SOUL.md
│   └── CONTEXT.md
├── skills/                  # 技能目录
│   ├── research/
│   ├── writing/
│   └── editing/
└── execution_report.txt     # 执行报告（自动生成）
```

---

## 高级用法

### 1. 多阶段内容创作流水线

```json
{
  "agents": [
    {
      "name": "planner",
      "system_prompt": "You create content outlines.",
      "skills": ["planning"]
    },
    {
      "name": "researcher",
      "system_prompt": "You gather information.",
      "skills": ["web_search", "research"]
    },
    {
      "name": "writer",
      "system_prompt": "You write engaging content.",
      "skills": ["writing"]
    },
    {
      "name": "editor",
      "system_prompt": "You refine and polish content.",
      "skills": ["editing"]
    }
  ],
  "queries": [
    {
      "agent_name": "planner",
      "text": "Create an outline for an article about: {topic}",
      "timeout": 180
    },
    {
      "agent_name": "researcher",
      "text": "Research the following outline: {result_planner}",
      "timeout": 300
    },
    {
      "agent_name": "writer",
      "text": "Write a full article based on:\nOutline: {result_planner}\nResearch: {result_researcher}",
      "timeout": 600
    },
    {
      "agent_name": "editor",
      "text": "Edit and improve this article: {result_writer}",
      "timeout": 300
    }
  ]
}
```

### 2. 数据分析工作流

```json
{
  "agents": [
    {
      "name": "collector",
      "system_prompt": "You collect and organize data.",
      "skills": ["data_collection"]
    },
    {
      "name": "analyst",
      "system_prompt": "You analyze data and find insights.",
      "skills": ["data_analysis"]
    },
    {
      "name": "reporter",
      "system_prompt": "You create clear reports.",
      "skills": ["reporting"]
    }
  ],
  "queries": [
    {
      "agent_name": "collector",
      "text": "Collect data about: {data_source}"
    },
    {
      "agent_name": "analyst",
      "text": "Analyze this data: {result_collector}"
    },
    {
      "agent_name": "reporter",
      "text": "Create a report on: {result_analyst}"
    }
  ]
}
```

### 3. 使用环境变量

```python
import os
from openclaw_automation import ConfigLoader, OpenClawAutomation

# 从环境变量获取连接信息
config = ConfigLoader.load_from_file("config.json")
config.gateway_ws_url = os.getenv("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:18789/gateway")
config.api_key = os.getenv("OPENCLAW_API_KEY")

automation = OpenClawAutomation(config)
await automation.run()
```

### 4. 自定义工作空间处理

```python
from openclaw_automation import WorkspaceManager

workspace_mgr = WorkspaceManager("./custom_workspaces")

# 自定义文件准备逻辑
workspace = workspace_mgr.get_agent_workspace("my_agent")

# 添加额外的文件
import shutil
shutil.copy("templates/prompt.txt", workspace / "prompt.txt")

# 动态生成配置
(workspace / "dynamic_config.json").write_text('{"key": "value"}')
```

### 5. 错误处理和重试

```python
from openclaw_automation import OpenClawAutomation
import asyncio

async def run_with_retry(config_file, max_retries=3):
    for attempt in range(max_retries):
        try:
            await main(config_file=config_file)
            print("✅ 执行成功")
            break
        except Exception as e:
            print(f"❌ 尝试 {attempt + 1} 失败: {e}")
            if attempt < max_retries - 1:
                print("⏳ 等待 10 秒后重试...")
                await asyncio.sleep(10)
            else:
                print("❌ 达到最大重试次数")
                raise

asyncio.run(run_with_retry("config.json"))
```

---

## 最佳实践

### 1. 配置管理

✅ **推荐做法**：

- 使用版本控制管理配置文件
- 敏感信息（如 API key）使用环境变量
- 为不同环境创建不同配置（dev/staging/prod）

```
configs/
├── config.dev.json
├── config.staging.json
└── config.prod.json
```

❌ **避免**：

- 硬编码敏感信息
- 所有环境共用一个配置

### 2. Agent 设计

✅ **推荐做法**：

- 每个 Agent 职责单一明确
- 使用描述性的 Agent 名称
- 提供清晰的系统提示词

```json
{
  "name": "technical_writer",
  "system_prompt": "You are a technical writer specializing in software documentation. Write clear, concise, and accurate technical content."
}
```

❌ **避免**：

- Agent 职责过于宽泛
- 使用模糊的名称（如 "agent1", "agent2"）

### 3. 查询编排

✅ **推荐做法**：

- 将复杂任务拆分为多个步骤
- 合理使用结果传递
- 设置适当的超时时间

```json
{
  "queries": [
    {"agent_name": "researcher", "text": "Research X", "timeout": 300},
    {"agent_name": "writer", "text": "Write about: {result_researcher}", "timeout": 600},
    {"agent_name": "editor", "text": "Edit: {result_writer}", "timeout": 300}
  ]
}
```

❌ **避免**：

- 单个查询包含过多任务
- 不合理的超时设置（过短或过长）

### 4. 工作空间组织

✅ **推荐做法**：

- 保持工作空间整洁
- 使用有意义的文件名
- 定期清理旧的工作空间

```python
# 清理脚本
import shutil
from pathlib import Path

def cleanup_old_workspaces(base_dir, keep_latest=5):
    workspaces = sorted(
        Path(base_dir).iterdir(),
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )
    for workspace in workspaces[keep_latest:]:
        shutil.rmtree(workspace)
```

❌ **避免**：

- 工作空间文件混乱
- 从不清理旧文件

### 5. 错误处理

✅ **推荐做法**：

- 捕获和记录异常
- 提供有意义的错误信息
- 实现优雅降级

```python
try:
    results = await automation.run()
except Exception as e:
    logging.error(f"Automation failed: {e}", exc_info=True)
    # 发送告警通知
    send_alert(f"Task failed: {e}")
```

❌ **避免**：

- 吞掉异常不处理
- 错误信息不明确

---

## 故障排查

### 常见问题

#### 1. 连接失败

**症状**：

```
❌ 连接到 OpenClaw 失败
```

**原因**：
- OpenClaw 实例未运行
- 网关 URL 配置错误
- 网络问题

**解决方案**：

```bash
# 检查 OpenClaw 是否运行
ps aux | grep openclaw

# 测试连接
curl http://127.0.0.1:18789/health

# 检查配置
cat config.json | grep gateway_ws_url
```

#### 2. Agent 创建失败

**症状**：

```
❌ 创建 Agent 失败: agent_id already exists
```

**原因**：
- Agent 已存在
- 权限问题
- 配置冲突

**解决方案**：

```python
# 先删除已存在的 Agent
try:
    await client.agents.delete("agent_name")
except:
    pass

# 然后创建新的
agent = await client.create_agent(config)
```

#### 3. 技能安装失败

**症状**：

```
⚠ 技能安装 API 不可用
```

**原因**：
- SDK 版本不支持
- 技能不存在
- 权限问题

**解决方案**：

```bash
# 手动安装技能
openclaw skills install skill_name

# 或在 OpenClaw 配置中预先配置技能
```

#### 4. 文件复制失败

**症状**：

```
⚠ 配置文件不存在: /path/to/file
```

**原因**：
- 路径配置错误
- 文件不存在
- 权限问题

**解决方案**：

```json
{
  "input_dir": {
    "user_dir": "./user_configs"  // 使用相对路径
  }
}
```

```bash
# 检查文件是否存在
ls -la ./user_configs/
```

#### 5. 查询执行超时

**症状**：

```
❌ 执行失败: Timeout
```

**原因**：
- 查询过于复杂
- 超时设置过短
- OpenClaw 响应慢

**解决方案**：

```json
{
  "queries": [
    {
      "agent_name": "agent",
      "text": "complex query",
      "timeout": 600  // 增加超时时间
    }
  ]
}
```

### 调试技巧

#### 1. 启用详细日志

```python
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

#### 2. 检查工作空间

```bash
# 查看工作空间内容
ls -la workspaces/agent_name/

# 检查配置文件
cat workspaces/agent_name/USER.md
```

#### 3. 测试单个 Agent

```python
# 单独测试 Agent 执行
async def test_single_agent():
    async with OpenClawClient.connect() as client:
        agent = client.get_agent("test_agent")
        result = await agent.execute("test query")
        print(result.content)
```

#### 4. 验证配置

```python
from openclaw_automation import ConfigLoader

# 加载并验证配置
config = ConfigLoader.load_from_file("config.json")
print(config.model_dump_json(indent=2))
```

### 性能优化

#### 1. 并行执行查询（高级）

```python
# 对于独立的查询，可以并行执行
async def execute_parallel(queries):
    tasks = []
    for query in queries:
        agent = agent_manager.get_agent(query.agent_name)
        task = agent.execute(query.text, timeout=query.timeout)
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results
```

#### 2. 使用缓存

```python
from openclaw_sdk.cache import InMemoryCache

# 启用响应缓存
cache = InMemoryCache(ttl=3600, max_size=100)
client = OpenClawClient.connect(cache=cache)
```

#### 3. 优化工作空间

```python
# 只复制必要的文件
workspace_mgr.setup_agent_files(
    agent_name="agent",
    config_files=["USER.md"],  # 只复制必要的配置
    skill_dirs={"essential_skill": "/path"},  # 只复制必要的技能
    user_dir=None  # 不复制整个用户目录
)
```

---

## 附录

### A. 完整示例项目

参见 `examples/` 目录下的完整示例项目。

### B. API 参考

参见 `openclaw_automation.py` 中的 docstring。

### C. 版本历史

- v1.0.0 - 初始版本
  - 基础配置加载
  - 多 Agent 支持
  - 查询编排
  - 工作空间管理

### D. 贡献指南

欢迎提交 Issue 和 Pull Request！

---

## 联系方式

- GitHub: https://github.com/openclaw/openclaw
- 文档: https://docs.openclaw.dev
- 社区: https://discord.gg/openclaw
