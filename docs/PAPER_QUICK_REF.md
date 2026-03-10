# 论文分析配置 - 快速参考

## 📋 文件清单

### 已创建的文件

✅ **配置文件**
- `C:\Users\nianzu\code\config_paper_reader.json`

✅ **Agent 定义**
- `C:\Users\nianzu\Music\openclaw_task\task1-read-article\SOUL.md`
- `C:\Users\nianzu\Music\openclaw_task\task1-read-article\USER.md`

✅ **文档**
- `C:\Users\nianzu\code\PAPER_READER_GUIDE.md` - 详细使用指南

✅ **快速启动**
- `C:\Users\nianzu\code\run_paper_analysis.py` - 一键运行脚本

---

## 🚀 快速开始（3 步）

### 方法 1：使用快速启动脚本

```bash
cd C:\Users\nianzu\code
python run_paper_analysis.py
```

### 方法 2：使用主程序

```bash
cd C:\Users\nianzu\code
python openclaw_automation.py config_paper_reader.json
```

### 方法 3：Python 代码

```python
import asyncio
from openclaw_automation import main

asyncio.run(main(config_file="config_paper_reader.json"))
```

---

## 📊 Agent 信息

| 项目 | 内容 |
|------|------|
| **名称** | paper_reader |
| **角色** | 学术论文分析助手 |
| **专长** | 实验论文阅读、技术文档分析、学术指导 |
| **模型** | claude-3-5-sonnet |
| **配置** | SOUL.md + USER.md |

---

## 📚 分析任务

### 输入文档（2 个）

1. **单容水箱液位控制实验_报告参考文献.pdf**
   - 类型：实验报告参考
   - 内容：液位控制理论和方法

2. **多功能过程控制实验平台用户手册.pdf**
   - 类型：设备手册
   - 内容：平台功能和操作

### 分析维度（4 部分）

1. **整体概述** - 文档性质、目的、关联
2. **实验分析** - 原理、方法、设计、结果
3. **平台分析** - 架构、规格、操作、支持
4. **综合分析** - 印证、优化、启发、延伸

---

## 📁 目录结构

```
C:\Users\nianzu\
├── code\
│   ├── openclaw_automation.py          # 主程序
│   ├── config_paper_reader.json        # 论文分析配置
│   ├── run_paper_analysis.py           # 快速启动脚本
│   ├── PAPER_READER_GUIDE.md           # 详细指南
│   └── execution_report.txt            # 输出报告（运行后生成）
│
└── Music\openclaw_task\task1-read-article\
    ├── SOUL.md                          # Agent 人格
    ├── USER.md                          # 用户背景
    ├── 单容水箱液位控制实验_报告参考文献.pdf
    └── 多功能过程控制实验平台用户手册.pdf
```

---

## ⚙️ 配置要点

### input_dir
```json
"input_dir": {
  "user_dir": "C:\\Users\\nianzu\\Music\\openclaw_task\\task1-read-article"
}
```
👉 SOUL.md 和 USER.md 会自动复制到工作空间

### agent
```json
{
  "name": "paper_reader",
  "config": ["SOUL.md", "USER.md"],
  "system_prompt": "专业的学术论文分析助手..."
}
```
👉 Agent 在首次运行时自动创建

### query
```json
{
  "agent_name": "paper_reader",
  "text": "请仔细阅读以下两份文档...",
  "timeout": 600
}
```
👉 600 秒（10 分钟）超时设置

---

## 🎯 预期输出

### execution_report.txt 包含

```
1. result_paper_reader:
   状态: 成功
   耗时: XXXXms
   内容预览:

   【第一部分：整体概述】
   ...

   【第二部分：单容水箱液位控制实验分析】
   ...

   【第三部分：实验平台技术分析】
   ...

   【第四部分：综合分析】
   ...
```

---

## 💡 使用技巧

### 1. 首次运行

```bash
# 检查 OpenClaw
curl http://127.0.0.1:18789/health

# 运行分析
python run_paper_analysis.py
```

### 2. 查看结果

```bash
# Windows
notepad execution_report.txt

# 或用编辑器打开
code execution_report.txt
```

### 3. 修改查询

编辑 `config_paper_reader.json`：
```json
{
  "queries": [
    {
      "text": "你的自定义问题..."
    }
  ]
}
```

### 4. 多次分析

```bash
# 第一次：总体分析
python run_paper_analysis.py

# 修改 query 后再次运行
# 第二次：深入某个方面
python run_paper_analysis.py
```

---

## 🔧 常见调整

### 增加分析深度

```json
{
  "timeout": 900,  // 增加到 15 分钟
  "text": "请详细分析，包括：\n1. ...\n2. ...\n3. ..."
}
```

### 聚焦特定内容

```json
{
  "text": "只分析 PID 控制策略的设计和参数调整方法"
}
```

### 分步分析

```json
{
  "queries": [
    {"text": "第一步：概述两份文档"},
    {"text": "第二步：分析实验方法"},
    {"text": "第三步：评估技术方案"}
  ]
}
```

---

## ❓ 故障排查

### 问题 1：连接失败

```bash
# 检查 OpenClaw
curl http://127.0.0.1:18789/health
```

### 问题 2：找不到文件

检查路径：
```bash
ls "C:\Users\nianzu\Music\openclaw_task\task1-read-article"
```

### 问题 3：分析不完整

增加 timeout 或简化查询。

---

## 📖 相关文档

- **PAPER_READER_GUIDE.md** - 完整使用指南
- **DESIGN.md** - 系统设计文档
- **README.md** - 项目总览

---

## ✅ 执行前检查

- [ ] OpenClaw 正在运行
- [ ] PDF 文件在正确位置
- [ ] 配置文件无误
- [ ] 已了解预期输出

---

**快速开始**: `python run_paper_analysis.py`

**配置位置**: `config_paper_reader.json`

**帮助文档**: `PAPER_READER_GUIDE.md`

Happy analyzing! 📚
