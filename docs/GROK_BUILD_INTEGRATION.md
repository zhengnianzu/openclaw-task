# Grok Build 集成说明

项目通过官方 Grok Build CLI 的 headless JSON 模式运行 Grok Agent。运行环境需要预先安装 `grok`。

## 部署前置条件

macOS、Linux 或 Git Bash：

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
grok --version
grok update
```

Windows PowerShell：

```powershell
irm https://x.ai/cli/install.ps1 | iex
grok --version
```

## 1. 接入方式

每次 harness 运行创建一个 `GrokClient`，每个 `(agent_name, session_name)` 对应一个独立的 Grok Agent：

- 首轮通过 `grok --output-format json --prompt-file` 创建原生会话；
- 后续轮通过 `grok --resume <sessionId>` 延续上下文；
- 每轮启动一个 Grok CLI 进程，结束后解析其 JSON 结果；
- 超时或取消会终止当前 CLI 进程，失败请求不会自动重试。

`GrokClient` 在每次启动 Grok CLI 时都会自动追加 `--always-approve`，用户不需要在
任务配置、`config.toml` 或环境变量中设置该参数。

## 2. Grok 配置

Grok 的服务端、协议、模型和凭据由 `$GROK_HOME/config.toml` 管理；未设置 `GROK_HOME` 时默认使用 `~/.grok`。

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

Grok 默认使用 `~/.grok-harness/workspace`，可通过 `GROK_HARNESS_WORKSPACE`
修改；任务 JSON 中的 `workspace_base` 不改变该路径。

每个 Agent 使用一个模板目录，实际执行目录位于模板下的 `.sessions`。普通 Agent
的 session 名为 `<query.session_name 或 main>_<run_id>`，Evaluator 的 session 名为
`eval_<evaluate.session_name 或 query.session_name 或 main>_<run_id>`。例如
`run_id=20260820T104927`、query 的 `session_name=eval_test`：

```text
主 Agent 模板：~/.grok-harness/workspace/main
主 Agent cwd：~/.grok-harness/workspace/main/.sessions/eval_test_20260820T104927
Evaluator 模板：~/.grok-harness/workspace/evaluator
Evaluator cwd：~/.grok-harness/workspace/evaluator/.sessions/eval_eval_test_20260820T104927
```

实际 session 中的配置和 skill 目录结构如下：

```text
~/.grok-harness/workspace/main/.sessions/eval_test_20260820T104927/
├── AGENTS.md
└── skills/
    └── demo-skill/
        └── SKILL.md
```
~/.grok/skills/
~/.grok/memory/MEMORY.md

首次创建 session 时，配置文件、skill 和用户文件会从模板复制到实际目录；skill
位于 `.agents/skills`。同一 harness run 内，相同 Agent 和 session 复用原生
`sessionId`，后续轮次通过 `--resume` 恢复对话。新一轮 run 创建新会话，但原目录
中的文件仍会保留；Evaluator 读取的是实际 session 目录，而不是模板目录。

同目录可识别的文件名包括：Agents.md、Claude.md、CLAUDE.md、CLAUDE.local.md、AGENT.md、AGENTS.md。另会扫描.grok/rules/、以及兼容模式下的.claude/rules/、.cursor/rules/中的*.md。

## 4. 运行与验收

部署环境先确认 Grok CLI 可用并已写好上述配置：

```bash
grok --version

python harness_automation.py --harness grok --config configs/config_simple.json
python harness_automation.py --harness grok --config configs/config_user.json
python harness_automation.py --harness grok --config configs/config_simple_eval.json
```


# Grok 工具调用捕获实现总结

## 实现方案

### 1. 数据结构扩展

#### 新增 ToolCall 数据类（第 32-38 行）
```python
@dataclass
class ToolCall:
    """一次 Grok 工具调用及其输入、输出和耗时（从 chat_history.jsonl 提取）。"""
    tool: str
    input: Any = ""
    output: Optional[str] = None
    duration_ms: Optional[int] = None
```

#### ExecutionResult 添加 tool_calls 字段（第 55 行）
```python
@dataclass
class ExecutionResult:
    # ... 其他字段 ...
    tool_calls: List[ToolCall] = field(default_factory=list)
```

同时更新了 `model_copy()` 方法（第 73 行）包含 `tool_calls` 字段。

### 2. 基于 prompt_index 的增量提取工具调用提取函数

实现了 `extract_tool_calls_grok()` 函数（第 97-176 行），核心逻辑：

1. **路径解析**：Grok 使用 URL 编码的 cwd 作为 session 存储路径
   - 例如：`~/.grok/sessions/%2Froot/<session-id>/chat_history.jsonl`
   - `/root` → `%2Froot`

2. **消息解析**：逐行读取 JSONL 格式的 chat_history，每条 user 消息都有 `prompt_index` 字段标识轮次：
   - `type: "assistant"` + `tool_calls` 数组：提取工具调用信息
   - `type: "tool_result"` + `tool_call_id`：匹配工具执行结果
```jsonl
{"type":"user","content":[...],"prompt_index":0}  // 第 1 轮
{"type":"assistant","tool_calls":[...]}
{"type":"tool_result",...}
{"type":"assistant","content":"..."}

{"type":"user","content":[...],"prompt_index":1}  // 第 2 轮
{"type":"assistant","tool_calls":[...]}
{"type":"tool_result",...}
{"type":"assistant","content":"..."}
```
通过识别**最后一个 prompt_index**，就能确定当前轮次的边界，只提取该轮次的工具调用。


3. **字段处理**：
   - `arguments` 字段可能是字符串或字典，统一解析为结构化数据
   - `content` 字段可能是列表，合并为字符串
   - 通过 `tool_call_id` 关联 tool_call 和 tool_result

4. **容错处理**：
   - 文件不存在时返回空列表
   - JSON 解析失败时跳过该行
   - 异常时记录警告日志并返回空列表

### 3. 集成到执行流程

在 `GrokAgent.execute()` 方法中（97-220行）：

第一遍：定位目标轮次
```python
last_prompt_index = None
with open(chat_history_path, "r") as f:
    for line in f:
        msg = json.loads(line)
        if msg.get("type") == "user" and "prompt_index" in msg:
            last_prompt_index = msg["prompt_index"]
```

第二遍：增量提取
```python
current_prompt_index = None
in_target_turn = False

with open(chat_history_path, "r") as f:
    for line in f:
        msg = json.loads(line)
        msg_type = msg.get("type")
        
        # 跟踪当前轮次
        if msg_type == "user" and "prompt_index" in msg:
            current_prompt_index = msg["prompt_index"]
            in_target_turn = (current_prompt_index == last_prompt_index)
        
        # 只处理目标轮次的消息
        if not in_target_turn:
            continue
        
        # 提取工具调用
        if msg_type == "assistant" and "tool_calls" in msg:
            # ...提取逻辑...
```


### 提取后的 ToolCall 对象

```python
ToolCall(
    tool="read_file",
    input={"target_file": "example.py"},
    output="file content here...",
    duration_ms=None  # Grok chat_history 不记录耗时
)
```