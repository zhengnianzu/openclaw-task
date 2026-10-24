# JiuwenSwarm 集成说明

本文说明 OpenClaw Harness 如何通过 `src/jiuwenswarm_client.py` 接入 JiuwenSwarm，并重点说明如何在**不修改 JiuwenSwarm 源码、也不修改 `jiuwenswarm_client.py`** 的情况下，通过 JiuwenSwarm 原生 `config.yaml` 装配模型、MCP Server、权限和沙箱配置。

本文基于以下版本整理：

- JiuwenSwarm 源码：[openJiuwen/jiuwenswarm](https://gitcode.com/openJiuwen/jiuwenswarm.git)
- 校验提交：`9f21b447874e83f3aff1646b5f9ae70213d33125`
- 对应分支：`dev-stable`
- Python 包版本：`jiuwenswarm==0.2.3`
- Python：3.11～3.13

> 后续版本如果调整了配置字段、TUI 协议或历史记录格式，应以对应版本源码为准。

## 1. 集成架构

```text
OpenClaw Harness
    │ WebSocket: chat.send / chat.*
    ▼
JiuwenSwarm TUI Gateway
    ├── 读取 ~/.jiuwenswarm/config/config.yaml
    ├── 创建并运行 Agent
    ├── 装配启用的 MCP Server
    └── 保存会话历史与用量
```

| 组件 | 职责 |
| --- | --- |
| `src/jiuwenswarm_client.py` | 连接 TUI Gateway、创建/复用 Agent、发送查询、消费流式事件、管理 Harness 会话 |
| JiuwenSwarm TUI Gateway | 加载 JiuwenSwarm 配置并实际运行 Agent、模型和 MCP 工具 |
| JiuwenSwarm `config.yaml` | 装配模型、MCP Server、权限、沙箱和启动参数 |
| `scripts/harness_automation.py` | 根据任务中的 `harness_type` 调用 JiuwenSwarm 客户端 |
| `scripts/harness_adapter.py` | 从 JiuwenSwarm 历史记录中提取轨迹、工具调用和 Token 用量 |

`jiuwenswarm_client.py` 不导入 JiuwenSwarm Python 包、不读取 JiuwenSwarm `config.yaml`、不创建 `McpServerConfig`，也不把 MCP 配置附加到 `chat.send`。它只通过 WebSocket 与已启动的 TUI Gateway 通信。因此，MCP 应由 **JiuwenSwarm 服务端的原生配置文件**装配。

## 2. 安装与启动

按固定提交安装：

```bash
python -m pip install \
  "git+https://gitcode.com/openJiuwen/jiuwenswarm.git@9f21b447874e83f3aff1646b5f9ae70213d33125"
jiuwenswarm-init
jiuwenswarm-start app
```

默认情况下，客户端连接 `ws://127.0.0.1:19001/tui`。若 JiuwenSwarm 与 Harness 不在同一容器或网络命名空间，需改成 Harness 可访问的主机名和端口。

推荐把配置目录挂载到默认位置：

```bash
docker run --rm \
  -p 19001:19001 \
  -v /host/path/jiuwenswarm-config:/root/.jiuwenswarm/config \
  <jiuwenswarm-image>
```

挂载目录中应包含 `config.yaml`。也可使用自定义目录：

```bash
export JIUWENSWARM_CONFIG_DIR=/opt/jiuwenswarm-config
jiuwenswarm-start app
```

注意：

- 环境变量名是 `JIUWENSWARM_CONFIG_DIR`；
- 它指向**包含 `config.yaml` 的目录**，不是 YAML 文件本身；
- 未设置时，默认配置文件是 `~/.jiuwenswarm/config/config.yaml`；
- 用户配置会与 JiuwenSwarm 内置模板合并，因此可以只保留需要覆盖的字段。

## 3. JiuwenSwarm 原生配置

### 3.1 模型

```yaml
version: "1.0"

models:
  default:
    model_name: "your-model-name"
    api_key: "${MODEL_API_KEY}"
    api_base: "https://your-model-endpoint.example.com/v1"
    temperature: 0.2
    max_tokens: 8192
```

如果现有 `jw_config.yaml` 已能正常调用模型，可原样保留其 `models` 部分，只追加 `mcp` 配置。密钥建议通过环境变量注入，不要写入仓库或镜像层。

### 3.2 MCP Server

JiuwenSwarm `dev-stable` 支持在顶层 `mcp.servers` 中声明 MCP Server。服务启动时会把所有 `enabled: true` 的条目转换为内部 `McpServerConfig`，无需修改源码。

```yaml
mcp:
  servers:
    # stdio：由 JiuwenSwarm 拉起本地 MCP 进程
    - name: "filesystem"
      enabled: false
      transport: "stdio"
      command: "npx"
      args:
        - "-y"
        - "@modelcontextprotocol/server-filesystem"
        - "/workspace"
      cwd: "/workspace"
      env:
        NODE_ENV: "production"
      timeout_s: 30

    # SSE：连接远端 MCP Server
    - name: "remote-sse"
      enabled: false
      transport: "sse"
      url: "https://mcp.example.com/sse"
      headers:
        Authorization: "Bearer ${MCP_TOKEN}"
      timeout_s: 30

    # Streamable HTTP
    - name: "remote-http"
      enabled: false
      transport: "streamable-http"
      url: "https://mcp.example.com/mcp"
      headers:
        Authorization: "Bearer ${MCP_TOKEN}"
      timeout_s: 30
```

| 字段 | 说明 |
| --- | --- |
| `name` | MCP Server 的唯一名称 |
| `enabled` | 只有 `true` 的条目会被装配 |
| `transport` | `stdio`、`sse` 或 `streamable-http` |
| `command` / `args` | `stdio` 模式下的命令及参数 |
| `cwd` / `env` | `stdio` 进程的工作目录及环境变量 |
| `url` / `headers` | 远端 MCP 地址及请求头 |
| `timeout_s` | 连接或调用超时，单位为秒 |

启用时把对应条目改为 `enabled: true`，设置所需环境变量并重启 JiuwenSwarm。对于 `stdio`，命令和运行时必须安装在 **JiuwenSwarm 服务所在环境**，只装在 Harness 容器中不会生效。

### 3.3 权限、沙箱与启动项

```yaml
preferred_language: "zh-CN"

permissions:
  enabled: false

sandbox:
  enabled: false

startup:
  check_update: false
```

`permissions.enabled: false` 表示关闭 JiuwenSwarm 的权限拦截流程。由于客户端在 `chat.send` 中声明 `supports_user_interaction: false`，非交互式 Harness 场景应提前配置好权限策略，避免任务执行时等待人工确认。沙箱是否启用应按部署环境的安全要求决定。

## 4. OpenClaw 客户端配置

`build_jiuwenswarm_client()` 只读取：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `JIUWENSWARM_GATEWAY_URL` | `ws://127.0.0.1:19001/tui` | TUI Gateway WebSocket 地址 |
| `JIUWENSWARM_MODE` | `code.normal` | JiuwenSwarm Agent 模式 |

```bash
export JIUWENSWARM_GATEWAY_URL=ws://jiuwenswarm:19001/tui
export JIUWENSWARM_MODE=code.normal
```

团队模式可使用以 `team` 开头的模式，或当前客户端识别的 `code.team`、`agent.team`；是否可运行仍取决于服务端版本和配置。

## 5. Harness 任务配置

建议统一使用规范名称 `jiuwenswarm`：

```json
{
  "agent_name": "main",
  "session_name": "default",
  "harness_type": "jiuwenswarm",
  "model_name": "your-model-name",
  "system_prompt": "你是一个软件工程助手。",
  "query": "分析并修复当前任务。",
  "project_dir": "/workspace/project",
  "config_files": ["/workspace/project/AGENTS.md"],
  "skills": ["/workspace/skills/example-skill"],
  "timeout": 3600
}
```

- `agent_name` 与 `session_name` 共同组成 Agent 缓存键；
- `model_name` 非空时会附加到 `chat.send`；
- `project_dir` 同时作为 `cwd`、`project_dir`、`workspace_dir` 发送，并加入 `trusted_dirs`；
- `timeout` 默认为 3600 秒；
- `jiuwenswarm-dev-stable` 虽可被部分分发代码识别，但工作区管理等逻辑以 `jiuwenswarm` 为规范值，不建议新任务使用别名。

## 6. Prompt、工作区与会话

某个 Agent 的第一次查询会依次拼接：

1. `system_prompt`；
2. `config_files` 文件内容；
3. 每个 skill 目录下的 `SKILL.md`；
4. 当前用户查询。

后续查询只发送当前 query。默认主工作区为 `~/.jiuwenswarm/agent/jiuwenclaw_workspace`，其他 Agent 使用带名称后缀的目录；显式传入 `project_dir` 时则使用该目录。

会话 ID 格式为 `harness-<uuid>`。`clear_session()` 会生成新 ID、清除首轮注入标记，使下次查询重新注入系统提示、配置文件和技能说明。客户端使用全局异步锁串行执行查询。

## 7. WebSocket 协议

客户端发送的 `chat.send` 主要字段：

```json
{
  "type": "chat.send",
  "content": "<prompt>",
  "query": "<prompt>",
  "cwd": "/workspace/project",
  "project_dir": "/workspace/project",
  "workspace_dir": "/workspace/project",
  "trusted_dirs": ["/workspace/project"],
  "supports_user_interaction": false,
  "agent_ref": "<agent>",
  "model_name": "<optional>"
}
```

| 事件 | 处理方式 |
| --- | --- |
| `chat.delta` | 累积并输出流式文本 |
| `chat.llm_usage` | 记录模型和 Token 用量 |
| `chat.tool_call` | 记录工具调用 |
| `chat.tool_result` | 记录工具结果 |
| `chat.processing_status` | 更新处理状态 |
| `chat.final` | 返回最终结果 |
| 错误事件 | 抛出异常并进入重试 |

失败时最多重试 5 次，每次等待 60 秒并重置会话。

## 8. 运行与验收

推荐启动顺序：

1. 准备 JiuwenSwarm `config.yaml`；
2. 设置模型和 MCP 所需环境变量；
3. 启动 TUI Gateway；
4. 确认 `19001` 端口和 `/tui` WebSocket 可达；
5. 设置 Harness 的 `JIUWENSWARM_GATEWAY_URL`；
6. 使用 `harness_type: jiuwenswarm` 执行任务。

MCP 验收顺序：

1. 先用普通查询确认模型调用成功；
2. 查看服务启动日志，确认启用的 MCP Server 已加载；
3. 对 `stdio` 服务，在 JiuwenSwarm 环境中检查 `command` 是否可执行；
4. 对远端服务，检查 URL、证书、网络和认证头；
5. 发起明确要求使用该 MCP 工具的查询；
6. 检查事件或历史记录中是否出现 `chat.tool_call` 与 `chat.tool_result`。

若模型正常但没有 MCP 工具，优先检查 `enabled`、`transport`、配置目录、服务重启、stdio 依赖、远端地址和请求头。

## 9. 轨迹与统计

`scripts/harness_adapter.py` 已包含 `JiuwenSwarmAdapter`，并非通用 fallback。适配器从以下目录查找会话历史：

```text
~/.jiuwenswarm/service_*/agent_*/agent/sessions/<session_id>/history.json
```

当同一会话 ID 出现在多个服务目录时，适配器结合 `metadata.project_dir` 区分任务，并从历史记录中统计 LLM 调用、Token 用量、工具调用数量、参数、结果和最终轨迹。

若统计缺失，应确认 JiuwenSwarm 已写入 `history.json`、Harness 保存了正确的 `session_id`、执行用户拥有读取权限，且服务端历史格式与本文基于的提交一致。

## 10. 已知边界

- MCP 配置由服务端读取；修改 Harness 任务 JSON 不会动态增删 MCP Server。
- 当前客户端不支持交互式权限确认。
- 客户端会将查询串行化，不适合作为单进程高并发网关。
- 会话历史目录和事件字段与版本有关，升级后应重新做端到端验收。
- 分容器部署时，需分别处理配置挂载、目录映射、MCP 依赖和网络可达性。

## 11. 最小 MCP 增量

已有模型配置时，可只把下面内容合并到现有 `jw_config.yaml` 顶层：

```yaml
# JiuwenSwarm 会在内部将下面已启用的条目转换为 McpServerConfig
mcp:
  servers:
    # Playwright MCP (stdio)
    - name: playwright
      enabled: false
      transport: stdio
      command: npx
      args:
        - "@playwright/mcp@0.0.79"
        - --browser
        - chromium
      timeout_s: 300

    # Image-reading MCP (stdio)
    - name: image-reading
      enabled: false
      transport: stdio
      command: node
      args:
        - /opt/mcp/image-reading/index.js
      env:
        VL_API_BASE: https://your-vl-gateway.example.com/v1
        VL_API_KEY: "${VL_API_KEY}"
        VL_MODEL: your-vl-model
      timeout_s: 300

    # GitHub MCP (stdio)
    - name: github
      enabled: false
      transport: stdio
      command: npx
      args:
        - -y
        - "@modelcontextprotocol/server-github"
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_PERSONAL_ACCESS_TOKEN}"
      timeout_s: 300

    # Remote Streamable HTTP MCP
    - name: remote-streamable
      enabled: false
      transport: streamable-http
      url: https://mcp.example.com/mcp
      headers:
        Authorization: "Bearer ${REMOTE_MCP_TOKEN}"
      timeout_s: 60

    # Legacy SSE MCP
    - name: remote-sse
      enabled: false
      transport: sse
      url: https://mcp.example.com/sse
      headers:
        Authorization: "Bearer ${REMOTE_MCP_TOKEN}"
      timeout_s: 60
```

确保它与 `models`、`permissions`、`sandbox`、`startup` 等字段同级，然后将文件作为 JiuwenSwarm 实际读取的 `config.yaml` 并重启服务。