# 单任务轨迹统计 (`scripts/task_status.py`)

每次任务成功失败，兜底`harness_automation.py` 调用 `run_stats(config_file, traj_stats_result, harness_type=config.harness_type)`，对**主 agent**的轨迹做一次
统计，结果写入 `logs/traj_stats_result.json`。

本文说明统计口径、harness 选路与适配器架构，以及**新增一个 harness 时如何接入统计**。

## 1. 分级口径（L0 → L3 漏斗）

统计的最终目的是给每个任务定一个 `task_level`，逐层递进，只依赖 5 个布尔/数值量：

| 量 | 含义 | 来源 |
| --- | --- | --- |
| `has_traj` | 主 agent 存在轨迹文件 | adapter.`find_trajectory` |
| `ge3` | 工具调用次数 ≥ 3 | `metrics["tool_calls"] >= 3` |
| `plain` | 出现过至少一轮纯文字回复（无工具调用） | `metrics["plain_rounds"] > 0` |
| `has_eval` | 有 evaluator 首轮裁决 | 三级裁决回退（见 §3） |
| `score` | 首轮裁决的 completion | 三级裁决回退（见 §3） |

分级规则（`_task_level`，取主 agent 的最好档）：

```
L3 : ge3 & plain & has_eval & score == 1
L2 : ge3 & plain & has_eval & score >= 0.5
L1.5: ge3 & plain & has_eval & score is not None
L1 : ge3 & plain
L0 : has_traj
none: 其余
```

> `token` / `task_done` / `char_len` 等**不在分级里**，只是随结果透传，便于排查。
> 兜底分级**忽略 ge3 门槛**，直接 `has_eval`+`score` 决定L1.5/L2/L3，至少保留 evaluator 裁决档。

## 2. harness
每种 harness 是 `HarnessAdapter` 的一个子类，在`scripts/harness_adapter.py`实现同名方法；新增 harness 只需加一个子类 + 注册一行，主流程 `scripts/task_status.py` 不改。

```
HarnessAdapter (基类)
├── find_trajectory(self, agent_name)          # 子类必须实现：定位该 agent 轨迹文件
├── analyze(self, path, agent_name)            # 子类必须实现：分析轨迹 → metrics dict
├── find_eval_traj(self)                       # 默认 = find_trajectory("evaluator")
└── extract_eval_traj_verdict(self, path)      # 默认实现见 §3，逐行 jsonl 的直接复用
```

已注册的八个子类（`HARNESS_ADAPTER_CLASSES`）：

| harness_type | 类 | 轨迹落盘（沙箱内） | 多轨迹取哪个 |
| --- | --- | --- | --- |
| `openclaw` | `OpenclawAdapter` | `~/.openclaw/agents/<name>/sessions/*.jsonl`（文件名不含 `trajectory`） | 文件**最大**者 |
| `hermes` | `HermesAdapter` | `~/.hermes/profiles/<name>/sessions/session_*.json` | 文件名**最晚**者（自带时间戳 `session_YYYYMMDD_HHMMSS_<hex>.json`） |
| `claude-code` | `ClaudeCodeAdapter` | `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`（顶层 `*.jsonl`） | 文件**最大**者 |
| `dsh` | `DshAdapter` | `~/.dsh/sessions/<name>/<session_name>/<encoded-cwd>/<name>-<session_name>/session.jsonl.zstd`（**zstd 压缩事件流**） | `session_name` 目录**最晚**者（自带时间戳 `*_YYYYMMDDTHHMMSS`） |
| `opencode` | `OpencodeAdapter` | `~/.local/share/opencode/opencode.db`（**SQLite 库，所有 session 共用**；main/evaluator 靠 `session.agent` 字段区分） | 库内 `agent=<name>` 的 session 中 `time_created`**最晚**者 |
| `pi` | `PiAdapter` | `~/.pi/agent/sessions/*.jsonl`（**所有 agent 混在同一目录**，沙箱布局 `<uuid>/session.jsonl`；main/evaluator 靠 session 首行 `cwd` 字段区分） | session 首行 `timestamp`**最晚**者 |
| `grok` | `GrokAdapter` | `~/.grok/sessions/<url-encoded-cwd>/<uuid>/chat_history.jsonl`（**扁平逐行 jsonl**，无 `message`/`role` 包裹；token 在同级 `updates.jsonl`；main/evaluator 靠 encoded-cwd 解码后的 `workspace[-<agent>]` 段区分） | `session_name`（encoded-cwd 末段）**最晚**者 |
| `openjiuwen` | `OpenjiuwenAdapter` | `~/.openjiuwen/sessions/<session_name>_<run_id>.trajectory.jsonl`（**逐行 jsonl、OpenAI 风格消息**：`{role, content, tool_calls, usage_metadata}`；main/evaluator 靠文件名 base 区分 —— base 来自 task_config `queries[].session_name`，evaluator 多一层 `eval_` 前缀；同目录 `<session>.json` 只存对话，不参与统计） | 文件名（`run_id` 时间戳）**最晚**者 |

>其余走「基类 `HarnessAdapter`」兜底如`codex`,主流程靠 `evaluator_use.log` + `run.log` 两级裁决拿到 `has_eval`/`score`，再用一个忽略 ge3 门槛的分级给大致结果。


## 3. evaluator 裁决：三级回退，统一「首轮」

裁决按以下顺序取，**三级都取「首轮」**（与分级约定一致），任一级拿到就用：

```
1. evaluator_use.log      取 turn 最小行        → source = "evaluator_use"
2. run.log [Evaluator]块  取 turn 编号最小的块   → source = "log"
3. evaluator 轨迹          取第一条 rubric+inclination 轮 → source = "eval_traj"
```

- **主源** `logs/evaluator_use.log`（JSONL，每行 `{turn, evaluation:{completion,...}}`），
  取 `turn` 最小的行。该文件通常肯定存在。
- **兜底 1** `run.log` 的 `[Evaluator] turn=N agent=... 输出:` 标记 + 紧随的 JSON 块，
  取 `turn` 编号最小的块（用大括号计数切出完整 JSON）。
- **兜底 2** evaluator 轨迹：`adapter.find_eval_traj()` 定位文件，`extract_eval_traj_verdict()`
  解析。取**第一条**同时含 `rubric_checks` 与 `inclination` 的 assistant 轮，正则抓
  `"completion": <num|null>`。（历史上是取「最后一条」，已改为「第一条」以对齐首轮语义。）

> 兜底 2 只在前两级都拿不到 `completion` 时触发。落地见 `resolve_first_verdict(
> evaluator_use_log, run_log, adapter)`，返回 `(has_eval, score, source)`，
> `source ∈ {"evaluator_use", "run.log", "eval_traj", None}`。


## 4. 各 harness 的轨迹格式与 `analyze` 口径
```
def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
    return {
        "tool_calls": 0, "plain_rounds": 0, "assistant_rounds": 0,
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        "total_tokens": 0,
    }
```


### openclaw（`OpenclawAdapter.analyze`）

- jsonl 每行 `{type:"message", message:{role, content:[...], usage}}`，一行 = 一轮 assistant。
- `tool_calls` = `content` 中 `type=="toolCall"` 部件总数；`plain_rounds` = 无 `toolCall` 的轮数。
- token：每轮 `usage.{input,output,reasoningTokens}` 累加（无则 0）。

### hermes（`HermesAdapter.analyze`）

- OpenAI 风格: `{messages:[...]}`, 工具调用挂在 `msg["tool_calls"]`。
- `tool_calls` = `tool_calls` 列表长度；`plain_rounds` = 无 `tool_calls` 的 assistant 消息数。
- token：由 `read_hermes_state_db()` 读`state.db` 的 `sessions` 表

### claude-code（`ClaudeCodeAdapter.analyze`）

- transcript jsonl，每行 `{type:"assistant", message:{role, content:[{type:thinking|text|tool_use|tool_result}], usage, id}}`。**一轮 assistant 常被拆成多行**，共享同一 `message.id`。
- `tool_calls` = `content` 中 `type=="tool_use"` 部件总数；`plain_rounds` = 整轮无 `tool_use` 的轮数。
- token：同 id `usage` 累加一次
- 目录识别：`~/.claude/projects/<dir>/` 下，`want_eval = (agent_name == "evaluator")` 区分两侧

### dsh（`DshAdapter.analyze`）

- 轨迹是 **zstd 压缩的事件流**, 只统计组装好的 `assistant/message` 事件。
- `data.message = {role:"assistant", content:[{type:text|reasoning|tool-call,...}], id}`
- `tool_calls` = `content` 中 `type=="tool-call"` 部件总数；`plain_rounds` = 无 `tool-call` 的轮数。
- token：每条 `assistant/message` 的 `data.usage.{inputTokens,outputTokens}` 累加

### opencode（`OpencodeAdapter.analyze`）

- 轨迹是**一个共享 SQLite 库** `~/.local/share/opencode/opencode.db`, 靠 `session.agent` 字段区分
- `find_trajectory(agent_name)`：在 `session` 表找 `agent=<name>` 中 `time_created` 最晚的一行；
- `analyze(path, agent_name)` 拿到 db_path 后内部再按 `agent_name` 定位 session_id
- 三张表协作：`session`（每 agent 一行）、`message`（每条消息一行，`data` JSON 里 `role` 为 user/assistant）、`part`（每条消息的内容部件一行，`data` JSON 里 `type`
  为 `text`/`tool`/`step-start`/`step-finish`/`reasoning`）。
- `tool_calls` = 该 session 下 `part` 表中 `type=="tool"` 的行数（每次工具调用一行）；`plain_rounds`
  = 不含 `tool` 部件的 assistant 消息数（纯文字轮）；`assistant_rounds` = `message` 表中
  `role=="assistant"` 的行数。
- token：session 的 `tokens_input`/`tokens_output`/`tokens_reasoning`

### pi（`PiAdapter.analyze`）

- agent 靠每个 session 首行 `type=="session"` 的 `cwd` 字段区分：
  - `main` → cwd 末段 `workspace`（`agent_name=="main"`）
  - 其他 agent → cwd 末段 `workspace-<agent>`
- 其他和openclaw相同

### grok（`GrokAdapter.analyze`）

- 轨迹是**扁平逐行 jsonl**，行 `type` ∈ `system`/`user`/`assistant`/`tool_result`/`reasoning`，
- 落盘`~/.grok/sessions/<url-encoded-cwd>/<uuid>/chat_history.jsonl`。
 agent 靠 encoded-cwd 解码后的 `workspace[-<agent>]` 段区分：
  - `main` → 解码后 `.sessions` 前一段 = `workspace`
  - 其他 agent → `workspace-<agent>`
- `find_trajectory(agent_name)`：`rglob chat_history.jsonl`，按 encoded-cwd 解码出的 agent 名筛选，
  按 `session_name`（encoded-cwd 末段，字典序 = 时间序）取**最晚**者。
- `tool_calls` = assistant 行**顶层** `tool_calls` list 长度累加；
- token：在同级 `updates.jsonl` 的 `turn_completed` 事件里

### openjiuwen（`OpenjiuwenAdapter.analyze`）

- 轨迹是**逐行 jsonl**（`~/.openjiuwen/sessions/<session_name>_<run_id>.trajectory.jsonl`），每行一条
  **OpenAI 风格消息**（`{role, content, tool_calls, usage_metadata}`；
- 主 agent / evaluator **靠文件名 base 区分**：文件名 = `{base}_{run_id}`，
  base 来自 `task_config["queries"][].session_name`（缺省 `main`），evaluator 多一层 `eval_` 前缀
- `tool_calls` = assistant 行**顶层** `tool_calls` 列表元素总数
- token：每行 `usage_metadata.{input_tokens, output_tokens, reasoning_tokens}` 累加；

## 5. 新增一个 harness 的统计接入

绝大多数情况下**只需加一个类 + 注册一行**，主流程不动。步骤：

### 5.1 加一个子类

在 `scripts/harness_adapter.py` 新增一个类，继承 `HarnessAdapter`，实现三个方法：

```python
class FooAdapter(HarnessAdapter):
    """foo harness。

    轨迹: ~/.foo/<布局说明>。
    <消息格式说明：逐行 jsonl 还是单 JSON？content 是 str 还是部件 list？工具调用在哪？
     一轮是否拆多行？token 字段名？>
    """

    name = "foo"

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位该 agent 轨迹文件；无则 None。多候选时取最完整的一次（按大小或时间）。"""
        return None

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """返回 7 个字段的 metrics dict（字段名必须与 openclaw 完全一致）：
        tool_calls / plain_rounds / assistant_rounds /
        input_tokens / output_tokens / reasoning_tokens / total_tokens。
        一轮若拆多行，按某个稳定 id 去重计轮数和 token，避免重复计数。"""
        ...
        return {"tool_calls": ..., "plain_rounds": ..., "assistant_rounds": ...,
                "input_tokens": ..., "output_tokens": ...,
                "reasoning_tokens": ..., "total_tokens": ...}

    def extract_eval_traj_verdict(self, path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """默认实现: 从 evaluator 轨迹 jsonl 兜底解析「首轮」裁决。
        """
        if not path or not os.path.isfile(path):
            return False, None
        return _first_verdict_round(_eval_traj_rounds_from_jsonl(path))
```

### 5.2 注册一行

新增 harness 只需在适配器类注册表加一行:

```python
# 适配器类注册表（落盘路径在各子类 find_trajectory 里内联，无需另维护字典）
HARNESS_ADAPTER_CLASSES: Dict[str, type] = {
    "openclaw": OpenclawAdapter,        # ~/.openclaw/agents/<name>/sessions
    "hermes": HermesAdapter,            # ~/.hermes/profiles/<name>/sessions
    "claude-code": ClaudeCodeAdapter,   # ~/.claude/projects/<encoded-cwd>
    "dsh": DshAdapter,                  # ~/.dsh/sessions/<name>/...
    "opencode": OpencodeAdapter,        # ~/.local/share/opencode/opencode.db
    "pi": PiAdapter,                    # ~/.pi/agent/sessions
    "grok": GrokAdapter,                # ~/.grok/sessions/<encoded-cwd>/<uuid>
    "openjiuwen": OpenjiuwenAdapter,    # ~/.openjiuwen/sessions (base 需 task_config)
    "foo": FooAdapter,                  # ← 加这行 (落盘路径在 FooAdapter 里内联)
}
```

`harness_type` 名字要与 `harness_automation.py` 里的 `harness_type` 字段/`--harness` 值**逐字一致**。

### 5.3 验证

准备一份该 harness 的真实交付件快照（含轨迹 + `evaluator_use.log` + `run.log`），用自测脚本直接实例化 adapter 跑一遍（无需 task_config）：

```python
from pathlib import Path
from scripts.task_status import HARNESS_ADAPTER_CLASSES, has_task_done_marker, resolve_first_verdict

base = Path("path/to/foo_snapshot")          # 快照根
adapter = HARNESS_ADAPTER_CLASSES["foo"]()   # 无参实例化; 落盘路径在子类里内联
# 若快照不在沙箱默认路径, monkeypatch 子类里的路径 helper 指向快照后再调用
traj = adapter.find_trajectory("main")       # 主 agent 名按 config 的 queries
m = adapter.analyze(traj, "main")
ev = adapter.find_eval_traj()                # 应找到 evaluator 轨迹，而非主轨迹
has_eval, score, src = resolve_first_verdict(
    base / "logs/evaluator_use.log", base / "workdir/run.log", adapter)
print(traj.name, m, ev.name if ev else None, has_eval, score, src)
```

> openjiuwen 特例：文件名 base 来自 task_config，自测时用 `HARNESS_ADAPTER_CLASSES["openjiuwen"](task_config=<已 load 的 config dict>)` 构造 adapter，否则退回 `eval_` 前缀启发式（可能定位到旧 session）。

检查：

- `traj` 命中预期文件；`m["tool_calls"]`、`m["assistant_rounds"]`、`m["plain_rounds"]` 数值合理
  （`assistant_rounds` 不应远大于肉眼所见的对话轮数 → 验证去重生效）；
- `ev` 命中 **evaluator** 轨迹（不是主轨迹）；
- `has_eval/score` 与 `evaluator_use.log` 里 turn 最小行的 `completion` 一致；
- 最后用 `run_stats(config_file, out, "foo")` 跑端到端，确认 `logs/traj_stats_result.json`
  的 `task_level` 符合预期。

## 6. 沙箱路径约定（关键）

脚本在**沙箱内**被 `harness_automation` 调用，路径用沙箱的，不是本机：

| 常量 | 沙箱路径 |
| --- | --- |
| `RUN_LOG_PATH` | `/home/ma-user/workspace/workdir/run.log`（字符串，所有 harness 共用） |
| `EVALUATOR_USE_LOG` | `<PROJECT_ROOT>/logs/evaluator_use.log` |
| 各 adapter 落盘路径 | 子类在 `find_trajectory` / helper 里内联 `Path("~/.<harness>/...").expanduser()`（沙箱 `~` = `/home/ma-user`）；opencode 例外是 `~/.local/share/opencode`（SQLite 库），pi 是 `~/.pi/agent/sessions` |
| `PROJECT_ROOT` | `scripts/../`（沙箱 = `/home/ma-user/workspace/openclaw-task`） |


## 7. 输出字段（`logs/traj_stats_result.json`）

```jsonc
{
  "task": "<config 文件名 stem>",
  "config_file": "...",
  "log_file": "...",
  "harness": "claude-code",
  "agents": [{
    "agent": "main",
    "trajectory": "/home/ma-user/.claude/projects/.../<uuid>.jsonl",
    "tool_calls": 9, "assistant_rounds": 9, "plain_rounds": 1,
    "has_trajectory": true, "has_ge3_toolcalls": true, "has_plain_round": true,
    "passed_gate": true,            // L1 门槛 = ge3 & plain
    "has_eval": true,
    "evaluator_completion": 1.0,
    "verdict_source": "evaluator_use",
    "char_len": 12345,
    "task_done": true,
    "level": "L3",
    "input_tokens": ..., "output_tokens": ..., "reasoning_tokens": 0, "total_tokens": 147136
    // token 四字段仅当 total_tokens > 0 时透传
  }],
  "task_level": "L3",
  "best_completion": 1.0
}
```

1. `task_done` 查 `run.log` 的 `【Task_Done】` 标记；`verdict_source` 标记裁决来自哪一级回退。
2. **兜底结果**（基类 `HarnessAdapter`，未识别 harness）额外多一个 `"fallback": true` 字段，提示此为轨迹布局未知时的大致档（分级忽略 ge3，见 §2.2）; `trajectory`/`tool_calls`/ `token` 等轨迹相关字段均为空/0，`passed_gate` 恒 `False`，但 `has_eval`/`evaluator_completion`/`level` 仍有效（近似结果）。

## 8. CLI

```bash
python scripts/task_status.py -c configs/xxx.json \
    [--harness openclaw|hermes|claude-code|dsh|opencode|pi|grok|openjiuwen] \
    [-o logs/traj_stats_result.json]
```
