# 模型 API 转发网关

网关部署在 Agent 或其他 API 客户端与模型供应商之间。它根据客户端连接的监听地址选择供应商，转发模型请求并记录请求、响应和会话标识。转发与统计直接沿用 llm-proxy-lite 的两个 Python 文件，适配多供应商配置和小时日志。[上游 app.py](https://github.com/zhengnianzu/llm-proxy-lite/blob/f88838f991d19676550718b06c87979fb22a458c/app.py) · [上游统计脚本](https://github.com/zhengnianzu/llm-proxy-lite/blob/f88838f991d19676550718b06c87979fb22a458c/print_stats_summary.py)

## 配置与启动

编辑网关目录中的 [proxy_config.json](proxy_config.json)。顶层键是供应商名称，会写入日志的 `provider` 字段。

| 字段 | 格式 |
| --- | --- |
| `local_ip` | 网关监听的主机和端口，例如 `127.0.0.1:5001`。 |
| `target_ip` | 以 `http://` 或 `https://` 开头的上游基础地址，可包含端口和路径前缀，末尾省去 `/`。例如 `https://api.deepseek.com/anthropic`。 |

安装项目依赖后，从项目根目录启动。网关启动时绑定所有配置的监听地址，配置校验或任一端口绑定失败时退出并报告原因。

```bash
python llm_proxy_lite/app.py --config llm_proxy_lite/proxy_config.json
```

## 请求如何经过网关

1. **选上游。** 网关按监听地址匹配供应商，将 `target_ip` 与客户端的原始路径和查询参数直接拼接。例如，上游配置为 `https://api.deepseek.com/anthropic` 时，客户端发往 `/v1/messages?beta=true` 的请求会转发到 `https://api.deepseek.com/anthropic/v1/messages?beta=true`。
2. **确定会话。** 网关依次检查 `session-id` 请求头、`X-Session-Id` 请求头、请求体顶层及 `metadata`。请求体兼容 `session-id`、`session_id`、`sessionId`，也识别 `metadata.user_id` 中的 `session_...` 格式和含会话字段的 JSON 对象。找不到非空且可写入 HTTP 头的 ASCII 值时，生成 32 位十六进制 UUID4。
3. **转发和返回。** 网关沿用 Lite 的鉴权方式，优先使用 `UPSTREAM_API_KEY`，其次从客户端请求读取凭据。上游请求头和客户端响应头使用同一个 `session-id`。模型路由按请求的 `stream` 字段选择普通或流式分支，并沿用各分支的重试与错误响应。
4. **落盘。** Lite 路由直接调用同一个写入函数。普通响应在返回前写入，SSE 在原有生成器的 `finally` 中写入。每条请求对应一条正文和一条索引，HTTP 错误、非法 JSON 和客户端断开也记录。流中断时保留已读取的响应部分，日志会隐藏鉴权头和 Cookie 的值。[写入函数](app.py#L873)

同一会话的后续请求应带回同一个 `session-id`。客户端每次都省略会话标识时，网关会为每次请求单独生成 UUID。日志中的 `id` 标识单次请求。

任务或网关在沙箱内首次启动时生成 `sandbox_id`，保存到项目目录下的 `cache/sandbox_id`，同一沙箱的多个进程及重启后的进程复用该值。`SANDBOX_ID` 可显式指定，`SANDBOX_ID_FILE` 可指定共享文件位置。每条轨迹和索引记录该字段，转发请求及响应头包含 `X-Sandbox-Id`，模型请求正文保持原协议。

## 协议处理

| 请求 | 代码位置 | 转发和解析 |
| --- | --- | --- |
| `POST /v1/messages` | [Anthropic 路由](app.py#L266) | 沿用 Lite 的普通 JSON 转发和用量解析。SSE 在收到 `message_start` 时放行缓冲内容，之后逐块返回。遇到 `406`、`429`、连接建立前出错或流结束时缺少 `message_start`，沿用原有重试分支。 |
| `POST /chat/completions` 或 `POST /v1/chat/completions` | [OpenAI 路由](app.py#L527) | 沿用 Lite 的普通 JSON 转发、SSE 逐块透传和 `data:` 行用量解析。SSE 在 `406`、`429` 或连接建立前出错时重试。 |
| `POST /v1/responses` | [OpenAI 路由](app.py#L527) | 复用同一 OpenAI 处理函数，透传 `response.*` 事件并读取 `response.usage`。 |
| 其他常用 HTTP 方法和路径 | [通用转发](app.py#L778) | 保留请求方法和原始响应字节。`GET /hi` 和 `GET /statistic` 在网关本地处理。 |

两种模型路由的普通分支遇到非 `200` 响应或连接异常时重试。模型路由最多尝试 20 次，重试间隔为 0.5 秒。流式错误沿用 Lite 的响应格式。日志记录返回给客户端的 HTTP 状态和尝试次数，状态与上游不同时另记上游状态。

## 日志文件与统计

控制台输出请求状态、尝试次数、耗时和 Token 摘要；逐次尝试的细节写入 `logs_proxy/gateway.log`。能力探测与健康检查标记 `request_kind=probe/health`，只输出 DEBUG，不计入模型调用统计。

SSE 按协议结束事件判断成功。完整响应后客户端关闭连接，记录 `client_closed_after_completion=true` 并保留成功；真实中断、上游 HTTP 或 SSE 错误仍失败，取消时也保留已收到的 Token 用量。

`index.jsonl` 中 `success=false` 的行记录脱敏后的 `error_message`（最多 1000 字符），兼容 JSON、SSE、Anthropic 嵌套错误和 Responses 失败事件，无正文时记录连接或断流原因。上下游状态不一致时增加 `upstream_status_code`；成功或重试恢复后不写失败文本。

```text
logs_proxy/traj/
├── index.jsonl
└── data/
    ├── 2026111314.jsonl
    └── 2026111315.jsonl
```

数据文件按请求到达时的本地时间命名，格式为 `YYYYMMDDHH.jsonl`，有记录时才创建。索引持续追加。运行期间清空日志目录后，下一条请求落盘时会重新建立目录。

每行都是 UTF-8 JSON 对象。以下是数据行和对应索引行的示例。

```jsonl
{"ts":"20261113145023000","id":"request-1","sandbox_id":"sandbox-1","session-id":"session-1","provider":"gongyingshang1","protocol":"openai","method":"POST","path":"/v1/chat/completions","headers":{"session-id":"session-1"},"req":{"model":"demo"},"res":{"headers":{"content-type":"application/json"},"status_code":429,"session-id":"session-1","json":{"error":"rate limit"}}}
```

```jsonl
{"ts":"20261113145023000","id":"request-1","sandbox_id":"sandbox-1","session-id":"session-1","provider":"gongyingshang1","model":"demo","tok_in":0,"tok_out":0,"status_code":429,"success":false,"error_message":"rate limit","file":"data/2026111314.jsonl","offset":0,"length":350,"total_attempts":20}
```

| 位置 | 字段 |
| --- | --- |
| 数据行 | `ts` 为本地时间，格式是 `YYYYMMDDHHmmssSSS`。`id` 为请求标识，`session-id` 为会话标识。`provider`、`protocol`、`method` 和 `path` 记录供应商、协议、请求方法和含查询参数的路径。`headers` 和 `req` 保存客户端请求，`res` 保存响应状态、头部、会话标识、响应内容和可用的 SSE 事件。 |
| 索引行 | `file` 相对于 `logs_proxy/traj/`，`offset` 是起始字节，`length` 是包括换行的字节长度。`model`、`tok_in`、`tok_out` 和 `success` 支持按模型和状态汇总。模型请求进入转发逻辑后，`total_attempts` 记录实际尝试次数。 |

网关将 `logs_proxy/traj/` 写在启动时的工作目录下。运行统计脚本前，先切换到启动网关时所在的目录，即使使用脚本的绝对路径也是如此。以下命令适用于从项目根目录启动的情况。也可访问网关监听地址的 `/statistic`。统计脚本支持按模型、日期和成败筛选，也能读取 Lite 旧版索引。

```bash
python llm_proxy_lite/print_stats_summary.py --date_start 2026-11-13 --status 全部
```
