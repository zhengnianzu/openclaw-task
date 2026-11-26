# TaiChu CLI 集成说明

| 项 | 位置 |
|---|---|
| 适配器 | `src/taichu_client.py`（批跑用，沙箱内直接起进程，不含 docker 逻辑） |
| docker 启动壳 | `test/taichu_docker.py`（**仅本地测试**，不参与批跑） |
| 入口 | `python harness_automation.py --harness taichu --config <task>.json` |
| 沙箱镜像 | `swr.cn-east-4.myhuaweicloud.com/rl_team/taichu-cli-linux:<版本>` |
| hive 侧 | `openclaw-hive/prepare_config.py` 的 `_FRAMEWORK_LAYOUTS["taichu"]` 与 `_prepare_taichu` |

适配器只做三件事：**按 taichu 原生约定组织目录、把 CLI 跑起来、读回本次证据**。
模型配置、system prompt 组装、技能发现都交给 taichu 自己。

---

## 1. 核心交互逻辑

### 1.1 一轮 = 一次 CLI 进程

taichu 没有常驻服务。每次 `agent.execute()` 都起一个 `taichu-cli` 子进程，跑完一轮就退出；
**多轮之间的上下文由 taichu 自己持久化**（`user/history/` 会话库 + `session_workspaces/`），
客户端只需要记住 `session_id`，下一轮带上 `--session <id>`。

```
第 1 轮   taichu-cli … run --new-session        --message <q1>   → 返回 session_id=S
第 2 轮   taichu-cli … run --session S          --message <q2>   → 同一个 S
第 N 轮   taichu-cli … run --session S          --message <qN>
```

### 1.2 自动交互的驱动方

多轮交互循环**不在 client 里**，而在 `src/executor.py::execute_queries`。taichu 只提供三个回调：

| 回调 | taichu 实现 |
|---|---|
| `get_agent_fn(name, session)` | `TaichuClient.get_agent` |
| `execute_with_retry_fn(agent, text, opts)` | `make_taichu_execute(client)` |
| `simulator_factory()` | 通用 `User_simulator`（与其他 harness 共用） |

完整时序：

```
harness_automation._run_taichu
 ├─ build_taichu_client()                       # TaichuPaths.resolve → TAICHU_HOME
 ├─ client.__aenter__
 │    ├─ paths.ensure()                         # 建 configs/ user/agent_instances/ evidence/
 │    ├─ check_runtime()                        # taichu-cli 可执行？否则立刻失败
 │    └─ 读 configs/.taichu.json → base_config
 ├─ _setup_workspaces()                         # 通用：user_dir / map_file / skills / agents[].config
 ├─ TaichuAgentManager.setup_agent(每个 agent)  # 只补 persona 目录 + 登记 agent
 └─ execute_queries(...)
      for query:
        for turn in 1..max_turn:
          agent = get_agent(agent_name, session_name)     # (agent, session) 首见即建
          result = make_taichu_execute → agent.execute(text)
          ── 见 1.3 ──
          reply = result.content
          evaluator（可选）按 eval_step 评审本轮
          user_reply = simulator.chat(reply)              # 模拟用户给出下一句
          若含【Task_Done】/【Task_Failed】→ 结束；否则 text = user_reply，进入下一轮
```

### 1.3 `TaichuAgent.execute` 单轮细节

```
execute(text, options)
 ├─ async with client.execution_lock()          # 全局一把锁：所有 agent 共用 user-data-root
 ├─ 前置检查：会话曾被中断(_unusable)→拒绝；空查询/非法 timeout → 报错
 ├─ _new_evidence_dir()                         # evidence/<agent>/，先 rmtree 再建 → 满足「已存在且为空」
 ├─ _build_command()                            # 见 1.4
 ├─ create_subprocess_exec(cwd=home, start_new_session=True)
 ├─ wait_for(communicate(), timeout + TAICHU_TIMEOUT_GRACE)
 │    └─ 超时/取消 → killpg 整个进程组，标记 _unusable，返回 stop_reason=timeout
 ├─ _parse_cli_json(stdout)                     # 取最后一行合法 JSON，跳过 plugin diagnostics 噪声
 ├─ _preflight_error()                          # 无 artifacts = 写证据前就失败 → 按错误码直接抛
 ├─ 校验 session_id 未漂移；首轮记下 session_id
 ├─ _collect_evidence()
 │    ├─ 读 taichu-cli-eval-receipt.json        # execution / candidate / nativeEvidence
 │    ├─ 证据路径必须落在本次 evidence 目录内
 │    ├─ coverage 非 canonical 或有 evidenceGaps → 记为缺口
 │    └─ read_taichu_ledger(run_id 切片)        # tool_calls / usage / provider_errors
 └─ ExecutionResult(success, content=assistantText, session_id, run_id, persona_id,
                    usage, tool_calls, evidence_incomplete, evidence_dir, error_message)
```

成功判定：CLI 返回 `status=completed` **且** receipt 里 `execution.status=completed` **且** 拿到了 `session_id`。

失败时 `error_message` 优先取 ledger 里 `model_output.assistant.error_message`（模型原始报错），
其次取 stderr 中去掉 `taichu-cli plugin diagnostics` 噪声后的最后一行。

### 1.4 下发给 CLI 的命令

```bash
taichu-cli \
  --config        $TAICHU_HOME/configs/.taichu.json \
  --user-data-root $TAICHU_HOME/user \
  --no-color \
  [--agent-instance-id <persona>]          # 仅非 main 的 agent 下发
  --output eval-json run \
  --new-session | --session <session_id> \
  --workspace-root $TAICHU_HOME/user/agent_instances/<persona>/workspace \
  --evidence-dir   $TAICHU_HOME/evidence/<agent> \
  --timeout-seconds <query.timeout，默认 3600> \
  --max-turns       <TAICHU_MAX_TURNS，默认 60> \
  --message '<query>'
```

为什么是这些参数：

- **`--output eval-json`**：只有这个模式支持 `--workspace-root`（共享 workspace）和证据目录；
  普通 `--output json` 带 `--workspace-root` 会直接报错退出。
- **`--evidence-dir` 必须已存在且为空**：CLI 的硬性要求，所以每轮先清空 `evidence/<agent>/`。
  沙箱本身是隔离的，用固定路径便于 hive 直接上传。
- **`--agent-instance-id`**：taichu 0.2.0 的分身（persona）。`main` 用内置 `sys_celia_main`，
  不传；其余 agent 以自身名称为 persona id。
- **`--max-turns`**：单轮内 LLM/tool 往返的上限，包含 taichu 自带的 completion-recovery 轮。

### 1.5 解析 CLI 输出

stdout 最后一行是本轮结果（前面可能有 `taichu-cli plugin diagnostics: …`）：

```json
{"protocol":"eval-json/v1","status":"completed","session_id":"…","run_id":"…",
 "artifacts":{"native_ledger_path":"native/taichu-session-ledger.jsonl",
              "receipt_path":"taichu-cli-eval-receipt.json"},
 "artifact_sha256":"…","error":null}
```

- **有 `artifacts`**：正常读 receipt + ledger。
- **无 `artifacts`**（如 `EVIDENCE_UNAVAILABLE`）：CLI 在写证据前就失败，`_preflight_error`
  按错误码直接抛，不会去读不存在的 receipt。

### 1.6 读 ledger

`evidence/<agent>/native/taichu-session-ledger.jsonl` 是原生事件流。续接会话时它会带上前几轮的
历史前缀，所以 `read_taichu_ledger` 先按 `run_id` 定位本轮的 `run_bootstrap`/`agent_start`，只统计其后：

| 事件 | 取什么 |
|---|---|
| `tool_call` | 工具名、参数；按 `tool_call_id` 挂起等待结果 |
| `tool_result` | 文本输出、`is_error`、耗时；与 `tool_call` 配对 |
| `model_output` | 助手文本、`usage`（累加 token）、`error_message`（模型报错原文） |

有未配对的 `tool_call`、无 `model_output`、或找不到本轮 `run_id` 时标记 `complete=False`，
最终反映为 `ExecutionResult.evidence_incomplete=True`。

---

## 2. 目录布局

`TAICHU_HOME` 默认 `/home/ma-user/taichu`，即 hive 的 `harness_dir`。

```text
$TAICHU_HOME/
├── configs/.taichu.json                  # hive 预注入，唯一模型配置来源
├── evidence/<agent>/                     # 固定证据目录，每轮调用前清空
│   ├── taichu-cli-eval-receipt.json
│   └── native/taichu-session-ledger.jsonl
└── user/                                 # --user-data-root（所有 agent 共用）
    ├── history/                          # taichu 原生会话库
    ├── interaction/
    └── agent_instances/
        └── <persona>/                    # main → sys_celia_main；其余 = agent 名
            ├── agent_config/persona_policy.json   # 客户端补 {}，表示继承 .taichu.json
            ├── meta_data/                # IDENTITY.md / SOUL.md / … + skills/
            ├── workspace/                # --workspace-root，同 persona 所有 session 共用
            ├── session_workspaces/       # taichu 为每个 session 自动创建
            └── memory/
```

通用工作区配置的落点（由 `TaichuWorkspaceManager` 决定）：

| 任务配置 | 落到 |
|---|---|
| `input_dir.user_dir` / `map_file` 中的 `~` | `agent_instances/<persona>/workspace/` |
| `agents[].skills` | `agent_instances/<persona>/meta_data/skills/<skill>/` |
| `agents[].config`（`SOUL.md`、`USER.md` 等） | `agent_instances/<persona>/meta_data/` |

persona 只是一个目录：`persona list` 以目录为准，因此客户端只补目录结构，不调用 `persona create`。

---

## 3. 模型配置

**唯一来源是 `$TAICHU_HOME/configs/.taichu.json`**，客户端不派生、不改写。它由 hive 在起沙箱前
预注入（`openclaw-hive/prepare_config.py::_prepare_taichu`）：

- `uploads/.taichu.json` 作为模板；
- `user_proxy_model.json` 里除 `user_simulator` 外的每个 agent，注册成同名 provider；
- `base_url` 统一规范为以 `/v1` 结尾（`normalize_base_url`）；
- **协议强制为 OpenAI**：任何非 `openai-completions` 的 `api`（如 `anthropic-messages`、
  `openai-responses`）都改写为 `openai-completions` / `wire_protocol: chat-completions` 并告警。

```json
{
  "providers": [
    {"name": "default",   "base_url": "http://…/v1", "api_key": "…",
     "models": [{"id": "qwen4-max", "wire_protocol": "chat-completions"}]},
    {"name": "evaluator", "base_url": "http://…/v1", "api_key": "…",
     "models": [{"id": "deepseek-v4.1-flash", "wire_protocol": "chat-completions"}]}
  ],
  "default_provider": "default",
  "default_model": "qwen4-max"
}
```

当前所有 agent 都使用 `default_model`。`agents[].model` 与之不同时，`setup_agent` 会告警：
按 agent 选模型需要写进该 persona 的 `agent_config/persona_policy.json`（taichu 原生机制）。

`tool_manifest` 可选。沙箱镜像里没有 `/tools`，声明了也会导致 CLI 以 `EVIDENCE_UNAVAILABLE`
在执行前失败——批跑配置里不要写，或确保工具包确实存在。

---

## 4. 并发、失败与重试

- **串行执行**：所有 agent 共用一个 user-data-root（`history/`、`interaction/` 同库），
  客户端用一把全局锁 `execution_lock()` 串行化。并发由 hive 层按沙箱横向扩展。
- **不重试**：`make_taichu_execute` 只执行一次，失败直接抛 `TaichuHarnessError`。
  taichu 自带 completion-recovery，预算由 `--max-turns` 控制；在适配器里重放会把有副作用的
  工具调用再执行一遍，污染轨迹。
- **会话中断不可续**：超时或取消后该 agent 标记 `_unusable`，后续请求直接拒绝，避免在半截状态上续写。

| 情况 | 表现 |
|---|---|
| 沙箱内没有 taichu-cli | `__aenter__` 即报 `找不到可执行的 taichu-cli`，提示用 `TAICHU_CLI` 指定 |
| 写证据前失败（如 `EVIDENCE_UNAVAILABLE`） | `TaiChu 在执行前失败（exit=…, code=…）：…` |
| 模型调用失败 | `TaiChu 执行失败（exit=5, code=PROMPT_FAILED）: <模型原始报错>` |
| 超时 | 先让 taichu 走完自己的 deadline 并落证据；超过 `TAICHU_TIMEOUT_GRACE` 再杀进程组 |
| 证据不完整 | `ExecutionResult.evidence_incomplete=True`（coverage 非 canonical / 有 gap / ledger 不完整） |

---

## 5. 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `TAICHU_HOME` | `/home/ma-user/taichu` | 配置、用户数据、证据的根目录 |
| `TAICHU_CLI` | `taichu-cli` | CLI 可执行路径 |
| `TAICHU_MAX_TURNS` | `60` | 单轮内 LLM/tool 轮次上限 |
| `TAICHU_TIMEOUT_GRACE` | `60` | 本地强杀比 taichu 自身 deadline 多等的秒数 |
| `TAICHU_TOOLS` | 空 | 工具包目录，仅 docker 启动壳挂载到 `/tools` |

---

## 6. 证据与 hive 回收

hive 的 `upload_paths` 会回收：

- `$TAICHU_HOME/evidence` —— 每个 agent 最后一轮的 receipt + ledger；
- `$TAICHU_HOME/user/agent_instances` —— 每个 persona 的 workspace、meta_data、session_workspaces、memory。
  上传前 hive 会清掉各 persona 的 `meta_data/skills`（避免把 `node_modules` 等依赖副本传上 OBS）。

> evidence 目录每轮都会清空，回收到的是**最后一轮**的证据；
> 完整的多轮历史在 `user/history/` 与 ledger 的历史前缀里。

| 证据文件 | 内容 |
|---|---|
| `taichu-cli-eval-receipt.json` | `execution`（status/sessionId/runId/assistantText/errorCode）、`candidate`（cliVersion、provider、model、agentInstanceId、toolBundleSha256）、`nativeEvidence`（coverage、evidenceGaps、sha256） |
| `native/taichu-session-ledger.jsonl` | 原生事件流：`model_input` / `model_output` / `tool_call` / `tool_result` 等 |

---

## 7. 本地测试（docker 启动壳）

`test/taichu_docker.py` 用子类把同一个客户端换成 `docker run` 启动，**只用于开发调试**：

| 覆盖点 | 沙箱（src） | docker（test） |
|---|---|---|
| `TaichuPaths.cli_*` | 本机路径 | `--config` → `/config/.taichu.json`，`TAICHU_HOME` → `/trial` |
| `TaichuAgent._launch_prefix` | `[taichu-cli]` | `docker run --rm --name … -v …` + 镜像 |
| `TaichuAgent._cleanup_launch` | 无操作 | 超时后 `docker rm -f` |
| `TaichuClient.check_runtime` | CLI 可执行 | `docker image inspect <镜像>` |

persona / workspace / evidence / ledger 逻辑完全复用 `src/taichu_client.py`。

```bash
# 沙箱形态
python3 test/test_taichu_client.py                       # 单元检查
TAICHU_E2E=1 python3 test/test_taichu_client.py          # 本机有 taichu-cli 时做真实调用

# docker 形态（需已 docker load 或 pull taichu-cli-linux:<版本>）
python3 test/test_taichu_docker.py                       # 单元检查
TAICHU_DOCKER_E2E=1 python3 test/test_taichu_docker.py   # 真实容器调用
```

---

