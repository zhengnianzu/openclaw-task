"""
测试 Evaluator 评分逻辑（独立脚本，不依赖 openclaw gateway）

直接调用 user_proxy_model.json 中 evaluator 段配置的 OpenAI 兼容 API，
喂一段模拟 trajectory 给 evaluator 模型，验证：
1. 结构化输出解析（EvaluationResult）
2. Scorer 确定性评分（gate × 桶加权）
3. rubric_checks 逐条 0/1 判定

用法：
  cd /home/nianzuzheng/project/openclaw-task
  python test/test_evaluator.py
  python test/test_evaluator.py --proxy-config configs/user_proxy_model.json
  python test/test_evaluator.py --mode scorer   # 仅测 Scorer 逻辑，不调 API
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from openai import OpenAI

from src.evaluator.evaluator import (
    DEFAULT_EVAL_PROMPT,
    EvaluateConfig,
    EvaluationResult,
    Rubric,
    RubricCheck,
    Scorer,
    ScoringSpec,
    _parse_json_as,
)

DEFAULT_PROXY_CONFIG = "configs/user_proxy_model.json"

# ============================================================================
# 模拟数据
# ============================================================================

MOCK_RUBRICS = [
    Rubric(id="R1", when="gate", evaluator="llm_judge",
           text="明确给出一个具体城市作为答案"),
    Rubric(id="R2", when="final", evaluator="llm_judge",
           text="给出该城市为经济中心的依据(如 GDP/金融机构/交易所等)"),
    Rubric(id="R3", when="final", evaluator="llm_judge",
           text="回答使用中文"),
]

MOCK_ORIGIN_QUERY = "中国的经济中心是哪里？请给出依据。"

MOCK_AGENT_REPLY = (
    "中国的经济中心是上海。上海是中国大陆GDP最高的城市，拥有上海证券交易所、"
    "众多跨国公司总部和金融机构，是中国最重要的金融、贸易和航运中心。"
)

MOCK_TRAJECTORY = f"""Turn 1:
[User]: {MOCK_ORIGIN_QUERY}
[Agent]: {MOCK_AGENT_REPLY}
"""


# ============================================================================
# Test 1: Scorer 确定性评分（纯逻辑，不调 API）
# ============================================================================

def test_scorer():
    print("=" * 60)
    print("Test 1: Scorer 确定性评分逻辑")
    print("=" * 60)

    spec = ScoringSpec.from_scoring(None, MOCK_RUBRICS)
    scorer = Scorer(spec)
    print(f"  gate_ids: {spec.gate_ids}")
    print(f"  buckets:  {json.dumps({k: v.model_dump() for k, v in spec.buckets.items()}, ensure_ascii=False, indent=4)}")

    # 场景 A: 全部通过
    checks_a = {"R1": 1, "R2": 1, "R3": 1}
    result_a = scorer.score(checks_a)
    print(f"\n  场景 A (全过): completion={result_a['completion']}, gate_passed={result_a['gate_passed']}")
    assert result_a["completion"] == 1.0, f"期望 1.0, 实际 {result_a['completion']}"
    assert result_a["gate_passed"] is True
    print("  ✓ 通过")

    # 场景 B: gate 失败 → completion=0
    checks_b = {"R1": 0, "R2": 1, "R3": 1}
    result_b = scorer.score(checks_b)
    print(f"\n  场景 B (gate 挂): completion={result_b['completion']}, gate_passed={result_b['gate_passed']}")
    assert result_b["completion"] == 0.0, f"期望 0.0, 实际 {result_b['completion']}"
    assert result_b["gate_passed"] is False
    print("  ✓ 通过")

    # 场景 C: gate 过，部分 final 失败
    checks_c = {"R1": 1, "R2": 1, "R3": 0}
    result_c = scorer.score(checks_c)
    print(f"\n  场景 C (半过): completion={result_c['completion']}, gate_passed={result_c['gate_passed']}")
    assert 0.0 < result_c["completion"] < 1.0, f"期望 0~1 之间, 实际 {result_c['completion']}"
    assert result_c["gate_passed"] is True
    print("  ✓ 通过")

    # 场景 D: 缺失的 rubric_id 视为 0
    checks_d = {"R1": 1}
    result_d = scorer.score(checks_d)
    print(f"\n  场景 D (缺失): completion={result_d['completion']}, gate_passed={result_d['gate_passed']}")
    assert result_d["gate_passed"] is True
    assert result_d["completion"] == 0.0, f"期望 0.0 (R2/R3 缺失视为 0), 实际 {result_d['completion']}"
    print("  ✓ 通过")

    print("\n✅ Scorer 全部测试通过\n")


# ============================================================================
# Test 2: EvaluateConfig 解析
# ============================================================================

def test_config_parsing():
    print("=" * 60)
    print("Test 2: EvaluateConfig 解析与 rubric 归一化")
    print("=" * 60)

    # 旧式字符串 rubrics
    cfg1 = EvaluateConfig(
        agent_name="evaluator",
        rubrics=["明确给出城市", "给出依据"],
        eval_step=2,
        to_simulator=True,
    )
    items = cfg1.rubric_items()
    print(f"  旧式 rubrics → {len(items)} 条 Rubric, ids={[r.id for r in items]}")
    assert len(items) == 2
    assert all(r.when == "final" and r.evaluator == "llm_judge" for r in items)
    print("  ✓ 通过")

    # resolve_runtime
    cfg1.resolve_runtime()
    print(f"  scoring_spec: gate_ids={cfg1.scoring_spec.gate_ids}, buckets={list(cfg1.scoring_spec.buckets.keys())}")
    assert cfg1.scoring_spec is not None
    print("  ✓ 通过")

    # 别名兼容
    cfg2 = EvaluateConfig.model_validate({
        "evaluator_agent": "my_eval",
        "evaluate_every_n_turns": 3,
        "feedback_to_user": True,
    })
    assert cfg2.agent_name == "my_eval"
    assert cfg2.eval_step == 3
    assert cfg2.to_simulator is True
    print("  别名兼容 ✓")

    print("\n✅ Config 解析全部通过\n")


# ============================================================================
# Test 3: 调 API 测试端到端评估
# ============================================================================

def test_api_evaluation(proxy_config_path: str):
    print("=" * 60)
    print("Test 3: 端到端 API 评估（调用 evaluator 模型）")
    print("=" * 60)

    # 加载 evaluator 模型配置
    path = Path(proxy_config_path)
    if not path.exists():
        print(f"  ⚠ 配置文件不存在: {path}，跳过 API 测试")
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = raw.get("evaluator", raw)

    model = cfg.get("model", "gpt-4o")
    api_key = cfg.get("api_key")
    base_url = cfg.get("base_url")

    if not api_key:
        print("  ⚠ evaluator api_key 未配置，跳过 API 测试")
        return

    print(f"  model:    {model}")
    print(f"  base_url: {base_url}")

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=httpx.Client(verify=False),
    )

    # 构造评估 prompt
    criteria_json = json.dumps(
        [r.model_dump(exclude_none=True) for r in MOCK_RUBRICS],
        ensure_ascii=False, indent=2,
    )
    schema_json = json.dumps(EvaluationResult.model_json_schema(), indent=2)

    prompt = f"""{DEFAULT_EVAL_PROMPT}

【Origin Query】
{MOCK_ORIGIN_QUERY}

【最近 1 轮证据】
{MOCK_TRAJECTORY}

【验收 Rubric】
{criteria_json}

请按上述 rubric 逐条判 0/1，rubric_checks 必须覆盖全部 id。

Respond with valid JSON matching this schema:
```json
{schema_json}
```"""

    print(f"\n  发送评估请求 (prompt {len(prompt)} chars)...")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an independent task evaluator."},
                {"role": "user", "content": prompt},
            ],
        )
        reply = response.choices[0].message.content
        print(f"  收到回复 ({len(reply)} chars)")

        # 解析结构化输出
        result = _parse_json_as(reply, EvaluationResult)
        print(f"\n  解析结果:")
        print(f"    completion:  {result.completion}")
        print(f"    inclination: {result.inclination}")
        print(f"    violations:  {result.violations}")
        print(f"    improvements:{result.improvements}")
        print(f"    rubric_checks: {len(result.rubric_checks)} 条")
        for rc in result.rubric_checks:
            print(f"      [{rc.rubric_id}] passed={rc.passed} | {rc.criterion[:40]}...")

        # 用 Scorer 重算 completion
        spec = ScoringSpec.from_scoring(None, MOCK_RUBRICS)
        scorer = Scorer(spec)
        checks = {rc.rubric_id: rc.passed for rc in result.rubric_checks}
        scored = scorer.score(checks)
        print(f"\n  Scorer 重算:")
        print(f"    completion:  {scored['completion']}")
        print(f"    gate_passed: {scored['gate_passed']}")
        print(f"    gate_status: {scored['gate_status']}")
        print(f"    buckets:     {json.dumps(scored['bucket_scores'], ensure_ascii=False)}")

        print(f"\n  usage: {response.usage.prompt_tokens}p + {response.usage.completion_tokens}c = {response.usage.total_tokens} tokens")
        print("\n✅ API 评估测试通过\n")

    except Exception as e:
        print(f"\n  ❌ API 评估失败: {e}\n")
        import traceback
        traceback.print_exc()


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试 Evaluator")
    parser.add_argument("--proxy-config", default=DEFAULT_PROXY_CONFIG,
                        help=f"user_proxy_model.json 路径（默认: {DEFAULT_PROXY_CONFIG}）")
    parser.add_argument("--mode", default="all", choices=["all", "scorer", "config", "api"],
                        help="测试模式: all/scorer/config/api")
    args = parser.parse_args()

    modes = [args.mode] if args.mode != "all" else ["scorer", "config", "api"]

    if "scorer" in modes:
        test_scorer()
    if "config" in modes:
        test_config_parsing()
    if "api" in modes:
        test_api_evaluation(args.proxy_config)

    print("=" * 60)
    print("全部测试完成")
    print("=" * 60)
