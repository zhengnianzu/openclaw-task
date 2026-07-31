# OpenCode 接入方案对比与演进建议

> - 审查日期：2026-07-31
> - 审查对象：`feat/opencode-harness` 分支中的 OpenCode harness 实现
> - 本机 OpenCode 版本：`1.18.10`

## 1. 结论

当前基于 `opencode run --format json` 的方案**方向合理，适合作为第一版 MVP**，不需要为了“看起来更正式”立即推倒重写。

OpenCode 官方将 `run` 定位为非交互脚本和自动化入口，并公开支持 `--format json`、`--session`、`--model`、`--agent`、`--dir` 和 `--attach`。因此，当前实现使用的是公开能力，不是依赖内部代码的临时方案。

但是，当前实现更适合本地、低并发、少量串行任务，还不能直接视为严格评测或长期生产级接入。主要风险不是“能不能得到回答”，而是：

- Evaluator 声称每轮重置，但包括 OpenCode 在内的无 gateway 客户端实际不会重置，会继承上一轮判词。
- `system_prompt` 只是拼进普通用户消息，角色语义不正确。
- OpenCode 已产生的工具调用事件没有进入评测轨迹。
- Agent 与 User Simulator 实际使用两套不同的配置解析规则。
- User Simulator 关闭了 TLS 证书校验，真实 API key 可能被中间人窃取。
- 复用用户全部 OpenCode 配置很方便，但严格评测时会引入全局插件、MCP、权限、自动分享和版本差异。
- 每轮启动一个新的 CLI 进程，规模增大后会出现冷启动、全量缓冲和会话清理问题。

结合“尽可能少的代码”这一原则，推荐路线是：

1. **现在保留当前 CLI 方案**，先解决影响正确性的高优先级问题。
2. **近期增加可选的 `opencode run --attach`**，连接外部长期运行的 `opencode serve`；未配置 Server 时仍使用当前模式。
3. **中期按需求迁移为 Python 直接调用 OpenCode HTTP/OpenAPI + SSE**；可以手写少量端点，官方 alpha Python SDK 经兼容性验证后也可作为候选。
4. 需要统一接入多种智能体，或明确需要标准化增量事件、取消和权限协议时，再考虑 ACP；当前不应优先引入 TypeScript sidecar。

## 2. 当前实现的实际调用链

```text
HarnessAutomation
  ├─ Agent / Evaluator
  │    └─ OpenCodeClient
  │         └─ 每次 execute 启动一个 opencode run 子进程
  │              ├─ --format json
  │              ├─ --dir <agent workspace>
  │              ├─ --model <provider/model>（可选）
  │              └─ --session <真实 OpenCode sessionID>（后续轮次）
  │
  └─ User Simulator
       └─ load_opencode_simulator_config()
            └─ 提取 OpenCode JSON 中的 baseURL / apiKey / model
                 └─ 仍由现有 OpenAI Python SDK 直接调用模型服务
```

这里需要特别区分：

- Agent 和 Evaluator 确实由 OpenCode CLI 执行。
- User Simulator **不是**由 OpenCode 执行，只是尝试读取部分 OpenCode 配置，然后直接调用 OpenAI-compatible API。

## 3. 与 Hermes 参考实现的符合程度

| 维度 | Hermes | 当前 OpenCode 实现 | 评价 |
|---|---|---|---|
| 对外接口 | `Client / Agent / WorkspaceManager / AgentManager` | 保持同样的抽象和调度方式 | 合适，减少了上层改动 |
| 启动方式 | Python 进程内创建 `AIAgent` | 每轮启动 `opencode run` | 接口一致、运行机制不同；第一版可以接受 |
| 配置 | Hermes 自己加载 profile/model 配置 | 核心 Agent 由 OpenCode CLI 读取其自身配置和认证 | 合适，避免把密钥写入项目 |
| Workspace | 每个 Agent 使用持久 profile/workspace | `~/.opencode/workspace/<agent>` | 符合 Hermes 语义；严格评测可再提供可选隔离模式 |
| Skills | Hermes 对应目录 | `.opencode/skills/<name>/SKILL.md` | 符合 OpenCode 约定 |
| Persona/规则 | Hermes 原生 system/profile | 汇总为 workspace 根目录 `AGENTS.md` | 基本可用 |
| 会话 | 每个 `(agent, session)` 独立实例 | 缓存真实 `sessionID`，后续传 `--session` | 基本正确，但映射只存在内存 |
| Agent 语义 | 项目 Agent 对应 Hermes profile | 由独立 workspace、`AGENTS.md`、session 和 model override 表达 | 已满足项目逻辑 Agent；OpenCode 原生 `--agent` 是可选增强 |
| System prompt | 真正的 system message | 首轮拼接到普通用户消息 | 语义不等价 |

当前 OpenCode 实现约 770 行，本功能提交总计约增加 925 行；Hermes 客户端约 687 行。因此它在依赖和上层改动方面较轻，但总代码量已经不能算“很少”。新增代码主要用于子进程生命周期、NDJSON 解析、重试、workspace 适配和 Simulator 配置兼容。

## 4. 当前 CLI 方案做得合适的部分

1. **使用官方自动化入口。** `opencode run` 本来就用于脚本和自动化，而 `--format json` 提供原始 JSON 事件。
2. **复用 OpenCode 能力。** Agent 继续使用 OpenCode 自身的 provider、模型、认证、插件、MCP、工具、规则和 skills。
3. **没有把密钥放进命令行。** endpoint 和 API key 不作为进程参数传递，也没有写进仓库配置。
4. **基本命令安全。** 使用 `create_subprocess_exec()` 参数数组，prompt 通过 stdin 传入，没有 shell 字符串拼接。
5. **会话续接方式正确。** 首轮记录 OpenCode `sessionID`，后续通过官方 `--session` 续接。
6. **同一会话串行。** 每个 `(agent_name, session_name)` 有独立锁，避免同一会话并发写入。
7. **跨平台进程终止较完整。** Windows 终止进程树，Unix 终止进程组；超时和任务取消都会清理子进程。
8. **易做离线契约测试。** NDJSON 解析和命令构造可以通过固定事件、假进程进行单元测试。

所以，CLI 方案本身并不是问题；需要处理的是当前适配层没有覆盖的语义和生命周期。

“复用用户配置”同时是优点和风险。建议以后明确区分两种模式：

- **本地兼容模式：** 继续继承用户现有 OpenCode 配置，开箱即用。
- **确定性评测模式：** 使用专用 `OPENCODE_CONFIG_DIR`/`OPENCODE_CONFIG_CONTENT`，可选 `--pure`，禁用自动分享和自动升级，并固定 OpenCode 版本；凭证只通过环境变量或受保护的 secret file 注入。

## 5. 当前实现可能存在的问题

### 5.1 最高优先级：会影响结果正确性

| 问题 | 当前证据 | 影响 | 建议 |
|---|---|---|---|
| Evaluator 实际没有逐轮重置 | [`Evaluator._reset_session()`](../src/evaluator/evaluator.py#L521) 只调用 `client.gateway`；OpenCode、Hermes 和 ClaudeCode 客户端都没有该 gateway，因此会直接返回。OpenCode 又会按相同 key 复用原会话 | 上一轮判词进入下一轮上下文，造成锚定偏差，严格评测结果不可靠 | 这是共享 Evaluator 抽象缺陷。最小修复是每轮使用新的 evaluator session key（例如追加 turn），一次覆盖所有无 gateway 客户端 |
| `system_prompt` 角色不正确 | [`_build_prompt()`](../src/opencode_client.py#L365) 只在首轮把 system prompt 和 query 拼成一段文本 | OpenCode 会把整段作为 user message；遇到冲突指令或 prompt injection 时，优先级不等价于 system message | 最小修复是把 system prompt 合入该逻辑 Agent workspace 的 `AGENTS.md` 并删除首轮拼接；需要原生权限/模型 Agent 时，再生成 `.opencode/agents/<name>.md` 并传 `--agent` |
| 工具调用证据被丢弃 | [`_parse_run_output()`](../src/opencode_client.py#L252) 只保留 `text`、最后一个 `step_finish` 和 `error`，虽然 CLI 已输出完成/失败的 `tool_use` 事件 | Evaluator 无法审计 bash/read/edit 等过程，只能依赖最终文本和部分文件结果 | 最小解析并保存脱敏、截断后的 call id、工具名、输入、状态和必要输出 |
| Agent 与 Simulator 的配置规则不一致 | [`load_opencode_simulator_config()`](../src/opencode_client.py#L60) 只读取 inline、一个指定文件或一个默认 JSON，只接受 `@ai-sdk/openai-compatible` | OpenCode Agent 可以正常运行，但 Simulator 可能选错模型、缺少认证或回退旧配置，出现 401/502 等不一致结果 | 保持 Hermes 式独立 Simulator；优先用环境变量或 secret file 显式配置。OpenCode loader 只作为有限、显式 opt-in 兼容层，不再扩展为第二套完整配置系统 |

OpenCode 官方支持 JSON 和 JSONC，并会合并 remote、global、custom、project、`.opencode`、inline、managed 等多层配置。当前 Simulator loader 不可能只靠少量代码完整复制该行为，继续扩展它会形成第二套易漂移的配置系统。

### 5.2 高优先级：影响可重复性、安全性和稳定性

| 问题 | 触发条件与影响 | 建议 |
|---|---|---|
| Simulator 关闭 TLS 验证 | [`User_simulator`](../user_simulator.py#L55) 使用 `httpx.Client(verify=False)`；OpenCode loader 提取出的 API key 会交给该客户端 | 默认恢复证书验证；私有 CA 使用 CA bundle；非 loopback 的明文 HTTP endpoint 不应携带真实 API key |
| 继承用户全部 OpenCode 配置 | 外部 plugin/MCP、全局 Agent、权限、`.env`、认证、自动分享和自动升级会改变工具集、输出、启动时间与数据去向 | 保留方便的本地模式，同时提供确定性评测模式：专用 config、当前 1.18.10 支持的 `--pure`、禁用分享/自动升级并固定版本 |
| 非交互权限会自动拒绝 | 未使用 `--auto` 时，当前 `opencode run` 会自动 reject `permission.asked`，并限制 question/plan 等交互；默认添加 `--auto` 又会放大写文件和 shell 风险 | 用专用配置预先定义细粒度 `allow/deny`；把权限拒绝作为明确结果；`--auto` 只能在隔离环境中显式启用 |
| 注入文件的覆盖顺序不明确 | [`BaseWorkspaceManager.setup_agent_files()`](../src/workspace.py#L46) 先写 persona/skills，再复制 `content_root`；其中的 `AGENTS.md` 或 `.opencode` 可覆盖刚生成的规则和 skills。配置为空时旧 `AGENTS.md` 也可能残留 | 先定义清晰的冲突优先级；至少保证本次托管的 `AGENTS.md` 被确定性覆盖或删除，并对冲突报错/告警 |
| 持久 workspace 的语义未显式区分 | `~/.opencode/workspace/<agent>` 与 Hermes 的持久 profile 思路一致，但严格评测可能读到旧产物或记忆 | 默认可继续保持 persistent；只有严格隔离场景再增加可选 `workspace_mode=run`，不要为 MVP 默认引入复杂清理框架 |

### 5.3 按规模或实际需求处理

| 问题 | 影响 | 建议 |
|---|---|---|
| 每轮冷启动 | 每个 `execute()` 都创建新的 OpenCode 进程和本地运行实例；多轮或并发增大后，重复初始化尤其是 MCP 会变得明显 | 先基准测试；需要时增加 `run --attach`，再按触发条件迁移长期 Server |
| stdout/stderr 全量缓冲 | `communicate()` 在进程退出后才一次性返回；大工具输出占用内存，超时时拿不到已经产生的事件 | 仅在大输出、实时进度或更精确重试成为需求时，改成并发按行读取两条管道 |
| 超时不是协议级 abort | 当前只杀 CLI 进程；OpenCode session 可能留下不明确状态 | CLI 模式把超时 session 标记为不可续接；迁移 Server 后调用 `/session/:id/abort` |
| Session 无生命周期策略 | 真实 sessionID 只存在 Python 内存，重启后无法恢复；`close()` 也不删除持久 session | 只有出现恢复、容量或隐私要求时，再增加持久化映射、保留数量或删除策略 |
| Usage 只保留最后一个 `step_finish` | 该字段是单 step 还是累计值尚未验证，可能遗漏前序 step，也可能已是累计值 | 先用多 step fixture 确认语义，再决定求和、保留明细或从 message/session API 读取 |
| 对 stdout 杂行过于严格 | 任意非 JSON 行都会变成失败，即使已经得到有效 text；插件提示或版本变化可能误伤 | 有合法事件时将杂行降为 warning；可选 `--pure`；增加固定版本契约测试 |
| 自动重试可能重复副作用 | 在 sessionID 尚未成功解析前，进程可能已经写文件，但 wrapper 仍可能重试 | 配置错误不重试；写任务默认不盲目重放；流式读取后可更早识别已产生副作用 |
| 没有 CLI 版本/能力检查 | 只验证可执行文件存在，事件字段变化会到运行时才暴露 | 检查最低/最高已测试版本；CI 固定 OpenCode 版本；禁用自动升级后由项目显式升级 |
| Agent 目录名可能碰撞 | 不同原始名称规范化后可能得到同一目录 | 不合法名称直接报错，或在规范化名称后追加稳定哈希 |
| `ExecutionOptions` 只是偶然兼容 | [`_make_options()`](../src/executor.py#L337) 没有 OpenCode 分支，依赖其他实现恰好也有 `timeout_seconds` | 抽出共享 `ExecutionOptions`，或增加明确的 OpenCode 分支 |
| 错误脱敏是 best-effort | URL query、自定义 header、非典型 token 仍可能进入日志 | 对结构化 error 递归删除敏感字段；日志默认不保留响应 headers/body；增加凭证格式测试 |

## 6. 可选方案对比

| 方案 | 增量代码 | OpenCode 功能保真 | 启动性能 | 流式/工具证据 | 会话/取消/权限 | 跨平台与部署 | 适用结论 |
|---|---:|---|---|---|---|---|---|
| A. 当前：每轮 `opencode run` | 最低，已完成 | 中高：OpenCode 内核保真高，但当前适配丢失部分语义和事件 | 低，多轮重复冷启动 | 中低 | 中低，主要依赖 CLI 退出码 | 较好，但要维护进程树 | 适合 MVP 和 fallback |
| B. `run --attach` 长期 Server | 低 | 与 A 相同 | 中高，避免 Server/MCP 重复冷启动 | 仍受 CLI parser 限制 | 中 | 较好，需要外部 Server | **近期最推荐** |
| C. Python 调用 Server HTTP/OpenAPI + SSE | 阻塞 message 为中；完整 SSE/权限/重连为中高 | 高 | 高 | 高 | 高，支持 health、abort、permission、delete | HTTP 跨平台好；需管理 Server | **中期最推荐** |
| D. 官方 JS/TS SDK + sidecar | 对当前 Python 项目高 | 与 Server API 同源，官方生成类型覆盖完整 | 高 | 高 | 高 | 需 Node/npm 和双语言发布 | 当前项目不优先 |
| E. `opencode acp` + ACP Python SDK | 中 | 高，且有协议版本/能力协商 | 高 | 高，标准化增量消息、工具和权限 | 高，支持标准取消；持久 session 删除仍需另管 | 较好 | 需要标准协议或常驻 stdio 时考虑 |

`opencode-ai` Python SDK是方案 C 的实现变体，不是新的 transport；直接模型 API不是 OpenCode harness；Plugin/Hook 只是 transport 的补充机制。后文分别说明。

### 6.1 方案 A：当前一次一进程 CLI

命令形式：

```bash
opencode run --format json --dir <workspace> [--model provider/model] [--session sessionID]
```

优点：

- 现在已经完成，改动范围最小。
- 不需要额外 Server、端口、HTTP 客户端或 Node SDK。
- 单轮进程崩溃通常不会直接拖垮 Python 主进程。
- 对 CLI 的命令和输出可以离线测试。

缺点：

- 每轮都付出进程及 Server 初始化成本。
- 需要自行维护 NDJSON schema、进程树、超时、重试和脱敏。
- 无法自然处理 SSE、权限请求、session abort/delete 等完整生命周期。

`--format json` 确实逐行输出事件，但当前版本的 `text` 在一个文本 part 完成后才输出，`tool_use` 也在工具 completed/error 后输出；它不是逐 token 文本流。当前 wrapper 又在进程结束后才统一解析，因此对调用方表现为非流式。

适用场景：

- 本地开发、串行任务、任务量较小。
- 首版交付或其他方案故障时的 fallback。

### 6.2 方案 B：CLI Attach

先由用户或部署系统启动长期 Server。服务应只绑定 loopback；如果跨机器暴露，应设置 `OPENCODE_SERVER_PASSWORD` 并另加 TLS、VPN 或可信反向代理：

```bash
opencode serve --hostname 127.0.0.1 --port 4096
```

执行时：

```bash
opencode run --attach http://127.0.0.1:4096 --format json --dir <workspace>
```

OpenCode 官方明确说明，`run --attach` 可以避免每次运行时的 MCP Server 冷启动。

推荐第一步只增加一个可选环境变量，例如：

```text
OPENCODE_SERVER_URL=http://127.0.0.1:4096
```

设置时添加 `--attach`，未设置时继续使用当前一次一进程模式。这样可以保留现有命令构造、NDJSON parser、session 和 fallback，新增代码很少。

此方案仍会为每次调用启动一个轻量 CLI 客户端，也没有解决工具事件丢失、全量缓冲、system prompt 和 session 清理；它只是一个很划算的性能过渡方案。

### 6.3 方案 C：Python 直接调用 OpenCode Server

`opencode serve` 提供公开的 OpenAPI 3.1 接口。当前项目真正需要的接口很少，可以用 Python `httpx` 手写一个小型适配器，而不必生成完整客户端：

- `GET /global/health`
- `POST /session`
- `POST /session/:id/message`（阻塞直到完整响应）
- `POST /session/:id/prompt_async`（异步提交）
- `GET /event`（SSE）
- `POST /session/:id/abort`
- `POST /session/:id/permissions/:permissionID`
- `DELETE /session/:id`（按保留策略）

优势：

- message API 可以结构化传入 `system`、`agent`、`model`、`tools` 和 parts。
- 能得到完整 message parts 和实时事件，不必解析 CLI 展示层输出。
- timeout 后可以显式 abort，会话结束后可以显式 delete。
- 可以结构化处理权限请求、错误类型、HTTP 状态和 health check。
- 长期 Server 更适合多 Agent 并发。

真正的事件驱动调用链应是：

```text
绑定正确的远端 directory
  → 先订阅 GET /event
  → POST /session/:id/prompt_async
  → 按 sessionID 过滤事件
  → 等待 session idle 或 session error
```

只使用同步 `/message` 可以得到更简单的第一版 HTTP adapter，但 SSE、权限响应和取消需要并发任务时，复杂度仍会上升。

代价：

- 需要管理 Server 启动、端口、ready check、Basic Auth 和退出。
- 需要自己维护 Python 请求模型和 SSE parser；官方 API 变更时仍需更新。
- 共享 Server 的每个 client/请求必须绑定正确的远端 directory；`run --attach --dir` 中的路径也是 **Server 所在机器** 的路径。
- Server API 没有版本化路径；启动时应读取 `/global/health` 版本，并针对该版本 `/doc` 做契约测试。
- 完整实现还要处理 SSE 重连、事件去重、session 过滤和权限请求。

这是当前 Python 项目在功能完整度与实现复杂度之间最平衡的中期方案。

#### 方案 C 的 Python 实现选择

最小实现可以直接用 `httpx` 手写上述少数端点。OpenCode 官方 GitHub 组织下也有 `opencode-sdk-python`，PyPI 包名是 `opencode-ai`。这是由 Stainless 生成的类型化 REST 客户端，提供：

- 同步和异步客户端。
- `httpx`/`aiohttp` 后端。
- Pydantic 请求/响应模型。
- SSE、超时、重试和结构化 HTTP 错误。

它比手写 REST/SSE 代码更省事，也更符合当前 Python 项目。但截至本次审查，公开 release 仍为 `v0.1.0-alpha.36`（2025-08-27），安装说明也要求 `pip install --pre`；而本机 OpenCode CLI 已是 `1.18.10`。这不等于它一定不兼容，但说明采用前必须针对当前 Server 的 `/doc` 做契约验证，不能仅凭包名假定覆盖完整。

建议：

1. 先在独立实验分支验证 health、session create、message、SSE、abort 和 permission。
2. 固定 Python SDK 与 OpenCode Server 的成对版本。
3. 关闭 SDK 对有副作用请求的默认自动重试，避免重复执行。
4. 如果缺端点或模型滞后，优先手写少量 HTTP 请求，或从当前 Server `/doc` 生成客户端。

因此，它是值得试验的方案 C 实现变体，但不是当前官方文档主推的 SDK，也不宜未经契约测试就替代已经跑通的 CLI。

### 6.4 方案 D：官方 JS/TS SDK + Node sidecar

官方 `@opencode-ai/sdk` 是由 Server OpenAPI 生成的类型安全 JS/TS 客户端：

- `createOpencode()` 可以同时启动 Server 和 Client。
- `createOpencodeClient()` 可以连接已有 Server。
- SDK 覆盖 sessions、messages、abort、permissions 和 SSE events。

优势：

- 与直接 HTTP 使用同一 Server API，但提供官方生成类型和完整客户端封装。
- Server API 变化时，升级 SDK 比手工维护 Python schema 更直接。

代价：

- 当前项目是 Python，需要新增 Node/npm 依赖。
- 还要设计 Python 与 sidecar 之间的 HTTP、stdio 或 JSON-RPC 桥。
- 构建、版本锁定、日志、退出和测试都要维护两套运行时。
- SDK 与 Server/OpenCode CLI 最好锁定相同版本；多个 worker 使用 `createOpencode()` 时还要分配不同端口，避免默认端口冲突。

除非项目以后本身就要引入 TypeScript 服务层，否则不建议只为调用 SDK 增加 sidecar。

### 6.5 方案 E：ACP

`opencode acp` 会启动一个长期子进程，通过 stdin/stdout 使用 JSON-RPC 通信。ACP 的价值是标准化：以后可以让同一个 harness 连接不同的 ACP 智能体，而不只连接 OpenCode。

优势：

- 长期进程，不需要每轮冷启动。
- 协议会先协商版本和能力；结构化增量消息、思考、工具、usage、权限和 cancel 比 CLI 原始事件更稳定。
- 对 IDE/客户端、工具交互和权限流程的抽象更通用。

ACP 官方已有 Python SDK `agent-client-protocol`，提供生成的 Pydantic schema、asyncio transport、stdio JSON-RPC 和生命周期 helper，因此不必从零实现所有协议基础设施。

代价：

- 仍需把 ACP 的 session/update、工具调用、权限和取消映射到本项目数据模型，并处理子进程重启与恢复。
- ACP 的主要定位是编辑器与编码 Agent 通信，未必与本项目的 Simulator/Evaluator/评分数据模型一一对应。
- 当前只接 OpenCode 时，实现成本高于 CLI Attach，也未必低于直接 HTTP。
- ACP `closeSession` 只结束 ACP 运行态并 abort backing session，不能等同于 Server 的持久 session delete。
- OpenCode ACP 的认证不会接收 provider API key，而是引导使用 `opencode auth login`；凭证仍由 OpenCode 管理。

因此，当前“最少代码”目标下不优先 ACP；但如果即使只接 OpenCode，也明确需要常驻 stdio、协议协商、标准化增量事件、取消和权限，它仍是合理候选，不限于多智能体场景。

### 6.6 排除项：直接模型 API

直接用 OpenAI-compatible SDK 调模型代码最少，但它不等于 OpenCode：

- 没有 OpenCode agent loop。
- 没有 tools、skills、MCP 和 `AGENTS.md` 规则加载。
- 没有 OpenCode session。
- 没有 OpenCode permissions 和工具轨迹。

它可以继续用于纯对话的 User Simulator，但不能作为核心 OpenCode Agent/Evaluator 的实现。

### 6.7 补充机制：OpenCode Plugin/Hook

OpenCode Plugin 可以订阅 message、session、permission、file 和 tool 等内部事件，也可以在工具执行前后增加策略或审计。它可用于：

- 对 CLI `tool_use` 未覆盖的内部审计点写入受控 sink；常规工具轨迹应先直接解析 CLI 已提供的 `tool_use`。
- 在工具执行前做安全策略检查。
- 为 harness 添加自定义工具或结构化日志。

但 Plugin 运行在 OpenCode/Bun 进程内部，不负责从 Python 发起 prompt、关联 harness session、控制超时或汇总 `ExecutionResult`，因此不是 CLI、HTTP 或 ACP transport 的替代品。它还会引入 JS/TS 代码和额外的 OpenCode 行为耦合。

只有当公开 transport 无法提供必须的审计事件或内部 hook 时，才应把 Plugin 作为补充层；不要用 Plugin 重写整个外部 harness。

## 7. 推荐演进路线

### 阶段一：保留 CLI，先保证语义正确

建议优先级：

1. 在使用真实凭证前恢复 Simulator TLS 验证；私有证书改用 CA bundle。
2. 每轮使用新的 evaluator session key，修复所有无 gateway 客户端的评估隔离。
3. 将 `system_prompt` 合入每个逻辑 Agent workspace 的 `AGENTS.md`，并明确它与 `content_root` 的覆盖优先级。
4. 最小解析 `tool_use`，只保留脱敏、截断后的必要证据；用多 step fixture 先确认 usage 语义。
5. 补 CLI 版本检查和 NDJSON fixture 契约测试；自动化环境固定版本并关闭自动升级。
6. 明确 Simulator loader 只是有限、显式 opt-in 的兼容层，User Simulator 继续使用独立安全配置。

原生 `.opencode/agents/<name>.md + --agent`、`workspace_mode=run`、session retention 和全量流式双管道都按真实需求再做，不列为 MVP 必改项。若启用 `--agent`，必须预先校验 Agent 存在且为 `primary/all`；OpenCode 对不存在或 subagent 的名称可能警告后回退默认 Agent。

### 阶段二：以最小改动支持 Attach

1. 支持可选 `OPENCODE_SERVER_URL`。
2. 设置后为 `opencode run` 添加 `--attach`。
3. Server 先由用户或部署系统管理，Python 不自动抢占端口或守护进程。
4. 未设置或连接失败时是否回退本地模式，应由显式配置决定，避免无提示地改变隔离和性能特征。

### 阶段三：满足触发条件后迁移 HTTP

出现下列任一需求时，应考虑从 CLI transport 迁移为 HTTP/OpenAPI：

- 同时运行多个 Agent 或多个 query。
- MCP、plugin、LSP 冷启动成为主要耗时。
- Evaluator 必须得到完整、实时、可审计的 tool evidence。
- 需要可靠的 timeout/abort、permission 响应或 session 清理。
- harness 要作为长期后台服务运行。
- CLI NDJSON schema 变化频繁，兼容成本超过 HTTP schema 维护成本。

迁移时应保留现在的 `OpenCodeClient`、`OpenCodeAgent`、`ExecutionResult` 和上层工厂接口，只替换内部 transport。这样 `harness_automation.py`、executor 和配置模型不需要跟着大改。

### 阶段四：出现标准协议需求时评估 ACP

如果未来目标变成“一个 harness 同时兼容 OpenCode、其他 ACP Agent 和 IDE Agent”，或者标准化的增量事件、取消和权限协议比 OpenCode 专用 API 更重要，再把 transport 抽象为 ACP。否则保持 OpenCode HTTP 适配器会更直接。

## 8. 建议的验证项

在把当前 CLI 实现用于正式评测前，至少补充以下验证：

| 验证项 | 通过标准 |
|---|---|
| Evaluator reset | 第二轮 evaluator 的输入上下文不含第一轮判词 |
| 逻辑 Agent | 两个 Agent 能加载不同 system prompt 和模型；启用原生 Agent 时再验证权限与回退行为 |
| 工具轨迹 | bash/read/edit 的 input、结果或错误能进入 trajectory |
| Usage | 用真实多 step 事件确认 `step_finish` 是单步还是累计，再验证统计值 |
| Workspace 语义 | persistent 模式会按设计复用；若实现 run 模式则互相隔离；托管的旧 `AGENTS.md` 不残留 |
| Session 安全 | 超时或状态不明的 session 不会被误续接；只有实现 retention 时再测保留/恢复/删除 |
| 权限 | 允许项可执行，危险项被拒绝，不依赖人工弹窗 |
| TLS | 默认验证服务端证书；私有 CA 使用指定 CA bundle |
| 确定性模式 | 不继承未声明的 plugin/MCP/分享策略，OpenCode 版本固定 |
| 配置一致性 | JSONC、多层配置、auth store 等不被 Simulator loader 错误地宣称支持 |
| 兼容版本 | 最低、当前和计划升级版本的 NDJSON fixture 均通过 |
| 性能对比 | 记录 one-shot 与 attach 的首轮、后续轮 p50/p95 延迟和资源占用 |

## 9. 最终选择建议

| 时间范围 | 推荐选择 | 原因 |
|---|---|---|
| 当前 PR / MVP | 保留一次一进程 CLI | 已跑通、公开接口、改动范围可控 |
| 近期优化 | CLI + 可选外部 Server Attach | 新增代码最少，能直接降低重复冷启动 |
| 中期正式服务 | Python + OpenCode HTTP/OpenAPI + SSE | 更完整的事件、session、abort、permission 和并发能力；alpha SDK 验证通过后可替代部分手写 HTTP |
| 标准协议/多智能体平台 | ACP + ACP Python SDK | 需要跨 Agent，或标准增量事件、取消、权限比最低代码量更重要时采用 |
| 补充审计或策略 | OpenCode Plugin | 可获取内部 hook，但不能替代外部 transport |
| 不建议作为核心方案 | 直接模型 API、现在引入 JS sidecar、未经验证直接采用 alpha Python SDK | 分别存在功能不保真、双运行时复杂、版本兼容风险 |

一句话结论：**当前 CLI 实现可以保留，但应把它定位为 MVP transport；先修复 Evaluator 隔离、system prompt、工具证据、TLS 和确定性问题，再用 `--attach` 做低成本过渡，需求成熟后迁移到 OpenCode HTTP/OpenAPI。**

## 10. 官方资料

- [OpenCode CLI：`run`、`--session`、`--agent`、`--format json`、`--attach`](https://dev.opencode.ai/docs/cli/)
- [OpenCode `run` 官方实现](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/run.ts)
- [OpenCode Server：HTTP API、OpenAPI 3.1、session、abort、permission、SSE](https://dev.opencode.ai/docs/server/)
- [OpenCode SDK：官方 JS/TS 类型安全客户端](https://dev.opencode.ai/docs/sdk/)
- [OpenCode 官方组织的 Python REST SDK](https://github.com/anomalyco/opencode-sdk-python)
- [Python REST SDK 发布记录](https://github.com/anomalyco/opencode-sdk-python/releases)
- [OpenCode ACP：JSON-RPC over stdio](https://dev.opencode.ai/docs/acp/)
- [ACP v1 协议](https://agentclientprotocol.com/protocol/v1/overview)
- [ACP Python SDK](https://github.com/agentclientprotocol/python-sdk)
- [OpenCode Config：JSON/JSONC、配置合并与优先级](https://dev.opencode.ai/docs/config/)
- [OpenCode Providers 与认证](https://dev.opencode.ai/docs/providers/)
- [OpenCode Permissions：`allow / ask / deny` 与 `--auto`](https://dev.opencode.ai/docs/permissions/)
- [OpenCode Agents](https://dev.opencode.ai/docs/agents/)
- [OpenCode Rules / `AGENTS.md`](https://dev.opencode.ai/docs/rules/)
- [OpenCode Agent Skills](https://dev.opencode.ai/docs/skills/)
- [OpenCode Plugins / Hooks](https://dev.opencode.ai/docs/plugins/)
