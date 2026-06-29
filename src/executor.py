"""
统一查询执行器

共享的查询循环逻辑(变量替换、simulator 多轮对话、turn 管理),
harness 差异通过 get_agent_fn / execute_with_retry_fn 回调注入。
"""

import asyncio
import logging
import re
from typing import Any, Callable, Awaitable, Dict, List, Optional

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60


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


async def execute_queries(
    queries: list,
    get_agent_fn: Callable[[str, str], Any],
    execute_with_retry_fn: Callable[[Any, str, Any], Awaitable[Any]],
    simulator=None,
    max_turn: int = 5,
    run_id: str = "",
    pre_query_hook: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """统一查询执行循环

    Args:
        queries: 查询任务列表 (QueryItem)
        get_agent_fn: (agent_name, session_name) -> agent 对象
        execute_with_retry_fn: (agent, query_text, options) -> result 对象
        simulator: 用户模拟器,None 则仅单轮
        max_turn: 多轮对话最大轮次
        run_id: 本次 run 的唯一 id
        pre_query_hook: 每个 query 执行前的钩子 (如 openclaw 的 check_readyz)
    """
    logger.info("=" * 60)
    logger.info("开始执行查询任务")
    logger.info("=" * 60)

    results: Dict[str, Any] = {}

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

        query_simulator = simulator if query.use_simulator else None

        if query_simulator is not None:
            query_simulator.update_origin_query(query_text)

        current_query = query_text
        last_result = None
        success = False
        retry = 0

        for turn in range(1, max_turn + 1 if query_simulator else 2):
            logger.debug("[Q%d] %s", turn, current_query)
            agent = get_agent_fn(query.agent_name, session_name)

            try:
                result = await execute_with_retry_fn(agent, current_query, options)
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

            user_reply = query_simulator.chat(agent_reply)
            logger.debug("[S%d] %s", turn, user_reply)

            if "【Task_Done】" in user_reply:
                logger.info("任务完成(Turn %d)", turn)
                try:
                    await execute_with_retry_fn(agent, "真棒", options)
                except Exception:
                    pass
                success = True
                break
            elif "【Task_Failed】" in user_reply:
                logger.error("任务失败(Turn %d):%s", turn, user_reply)
                try:
                    await execute_with_retry_fn(agent, "好吧", options)
                except Exception:
                    pass
                break

            current_query = user_reply
        else:
            if query_simulator is not None:
                logger.warning("达到最大轮次 %d,任务未完成", max_turn)

        results[f"result_{query.agent_name}"] = last_result

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
    return None
