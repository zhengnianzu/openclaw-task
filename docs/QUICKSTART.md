# OpenClaw 自动化任务系统 - 快速开始

## 5 分钟快速入门

### 1. 环境准备

```bash
# 确保 Python 3.11+ 已安装
python --version

# 安装依赖
pip install openclaw-sdk pydantic

# 可选：YAML 支持
pip install pyyaml
```

### 2. 启动 OpenClaw

确保 OpenClaw 实例正在运行：

```bash
# 检查 OpenClaw 是否运行
curl http://127.0.0.1:18789/health

# 或检查进程
ps aux | grep openclaw
```

如果没有运行，启动 OpenClaw：

```bash
# 根据您的安装方式启动
openclaw start
# 或
docker-compose up -d
```

### 3. 创建第一个配置

创建 `my_first_config.json`：

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
      "text": "Hello! Please introduce yourself and tell me what you can help with."
    }
  ]
}
```

### 4. 运行任务

```bash
python openclaw_automation.py my_first_config.json
```

### 5. 查看结果

执行完成后，查看：
- 终端输出：实时执行状态
- `execution_report.txt`：详细执行报告
- `workspaces/assistant/`：Agent 工作空间

---

## 常见使用场景

### 场景 1: 简单问答

```json
{
  "agents": [
    {"name": "qa_bot", "system_prompt": "You answer questions accurately."}
  ],
  "queries": [
    {"agent_name": "qa_bot", "text": "What is machine learning?"},
    {"agent_name": "qa_bot", "text": "Give me 3 examples of machine learning applications"}
  ]
}
```

### 场景 2: 内容创作

```json
{
  "agents": [
    {"name": "researcher", "system_prompt": "You research topics thoroughly."},
    {"name": "writer", "system_prompt": "You write engaging articles."}
  ],
  "queries": [
    {"agent_name": "researcher", "text": "Research: The benefits of meditation"},
    {"agent_name": "writer", "text": "Write an article based on: {result_researcher}"}
  ]
}
```

### 场景 3: 代码审查

```json
{
  "agents": [
    {"name": "reviewer", "system_prompt": "You review code for bugs and improvements."}
  ],
  "queries": [
    {
      "agent_name": "reviewer",
      "text": "Review this Python function:\n\ndef calculate(a, b):\n    return a / b"
    }
  ]
}
```

---

## 文件结构

建议的项目结构：

```
my_project/
├── openclaw_automation.py    # 主程序
├── config.json               # 配置文件
├── workspaces/               # 工作空间（自动创建）
├── user_configs/             # 用户配置文件
│   ├── USER.md
│   └── SOUL.md
├── skills/                   # 技能目录
│   ├── research/
│   └── writing/
└── execution_report.txt      # 执行报告（自动生成）
```

---

## 下一步

### 进阶配置

1. **添加技能**

```json
{
  "input_dir": {
    "skill_dir": {
      "web_search": "./skills/web_search"
    }
  },
  "agents": [
    {
      "name": "researcher",
      "skills": ["web_search"]
    }
  ]
}
```

2. **使用配置文件**

创建 `user_configs/USER.md`：

```markdown
# User Configuration

Name: Your Name
Role: Content Creator
Preferences:
- Writing style: Professional but friendly
- Language: English
```

然后在配置中引用：

```json
{
  "input_dir": {
    "user_dir": "./user_configs"
  },
  "agents": [
    {
      "name": "writer",
      "config": ["USER.md"]
    }
  ]
}
```

3. **结果传递**

```json
{
  "queries": [
    {"agent_name": "agent1", "text": "Task 1"},
    {"agent_name": "agent2", "text": "Continue from: {result_agent1}"},
    {"agent_name": "agent3", "text": "Combine: {result_agent1} and {result_agent2}"}
  ]
}
```

### 查看示例

```bash
# 查看所有示例
python examples.py

# 运行单个示例
python examples.py 1  # 简单使用
python examples.py 4  # 内容创作流水线
python examples.py 9  # 并行执行
```

### 阅读完整文档

参见 `DESIGN.md` 了解：
- 详细架构设计
- 配置字段说明
- 高级用法
- 故障排查
- 最佳实践

---

## 故障排查速查

### 问题：连接失败

```bash
# 检查 OpenClaw
curl http://127.0.0.1:18789/health

# 检查配置
cat config.json | grep gateway_ws_url
```

### 问题：Agent 不存在

```python
# 配置中添加 agent
{
  "agents": [
    {"name": "your_agent", "system_prompt": "..."}
  ]
}
```

### 问题：执行超时

```json
{
  "queries": [
    {
      "agent_name": "agent",
      "text": "query",
      "timeout": 600  // 增加超时时间
    }
  ]
}
```

---

## 获取帮助

- 查看示例：`python examples.py`
- 阅读文档：`DESIGN.md`
- 检查配置：确保 JSON 格式正确
- 启用调试：在代码中添加 `logging.DEBUG`

---

## 快速参考

### 最小配置

```json
{
  "agents": [{"name": "bot", "system_prompt": "You are helpful."}],
  "queries": [{"agent_name": "bot", "text": "Hello"}]
}
```

### 多 Agent 协作

```json
{
  "agents": [
    {"name": "agent1", "system_prompt": "..."},
    {"name": "agent2", "system_prompt": "..."}
  ],
  "queries": [
    {"agent_name": "agent1", "text": "Task 1"},
    {"agent_name": "agent2", "text": "Task 2: {result_agent1}"}
  ]
}
```

### 命令行使用

```bash
# 基本使用
python openclaw_automation.py config.json

# 指定工作空间
python openclaw_automation.py config.json --workspace ./my_workspaces

# Python 方式
python -c "import asyncio; from openclaw_automation import main; asyncio.run(main('config.json'))"
```

---

祝您使用愉快！ 🚀
