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

#### user_dir 对象形式与 `user_workspace`

`user_dir` 除字符串外还可写成对象，支持以下字段：

```json
{
  "input_dir": {
    "user_dir": {
      "path": "C:\\Users\\nianzu\\Music\\openclaw_task\\task1-read-article",
      "user_workspace": "ws"        // 可选：内容所在子目录名
    }
  }
}
```

真正被复制进 workspace 的内容取自 **content_root**，由 `user_workspace` 决定（仅接受子目录名，不支持相对多级/绝对路径）：

| `user_workspace` | content_root | 说明 |
|---|---|---|
| 不写（缺省） | `path/<与 path 同名的子目录>` | 旧行为，向后兼容 |
| `"ws"`（子目录名） | `path/ws` | 显式指定，避免冗长同名层 |
| `""`（空字符串） | `path` 本身 | 内容直接在 path 根下，拍平嵌套 |

> 注意：`user_workspace: ""`（空串）与「不写」语义不同——空串表示 content_root 就是 `path`。
> 另：`evaluate` 的 `oracle_ref` / `rubrics_ref` / `scoring_ref` 始终相对 `user_dir.path`（父层）解析，不受 `user_workspace` 影响。

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

### evaluator 配置（第三方裁判，可选）

引入一个**独立于执行 agent** 的 OC agent 作为裁判，在每个 turn 的 agent 回复后，
基于**可核验证据**（`tool_calls` + 经 `agents.files.get` 读取的磁盘真相文件）评估，
并把"完成度/改进点/不符合项 + 引证"反馈给 `user_simulator`（由 simulator 拍板）。

```json
{
  "evaluator": {
    "enabled": false,            // 默认关闭 → 退回 simulator 自判旧行为（回滚开关）
    "agent_name": "evaluator",   // 独立 OC agent 名，必须 ≠ 任一执行 agent
    "model": null,               // 评估模型；初期对齐 user_simulator。见下方说明
    "prompt_file": null,         // 评估 system prompt 模板路径；null=内置模板
    "feedback_to_simulator": false,  // false(默认)=只评估并落盘、不回流 simulator（先观测质量）；true=反馈回流
    "log_evaluations": true,     // 是否把每次评估写入 evaluator_use.log
    "review_subdir": "_under_review"  // 被审查产物推进 evaluator 工作区的子目录
  }
}
```

**要点**：
- `enabled=false` 是回滚开关：关闭后行为与旧版完全一致（仍逐轮捕获 `tool_calls`，但不取磁盘真相、不调裁判）。
- 首次创建 evaluator agent 会触发一次网关重启等待（约 90s），与创建任何新 agent 一致。
- `model`：SDK 的 `agents.create` 只下发 `name/workspace`，**不下发模型**，故 evaluator 实际使用网关默认模型；配置该字段仅记录意图并打印告警（待后续网关支持）。
- `feedback_to_simulator=false`（默认）时，评估只落盘到 `evaluator_use.log`、不影响 simulator 判定；确认评估质量后再切 `true` 起用反馈闭环。（原 `dry_run` 字段已改名,语义相反:`dry_run=true` ≡ `feedback_to_simulator=false`。）

### queries[].rubric（验收清单，可选）

可在任一 query 上携带 `rubric`（字符串数组）作为该任务的固定验收准则；它随 query 传入并在整段多轮对话中**冻结**，由 evaluator 逐条质检 agent 产物（每条产出 pass/fail/partial/unverifiable + 引证）。

```json
{
  "queries": [
    {
      "agent_name": "main3",
      "text": "帮我订一张去北京的往返机票",
      "rubric": [
        "机票为往返程",
        "出发地与目的地正确",
        "已给出订单确认号"
      ]
    }
  ]
}
```

**要点**：
- 边界 X：rubric 原文**只作用于 evaluator**，`user_simulator` 不感知 rubric；evaluator 按 rubric 打分后的提炼反馈（未满足项/改进点）仍回流，逐条 rubric 结果只进 `evaluator_use.log`。
- `unverifiable` 与 `evidence_incomplete` 同源：核验受阻**不判负**，避免冤枉掉线的 harness。
- rubric 为可选；缺省（空数组）即退回原有自由维度评估，不影响任务推进。

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

content_root 下的所有文件会被复制到 agent 的工作空间，agent 可以直接访问。
默认 content_root 为 `path/<同名子目录>`；可用 `user_workspace` 覆盖（见上文「user_dir 对象形式」）。

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
