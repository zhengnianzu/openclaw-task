# OpenClaw TUI（Local CLI）集成说明

本文说明 OpenClaw Harness 如何通过 `src/openclaw_local_client.py` 调用官方 OpenClaw CLI 的 `agent --local --json` 模式。

当前代码中的规范后端名称是：

```text
openclaw-tui
```

文件名和部分 Python 类名仍保留 `openclaw_local` / `OpenclawLocal*`，但任务配置及 `--harness` 参数必须以当前 `harness_automation.py` 实现为准，使用 `openclaw-tui`。

## 1. 架构与职责

```text
harness_automation.py
    │
    ▼
OpenclawLocalClient
    │  子进程调用
    ▼
node <OPENCLAW_DIR>/openclaw.mjs agent --local --json ...
    │
    ├── OpenClaw 原生 Agent / provider / model / tools / skills
    ├── OpenClaw 原生 session 与 transcript
    └── JSON: payloads + meta
            │
            ▼
ExecutionResult + 本轮工具调用证据
```

Python 适配层负责：

- 探测 OpenClaw 版本并选择 Node.js 与 `openclaw.mjs`；
- 通过 `agents list --json` 读取 Agent ID 和 workspace；
- 把 Harness 的 `(agent_name, session_name)` 映射为原生 session key；
- 首轮注入 Harness `system_prompt`；
- 根据版本选择 `--message` 或 `--message-file`；
- 启动、超时终止并回收 CLI 子进程；
- 校验 JSON 返回值并转换为统一的 `ExecutionResult`；
- 从 JSONL 或 SQLite transcript 提取本轮工具调用；
- 对 CLI 错误信息做脱敏处理。

OpenClaw 原生侧负责：

- provider、模型、凭据和 Agent 配置；
- workspace、skills、工具及其权限；
- 模型请求、工具循环、上下文和会话持久化；
- transcript、usage、stop reason 和最终回复。

客户端仅接受**模型名称覆盖**。Harness 的 `simulator_config` 中即使包含 `base_url`、`api_key` 或 `api`，这些连接参数也不会注入 OpenClaw，必须配置在 OpenClaw 原生配置中。

## 2. 版本分流与镜像布局

### 2.1 版本探测

模块加载时会执行：

```bash
openclaw --version
```

客户端用正则提取 `YYYY.M.P` 形式的版本号，命令失败或 3 秒内未返回时降级为 `2026.6.6`。版本值在模块加载时写入 `OC_TUI_VERSION`，运行中不会再次探测。

因此，镜像中的 `openclaw` 命令必须在 `PATH` 中且能正常返回版本。仅设置 `OPENCLAW_DIR` 不会改变前面的版本探测结果。

### 2.2 当前内置运行时映射

| 检测版本 | 默认 Node.js | 默认 OpenClaw 目录 | 消息方式 |
| --- | --- | --- | --- |
| 精确等于 `2026.10.6` | `/usr/local/node26/bin/node` | `/usr/local/node26/lib/node_modules/openclaw` | `--message-file` |
| 其他版本或探测失败 | `/usr/local/node24/bin/node` | `/usr/local/node24/lib/node_modules/openclaw` | `--message` |

当前分支条件是**精确比较 `2026.10.6`**。包括 `2026.8.2`、未来版本以及无法识别的版本在内，都会进入默认分支。升级 OpenClaw 前必须验证 CLI 参数、Node.js 版本和配置 schema，不能仅替换 npm 包。

### 2.3 环境变量

| 环境变量 | 未设置时 | 用途 |
| --- | --- | --- |
| `OPENCLAW_DIR` | 使用版本映射中的目录 | 必须指向直接包含 `openclaw.mjs` 的目录 |
| `OPENCLAW_LOCAL_NODE` | 使用版本映射中的 Node.js | Node.js 可执行文件或绝对路径 |

示例：

```bash
export OPENCLAW_DIR=/opt/openclaw
export OPENCLAW_LOCAL_NODE=/usr/local/bin/node
```

如果映射目录或 `OPENCLAW_DIR/openclaw.mjs` 不存在，客户端会从 `PATH` 查找 `openclaw`。当找到的是 npm shim 时，会尝试推导同级的 `node_modules/openclaw/openclaw.mjs`。

> PATH 回退只负责寻找 OpenClaw 入口；Node.js 仍使用 `OPENCLAW_LOCAL_NODE` 或版本映射值。

## 3. 镜像内安装建议

### 3.1 OpenClaw 2026.6.6

```bash
npm install -g openclaw@2026.6.6
openclaw --version
openclaw onboard
```

使用当前内置默认布局时，应确认：

```text
/usr/local/node24/bin/node
/usr/local/node24/lib/node_modules/openclaw/openclaw.mjs
```

如果镜像目录不同，设置 `OPENCLAW_LOCAL_NODE` 和 `OPENCLAW_DIR`。

### 3.2 OpenClaw 2026.10.6

当前客户端将 `2026.10.6` 绑定到 Node 26 运行时：

```text
/usr/local/node26/bin/node
/usr/local/node26/lib/node_modules/openclaw/openclaw.mjs
```

安装后至少检查：

```bash
openclaw --version
/usr/local/node26/bin/node \
  /usr/local/node26/lib/node_modules/openclaw/openclaw.mjs --version
```

若实际安装位置不同，应显式设置两个环境变量。客户端是否进入 `--message-file` 分支仍取决于 `PATH` 中 `openclaw --version` 的结果。

## 4. OpenClaw 原生配置

默认配置文件是 `~/.openclaw/openclaw.json`。以下仅为结构示例，字段最终应以所部署 OpenClaw 版本的 schema 为准：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "primary": {
        "baseUrl": "https://model.example.com/v1",
        "apiKey": "replace-with-secret",
        "api": "openai-completions",
        "timeoutSeconds": 3600,
        "models": [
          {
            "id": "main-model",
            "name": "Main Model",
            "reasoning": true,
            "input": ["text"],
            "contextWindow": 200000,
            "maxTokens": 65536
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
        "model": "primary/main-model"
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

注意：

- 模型名使用完整的 `provider/model`；
- Harness 的 `agents[].name` 必须与 `agents.list[].id` 一致；
- 每个任务 Agent 都必须有可用的 workspace；
- 主 Agent 与 Evaluator 建议使用不同 ID 和 workspace；
- API key 不应提交到仓库或固化到公共镜像层；
- 不同 OpenClaw 版本的 schema 可能不同，升级后需要重新校验。

配置完成后执行：

```bash
openclaw config validate
openclaw agents list --json
```

`agents list --json` 必须返回数组，并且每项至少包含 `id` 和 `workspace`。客户端进入上下文时会读取它；任务引用未配置的 Agent 会直接失败。

## 5. Harness 配置

```json
{
  "harness_type": "openclaw-tui",
  "agents": [
    {
      "name": "main",
      "model": "primary/main-model",
      "system_prompt": "完成任务后核验产物。"
    },
    {
      "name": "evaluator",
      "model": "primary/main-model"
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

模型选择优先级：

1. `simulator_config` 中同名 Agent 明确配置的模型；
2. Harness 的 `agents[].model`；
3. OpenClaw 原生 Agent 默认模型。

这里的 override 仅覆盖模型名称，不覆盖 provider URL、密钥或 API 协议。

## 6. 每轮执行行为

### 6.1 基础命令

每轮都会调用：

```bash
node <OPENCLAW_DIR>/openclaw.mjs agent \
  --local \
  --agent <agent-id> \
  --session-key <harness-uuid> \
  --timeout <seconds> \
  --json \
  [--model <provider/model>] \
  <message-option>
```

消息参数由版本分支决定：

- `2026.10.6`：`--message-file <temp-file>`；
- 其他版本：`--message <prompt>`。

默认任务超时是 3600 秒，初始化 `agents list --json` 的默认超时是 60 秒。进程退出或超时后，POSIX 使用进程组信号，Windows 使用 `terminate()`；等待 3 秒仍未结束则强制 kill。

### 6.2 Prompt 与长度限制

Agent 配置了 `system_prompt` 时，仅在会话首轮拼接：

```text
任务约束：
<system_prompt>

<query>
```

后续轮次只发送当前 query，依赖原生 session 保留上下文。

| 分支 | 上限 | 超限行为 |
| --- | --- | --- |
| `--message` | 128,000 UTF-8 字节 | 启动 CLI 前报错 |
| `--message-file` | 4 MiB UTF-8 字节 | 启动 CLI 前报错 |

`--message-file` 使用系统临时文件，执行成功、失败或超时后都会在 `finally` 中删除。

### 6.3 结果解析

客户端要求 stdout 是 JSON 对象，并读取：

- `payloads[].text`：以两个换行拼接为最终回复；
- `payloads[].isError`：任一为真即失败；
- `meta.error` / `meta.aborted`：存在即失败；
- `meta.stopReason`：`error`、`timeout`、`aborted`、`cancelled` 均视为终止错误；
- `meta.agentMeta.sessionId`：原生 session ID；
- `meta.agentMeta.usage`：Token 用量。

最终回复为空也会失败。CLI 退出码非零时，脱敏后的 stderr 会作为错误信息；退出码为零时，stderr 仅写入 debug 日志。

## 7. Workspace、会话与工具证据

### 7.1 Workspace

workspace 以 `agents list --json` 返回值为准。Harness 随后把任务配置文件、skills 和输入文件装配到对应 workspace。客户端不会临时创建 OpenClaw Agent。

### 7.2 会话

- 相同 `(agent_name, session_name)` 在一次 Harness 运行中复用同一个 Agent 对象和 session key；
- 不同 session 使用不同的 `harness-<uuid>`；
- 新 Harness 进程不会自动续接上一次运行；
- Evaluator Agent 且 session name 以 `eval_` 开头时，每次都创建新会话，避免评估上下文污染。

### 7.3 Transcript 与工具调用

客户端固定读取：

```text
~/.openclaw/
└── agents/
    └── <agent-id>/
        ├── sessions/
        │   └── <session-id>.jsonl
        └── agent/
            └── openclaw-agent.sqlite
```

执行前记录已有 message ID，执行后只对新增 message 调用 `extract_tool_calls()`。

若 SQLite 存在，客户端以只读模式查询：

```sql
SELECT event_json
FROM transcript_events
WHERE session_id = ?
ORDER BY seq
```

查到事件后，会写出当前 session 的 JSONL 镜像，兼容现有轨迹提取逻辑。历史读取失败不会丢弃模型回复，但本轮 `tool_calls` 可能为空，并返回 `evidence_incomplete=true`。

重要限制：历史路径固定为 `Path.home()/.openclaw`。如果用 `OPENCLAW_STATE_DIR`、profile 或其他方式改变原生状态目录，Agent 可能执行成功，但工具证据读取会失败。

同一状态目录不应同时由独立 Gateway 与 `--local` Harness 竞争使用。运行 Harness 前建议停止使用相同状态目录的 Gateway。

## 8. 运行与验收

运行前检查：

```bash
node --version
openclaw --version
openclaw config validate
openclaw agents list --json
openclaw gateway stop --force
```

使用显式目录时：

```bash
test -f "$OPENCLAW_DIR/openclaw.mjs"
"$OPENCLAW_LOCAL_NODE" "$OPENCLAW_DIR/openclaw.mjs" --version
"$OPENCLAW_LOCAL_NODE" "$OPENCLAW_DIR/openclaw.mjs" agents list --json
```

运行 Harness：

```bash
python harness_automation.py \
  --harness openclaw-tui \
  --config configs/config_session.json
```

`--harness` 会覆盖配置文件中的 `harness_type`。

验收要点：

1. 日志打印的 OpenClaw 版本、Node.js 和入口路径与预期一致；
2. `agents list --json` 包含任务引用的全部 Agent 和正确 workspace；
3. 短任务返回非空内容、原生 session ID 和 usage；
4. 同一 session 的第二轮能使用第一轮上下文；
5. 工具任务返回非空 `tool_calls`，且没有证据不完整标记；
6. 2026.10.6 的长消息走 `--message-file`，临时文件能被清理；
7. 超时后没有遗留的 OpenClaw/Node 子进程。

## 9. 常见问题

### 版本探测错误

确认 `PATH` 中的 `openclaw --version` 能在 3 秒内返回正确版本。如果探测失败，客户端会按 `2026.6.6` 和默认运行时处理，即使 `OPENCLAW_DIR` 指向其他版本。

### 未找到 OpenClaw 入口

确认映射目录存在，或将 `OPENCLAW_DIR` 指向直接包含 `openclaw.mjs` 的目录。若只依赖 PATH 回退，还要确认 npm shim 可以推导到实际包目录。

### Agent 未配置

任务的 `agents[].name` 与 `openclaw agents list --json` 返回的 `id` 不一致，或该 Agent 没有 workspace。

### 消息过长

- 2026.10.6：确保版本探测确实得到 `2026.10.6`；该分支最大 4 MiB；
- 其他版本：当前实现只允许最大 128,000 UTF-8 字节的内联消息。

### 回复正常但工具调用为空

检查：

- CLI 是否返回 `meta.agentMeta.sessionId`；
- `~/.openclaw/agents/<agent-id>/sessions/` 是否可读写；
- 是否使用了 `OPENCLAW_STATE_DIR` 或 profile；
- SQLite 是否包含对应 session 的 `transcript_events`；
- Gateway 是否与 Harness 同时访问同一状态目录。

### Provider 连接失败

检查 OpenClaw 原生配置中的 `baseUrl`、`api`、`apiKey`、自定义 headers、模型 ID 和 timeout。不要只把这些字段写入 Harness 的 simulator override。

## 10. 当前已知接线问题

执行入口当前只识别：

```text
harness_type == "openclaw-tui"
```

但 `scripts/harness_adapter.py` 的统计适配器注册表当前仍使用旧键 `openclaw-local`，没有注册 `openclaw-tui`。因此：

- 任务执行必须使用 `openclaw-tui`；
- 运行中的 `ExecutionResult.tool_calls` 仍由本客户端提取；
- 任务结束后的通用统计入口可能退回基础 `HarnessAdapter`，无法按 `OpenclawAdapter` 解析完整原生轨迹。

这是当前源码中的命名不一致，不应通过把任务改回 `openclaw-local` 绕过；那样不会进入本地 CLI 执行分支。若需要完整的结束后轨迹统计，应在代码中为 `openclaw-tui` 注册同一个 `OpenclawAdapter`，并完成回归测试。

## 11. 相关文件

- `src/openclaw_local_client.py`：本地 CLI 客户端和版本分流；
- `harness_automation.py`：`openclaw-tui` 执行入口；
- `src/workspace.py`：workspace 文件装配；
- `scripts/harness_adapter.py`：结束后轨迹与统计适配；
- `~/.openclaw/openclaw.json`：OpenClaw 原生配置；
- 所部署 OpenClaw 版本自带文档和 schema：配置与 CLI 的最终依据。
