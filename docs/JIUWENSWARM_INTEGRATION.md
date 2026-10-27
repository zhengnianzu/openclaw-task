# JiuwenSwarm 双版本集成说明

本文说明 OpenClaw Task/Hive 如何同时接入 JiuwenSwarm `dev-stable` 和
`xiaoyi_0.2.4.beta3`。两个版本共用 `src/jiuwenswarm_client.py` 和 `/tui`
WebSocket 协议，通过 `harness_type` 选择各自的镜像、配置模板、工作区和轨迹解析。
源码仓库为 [openJiuwen/jiuwenswarm](https://gitcode.com/openJiuwen/jiuwenswarm)。

不要再使用通用的 `jiuwenswarm` 或额外的 `jiuwenswarm_variant` 字段。规范值只有：

- `jiuwenswarm-dev`
- `jiuwenswarm-xiaoyi`

## 1. 版本与镜像

| Harness | 源码基线 | Python 包 | 当前基础镜像 |
| --- | --- | --- | --- |
| `jiuwenswarm-dev` | `dev-stable`；已校验提交 `9f21b447874e83f3aff1646b5f9ae70213d33125` | `0.2.3` | `swr.cn-east-4.myhuaweicloud.com/rl_team/jiuwenswarm:dev-stable-2026.10.23v1` |
| `jiuwenswarm-xiaoyi` | `xiaoyi_0.2.4.beta3` | 以固定镜像内版本为准 | `swr.cn-east-4.myhuaweicloud.com/rl_team/jiuwenswarm:xiaoyi-2026.10.26` |

生产和批量评测应优先使用上表的固定镜像，避免分支移动导致配置或轨迹格式发生变化。
需要从源码安装时可使用：

```bash
# dev-stable：固定到已校验提交
python -m pip install \
  "git+https://gitcode.com/openJiuwen/jiuwenswarm.git@9f21b447874e83f3aff1646b5f9ae70213d33125"

# xiaoyi：按分支安装；生产使用前建议再替换为实际验证过的 commit SHA
python -m pip install \
  "git+https://gitcode.com/openJiuwen/jiuwenswarm.git@xiaoyi_0.2.4.beta3"
```

## 2. 总体架构

```text
task config: harness_type
        │
        ├── jiuwenswarm-dev ────── dev-stable 镜像/配置/工作区/轨迹适配器
        │
        └── jiuwenswarm-xiaoyi ─── xiaoyi 镜像/配置/工作区/轨迹适配器
                                      │
                                      ▼
                            JiuwenSwarm /tui Gateway
                                      ▲
                                      │ WebSocket
                           src/jiuwenswarm_client.py
```

| 组件 | 职责 |
| --- | --- |
| `harness_automation.py` | 只根据 `harness_type` 选择工作区并进入 JiuwenSwarm 执行流程 |
| `src/jiuwenswarm_client.py` | 共用客户端；连接 `/tui`、发送 `chat.send`、消费流式事件和管理会话 |
| JiuwenSwarm `config.yaml` | 配置模型、MCP、权限、沙箱和分支特有能力 |
| `scripts/harness_adapter.py` | 根据 `harness_type` 选择对应的轨迹位置与解析格式 |
| Hive `prepare_config.py` | 选择配置模板、写入模型并把配置上传到沙箱目标路径 |

客户端不导入 JiuwenSwarm Python 包，也不直接解析服务端 `config.yaml`。模型、MCP、
权限和沙箱均由对应镜像内的 JiuwenSwarm 服务加载。

## 3. 两个版本的关键差异

| 项目 | `jiuwenswarm-dev` | `jiuwenswarm-xiaoyi` |
| --- | --- | --- |
| 基础镜像 | `...:dev-stable-2026.10.23v1` | `...:xiaoyi-2026.10.26` |
| Hive 本地配置模板 | `uploads/jw_dev_config.yaml` | `uploads/jw_xiaoyi_config.yaml` |
| 沙箱配置目标 | `~/.jiuwenswarm/config/config.yaml` | `~/.jiuwenswarm/config/config.yaml` |
| 主 Agent 工作区 | `~/.jiuwenswarm/agent/jiuwenclaw_workspace` | `~/.jiuwenswarm/agent/workspace` |
| 其他 Agent 工作区 | `.../jiuwenclaw_workspace-<agent>` | `.../workspace-<agent>` |
| 主要日志/轨迹根目录 | `~/.jiuwenswarm/service_default/...` | `~/.jiuwenswarm/logs`、`~/.jiuwenswarm/agent/...` |
| 会话轨迹 | `service_*/agent_*/agent/sessions/<id>/history.json` | `agent/sessions/<id>/history.jsonl` |
| 轨迹内容 | `event_type` 事件流，usage 常在 `metadata.usage_metadata` | 按消息逐行保存，含 `role`、`event_type`，最终 usage 常在顶层 `usage` |
| 配置规模 | 主要为 `models`、`permissions`、`sandbox` 等基础段 | 另含 `agents`、`gateway`、`modes`、`mcp`、hooks/扩展等能力 |

两者不能通过“先找哪个目录存在”来猜版本。沙箱中可能残留旧目录，必须以
`harness_type` 为唯一分支依据。

## 4. Hive 配置

### 4.1 dev-stable

```yaml
sandbox:
  x86_cpu:
    sandbox:
      image: swr.cn-east-4.myhuaweicloud.com/rl_team/jiuwenswarm:dev-stable-2026.10.23v1

run_config:
  harness_type: jiuwenswarm-dev
  sandbox:
    openclaw_local_config_file: uploads/jw_dev_config.yaml
```

### 4.2 xiaoyi

```yaml
sandbox:
  x86_cpu:
    sandbox:
      image: swr.cn-east-4.myhuaweicloud.com/rl_team/jiuwenswarm:xiaoyi-2026.10.26

run_config:
  harness_type: jiuwenswarm-xiaoyi
  sandbox:
    openclaw_local_config_file: uploads/jw_xiaoyi_config.yaml
```

两份模板最终都上传到：

```text
/home/ma-user/.jiuwenswarm/config/config.yaml
```

不要让 `jiuwenswarm-dev` 使用 Xiaoyi 的完整配置模板，反之亦然。Xiaoyi 配置包含更多
顶层模块，dev-stable 不一定识别；直接共用模板会把镜像差异变成启动期配置错误。

## 5. 模型配置

两个版本的共同模型入口均为 `models.defaults`，因此 Hive 可以共用模型注入逻辑。
每个 Agent 使用独立 `alias`，`agents[].model` 由客户端作为 `model_name` 透传，服务端
再按 alias 或实际模型名解析。

```yaml
models:
  defaults:
    - alias: main
      is_default: true
      model_client_config:
        model_name: your-model-name
        client_provider: OpenAI
        api_base: https://your-model-endpoint.example.com/v1
        api_key: ${MODEL_API_KEY}
        timeout: 1800
        verify_ssl: true
      model_config_obj: {}
```

注意：

- `client_provider` 应与 API 协议一致，例如 `OpenAI` 或 `Anthropic`；
- `api_base` 是否带 `/v1` 应由 provider 规则统一处理；
- 不要把密钥提交到仓库或烘焙进公共镜像；
- 两个版本的可选模型参数不完全相同，应保留各自模板中的已有字段；
- 不要把 `context_window` 注入到会直接透传给 OpenAI SDK 的 `extra_params`。

## 6. MCP、权限与沙箱

MCP 由 JiuwenSwarm 服务端配置，不由 `chat.send` 动态传入。本文采用的 dev-stable
基线支持顶层 `mcp.servers`，Xiaoyi 模板也明确包含该配置段；但两个镜像支持的字段、
transport 和内置工具可能不同，应以各自配置 schema 与启动日志为准，不要直接共用
完整 MCP 模板。

Xiaoyi 示例：

```yaml
mcp:
  servers:
    - name: filesystem
      enabled: false
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
      timeout_s: 30

    - name: remote-http
      enabled: false
      transport: streamable-http
      url: https://mcp.example.com/mcp
      headers:
        Authorization: "Bearer ${MCP_TOKEN}"
      timeout_s: 30
```

`stdio` 命令及依赖必须存在于 JiuwenSwarm 沙箱内。非交互式 Harness 会发送
`supports_user_interaction: false`，因此权限策略必须在运行前配置完成，避免执行中等待
人工审批。权限和沙箱字段应分别基于 `jw_dev_config.yaml` 与
`jw_xiaoyi_config.yaml` 修改。

## 7. Task 侧配置和工作区

任务配置只需要选定 harness：

```json
{
  "harness_type": "jiuwenswarm-dev",
  "agents": [
    {
      "name": "main",
      "model": "main",
      "system_prompt": "你是一个软件工程助手。"
    }
  ],
  "queries": []
}
```

切换 Xiaoyi 时只把 `harness_type` 改为 `jiuwenswarm-xiaoyi`，并确保 Hive 同时选择
Xiaoyi 镜像和 `jw_xiaoyi_config.yaml`。不要增加 `jiuwenswarm_variant`、
`server_variant` 等重复字段。

工作区规则：

```text
jiuwenswarm-dev
  main       ~/.jiuwenswarm/agent/jiuwenclaw_workspace
  evaluator  ~/.jiuwenswarm/agent/jiuwenclaw_workspace-evaluator

jiuwenswarm-xiaoyi
  main       ~/.jiuwenswarm/agent/workspace
  evaluator  ~/.jiuwenswarm/agent/workspace-evaluator
```

`AGENT.md`、`SOUL.md`、`USER.md`、`IDENTITY.md` 及项目技能由 Task 侧复制到上述
Agent 工作区。客户端首次查询显式注入这些配置与 `SKILL.md`，后续查询只发送当前
query。

## 8. 客户端和 WebSocket 协议

两个分支共用以下环境变量：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `JIUWENSWARM_GATEWAY_URL` | `ws://127.0.0.1:19001/tui` | TUI Gateway 地址 |
| `JIUWENSWARM_MODE` | `code.normal` | Agent 运行模式 |

```bash
export JIUWENSWARM_GATEWAY_URL=ws://127.0.0.1:19001/tui
export JIUWENSWARM_MODE=code.normal
```

客户端发送的核心请求为：

```json
{
  "type": "req",
  "method": "chat.send",
  "is_stream": true,
  "params": {
    "session_id": "harness-<uuid>",
    "mode": "code.normal",
    "content": "<prompt>",
    "query": "<prompt>",
    "cwd": "<branch-specific-workspace>",
    "project_dir": "<branch-specific-workspace>",
    "workspace_dir": "<branch-specific-workspace>",
    "trusted_dirs": ["<branch-specific-workspace>"],
    "supports_user_interaction": false,
    "agent_ref": {"mode": "code.normal", "id": "default"},
    "model_name": "<optional-agent-alias>"
  }
}
```

共用客户端处理 `connection.ack`、`chat.delta`、`chat.llm_usage`、
`chat.tool_call`、`chat.tool_result`、`chat.final`、`chat.processing_status` 和错误事件。
若某个镜像改变事件包裹方式，应在客户端做字段兼容，不应通过增加 variant 字段处理。

## 9. 轨迹统计

`task_status.py` 的统计流程可以共用，版本差异应封装在
`scripts/harness_adapter.py`：

### 9.1 dev-stable

```text
~/.jiuwenswarm/service_*/agent_*/agent/sessions/<session_id>/
├── metadata.json
└── history.json
```

- 用 `metadata.project_dir` 的末级目录区分 main/evaluator；
- 主工作区末级名为 `jiuwenclaw_workspace`；
- `chat.tool_call` 统计工具调用；
- `chat.usage_metadata` 的用量通常位于 `metadata.usage_metadata`。

### 9.2 Xiaoyi

```text
~/.jiuwenswarm/agent/sessions/<session_id>/history.jsonl
```

- 每行是一条消息，包含 `role`，Assistant 事件还可包含 `event_type`；
- 主工作区末级名为 `workspace`；
- `event_type: chat.tool_call` 的工具信息位于 `tool_call` 等顶层字段；
- 最终 Assistant 行可带顶层 `usage`，包括 `input_tokens`、`output_tokens`、
  `total_tokens` 和 `cache_tokens`；
- 不应假设存在 dev-stable 的 `metadata.json` 或嵌套 `metadata.usage_metadata`。

适配器注册关系应为：

```text
jiuwenswarm-dev     -> dev 轨迹定位/解析
jiuwenswarm-xiaoyi  -> xiaoyi 轨迹定位/解析
```

是否保留 `jiuwenswarm-dev-stable` 旧别名由兼容策略决定；新任务和 Hive 配置统一使用
`jiuwenswarm-dev`。通用 `jiuwenswarm` 不应再用于新任务。

## 10. 启动与验收

建议按以下顺序验收每个镜像：

1. 核对 `harness_type`、镜像和配置模板三者属于同一版本；
2. 将对应模板上传到 `~/.jiuwenswarm/config/config.yaml`；
3. 启动 JiuwenSwarm，并确认 `19001/tui` 可连接且收到 `connection.ack`；
4. 用普通查询验证模型 alias、provider、base URL 和密钥；
5. 用工具型查询验证 `chat.tool_call` 与 `chat.tool_result`；
6. 分别运行 main 和 evaluator，确认工作区没有串用；
7. 检查对应版本的轨迹目录和文件名；
8. 执行轨迹统计，核对轮次、工具调用和 Token 是否与原始文件一致。

常见问题：

| 现象 | 优先检查 |
| --- | --- |
| 任务进入 OpenClaw 默认流程 | `harness_type` 拼写错误或仍使用通用 `jiuwenswarm` |
| 模型找不到 | `agents[].model` 与 `models.defaults[].alias` 不一致 |
| 服务启动即配置报错 | 镜像与 `jw_dev_config.yaml`/`jw_xiaoyi_config.yaml` 混用 |
| main/evaluator 文件互相覆盖 | 工作区基名没有按 harness 分支 |
| dev 有回复但无统计 | 是否读取 `service_*/agent_*/agent/sessions/*/history.json` |
| Xiaoyi 有回复但无统计 | 是否误按 dev 的 `history.json` 和 metadata 结构解析 |
| MCP 未加载 | `enabled`、transport、stdio 依赖、远端网络和启动日志 |

## 11. 兼容边界

- 一份 `jiuwenswarm_client.py` 可以兼容两个分支，因为在线 `/tui` 协议主体一致；
- 工作区、配置模板和轨迹格式不能共用硬编码路径，必须按 `harness_type` 分支；
- `task_status.py` 可保持通用，差异放在轨迹 Adapter；
- 镜像升级后必须重新校验 config schema、WebSocket 事件和历史文件格式；
- 分容器部署时还需确认配置挂载、工作区映射、MCP 依赖和网络可达性；
- 当前客户端串行执行查询，以兼容仅允许单 TUI 客户端的服务版本，不适合作为单进程
  高并发网关。
