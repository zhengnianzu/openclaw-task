import asyncio
from openclaw_sdk import OpenClawClient

async def main():
    async with await OpenClawClient.connect() as client:
        # 先列出所有可用 agent
        agents = await client.list_agents()
        if not agents:
            print("gateway 上没有任何已注册的 agent，请先通过 OpenClaw 创建 agent")
            return

        print("可用 agent 列表：")
        for a in agents:
            print(f"  - {a.agent_id}")

        # 用第一个可用的 agent 做测试
        agent_id = agents[0].agent_id
        print(f"\n使用 agent: {agent_id}")

        agent = client.get_agent(agent_id)
        result = await agent.execute("hi")
        print(result.content)

asyncio.run(main())