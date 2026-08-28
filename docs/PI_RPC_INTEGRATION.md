# Pi RPC 集成说明

## 方案选型

Pi 官方 SDK 面向 Node.js/TypeScript，没有官方 Python SDK；`pi-py-sdk` 属于
第三方封装。当前项目是 Python 系统，且部署环境可以预装 Pi CLI，因此选择 Pi
官方提供的 RPC 模式：Python 只实现一层较薄的协议客户端，不引入第三方 SDK，
也不需要编写和维护额外的 Node.js 中间服务。

RPC 全称 Remote Procedure Call（远程过程调用），含义是一端通过约定协议调用
另一端提供的能力。Pi RPC 不是 HTTP 服务，也不是 JSON-RPC 2.0；它会启动一个
本地 `pi --mode rpc` 子进程，通过 stdin/stdout 按行传输 JSON（JSONL）。Python
向 stdin 写入 command，Pi 从 stdout 返回 command response，并持续输出 Agent、
消息和工具调用 event。

## 部署前置条件

运行本项目之前必须预安装 Pi CLI。使用官方 npm 包安装：

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi --version
```

通过 npm 安装时需要部署环境先提供 Node.js 和 npm。Node.js 用于运行 Pi CLI，
本项目的 Python RPC 客户端本身只使用 Python 标准库，不增加 Python 依赖。

## 1. 接入方式

每个 `(agent_name, session_name)` 启动一个
`pi --mode rpc --session-dir <dir> --approve` 子进程。Python 通过 stdin 发送一行一个
JSON 命令，通过 stdout 接收 JSONL response 和 event；stderr 单独持续读取。

一次请求发送 `prompt` 命令，收到同 ID 的成功 response 表示 Pi 已接受请求。
最终文本取最后一个 `message_end` 中的 assistant message；`agent_end` 后仍可能发生
自动重试或压缩，因此只把 `agent_settled` 作为本次请求真正结束的标志。工具调用由
`tool_execution_start` 和 `tool_execution_end` 按 `toolCallId` 关联并写入轨迹证据。

RPC 子进程在同一会话内持续运行，所以多轮对话保留上下文。超时或取消时发送
`abort`，随后关闭该子进程；失败请求不自动重放。

当 Pi 在 `message_end` 事件中返回 `stopReason=error` 或 `stopReason=aborted`
时，客户端会在 `harness_automation` logger 上以 `ERROR` 级别打印整条
assistant message（含 `errorMessage`、`provider`、`model`、`usage`）以及最近
10 行 Pi 子进程 stderr，便于快速定位是上游网关 4xx、模型不支持工具、还是
Pi 内部异常。

## 2. Pi 配置

部署前将模型配置写入 Pi 标准目录 `~/.pi/agent`。项目只读取该目录，不生成、
复制或修改其中的配置。

自定义服务写入 `~/.pi/agent/models.json`。按照顺序选择第一个provider的第一个model为默认模型。下面示例：

```json
{
  "providers": {
    "primary_gateway": {
      "baseUrl": "https://primary.example.com/v1",
      "api": "openai-responses",
      "apiKey": "primary-key",
      "models": [
        { "id": "gpt-5.6-terra" },
        { "id": "gpt-5.5" }
      ]
    }
  }
}
```

默认模型写入 `~/.pi/agent/settings.json`：

```json
{
  "defaultProvider": "primary_gateway",
  "defaultModel": "gpt-5.6-terra"
}
```

不配置 Agent 模型时使用上述默认值。`agents[].model` 可写成
`provider/model`，例如 `primary_gateway/gpt-5.6-terra`；如果
`simulator_config` 中存在同名 Agent，则其中的 `model` 和 `provider` 优先，并在
与 `agents[].model` 不一致时记录警告：

```json
{
  "main": {
    "model": "gpt-5.6-terra",
    "provider": "primary_gateway"
  },
  "evaluator": {
    "model": "gpt-5.5",
    "provider": "primary_gateway"
  }
}
```

`provider` 必须与 `models.json` 中 `providers` 的键一致。Pi 不使用该 JSON 中的
`base_url` 和 `api_key`，连接信息统一来自 `~/.pi/agent/models.json`。

## 3. Workspace 与会话

- Pi 固定使用 `~/.pi/workspace`，任务 JSON 中的 `workspace_base` 不改变该路径；
- `~/.pi/workspace/<agent>` 是 Agent 模板目录，`<agent>` 直接使用配置中的
  `agent_name`；配置文件和 skill 先放入模板，首次启动 RPC 子进程时再复制到实际
  session 目录；
- 普通 Agent 的运行时 session 名为
  `<query.session_name 或 main>_<run_id>`；evaluator 为
  `eval_<evaluate.session_name 或 query.session_name 或 main>_<run_id>`。
  `run_id` 是进程启动时间，格式为 `YYYYMMDDTHHMMSS`；
- RPC 子进程的实际 `cwd` 为
  `~/.pi/workspace/<agent>/.sessions/<运行时 session 名>`。例如一次运行的
  `run_id=20260820T104927`，query 使用 `agent_name=main`、
  `session_name=eval_test`，则路径为：

  ```text
  主 Agent 模板：~/.pi/workspace/main
  主 Agent cwd：~/.pi/workspace/main/.sessions/eval_test_20260820T104927
  Evaluator 模板：~/.pi/workspace/evaluator
  Evaluator cwd：~/.pi/workspace/evaluator/.sessions/eval_eval_test_20260820T104927
  ```

  主 Agent 实际 session 中的 Agent 配置和 skill 目录示例：

  ```text
  ~/.pi/workspace/main/.sessions/eval_test_20260820T104927/
  ├── AGENTS.md
  └── .agents/
      └── skills/
          └── demo-skill/
              └── SKILL.md
  ```

- session 的路径片段只保留字母、数字、`.`、`_`、`-`，其他字符替换为 `-`；
- 当前进程内，相同 `(agent_name, 运行时 session 名)` 复用同一个 RPC 子进程；
  会话历史通过 `--session-dir /home/ma-user/.pi/agent/sessions` 落盘，pi 在该目录
  下按 UUID 建 `<uuid>/session.jsonl`；进程退出后可用 `pi --session <uuid>`
  或 `pi --resume` 事后回溯，但本项目不会自动接续上次会话；
- `system_prompt` 通过 `--append-system-prompt` 追加到 Pi 默认系统提示词；
- Agent 配置文件按原文件名复制，不合并；Pi 自动加载 `AGENTS.md` 和
  `CLAUDE.md`；
- skill 位于实际 session 目录的 `.agents/skills`；`--approve` 使 Pi 在 RPC
  启动时加载该项目级目录；
- 每次向 Agent 发起请求对应一个 prompt；启用 User Simulator 后，一个 query
  可以有多个 prompt。每个会话内部串行执行；
- 落盘的 session 目录布局示例：

  ```text
  /home/ma-user/.pi/agent/sessions/
  └── <uuid>/
      └── session.jsonl   # 一行一条 JSONL 事件,可用 pi --session <uuid> 打开或 pi --export 转 HTML
  ```

  该目录由 pi 自动维护，本项目只负责传入 `--session-dir`；如需换位置，修改
  `src/pi_client.py` 顶部常量 `_PI_SESSION_DIR` 即可。

## 4. 运行与验收

部署环境先确认 Pi CLI 可用并已写好上述配置：

```bash
pi --version

python harness_automation.py --harness pi --config configs/config_simple.json
python harness_automation.py --harness pi --config configs/config_user.json
python harness_automation.py --harness pi --config configs/config_simple_eval.json

```