"""Evaluator 单测(能力: trajectory-evaluation)。

用法:  python test/test_evaluator.py
不依赖网关/网络/LLM,只验证纯逻辑:结构化解析、证据渲染、反馈格式化。
LLM 裁判本身的判准需后续用校准集验证(本测试不覆盖)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openclaw_sdk.output.structured import StructuredOutput

from evaluator import EvaluationResult, Evaluator, EvaluatorConfig, RubricCheck
from trajectory import FileEvidence, TurnRecord, Trajectory, _render_turn


def test_structured_eval_output_parses():
    """3.5: 评估结构化输出可解析。"""
    raw = (
        "好的,这是我的裁决:\n```json\n"
        '{"completion": 40, "inclination": "reject", '
        '"violations": ["声称生成 b.md 但磁盘上不存在"], '
        '"improvements": ["先真正写出文件"], '
        '"citations": ["b.md: 声称生成,但磁盘上不存在 ✗"], '
        '"reason": "声称与磁盘证据矛盾"}\n```'
    )
    ev = StructuredOutput.parse(raw, EvaluationResult)
    assert ev.completion == 40
    assert ev.inclination == "reject"
    assert ev.violations and "b.md" in ev.violations[0]
    print("✓ 评估结构化输出解析")


def test_false_positive_surfaced_to_evaluator():
    """3.5: 话术型假阳性(声称 vs 磁盘矛盾)在喂给裁判的证据里被点名。"""
    rec = TurnRecord(
        turn=1,
        user_input="帮我写 b.md",
        agent_content="我已经生成了 b.md",
        files=[FileEvidence(name="b.md", checked=True, exists=False)],
    )
    text = _render_turn(rec)
    assert "磁盘上不存在" in text  # 裁判能看到矛盾
    print("✓ 声称但磁盘无的文件被暴露给裁判")


def test_evidence_incomplete_marked_non_negative():
    """3.5: evidence_incomplete 样本被标注为"不得当负面证据"。"""
    rec = TurnRecord(turn=1, user_input="q", agent_content="...", evidence_incomplete=True)
    text = _render_turn(rec)
    assert "证据缺失 ≠ 证据为负" in text
    print("✓ 证据不完整被标注为非负面")


def test_format_feedback():
    """4.x: 反馈文本格式化(给 simulator 看)。"""
    ev_obj = Evaluator(EvaluatorConfig(enabled=True), client=None, run_id="t")
    fb = ev_obj.format_feedback(
        EvaluationResult(
            completion=80, inclination="accept",
            improvements=["补充数据来源"], violations=[],
            citations=["report.md: 存在 ✓"], reason="基本达成",
        )
    )
    assert "完成度: 80/100" in fb
    assert "倾向: accept" in fb
    assert "改进点" in fb
    print("✓ 反馈格式化")


def _mk_eval(**cfg) -> Evaluator:
    return Evaluator(EvaluatorConfig(enabled=True, **cfg), client=None, run_id="t")


def _mk_turn() -> tuple[Trajectory, TurnRecord]:
    traj = Trajectory(query="帮我订一张去北京的往返机票", agent_name="main")
    rec = TurnRecord(turn=1, user_input="订票", agent_content="已为你预订")
    traj.turns.append(rec)
    return traj, rec


RUBRIC = ["机票为往返程", "出发地与目的地正确", "已给出订单确认号"]


def test_rubric_check_and_result_parse():
    """5.1: RubricCheck/EvaluationResult.rubric_checks 结构化解析。"""
    raw = (
        '{"completion": 60, "inclination": "reject", '
        '"violations": ["缺少返程"], "improvements": ["补订返程"], '
        '"citations": ["无往返工具调用记录"], '
        '"rubric_checks": ['
        '{"criterion": "机票为往返程", "status": "fail", "evidence": "仅见单程预订"},'
        '{"criterion": "已给出订单确认号", "status": "unverifiable", "evidence": "工具返回缺失"}'
        '], "reason": "返程未完成"}'
    )
    ev = StructuredOutput.parse(raw, EvaluationResult)
    assert len(ev.rubric_checks) == 2
    assert ev.rubric_checks[0].status == "fail"
    # 5.5: unverifiable 被原样保留,未被强制改判为 fail
    assert ev.rubric_checks[1].status == "unverifiable"
    print("✓ rubric_checks 解析 + unverifiable 保留")


def test_build_prompt_injects_rubric_when_present():
    """5.2: 有 rubric 时注入逐条清单, 空 rubric 时不注入。"""
    ev = _mk_eval()
    traj, rec = _mk_turn()
    with_rubric = ev._build_prompt(traj, rec, None, rubric=RUBRIC)
    assert "验收清单(Rubric" in with_rubric
    for c in RUBRIC:
        assert c in with_rubric  # 注入给 evaluator(非 simulator)
    without = ev._build_prompt(traj, rec, None, rubric=[])
    assert "验收清单(Rubric" not in without  # 4.1: 空 rubric 走自由维度
    print("✓ rubric 注入/缺省分支")


def test_build_prompt_states_unverifiable_rule():
    """5.5: 提示词明确 unverifiable 不得判负。"""
    ev = _mk_eval()
    traj, rec = _mk_turn()
    p = ev._build_prompt(traj, rec, None, rubric=RUBRIC)
    assert "unverifiable" in p
    assert "MUST NOT 当作 `fail`" in p
    print("✓ 提示词含 unverifiable 铁律")


def test_format_feedback_does_not_leak_rubric():
    """5.3: 回流 simulator 的反馈不含 rubric 准则原文(边界 X)。"""
    ev_obj = _mk_eval()
    fb = ev_obj.format_feedback(
        EvaluationResult(
            completion=60, inclination="reject",
            improvements=["补订返程"], violations=["缺少返程"],
            citations=["无往返工具调用记录"],
            rubric_checks=[
                RubricCheck(criterion="机票为往返程", status="fail", evidence="仅见单程"),
            ],
            reason="返程未完成",
        )
    )
    assert "机票为往返程" not in fb  # rubric 准则原文 MUST NOT 泄漏给 simulator
    assert "补订返程" in fb           # 提炼后的改进点仍回流
    print("✓ format_feedback 不泄漏 rubric 原文")


def test_feedback_to_simulator_switch():
    """5.4: feedback_to_simulator 开关值; 默认 False(安全不回流)。"""
    assert EvaluatorConfig(enabled=True).feedback_to_simulator is False  # 安全默认
    assert _mk_eval(feedback_to_simulator=True).feedback_to_simulator is True
    assert _mk_eval(feedback_to_simulator=False).feedback_to_simulator is False
    print("✓ feedback_to_simulator 开关/默认")


if __name__ == "__main__":
    test_structured_eval_output_parses()
    test_false_positive_surfaced_to_evaluator()
    test_evidence_incomplete_marked_non_negative()
    test_format_feedback()
    test_rubric_check_and_result_parse()
    test_build_prompt_injects_rubric_when_present()
    test_build_prompt_states_unverifiable_rule()
    test_format_feedback_does_not_leak_rubric()
    test_feedback_to_simulator_switch()
    print("\n全部通过 ✅ (test_evaluator)")
