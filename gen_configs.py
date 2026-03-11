#!/usr/bin/env python3
"""
批量生成 openclaw 任务配置文件

从 user_queries.json 中读取 queries，每条生成一个独立的 config JSON，
输出到指定目录。

用法：
  python gen_configs.py <input_dir> <output_dir> [options]

示例：
  python gen_configs.py "周子敬_资深财务会计师 _ 审计主管" configs/zhouzijing/
  python gen_configs.py "周子敬_资深财务会计师 _ 审计主管" configs/zhouzijing/ \\
      --platform linux \\
      --agent-dir /home/nianzuzheng/project/openclaw-task/agents \\
      --workspace /home/nianzuzheng/.openclaw/workspace \\
      --gateway ws://127.0.0.1:18789/gateway
"""

import argparse
import json
from pathlib import Path
from datetime import datetime


def make_agent_name(index: int) -> str:
    """index 从 1 开始，第一个用 audit_assistant，其余加序号"""
    return "audit_assistant" if index == 1 else f"audit_assistant_{index:02d}"


def load_queries(input_dir: Path) -> list[str]:
    queries_file = input_dir / "user_queries.json"
    if not queries_file.exists():
        raise FileNotFoundError(f"user_queries.json 不存在: {queries_file}")
    data = json.loads(queries_file.read_text(encoding="utf-8"))
    queries = data.get("queries", [])
    if not queries:
        raise ValueError("user_queries.json 中 queries 为空")
    return queries


def build_config(
    input_dir: Path,
    query_text: str,
    agent_name: str,
    platform: list[str],
    profile_file: str,
    simulator_config: str,
    agent_dir: str,
    workspace: str,
    gateway_url: str,
    agent_model: str,
    timeout: int,
) -> dict:
    return {
        "system": {
            "platform": platform,
            "python": "3.11",
            "tools": []
        },
        "input_dir": {
            "skill_dir": {},
            "agent_dir": agent_dir,
            "user_dir": {
                "path": str(input_dir.resolve()),
                "map_file": "MAP_Linux" if "linux" in platform else "MAP_Windows",
                "profile_file": profile_file,
            }
        },
        "agents": [
            {
                "name": agent_name,
                "config": ["SOUL.md", "USER.md"],
                "skills": [],
                "system_prompt": None,
                "model": agent_model
            }
        ],
        "queries": [
            {
                "agent_name": agent_name,
                "text": query_text,
                "session_name": "main",
                "timeout": timeout
            }
        ],
        "gateway_ws_url": gateway_url,
        "api_key": None,
        "workspace_base": workspace,
        "simulator_config": simulator_config
    }


def gen_configs(
    input_dir: Path,
    output_dir: Path,
    platform: list[str],
    profile_file: str,
    proxy_file: str,
    agent_dir: str,
    workspace: str,
    gateway_url: str,
    agent_model: str,
    timeout: int,
    prefix: str,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    queries = load_queries(input_dir)

    # simulator_config 为绝对路径
    simulator_config = str((input_dir / proxy_file).resolve())

    print(f"📂 输入目录       : {input_dir.resolve()}")
    print(f"📂 输出目录       : {output_dir.resolve()}")
    print(f"🖥  平台           : {platform}")
    print(f"🤖 simulator_config: {simulator_config}")
    print(f"📋 queries 数     : {len(queries)}")
    print()

    for idx, query_text in enumerate(queries, 1):
        agent_name = make_agent_name(idx)
        filename = f"{prefix}_{idx:02d}.json"
        out_path = output_dir / filename

        cfg = build_config(
            input_dir=input_dir,
            query_text=query_text,
            agent_name=agent_name,
            platform=platform,
            profile_file=profile_file,
            simulator_config=simulator_config,
            agent_dir=agent_dir,
            workspace=workspace,
            gateway_url=gateway_url,
            agent_model=agent_model,
            timeout=timeout,
        )

        out_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ [{idx:02d}] {filename}  agent={agent_name}")
        print(f"       {query_text[:80]}{'...' if len(query_text) > 80 else ''}")

    print(f"\n✅ 生成完成，共 {len(queries)} 个配置文件 → {output_dir.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="根据 user_queries.json 批量生成 openclaw 配置文件"
    )
    parser.add_argument("input_dir",  help="用户文件夹路径（含 user_queries.json）")
    parser.add_argument("output_dir", help="输出 config 文件夹路径")

    parser.add_argument("--platform",
                        nargs="+", default=["linux"],
                        choices=["linux", "windows"],
                        help="目标平台（默认: linux）")
    parser.add_argument("--profile-file",  default="profile_analyzed.json",
                        help="用户画像文件名，相对于 input_dir（默认: profile_analyzed.json）")
    parser.add_argument("--proxy-file",    default="user_proxy_model.json",
                        help="Simulator 配置文件名，相对于 input_dir（默认: user_proxy_model.json）")
    parser.add_argument("--agent-dir",
                        default="/home/nianzuzheng/project/openclaw-task/agents",
                        help="agents 目录绝对路径")
    parser.add_argument("--workspace",
                        default="/home/nianzuzheng/.openclaw/workspace",
                        help="workspace_base 路径（默认: ~/.openclaw/workspace）")
    parser.add_argument("--gateway",
                        default="ws://127.0.0.1:18789/gateway",
                        help="OpenClaw gateway WebSocket URL")
    parser.add_argument("--agent-model",  default="claude-3-5-sonnet",
                        help="Agent 使用的模型（默认: claude-3-5-sonnet）")
    parser.add_argument("--timeout",      type=int, default=600,
                        help="每条 query 超时秒数（默认: 600）")
    parser.add_argument("--prefix",
                        help="输出文件名前缀，默认取 input_dir 文件夹名")

    args = parser.parse_args()

    input_dir  = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    prefix     = args.prefix or input_dir.name.replace(" ", "_")

    gen_configs(
        input_dir=input_dir,
        output_dir=output_dir,
        platform=args.platform,
        profile_file=args.profile_file,
        proxy_file=args.proxy_file,
        agent_dir=args.agent_dir,
        workspace=args.workspace,
        gateway_url=args.gateway,
        agent_model=args.agent_model,
        timeout=args.timeout,
        prefix=prefix,
    )
