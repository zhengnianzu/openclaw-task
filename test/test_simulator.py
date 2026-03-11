"""
测试 User_simulator

用法：
  cd /home/nianzuzheng/project/openclaw-task
  python test_simulator.py
  python test_simulator.py --proxy-config "周子敬_资深财务会计师 _ 审计主管/user_proxy_model.json"
  python test_simulator.py --rounds 3
"""

import argparse
import json
from pathlib import Path

from user_simulator import User_simulator

DEFAULT_PROXY_CONFIG = "周子敬_资深财务会计师 _ 审计主管/user_proxy_model.json"
DEFAULT_PROFILE      = "周子敬_资深财务会计师 _ 审计主管/profile_analyzed.json"
DEFAULT_QUERY        = "帮我分析一下我的个人财务状况，重点看下负债情况。"


def load_proxy_config(proxy_config_path: str) -> dict:
    path = Path(proxy_config_path)
    if not path.exists():
        raise FileNotFoundError(f"user_proxy_model 文件不存在: {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    print(f"✓ 加载 Simulator 配置: {path}")
    print(f"  model    = {cfg.get('model')}")
    print(f"  base_url = {cfg.get('base_url')}")
    print(f"  api_key  = {'***' + cfg['api_key'][-4:] if cfg.get('api_key') else '（未配置）'}")
    print(f"  proxy    = {cfg.get('proxy') or '（未配置）'}")
    return cfg


def load_profile(profile_path: str) -> str:
    path = Path(profile_path)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"✓ 加载用户画像: {path}")
        return json.dumps(data, ensure_ascii=False, indent=2)
    print(f"⚠ 用户画像文件不存在，使用空字符串: {path}")
    return ""


def test_single_turn(simulator: User_simulator, agent_message: str) -> str:
    """模拟 agent 发一条消息，获取 simulator 回复"""
    print(f"\n[Agent → Simulator]: {agent_message}")
    reply = simulator.chat(agent_message)
    print(f"[Simulator 回复]: {reply}")
    return reply


def run_test(proxy_config_path: str, profile_path: str, origin_query: str, rounds: int):
    print("=" * 60)
    print("User_simulator 测试")
    print("=" * 60)

    cfg = load_proxy_config(proxy_config_path)
    user_profile = load_profile(profile_path)

    simulator = User_simulator(
        origin_query=origin_query,
        user_profile=user_profile,
        model=cfg.get("model", "gpt-4o"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        proxy=cfg.get("proxy"),
    )

    print(f"\n📋 Origin Query: {origin_query}")
    print(f"🔄 模拟 {rounds} 轮对话\n")
    print("-" * 60)

    # 模拟 agent 的几条回复，测试 simulator 的响应
    agent_messages = [
        f"好的，我来帮你分析。请问你目前的主要负债有哪些？",
        "我看到你提到了房贷，能告诉我贷款金额和当前利率吗？",
        "根据你的信息，我已经计算出两种还款方案的差异。你还有其他问题吗？",
    ]

    for i, msg in enumerate(agent_messages[:rounds], 1):
        print(f"\n--- 第 {i} 轮 ---")
        reply = test_single_turn(simulator, msg)
        if "【Task_Done】" in reply:
            print("\n✅ Simulator 判定任务完成")
            break
        if "【Task_Failed】" in reply:
            print("\n❌ Simulator 判定任务失败")
            break

    print("\n" + "=" * 60)
    print(f"测试完成，共 {len(simulator.messages) // 2} 轮对话")
    print(f"Token 消耗记录已写入 api_use.log")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试 User_simulator")
    parser.add_argument("--proxy-config", default=DEFAULT_PROXY_CONFIG,
                        help=f"user_proxy_model.json 路径（默认: {DEFAULT_PROXY_CONFIG}）")
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help=f"用户画像 JSON 路径（默认: {DEFAULT_PROFILE}）")
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help="Origin query（默认内置测试任务）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="模拟对话轮数（默认: 3）")
    args = parser.parse_args()

    run_test(
        proxy_config_path=args.proxy_config,
        profile_path=args.profile,
        origin_query=args.query,
        rounds=args.rounds,
    )
