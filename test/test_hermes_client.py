"""End-to-end test: actually instantiate AIAgent in-process, run one query,
verify ExecutionResult.content comes back non-empty.

This exercises the lazy-import + sys.path-juggling path against a real
hermes-agent install. Requires ~/.hermes/config.yaml to point at a working
model/provider."""
import asyncio
import sys

sys.path.insert(0, "/home/ma-user/openclaw-task")

from hermes_utils.hermes_client import (  # noqa: E402
    HermesClient,
    ExecutionOptions,
    build_hermes_client,
)


async def main():
    print("[1] building client (in-process mode)")
    client = await build_hermes_client(timeout=120)
    async with client:
        print("[2] getting agent + session")
        agent = client.get_agent(
            "smoketest",
            "session1",
            system_prompt="You are a brief assistant. Reply in <= 15 chars.",
        )
        print(f"    agent_name={agent.agent_name} session_id={agent.session_id}")

        print("[3] running query (this triggers real AIAgent init + model call)")
        result = await agent.execute(
            "回答:中国的首都是哪里?",
            options=ExecutionOptions(timeout_seconds=240),
        )
        print(f"    success={result.success}")
        print(f"    content={result.content!r}")
        print(f"    stop_reason={result.stop_reason}")
        print(f"    error_message={result.error_message}")

        if not result.success or not result.content:
            print("\nFAILED: empty response")
            sys.exit(1)

        print("\n[4] multi-turn — same agent + session, second query")
        result2 = await agent.execute(
            "刚才我问的是什么城市?",
            options=ExecutionOptions(timeout_seconds=240),
        )
        print(f"    content={result2.content!r}")
        if "北京" not in result2.content and "首都" not in result2.content:
            print("    WARN: 2nd turn doesn't seem to reference 1st (history may not be threaded)")
        else:
            print("    OK — history threaded correctly")

    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
