"""
openclaw-task 示例测试

测试 SDK 连接和配置
"""

import asyncio
import json
import sys
import os

# 添加项目路径(test/ 的上一级为项目根)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def show_project_info():
    """显示项目信息"""
    print("""
    openclaw-task — OpenClaw 自动化任务系统

    功能: 配置驱动的 OpenClaw Agent 任务编排框架

    核心特性:
      - JSON 配置定义任务
      - 多 Agent 协作 (顺序/并行)
      - 工作空间管理
      - User Simulator 多轮对话

    运行前需要:
      1. OpenClaw Gateway 运行在 ws://127.0.0.1:18789
      2. 安装依赖: pip install -r requirements.txt

    配置文件示例 (configs/config_simple.json):
      {
        "agents": [
          {"name": "bot", "system_prompt": "You are helpful."}
        ],
        "queries": [
          {"agent_name": "bot", "text": "Hello"}
        ]
      }

    运行命令:
      python openclaw_automation.py configs/config_simple.json
    """)


def test_imports():
    """测试模块导入"""
    print("=" * 60)
    print("测试模块导入")
    print("=" * 60)

    modules = [
        ("openclaw_sdk", "OpenClaw SDK"),
        ("pydantic", "Pydantic"),
        ("aiohttp", "aiohttp"),
        ("utils.connection", "连接工具"),
        ("user_simulator", "用户模拟器"),
    ]

    success = 0
    failed = 0

    for module_name, desc in modules:
        try:
            __import__(module_name)
            print(f"  [OK] {desc} ({module_name})")
            success += 1
        except ImportError as e:
            print(f"  [FAIL] {desc} ({module_name}): {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {desc} ({module_name}): {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"结果: {success} 成功, {failed} 失败")
    return failed == 0


def test_config_loading():
    """测试配置文件加载"""
    print()
    print("=" * 60)
    print("测试配置文件加载")
    print("=" * 60)

    config_dir = os.path.join(project_root, "configs")

    if not os.path.exists(config_dir):
        print(f"  [WARN] configs 目录不存在")
        return False

    config_files = [f for f in os.listdir(config_dir) if f.endswith(".json")]

    if not config_files:
        print(f"  [WARN] configs 目录中没有 JSON 配置文件")
        return False

    print(f"  找到 {len(config_files)} 个配置文件:")
    for f in config_files:
        config_path = os.path.join(config_dir, f)
        try:
            with open(config_path, "r", encoding="utf-8") as fp:
                config = json.load(fp)
            agents = len(config.get("agents", []))
            queries = len(config.get("queries", []))
            print(f"    [OK] {f}: {agents} agents, {queries} queries")
        except Exception as e:
            print(f"    [FAIL] {f}: {e}")

    return True


async def test_gateway_connection():
    """测试 Gateway 连接"""
    print()
    print("=" * 60)
    print("测试 Gateway 连接")
    print("=" * 60)

    try:
        from openclaw_sdk import OpenClawClient
        from utils.connection import check_http_health

        # 尝试连接 Gateway
        gateway_url = "http://127.0.0.1:18789"

        print(f"  正在连接 {gateway_url}...")

        liveness_ok, readiness_ok, body = await check_http_health(gateway_url)

        if liveness_ok:
            print(f"  [OK] Gateway 存活检查通过")
        else:
            print(f"  [WARN] Gateway 存活检查失败 (body: {body})")

        if readiness_ok:
            print(f"  [OK] Gateway 就绪检查通过")
            return True
        else:
            print(f"  [INFO] Gateway 未就绪，可能还在启动中")
            print(f"        如果 Gateway 未运行，请先启动 OpenClaw")
            return False

    except ImportError as e:
        print(f"  [FAIL] 导入失败: {e}")
        return False
    except ConnectionRefusedError:
        print(f"  [FAIL] 连接被拒绝 - Gateway 可能未运行")
        print(f"        请先启动 OpenClaw Gateway")
        return False
    except Exception as e:
        print(f"  [ERROR] 连接测试失败: {type(e).__name__}: {e}")
        return False


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("  openclaw-task 测试程序")
    print("=" * 60 + "\n")

    show_project_info()

    test1_ok = test_imports()
    test2_ok = test_config_loading()

    # 异步测试 Gateway
    try:
        test3_ok = asyncio.run(test_gateway_connection())
    except KeyboardInterrupt:
        print("\n  [INFO] 测试被中断")
        test3_ok = False

    print()
    print("=" * 60)
    print("  测试结果汇总")
    print("=" * 60)
    print(f"    模块导入:    {'通过' if test1_ok else '失败'}")
    print(f"    配置加载:    {'通过' if test2_ok else '失败'}")
    print(f"    Gateway连接: {'通过' if test3_ok else '未通过(可能Gateway未运行)'}")
    print("=" * 60)

    return test1_ok and test2_ok


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
