# 模型 API 转发网关

模型 API 转发网关部署在 Agent 或其他 API 客户端与模型供应商之间。每个监听地址对应一个上游基础地址，客户端把 API 地址指向网关并沿用自己的供应商凭据。网关转发 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 和其他常用 HTTP 请求，并以 `session-id` 关联请求与响应记录。

## 配置和启动

在项目根目录编辑 [proxy_config.json](../proxy_config.json)。顶层键是供应商名称，会写入日志的 `provider` 字段。

```json
{
  "gongyingshang1": {
    "target_ip": "https://www.yibuapi.com",
    "local_ip": "127.0.0.1:5001"
  },
  "gongyingshang2": {
    "target_ip": "http://115.111.111.111:8082",
    "local_ip": "127.0.0.1:5002"
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `local_ip` | 网关监听的主机和端口。每个供应商使用唯一的监听地址。 |
| `target_ip` | 上游基础地址，必须写明 `http://` 或 `https://`。可包含端口和路径前缀，末尾省去 `/`。 |

安装项目依赖后，在项目根目录启动网关。启动目录决定日志位置。配置格式错误或地址绑定失败时，进程报错退出。

```bash
python -m src.proxy_gateway --config proxy_config.json
```

## 请求如何经过网关

1. **选上游。** 网关按接收请求的监听地址选择供应商，将 `target_ip` 与客户端原始请求路径和查询参数直接拼接。例如，`http://127.0.0.1:5001/v1/chat/completions?foo=bar` 会转发到 `https://www.yibuapi.com/v1/chat/completions?foo=bar`。
2. **确定会话。** 按顺序读取 `session-id`、`X-Session-Id` 请求头、请求体顶层以及 `metadata`。请求体支持 `session-id`、`session_id` 和 `sessionId`，也兼容 `metadata.user_id` 中的 `session_...` 和包含会话字段的 JSON。首个非空且可写入 HTTP 头的 ASCII 值即为本次会话标识。缺失时生成 32 位十六进制 UUID4。
3. **转发请求。** 原路由处理函数根据路径和请求体中的 `stream` 选择普通请求或流式请求。鉴权头随请求传给上游，每次只尝试一次。普通响应和 SSE 分块由原路由返回客户端，网关通过响应头回传本次的 `session-id`。
4. **写入日志。** 请求结束后，网关先追加一条小时数据记录，再追加索引。上游 HTTP 错误、连接失败、超时和客户端中途断开都会进入记录。响应开始前，上游连接失败返回 `502`，超时返回 `504`。

网关在上游请求头、客户端响应头、轨迹和索引中使用同一个 `session-id`。同一会话的多轮请求需要重复发送这个值。客户端每次都省略标识时，网关会为每次请求单独生成 UUID。`id` 始终标识单次请求。

## 协议实现

[网关入口](../src/proxy_gateway.py)负责监听、会话关联和日志。网关直接注册并执行从 `llm-proxy` 提交 `0b43771` 收录的 FastAPI 路由，使用原连接池和各路由的普通响应、SSE 转发及解析代码。适配部分接入端口映射、客户端鉴权头、一次转发和 JSONL 轨迹。 [上游 app.py](https://github.com/zhengnianzu/llm-proxy/blob/0b437714ab34693be2258cf3bbf4195624d2920e/app.py)

| 请求 | 实际处理函数 | 转发和解析 |
| --- | --- | --- |
| `POST /v1/messages` | [`anthropic_messages()`](../src/llm_proxy/app.py#L289) | 普通 JSON 检查 `text`、`thinking`、`tool_use` 内容块。SSE 在 `message_start` 前缓冲，此后逐块转发。显式错误事件可直接转发，缺少起始事件时返回 `502`。用量来自 `message.usage` 和后续事件的 `usage`。 |
| `POST /v1/chat/completions` 或 `POST /chat/completions` | [`openai_chat_completions()`](../src/llm_proxy/app.py#L502) | 转发普通 JSON，或逐块转发并解析 SSE 的 `data:` 行。保留 `choices[].delta` 和顶层 `usage`。 |
| `POST /v1/responses` | [`openai_responses()`](../src/llm_proxy/app.py#L675) | 转发普通 JSON，或逐块转发并解析 `response.*` 事件。用量可来自顶层 `usage` 或 `response.usage`。 |
| 其他常用 HTTP 请求 | [`passthrough()`](../src/llm_proxy/app.py#L848) | 保留方法、路径和请求体，使用 `client.send(..., stream=True)` 发送请求，逐块返回上游响应。 |

## 日志目录和记录格式

日志按请求到达时的本地时间写入 `data/YYYYMMDDHH.jsonl`，每小时一个数据文件。有请求写入时才创建该小时的文件。索引持续追加。运行中清空整个日志目录后，下一条完成的请求会重新创建目录和文件。

```text
logs_proxy/traj/
├── index.jsonl
└── data/
    ├── 2026111314.jsonl
    └── 2026111315.jsonl
```

### 小时数据文件

每行是一个 UTF-8 JSON 对象。下面是一条 HTTP `429` 的记录。

```jsonl
{"ts":"20261113145023000","id":"request-1","provider":"gongyingshang1","protocol":"openai_chat","method":"POST","path":"/v1/chat/completions","headers":{"session-id":"session-1"},"req":{"model":"example","messages":[]},"session-id":"session-1","res":{"status_code":429,"headers":{"content-type":"application/json"},"json":{"error":{"message":"rate limit"}}}}
```

| 字段 | 含义 |
| --- | --- |
| `ts` | 请求到达时间，格式为 `YYYYMMDDHHMMSSmmm`。 |
| `id`、`session-id` | 单次请求标识、会话标识。 |
| `provider`、`protocol`、`method`、`path` | 供应商、处理协议、HTTP 方法、包含查询参数的路径。 |
| `headers`、`req` | 请求头和请求体。日志隐藏鉴权头和 Cookie 的值。二进制请求体按 Base64 保存。 |
| `res.status_code`、`res.headers` | 响应状态和响应头。上游与客户端状态不同时，另存 `upstream_status_code`。客户端在响应开始前断开时使用 `499`。 |
| `res.json`、`res.text`、`res.base64`、`res.chunks` | 普通响应保存 JSON、文本或 Base64。SSE 保存原文和事件，流中断时保存已收到的部分。 |
| `res.usage`、`res.error` | 用量对象和网关捕获的错误类型，有值时写入。 |

### 索引 `index.jsonl`

每行对应一条数据记录。可按 `session-id` 筛选同一会话，再从 `logs_proxy/traj/` 下的 `file` 打开数据文件，按字节 `offset` 读取 `length` 个字节。`length` 包含行末换行。响应状态为 `200` 至 `399` 且处理过程无错误时，`success` 为 `true`。

```jsonl
{"ts":"20261113145023000","id":"request-1","session-id":"session-1","provider":"gongyingshang1","status_code":429,"success":false,"file":"data/2026111314.jsonl","length":359,"offset":0}
```
