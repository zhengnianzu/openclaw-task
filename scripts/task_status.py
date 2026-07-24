# -*- coding: utf-8 -*-
"""
轨迹统计脚本 —— 单任务版。

固定路径约定 (脚本内联, 无需外部传参):
  - task_config.json : <project_root>/configs/<task_config>.json
                       (等价 /home/ma-user/workspace/openclaw-task/configs/...)
    <agent-name>     : task_config.json 的 agents[].name, 去掉 evaluator
  - assistant 轨迹  : ~/.openclaw/agents/<agent-name>/sessions/
                       下文件名不含 "trajectory" 的 .jsonl 文件
  - 任务记录 log    : <project_root>/logs/<task_config>.log
                       从中提取「首轮」evaluator 的 completion 分数
  - 统计结果输出   : <project_root>/logs/traj_stats_result.json

<project_root> 通过脚本自身位置向上定位 (scripts/../), 天然同时兼容
  /home/ma-user/openclaw-task/           (开发)
  /home/ma-user/workspace/openclaw-task/ (部署)

统计指标（逐层递进）:
  2. assistant_trajectories  找到的 主agent 轨迹数
  3. ge3_toolcalls           至少有三次工具调用的 主agent 轨迹数
  4. ge3_and_plain_round     在满足(3)的前提下, 输出过不带工具调用的
                             assistant 轮 (只有 reasoning / content) 的轨迹数
  5. with_evaluator_score    满足(4) 且有 evaluator 首轮打分的轨迹数
  6. score_ge_0_5            满足(5) 且打分 >= 0.5
  7. score_eq_1              满足(5) 且打分 == 1
  8. task_level              L0:存在轨迹; L1:满足(4); L1.5:满足(5);
                             L2:满足(6); L3:满足(7)

对外入口:
  run_stats(config_file) -> Path      # 被 harness_automation.py 直调
命令行:
  python scripts/task_status.py --config configs/xxx.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# 主 agent 轨迹落盘位置 (openclaw harness 硬编码)
HARNESS_HOME = Path("~/.openclaw").expanduser()


# ============================================================================
# assistant 轨迹分析
# ============================================================================

def find_assistant_trajectory(agent_name: str) -> Optional[Path]:
    """定位指定 agent 的 assistant 轨迹 (.jsonl, 文件名不含 trajectory)。

    多条候选时取「文件最大者」(通常也是最完整的一次运行)。
    """
    sessions_dir = HARNESS_HOME / "agents" / agent_name / "sessions"
    if not sessions_dir.is_dir():
        return None
    cands = [
        p for p in sorted(sessions_dir.iterdir())
        if p.suffix == ".jsonl" and "trajectory" not in p.name
    ]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_size)


def analyze_trajectory(path: Path) -> Dict[str, int]:
    """分析一条 assistant 轨迹。

    返回:
      tool_calls       : 全轨迹中 toolCall 的总次数
      plain_rounds     : 不带工具调用的 assistant 轮数(只有 thinking / text)
      assistant_rounds : assistant 消息轮数
    """
    tool_calls = 0
    plain_rounds = 0
    assistant_rounds = 0

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
                plain_rounds += 1

    return {
        "tool_calls": tool_calls,
        "plain_rounds": plain_rounds,
        "assistant_rounds": assistant_rounds,
    }


# ============================================================================
# evaluator 打分 (log 优先, evaluator 轨迹兜底)
# ============================================================================

# evaluator 输出标记, 捕获 turn 编号。注意: 有些任务的评测并非从 turn=1 开始
# (前面的 turn 可能未触发 evaluator 或被 reset), 首轮可能是 turn=2/3。
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


def extract_first_evaluator_obj(log_path: Path):
    """从任务 log 中提取「首轮」evaluator 裁决的完整 dict。

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


def extract_first_evaluator_verdict(log_path: Path) -> Tuple[bool, Optional[float]]:
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


def find_evaluator_trajectory() -> Optional[Path]:
    """evaluator 侧的非 trajectory session jsonl。

    有些任务的裁决没有回写进主 log(log 里 evals=0), 只落在 evaluator 会话轨迹里,
    此时从这里兜底解析裁决。
    """
    sessions = HARNESS_HOME / "agents" / "evaluator" / "sessions"
    if not sessions.is_dir():
        return None
    cands = [
        p for p in sorted(sessions.iterdir())
        if p.suffix == ".jsonl" and "trajectory" not in p.name
    ]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_size)


def extract_eval_traj_verdict(jsonl_path: Optional[Path]) -> Tuple[bool, Optional[float]]:
    """从 evaluator 轨迹 jsonl 兜底解析首轮裁决(log 无裁决时用)。

    取最后一条同时含 rubric_checks 与 inclination 的 assistant 消息(即裁决原文),
    用正则抓其中的 completion。返回 (has_verdict, completion)。
    """
    if not jsonl_path or not jsonl_path.is_file():
        return False, None
    last = None
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
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
            c = msg.get("content")
            if isinstance(c, str):
                txt = c
            elif isinstance(c, list):
                txt = "\n".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            else:
                txt = ""
            if "rubric_checks" in txt and "inclination" in txt:
                last = txt
    if last is None:
        return False, None
    ms = _EVAL_COMP_RE.findall(last)
    if not ms:
        return True, None
    v = ms[-1]
    return True, (None if v == "null" else float(v))


def resolve_first_verdict(log_path: Path) -> Tuple[bool, Optional[float], Optional[str]]:
    """统一取「首轮裁决」: 先读 log, log 无数值分时回退 evaluator 轨迹。

    返回 (has_eval, completion, source), source ∈ {"log", "eval_traj", None}
    """
    has_eval, score = extract_first_evaluator_verdict(log_path)
    source: Optional[str] = "log" if has_eval else None
    if score is None:
        ev = find_evaluator_trajectory()
        if ev:
            ev_has, ev_score = extract_eval_traj_verdict(ev)
            if ev_score is not None:
                return True, ev_score, "eval_traj"
            if ev_has and not has_eval:
                return True, None, "eval_traj"
    return has_eval, score, source


# ============================================================================
# 单任务统计主流程
# ============================================================================

def _load_task_config(config_file: Path) -> Dict[str, Any]:
    with open(config_file, encoding="utf-8") as f:
        return json.load(f)


def _main_agent_names(task_config: Dict[str, Any]) -> List[str]:
    """agents[].name 去掉 evaluator, 顺序保持."""
    names: List[str] = []
    for a in task_config.get("agents") or []:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not name or name == "evaluator":
            continue
        names.append(name)
    return names


def _task_level(
    has_traj: bool,
    ge3: bool,
    plain: bool,
    has_eval: bool,
    score: Optional[float],
) -> str:
    """L0/L1/L1.5/L2/L3 逐层递进."""
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
    log_path: Path,
) -> Dict[str, Any]:
    """对单个「主 agent」出一份指标 (合并到任务级前的原子结果)."""
    has_traj = traj_path is not None
    metrics = analyze_trajectory(traj_path) if traj_path else {
        "tool_calls": 0,
        "plain_rounds": 0,
        "assistant_rounds": 0,
    }
    has_eval, score, verdict_source = resolve_first_verdict(log_path)

    ge3 = metrics["tool_calls"] >= 3
    plain = metrics["plain_rounds"] > 0

    return {
        "agent": agent_name,
        "trajectory": str(traj_path) if traj_path else None,
        "tool_calls": metrics["tool_calls"],
        "assistant_rounds": metrics["assistant_rounds"],
        "plain_rounds": metrics["plain_rounds"],
        "has_trajectory": has_traj,
        "has_ge3_toolcalls": ge3,
        "has_plain_round": plain,
        "has_eval": has_eval,
        "evaluator_completion": score,
        "verdict_source": verdict_source,
        "level": _task_level(has_traj, ge3, plain, has_eval, score),
    }


def compute_task_status(config_file: Optional[str] = None) -> Dict[str, Any]:
    """核心函数: 对单个任务算出完整统计结果 (dict)。

    参数:
      config_file : task_config.json 路径

    路径全部内联:
      log       = logs/<config_stem>.log
      harness   = ~/.openclaw/agents/<agent-name>/sessions/*.jsonl
    """
    task_config = _load_task_config(config_file)
    task_name = config_file.stem
    log_path = f"logs/{task_name}.log"

    agent_names = _main_agent_names(task_config)
    per_agent = [
        _summarize_per_agent(name, find_assistant_trajectory(name), log_path)
        for name in agent_names
    ]

    # 任务级汇总: 取所有主 agent 里最好的一档
    def _rank(entry: Dict[str, Any]) -> int:
        return {"none": 0, "L0": 1, "L1": 2, "L1.5": 3, "L2": 4, "L3": 5}.get(entry["level"], 0)

    best = max(per_agent, key=_rank) if per_agent else None

    return {
        "task": task_name,
        "config_file": str(config_file),
        "log_file": str(log_path),
        "harness_home": str(HARNESS_HOME),
        "agents": per_agent,
        "task_level": best["level"] if best else "none",
        "best_completion": best["evaluator_completion"] if best else None,
    }


# ============================================================================
# harness_automation.py 集成入口
# ============================================================================

def run_stats(config_file: Optional[str] = None, traj_stats_result: Optional[str] = None, harness_type: Optional[str] = None) -> Path:
    """在 harness 跑完后写一份任务统计 json"""
    config_path = Path(config_file) if config_file else None
    result_path = Path(traj_stats_result) if traj_stats_result else None
    result = compute_task_status(config_path)
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
            default="openclaw",
            help="harness类型(不指定则使用配置文件中的 harness_type,缺省为 openclaw)"
    )
    args = parser.parse_args()

    config_file = Path(args.config).expanduser().resolve()

    run_stats(config_file, args.traj_stats_result, args.harness)
    print(f"任务统计已写入: {args.traj_stats_result}")


if __name__ == "__main__":
    main()
