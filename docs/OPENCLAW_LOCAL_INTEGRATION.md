# OpenClaw Local 集成说明

项目通过官方 OpenClaw CLI 的 `agent --local --json` 模式运行 Agent。模型服务、凭据和工作区由 OpenClaw 原生配置管理，任务通过 `--harness openclaw-local` 选择该后端。

## 部署前置条件

在 Linux 环境准备 Node.js 和 npm，Node.js 版本须满足所安装 OpenClaw 的要求。

```bash
npm install -g openclaw@2026.8.2
openclaw --version
openclaw onboard
```

适配器从 `PATH` 自动定位官方 npm 安装。使用自定义安装目录时，设置 `OPENCLAW_DIR` 指向包含 `openclaw.mjs` 的目录。原生配置保存在 `~/.openclaw/openclaw.json`。

## 1. 接入方式

每次 harness 运行创建一个 `OpenclawLocalClient`，每个 `(agent_name, session_name)` 对应一个原生会话：

- 启动时通过 `agents list --json` 读取 Agent ID 和实际工作区；
- 每轮启动一个本地 CLI 进程，通过 `--session-key` 延续同一会话；
- 通过 `--model` 选择模型，使用消息文件传入任务并解析 JSON 回复；
- 从本轮原生会话记录提取工具调用，供 Evaluator 使用；
- 超时或取消会终止进程，失败轮次不自动重发，模型请求恢复由 OpenClaw 原生机制处理。

运行参数由环境变量指定：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `OPENCLAW_DIR` | 从 `PATH` 自动定位 | 自定义 OpenClaw 安装目录 |
| `OPENCLAW_LOCAL_NODE` | `node` | Node.js 可执行文件 |

## 2. OpenClaw 配置

### 2.1 配置模型和 Agent

在 `~/.openclaw/openclaw.json` 中配置多个模型服务，每个服务可提供多个模型。下面示例包含两个服务、每个服务两个模型；将字段合并到原生配置，替换服务地址、密钥和模型 ID：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "primary": {
        "baseUrl": "https://primary.example.com/v1",
        "apiKey": "primary-api-key",
        "api": "openai-completions",
        "models": [
          {"id": "main-model", "name": "主模型"},
          {"id": "fast-model", "name": "快速模型"}
        ]
      },
      "review": {
        "baseUrl": "https://review.example.com/v1",
        "apiKey": "review-api-key",
        "api": "openai-completions",
        "models": [
          {"id": "review-model", "name": "评估模型"},
          {"id": "fast-model", "name": "评估服务快速模型"}
        ]
      }
    }
  },
  "agents": {
    "ownership": "explicit",
    "defaults": {
      "model": {"primary": "primary/main-model"},
      "skipBootstrap": true,
      "memorySearch": {"enabled": false}
    },
    "entries": {
      "main": {"workspace": "~/.openclaw/workspace"},
      "evaluator": {"model": "review/review-model", "workspace": "~/.openclaw/workspace-evaluator"}
    }
  },
  "tools": {
    "deny": ["sessions_spawn", "subagents", "conversations_send", "conversations_turn"]
  }
}
```

每个 provider 独立配置服务地址、密钥、协议和模型列表。模型通过 `provider/model` 引用；同名的 `fast-model` 分别使用 `primary/fast-model` 和 `review/fast-model` 区分。主 Agent 默认使用 `primary/main-model`，Evaluator 使用 `review/review-model`。

任务中的 `agents[].name` 须与原生 Agent ID 一致；Evaluator 使用独立 Agent 和工作区。`tools.deny` 禁用后台子 Agent 和跨会话派生，任务由公共执行器顺序编排。

配置完成后检查原生配置和 Agent 列表：

```bash
openclaw config validate
openclaw agents list --json
```

### 2.2 在任务中选择模型

通过 `agents[].model` 引用 `provider/model`，例如：

```json
{
  "harness_type": "openclaw-local",
  "agents": [
    {"name": "main", "model": "primary/main-model", "system_prompt": "完成任务后核验产物。"},
    {"name": "evaluator", "model": "review/review-model"}
  ],
  "queries": [{
    "agent_name": "main",
    "session_name": "task1",
    "text": "在工作区创建 hello.txt，并说明文件内容。",
    "timeout": 1800,
    "use_simulator": false
  }]
}
```

模型选择优先级为 `simulator_config` 同名 Agent 的模型配置 > `agents[].model` > OpenClaw 默认模型。服务地址、协议和密钥统一写在 OpenClaw 原生配置中。

## 3. Workspace 与会话

工作区以原生 `agents.entries.<id>.workspace` 为准，配置文件、技能和输入文件自动装配到对应目录。例如：

```text
~/.openclaw/
├── workspace/
│   ├── USER.md
│   ├── SOUL.md
│   └── skills/demo-skill/SKILL.md
├── workspace-evaluator/
└── agents/
    ├── main/sessions/
    │   └── <session-id>.jsonl
    └── evaluator/sessions/
        └── <session-id>.jsonl
```

同一次运行中，相同 Agent 和 session 复用会话；不同 session 和新一次运行分别创建新会话。Agent 工作区跨运行保留，Evaluator 每次评估创建独立会话。

`system_prompt` 在会话首轮作为任务约束注入，`USER.md`、`SOUL.md` 和 skills 由 OpenClaw 加载。工具证据根据 CLI 返回的会话 ID 读取。SQLite 会话在上述目录生成当前会话的 JSONL 视图，供现有 OpenClaw 统计器使用；任务统计沿用单轨迹选择规则。

## 4. 运行与验收

准备好原生模型、Agent、输入文件，以及项目通用的模拟用户和评估器配置后运行。使用同一状态目录的 Gateway 应先停止：

```bash
openclaw gateway stop --force

python harness_automation.py --harness openclaw-local --config configs/config_simple.json
python harness_automation.py --harness openclaw-local --config configs/config_user.json
python harness_automation.py --harness openclaw-local --config configs/config_simple_eval.json
```

`--harness` 覆盖任务文件的 `harness_type`。后两项的模拟用户直接使用 `simulator_config` 中 `user_simulator` 的服务配置，与 OpenClaw 原生模型配置独立。第二项检查模拟用户的 `【Task_Done】`；第三项检查 Evaluator 评分和 `logs/trajectories/<run_id>/` 下的评估轨迹。

原生配置和参数参见 [OpenClaw 配置文档](https://docs.openclaw.ai/gateway/configuration) 和 [Agent CLI](https://docs.openclaw.ai/cli/agent)。
