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
    extract_tool_calls,
    extract_tool_calls_openai,
)
# 后台监测机制(子代理 spawn 完成检测 + 真实交付提取)
from src.bg_watch import (
    BG_NEW_CONTENT,
    BG_TIMEOUT,
    BG_WATCH_INTERVAL,
    BG_WATCH_TIMEOUT,
    _background_watch,
    _collect_spawned_children,
    _gateway_of,
    _new_messages_since,
    _safe_chat_history,
)

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

    # 能力1:从各 client 获取本轮工具调用证据,再逐轮捕获带证据的轨迹(文件证据升级为磁盘真相 D5)。
    # 即便本轮不评审也要捕获,否则评审点窗口取不到中间轮数据。
    # before_history 须由调用方在 execute 之前采集(本轮基线),after 在此处取以截取增量。
    turn_tool_calls: Optional[List[ToolCallEvidence]] = None
    if agent is not None:
        try:
            # 优先级1: 直接从 ExecutionResult.tool_calls 获取
            result_tool_calls = getattr(result, "tool_calls", None)
            if result_tool_calls:
                # Pi/Grok/Codex/CC: ExecutionResult.tool_calls 已填充,直接使用
                turn_tool_calls = [
                    ToolCallEvidence(
                        tool=tc.tool,
                        input=tc.input,
                        output=tc.output,
                        duration_ms=getattr(tc, "duration_ms", None),
                    )
                    for tc in result_tool_calls
                ]
            else:
                # 优先级2: 从 chat_history 或 messages 解析
                gateway = getattr(getattr(agent, "_client", None), "gateway", None)
                if gateway is None:
                    # Hermes/opencode/openjiuwen工具证据从messages(run_conversation 返回的原生 OpenAI 消息)解析。
                    native_msgs = getattr(result, "messages", None) or []
                    turn_tool_calls = extract_tool_calls_openai(native_msgs)
                else:
                    # OpenClaw:走网关 chat_history,按 timestamp 增量截取本轮新增消息。
                    after_history = await _safe_chat_history(agent)
                    new_msgs = _new_messages_since(before_history or [], after_history)
                    turn_tool_calls = extract_tool_calls(new_msgs)
            # 兜底但从证据里救回了工具调用 → 不再算"证据不完整"
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
    # 前置检测门控:执行中(task_declared_complete=false)本轮不回流反馈给 simulator
    # (走已有空反馈降级路径);评估自身仍照常执行并已落盘,仅门控"是否回流"这一步。
    if evaluator.to_simulator and ev is not None and ev.task_declared_complete:
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
            options = _make_options(query.timeout, client)

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
            # 后台监测: 扫描本轮增量消息，从 sessions_spawn 里提取 childSessionKey
            # 等全部子代理都完成(completion event 收齐)后才进入下一轮真实对话。
            #   - 全部完成 → 取父 agent 最终交付给 S,清空待完成集合,正常走本轮
            #   - 超时/子代理进程失效 → 兜底,注入系统提示促 simulator 追问
            bg_notice = ""
            newly_spawned = await _collect_spawned_children(agent, before_history)
            # pending_children 挂在 agent 上跨轮持久,覆盖"上一轮未完成的子代理超时未完成"的情况
            pending_children: set = getattr(agent, "_bg_pending_children", None) or set()
            pending_children.update(newly_spawned)

            if pending_children:
                logger.info(
                    "[后台检测] 待完成子代理 %d 个(turn=%d): %s",
                    len(pending_children), turn, sorted(pending_children),
                )
                bg_status, bg_text = await _background_watch(
                    agent, before_history, list(pending_children),
                )
                if bg_status == BG_NEW_CONTENT and bg_text:
                    # 全部子代理完成,父 agent 已综合交付 → 本轮真实交付
                    agent_reply = bg_text
                    result = result.model_copy(update={"content": bg_text})
                    last_result = result
                    logger.info("[A%d·后台交付] %s", turn, agent_reply)
                    pending_children.clear()
                else:
                    # 超时兜底:未完成子代理保留在 pending,带入下一轮继续追踪
                    wait_min = int(BG_WATCH_TIMEOUT // 60) or 1
                    bg_notice = (
                        f"\n\n【系统提示】后台子代理长时间未全部完成,"
                        f"已等待约 {wait_min} 分钟。请向对方确认后台任务实际进展"
                    )
            agent._bg_pending_children = pending_children  # 空集也回写,确保状态一致

            evaluator_feedback = await process_turn(
                client, query, turn, current_query, result, evidence_incomplete,
                trajectory, evaluator, agent=agent, before_history=before_history,
            )

            user_reply = query_simulator.chat(agent_reply + bg_notice, evaluator_feedback=evaluator_feedback)
            logger.debug("[S%d] %s", turn, user_reply)

            # 空 user_reply 绝不能原样作为下一轮 current_query 下发给网关
            # (网关拒空消息 → message or attachment required → 误判连接异常空转)。
            # simulator 层已保证重试仍空时返回【Task_Failed】,此处为纵深防御:
            # 任何来源的空回复都判失败(simulator 未正常产出=故障),不下发、保留轨迹落盘。
            if not (user_reply or "").strip():
                logger.error("simulator 回复为空(Turn %d);判为失败,不下发空消息", turn)
                trajectory.outcome = "failed"
                break

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


def _make_options(timeout: int, client: Any):
    """构造 ExecutionOptions — 按 client 所属模块路由,延迟导入避免循环依赖。

    openclaw_sdk.ExecutionOptions 有 pydantic 约束 le=3600,需要绕过并打印一次提示;
    其余 harness 是简单 dataclass,直接构造即可。
    """
    client_module = type(client).__module__ or ""

    # openclaw_sdk: pydantic 约束 le=3600,需绕过 + 打印提示
    if client_module.startswith("openclaw_sdk") or client_module.startswith("src.openclaw_client"):
        from openclaw_sdk import ExecutionOptions as OpenclawOptions
        opts = OpenclawOptions()
        try:
            object.__setattr__(opts, "timeout_seconds", int(timeout))
        except Exception:
            from pydantic import Field
            class _Unbounded(OpenclawOptions):
                timeout_seconds: int = Field(default=300, ge=1)
            opts = _Unbounded(timeout_seconds=int(timeout))
        if timeout > 3600:
            logger.info("openclaw: 已绕过 SDK 3600 上限,向网关下发单次超时 %ds", timeout)
        return opts

    # 其余 harness:模块 → ExecutionOptions 直接映射
    harness_options = {
        "src.hermes_client":     ("src.hermes_client",     "ExecutionOptions"),
        "src.claudecode_client": ("src.claudecode_client", "ExecutionOptions"),
        "src.openjiuwen_client": ("src.openjiuwen_client", "ExecutionOptions"),
        "src.opencode_client":   ("src.opencode_client",   "ExecutionOptions"),
        "src.codex_client":      ("src.codex_client",      "ExecutionOptions"),
        "src.pi_client":         ("src.pi_client",         "ExecutionOptions"),
        "src.grok_client":       ("src.grok_client",         "ExecutionOptions"),
    }
    entry = harness_options.get(client_module)
    if entry is None:
        logger.warning("_make_options: 未识别的 client 模块 %s,不下发 timeout", client_module)
        return None
    mod_path, cls_name = entry
    mod = __import__(mod_path, fromlist=[cls_name])
    Cls = getattr(mod, cls_name)
    return Cls(timeout_seconds=int(timeout))
