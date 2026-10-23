# JiuwenSwarm dev-stable 集成说明

项目通过官方 JiuwenSwarm CLI 同款 `/tui` WebSocket 协议运行 JiuwenSwarm Agent，不依赖其 Python 运行时。模型选择、原生技能库和会话存储由服务端管理，客户端只透传 `agents[].model`。使用前先在 JiuwenSwarm 中配置模型服务，再通过 `agents[].model` 为编写、审查等任务指定不同模型。

本版使用 `--harness jiuwenswarm-dev-stable` 或 `harness_type: jiuwenswarm-dev-stable` 选择。旧的 `jiuwenswarm` 入口保留，两者复用同一个客户端和查询循环。

## 部署前置条件

服务端使用 [GitCode 的 dev-stable 分支](https://gitcode.com/openJiuwen/jiuwenswarm/tree/dev-stable)，本次验证提交为 `9f21b447874e83f3aff1646b5f9ae70213d33125`，包名为 `jiuwenswarm`。该分支要求 Python 3.11–3.13，必须与本项目分开安装：它要求 `Pillow>=12.3.0`，而 Hermes 固定 `Pillow==12.2.0`。

先确认 `uv --version` 和 `git --version` 可用。安装只做一次，日常使用按“切换环境 → 初始化 → 配置 → 启动 → 执行”的顺序进行。

### 首次安装

本项目已有 `.venv` 时直接复用；首次创建时执行：

```bash
uv venv --python 3.12 .venv
```

仅运行 JiuwenSwarm 接入所需的项目依赖：

```bash
uv pip install --python .venv aiohttp httpx "pydantic>=2" pyyaml openai zstandard
```

`requirements.txt` 包含其它 Agent 的依赖；仅测试本接入时，安装上面的最小依赖即可。

服务端安装到 `uv` 管理的独立环境中，完整源码地址只在安装时使用：

```bash
uv tool install --python 3.11 "jiuwenswarm @ git+https://gitcode.com/openJiuwen/jiuwenswarm.git@9f21b447874e83f3aff1646b5f9ae70213d33125"
```

### 使用流程

在 VS Code 准备两个 Bash 终端，都从项目根目录开始。以下命令均在 Bash 中执行。

两个终端都先设置 Python 使用 UTF-8，保证终端重定向日志的编码一致：

```bash
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
```

需要长期生效时，将这两行加入 `~/.bashrc`，再执行 `source ~/.bashrc`。设置对之后启动的进程生效。

**1. 切换环境：终端一使用服务端环境。**

```bash
source "$(uv tool dir)/jiuwenswarm/Scripts/activate"
alias jiuwenswarm-dev-stable='jiuwenswarm-start app'
```

`jiuwenswarm-dev-stable` 是本终端的启动别名，执行独立环境中的官方 `jiuwenswarm-start app`。每次新开服务端终端时，先运行上面的环境激活和别名设置。

**2. 初始化：首次安装或升级后执行一次。**

```bash
jiuwenswarm-init
```

**3. 配置：按第 2 节编辑 `~/.jiuwenswarm/config/config.yaml`。**

写入自己的服务地址、模型与真实密钥；已有配置时直接修改，不必反复初始化。

**4. 启动：仍在终端一执行，并保持运行。**

```bash
jiuwenswarm-dev-stable
```

**5. 执行：终端二切换到项目环境，再运行第 4 节的测试命令。**

```bash
source .venv/Scripts/activate
```

两个终端的环境各自独立：启动服务不会让另一个终端自动切换环境。测试提示缺包时，先执行 `python -c "import sys; print(sys.executable)"`，确认指向项目 `.venv`；若正确，再补装本节的最小依赖。

日常使用只需激活两个环境、启动服务、执行测试；配置变更后重启服务。`requirements.txt` 中的 `openjiuwen==0.1.19` 供独立的 openjiuwen harness 使用；JiuwenSwarm 接入只通过 WebSocket 通信。

## 1. 接入方式

每次 harness 运行创建一个 `JiuwenSwarmClient`，内部按 `(agent_name, session_name)` 缓存 `JiuwenSwarmAgent`。同一元组复用同一原生会话，不同元组互相隔离；evaluator 的 `eval_` 会话与主 Agent 自然分开。

- 原生 `session_id` 形如 `harness-<32 位十六进制>`，在进程内随机生成，因此每次运行都从新会话开始，不跨运行续聊。
- 每轮单独建立 WebSocket 连接，首帧必须是 `connection.ack`；按请求编号忽略上一轮迟到的 `chat.processing_status`，避免连续调用提前结束。
- 每轮通过 `chat.send` 发送：`session_id`、`content` 与 `query`（同一段文本）、`mode`、`cwd` / `project_dir` / `workspace_dir`（都指向该 Agent 的 workspace）、`trusted_dirs=[workspace]`、`supports_user_interaction=false`、`agent_ref={"mode": <mode>, "id": "default"}`，以及可选的 `model_name`。
- 只处理 `payload.session_id` 为空或等于自身会话的事件，共享网关上其它会话的事件直接丢弃。
- 服务端返回 `type=res` 且 `ok=false` 时（例如请求参数不被接受），本轮按失败处理并进入重试。
- 取消、超时或异常时先发送 `chat.interrupt` 尽力停止服务端任务，再关闭连接。

### 1.1 事件处理

| 事件 | 处理 |
| --- | --- |
| `chat.delta` | 累积回复文本 |
| `chat.tool_call` | 记录工具调用，组装为 OpenAI 风格消息 |
| `chat.tool_result` | 追加一条 `role=tool` 消息，带 `tool_call_id` 与结果内容 |
| `chat.llm_usage`，或 `chat.final` 内层 `event_type=chat.llm_usage` | 记录本轮 usage |
| `chat.final`（内层 `event_type` 为空或 `chat.final`） | `content` 覆盖累积文本；非团队模式随即结束本轮 |
| `chat.processing_status` 且 `is_processing=false` | 请求编号匹配本轮或未携带编号时结束本轮（两种模式都生效） |
| `chat.error`，或内层 `event_type=team.error` | 本轮失败 |
| `chat.ask_user_question` / `plan.approval_required` | 本轮失败：当前为非交互执行 |

非团队模式在 `chat.final` 后立即返回；团队模式忽略 `chat.final`，等待 `is_processing=false`。服务端始终没有发出完成信号时，本轮等到任务超时。结束时没有任何文本内容，本轮按失败处理。

`chat.error`、人工输入请求和空回复都会先转成失败结果，再进入第 1.2 节的重试，因此一次需要人工审批的任务会消耗 5 次尝试和 4 次 60 秒等待。

### 1.2 超时与重试

`query.timeout` 经 `ExecutionOptions.timeout_seconds` 下发，缺省 3600 秒，覆盖整轮等待。

`make_jiuwenswarm_execute_with_retry` 最多尝试 5 次，间隔 60 秒。每次重试前调用 `clear_session()`：换一个新的原生 `session_id` 并重置首轮注入标记。重试因此会丢掉已累积的多轮上下文，并在下一轮重新注入 `system_prompt` 和配置文件。超时、连接失败、`chat.error`、空回复都会进入重试；5 次全部失败后抛出最后一次的错误。

### 1.3 串行与模式

`JiuwenSwarmClient` 内部用 `asyncio.Lock` 串行化整轮执行，包括 evaluator 的调用，以兼容只允许一个 TUI 客户端的服务版本。排队等待的时间不计入单轮超时。

默认模式为 `code.normal`（服务端归一为 `agent.code.normal`），也可使用 `agent.work.normal`、`team.code.normal`、`team.work.normal` 等服务端原生模式。客户端把以 `team` 开头的模式，以及 `code.team`、`agent.team`，按团队模式处理；`team.code` 属于前者，同样走团队模式。模式名不被服务端接受时，本轮报错并进入重试。

## 2. JiuwenSwarm 配置

模型的服务地址、协议和凭据写入 JiuwenSwarm 原生配置文件；任务配置引用其中的模型别名或模型名。每个 Agent 可以选择不同服务、不同模型。

### 2.1 配置多个服务

编辑初始化生成的 `~/.jiuwenswarm/config/config.yaml`，在 `models.defaults` 下添加模型条目，保留其它配置。下面包含两个服务、三个模型配置；替换服务地址、模型名和密钥，示例中的密钥已隐藏。

新版配置文件只保存用户覆盖项，初始可能只有 `version`、`permissions`、`preferred_language` 等字段，看不到 `models` 时直接在文件末尾补上该段即可。`alias` 是任务引用的名称，`model_client_config.model_name` 才是实际发送给服务的模型名称；多个别名也可以统一指向同一个 DeepSeek 模型。

```yaml
models:
  defaults:
    # 服务一：默认模型
    - alias: deepseek-flash
      is_default: true
      model_client_config:
        client_provider: OpenAI
        model_name: deepseek-flash
        api_base: https://api.deepseek.com
        api_key: "***"
      model_config_obj: {}

    # 服务二：审查服务
    - alias: reviewing
      is_default: false
      model_client_config:
        client_provider: OpenAI
        model_name: your-review-model
        api_base: https://review.example.com/v1
        api_key: "***"
      model_config_obj: {}

    # 服务二的另一个模型
    - alias: review-flash
      is_default: false
      model_client_config:
        client_provider: OpenAI
        model_name: deepseek-flash
        api_base: https://review.example.com/v1
        api_key: "***"
      model_config_obj: {}
```

`alias` 需要在整个文件内唯一，重名时服务端无法据此定位条目。`is_default: true` 建议只设一项，作为未指定模型时的默认选择；服务端命中多个默认项时取第一个。

客户端把 `agents[].model` 原样放进 `chat.send` 的 `model_name` 参数，服务端按模型名或 `alias` 在 `models.defaults` 中查找对应条目。上面第一项和第三项的 `model_name` 都是 `deepseek-flash`，分别位于不同服务：直接传 `deepseek-flash` 时命中默认的那条，要指定服务二的条目则使用唯一别名 `review-flash`。别名重名时服务端返回 “multiple models match”，需要改用不会重复的写法。未配置 `agents[].model` 时不发送 `model_name`，由服务端使用当前选择的模型。

连接地址和执行模式由运行本项目的终端环境变量指定：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `JIUWENSWARM_GATEWAY_URL` | `ws://127.0.0.1:19001/tui` | TUI 网关地址 |
| `JIUWENSWARM_MODE` | `code.normal` | 服务端原生执行模式 |

本版工作区根目录固定为 `~/.jiuwenswarm/agent/jiuwenclaw_workspace`（定义在 `harness_automation.py`）；旧入口仍使用 `~/.jiuwenswarm/agent/workspace`。任务 JSON 的 `workspace_base` 和命令行 `--workspace` 都不影响本接入。

### 2.2 在任务中选择模型

通过 `agents[].model` 引用第 2.1 节的模型别名。本例均使用 `deepseek-flash`；需要不同模型分工时，将 `reviewer.model` 改为 `reviewing` 或 `review-flash`。evaluator 也按同样方式取 `agents` 中名为 `evaluator` 的 `model`，因此它的模型同样要能在 `config.yaml` 中解析到。

```json
{
  "harness_type": "jiuwenswarm-dev-stable",
  "agents": [
    {
      "name": "main",
      "model": "deepseek-flash",
      "system_prompt": "你是一个编码助手。"
    },
    {
      "name": "reviewer",
      "model": "deepseek-flash",
      "system_prompt": "你负责代码审查。"
    }
  ],
  "queries": [
    {
      "agent_name": "main",
      "session_name": "main",
      "text": "编写一个计算器模块，并说明实现。",
      "timeout": 1800,
      "use_simulator": false
    },
    {
      "agent_name": "reviewer",
      "session_name": "review",
      "text": "审查这份实现，指出问题并给出建议：{result_main}",
      "timeout": 1800,
      "use_simulator": false
    }
  ]
}
```

任务同时配置了 `simulator_config` 时，其中与 Agent 同名的段落会覆盖 `agents[].model`，两者不一致时打印一条冲突 warning。覆盖后发送的是该段落 `model` 字段的原文，`provider` 不会拼成前缀；该段落未写 `model` 时仍用 `agents[].model`。该段落里的 `base_url`、`api_key`、`api` 在本接入中不生效，设置了这些字段时日志会提示把服务地址、密钥和协议写进 JiuwenSwarm 的 `config.yaml`。

## 3. Workspace 与会话

工作区按 Agent 名称隔离：`main` 使用 `~/.jiuwenswarm/agent/jiuwenclaw_workspace`，其它 Agent 使用同级的 `jiuwenclaw_workspace-<agent_name>`。新版启动时会把旧的 `agent/workspace` 自动改名，因此本项目同步使用新目录名。启动任务时把 `input_dir` 指定的用户内容、技能和 Agent 配置文件复制进各自 workspace，技能位于 `<workspace>/skills`。服务端的原生默认工作区与会话目录另在 `service_default/agent_default/agent/` 下，本项目通过请求参数指定自己的工作区。

```text
~/.jiuwenswarm/
├── agent/
│   ├── jiuwenclaw_workspace/           # 本项目 main 的 workspace
│   │   ├── AGENT.md                    # 项目配置文件
│   │   ├── SOUL.md
│   │   ├── USER.md
│   │   ├── IDENTITY.md
│   │   ├── skills/                     # 项目技能，首轮注入声明的 SKILL.md
│   │   └── ...
│   └── jiuwenclaw_workspace-evaluator/ # 其它 Agent：<工作区名>-<agent_name>
│       └── skills/
├── service_default/agent_default/agent/
│   ├── jiuwenclaw_workspace/           # 新版服务端原生工作区与技能库
│   └── sessions/                       # 原生会话记录，按 session_id 命名
└── config/
    └── config.yaml
```

原生 `session_id` 每个 `(agent_name, session_name)` 一个。`session_name` 由任务配置的 `session_name` 拼上 `run_id` 得到，同一会话的后续轮次复用同一个原生 `session_id`，不同会话互不可见。会话记录是否落盘、保留多久由服务端决定，本项目的验收不依赖这些目录。

配置了 `input_dir.agent_dir` 时，每个 Agent 的 `config` 中列出的文件会从 `agent_dir/<文件名>` 复制到该 Agent 自己的 workspace，覆盖同名文件。未配置 `agent_dir` 时不做复制，首轮注入读取的是各 workspace 中已有的内容。各 Agent 的目录独立，互不影响。

用户目录和项目技能的复制同样是覆盖语义：`input_dir.user_dir` 下的条目按名称复制进每个 Agent 的 workspace，目标同名项先删除再复制；`skill_dir` 下的技能复制到 `<workspace>/skills`，同名技能目录整体替换。改用 `user_dir.map_file` 时按映射表逐条复制，`~` 解析为第一个 query 所用 Agent 的 workspace，只影响该 Agent。

首轮依次注入 `system_prompt`、`agents[].config` 列出的配置文件、`agents[].skills` 声明的技能正文及其目录、本轮 query；文件不存在则跳过。后续轮次只发送 query。新版仅自动发现服务端注册目录中的技能，因此项目技能需显式注入；技能的脚本和资源仍可由工具按目录读取。服务端原生技能库与技能演进继续由服务端管理。

Evaluator 的会话名为 `eval_<base>_<run_id>`，`base` 取 `evaluate.session_name`，未设置时取 `query.session_name`。同一个 query 复用同一个原生会话，不同 query、不同 run 各自独立。

两处 evaluator 辅助逻辑依赖 OpenClaw 风格的 `client.gateway` 属性，本接入没有该属性，会直接跳过：

- `_reset_session` 不执行，评估前不清空会话，历次判词留在上下文中；
- `_push_review_files` 不执行，被审查文件不会复制到 evaluator 工作区的 `_under_review/` 目录。evaluator 仍会收到产物文件的路径指针，可以自行读取被测 Agent 的 workspace。

## 4. 运行与验收

保持终端一的网关运行，按项目通用配置准备模拟用户和评估器后，在已激活项目 `.venv` 的终端二执行：

```bash
python harness_automation.py --harness jiuwenswarm-dev-stable --config configs/config_simple.json
python harness_automation.py --harness jiuwenswarm-dev-stable --config configs/config_user.json
python harness_automation.py --harness jiuwenswarm-dev-stable --config configs/config_simple_eval.json
```

保存测试日志时，可使用：

```bash
python -u harness_automation.py --harness jiuwenswarm-dev-stable --config configs/config_simple.json > xxx.log 2>&1
```

`-u` 让日志及时写入，`2>&1` 同时保存错误输出；编码由前面设置的 UTF-8 环境变量控制。

`--harness` 覆盖任务文件的 `harness_type`；运行自己的任务时，将 `--config` 改为对应文件路径。本接入没有前置健康检查，网关未启动时第一条 query 会以连接失败告终并进入 60 秒重试，因此开始前先确认网关已就绪。测试时避免与手动 TUI 抢占连接。

`evaluate.rubrics_ref` / `scoring_ref` 的相对路径按 `input_dir.user_dir.path` 解析，不按任务配置文件所在目录。`config_simple_eval.json` 的 `user_dir.path` 为 `configs`，因此评分文件 `simple_eval_rubrics.json` 必须位于 `configs/` 下；文件缺失时配置加载阶段直接报 `FileNotFoundError`，不会静默跳过评估。

第二项应输出 `【Task_Done】`；第三项还应输出 Evaluator 评分，轨迹保存到 `logs/trajectories/<run_id>/eval_test.json`。

轨迹中的 `tool_calls` 由 `chat.tool_call` / `chat.tool_result` 事件解析得到，与其它 harness 的落盘结构一致。本接入的成功结果 `stop_reason` 恒为 `complete`，所以轨迹里的 `evidence_incomplete` 始终是 `false`。`logs/traj_stats_result.json` 目前走通用兜底适配器（`scripts/harness_adapter.py` 未注册 jiuwenswarm）：兜底适配器不读取轨迹文件，工具次数和轮次均为 0，分级只依据 `evaluator_use.log` 与 `run.log` 的裁决，结果带 `"fallback": true` 标记。`chat.llm_usage` 的用量只保存在本轮的 `ExecutionResult.usage`，没有写入轨迹，token 统计暂缺。

更多操作参见 [JiuwenSwarm dev-stable 文档](https://gitcode.com/openJiuwen/jiuwenswarm/tree/dev-stable/docs)。

## 5. 两版的核心差异与改动原因

这里比较的是 JiuwenSwarm 服务端的两种安装来源：

- **旧版：`workswarm==0.2.7` 发布包**，依赖底层 SDK `openjiuwen==0.1.19`。SDK 版本不能代指旧版服务端版本。
- **本版：GitCode `jiuwenswarm` 的 dev-stable 源码**，固定提交 `9f21b447874e83f3aff1646b5f9ae70213d33125`，依赖 GitCode 上的 agent-core、agent-runtime、deepsearch。直接升级 `workswarm` 不会切换到该源码实现。

两版复用 `/tui` 客户端、任务格式和评估流程，常规功能接近。**实际版本由启动的服务端决定**：两个 harness 入口连接同一个网关时，使用的就是同一版服务。对比时应分别启动对应环境，或用 `JIUWENSWARM_GATEWAY_URL` 指向各自的网关。

| 差异 | 为什么需要改动 | 本版处理 |
| --- | --- | --- |
| 安装依赖 | dev-stable 要求 `Pillow>=12.3.0`，与 Hermes 的 `Pillow==12.2.0` 冲突 | 服务端独立安装，本项目通过 WebSocket 调用 |
| 完成事件 | 上一轮结束状态可能迟到，导致第二轮被提前结束、返回空回复 | 按本轮请求编号过滤迟到状态 |
| 技能发现 | 新版自动发现服务端注册目录，复制到项目工作区的技能可能无法进入上下文 | 首轮注入声明的 `SKILL.md` 和目录 |
| 目录布局 | 新版迁移 `agent/workspace`，继续使用旧路径会重新创建旧目录 | 新入口使用 `agent/jiuwenclaw_workspace`，会话由服务端管理 |

本次价值是兼容指定 dev-stable 运行时，尚未验证速度或模型效果提升。项目技能注入不等同于原生注册和演进，团队、分布式协作也未在本项目验收；旧版已满足现有任务时，不必仅为普通问答和评估流程切换。
