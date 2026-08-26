# Grok Build 集成说明

项目通过官方 Grok Build CLI 的 headless JSON 模式运行 Grok Agent。运行环境需要预先安装 `grok`。

## 部署前置条件

macOS、Linux 或 Git Bash：

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
grok --version
```

Windows PowerShell：

```powershell
irm https://x.ai/cli/install.ps1 | iex
grok --version
```

Windows 建议使用官方预编译包。harness 不负责安装、升级或登录 Grok；模型服务、认证信息和全局配置均由部署环境维护。

## 1. 接入方式

每次 harness 运行创建一个 `GrokClient`，每个 `(agent_name, session_name)` 对应一个独立的 Grok Agent：

- 首轮通过 `grok --output-format json --prompt-file` 创建原生会话；
- 后续轮通过 `grok --resume <sessionId>` 延续上下文；
- 每轮启动一个 Grok CLI 进程，结束后解析其 JSON 结果；
- 超时或取消会终止当前 CLI 进程，失败请求不会自动重试。

Grok 的 headless JSON 结果不包含完整的逐工具调用轨迹，因此 `ExecutionResult.tool_calls` 可能为空。文件类任务仍由 Evaluator 扫描当前 session workspace 中的实际产物进行验收。

`GrokClient` 在每次启动 Grok CLI 时都会自动追加 `--always-approve`，用户不需要在
任务配置、`config.toml` 或环境变量中设置该参数。

## 2. Grok 配置

Grok 的服务端、协议、模型和凭据由 `$GROK_HOME/config.toml` 管理；未设置 `GROK_HOME` 时默认使用 `~/.grok`。harness 不生成或修改该文件，也不会把密钥写入 workspace。

任务配置中的 `model` 是 Grok 模型别名，选择优先级为：

1. `simulator_config` 中同名 Agent 的 `model`；
2. `agents[].model`；
3. `$GROK_HOME/config.toml` 中的 `[models].default`。

`simulator_config` 中的 `provider`、`base_url` 和 `api_key` 不参与 Grok 路由。对应信息必须写在 `config.toml` 中。

### 2.1 配置多个服务

一个 `config.toml` 可以配置多个服务。每个 `[model.<alias>]` 都是一个可被
`agents[].model` 引用的模型别名，可以分别设置模型 ID、服务地址、协议和明文密钥。

以下示例同时配置 Anthropic Messages、OpenAI Responses 和 OpenAI Chat
Completions 三种服务：

```toml
[cli]
auto_update = false

[models]
default = "claude-opus"

# 服务一：Anthropic Messages
[model.claude-opus]
model = "claude-opus-4-6"
name = "Claude Opus 4.6"
base_url = "https://api.anthropic.com/v1"
api_backend = "messages"
extra_headers = { "x-api-key" = "sk-ant-your-key", "anthropic-version" = "2023-06-01" }
context_window = 200000
max_completion_tokens = 8192

# 服务二：OpenAI Responses 兼容中转服务
[model.responses-model]
model = "upstream-responses-model-id"
name = "Responses Model"
base_url = "https://responses-gateway.example.com/v1"
api_backend = "responses"
api_key = "sk-responses-your-key"
context_window = 200000
max_completion_tokens = 8192

# 服务三：OpenAI Chat Completions 兼容中转服务
[model.deepseek-chat]
model = "deepseek-chat"
name = "DeepSeek Chat"
base_url = "https://chat-gateway.example.com/v1"
api_backend = "chat_completions"
api_key = "sk-chat-your-key"
context_window = 128000
max_completion_tokens = 8192
```

示例中的密钥均为明文：OpenAI 兼容服务使用 `api_key`，Anthropic Messages 的
`x-api-key` 直接写入 `extra_headers`，不需要再设置密钥环境变量。

同一服务如果提供多个模型，可继续增加新的 `[model.<alias>]`，复用相同的
`base_url` 和密钥配置，并填写不同的 `model`。`[models].default` 只指定默认别名，
不会限制其他模型的使用。

Grok 支持 `chat_completions`、`responses` 和 `messages` 三种自定义模型协议。配置完成后可检查最终生效的配置和模型列表：

```bash
grok inspect --json
grok models
```

更多字段参见 [Grok Build 自定义模型文档](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md) 和 [Grok Build Settings](https://docs.x.ai/build/settings)。

### 2.2 在任务中选择服务

任务文件只需要引用对应的模型别名。不同 Agent 可以使用不同服务：

```json
{
  "harness_type": "grok",
  "agents": [
    {
      "name": "main",
      "model": "claude-opus",
      "system_prompt": "你是一个编码助手。"
    },
    {
      "name": "reviewer",
      "model": "responses-model",
      "system_prompt": "你负责代码审查。"
    }
  ],
  "queries": [
    {
      "agent_name": "main",
      "session_name": "main",
      "text": "完成当前任务。"
    }
  ]
}
```

## 3. Workspace 与会话

Grok harness 的默认 workspace 为 `~/.grok-harness/workspace`，可通过 `GROK_HARNESS_WORKSPACE` 修改。

每个 Agent 先准备模板目录，真正执行时再为每个 session 创建独立 cwd：

```text
~/.grok-harness/workspace/
└─ main/                                  # Agent 模板
   ├─ AGENTS.md
   ├─ .agents/skills/<skill>/SKILL.md
   └─ .sessions/
      └─ main_20260820T104927/             # 实际执行目录
```

首次创建 session 时，模板中的配置文件、skills 和用户文件会复制到实际执行目录；已有 session 不会被模板再次覆盖。Evaluator 读取的也是该实际执行目录。

会话行为如下：

- framework 会将 `run_id` 加入运行时 session 名；
- 同一 harness run 内，相同 Agent 和 session 会复用原生 `sessionId`；
- 新一轮 harness run 默认创建新会话，不自动接续上一次运行；
- `agents[].system_prompt` 仅在创建新会话时通过 `--rules` 注入；
- `agents[].config` 按原文件名复制，Grok 可自动加载 `AGENTS.md` 等项目规则文件；
- skills 使用 `.agents/skills/<name>/SKILL.md` 布局；
- 每轮使用 `--no-memory`，不启用跨会话 memory；
- 恢复会话只恢复对话状态，不回滚 workspace 中的文件改动。

## 4. 运行与验收

部署环境先确认 Grok CLI 可用并已写好上述配置：

```bash
grok --version

python harness_automation.py --harness grok --config configs/config_simple.json
python harness_automation.py --harness grok --config configs/config_user.json
python harness_automation.py --harness grok --config configs/config_simple_eval.json

python test/test_grok_client.py
```
