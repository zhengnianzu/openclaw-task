# DeepSeek Harness Python SDK 集成说明

项目使用 `deepseek-harness-sdk==0.1.1rc1`。官方 runtime 目前支持 Linux x64、
Linux arm64 和 macOS 14+ arm64；Windows 下应通过 WSL 运行真实任务。

## 1. 接入方式

- 项目直接使用官方 Python SDK，不重新实现 Agent loop、工具协议或会话存储；
- 每个 `(agent_name, session_name)` 使用独立 runtime、workspace 和 `sessionId`，
  同一键在当前任务内复用；
- SDK 是同步接口，项目通过 `asyncio.to_thread()` 接入异步执行器；
- 最终文本取 `RunResult.final_response`，usage 从官方事件中累加；
- 超时或取消时关闭对应 runtime；请求可能已经修改文件，因此失败后不自动重放。

runtime 使用项目内的 `src/deepseek_harness.cordis.yml`，加载官方 Agent、
provider adapter、JSONL session、workspace instructions、skills 和 bash 工具。

## 2. 配置

服务、模型和明文密钥统一写入 WSL 的
`~/.deepseek-harness/config.yml`。一个文件可以配置多个 provider 和多个模型：

```yaml
model: deepseek-v4-flash
provider: primary-gateway
tools: true

# 仅列出不能返回标准 SSE 的 provider；正常服务保持空列表。
nonstream: []

providers:
  primary-gateway:
    displayName: Primary DeepSeek Gateway
    api: openai-completions
    baseURL: https://primary.example.com/v1
    headers:
      Authorization: Bearer primary-key
    models:
      - id: deepseek-v4-flash
        contextWindow: 128000
        maxTokens: 8192
      - id: deepseek-v4-pro
        contextWindow: 128000
        maxTokens: 8192

  backup-gateway:
    displayName: Backup DeepSeek Gateway
    api: openai-completions
    baseURL: https://backup.example.com/v1
    headers:
      Authorization: Bearer backup-key
    models:
      - id: deepseek-v4-flash
        contextWindow: 128000
        maxTokens: 8192
```

`providers` 使用官方 `llm-pi-ai` 字段并原样传入，不转换 provider、模型或密钥。
顶层 `provider` 和 `model` 是默认值；任务中的 `agents[].model` 可用
`provider/model` 选择其他服务，例如：

```json
{
  "name": "main",
  "model": "backup-gateway/deepseek-v4-flash",
  "system_prompt": "你是一个编码助手。"
}
```

DeepSeek Agent 不读取 `configs/user_proxy_model.json` 中的模型配置；该文件只负责
公共的 `user_simulator`。DeepSeek 的 provider、模型、地址和密钥均来自上述 YAML。

## 3. Workspace 与会话

- Agent 模板位于 `~/.deepseek-harness/workspace/<agent>`；
- 实际执行目录位于模板下的 `.sessions/<运行时 session 名>`，不同 session 使用
  不同 cwd；
- session 名进入目录和官方 `sessionId` 前会转换为单个路径片段，避免 `/`
  等字符产生额外目录层级；
- JSONL 会话默认写入
  `~/.deepseek-harness/sessions/<agent>/<运行时 session 名>`；
- `system_prompt`、Agent 配置和 `.agents/skills` 会进入对应 session；
- Evaluator 读取最近执行的实际 session workspace，不读取 Agent 模板目录。

## 4. 非流式兼容 bridge

### 4.1 为什么需要

DeepSeek Harness 的 `llm-pi-ai` adapter 按标准流式 Chat Completions 消费 SSE。
当前自建服务在非流式模式下可正常返回 JSON，但其流式响应不满足 DSH 所需的标准
SSE 结束语义，可能导致 DSH 一直等待或无法正常结束 turn。Codex、Grok 能直接
使用该服务，并不能说明 DSH 的流式消费链路也兼容。

因此，使用当前服务的 provider 必须加入顶层 `nonstream`。这也是
`config_simple_eval.json` 需要 bridge 的原因；问题发生在模型传输协议层，与
Evaluator 逻辑无关。

### 4.2 bridge 做什么

`src/deepseek_stream_bridge.py` 只做一次协议转换：

1. 在当前进程的 `127.0.0.1` 随机端口监听；
2. 将 DSH 请求改为 `stream: false` 后转发给原 provider；
3. 将返回的 Chat Completions JSON 包装为一个标准 SSE chunk，并补充 `[DONE]`；
4. DeepSeek 客户端退出时关闭监听。

bridge 不实现 Agent、工具调用或会话管理，也不读写 workspace，因此不会改变
现有的 session cwd 隔离。它只影响 `nonstream` 中 provider 的 HTTP 传输；能
正确返回标准 SSE 的服务应保持 `nonstream: []`，此时 bridge 完全不启动。

## 5. 运行与验证

在 WSL 项目目录运行：

```bash
python -m pip show deepseek-harness-sdk

python harness_automation.py --harness deepseek --config configs/config_simple.json
python harness_automation.py --harness deepseek --config configs/config_user.json
python harness_automation.py --harness deepseek --config configs/config_simple_eval.json

python test/test_deepseek_client.py
```

当前基础验证包括：

- 离线回归测试 14 项全部通过；其中 bridge 测试确认请求改为非流式，响应包含
  标准 SSE 数据和 `[DONE]`；
- WSL 真实服务下，上述三个配置均运行通过；`config_simple_eval.json` 验证了
  bridge、主 Agent 和 Evaluator 的完整链路。
