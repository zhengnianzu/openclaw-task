"""6.2 受控对比:固定一条 6 轮轨迹,在 eval_step ∈ {1,2,3} 下用真实 evaluator 跑,
比较【触发轮 / 评估次数 / 时延 / 投喂规模(prompt_chars) / 评估效果】。

为何受控:真实简单任务 capable agent 1 轮即完成,eval_step 差异无从显现(见 #3.1)。
故用可控的多轮轨迹隔离 eval_step 单变量;evaluator 仍是真网关 + flash + 真 reset + 真压缩投喂。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

# 本脚本已移入变更目录,向上查找项目根(含 openclaw_automation.py 的目录)
ROOT = Path(__file__).resolve().parent
while not (ROOT / "openclaw_automation.py").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from utils.connection import build_openclaw_client  # noqa: E402
from trajectory import Trajectory, TurnRecord, ToolCallEvidence, FileEvidence  # noqa: E402
from evaluator import Evaluator, EvaluateConfig  # noqa: E402

S = json.loads((ROOT / "configs" / "config_session.json").read_text(encoding="utf-8"))
RUBRICS = [
    "calc.py 真实存在且实现了 add/sub/mul/div 四个函数",
    "test_calc.py 真实存在且覆盖四则运算正常用例与 div 除零异常",
    "README.md 真实存在且用中文说明各函数用途与至少一个调用示例",
]


def make_turn(n, content, tools, files):
    return TurnRecord(
        turn=n, user_input=f"u{n}", agent_content=content,
        tool_calls=[ToolCallEvidence(tool=t[0], input=t[1], output=t[2]) for t in tools],
        files=[FileEvidence(name=f[0], checked=True, exists=f[1], path=f"/ws/{f[0]}",
                            content=f[2] if f[1] else None) for f in files],
    )


# 6 轮:文件逐轮累积;turn3 声称"测试通过"但无证据(幻觉轮)
TURNS = [
    make_turn(1, "已创建 calc.py,实现了四个函数。",
              [("fs.write", "calc.py", "ok 200B")],
              [("calc.py", True, "def add(a,b):return a+b\ndef sub(a,b):return a-b\ndef mul(a,b):return a*b\ndef div(a,b):return a/b")]),
    make_turn(2, "已创建 test_calc.py。",
              [("fs.write", "test_calc.py", "ok 150B")],
              [("test_calc.py", True, "assert add(1,2)==3\nassert sub(5,3)==2\n# 缺除零用例")]),
    make_turn(3, "我已运行测试,全部通过,包含除零异常处理。",  # 声称但无证据
              [], []),
    make_turn(4, "已修复 div 的除零处理,并补充除零测试。",
              [("fs.write", "calc.py", "ok"), ("fs.write", "test_calc.py", "ok")],
              [("calc.py", True, "def div(a,b):\n    if b==0: raise ZeroDivisionError\n    return a/b"),
               ("test_calc.py", True, "import pytest\nwith pytest.raises(ZeroDivisionError): div(1,0)")]),
    make_turn(5, "已创建 README.md,中文说明四个函数及示例。",
              [("fs.write", "README.md", "ok 300B")],
              [("README.md", True, "# 计算器\n- add: 加法,示例 add(1,2)=3\n- sub/mul/div 同理")]),
    make_turn(6, "三个文件均已完成并通过测试。", [], []),
]


async def run_one(client, step):
    ev = Evaluator(
        EvaluateConfig(agent_name="evaluator", eval_step=step, rubrics=RUBRICS),
        client, run_id=f"SWEEP_s{step}", session_name=f"eval_sweep_s{step}",
    )
    tj = Trajectory(query="创建 calc.py / test_calc.py / README.md 三件套", agent_name="main3")
    fired, calls, total_t, prompt_chars, comps, incs = [], 0, 0.0, [], [], []
    for n in range(1, 7):
        tj.turns.append(TURNS[n - 1])
        if n % step != 0:
            continue  # 跳过轮:simulator 喂空
        window = step
        pc = len(ev._build_prompt(tj, RUBRICS, window))  # 投喂规模(token 代理)
        t0 = time.time()
        res = await ev.evaluate_turn(tj, TURNS[n - 1], rubric=RUBRICS, window=window)
        dt = time.time() - t0
        fired.append(n); calls += 1; total_t += dt; prompt_chars.append(pc)
        comps.append(res.completion if res else None)
        incs.append(res.inclination if res else "ERR")
    return {
        "eval_step": step, "fired_turns": fired, "calls": calls,
        "total_time_s": round(total_t, 1), "avg_time_s": round(total_t / max(calls, 1), 1),
        "avg_prompt_chars": round(sum(prompt_chars) / max(len(prompt_chars), 1)),
        "completions": comps, "inclinations": incs,
    }


async def main():
    client = await build_openclaw_client(
        gateway_ws_url=S.get("gateway_ws_url"), api_key=S.get("api_key"),
        gateway_timeout=S.get("gateway_timeout"),
    )
    async with client:
        await client.gateway.agents_update("evaluator", model="gemini-3-flash-preview")
        rows = []
        for step in (1, 2, 3):
            print(f"--- 跑 eval_step={step} ---", flush=True)
            rows.append(await run_one(client, step))
        print("\n===== 6.2 eval_step 对比 =====")
        print(f"{'step':<5}{'触发轮':<14}{'次数':<5}{'总时延s':<9}{'均时延s':<9}{'均投喂chars':<12}{'完成度序列':<18}{'倾向序列'}")
        for r in rows:
            print(f"{r['eval_step']:<5}{str(r['fired_turns']):<14}{r['calls']:<5}"
                  f"{r['total_time_s']:<9}{r['avg_time_s']:<9}{r['avg_prompt_chars']:<12}"
                  f"{str(r['completions']):<18}{r['inclinations']}")
        (ROOT / "eval_step_sweep_result.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n结果已写入 eval_step_sweep_result.json")


if __name__ == "__main__":
    asyncio.run(main())
