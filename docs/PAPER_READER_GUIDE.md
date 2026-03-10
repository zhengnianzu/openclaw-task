# 论文阅读分析配置 - 使用指南

## 📚 配置概述

这是一个专门用于阅读和分析实验论文的 OpenClaw 自动化配置，特别适合高校学生进行学术文献研究。

---

## 📁 文件结构

```
C:\Users\nianzu\Music\openclaw_task\task1-read-article\
├── SOUL.md                                        # Agent 人格定义
├── USER.md                                        # 用户背景说明
├── 单容水箱液位控制实验_报告参考文献.pdf          # 待分析论文 1
└── 多功能过程控制实验平台用户手册.pdf             # 待分析论文 2

C:\Users\nianzu\code\
└── config_paper_reader.json                       # 自动化配置文件
```

---

## 🤖 Agent 配置

### paper_reader Agent

**专业定位**：学术论文分析助手

**核心能力**：
- 系统化解析论文内容
- 提取关键技术信息
- 提供批判性分析视角
- 建立知识关联和应用建议

**适用场景**：
- 实验论文阅读和分析
- 技术文档理解和总结
- 学术研究的文献调研
- 实验设计的理论学习

---

## 📝 SOUL.md 说明

定义了 Agent 的核心特质：

### 核心定位
- 学术研究的引导者
- 具备批判性思维
- 注重实用性和学术严谨性平衡
- 善于建立知识关联

### 专业能力
1. **论文阅读**：快速定位核心、理解方法论、评估价值
2. **内容分析**：结构化解析、技术细节提取、数据解读
3. **学术指导**：文献建议、术语解释、研究方向

### 分析框架
- 研究背景与目标
- 方法论与技术细节
- 数据结果与结论
- 学术价值与实践意义

---

## 👤 USER.md 说明

描述了用户背景和需求：

### 用户画像
- 身份：高校大学生
- 专业：控制工程、自动化
- 需求：阅读分析实验论文

### 当前任务
分析两份文档：
1. 单容水箱液位控制实验报告
2. 多功能过程控制实验平台用户手册

### 预期成果
- 理解核心概念
- 掌握实验方法
- 提升学术能力
- 建立知识联系

---

## 🎯 查询任务设计

配置中的查询任务包含四个部分：

### 第一部分：整体概述
- 文档性质和目的
- 主要内容概览
- 文档间的关联性

### 第二部分：实验论文分析
- 研究背景和目标
- 控制原理和方法
- 实验设计和流程
- 关键参数和指标
- 结果和结论
- 学术和实践价值

### 第三部分：技术平台分析
- 系统架构和功能
- 技术规格和参数
- 操作流程和注意事项
- 对实验的支持

### 第四部分：综合分析
- 内容互补和印证
- 设计优点和改进
- 学习和研究启发
- 延伸阅读建议

---

## 🚀 使用方法

### 前置准备

1. **确保 OpenClaw 运行**
   ```bash
   # 检查连接
   curl http://127.0.0.1:18789/health
   ```

2. **准备文档**
   - 确保两个 PDF 文件在指定目录
   - 路径：`C:\Users\nianzu\Music\openclaw_task\task1-read-article\`

3. **配置文件就绪**
   - SOUL.md ✅
   - USER.md ✅
   - config_paper_reader.json ✅

### 运行命令

```bash
cd C:\Users\nianzu\code

# 运行论文分析任务
python openclaw_automation.py config_paper_reader.json
```

### 预期执行流程

```
============================================================
🤖 OpenClaw 自动化任务系统
============================================================

📁 设置工作空间...
  ✓ 复制配置文件: SOUL.md
  ✓ 复制配置文件: USER.md

📦 设置 Agent: paper_reader
  ✓ 创建新 Agent: paper_reader

============================================================
🚀 开始执行查询任务
============================================================

📝 任务 1/1: paper_reader
   查询: 请仔细阅读以下两份文档...
   ✓ 执行成功
   耗时: XXXXms
   内容: [论文分析结果]

============================================================
📊 执行报告
============================================================

💾 报告已保存到: execution_report.txt
```

---

## 📄 输出结果

### 执行报告位置

```
C:\Users\nianzu\code\execution_report.txt
```

### 报告内容

包含完整的论文分析结果：
- 整体概述
- 实验论文详细分析
- 技术平台分析
- 综合学术见解

### 工作空间

```
C:\Users\nianzu\code\workspaces\paper_reader\
├── SOUL.md          # Agent 配置（已复制）
└── USER.md          # 用户信息（已复制）
```

---

## 🎓 使用建议

### 1. 首次使用

先熟悉配置文件的结构：
```bash
# 查看配置
cat config_paper_reader.json

# 查看 SOUL 定义
cat "C:\Users\nianzu\Music\openclaw_task\task1-read-article\SOUL.md"
```

### 2. 自定义查询

修改 `config_paper_reader.json` 中的 `queries` 部分：

```json
{
  "queries": [
    {
      "agent_name": "paper_reader",
      "text": "你的自定义问题...",
      "timeout": 600
    }
  ]
}
```

### 3. 多查询任务

可以添加多个查询进行深入分析：

```json
{
  "queries": [
    {
      "agent_name": "paper_reader",
      "text": "第一步：总体概述..."
    },
    {
      "agent_name": "paper_reader",
      "text": "第二步：深入分析实验方法..."
    },
    {
      "agent_name": "paper_reader",
      "text": "第三步：提出改进建议..."
    }
  ]
}
```

### 4. 调整 timeout

如果文档较长或分析较复杂，增加超时时间：

```json
{
  "timeout": 900  // 15 分钟
}
```

---

## 💡 高级用法

### 1. 结果传递

进行多轮分析：

```json
{
  "queries": [
    {
      "agent_name": "paper_reader",
      "text": "先总结两份文档的核心内容"
    },
    {
      "agent_name": "paper_reader",
      "text": "基于刚才的总结：{result_paper_reader}，请深入分析控制策略的优缺点"
    }
  ]
}
```

### 2. 不同视角分析

创建多个 Agent 从不同角度分析：

```json
{
  "agents": [
    {
      "name": "technical_analyst",
      "system_prompt": "从技术实现角度分析..."
    },
    {
      "name": "academic_reviewer",
      "system_prompt": "从学术价值角度评审..."
    }
  ]
}
```

### 3. 批量文档处理

处理多个论文：

```
task1-read-article/
task2-read-article/
task3-read-article/
```

为每个任务创建独立配置。

---

## 🔍 常见问题

### Q1: 如果 Agent 无法读取 PDF？

**解决方案**：
- 确保 PDF 文件路径正确
- 确保 OpenClaw 有读取文件的权限
- 尝试将 PDF 转换为文本后分析

### Q2: 分析结果太简略？

**解决方案**：
- 在查询中明确要求详细程度
- 增加分析维度的具体要求
- 分多个查询逐步深入

### Q3: 想保存分析笔记？

**解决方案**：
```bash
# 执行报告已自动保存
cat execution_report.txt

# 也可以重定向输出
python openclaw_automation.py config_paper_reader.json > analysis_log.txt
```

---

## 📊 配置参数说明

### Agent 配置

| 参数 | 值 | 说明 |
|------|---|------|
| `name` | `paper_reader` | Agent 标识符 |
| `config` | `["SOUL.md", "USER.md"]` | 配置文件列表 |
| `skills` | `[]` | 技能列表（当前为空） |
| `system_prompt` | 详见配置文件 | 系统提示词 |
| `model` | `claude-3-5-sonnet` | 使用的模型 |

### 查询配置

| 参数 | 值 | 说明 |
|------|---|------|
| `agent_name` | `paper_reader` | 执行的 Agent |
| `text` | 详见配置文件 | 查询文本 |
| `session_name` | `paper_analysis` | 会话名称 |
| `timeout` | `600` | 超时时间（秒）|

---

## 🎯 学习路径

### 阶段 1：基础使用
1. 运行预设配置
2. 查看分析报告
3. 理解输出结果

### 阶段 2：定制化
1. 修改查询问题
2. 调整分析重点
3. 优化输出格式

### 阶段 3：高级应用
1. 多 Agent 协作
2. 批量文档处理
3. 建立知识库

---

## ✅ 检查清单

执行前确认：

- [ ] OpenClaw 正在运行
- [ ] PDF 文件在指定位置
- [ ] SOUL.md 和 USER.md 已创建
- [ ] config_paper_reader.json 配置正确
- [ ] openclaw_automation.py 可用

---

**配置版本**: v1.0
**创建日期**: 2026-03-06
**适用对象**: 高校学生
**文档类型**: 实验论文

祝您学习愉快！📚
