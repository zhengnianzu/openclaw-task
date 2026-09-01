# DeepSeek Harness (DSH) 集成说明

项目使用官方 `deepseek-harness-sdk`，通过 `src/dsh_client.py` 薄封装接入统一执行器。
runtime 支持 Linux x64 / arm64 与 macOS 14+ arm64；Windows 请在 WSL 运行。

## 1. 核心思路

- 直接调用官方 `DeepSeekHarness` Python SDK，不再包一层 Config。
- 每个 `(agent_name, session_name)` 一份独立 runtime，绑定独立 `cwd` 和 `sessionId`；
  同一键在一次运行内复用，跨 session 完全隔离。
- SDK 同步接口通过 `asyncio.to_thread` 接入异步执行器；`RunResult.final_response` 作为
  最终文本，`usage` 从 `assistant/message` 事件累加，tool_calls 从 `tool/call/*` 配对提取。
- 超时 / 取消 / 抛错一律折为 `ExecutionResult(success=False, ...)` 并关闭该 runtime；
  文件可能已被工具修改，故不自动重放。

`harness_automation.py` 通过 `harness_type: "dsh"` 路由到 `_run_dsh`，调用链固定为
`build_dsh_client → DshAgentManager.setup_agent → execute_dsh`，与其他 harness 对齐。

## 2. 配置来源

一次启动读两处，全部由 SDK 惯例决定路径（`DSH_HOME` 可覆盖，默认 `~/.dsh`）：

| 文件 | 作用 |
| --- | --- |
| `~/.dsh/settings.yaml` | `llm-pi-ai.providers`（provider adapter）+ `agent-default-model.{provider,model}`（全局兜底路由）+ `credentials`（`{ENV_NAME: value}`，由 provider 的 `apiKeyEnv` 引用） |
| `configs/deepseek_harness.cordis.yml` | 官方 Agent / provider adapter / JSONL session / skills / bash 工具的组合（项目内固定，`DSH_CORDIS_CONFIG` 可覆盖） |

`settings.yaml` 示例（provider 与密钥同文件，权限务必 `chmod 600`）：

```yaml
agent-default-model:
  provider: custom
  model: claude-opus-4-7

credentials:                    # ← 由 provider.apiKeyEnv 按名字引用
  CUSTOM_API_KEY: sk-...
  TMP_API_KEY: sk-...

llm-pi-ai:
  providers:
    custom:
      apiKeyEnv: CUSTOM_API_KEY    # ← 名字要与 credentials 里逐字一致
      api: anthropic-messages
      baseURL: http://host:8082/
      models:
        - id: claude-opus-4-7
    local-evaluator:
      apiKeyEnv: CUSTOM_API_KEY
      api: openai-completions
      baseURL: http://host:8082/v1
      models:
        - id: deepseek-v4-flash
```

密钥解析链路：`_load_credentials` 读出 `credentials` 段 → `_build_env` 把 `{ENV_NAME:
value}` 展开进 DSH runtime 子进程环境 → DSH 按 `apiKeyEnv` 名字在子进程 `os.environ`
里取真值放进请求头。

### 2.1 向后兼容：旧的 `.credentials.yaml`

历史部署在 `~/.dsh/.credentials.yaml` 里维护 `refs: {ENV_NAME: value}`。
`_load_credentials` 仍会兜底读该文件，两处都写时以 `settings.yaml.credentials` 为准
（同名 key 覆盖 `refs`，不同名 key 合并）。迁移建议：把 `refs` 内容搬进
`settings.yaml.credentials`，`chmod 600 ~/.dsh/settings.yaml`，验证跑通后删掉
`~/.dsh/.credentials.yaml`。

## 3. Agent 模型选路（三层 fallback）

`DshAgentManager.setup_agent` 按下面顺序拿 `(provider, model)`：

1. **`simulator_config` 同名 Agent（最高）**：`AgentModelConfig.provider` / `.model`；
   与 `agents[].model` 冲突时 warning 后仍以 override 为准。
2. **`agents[].model`**：支持 `provider/model` 简写（用 `/` 拆成 DSH 的两个独立 kwargs）。
3. **`settings.yaml` 默认路由**：`agent-default-model.{provider, model}`，最终兜底到
   `deepseek-official / deepseek-v4-flash`。

`provider` 与 `model` 独立判定：override 只写 `model` 时，`provider` 仍继续走 fallback。

```jsonc
// configs/user_proxy_model.json 片段（simulator_config）
{
  "main": { "provider": "local-evaluator", "model": "deepseek-v4-pro" },
  "user_simulator": { "model": "tokenfly-01/gemini-3.5-flash" }
}
```

同名 `main` 命中 override；`user_simulator` 段由 `harness_automation` 单独取给 User_simulator。

## 4. Workspace 与会话

- Agent workspace 模板：`~/.dsh/workspace/<agent>`（`main` 落在 base_dir，其余是
  `base_dir-<agent>`），`.agents/skills/` 存技能。
- 每个 session 独立 cwd：模板下的 `.sessions/<session_name>`，首次执行时从模板拷贝一份，
  避免多 session 互相污染。
- 官方 JSONL 会话写入 `~/.dsh/sessions/<agent>/<session_name>`。
- `system_prompt` 通过 `DSH_SYSTEM_PROMPT` 环境变量进入 cordis 里的 `agent-core.persona`。
- Evaluator 读取实际 session 目录，不读模板。

## 5. 非流式 bridge（`src/dsh_stream_bridge.py`）

`llm-pi-ai` adapter 消费标准 SSE。当某上游只能非流式返回、或流式协议不合规时，把该 provider
名加入 `build_dsh_client(nonstream=[...])`：

1. 本地 `127.0.0.1` 随机端口起 aiohttp server。
2. 拦截 DSH 请求，改写 `stream: false` 转发到原 baseURL。
3. 把非流式 JSON 包装成一个 SSE chunk 并补 `[DONE]` 返回。
4. `DshClient.close()` 时清理监听。

Bridge 只做协议转换，不碰 Agent / 工具 / workspace；`nonstream=[]` 时完全不启动。

## 6. 运行

```bash
python -m pip show deepseek-harness-sdk

python harness_automation.py --harness dsh --config configs/config_simple.json
python harness_automation.py --harness dsh --config configs/config_user.json
python harness_automation.py --harness dsh --config configs/config_simple_eval.json
```

CLI 的 `--harness dsh` 会覆盖配置文件里的 `harness_type` 字段。
