"""
harness 轨迹落盘位置 (沙箱内路径, 脚本在沙箱里被 harness_automation 调用):
  - openclaw   : ~/.openclaw/agents/<agent-name>/sessions/
  - hermes     : ~/.hermes/profiles/<agent-name>/sessions/session_*.json
  - claude-code: ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
                 (主 agent 目录名不含 "evaluator"; evaluator 目录名含 "evaluator")
  - dsh        : ~/.dsh/sessions/<agent>/<session_name>/<encoded-cwd>/
                 <agent>-<session_name>/session.jsonl.zstd  (zstd 压缩事件流)
  - opencode   : ~/.local/share/opencode/opencode.db (SQLite, 所有 session 共用一个库)
                 main / evaluator 靠 session.agent 字段区分, 工具调用在 part 表
                 (type=='tool' 行), 逐轮文本/工具在 message+part 表
  - pi         : ~/.pi/agent/sessions/*.jsonl, 工具调用在 content 部件 type=='toolCall'
  - grok       : ~/.grok/sessions/<url-encoded-cwd>/<uuid>/chat_history.jsonl (扁平逐行,
                 type=assistant 行顶层 tool_calls; token 在同级 updates.jsonl 的
                 turn_completed 事件里; agent 靠 encoded-cwd 解码后的 workspace[-<agent>] 段区分)
  - openjiuwen : ~/.openjiuwen/sessions/<session_name>_<run_id>.trajectory.jsonl
                 (逐行 jsonl、OpenAI 风格消息: 工具调用在顶层 tool_calls, token 在
                 usage_metadata; 文件名 base 来自 task_config queries[].session_name,
                 evaluator 多一层 eval_ 前缀; 同目录 <session>.json 只存对话记录不统计)
  - codex： 暂未保存session
  (其余 harness 走「基类 HarnessAdapter」兜底: 不解析轨迹, 只靠 evaluator_use.log +
   run.log 给大致结果, 分级忽略 ge3 门槛, 结果带 "fallback": True)
"""


from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote
import sqlite3
try:
    import zstandard
except ImportError:  # 部署环境缺 zstandard 时降级:DSH 轨迹解析返回空,其余 harness 不受影响
    zstandard = None

evaluator_agent = "evaluator"

# ============================================================================
# harness 适配器
# ============================================================================
def _first_verdict_round(round_texts: List[str]) -> Tuple[bool, Optional[float]]:
    """从按顺序的各轮裁决文本里取「首轮裁决」(第一条同时含 rubric_checks+inclination 的轮),
    正则抓 completion。
    """
    for txt in round_texts:
        if "rubric_checks" in txt and "inclination" in txt:
            return _verdict_from_text(txt)
    return False, None


class HarnessAdapter:
    """单个 harness 的轨迹采集适配器 (基类)。

    子类需实现:
      find_trajectory(self, agent_name)   : 定位主 agent 轨迹文件 (无则 None)
      analyze(self, path)                 : 分析轨迹, 返回 metrics dict (含 token 字段)
      extract_eval_traj_verdict(self, p)  : 从 evaluator 轨迹兜底解析裁决 (has_verdict, completion)

    find_eval_traj 默认实现 = find_trajectory(evaluator_agent)。

    基类自身即可作为未识别 harness 的兜底：兜底采用忽略 ge3 门槛的分级, 给出大致档。

    task_config: 可选的任务配置原始 dict。子类需要 task_config 推算落盘路径时
    (如 openjiuwen 的 session 文件名 = query.session_name + run_id) 通过
    ``self.task_config`` 读取;None 时子类应退回启发式。由 get_adapter 在构造时注入。
    """

    name: str = "base"

    def __init__(self, task_config: Optional[Dict[str, Any]] = None) -> None:
        self._task_config: Dict[str, Any] = task_config if isinstance(task_config, dict) else {}

    @property
    def task_config(self) -> Dict[str, Any]:
        """任务配置原始 dict(未注入则为 {})。"""
        return self._task_config

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        return None

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        return {
            "tool_calls": 0, "plain_rounds": 0, "assistant_rounds": 0,
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "total_tokens": 0,
        }

    def find_eval_traj(self) -> Optional[Path]:
        """默认: evaluator 轨迹 = 主 agent 轨迹同布局, agent 名换成 evaluator_agent。

        兜底下 find_trajectory 恒 None, 故这里也 None (不解析 evaluator 轨迹, 让
        resolve_first_verdict 只用 evaluator_use.log + run.log 两级裁决)。
        """
        return self.find_trajectory(evaluator_agent)

    def extract_eval_traj_verdict(self, path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """默认实现: 从 evaluator 轨迹 jsonl 兜底解析「首轮」裁决。
        """
        if not path or not os.path.isfile(path):
            return False, None
        return _first_verdict_round(_eval_traj_rounds_from_jsonl(path))


class OpenclawAdapter(HarnessAdapter):
    name = "openclaw"

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位指定 agent 的 assistant 轨迹 (.jsonl, 文件名不含 trajectory)。
        多条候选时取「文件最大者」(通常也是最完整的一次运行)。
        """
        sessions_dir = Path(f"~/.openclaw/agents/{agent_name}/sessions").expanduser()
        if not sessions_dir.is_dir():
            return None
        cands = [
            p for p in sorted(sessions_dir.iterdir())
            if p.suffix == ".jsonl" and "trajectory" not in p.name
        ]
        if not cands:
            return None
        return max(cands, key=lambda p: p.stat().st_size)

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析一条 assistant 轨迹并提取 token 用量。

        返回 dict: {tool_calls, plain_rounds, assistant_rounds,
                    input_tokens, output_tokens, reasoning_tokens, total_tokens}
          tool_calls       : 全轨迹中 toolCall 的总次数
          plain_rounds     : 不带工具调用的 assistant 轮数 (只有 thinking / text)
          assistant_rounds : assistant 消息轮数
          *_tokens         : 各 assistant 消息 usage 字段累加 (无 usage 时 = 0)
        """
        tool_calls = 0
        plain_rounds = 0
        assistant_rounds = 0
        input_tk = output_tk = reasoning_tk = 0

        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue

                assistant_rounds += 1
                # token 用量: 每条 assistant 消息可能有 usage
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    input_tk += usage.get("input") or 0
                    output_tk += usage.get("output") or 0
                    reasoning_tk += usage.get("reasoningTokens") or 0

                content = msg.get("content")
                if isinstance(content, str):
                    parts_types = ["text"] if content else []
                elif isinstance(content, list):
                    parts_types = [p.get("type") for p in content if isinstance(p, dict)]
                else:
                    parts_types = []

                n_tc = parts_types.count("toolCall")
                tool_calls += n_tc
                if n_tc == 0:
                    # 只有 reasoning(thinking) 和 content(text)，没有 toolCall
                    plain_rounds += 1

        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": reasoning_tk,
            "total_tokens": input_tk + output_tk + reasoning_tk,
        }


class HermesAdapter(HarnessAdapter):
    name = "hermes"

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位指定 agent 的 Hermes 轨迹 (profiles/<agent>/sessions/session_*.json)。
        多条候选时取「时间最晚者」: session 文件名形如 session_YYYYMMDD_HHMMSS_<hex>.json,
        """
        sessions_dir = Path(f"~/.hermes/profiles/{agent_name}/sessions").expanduser()
        if not sessions_dir.is_dir():
            return None
        cands = [
            p for p in sessions_dir.iterdir()
            if p.suffix == ".json" and p.name.startswith("session_")
        ]
        if not cands:
            return None
        return max(cands, key=lambda p: p.name)

    def read_hermes_state_db(self, agent_name: str):
        db_path = Path(f"~/.hermes/profiles/{agent_name}/state.db").expanduser()
        if not os.path.isfile(db_path):
            return None
        try:
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT input_tokens, output_tokens, reasoning_tokens "
                "FROM sessions LIMIT 1"
            ).fetchone()
            con.close()
            if not row:
                return None
            inp = row["input_tokens"] or 0
            out = row["output_tokens"] or 0
            reas = row["reasoning_tokens"] or 0
            return {
                "input_tokens": inp,
                "output_tokens": out,
                "reasoning_tokens": reas,
                "total_tokens": inp + out + reas,
            }
        except Exception:
            return None

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 Hermes profiles session json (OpenAI 风格 event-stream {messages: [...]}).

        返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}:
          tool_calls       : assistant 消息 tool_calls 字段里工具调用总数
                             (Hermes/OpenAI 风格: content 是纯文本字符串, 工具调用挂在
                             msg["tool_calls"] = [{type:"function", function:{...}}, ...],
                             不在 content)
          plain_rounds     : 不带 tool_calls 的 assistant 消息数 (只有 content/reasoning)
          assistant_rounds : assistant 消息总数
          *_tokens         : Hermes session 无 usage 字段, 读 state.db
        """
        tool_calls = plain_rounds = assistant_rounds = 0
        token_info = self.read_hermes_state_db(agent_name) or {
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0,
        }
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)

        for msg in (data.get("messages") or []):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            assistant_rounds += 1
            # 工具调用在 msg["tool_calls"] (OpenAI 风格 list), 不在 content 部件里
            tcs = msg.get("tool_calls") or []
            n_tc = len(tcs) if isinstance(tcs, list) else 0
            tool_calls += n_tc
            if n_tc == 0:
                plain_rounds += 1
        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            **token_info,
        }


    def extract_eval_traj_verdict(self, json_path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """从 hermes evaluator session json 兜底解析「首轮」裁决。

        容器是 OpenAI 风格 {messages:[{role, content, ...}]} (单个 JSON, 非逐行 jsonl),
        """
        if not json_path or not os.path.isfile(json_path):
            return False, None
        try:
            with open(json_path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False, None
        round_texts = [
            msg.get("content") if isinstance(msg.get("content"), str) else ""
            for msg in (data.get("messages") or [])
            if isinstance(msg, dict) and msg.get("role") == "assistant"
        ]
        return _first_verdict_round(round_texts)


class ClaudeCodeAdapter(HarnessAdapter):
    name = "claude-code"

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位 claude-code session .jsonl。

        agent_name == "evaluator" -> evaluator 侧目录; 否则 -> 主 agent 侧。
        只取顶层 *.jsonl(不进子目录), 按文件大小取最大者。无则 None。
        """
        projects = Path("~/.claude/projects").expanduser()
        if not projects.is_dir():
            return None
        want_eval = (agent_name == evaluator_agent)
        cands: List[Path] = []
        for d in projects.iterdir():
            if not d.is_dir():
                continue
            is_eval = evaluator_agent in d.name.lower()
            if is_eval != want_eval:
                continue
            cands.extend(p for p in d.iterdir() if p.is_file() and p.suffix == ".jsonl")
        if not cands:
            return None
        return max(cands, key=lambda p: p.stat().st_size)

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 claude-code transcript jsonl (主 agent 轨迹)。

        返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}, 口径同 openclaw:
          tool_calls       : content 中 type=="tool_use" 部件的总数
          plain_rounds     : 不带 tool_use 的 assistant 轮数 (只有 thinking/text)
          assistant_rounds : assistant 轮数 —— 按 message.id 去重 (一轮可能拆成多行分段)
          *_tokens         : 每轮 usage 累加一次(同 id 多行只算一次, 避免分段重复计数);
        """
        tool_calls = 0
        plain_rounds = 0
        assistant_rounds = 0
        input_tk = output_tk = 0
        seen_ids: set = set()
        # 每轮(按 message.id)的工具调用数, 用于判定该轮是否 plain
        round_tool: Dict[Any, int] = {}

        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                mid = msg.get("id")
                is_new_round = mid not in seen_ids
                if is_new_round:
                    seen_ids.add(mid)
                    assistant_rounds += 1
                    # token 用量: 同 id 多行只累加一次
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        input_tk += usage.get("input_tokens") or 0
                        output_tk += usage.get("output_tokens") or 0

                content = msg.get("content")
                if isinstance(content, list):
                    n_tu = 0
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            n_tu += 1
                            tool_calls += 1
                    if mid is not None:
                        round_tool[mid] = round_tool.get(mid, 0) + n_tu

        plain_rounds = sum(1 for n in round_tool.values() if n == 0)
        # message.id 缺失的轮次(无法归类)若 content 也无 tool_use, 计入 plain
        plain_rounds += sum(1 for mid in seen_ids if mid not in round_tool)

        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": 0,
            "total_tokens": input_tk + output_tk,
        }


def _read_dsh_events(zst_path: Path) -> List[Dict[str, Any]]:
    if zstandard is None or not os.path.isfile(zst_path):
        return []
    try:
        dctx = zstandard.ZstdDecompressor()
        with open(zst_path, "rb") as f:
            text = dctx.stream_reader(f).read().decode("utf-8", errors="replace")
    except (OSError, ValueError, zstandard.ZstdError):
        return []
    events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


class DshAdapter(HarnessAdapter):
    name = "dsh"

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位指定 agent 的 DSH 压缩轨迹。

        布局: ~/.dsh/sessions/<agent>/<session_name>/<encoded-cwd>/<agent>-<session_name>/
              session.jsonl.zstd
        多次运行时取 session_name 目录最新者 (按目录名字典序, 形如 *_YYYYMMDDTHHMMSS)。
        """
        agent_dir = Path(f"~/.dsh/sessions/{agent_name}").expanduser()
        if not agent_dir.is_dir():
            return None
        # 收集所有 session_name 目录下深度为 2 的 session.jsonl.zstd
        cands: List[Path] = []
        for session_dir in agent_dir.iterdir():
            if not session_dir.is_dir():
                continue
            for encoded_dir in session_dir.iterdir():
                if not encoded_dir.is_dir():
                    continue
                for leaf in encoded_dir.iterdir():
                    if not leaf.is_dir():
                        continue
                    p = leaf / "session.jsonl.zstd"
                    if p.is_file():
                        cands.append(p)
        if not cands:
            return None
        # session_name 形如 eval_test_20260902T141638, 字典序 = 时间序, 取最大
        return max(cands, key=lambda p: p.parent.parent.parent.name)

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 DSH 压缩事件流轨迹 (主 agent)。

        返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}, 口径同其他 harness:
          tool_calls       : assistant/message 的 content 中 type=="tool-call" 部件总数
          plain_rounds     : 不带 tool-call 的 assistant/message 轮数 (只有 text/reasoning)
          assistant_rounds : assistant/message 事件数 (组装好的整条消息, 非流式 chunk)
          *_tokens         : 每条 assistant/message 的 data.usage 累加
        """
        zero = {
            "tool_calls": 0, "plain_rounds": 0, "assistant_rounds": 0,
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        }
        events = _read_dsh_events(path)
        if not events:
            return dict(zero, total_tokens=0)

        tool_calls = plain_rounds = assistant_rounds = 0
        input_tk = output_tk = 0
        for obj in events:
            if obj.get("type") != "assistant/message":
                continue
            data = obj.get("data") or {}
            msg = data.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            assistant_rounds += 1
            # token 用量: data.usage (inputTokens/outputTokens/cacheWriteTokens)
            usage = data.get("usage")
            if isinstance(usage, dict):
                input_tk += usage.get("inputTokens") or 0
                output_tk += usage.get("outputTokens") or 0
            # 工具调用: content 部件 type=="tool-call"
            content = msg.get("content")
            n_tc = 0
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool-call":
                        n_tc += 1
            tool_calls += n_tc
            if n_tc == 0:
                plain_rounds += 1

        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": 0,
            "total_tokens": input_tk + output_tk,
        }

    def extract_eval_traj_verdict(self, path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """从 DSH evaluator 压缩轨迹兜底解析「首轮」裁决。

        DSH evaluator 轨迹同样是 zstd 事件流; 裁决 JSON 藏在 assistant/message 的
        text 部件里, 复用首轮 (rubric_checks+inclination) 判定。
        """
        events = _read_dsh_events(path)
        if not events:
            return False, None
        round_texts: List[str] = []
        for obj in events:
            if obj.get("type") != "assistant/message":
                continue
            data = obj.get("data") or {}
            msg = data.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                txt = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            elif isinstance(content, str):
                txt = content
            else:
                txt = ""
            round_texts.append(txt)
        return _first_verdict_round(round_texts)


def _extract_completion_from_eval_text(texts: List[str]) -> Tuple[bool, Optional[float]]:
    """从 evaluator 的若干文本部件里取「首轮裁决」的 completion。

    opencode 的 evaluator 会把「裁决指令模板」和「最终裁决 JSON」都留在 text 部件里:
        故代码块优先、且代码块需真正 parse 出该字段。
    """
    fence_re = re.compile(r"```(?:json)?\s*", re.DOTALL)

    def _json_blocks(t: str):
        """从一段文本里用括号计数逐个提取 ``` 围栏后的完整 JSON 对象。"""
        for fm in fence_re.finditer(t):
            i = fm.end()
            start = t.find("{", i)
            if start < 0:
                continue
            depth = 0
            end = -1
            in_str = False
            esc = False
            for k in range(start, len(t)):
                ch = t[k]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = k + 1
                            break
            if end < 0:
                continue
            try:
                obj = json.loads(t[start:end])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj

    # 优先: 任一 text 部件里的代码块, 第一个含 task_declared_complete 的即为裁决
    for txt in texts:
        for obj in _json_blocks(txt):
            if "task_declared_complete" in obj:
                comp = obj.get("completion")
                if isinstance(comp, (int, float)):
                    return True, float(comp)
                return True, None
    # 次选: 没有合规代码块, 但有裸的 task_declared_complete (无围栏) —— 取末个 completion
    for txt in texts:
        if "task_declared_complete" in txt:
            comp = _EVAL_COMP_RE.findall(txt)
            if comp:
                v = comp[-1]
                return True, (None if v == "null" else float(v))
            return True, None
    # 最后兜底: 通用首轮正则 (含 rubric_checks+inclination 的轮)
    return _first_verdict_round(texts)


class OpencodeAdapter(HarnessAdapter):
    """opencode 轨迹适配器。

    opencode.db库内三张表协作:
      session : 每个 agent 一行, 直接存该 session 的 token 汇总 (tokens_input/output/...)
      message : 每条消息一行 (role=user/assistant), time_created 决定轮序
      part    : 每条消息的内容部件一行 (type=text/tool/step-start/step-finish/reasoning)
    """
    name = "opencode"

    def _db_path(self) -> Path:
        return Path("~/.local/share/opencode/opencode.db").expanduser()

    def _find_session_id(self, agent_name: str) -> Optional[str]:
        """在库里找指定 agent 最新一次 session 的 id。库不可读/无匹配返回 None。"""
        db = self._db_path()
        if not os.path.isfile(db):
            return None
        try:
            con = sqlite3.connect(str(db))
        except sqlite3.Error:
            return None
        try:
            row = con.execute(
                "SELECT id FROM session WHERE agent = ? ORDER BY time_created DESC LIMIT 1",
                (agent_name,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        return row[0] if row else None

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位主 agent 轨迹。opencode 是单库多 session, 返回库路径(命中 session 才返回)。

        与其他 harness 不同: 这里返回的是共享 db 路径, 而非每 agent 独立文件; agent 区分
        交给 analyze/extract_eval_traj_verdict 内部按 agent 名查 session_id。
        """
        if self._find_session_id(agent_name) is None:
            return None
        return self._db_path()

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 opencode 主 agent session (SQLite 聚合)。

        返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}, 口径同其他 harness:
          tool_calls       : part 表中 type=='tool' 行数 (每次工具调用一行)
          plain_rounds     : 不含 tool 部件的 assistant 消息数 (纯文字轮)
          assistant_rounds : message 表中 role=='assistant' 行数
          *_tokens         : session 行的 tokens_input/output/reasoning 汇总 (库已聚合好)
        path 由 find_trajectory 传入 (= db_path); 主 agent session_id 用最近一次 main。
        """
        zero = {
            "tool_calls": 0, "plain_rounds": 0, "assistant_rounds": 0,
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        }
        if not path or not os.path.isfile(path):
            return dict(zero, total_tokens=0)
        sid = self._find_session_id(agent_name)
        if not sid:
            return dict(zero, total_tokens=0)
        try:
            con = sqlite3.connect(str(path))
        except sqlite3.Error:
            return dict(zero, total_tokens=0)
        try:
            # token: session 行已聚合
            srow = con.execute(
                "SELECT tokens_input, tokens_output, tokens_reasoning "
                "FROM session WHERE id = ?", (sid,),
            ).fetchone()
            input_tk = (srow[0] or 0) if srow else 0
            output_tk = (srow[1] or 0) if srow else 0
            reas_tk = (srow[2] or 0) if srow else 0

            # assistant 消息及其工具部件
            msgs = con.execute(
                "SELECT id FROM message WHERE session_id = ? AND data LIKE '%\"role\":\"assistant\"%'",
                (sid,),
            ).fetchall()
            assistant_ids = [r[0] for r in msgs]
            assistant_rounds = len(assistant_ids)

            # 每条 assistant 消息的工具调用数
            tool_calls = 0
            plain_rounds = 0
            if assistant_ids:
                # 用 IN 批量取 tool 部件, 按消息分组计数
                placeholders = ",".join("?" for _ in assistant_ids)
                tool_rows = con.execute(
                    f"SELECT message_id FROM part WHERE session_id = ? "
                    f"AND message_id IN ({placeholders}) AND data LIKE '%\"type\":\"tool\"%'",
                    (sid, *assistant_ids),
                ).fetchall()
                tool_by_msg = Counter(r[0] for r in tool_rows)
                tool_calls = sum(tool_by_msg.values())
                plain_rounds = sum(1 for mid in assistant_ids if tool_by_msg.get(mid, 0) == 0)
        except sqlite3.Error:
            return dict(zero, total_tokens=0)
        finally:
            con.close()

        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": reas_tk,
            "total_tokens": input_tk + output_tk + reas_tk,
        }

    def extract_eval_traj_verdict(self, path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """从 opencode evaluator session 兜底解析「首轮裁决」。

        evaluator 的 text 部件里既有指令模板(含 "completion" 字样说明)又有最终裁决 JSON
        代码块。用 _extract_completion_from_eval_text 找含 task_declared_complete 的
        真实裁决块, 避免误抓指令模板。
        """
        if not path or not os.path.isfile(path):
            return False, None
        sid = self._find_session_id(evaluator_agent)
        if not sid:
            return False, None
        try:
            con = sqlite3.connect(str(path))
        except sqlite3.Error:
            return False, None
        try:
            rows = con.execute(
                "SELECT data FROM part WHERE session_id = ? AND data LIKE '%\"type\":\"text\"%' "
                "ORDER BY time_created",
                (sid,),
            ).fetchall()
        except sqlite3.Error:
            con.close()
            return False, None
        con.close()
        texts: List[str] = []
        for r in rows:
            try:
                pd = json.loads(r[0]) if r[0] else {}
            except json.JSONDecodeError:
                continue
            if isinstance(pd, dict) and pd.get("type") == "text":
                texts.append(pd.get("text") or "")
        if not texts:
            return False, None
        return _extract_completion_from_eval_text(texts)


class PiAdapter(HarnessAdapter):
    """pi 轨迹适配器。

    pi 把所有 agent 的 session (main / evaluator / 其他) 都落到同一个目录
    ~/.pi/agent/sessions/*.jsonl, 区分靠每个session 首行 `type=="session"` 的 `cwd` 字段:
      - main       : cwd 末段 = "workspace"        (agent_name=="main")
      - <其他agent> : cwd 末段 = "workspace-<agent>" (如 main3 -> workspace-main3)
    eval直接复用基类默认实现
    """
    name = "pi"

    @staticmethod
    def _read_session_cwd(path: Path) -> Tuple[Optional[str], Optional[str]]:
        """读 session 首行的 (cwd, timestamp); 无 session 行返回 (None, None)。"""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "session":
                        return obj.get("cwd"), obj.get("timestamp")
        except OSError:
            pass
        return None, None

    @staticmethod
    def _cwd_matches(cwd: Optional[str], agent_name: str) -> bool:
        """pi session 的 cwd 末段是否对应指定 agent。"""
        if not cwd:
            return False
        leaf = Path(cwd).name
        if agent_name == "main":
            return leaf == "workspace"
        return leaf == f"workspace-{agent_name}"

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位指定 agent 的 pi session 文件"""
        d = Path("~/.pi/agent/sessions").expanduser()
        if not d.is_dir():
            return None
        cands: List[Tuple[str, Path]] = []
        for p in d.rglob("*.jsonl"):
            if not p.is_file():
                continue
            cwd, ts = self._read_session_cwd(p)
            if self._cwd_matches(cwd, agent_name):
                cands.append((ts or "", p))
        if not cands:
            return None
        cands.sort(key=lambda x: x[0])  # ISO 时间戳字典序 = 时间序
        return cands[-1][1]

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 pi 主 agent session (逐行 jsonl)。

        返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}, 口径同其他 harness:
          tool_calls       : assistant content 中 type=="toolCall" 部件总数
          plain_rounds     : 不带 toolCall 的 assistant 消息数 (只有 text/thinking)
          assistant_rounds : role=="assistant" 的 message 行数
          *_tokens         : 各 assistant 消息 usage.{input,output,reasoning} 累加
        """
        tool_calls = plain_rounds = assistant_rounds = 0
        input_tk = output_tk = reasoning_tk = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                assistant_rounds += 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    input_tk += usage.get("input") or 0
                    output_tk += usage.get("output") or 0
                    reasoning_tk += usage.get("reasoning") or 0
                content = msg.get("content")
                if isinstance(content, list):
                    n_tc = sum(
                        1 for p in content
                        if isinstance(p, dict) and p.get("type") == "toolCall"
                    )
                else:
                    n_tc = 0
                tool_calls += n_tc
                if n_tc == 0:
                    plain_rounds += 1
        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": reasoning_tk,
            "total_tokens": input_tk + output_tk + reasoning_tk,
        }


class GrokAdapter(HarnessAdapter):
    """grok 轨迹适配器。

    grok 把每个 (agent, session) 的轨迹落到
      ~/.grok/sessions/<url-encoded-cwd>/<session-uuid>/{chat_history.jsonl, updates.jsonl}
    encoded-cwd 解码后 = /home/ma-user/.grok/workspace[-<agent>]/.sessions/<session_name>,
    agent 区分靠路径里 "workspace" vs "workspace-<agent>" 段

    chat_history.jsonl 是扁平逐行 jsonl: type ∈ {system,user,assistant,tool_result,reasoning},
    token 用量在同级 updates.jsonl 的 turn_completed 事件 params.update.usage 里。
    """
    name = "grok"

    @staticmethod
    def _agent_from_encoded(encoded_dir_name: str) -> Optional[str]:
        """把 encoded-cwd 目录名解出 agent_name。
        取 ".sessions" 前一段: "workspace" -> "main", "workspace-<agent>" -> <agent>。
        """
        decoded = unquote(encoded_dir_name)
        parts = decoded.split("/")
        try:
            i = parts.index(".sessions")
        except ValueError:
            return None
        leaf = parts[i - 1] if i > 0 else ""
        if leaf == "workspace":
            return "main"
        if leaf.startswith("workspace-"):
            return leaf[len("workspace-"):]
        return None

    @staticmethod
    def _session_ts(path: Path) -> str:
        """取 session 目录 (encoded-cwd 名) 的时间排序键。

        session_name 形如 main_20260902T103211, 字典序 = 时间序; 取 encoded 目录名末段。
        """
        return path.parent.parent.name  # encoded-cwd 段 (含 session_name)

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位指定 agent 的 grok chat_history.jsonl。

        遍历 ~/.grok/sessions/*/<uuid>/chat_history.jsonl, 按 encoded-cwd 解出的 agent 名
        筛选; 多条候选取 session_name 字典序最大者 (= 时间最晚的一次运行)。
        """
        d = Path("~/.grok/sessions").expanduser()
        if not d.is_dir():
            return None
        cands: List[Tuple[str, Path]] = []
        for p in d.rglob("chat_history.jsonl"):
            if not p.is_file():
                continue
            # p = <sessions>/<encoded-cwd>/<uuid>/chat_history.jsonl
            if self._agent_from_encoded(p.parent.parent.name) != agent_name:
                continue
            cands.append((self._session_ts(p), p))
        if not cands:
            return None
        cands.sort(key=lambda x: x[0])
        return cands[-1][1]

    @staticmethod
    def _usage_from_updates(chat_history_path: Path) -> Dict[str, int]:
        """从同级 updates.jsonl 累加各 turn_completed 事件的 usage (input/output/reasoning)。

        chat_history.jsonl 自身无 usage; token 在同级 updates.jsonl 里, 每个 turn_completed
        的 usage 是该轮用量 (非累积), 故逐轮相加。文件缺失/解析失败返回全 0。
        """
        zero = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        up = chat_history_path.parent / "updates.jsonl"
        if not up.is_file():
            return zero
        inp = out = rea = 0
        try:
            with open(up, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    upd = (obj.get("params") or {}).get("update") or {}
                    if upd.get("sessionUpdate") != "turn_completed":
                        continue
                    usage = upd.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    inp += usage.get("inputTokens") or 0
                    out += usage.get("outputTokens") or 0
                    rea += usage.get("reasoningTokens") or 0
        except OSError:
            return zero
        return {"input_tokens": inp, "output_tokens": out, "reasoning_tokens": rea}

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 grok 主 agent chat_history.jsonl (扁平逐行 jsonl)。

        返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}, 口径同其他 harness:
          tool_calls       : assistant 行顶层 tool_calls list 长度累加
          plain_rounds     : 不带 tool_calls 的 assistant 行数 (只有 content 文字)
          assistant_rounds : type=="assistant" 的行数
          *_tokens         : 同级 updates.jsonl 各 turn_completed.usage 累加
        """
        tool_calls = plain_rounds = assistant_rounds = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                assistant_rounds += 1
                tcs = obj.get("tool_calls")
                n_tc = len(tcs) if isinstance(tcs, list) else 0
                tool_calls += n_tc
                if n_tc == 0:
                    plain_rounds += 1
        tokens = self._usage_from_updates(path)
        input_tk, output_tk, reasoning_tk = (
            tokens["input_tokens"], tokens["output_tokens"], tokens["reasoning_tokens"]
        )
        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": reasoning_tk,
            "total_tokens": input_tk + output_tk + reasoning_tk,
        }

    def extract_eval_traj_verdict(self, path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """从 grok evaluator chat_history.jsonl 兜底解析「首轮裁决」。

        grok 扁平格式 (无 message/role): assistant 行 content 是字符串, 裁决 JSON 以 ```json
        围栏代码块给出, 含 task_declared_complete + completion。
        """
        if not path or not os.path.isfile(path):
            return False, None
        texts: List[str] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    content = obj.get("content")
                    if isinstance(content, str):
                        texts.append(content)
        except OSError:
            return False, None
        if not texts:
            return False, None
        return _extract_completion_from_eval_text(texts)


class OpenjiuwenAdapter(HarnessAdapter):
    """openjiuwen 轨迹适配器。

      session  : ~/.openjiuwen/sessions/<session_name>_<run_id>.trajectory.jsonl
                 —— 逐行 JSONL, 每行是一条 OpenAI 风格消息 ({role, content,
                 tool_calls, usage_metadata}), 工具调用详情、token 用量都在此。
                 (openjiuwen_client._append_tool_call_details 落盘)
                 文件名 = session_name + run_id (两者都来自 task_config, 见 _session_bases):
                   主 agent   : {query.session_name|main}_{run_id}        (executor.py)
                   evaluator  : eval_{eval.session_name|query.session_name|main}_{run_id}
                                                                (evaluator.py, 多一层 eval_ 前缀)
                 故定位必须按 task_config 的 queries[].session_name 匹配。
    """

    name = "openjiuwen"

    def _sessions_dir(self) -> Path:
        return Path("~/.openjiuwen/sessions").expanduser()

    def _session_bases(self, agent_name: str) -> List[str]:
        """按 task_config 算出该 agent 的 session 名 base 候选 (不含 _run_id 后缀)。

        落盘约定:
          主 agent   : {query.session_name|main}_{run_id}
          evaluator  : eval_{eval.session_name|query.session_name|main}_{run_id}
        """
        queries = self.task_config.get("queries") or []
        if not isinstance(queries, list):
            return []
        bases: List[str] = []
        for query in queries:
            if not isinstance(query, dict):
                continue
            base = query.get("session_name") or "main"
            if agent_name == evaluator_agent:
                evaluate = query.get("evaluate")
                if isinstance(evaluate, dict) and evaluate.get("session_name"):
                    base = evaluate["session_name"]
                base = f"eval_{base}"
            if base not in bases:
                bases.append(base)
        return bases

    def find_trajectory(self, agent_name: str) -> Optional[Path]:
        """定位指定 agent 的轨迹文件 (~/.openjiuwen/sessions/*.trajectory.jsonl)。

        有 task_config: 按落盘文件名约定 {base}_{run_id}.trajectory.jsonl 精确匹配
        —— base 取自 queries[].session_name (evaluator 多一层 eval_ 前缀, 见
        _session_bases), run_id 形如 YYYYMMDDTHHMMSS; 多个 base 命中时取文件名
        字典序最大者 (= 时间最晚)。
        无 task_config: 退回旧前缀启发式 (eval_ 前缀 = evaluator)。
        """
        sessions_dir = self._sessions_dir()
        if not sessions_dir.is_dir():
            return None
        cands: List[Path] = []
        bases = self._session_bases(agent_name)
        if bases:
            run_id_re = re.compile(r"^\d{8}T\d{6}$")
            for p in sessions_dir.iterdir():
                if not p.name.endswith(".trajectory.jsonl"):
                    continue
                stem = p.name[: -len(".trajectory.jsonl")]  # {base}_{run_id}
                # 文件名 = {base}_{run_id}; base 本身可含 "_", 故按最长后缀 _run_id 切
                i = stem.rfind("_")
                if i < 0 or not run_id_re.fullmatch(stem[i + 1:]):
                    continue
                if stem[:i] in bases:
                    cands.append(p)
        else:
            want_eval = (agent_name == evaluator_agent)
            cands = [
                p for p in sessions_dir.iterdir()
                if p.name.endswith(".trajectory.jsonl")
                and p.name.startswith("eval_") == want_eval
            ]
        if not cands:
            return None
        return max(cands, key=lambda p: p.name)

    def analyze(self, path: Path, agent_name: str) -> Dict[str, int]:
        """分析 openjiuwen 轨迹 (.trajectory.jsonl, OpenAI 风格逐行 JSONL)。

        返回口径同其他 harness:
          tool_calls       : assistant 消息 tool_calls 列表里工具调用总数
          plain_rounds     : 不带 tool_calls 的 assistant 消息数 (纯文本轮)
          assistant_rounds : role=="assistant" 的消息数
          *_tokens         : 各 assistant 消息 usage_metadata 的 token 字段累加
                             (usage_metadata: {input_tokens, output_tokens,
                             reasoning_tokens, ...}, 逐轮非累积, 累加即总量)
        """
        tool_calls = plain_rounds = assistant_rounds = 0
        input_tk = output_tk = reasoning_tk = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or obj.get("role") != "assistant":
                        continue
                    assistant_rounds += 1
                    usage = obj.get("usage_metadata")
                    if isinstance(usage, dict):
                        input_tk += int(usage.get("input_tokens") or 0)
                        output_tk += int(usage.get("output_tokens") or 0)
                        reasoning_tk += int(usage.get("reasoning_tokens") or 0)
                    tcs = obj.get("tool_calls")
                    n_tc = len(tcs) if isinstance(tcs, list) else 0
                    tool_calls += n_tc
                    if n_tc == 0:
                        plain_rounds += 1
        except OSError:
            pass
        return {
            "tool_calls": tool_calls,
            "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds,
            "input_tokens": input_tk,
            "output_tokens": output_tk,
            "reasoning_tokens": reasoning_tk,
            "total_tokens": input_tk + output_tk + reasoning_tk,
        }

    def extract_eval_traj_verdict(self, path: Optional[Path]) -> Tuple[bool, Optional[float]]:
        """从 openjiuwen evaluator 轨迹 (.trajectory.jsonl) 兜底解析「首轮裁决」。

        逐行 JSONL、OpenAI 风格: assistant 行的 content 是纯文本, 用统一的
        _extract_completion_from_eval_text 解析。
        """
        if not path or not os.path.isfile(path):
            return False, None
        texts: List[str] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or obj.get("role") != "assistant":
                        continue
                    content = obj.get("content")
                    if isinstance(content, str):
                        texts.append(content)
        except OSError:
            return False, None
        if not texts:
            return False, None
        return _extract_completion_from_eval_text(texts)

HARNESS_ADAPTER_CLASSES: Dict[str, type] = {
    "openclaw": OpenclawAdapter,
    "hermes": HermesAdapter,
    "claude-code": ClaudeCodeAdapter,
    "dsh": DshAdapter,
    "opencode": OpencodeAdapter,
    "pi": PiAdapter,
    "grok": GrokAdapter,
    "openjiuwen": OpenjiuwenAdapter,
}


def get_adapter(harness_type: str, task_config: Optional[Dict[str, Any]] = None) -> HarnessAdapter:
    """按 harness_type 构造适配器, 并把 task_config 原始 dict 注入。

    task_config 从哪来: task_status.py 的 compute_task_status 在执行前已经
    json.load 过同一文件(确认存在且合法), 此处传引用即可, 不在 adapter 侧
    重新读文件 —— 避免双读路径与文件不存在的报错分叉。
    task_config 为 None 时 OpenjiuwenAdapter._session_bases 退回旧前缀启发式
    (eval_ 前缀 = evaluator), 其余 adapter 不受影响。
    """
    cls = HARNESS_ADAPTER_CLASSES.get(harness_type, HarnessAdapter)
    return cls(task_config=task_config)
