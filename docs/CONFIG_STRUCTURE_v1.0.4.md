# 配置结构说明 v1.0.4

## 📁 目录结构

### 源文件
```
C:\Users\nianzu\Music\openclaw_task\
├── agents\                                    # Agent 源文件目录
│   └── paper_reader\
│       ├── SOUL.md
│       └── USER.md
│
└── task1-read-article\                        # 用户数据目录
    ├── 单容水箱液位控制实验_报告参考文献.pdf
    └── 多功能过程控制实验平台用户手册.pdf
```

### 工作空间（运行后自动创建）
```
C:\Users\nianzu\.openclaw\workspace-paper_reader\
├── SOUL.md                                    # 从 agents/paper_reader/ 复制
├── USER.md                                    # 从 agents/paper_reader/ 复制
└── task1-read-article\                        # 整个目录复制过来
    ├── 单容水箱液位控制实验_报告参考文献.pdf
    └── 多功能过程控制实验平台用户手册.pdf
```

## 🔧 关键改进

### user_dir 整体复制

**修改前**（逐个文件复制）：
```
workspace-paper_reader/
├── SOUL.md
├── USER.md
├── 单容水箱液位控制实验_报告参考文献.pdf  ← 文件直接在根目录
└── 多功能过程控制实验平台用户手册.pdf     ← 文件直接在根目录
```

**修改后**（目录整体复制）：
```
workspace-paper_reader/
├── SOUL.md
├── USER.md
└── task1-read-article/                       ← 保持目录结构
    ├── 单容水箱液位控制实验_报告参考文献.pdf
    └── 多功能过程控制实验平台用户手册.pdf
```

## 💡 优势

1. **保持目录结构**：user_dir 作为子目录存在，结构清晰
2. **避免文件冲突**：不同来源的文件不会混在一起
3. **便于管理**：可以有多个数据目录，互不干扰
4. **路径明确**：Agent 访问文件路径为 `task1-read-article/xxx.pdf`

## 📝 配置示例

```json
{
  "input_dir": {
    "agent_dir": "C:\\Users\\nianzu\\Music\\openclaw_task\\agents",
    "user_dir": "C:\\Users\\nianzu\\Music\\openclaw_task\\task1-read-article"
  },
  "agents": [
    {
      "name": "paper_reader",
      "config": ["SOUL.md", "USER.md"]
    }
  ],
  "queries": [
    {
      "agent_name": "paper_reader",
      "text": "请阅读 task1-read-article 目录下的文档..."
    }
  ]
}
```

## 🚀 执行输出

```
📁 设置工作空间...
  ✓ 复制 Agent 配置: SOUL.md
  ✓ 复制 Agent 配置: USER.md
  ✓ 复制用户目录: task1-read-article/ -> C:\Users\nianzu\.openclaw\workspace-paper_reader\task1-read-article
```

## ✅ 查询文本更新

需要在 query 中指定子目录路径：

```json
{
  "text": "请阅读 task1-read-article 目录下的以下两份文档：\n\n1. task1-read-article/单容水箱液位控制实验_报告参考文献.pdf\n2. task1-read-article/多功能过程控制实验平台用户手册.pdf"
}
```

或者：

```json
{
  "text": "请阅读工作目录的 task1-read-article 子目录中的所有 PDF 文件，并进行分析..."
}
```

---

**版本**: v1.0.4
**更新**: user_dir 整体复制到 workspace
**日期**: 2026-03-06
