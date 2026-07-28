# OpenClaw Task 目录结构

## 📁 目录说明

```
C:\Users\nianzu\Music\openclaw_task\
│
├── agents\                          # Agent 源文件目录
│   ├── paper_reader\                # 论文阅读 agent
│   │   ├── SOUL.md
│   │   └── USER.md
│   └── [其他 agent]/
│
├── configs\                         # 配置文件目录
│   ├── config_simple.json          # 简单示例配置
│   ├── config_paper_reader.json    # 论文阅读配置
│   ├── config_code_review.json     # 代码审查配置
│   ├── config_news.json            # 新闻阅读配置
│   └── example_config.json         # 完整示例配置
│
├── docs\                            # 文档目录
│   ├── README.md                    # 项目说明
│   ├── QUICKSTART.md               # 快速开始
│   ├── DESIGN.md                   # 详细设计文档
│   ├── CHANGELOG.md                # 更新日志
│   ├── PROJECT_SUMMARY.md          # 项目总结
│   ├── CONFIG_STRUCTURE.md         # 配置结构说明
│   ├── CONFIG_STRUCTURE_v1.0.4.md  # v1.0.4 配置说明
│   ├── FIX_SUMMARY.md              # 修复总结
│   ├── FIX_v1.0.2.md               # v1.0.2 修复
│   ├── FIX_v1.0.3.md               # v1.0.3 修复
│   ├── COMPLETE_FIX_SUMMARY.md     # 完整修复总结
│   ├── PAPER_READER_GUIDE.md       # 论文阅读指南
│   └── PAPER_QUICK_REF.md          # 论文阅读快速参考
│
├── logs\                            # 日志和输出目录
│   └── execution_report.txt        # 执行报告
│
├── task1-read-article\              # 任务1：论文阅读数据
│   ├── 单容水箱液位控制实验_报告参考文献.pdf
│   └── 多功能过程控制实验平台用户手册.pdf
│
├── task2-read-news\                 # 任务2：新闻阅读数据
│   └── [新闻文件]
│
├── openclaw_automation.py           # 主程序
├── examples.py                      # 示例代码
├── run_paper_analysis.py           # 快速启动脚本
├── test_automation.py              # 自动化测试
├── test_connect_fix.py             # 连接修复测试
└── test_resource_management.py     # 资源管理测试
```

## 📂 目录职责

### agents/
存放各个 agent 的源文件（SOUL.md, USER.md 等）

**结构**：`agents/<agent_name>/SOUL.md`

### configs/
存放所有配置文件

**命名**：`config_<任务名>.json`

### docs/
存放所有文档文件

**包含**：
- 设计文档
- 使用指南
- 修复说明
- 项目总结

### logs/
存放执行日志和输出文件

**包含**：
- execution_report.txt（执行报告）
- 其他日志文件

### task1-read-article/, task2-read-news/, ...
任务数据目录

**用途**：存放各个任务的输入数据（PDF、文本等）

## 🚀 快速使用

### 1. 查看文档
```bash
cd C:\Users\nianzu\Music\openclaw_task\docs
cat README.md          # 项目说明
cat QUICKSTART.md      # 快速开始
```

### 2. 查看配置
```bash
cd C:\Users\nianzu\Music\openclaw_task\configs
cat config_paper_reader.json  # 论文阅读配置
```

### 3. 运行任务
```bash
cd C:\Users\nianzu\Music\openclaw_task
python openclaw_automation.py configs/config_paper_reader.json
```

### 4. 查看结果
```bash
cat logs/execution_report.txt
```

## 📝 添加新任务

### 1. 创建 agent 源文件
```bash
mkdir agents/my_agent
# 创建 agents/my_agent/SOUL.md
# 创建 agents/my_agent/USER.md
```

### 2. 创建任务数据目录
```bash
mkdir task3-my-task
# 添加数据文件到 task3-my-task/
```

### 3. 创建配置文件
```bash
# 创建 configs/config_my_task.json
```

### 4. 运行
```bash
python openclaw_automation.py configs/config_my_task.json
```

## 🔧 维护

### 清理日志
```bash
rm logs/*.txt
```

### 备份配置
```bash
cp -r configs configs_backup_$(date +%Y%m%d)
```

### 更新文档
文档统一放在 `docs/` 目录，按类型组织。

---

**目录整理日期**: 2026-03-06
**版本**: v1.0.4
**维护**: 定期整理，保持结构清晰
