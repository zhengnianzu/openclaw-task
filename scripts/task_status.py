# -*- coding: utf-8 -*-
"""
轨迹统计脚本 —— 单任务版。

新增一个 harness: 继承 HarnessAdapter, 实现 find_trajectory / analyze / extract_eval_traj_verdict 三个方法

固定路径约定 (沙箱路径):
  - task_config.json : <project_root>/configs/<task_config>.json
    <agent-name>     : task_config.json 的 主 agent
  - 任务记录 log    : /home/ma-user/workspace/workdir/run.log (所有 harness 共用)
  - evaluator 裁决  : <project_root>/logs/evaluator_use.log (所有 harness 共用,
                       缺失时回退 run.log 的 [Evaluator] 正则解析,
                       再缺失回退 evaluator 轨迹 —— 落盘布局按 harness 不同,
                       由各 adapter 子类的 find_trajectory/extract_eval_traj_verdict 适配)
  - 统计结果输出   : <project_root>/logs/traj_stats_result.json

harness_type 来源优先级: 显式传参 > config 的 harness_type 字段 > openclaw。

task_level 分级 (逐层递进, 直接统计主 agent 的最好档):
  L0  : 存在轨迹
  L1  : assistant 轨迹的工具调用次数>=3, 出现过一次纯文字回复就算
  L1.5: 在 L1 基础上, 有 evaluator 裁决(evaluator_use.log 里有记录)
  L2  : 在 L1.5 基础上, 首轮 completion >= 0.5
  L3  : 在 L1.5 基础上, 首轮 completion == 1

对外入口:
  run_stats(config_file, traj_stats_result, harness_type) -> Path  # 被 harness_automation.py 直调
命令行:
  python scripts/task_status.py -c configs/xxx.json [--harness openclaw|...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import zstandard

from scripts.harness_adapter import get_adapter, HarnessAdapter


# 项目根 = scripts/../ (沙箱内 = /home/ma-user/workspace/openclaw-task)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
RUN_LOG_PATH = "/home/ma-user/workspace/workdir/run.log"
EVALUATOR_USE_LOG = LOGS_DIR / "evaluator_use.log"

# ============================================================================
# 通用工具
# ============================================================================

def _char_len(path) -> int:
    """返回轨迹文件的原始字符数(读取为文本, 非法字节替换)。文件不存在/读取失败返回 0。

    DSH 轨迹是 zstd 压缩的 (.zstd), 先解压再计字符
    """
    if not path:
        return 0
    try:
        if str(path).endswith(".zstd") or str(path).endswith(".zst"):
            if zstandard is None:
                return 0
            dctx = zstandard.ZstdDecompressor()
            with open(path, "rb") as f:
                return len(dctx.stream_reader(f).read().decode("utf-8", errors="replace"))
        with open(path, encoding="utf-8", errors="replace") as f:
            return len(f.read())
    except (OSError, ValueError):
        return 0
    except Exception:
        return 0


def has_task_done_marker(log_path) -> bool:
    """检查任务记录 log 是否含「【Task_Done】」标记(assistant 回答末尾的任务完成信号)。"""
    if not log_path or not os.path.isfile(log_path):
        return False
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return "【Task_Done】" in f.read()
    except OSError:
        return False


# ============================================================================
# 通用裁决解析 (各 harness 共享, 与轨迹落盘格式无关)
#   主源: logs/evaluator_use.log
#   兜底1: run.log 的 [Evaluator] turn=N 正则解析
#   兜底2: evaluator 轨迹 (格式按 harness 不同, 由各 adapter 子类提供)
# ============================================================================

def extract_evaluator_use_verdict(log_path: Path) -> Tuple[bool, Optional[float], Optional[str]]:
    """从 logs/evaluator_use.log 取「首轮」裁决

    首轮 = turn 号最小的行; has_eval = 文件有记录; completion = 该行 evaluation.completion
    返回 (has_eval, completion, source="evaluator_use")。
    文件不存在/读失败 → (False, None, None), 由上层回退 run.log。
    """
    if not log_path or not os.path.isfile(log_path):
        return False, None, None
    rows: List[Tuple[int, Any]] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                turn = obj.get("turn")
                turn_n = turn if isinstance(turn, (int, float)) else float("inf")
                rows.append((turn_n, obj))
    except OSError:
        return False, None, None
    if not rows:
        return False, None, None
    rows.sort(key=lambda x: x[0])
    _, first = rows[0]
    ev = first.get("evaluation") if isinstance(first.get("evaluation"), dict) else {}
    comp = ev.get("completion")
    if isinstance(comp, (int, float)):
        return True, float(comp), "evaluator_use"
    return True, None, "evaluator_use"


# ---- run.log 正则解析兜底 (evaluator_use.log 缺失时用) ----
# evaluator 输出标记, 捕获 turn 编号。注意: 有些任务的评测并非从 turn=1 开始
_EVAL_MARKER = re.compile(r"\[Evaluator\]\s+turn=(\d+)\s+agent=\S+.*输出")
_EVAL_COMP_RE = re.compile(r'"completion"\s*:\s*(null|-?[0-9.]+)')


def _parse_json_block_after(lines: List[str], idx: int):
    """从 lines[idx] 之后第一个以 '{' 开头的行起, 用大括号计数取出完整 JSON 块并解析。

    返回 dict; 找不到块返回 None; 块内 JSON 非法返回字符串标记 '__BADJSON__'。
    """
    j = idx + 1
    while j < len(lines) and lines[j].strip() != "{":
        j += 1
    if j >= len(lines):
        return None
    depth = 0
    buf: List[str] = []
    for k in range(j, len(lines)):
        buf.append(lines[k])
        depth += lines[k].count("{") - lines[k].count("}")
        if depth <= 0:
            break
    try:
        return json.loads("".join(buf))
    except json.JSONDecodeError:
        return "__BADJSON__"


def extract_first_evaluator_obj(log_path):
    """run.log 中提取「首轮」evaluator 裁决的完整 dict。

    首轮 = 编号最小的 [Evaluator] turn=N 块(而非死守 turn=1)。找不到任何裁决块返回
    None; 块存在但 JSON 非法返回字符串 '__BADJSON__'。
    """
    if not os.path.isfile(log_path):
        return None
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    marks: List[Tuple[int, int]] = []
    for i, l in enumerate(lines):
        m = _EVAL_MARKER.search(l)
        if m:
            marks.append((int(m.group(1)), i))
    if not marks:
        return None
    marks.sort(key=lambda x: (x[0], x[1]))
    _, idx = marks[0]
    return _parse_json_block_after(lines, idx)


def extract_run_log_verdict(log_path) -> Tuple[bool, Optional[float]]:
    """返回 (has_verdict, completion)."""
    obj = extract_first_evaluator_obj(log_path)
    if obj is None:
        return False, None
    if obj == "__BADJSON__":
        return True, None
    comp = obj.get("completion") if isinstance(obj, dict) else None
    if isinstance(comp, (int, float)):
        return True, float(comp)
    return True, None


def _verdict_from_text(txt: str) -> Tuple[bool, Optional[float]]:
    """从裁决原文里正则抓 completion。返回 (has_verdict, completion)。

    无 completion 匹配 → (True, None); 匹配到 null → (True, None)。
    """
    ms = _EVAL_COMP_RE.findall(txt)
    if not ms:
        return True, None
    v = ms[-1]
    return True, (None if v == "null" else float(v))


def _round_key(mid: Any, line_idx: int) -> Any:
    """把一条 assistant 行归到「轮」的分组键。

    claude-code 一轮 assistant 常被拆成多行(thinking/text/tool_use 分段), 共享同一
    message.id → 用 id 归并。openclaw 行无 message.id (id=None) → 每行即一轮, 用行号
    归并(行号各自不同, 互不合并)。hermes 是 {messages:[...]}, 每条 assistant 消息即
    一轮, 同样无 id → 调用方用消息序号当 line_idx。
    """
    return mid if mid is not None else f"__line{line_idx}"


def _assistant_text(content: Any) -> str:
    """从 assistant 消息的 content 里取出 text 文本(裁决原文藏在 text 部件里)。

    content 为 str → 原样返回; 为部件 list → 拼接所有 type=="text" 部件的 text;
    其他 → 空串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _eval_traj_rounds_from_jsonl(path: Path) -> List[str]:
    """读 transcript jsonl, 把所有 assistant 行按「轮」聚合成裁决文本列表(按出现顺序)。
    """
    rounds: Dict[Any, str] = {}
    order: List[Any] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # openclaw 行 type 为 "message"; claude-code 为 "assistant" (都带 message.role)
            if obj.get("type") not in ("assistant", "message"):
                continue
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            key = _round_key(msg.get("id"), i)
            txt = _assistant_text(msg.get("content"))
            if key not in rounds:
                rounds[key] = txt
                order.append(key)
            else:
                rounds[key] += "\n" + txt
    return [rounds[k] for k in order]


# ============================================================================
# 统一裁决取回 (3 级回退): evaluator_use.log -> run.log 正则 -> evaluator 轨迹
# ============================================================================

def resolve_first_verdict(
    evaluator_use_log: Path,
    run_log,
    adapter: Optional[HarnessAdapter],
) -> Tuple[bool, Optional[float], Optional[str]]:
    """统一取「首轮裁决」: evaluator_use.log 为主, 缺失时回退 run.log 正则解析,
    再缺失回退 evaluator 轨迹(由 adapter 提供 find_eval_traj/extract_eval_traj_verdict,
    适配各 harness 落盘布局)。

    返回 (has_eval, completion, source), source ∈ {"evaluator_use", "run.log", "eval_traj", None}
    """
    # 主: evaluator_use.log
    has_eval, score, source = extract_evaluator_use_verdict(evaluator_use_log)
    if has_eval:
        return has_eval, score, source

    # 兜底1: run.log 正则解析 [Evaluator] turn=N 块
    has_eval, score = extract_run_log_verdict(run_log)
    source = "run.log" if has_eval else None
    if score is None and adapter is not None:
        # 兜底2: evaluator 轨迹 (落盘布局/格式按 harness 不同, 由 adapter 子类适配)
        ev = adapter.find_eval_traj()
        if ev:
            ev_has, ev_score = adapter.extract_eval_traj_verdict(ev)
            if ev_score is not None:
                return True, ev_score, "eval_traj"
            if ev_has and not has_eval:
                return True, None, "eval_traj"
    return has_eval, score, source


# ============================================================================
# 单任务统计主流程
# ============================================================================

def _load_task_config(config_file: Optional[Path]) -> Dict[str, Any]:
    if not config_file:
        return {}
    with open(config_file, encoding="utf-8") as f:
        return json.load(f)


def _primary_agent_name(task_config: Dict[str, Any]) -> str:
    """主 agent: 遍历 queries, 取第一个 agent_name != "evaluator" 的, 无则 "main"。
    (与 harness_automation._primary_agent_name 完全一致, 只统计这一个 agent。)"""
    for query in task_config.get("queries") or []:
        if not isinstance(query, dict):
            continue
        name = query.get("agent_name")
        if name and name != "evaluator":
            return name
    return "main"


def _task_level(
    has_traj: bool,
    ge3: bool,
    plain: bool,
    has_eval: bool,
    score: Optional[float],
    ignore_ge3: bool = False,
) -> str:
    """L0/L1/L1.5/L2/L3 逐层递进.

    L0:有轨迹; L1:ge3&plain; L1.5:再加 has_eval; L2:score>=0.5; L3:score==1.

    ignore_ge3=True (兜底适配器用): 不把 ge3&plain 当 L1 门槛, 直接 has_eval+score 决定
    L1.5/L2/L3, has_traj 但无裁决给 L0 —— 给「轨迹布局未知、读不到工具调用」的 harness
    一个大致档, 不至于一律 none。
    """
    if ignore_ge3:
        if has_eval and score is not None:
            if score >= 1.0:
                return "L3"
            if score >= 0.5:
                return "L2"
            return "L1.5"
        if has_traj:
            return "L0"
        return "none"
    if ge3 and plain and has_eval and score is not None:
        if score >= 1.0:
            return "L3"
        if score >= 0.5:
            return "L2"
        return "L1.5"
    if ge3 and plain:
        return "L1"
    if has_traj:
        return "L0"
    return "none"


def _summarize_per_agent(
    agent_name: str,
    traj_path: Optional[Path],
    metrics: Dict[str, int],
    has_eval: bool,
    score: Optional[float],
    verdict_source: Optional[str],
    task_done: bool,
    ignore_ge3: bool = False,
) -> Dict[str, Any]:
    """对「主 agent」出一份指标 (与 traj_stats 口径对齐).

    输出含: 轨迹指标 + token 用量 + char_len + task_done + passed_gate + 分级。
    L1 门槛对所有 harness 一致: tool_calls>=3 且有 plain_round (与 traj_stats 对齐)。
    ignore_ge3=True (兜底适配器): 分级忽略 ge3 门槛 (见 _task_level), passed_gate 标
    False 且加 "fallback": True 标记, 提示此结果是轨迹布局未知时的大致档。
    """
    has_traj = traj_path is not None
    ge3 = metrics["tool_calls"] >= 3
    plain = metrics["plain_rounds"] > 0
    passed_gate = ge3 and plain          # L1 门槛

    entry: Dict[str, Any] = {
        "agent": agent_name,
        "trajectory": str(traj_path) if traj_path else None,
        "tool_calls": metrics["tool_calls"],
        "assistant_rounds": metrics["assistant_rounds"],
        "plain_rounds": metrics["plain_rounds"],
        "has_trajectory": has_traj,
        "has_ge3_toolcalls": ge3,
        "has_plain_round": plain,
        "passed_gate": passed_gate,       # L1 门槛(ge3 & plain)
        "has_eval": has_eval,
        "evaluator_completion": score,
        "verdict_source": verdict_source,
        "char_len": _char_len(traj_path),
        "task_done": task_done,
        "level": _task_level(has_traj, ge3, plain, has_eval, score, ignore_ge3=ignore_ge3),
    }
    if ignore_ge3:
        # 兜底结果标记: 轨迹布局未知, 分级忽略 ge3, 仅为大致档
        entry["fallback"] = True
    # 透传 token 用量 (total_tokens>0 才加, 与 traj_stats 一致)
    if metrics.get("total_tokens"):
        entry["input_tokens"] = metrics["input_tokens"]
        entry["output_tokens"] = metrics["output_tokens"]
        entry["reasoning_tokens"] = metrics["reasoning_tokens"]
        entry["total_tokens"] = metrics["total_tokens"]
    return entry


def compute_task_status(
    config_file: Optional[str] = None,
    harness_type: Optional[str] = None,
) -> Dict[str, Any]:
    """核心函数: 对单个任务算出完整统计结果 (dict)。

    参数:
      config_file  : task_config.json 路径
      harness_type : 未识别 harness 用基类 HarnessAdapter 兜底
                     (不解析轨迹, 只靠 evaluator_use.log + run.log 给大致结果, 分级忽略
                     ge3 门槛, 结果带 "fallback": True 标记)。

    路径全部内联 (沙箱路径):
      run.log         = /home/ma-user/workspace/workdir/run.log (所有 harness 共用)
      evaluator_use   = <project_root>/logs/evaluator_use.log (裁决主源)
    """
    config_path = Path(config_file) if config_file else None
    task_config = _load_task_config(config_path)
    task_name = config_path.stem if config_path else "unknown"

    # harness 选路: 显式传参 > config 字段 > openclaw
    if harness_type is None:
        harness_type = task_config.get("harness_type") or "openclaw"
    # task_config 已 json.load 落内存, 直接注入 adapter(不二次读文件)
    adapter = get_adapter(harness_type, task_config=task_config)

    run_log = RUN_LOG_PATH
    task_done = has_task_done_marker(run_log)
    # evaluator 裁决: evaluator_use.log 为主, run.log 兜底, 再兜底 evaluator 轨迹
    has_eval, score, verdict_source = resolve_first_verdict(
        EVALUATOR_USE_LOG, run_log, adapter,
    )

    # 主 agent: 只统计 _primary_agent_name
    agent_name = _primary_agent_name(task_config)
    traj_path = adapter.find_trajectory(agent_name)
    metrics = adapter.analyze(traj_path, agent_name) if traj_path else {
        "tool_calls": 0, "plain_rounds": 0, "assistant_rounds": 0,
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0,
    }
    per_agent: List[Dict[str, Any]] = [
        _summarize_per_agent(
            agent_name, traj_path, metrics, has_eval, score, verdict_source,
            task_done=task_done,
            # 分级忽略 ge3 门槛, 只靠裁决给大致档
            ignore_ge3=(type(adapter) is HarnessAdapter),
        )
    ]

    # 任务级汇总: 取所有主 agent 里最好的一档
    def _rank(entry: Dict[str, Any]) -> int:
        return {"none": 0, "L0": 1, "L1": 2, "L1.5": 3, "L2": 4, "L3": 5}.get(entry["level"], 0)

    best = max(per_agent, key=_rank) if per_agent else None

    return {
        "task": task_name,
        "config_file": str(config_path) if config_path else None,
        "log_file": str(run_log),
        "harness": harness_type,
        "agents": per_agent,
        "task_level": best["level"] if best else "none",
        "best_completion": best["evaluator_completion"] if best else None,
    }


# ============================================================================
# harness_automation.py 集成入口
# ============================================================================

def run_stats(config_file: Optional[str] = None, traj_stats_result: Optional[str] = None, harness_type: Optional[str] = None) -> Path:
    """在 harness 跑完后写一份任务统计 json。

    harness_type 透传给 compute_task_status; 为 None 时由后者按
    config 的 harness_type 字段决定, 再缺省 openclaw。
    """
    config_path = Path(config_file) if config_file else None
    result_path = Path(traj_stats_result) if traj_stats_result else None
    result = compute_task_status(config_path, harness_type=harness_type)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result_path


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="轨迹统计 (单任务)")
    parser.add_argument("-c", "--config", required=True,
                        help="task_config.json 路径")
    parser.add_argument("-o", "--traj_stats_result",
                        default="logs/traj_stats_result.json",
                        help="轨迹质量统计路径")
    parser.add_argument(
            "--harness",
            default=None,
            help="harness类型(openclaw|hermes|claude-code|dsh|opencode|pi|grok; 不指定则使用配置文件中的 harness_type,缺省为 openclaw)"
    )
    args = parser.parse_args()

    config_file = Path(args.config).expanduser().resolve()

    run_stats(config_file, args.traj_stats_result, args.harness)
    print(f"任务统计已写入: {args.traj_stats_result}")


if __name__ == "__main__":
    main()
