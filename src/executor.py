"""
统一查询执行器

共享的查询循环逻辑(变量替换、simulator 多轮对话、turn 管理),
harness 差异通过 get_agent_fn / execute_with_retry_fn 回调注入。
"""

import asyncio
import logging
import re
from typing import Any, Callable, Awaitable, Dict, List, Optional
from pathlib import Path

from user_simulator import User_simulator
from src.config import QueryItem
from src.evaluator.evaluator import (
    Evaluator, 
    EvaluateConfig,
    create_evaluator,
    _restore_eval_files, 
    _isolate_eval_files
)
from src.evaluator.trajectory import (
    Trajectory, 
    ToolCallEvidence,
    build_turn_record, 
    capture_file_evidence,
    extract_tool_calls
)

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60
EXECUTION_HISTORY_FALLBACK_LIMIT = 50
EXECUTION_HISTORY_FALLBACK_MAX_POLLS = 40
EXECUTION_HISTORY_FALLBACK_POLL_INTERVAL_SECONDS = 30.0


def _replace_variables(text: str, results: Dict[str, Any]) -> str:
    pattern = r'\{result_(\w+)\}'

    def replacer(match):
        result_key = match.group(0)[1:-1]
        result = results.get(result_key)
        if result is None:
            return f"[Error: {result_key} not found]"
        elif hasattr(result, 'content'):
            return result.content
        return str(result)

    return re.sub(pattern, replacer, text)


async def _safe_chat_history(agent) -> List[dict[str, Any]]:
    """安全拉取被测 agent 会话历史(失败降级为空,绝不中断主流程)。

    仅 OpenClaw client 提供 `gateway.chat_history`;Hermes/Claudecode 无 gateway,
    直接返回空(其 ExecutionResult 自带完整历史,不需要此 fallback)。
    """
    gateway = getattr(getattr(agent, "_client", None), "gateway", None)
    if gateway is None:
        return []
    try:
        return await gateway.chat_history(
            agent.session_key, limit=EXECUTION_HISTORY_FALLBACK_LIMIT
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("chat_history 采集失败: %s", e)
        return []

def _new_messages_since(
    before: List[dict[str, Any]], after: List[dict[str, Any]]
) -> List[dict[str, Any]]:
    """从 after 取出相对 before 新增的消息(按 timestamp 界,稳健于 limit 截断)。"""
    if not after:
        return []
    if not before:
        return list(after)
    before_max_ts = max(
        (m.get("timestamp", 0) for m in before if isinstance(m, dict)), default=0
    )
    return [
        m for m in after
        if isinstance(m, dict) and m.get("timestamp", 0) > before_max_ts
    ]

async def process_turn(
    client: Any,
    query: QueryItem,
    turn: int,
    current_query: str,
    result: Any,
    evidence_incomplete: bool,
    trajectory: Trajectory,
    evaluator: Optional[Evaluator],
    agent: Any = None,
    before_history: Optional[List[dict[str, Any]]] = None,
) -> Optional[str]:
    """逐轮处理(仅多轮 simulator 路径):能力1 每轮捕获带证据轨迹 + 能力2 按 eval_step 节流评估。
    
    单轮对话不进入本函数(不采集轨迹)。每个 turn 都捕获轨迹(供评审窗口取数),
    但仅在评审点(`turn % eval_step == 0`)触发 evaluator;被跳过的轮给 simulator 喂空。

    Returns:
        evaluator_feedback: 本轮喂回 simulator 的反馈;跳过轮/未回流/评估失败均为 None。
    """
    # evaluator 未启用:轨迹无人消费(evaluator 是其唯一 reader),既不评估也不采集
    if evaluator is None:
        return None

    # 能力1:从 OC chat_history 解析本轮新增工具调用(SDK 的 ExecutionResult.tool_calls
    # 对服务端自主 agent 恒空),再逐轮捕获带证据的轨迹(文件证据升级为磁盘真相 D5)。
    # 即便本轮不评审也要捕获,否则评审点窗口取不到中间轮数据。
    # before_history 须由调用方在 execute 之前采集(本轮基线),after 在此处取以截取增量。
    turn_tool_calls: Optional[List[ToolCallEvidence]] = None
    if agent is not None:
        try:
            after_history = await _safe_chat_history(agent)
            new_msgs = _new_messages_since(before_history or [], after_history)
            turn_tool_calls = extract_tool_calls(new_msgs)
            # 兜底但从 history 救回了工具证据 → 不再算"证据不完整"
            if evidence_incomplete and turn_tool_calls:
                evidence_incomplete = False
        except Exception as e:  # noqa: BLE001
            logger.debug("解析本轮 tool_calls 失败,降级为空: %s", e)

    turn_record = build_turn_record(
        turn, current_query, result, evidence_incomplete, tool_calls=turn_tool_calls
    )
    try:
        await capture_file_evidence(client, query.agent_name, turn_record)
    except Exception as e:  # noqa: BLE001
        logger.debug("文件证据捕获失败: %s", e)
    trajectory.turns.append(turn_record)

    # 能力2:eval_step 节流——仅在评审点触发评估;跳过轮喂空(simulator 仍拍板)
    step = evaluator.config.eval_step
    if turn % step != 0:
        logger.debug("[Evaluator] turn=%d 未达评审点(eval_step=%d),跳过并喂空", turn, step)
        return None

    window = step  # 最近 X 轮 = eval_step,窗口正好覆盖两次评审之间的全部 turn
    logger.info("[Evaluator] 调用 agent=%s turn=%d window=%d", evaluator.config.agent_name, turn, window)
    rubric_items = evaluator.config.rubric_items()  # 结构化 rubric(旧式字符串自动归一)
    try:
        ev = await evaluator.evaluate_turn(
            trajectory, turn_record, rubric=rubric_items, window=window
        )
    except Exception as e:
        logger.warning("evaluator 调用异常: %s", e)
        ev = None

    if ev is not None:
        # 能力3:评分累积进轨迹(供落盘);终局评审点的 completion(0~1) 为该 query 最终成绩
        trajectory.evaluations.append({
            "turn": turn,
            "completion": ev.completion,
            "gate_status": ev.gate_status,
            "bucket_scores": ev.bucket_scores,
            "inclination": ev.inclination,
            "rubric_checks": [rc.model_dump() for rc in ev.rubric_checks],
        })
    if evaluator.to_simulator and ev is not None:
        return evaluator.format_feedback(ev)
    return None

async def execute_queries(
    queries: List[QueryItem],
    client: Any,
    get_agent_fn: Callable[[str, str], Any],
    execute_with_retry_fn: Callable[[Any, str, Any], Awaitable[Any]],
    simulator_factory: Optional[Callable[[], Optional[User_simulator]]] = None,
    max_turn: int = 5,
    agent_system_prompts: Optional[Dict[str, str]] = None,
    run_id: str = "",
    pre_query_hook: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """统一查询执行循环

    Args:
        queries: 查询任务列表 (QueryItem)
        get_agent_fn: (agent_name, session_name) -> evaluator-agent 对象
        execute_with_retry_fn: (agent, query_text, options) -> result 对象
        simulator_factory: 构造 User_simulator 的工厂(每个 session 调用一次);
            返回 None 表示未启用 simulator → 仅单轮
        max_turn: 多轮对话最大轮次
        evaluator: 第三方 Evaluator,None 或 disabled 则退回 simulator 自判
        run_id: 本次 run 的唯一 id
        pre_query_hook: 每个 query 执行前的钩子 (如 openclaw 的 check_readyz)
    """
    logger.info("=" * 60)
    logger.info("开始执行查询任务")
    logger.info("=" * 60)

    results: Dict[str, Any] = {}
    simulators: Dict[str, User_simulator] = {}

    for idx, query in enumerate(queries, 1):
        logger.info("任务 %d/%d: [%s|%s]", idx, len(queries), query.agent_name, query.session_name)
        logger.info("[Q] %s", query.text)

        query_text = _replace_variables(query.text, results)

        options = None
        if query.timeout:
            options = _make_options(query.timeout)

        base_session = query.session_name or "main"
        session_name = f"{base_session}_{run_id}"

        if pre_query_hook is not None:
            await pre_query_hook()

        # 首见即建、再见复用 → 同会话续聊保留记忆,跨会话隔离不泄露。
        query_simulator: Optional[User_simulator] = None
        if query.use_simulator and simulator_factory is not None:
            query_simulator = simulators.get(base_session)
            if query_simulator is None:
                query_simulator = simulator_factory()
                if query_simulator is not None:
                    simulators[base_session] = query_simulator

        if query_simulator is not None:
            query_simulator.update_origin_query(query_text)

        current_query = query_text
        last_result = None
        success = False
        retry = 0

        # 能力1:逐轮累积带证据的轨迹
        trajectory = Trajectory(query=query_text, agent_name=query.agent_name)
        # 能力2:per-query 构建持久 evaluator(无 evaluate 块则为 None);rubric/eval_step 取自该块。
        eval_sys_prompt = None
        if query.evaluate is not None:
            eval_sys_prompt = (agent_system_prompts or {}).get(query.evaluate.agent_name)
        evaluator = create_evaluator(query.evaluate, client, run_id, base_session, eval_sys_prompt, get_agent_fn)

        # 文件隔离:被测 agent 执行前,把本 query 的 oracle/rubrics 从磁盘删除(内容已在内存)。
        if evaluator is not None:
            _isolate_eval_files(query.evaluate)

        for turn in range(1, max_turn + 1 if query_simulator else 2):
            logger.debug("[Q%d] %s", turn, current_query)
            agent = get_agent_fn(query.agent_name, session_name)

            # 能力1:采集本轮工具证据基线(发送前的会话历史),供本轮结束后做增量解析
            before_history = await _safe_chat_history(agent)

            try:
                result, evidence_incomplete = await execute_with_retry_fn(
                    agent, current_query, options)
                last_result = result
                agent_reply = result.content
                logger.info("[A%d] %s", turn, agent_reply)
                if not agent_reply:
                    logger.debug(result)
            except Exception as e:
                import traceback
                logger.error("Agent 执行失败: %s", e)
                logger.debug(traceback.format_exc())
                break

            if not agent_reply:
                retry += 1
                if retry >= 3:
                    logger.error("连续3次未收到回复,任务失败")
                    break
                current_query = "没有看到你的回复,请重新执行。"
                continue

            retry = 0

            if query_simulator is None:
                success = True
                break
            
            evaluator_feedback = await process_turn(
                client, query, turn, current_query, result, evidence_incomplete,
                trajectory, evaluator, agent=agent, before_history=before_history,
            )

            user_reply = query_simulator.chat(agent_reply, evaluator_feedback=evaluator_feedback)
            logger.debug("[S%d] %s", turn, user_reply)

            if "【Task_Done】" in user_reply:
                logger.info("任务完成(Turn %d)", turn)
                trajectory.outcome = "done"
                success = True
                break
            elif "【Task_Failed】" in user_reply:
                logger.error("任务失败(Turn %d):%s", turn, user_reply)
                trajectory.outcome = "failed"
                break

            current_query = user_reply
        else:
            if query_simulator is not None:
                trajectory.outcome = "max_turn"
                logger.warning("达到最大轮次 %d,任务未完成", max_turn)

        # 文件隔离收尾:任务结束后把 oracle/rubrics 原始字节写回(best-effort,纯调试便利)。
        if evaluator is not None:
            _restore_eval_files(query.evaluate)

        results[f"result_{query.agent_name}"] = last_result

        # 能力3:轨迹 + 评分落盘(RL 样本)。evaluator 启用时才采集了轨迹,故仅此时落盘。
        if evaluator is not None and trajectory.turns:
            try:
                out_path = Path("logs") / "trajectories" / run_id / f"{base_session}.json"
                trajectory.save(out_path)
                logger.info("轨迹已落盘: %s (turns=%d, evals=%d, outcome=%s)",
                            out_path, len(trajectory.turns), len(trajectory.evaluations), trajectory.outcome)
            except Exception as e:
                logger.warning("轨迹落盘失败: %s", e)

        if not success:
            logger.error("任务 %d 失败,终止后续 %d 个任务", idx, len(queries) - idx)
            break

    return results


def _make_options(timeout: int):
    """构造 ExecutionOptions — 延迟导入避免循环依赖"""
    try:
        from openclaw_sdk import ExecutionOptions
        return ExecutionOptions(timeout_seconds=timeout)
    except ImportError:
        pass
    try:
        from src.hermes_client import ExecutionOptions
        return ExecutionOptions(timeout_seconds=timeout)
    except ImportError:
        pass
    try:
        from src.claudecode_client import ExecutionOptions
        return ExecutionOptions(timeout_seconds=timeout)
    except ImportError:
        pass
    return None

