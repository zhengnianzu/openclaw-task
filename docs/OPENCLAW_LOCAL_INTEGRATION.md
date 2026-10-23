# OpenClaw Local 集成说明

本项目通过官方 OpenClaw CLI 的 `agent --local --json` 模式运行 Agent。`src/openclaw_local_client.py` 是 harness 与 OpenClaw 之间的 Python 适配层，不包含模型运行时；Agent、模型服务、凭据、工具、skills、workspace 和原生会话均由 OpenClaw 管理。

任务通过 `--harness openclaw-local` 选择该后端。本文以当前 `openclaw_local_client.py` 的实际行为为准，并说明它与 zhangchen 中 OpenClaw 2026.7.2 独立源码包的关系。

## 1. 架构与职责边界

一次调用的主要链路如下：

```text
harness_automation.py
  -> OpenclawLocalClient
  -> node <OPENCLAW_DIR>/openclaw.mjs agent --local --json ...
  -> OpenClaw Agent / 模型 / 工具
  -> payloads + meta
  -> ExecutionResult + 本轮工具调用证据
```

Python 适配层负责：

- 定位 `openclaw.mjs` 和 Node.js；
- 通过 `agents list --json` 读取已配置 Agent 的 ID 和 workspace；
- 将 harness 的 `(agent_name, session_name)` 映射为原生 session key；
- 在首轮把 harness 的 `system_prompt` 作为“任务约束”添加到用户消息前；
- 启动、超时终止并回收每轮 OpenClaw CLI 子进程；
- 校验原生 JSON 返回值并转换为统一的 `ExecutionResult`；
- 从 OpenClaw JSONL 或 SQLite transcript 中提取本轮工具调用，供 Evaluator 和轨迹统计使用；
- 对 CLI 错误文本做脱敏处理。

OpenClaw 负责：

- provider、模型和凭据配置；
- Agent、workspace、skills 和工具加载；
- 模型请求、工具循环、上下文和会话持久化；
- 原生 transcript、usage、stop reason 和最终回复。

适配器只接受模型选择覆盖。即使任务的 `simulator_config` 中为同名 Agent 设置了 `base_url`、`api_key` 或 `api`，这些连接字段也不会注入 OpenClaw，必须写入 OpenClaw 原生配置。

## 2. 版本与安装方式

### 2.1 当前适配器的兼容基线

当前客户端按 OpenClaw 2026.6.6 的 CLI 行为实现：任务消息通过 `--message <text>` 传入，没有使用 `--message-file`。建议部署时固定并记录实际 OpenClaw 版本，不要无验证地跟随 latest。

通过 npm 安装时：

```bash
npm install openclaw@2026.6.6
openclaw --version
openclaw onboard
```

适配器优先使用 `OPENCLAW_DIR`，否则从 `PATH` 查找 `openclaw`。如果 npm shim 不是 `openclaw.mjs`，适配器会按其所在目录推导 `node_modules/openclaw/openclaw.mjs`。

### 2.2 使用 `local_oc` 独立源码包

`D:\openclaw-task-codes\local_oc` 不是另一套 Python 客户端，也不是已安装的 OpenClaw 目录。它是 OpenClaw 的独立源码交付目录，核心文件为：

```text
D:\openclaw-task-codes\local_oc\
├── openclaw-standalone.tar.gz
├── openclaw-README.zh-CN.md
├── openclaw-requirements.md
└── openclaw_config_20261019_zh.md
```

源码包元数据：

| 项目 | 值 |
| --- | --- |
| OpenClaw 版本 | `2026.7.2` |
| Git commit | `2f967bc8f34685c73f532c4b6921b83a9ef09457` |
| 源码文件数 | 27,345 |
| 内容 | OpenClaw 核心、CLI、Gateway、UI、skills、插件、测试和文档 |
| 不包含 | `.git` 历史、`node_modules`、构建产物、个人配置、会话和密钥 |

该包面向 Linux/Bash。建议使用 Node.js 24.15.0 和 pnpm 11.2.2；源码声明的 Node.js 范围以包内 `openclaw-requirements.md` 和 `package.json` 为准。部署示例：

```bash
tar -xzf openclaw-standalone.tar.gz
cd openclaw-standalone
./install.sh
./run.sh onboard
./run.sh skills list --eligible
./run.sh skills check
```

构建完成后，`OPENCLAW_DIR` 必须指向直接包含 `openclaw.mjs` 的内层目录，而不是 `local_oc` 或 `openclaw-standalone`：

```bash
export OPENCLAW_DIR=/path/to/openclaw-standalone/openclaw
export OPENCLAW_LOCAL_NODE=/path/to/node
```

Windows 原生环境不能直接执行包内的 `install.sh` 和 `run.sh`；可以在 WSL 中部署，或按脚本内容手动使用满足版本要求的 Node.js/pnpm 安装和构建。

### 2.3 运行环境变量

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `OPENCLAW_DIR` | 从 `PATH` 自动定位 | 指向直接包含 `openclaw.mjs` 的已安装或已构建目录 |
| `OPENCLAW_LOCAL_NODE` | `node` | Node.js 可执行文件或绝对路径 |

当前适配器没有专门解析 profile，也不会依据 `OPENCLAW_CONFIG_PATH` 推导 transcript 位置。有关状态目录的限制见“Workspace、会话与工具证据”。

## 3. OpenClaw 原生配置

### 3.1 配置 provider、模型和 Agent

默认配置文件为 `~/.openclaw/openclaw.json`。下面示例使用 OpenClaw 2026.7.2 的 `agents.list` 格式；密钥和服务地址仅为占位符：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "primary": {
        "baseUrl": "https://primary.example.com/v1",
        "apiKey": "replace-with-secret",
        "api": "openai-completions",
        "timeoutSeconds": 3600,
        "headers": {
          "X-Custom-Header": "replace-if-required"
        },
        "models": [
          {
            "id": "main-model",
            "name": "主模型",
            "reasoning": true,
            "input": ["text"],
            "contextWindow": 200000,
            "maxTokens": 65536
          }
        ]
      },
      "review": {
        "baseUrl": "https://review.example.com/v1",
        "apiKey": "replace-with-secret",
        "api": "openai-completions",
        "models": [
          {
            "id": "review-model",
            "name": "评估模型",
            "reasoning": false,
            "input": ["text"],
            "contextWindow": 200000,
            "maxTokens": 8192
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {"primary": "primary/main-model"},
      "skipBootstrap": true,
      "memorySearch": {"enabled": false}
    },
    "list": [
      {
        "id": "main",
        "workspace": "~/.openclaw/workspace",
        "model": "primary/main-model"
      },
      {
        "id": "evaluator",
        "workspace": "~/.openclaw/workspace-evaluator",
        "model": "review/review-model"
      }
    ]
  },
  "tools": {
    "deny": [
      "sessions_spawn",
      "subagents",
      "conversations_send",
      "conversations_turn"
    ]
  }
}
```

注意事项：

- 模型使用完整的 `provider/model` 名称，例如 `primary/main-model`；
- 需要额外 HTTP Header 时，使用 provider 的 `headers` 字段；
- `baseUrl` 是否包含 `/v1` 应与实际 OpenAI 兼容端点一致；
- 建议使用 HTTPS，避免 API key 和自定义认证头通过明文 HTTP 传输；
- harness 任务中的 `agents[].name` 必须与原生 `agents.list[].id` 一致；
- 主 Agent 和 Evaluator 建议使用不同 ID 和 workspace；
- OpenClaw 版本之间配置 schema 可能变化，其他版本应以对应版本的 schema 和 `config validate` 结果为准。

配置完成后执行：

```bash
openclaw config validate
openclaw agents list --json
```

`agents list --json` 必须返回数组，且每个 harness Agent 至少包含可用的 `id` 和 `workspace`。客户端启动时会读取该数组；任务引用了未配置的 Agent 时会立即报错。

### 3.2 在 harness 任务中选择模型

示例：

```json
{
  "harness_type": "openclaw-local",
  "agents": [
    {
      "name": "main",
      "model": "primary/main-model",
      "system_prompt": "完成任务后核验产物。"
    },
    {
      "name": "evaluator",
      "model": "review/review-model"
    }
  ],
  "queries": [
    {
      "agent_name": "main",
      "session_name": "task1",
      "text": "在工作区创建 hello.txt，并说明文件内容。",
      "timeout": 1800,
      "use_simulator": false
    }
  ]
}
```

模型选择顺序为：

1. `simulator_config` 中同名 Agent 的配置里明确指定的模型；
2. harness `agents[].model`；
3. OpenClaw 原生 Agent 的默认模型。

这里的 simulator override 只覆盖模型名称，不覆盖 OpenClaw provider 的 URL、密钥或协议。

## 4. 每轮执行行为

### 4.1 实际 CLI 命令

每轮等价于：

```bash
node <OPENCLAW_DIR>/openclaw.mjs agent \
  --local \
  --agent <agent-id> \
  --session-key <generated-session-key> \
  --timeout <seconds> \
  --json \
  --message <prompt> \
  [--model <provider/model>]
```

默认任务执行超时为 3600 秒；初始化时执行 `agents list --json` 的默认 CLI 超时为 60 秒。进程退出或超时后，POSIX 使用进程组信号，Windows 使用 `terminate()`；等待 3 秒仍未结束则强制 kill。

### 4.2 Prompt 与长度限制

如果 Agent 配置了 `system_prompt`，仅在该会话首轮拼接：

```text
任务约束：
<system_prompt>

<query>
```

后续轮次只发送本轮 query，依靠原生 session 保留上下文。

当前适配器使用内联 `--message`。为避开 Windows CreateProcess 32,767 UTF-16 code unit 的命令行上限，prompt 最大允许 24,000 UTF-16 code unit；超过后在启动 OpenClaw 前直接失败。非 BMP 字符可能占两个 UTF-16 code unit，因此不能用 Python 字符数简单等同该限制。

`local_oc` 2026.7.2 已支持 `--message-file`，单个 UTF-8 文件最大 4 MiB，但当前适配器没有使用该能力。这是上游能力与当前 Python 实现的明确差异。

### 4.3 返回值和失败判定

客户端要求 stdout 是 JSON 对象，并读取：

- `payloads[].text`：按两个换行连接为最终回复；
- `payloads[].isError`：任一为真即视为失败；
- `meta.error` / `meta.aborted`：视为失败；
- `meta.stopReason`：`error`、`timeout`、`aborted`、`cancelled` 均为终止错误；
- `meta.agentMeta.sessionId`：原生 session ID；
- `meta.agentMeta.usage`：token usage。

回复为空也会失败。CLI stderr 在退出码为 0 时只写 debug 日志；非零退出时作为脱敏后的异常信息。

## 5. Workspace、会话与工具证据

### 5.1 Workspace

workspace 完全以 `agents list --json` 返回值为准。启动后，harness 会把任务指定的配置文件、skills 和输入文件装配到相应 workspace。当前客户端不会根据 harness 配置临时创建一个不存在的 OpenClaw Agent。

以 2026.7.2 配置为例，来源字段是 `agents.list[].workspace`。

### 5.2 会话复用规则

- 普通 Agent：同一次 harness 运行中，相同 `(agent_name, session_name)` 复用同一个 `OpenclawLocalAgent` 和 session key；
- 不同 session：生成不同的随机 `harness-<uuid>` session key；
- 新一次 harness 运行：重新生成 session key，不自动续接上一次运行；
- Evaluator：当 Agent ID 属于 evaluator 且 session name 以 `eval_` 开头时，每次获取都会创建新会话，避免评估之间污染上下文。

### 5.3 Transcript 与工具调用

当前客户端固定从以下位置读取原生 transcript：

```text
~/.openclaw/
└── agents/
    └── <agent-id>/
        ├── sessions/
        │   └── <session-id>.jsonl
        └── agent/
            └── openclaw-agent.sqlite
```

执行前记录已有 message ID，执行后只对新增 message 调用 `extract_tool_calls()`，从而得到本轮工具证据。

如果 `openclaw-agent.sqlite` 存在，客户端只读查询：

```sql
SELECT event_json
FROM transcript_events
WHERE session_id = ?
ORDER BY seq
```

查到事件后，会在 `sessions/<session-id>.jsonl` 写出仅含当前会话的镜像，兼容现有轨迹统计器。若历史读取失败，本轮仍可返回模型回复，但工具调用可能为空，并通过 `evidence_incomplete` 标记证据不完整。

重要限制：路径目前硬编码为 `Path.home()/.openclaw`。如果通过 `OPENCLAW_STATE_DIR`、profile 或其他方式把原生状态放在别处，Agent 执行可能成功，但工具证据读取会失败。使用当前适配器时应保持默认状态目录，或同步修改客户端的历史路径解析逻辑。

同一状态目录不应同时由独立 Gateway 和 `--local` harness 竞争使用。运行 harness 前建议停止同状态目录下的 Gateway。

## 6. 与 `local_oc` 的相同点和差异

### 6.1 相同点

- 都使用 OpenClaw 原生 Agent、provider、工具、skills 和会话实现；
- 核心执行入口都是 `agent --local --json`；
- 都支持 `--agent`、`--session-key`、`--model` 和 `--timeout`；
- 都通过原生 workspace 工作，不由 Python 重新实现 Agent 沙箱；
- JSON 契约均以 `payloads` 和 `meta` 为核心；
- 无需预先启动 Gateway 即可执行单轮任务。

### 6.2 差异

| 维度 | `openclaw_local_client.py` | `local_oc` 2026.7.2 |
| --- | --- | --- |
| 定位 | harness 适配器 | 完整 OpenClaw 源码发行包 |
| 技术栈 | Python / asyncio / sqlite3 | TypeScript / Node.js / pnpm |
| 规模 | 单文件适配逻辑 | 27,345 个源码文件及完整产品能力 |
| 配置职责 | 读取 Agent、选择模型 | 管理 provider、Agent、工具、插件、MCP 等完整配置 |
| 消息输入 | 仅 `--message`，24,000 UTF-16 单元 | 同时支持 `--message` 和最大 4 MiB 的 `--message-file` |
| 会话证据 | 事后读取 transcript 并转换为 harness 工具轨迹 | 原生生成和维护 transcript/SQLite |
| Gateway/渠道/UI | 不提供 | 包含 Gateway、多渠道、控制台 UI 等 |
| 安装状态 | 项目代码的一部分 | 压缩源码包，解压、安装依赖和构建后才能运行 |
| 状态目录 | 工具证据固定读取 `~/.openclaw` | 支持默认目录、profile 和 `OPENCLAW_STATE_DIR` 等模式 |

二者是调用关系，不适合按“谁替代谁”理解：harness 仍需要 Python 适配器，而适配器必须连接一个已安装或已构建的 OpenClaw。

### 6.3 已确认的版本差异

| 项目 | 当前适配器 | `local_oc` 2026.7.2 | 影响 |
| --- | --- | --- | --- |
| 消息参数 | `--message` | 支持 `--message-file` | 长 prompt 能力未被适配器使用 |
| Agent 配置示例 | 客户端只依赖 `agents list` 输出 | 原生 schema 使用 `agents.list[]` | 配置应采用 `list`，不要使用旧的 `agents.entries` 示例 |
| Transcript 状态目录 | 固定 `~/.openclaw` | 可配置状态目录 | 非默认目录会丢失工具证据 |
| 安装入口 | 要求找到已构建的 `openclaw.mjs` | 交付物只有源码和安装脚本 | 必须先完成解压、依赖安装和 build |

## 7. 运行与验收

### 7.1 运行前检查

```bash
node --version
openclaw --version
openclaw config validate
openclaw agents list --json
openclaw gateway stop --force
```

使用 `OPENCLAW_DIR` 时，可直接检查入口：

```bash
test -f "$OPENCLAW_DIR/openclaw.mjs"
"${OPENCLAW_LOCAL_NODE:-node}" "$OPENCLAW_DIR/openclaw.mjs" --version
"${OPENCLAW_LOCAL_NODE:-node}" "$OPENCLAW_DIR/openclaw.mjs" agents list --json
```

### 7.2 运行 harness

```bash
python harness_automation.py \
  --harness openclaw-local \
  --config configs/config_session.json
```

也可以对其他任务配置使用相同后端：

```bash
python harness_automation.py --harness openclaw-local --config configs/config_simple.json
python harness_automation.py --harness openclaw-local --config configs/config_user.json
python harness_automation.py --harness openclaw-local --config configs/config_simple_eval.json
```

`--harness` 会覆盖任务文件中的 `harness_type`。模拟用户仍由项目公共 simulator 创建，其模型连接配置与 OpenClaw provider 相互独立。

### 7.3 验收要点

1. `agents list --json` 中存在任务引用的全部 Agent，并返回正确 workspace；
2. harness 日志中显示 Agent 已设置，且没有 provider URL/key override 警告；
3. 短任务能返回非空内容和原生 session ID；
4. 相同 `session_name` 的第二轮能使用第一轮上下文；
5. 执行工具后，结果中的 `tool_calls` 非空，轨迹没有标记证据不完整；
6. Evaluator 使用独立 Agent/workspace，并在 `logs/trajectories/<run_id>/` 生成评估轨迹；
7. 超时任务能够结束，且没有遗留的 OpenClaw/Node 子进程。

## 8. 常见问题

### 未找到 OpenClaw 入口

确认 `openclaw` 在 `PATH` 中，或将 `OPENCLAW_DIR` 指向直接包含 `openclaw.mjs` 的目录。不要指向 `local_oc` 压缩包所在目录。

### Agent 未配置

任务的 `agents[].name` 与 `openclaw agents list --json` 返回的 `id` 不一致。对于 `local_oc` 2026.7.2，检查 `agents.list[].id`。

### 消息过长

当前实现不能使用 2026.7.2 的 `--message-file`，应缩短 query/system prompt，或在代码中增加消息文件支持后再提交长任务。

### 回复正常但工具调用为空

检查：

- CLI JSON 是否返回 `meta.agentMeta.sessionId`；
- `~/.openclaw/agents/<agent-id>/sessions/` 是否可读写；
- 是否设置了 `OPENCLAW_STATE_DIR` 或使用了 profile；
- SQLite 是否包含对应 session 的 `transcript_events`；
- Gateway 是否与 harness 同时访问同一状态目录。

### provider 连接失败

先用最小请求验证端点，再检查 OpenClaw provider 的 `baseUrl`、`api`、`apiKey`、`headers`、模型 ID 和 timeout。不要把这些连接字段只写在 harness 的 simulator override 中。

## 9. 相关文件

- `src/openclaw_local_client.py`：当前 Python 适配器实现；
- `harness_automation.py`：`openclaw-local` 后端接线；
- `src/workspace.py`：workspace 文件装配；
- `D:\openclaw-task-codes\local_oc\openclaw-README.zh-CN.md`：独立源码包部署说明；
- `D:\openclaw-task-codes\local_oc\openclaw-requirements.md`：Node.js、pnpm 和系统依赖；
- `D:\openclaw-task-codes\local_oc\openclaw_config_20261019_zh.md`：生产环境配置记录；
- OpenClaw 原生配置和 CLI 文档：以所部署版本随包文档为准。
