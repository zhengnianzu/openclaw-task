"""
批量生成 AutomationConfig JSON 配置文件

用法示例：
  python generate_configs.py \
    --input  standard_output \
    --output configs/generated \
    --skill-dir /mnt/c/Users/Administrator/Downloads/dataset/skill_localize/skills_library \
    --agent-dir agents \
    --simulator-config configs/user_proxy_model.json \
    --workspace /home/nianzuzheng/.openclaw/workspace \
    --model claude-3-5-sonnet \
    --gateway-ws-url ws://127.0.0.1:18789/gateway
"""

import argparse
import json
from pathlib import Path


def find_profile_file(folder: Path) -> str | None:
    """找到文件夹下 user_profile 开头的 JSON 文件，返回文件名"""
    for f in folder.iterdir():
        if f.name.startswith("user_profile") and f.suffix == ".json":
            return f.name
    return None


def generate_single_config(
    folder: Path,
    agent_index: int,
    query_index: int,
    query_text: str,
    skills: list,
    profile_file: str | None,
    skill_dir: str,
    agent_dir: str,
    simulator_config: str,
    workspace: str,
    model: str,
    gateway_ws_url: str,
    api_key: str | None,
) -> dict:
    """为单条 query 生成一份完整配置"""
    agent_name = f"assistant{agent_index}"

    return {
        "system": {
            "platform": ["linux"],
            "python": "3.12",
            "tools": []
        },
        "input_dir": {
            "skill_dir": skill_dir,
            "agent_dir": agent_dir,
            "user_dir": {
                "path": str(folder),
                "profile_file": profile_file
            }
        },
        "agents": [
            {
                "name": agent_name,
                "config": [],
                "skills": skills,
                "system_prompt": None,
                "model": model
            }
        ],
        "queries": [
            {
                "agent_name": agent_name,
                "text": query_text,
                "session_name": f"query{query_index}",
                "timeout": 600
            }
        ],
        "gateway_ws_url": gateway_ws_url,
        "api_key": api_key,
        "workspace_base": workspace,
        "simulator_config": simulator_config
    }


def main():
    parser = argparse.ArgumentParser(description="批量生成 AutomationConfig JSON 配置文件（每条 query 一个文件）")
    parser.add_argument("--input",  required=True, help="输入文件夹，包含各 profile 子目录")
    parser.add_argument("--output", required=True, help="输出配置文件夹")
    parser.add_argument("--skill-dir",        required=True,  help="技能库根目录")
    parser.add_argument("--agent-dir",        required=True,  help="Agent 源文件目录")
    parser.add_argument("--simulator-config", required=True,  help="Simulator 配置 JSON 绝对路径")
    parser.add_argument("--workspace",        required=True,  help="工作空间基础目录")
    parser.add_argument("--model",            required=True,  help="模型名称，如 claude-3-5-sonnet")
    parser.add_argument("--gateway-ws-url",   default="ws://127.0.0.1:18789/gateway", help="WebSocket 网关 URL")
    parser.add_argument("--api-key",          default=None,   help="API Key（可选）")
    args = parser.parse_args()

    input_dir  = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 按名称排序，保证编号稳定
    folders = sorted(
        [f for f in input_dir.iterdir() if f.is_dir() and (f / "user_queries.json").exists()]
    )

    if not folders:
        print(f"⚠ 未找到包含 user_queries.json 的子目录: {input_dir}")
        return

    print(f"📂 共发现 {len(folders)} 个有效文件夹，开始生成配置...")

    total = 0
    success = 0
    for folder_idx, folder in enumerate(folders, start=1):
        try:
            queries_data = json.loads((folder / "user_queries.json").read_text(encoding="utf-8"))
            skills: list = queries_data.get("skills", [])
            queries_list: list = queries_data.get("queries", [])
            profile_file = find_profile_file(folder)

            for q_idx, query_text in enumerate(queries_list, start=1):
                total += 1
                config = generate_single_config(
                    folder=folder,
                    agent_index=folder_idx,
                    query_index=q_idx,
                    query_text=query_text,
                    skills=skills,
                    profile_file=profile_file,
                    skill_dir=args.skill_dir,
                    agent_dir=args.agent_dir,
                    simulator_config=args.simulator_config,
                    workspace=args.workspace,
                    model=args.model,
                    gateway_ws_url=args.gateway_ws_url,
                    api_key=args.api_key,
                )
                out_file = output_dir / f"{folder.name}_q{q_idx}.json"
                out_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  ✓ {out_file.name}")
                success += 1

        except Exception as e:
            print(f"  ✗ [{folder_idx:03d}] {folder.name}: {e}")

    print(f"\n✅ 完成：{success}/{total} 个配置已写入 {output_dir}")


if __name__ == "__main__":
    main()
