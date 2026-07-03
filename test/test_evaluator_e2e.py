"""
端到端 Evaluator 测试（基于 config_eval.json 真实数据）

从 260702/environments/ 加载真实 oracle.json 和 user_queries.json，
直接调 user_proxy_model.json 的 evaluator 模型走完整评估流程：
  加载配置 → 解析 rubrics/scoring → 构建 prompt → 调 LLM → 解析 → Scorer 评分

用法：
  cd /home/nianzuzheng/project/openclaw-task
  python test/test_evaluator_e2e.py
  python test/test_evaluator_e2e.py --mock-reply good   # 模拟好回复
  python test/test_evaluator_e2e.py --mock-reply bad    # 模拟差回复
"""

import json
import argparse
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
    Scorer,
    ScoringSpec,
    _parse_json_as,
)

DEFAULT_PROXY_CONFIG = "configs/user_proxy_model.json"
DATA_DIR = Path("260702/environments/08_科研助手_task1_描述统计与相关分析")

ORIGIN_QUERY = (
    "我D盘论文数据文件夹里有个《调研数据.xlsx》，20个受访者的受教育年限、工龄和月薪。"
    "帮我算下这三项各自的均值和样本标准差，再看看两两之间（学历跟月薪、工龄跟月薪、"
    "学历跟工龄）的相关性，哪对显著哪对不显著。结果我要写进毕业论文的，得准。"
)

# 两种模拟回复：好的和差的
MOCK_REPLIES = {
    "good": """
根据《调研数据.xlsx》的20条数据，使用 Python (pandas + scipy) 计算结果如下：

```python
import pandas as pd
from scipy.stats import pearsonr
df = pd.read_excel('调研数据.xlsx')
print(df[['edu_years','work_exp','monthly_wage']].describe())
```

## 描述统计（n=20, ddof=1）

| 指标 | 均值 | 样本标准差 |
|------|------|-----------|
| 受教育年限 | 15.10 | 3.2265 |
| 工龄 | 5.15 | 3.265 |
| 月薪 | 8060.00 | 3250.81 |

## Pearson 相关分析

| 变量对 | r | p值 | 显著性(α=0.05) |
|--------|---|-----|----------------|
| 学历 × 月薪 | 0.9483 | 2.09e-10 | **显著** |
| 工龄 × 月薪 | 0.3031 | 0.194 | 不显著 |
| 学历 × 工龄 | 0.1334 | 0.575 | 不显著 |

结论：仅"受教育年限与月薪"存在显著强正相关(r=0.948, p<0.001)，其余两对均不显著。
""",
    "bad": """
根据数据分析：

三个变量的均值分别是：受教育年限16.5年，工龄7.2年，月薪9500元。
标准差分别是2.1、4.3、2800。

相关性分析显示三对变量之间都存在显著相关关系：
- 学历与月薪相关系数0.85
- 工龄与月薪相关系数0.72
- 学历与工龄相关系数0.68

以上结果可用于论文。
""",
}


def load_real_data():
    """加载真实 oracle 和 rubrics/scoring"""
    oracle_path = DATA_DIR / "oracle.json"
    queries_path = DATA_DIR / "user_queries.json"

    if not oracle_path.exists():
        raise FileNotFoundError(f"oracle.json 不存在: {oracle_path}")
    if not queries_path.exists():
        raise FileNotFoundError(f"user_queries.json 不存在: {queries_path}")

    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    queries = json.loads(queries_path.read_text(encoding="utf-8"))

    evaluate_block = queries[0]["evaluate"][0]
    raw_rubrics = evaluate_block["custom_rubrics"]
    scoring = evaluate_block["scoring"]

    rubrics = [Rubric.from_raw(r, i) for i, r in enumerate(raw_rubrics, 1)]

    return oracle, rubrics, scoring


def load_evaluator_config(proxy_config_path: str) -> dict:
    path = Path(proxy_config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw.get("evaluator", raw)


def build_prompt(rubrics: list[Rubric], oracle: dict, trajectory: str) -> str:
    criteria = json.dumps(
        [r.model_dump(exclude_none=True) for r in rubrics],
        ensure_ascii=False, indent=2,
    )
    oracle_json = json.dumps(oracle, ensure_ascii=False, indent=2)
    schema = json.dumps(EvaluationResult.model_json_schema(), indent=2)

    return f"""{DEFAULT_EVAL_PROMPT}

【Origin Query】
{ORIGIN_QUERY}

【最近 1 轮证据】
{trajectory}

【Oracle Ground Truth】
{oracle_json}

【验收 Rubric】
{criteria}

请按上述 rubric 逐条判 0/1，rubric_checks 必须覆盖全部 id。

Respond with valid JSON matching this schema:
```json
{schema}
```"""


def run_e2e(proxy_config_path: str, mock_reply_type: str):
    print("=" * 60)
    print(f"Evaluator 端到端测试 (mock_reply={mock_reply_type})")
    print("=" * 60)

    # 1. 加载真实数据
    print("\n[加载数据]")
    oracle, rubrics, scoring = load_real_data()
    print(f"  oracle: {len(json.dumps(oracle))} chars")
    print(f"  rubrics: {len(rubrics)} 条")
    for r in rubrics:
        print(f"    [{r.id}] when={r.when}, evaluator={r.evaluator} | {r.text[:50]}...")
    print(f"  scoring: gate_zero={scoring['gate_zero']}, buckets={list(scoring['bucket_map'].keys())}")

    # 2. 构建 Scorer
    spec = ScoringSpec.from_scoring(scoring, rubrics)
    scorer = Scorer(spec)
    print(f"\n[Scorer]")
    print(f"  gate_ids: {spec.gate_ids}")
    for name, b in spec.buckets.items():
        ids_str = ", ".join(b.rubric_ids) if b.rubric_ids else "(空)"
        print(f"  bucket '{name}': weight={b.weight}, ids=[{ids_str}]")

    # 3. 加载 evaluator 模型配置
    cfg = load_evaluator_config(proxy_config_path)
    model = cfg.get("model", "gpt-4o")
    api_key = cfg.get("api_key")
    base_url = cfg.get("base_url")

    print(f"\n[Evaluator 模型]")
    print(f"  model:    {model}")
    print(f"  base_url: {base_url}")

    if not api_key:
        print("  ⚠ api_key 未配置，无法调用 API")
        return

    # 4. 构建模拟 trajectory
    agent_reply = MOCK_REPLIES[mock_reply_type]
    trajectory = f"Turn 1:\n[User]: {ORIGIN_QUERY}\n[Agent]: {agent_reply}\nevidence_incomplete: false\n"

    prompt = build_prompt(rubrics, oracle, trajectory)
    print(f"\n[评估请求]")
    print(f"  prompt: {len(prompt)} chars")

    # 5. 调 API
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=httpx.Client(verify=False),
    )

    print(f"  调用 {model} ...")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an independent task evaluator."},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as e:
        print(f"\n  API 调用失败: {e}")
        return

    reply = response.choices[0].message.content
    print(f"  收到回复 ({len(reply)} chars)")

    # 6. 解析
    print(f"\n[解析 EvaluationResult]")
    try:
        result = _parse_json_as(reply, EvaluationResult)
    except Exception as e:
        print(f"  解析失败: {e}")
        print(f"  原始回复:\n{reply[:800]}")
        return

    print(f"  completion(模型自报): {result.completion}")
    print(f"  inclination: {result.inclination}")

    if result.rubric_checks:
        print(f"\n  rubric_checks ({len(result.rubric_checks)} 条):")
        for rc in result.rubric_checks:
            status = "PASS" if rc.passed else "FAIL"
            print(f"    [{rc.rubric_id:4s}] {status} | {rc.criterion[:60]}")
            if rc.evidence:
                print(f"           evidence: {rc.evidence[:80]}...")

    if result.violations:
        print(f"\n  violations:")
        for v in result.violations:
            print(f"    - {v[:80]}")

    if result.improvements:
        print(f"\n  improvements:")
        for imp in result.improvements:
            print(f"    - {imp[:80]}")

    # 7. Scorer 重算
    checks = {rc.rubric_id: rc.passed for rc in result.rubric_checks}
    scored = scorer.score(checks)

    print(f"\n[Scorer 重算]")
    print(f"  completion: {scored['completion']}")
    print(f"  gate_passed: {scored['gate_passed']}")
    print(f"  gate_status: {scored['gate_status']}")
    for name, bs in scored["bucket_scores"].items():
        print(f"  bucket '{name}': {bs['passed']}/{bs['total']} = {bs['ratio']:.2f} (w={bs['weight']}, score={bs['score']:.4f})")

    # 8. 模拟 format_feedback
    result.completion = scored["completion"]
    feedback_lines = [f"完成度: {result.completion} | 倾向: {result.inclination}"]
    if result.violations:
        feedback_lines.append("不符合要求项:\n- " + "\n- ".join(result.violations))
    if result.improvements:
        feedback_lines.append("改进点:\n- " + "\n- ".join(result.improvements))
    print(f"\n[Simulator 反馈]")
    print("  " + "\n  ".join(feedback_lines))

    # 9. Token
    usage = response.usage
    print(f"\n[Token] prompt={usage.prompt_tokens} + completion={usage.completion_tokens} = {usage.total_tokens}")

    print(f"\n{'=' * 60}")
    gate_str = "PASS" if scored["gate_passed"] else "FAIL"
    print(f"结果: completion={scored['completion']}, gate={gate_str}, inclination={result.inclination}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluator 端到端测试")
    parser.add_argument("--proxy-config", default=DEFAULT_PROXY_CONFIG,
                        help=f"user_proxy_model.json 路径（默认: {DEFAULT_PROXY_CONFIG}）")
    parser.add_argument("--mock-reply", default="good", choices=["good", "bad"],
                        help="模拟 agent 回复类型: good(数值正确) / bad(数值错误)")
    args = parser.parse_args()
    run_e2e(args.proxy_config, args.mock_reply)
