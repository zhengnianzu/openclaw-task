# 论文阅读配置说明

## 📁 目录结构

```
C:\Users\nianzu\Music\openclaw_task\
├── agents\                                    # Agent 源文件目录
│   └── paper_reader\                          # paper_reader agent 源文件
│       ├── SOUL.md                            # Agent 灵魂定义
│       └── USER.md                            # 用户信息
│
└── task1-read-article\                        # 任务数据目录
    ├── 单容水箱液位控制实验_报告参考文献.pdf
    └── 多功能过程控制实验平台用户手册.pdf

C:\Users\nianzu\code\
└── config_paper_reader.json                   # 配置文件

C:\Users\nianzu\.openclaw\
└── workspace-paper_reader\                    # Agent 工作空间（自动创建）
    ├── SOUL.md                                # 从 agents/ 复制
    ├── USER.md                                # 从 agents/ 复制
    ├── 单容水箱液位控制实验_报告参考文献.pdf  # 从 task1-read-article/ 复制
    └── 多功能过程控制实验平台用户手册.pdf     # 从 task1-read-article/ 复制
```

## 🔧 配置说明

### input_dir 配置

```json
{
  "input_dir": {
    "skill_dir": {},                                          // 技能目录（暂无）
    "agent_dir": "C:\\Users\\nianzu\\Music\\openclaw_task\\agents",      // Agent 源文件根目录
    "user_dir": "C:\\Users\\nianzu\\Music\\openclaw_task\\task1-read-article"  // 数据文件目录
  }
}
```

**说明**：
- `agent_dir`：包含各个 agent 的源文件，结构为 `agent_dir/<agent_name>/SOUL.md`
- `user_dir`：包含要分析的 PDF 文件等数据

### agents 配置

```json
{
  "agents": [
    {
      "name": "paper_reader",
      "config": ["SOUL.md", "USER.md"],   // 会从 agent_dir/paper_reader/ 复制
      "skills": [],
      "system_prompt": "...",
      "model": "claude-3-5-sonnet"
    }
  ]
}
```

### workspace_base 默认值

在 `openclaw_automation.py` 中：
```python
workspace_base: str = Field(r"C:\Users\nianzu\.openclaw\workspace", ...)
```

**工作空间规则**：
- 如果 `agent_name == "main"`：工作空间 = `workspace_base`
- 否则：工作空间 = `workspace_base-<agent_name>`

例如：
- `main` → `C:\Users\nianzu\.openclaw\workspace`
- `paper_reader` → `C:\Users\nianzu\.openclaw\workspace-paper_reader`

## 🚀 执行流程

### 1. 设置工作空间

```
📁 设置工作空间...
  ✓ 复制 Agent 配置: SOUL.md          (从 agents/paper_reader/)
  ✓ 复制 Agent 配置: USER.md          (从 agents/paper_reader/)
  ✓ 复制数据文件: 单容水箱液位控制实验_报告参考文献.pdf  (从 task1-read-article/)
  ✓ 复制数据文件: 多功能过程控制实验平台用户手册.pdf     (从 task1-read-article/)
```

### 2. 设置 Agents

```
📦 设置 Agent: paper_reader
  ✓ 创建新 Agent: paper_reader
  工作空间: C:\Users\nianzu\.openclaw\workspace-paper_reader
```

### 3. 执行查询

```
🚀 开始执行查询任务
📝 任务 1/1: paper_reader
   查询: 请阅读工作目录下的以下两份文档...
   ✓ 执行成功
   耗时: XXXXms
```

## 📝 运行命令

```bash
cd C:\Users\nianzu\code
python openclaw_automation.py config_paper_reader.json
```

或使用快速启动脚本：
```bash
python run_paper_analysis.py
```

## ✅ 检查清单

执行前确认：

- [ ] OpenClaw 正在运行
- [ ] Agent 源文件存在：
  - `C:\Users\nianzu\Music\openclaw_task\agents\paper_reader\SOUL.md`
  - `C:\Users\nianzu\Music\openclaw_task\agents\paper_reader\USER.md`
- [ ] 数据文件存在：
  - `C:\Users\nianzu\Music\openclaw_task\task1-read-article\*.pdf`
- [ ] 配置文件正确：
  - `C:\Users\nianzu\code\config_paper_reader.json`

## 💡 配置要点

### 1. agent_dir 必须指向包含 agent 子目录的根目录

```
✅ 正确：agent_dir = "C:\\...\\agents"
   结构：agents/paper_reader/SOUL.md

❌ 错误：agent_dir = "C:\\...\\agents\\paper_reader"
```

### 2. user_dir 包含要复制到工作空间的数据文件

所有文件会被复制到 agent 的工作空间，agent 可以直接访问。

### 3. 不需要在配置中指定 workspace

工作空间路径由系统自动计算：
- `workspace_base-<agent_name>`

## 🔍 验证工作空间

运行后检查：

```bash
ls "C:\Users\nianzu\.openclaw\workspace-paper_reader"
```

应该看到：
- SOUL.md
- USER.md
- 单容水箱液位控制实验_报告参考文献.pdf
- 多功能过程控制实验平台用户手册.pdf

---

**配置版本**: v1.0.4
**更新日期**: 2026-03-06
**主要改进**: 支持 agent_dir，正确的工作空间命名
