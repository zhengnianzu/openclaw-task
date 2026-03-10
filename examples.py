"""
示例使用脚本集合
"""

import asyncio
from openclaw_automation import (
    main,
    OpenClawAutomation,
    ConfigLoader,
    AutomationConfig,
    AgentConfigItem,
    QueryItem,
)


# ============================================================================
# 示例 1: 最简单的使用方式
# ============================================================================

async def example_1_simple_usage():
    """最简单的使用示例 - 从文件加载配置"""
    print("=" * 60)
    print("示例 1: 简单使用")
    print("=" * 60)

    await main(config_file="config_simple.json")


# ============================================================================
# 示例 2: 从字典创建配置
# ============================================================================

async def example_2_dict_config():
    """从 Python 字典创建配置"""
    print("=" * 60)
    print("示例 2: 字典配置")
    print("=" * 60)

    config_dict = {
        "agents": [
            {
                "name": "greeting_bot",
                "system_prompt": "You are a friendly greeting bot. Always be enthusiastic!"
            }
        ],
        "queries": [
            {
                "agent_name": "greeting_bot",
                "text": "Greet the user and introduce yourself"
            }
        ]
    }

    await main(config_dict=config_dict)


# ============================================================================
# 示例 3: 使用 Pydantic 模型
# ============================================================================

async def example_3_pydantic_models():
    """使用 Pydantic 模型构建配置"""
    print("=" * 60)
    print("示例 3: Pydantic 模型")
    print("=" * 60)

    config = AutomationConfig(
        agents=[
            AgentConfigItem(
                name="math_tutor",
                system_prompt="You are a patient math tutor. Explain concepts clearly with examples.",
                skills=[],
                config=[]
            )
        ],
        queries=[
            QueryItem(
                agent_name="math_tutor",
                text="Explain the Pythagorean theorem with a simple example",
                timeout=120
            )
        ],
        workspace_base="./example_workspaces"
    )

    automation = OpenClawAutomation(config)
    results = await automation.run()

    # 访问结果
    for key, result in results.items():
        print(f"\n{key}:")
        print(f"  Success: {result.success}")
        print(f"  Content: {result.content[:200]}...")


# ============================================================================
# 示例 4: 内容创作流水线
# ============================================================================

async def example_4_content_pipeline():
    """完整的内容创作流水线"""
    print("=" * 60)
    print("示例 4: 内容创作流水线")
    print("=" * 60)

    topic = "The Future of Renewable Energy"

    config = AutomationConfig(
        agents=[
            AgentConfigItem(
                name="topic_analyzer",
                system_prompt="You analyze topics and create content outlines."
            ),
            AgentConfigItem(
                name="content_writer",
                system_prompt="You write engaging, informative articles."
            ),
            AgentConfigItem(
                name="seo_optimizer",
                system_prompt="You optimize content for SEO."
            )
        ],
        queries=[
            QueryItem(
                agent_name="topic_analyzer",
                text=f"Create a detailed outline for an article about: {topic}"
            ),
            QueryItem(
                agent_name="content_writer",
                text="Write a full article (1000 words) based on this outline: {result_topic_analyzer}",
                timeout=600
            ),
            QueryItem(
                agent_name="seo_optimizer",
                text="Optimize this article for SEO: {result_content_writer}. Add meta description, keywords, and suggest improvements.",
                timeout=300
            )
        ]
    )

    automation = OpenClawAutomation(config)
    await automation.run()


# ============================================================================
# 示例 5: 数据分析流程
# ============================================================================

async def example_5_data_analysis():
    """数据分析工作流"""
    print("=" * 60)
    print("示例 5: 数据分析流程")
    print("=" * 60)

    data_description = """
    Sales data for Q4 2025:
    - January: $120,000
    - February: $135,000
    - March: $142,000
    """

    config = AutomationConfig(
        agents=[
            AgentConfigItem(
                name="data_cleaner",
                system_prompt="You clean and organize data."
            ),
            AgentConfigItem(
                name="data_analyst",
                system_prompt="You analyze data and find insights."
            ),
            AgentConfigItem(
                name="report_generator",
                system_prompt="You create executive summary reports."
            )
        ],
        queries=[
            QueryItem(
                agent_name="data_cleaner",
                text=f"Clean and structure this data: {data_description}"
            ),
            QueryItem(
                agent_name="data_analyst",
                text="Analyze this data and find trends, patterns, and insights: {result_data_cleaner}"
            ),
            QueryItem(
                agent_name="report_generator",
                text="Create an executive summary report based on: {result_data_analyst}"
            )
        ]
    )

    await main(config_dict=config.model_dump())


# ============================================================================
# 示例 6: 多语言翻译流程
# ============================================================================

async def example_6_translation_pipeline():
    """多语言翻译和本地化"""
    print("=" * 60)
    print("示例 6: 翻译流程")
    print("=" * 60)

    original_text = "Welcome to our application. This is a revolutionary new way to manage your tasks."

    config = AutomationConfig(
        agents=[
            AgentConfigItem(
                name="translator_cn",
                system_prompt="You are a professional translator specializing in English to Chinese translation."
            ),
            AgentConfigItem(
                name="translator_es",
                system_prompt="You are a professional translator specializing in English to Spanish translation."
            ),
            AgentConfigItem(
                name="localizer",
                system_prompt="You adapt content for local markets and cultures."
            )
        ],
        queries=[
            QueryItem(
                agent_name="translator_cn",
                text=f"Translate to Chinese (Simplified): {original_text}"
            ),
            QueryItem(
                agent_name="translator_es",
                text=f"Translate to Spanish: {original_text}"
            ),
            QueryItem(
                agent_name="localizer",
                text="Review these translations and suggest cultural adaptations:\nChinese: {result_translator_cn}\nSpanish: {result_translator_es}"
            )
        ]
    )

    automation = OpenClawAutomation(config)
    await automation.run()


# ============================================================================
# 示例 7: 错误处理和重试
# ============================================================================

async def example_7_error_handling():
    """展示错误处理"""
    print("=" * 60)
    print("示例 7: 错误处理")
    print("=" * 60)

    max_retries = 3

    for attempt in range(max_retries):
        try:
            config = AutomationConfig(
                agents=[
                    AgentConfigItem(
                        name="test_agent",
                        system_prompt="You are a test agent."
                    )
                ],
                queries=[
                    QueryItem(
                        agent_name="test_agent",
                        text="Say hello",
                        timeout=30
                    )
                ]
            )

            automation = OpenClawAutomation(config)
            results = await automation.run()

            print(f"✅ 成功完成（尝试 {attempt + 1}）")
            break

        except Exception as e:
            print(f"❌ 尝试 {attempt + 1} 失败: {e}")

            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 指数退避
                print(f"⏳ 等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
            else:
                print("❌ 达到最大重试次数")
                raise


# ============================================================================
# 示例 8: 自定义结果处理
# ============================================================================

async def example_8_custom_result_handling():
    """自定义结果处理"""
    print("=" * 60)
    print("示例 8: 自定义结果处理")
    print("=" * 60)

    config = AutomationConfig(
        agents=[
            AgentConfigItem(
                name="summarizer",
                system_prompt="You create concise summaries."
            )
        ],
        queries=[
            QueryItem(
                agent_name="summarizer",
                text="Summarize the importance of clean code in software development"
            )
        ]
    )

    automation = OpenClawAutomation(config)
    results = await automation.run()

    # 自定义处理结果
    for key, result in results.items():
        print(f"\n处理结果: {key}")

        # 保存到文件
        from pathlib import Path
        output_file = Path(f"output_{key}.txt")
        output_file.write_text(result.content, encoding="utf-8")
        print(f"  保存到: {output_file}")

        # 提取统计信息
        word_count = len(result.content.split())
        char_count = len(result.content)
        print(f"  字数: {word_count}")
        print(f"  字符数: {char_count}")
        print(f"  耗时: {result.latency_ms}ms")

        # 计算成本（假设）
        estimated_tokens = word_count * 1.3  # 粗略估算
        estimated_cost = estimated_tokens * 0.000003  # 假设价格
        print(f"  估算成本: ${estimated_cost:.6f}")


# ============================================================================
# 示例 9: 并行执行独立任务
# ============================================================================

async def example_9_parallel_execution():
    """并行执行独立的任务"""
    print("=" * 60)
    print("示例 9: 并行执行")
    print("=" * 60)

    # 创建多个独立的自动化任务
    tasks = []

    # 任务 1: 生成故事
    config1 = AutomationConfig(
        agents=[AgentConfigItem(name="storyteller", system_prompt="You write short stories.")],
        queries=[QueryItem(agent_name="storyteller", text="Write a short story about a robot learning to paint")],
        workspace_base="./workspace_story"
    )

    # 任务 2: 生成诗歌
    config2 = AutomationConfig(
        agents=[AgentConfigItem(name="poet", system_prompt="You write poetry.")],
        queries=[QueryItem(agent_name="poet", text="Write a haiku about autumn")],
        workspace_base="./workspace_poetry"
    )

    # 任务 3: 生成笑话
    config3 = AutomationConfig(
        agents=[AgentConfigItem(name="comedian", system_prompt="You tell jokes.")],
        queries=[QueryItem(agent_name="comedian", text="Tell a programming joke")],
        workspace_base="./workspace_comedy"
    )

    # 并行执行
    async def run_task(config, task_name):
        print(f"  开始任务: {task_name}")
        automation = OpenClawAutomation(config)
        result = await automation.run()
        print(f"  完成任务: {task_name}")
        return result

    results = await asyncio.gather(
        run_task(config1, "故事生成"),
        run_task(config2, "诗歌创作"),
        run_task(config3, "笑话生成"),
        return_exceptions=True
    )

    print(f"\n完成 {len(results)} 个并行任务")


# ============================================================================
# 示例 10: 环境变量配置
# ============================================================================

async def example_10_environment_config():
    """使用环境变量配置"""
    print("=" * 60)
    print("示例 10: 环境变量配置")
    print("=" * 60)

    import os

    # 设置环境变量
    os.environ["OPENCLAW_GATEWAY_WS_URL"] = "ws://127.0.0.1:18789/gateway"
    # os.environ["OPENCLAW_API_KEY"] = "your_api_key_here"

    config = AutomationConfig(
        agents=[
            AgentConfigItem(
                name="env_test",
                system_prompt="You are a test agent."
            )
        ],
        queries=[
            QueryItem(
                agent_name="env_test",
                text="Confirm you are connected"
            )
        ],
        # 从环境变量读取（如果配置中没有设置，SDK 会自动从环境变量读取）
        gateway_ws_url=os.getenv("OPENCLAW_GATEWAY_WS_URL"),
        api_key=os.getenv("OPENCLAW_API_KEY")
    )

    automation = OpenClawAutomation(config)
    await automation.run()


# ============================================================================
# 运行所有示例
# ============================================================================

async def run_all_examples():
    """运行所有示例"""
    examples = [
        ("简单使用", example_1_simple_usage),
        ("字典配置", example_2_dict_config),
        ("Pydantic 模型", example_3_pydantic_models),
        ("内容创作", example_4_content_pipeline),
        ("数据分析", example_5_data_analysis),
        ("翻译流程", example_6_translation_pipeline),
        ("错误处理", example_7_error_handling),
        ("结果处理", example_8_custom_result_handling),
        ("并行执行", example_9_parallel_execution),
        ("环境变量", example_10_environment_config),
    ]

    for name, example_func in examples:
        print(f"\n\n{'=' * 60}")
        print(f"运行示例: {name}")
        print(f"{'=' * 60}\n")

        try:
            await example_func()
            print(f"\n✅ {name} 完成")
        except Exception as e:
            print(f"\n❌ {name} 失败: {e}")

        # 等待一下再运行下一个示例
        await asyncio.sleep(1)


# ============================================================================
# 主入口
# ============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # 运行指定的示例
        example_number = sys.argv[1]
        example_map = {
            "1": example_1_simple_usage,
            "2": example_2_dict_config,
            "3": example_3_pydantic_models,
            "4": example_4_content_pipeline,
            "5": example_5_data_analysis,
            "6": example_6_translation_pipeline,
            "7": example_7_error_handling,
            "8": example_8_custom_result_handling,
            "9": example_9_parallel_execution,
            "10": example_10_environment_config,
        }

        if example_number in example_map:
            asyncio.run(example_map[example_number]())
        else:
            print(f"未知示例编号: {example_number}")
            print("可用示例: 1-10")
    else:
        # 运行所有示例
        print("运行所有示例...")
        print("提示: 使用 'python examples.py <编号>' 运行单个示例")
        asyncio.run(run_all_examples())
