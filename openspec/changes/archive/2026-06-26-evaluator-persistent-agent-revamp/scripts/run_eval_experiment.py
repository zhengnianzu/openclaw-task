"""6.2 实验:用复杂多轮任务,在指定 eval_step 下跑一次完整闭环。

用法:python openspec/changes/evaluator-persistent-agent-revamp/scripts/run_eval_experiment.py <eval_step>
每次进程独立(run_id 唯一),便于按 run_id 切片 evaluator_use.log。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

# 本脚本已移入变更目录,向上查找项目根(含 openclaw_automation.py 的目录)
HERE = Path(__file__).resolve().parent
ROOT = HERE
while not (ROOT / "openclaw_automation.py").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from openclaw_automation import main  # noqa: E402

step = int(sys.argv[1]) if len(sys.argv) > 1 else 1
# 实验配置与本脚本同目录
cfg = json.loads((HERE / "config_eval_exp.json").read_text(encoding="utf-8"))
cfg["queries"][0]["evaluate"]["eval_step"] = step

print(f"[EXP] 开始 eval_step={step}")
t0 = time.time()
asyncio.run(main(config_dict=cfg))
print(f"[EXP] eval_step={step} 完成,用时 {time.time() - t0:.1f}s")
