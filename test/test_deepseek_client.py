"""DeepSeek Harness SDK 适配层的离线回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness_automation import HarnessAutomation
from src.config import ConfigLoader
from src.dsh_client import (
    DeepSeekAgentManager,
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekHarnessError,
    DeepSeekWorkspaceManager,
    ExecutionOptions,
    ExecutionResult,
    _extract_usage,
    build_deepseek_client,
    execute_deepseek,
)
from src.deepseek_stream_bridge import DeepSeekNonstreamBridge


CORDIS_PATH = PROJECT_ROOT / "src" / "deepseek_harness.cordis.yml"


def _events(*, error: bool = False):
    reason = (
        {"kind": "error", "message": "provider failed"}
        if error
        else {"kind": "completed"}
    )
    return [
        {
            "type": "assistant/message",
            "data": {
                "usage": {"inputTokens": 10, "outputTokens": 2},
                "message": {"content": [{"type": "text", "text": "done"}]},
            },
        },
        {
            "type": "assistant/message",
            "data": {
                "usage": {"inputTokens": 4, "outputTokens": 1},
                "message": {"content": [{"type": "text", "text": "final"}]},
            },
        },
        {"type": "turn/end", "data": {"reason": reason}},
    ]


class _FakeHarness:
    def __init__(self, kwargs, *, block=False, result_factory=None):
        self.kwargs = kwargs
        self.block = block
        self.result_factory = result_factory
        self.runs = []
        self.closed = False
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, query, *, session_id):
        self.runs.append((query, session_id))
        self.started.set()
        if self.block:
            self.release.wait(timeout=5)
            if self.closed:
                raise RuntimeError("runtime closed")
        if self.result_factory:
            return self.result_factory(query, session_id)
        return SimpleNamespace(
            session_id=session_id,
            final_response=query,
            finish_reason="completed",
            events=_events(),
        )

    def close(self):
        self.closed = True
        self.release.set()


class _HarnessFactory:
    def __init__(self, *, block=False, result_factory=None):
        self.block = block
        self.result_factory = result_factory
        self.calls = []
        self.instances = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        harness = _FakeHarness(
            kwargs,
            block=self.block,
            result_factory=self.result_factory,
        )
        self.instances.append(harness)
        return harness


def _client(root: Path, factory: _HarnessFactory) -> DeepSeekClient:
    return DeepSeekClient(
        session_root=root / "session-logs",
        cordis_path=CORDIS_PATH,
        harness_factory=factory,
    )


def _register(
    client: DeepSeekClient,
    workspace: Path,
    manager: DeepSeekWorkspaceManager | None = None,
):
    client.workspace_manager = manager
    client.register_agent_defaults(
        "agent",
        system_prompt="只输出结论",
        model="deepseek-v4-flash",
        model_provider="deepseek-official",
        cwd=workspace,
    )


class DeepSeekLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_runtime_reuse_and_workspace_isolation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            factory = _HarnessFactory()
            manager = DeepSeekWorkspaceManager(str(root / "workspaces"))
            workspace = manager.get_agent_template_workspace("agent")
            (workspace / "INPUT.md").write_text("input", encoding="utf-8")
            skill = workspace / ".agents/skills/demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("skill", encoding="utf-8")
            client = _client(root, factory)
            _register(client, workspace, manager)

            async with client:
                main = client.get_agent("agent", "main/session")
                self.assertIs(main, client.get_agent("agent", "main/session"))
                other = client.get_agent("agent", "other")
                first = await main.execute("first")
                second = await main.execute("second")
                third = await other.execute("other")
                fourth = await main.execute("back")

            self.assertEqual(
                [first.content, second.content, third.content, fourth.content],
                ["first", "second", "other", "back"],
            )
            self.assertEqual(len(factory.instances), 2)
            self.assertEqual(
                [query for query, _ in factory.instances[0].runs],
                ["first", "second", "back"],
            )
            self.assertTrue(all(item.closed for item in factory.instances))

            main_call, other_call = factory.calls
            self.assertEqual(main_call["provider"], "deepseek-official")
            self.assertEqual(main_call["model"], "deepseek-v4-flash")
            self.assertEqual(
                main_call["env"]["DSH_SYSTEM_PROMPT"], "只输出结论"
            )
            self.assertEqual(Path(main_call["cordis"]), CORDIS_PATH)
            self.assertNotEqual(main_call["cwd"], other_call["cwd"])
            main_cwd = Path(main_call["cwd"])
            self.assertTrue((main_cwd / "INPUT.md").is_file())
            self.assertTrue(
                (main_cwd / ".agents/skills/demo/SKILL.md").is_file()
            )
            self.assertEqual(manager.get_agent_workspace("agent"), main_cwd)
            self.assertEqual(first.stop_reason, "complete")
            self.assertEqual(first.usage, {"inputTokens": 14, "outputTokens": 3})

    async def test_error_finish_reason_preserves_runtime_message(self):
        def result_factory(_query, session_id):
            return SimpleNamespace(
                session_id=session_id,
                final_response="partial",
                finish_reason="error",
                events=_events(error=True),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            factory = _HarnessFactory(result_factory=result_factory)
            workspace = root / "agent"
            workspace.mkdir()
            client = _client(root, factory)
            _register(client, workspace)
            async with client:
                result = await client.get_agent("agent", "main").execute("query")

            self.assertFalse(result.success)
            self.assertEqual(result.stop_reason, "error")
            self.assertEqual(result.error_message, "provider failed")

    async def test_timeout_closes_runtime_without_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            factory = _HarnessFactory(block=True)
            workspace = root / "agent"
            workspace.mkdir()
            client = _client(root, factory)
            _register(client, workspace)

            async with client:
                result = await client.get_agent("agent", "main").execute(
                    "slow", ExecutionOptions(timeout_seconds=0.01)
                )

            self.assertFalse(result.success)
            self.assertEqual(result.stop_reason, "timeout")
            self.assertEqual(len(factory.instances), 1)
            self.assertEqual(len(factory.instances[0].runs), 1)
            self.assertTrue(factory.instances[0].closed)

    async def test_cancellation_closes_runtime_and_propagates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            factory = _HarnessFactory(block=True)
            workspace = root / "agent"
            workspace.mkdir()
            client = _client(root, factory)
            _register(client, workspace)

            async with client:
                task = asyncio.create_task(
                    client.get_agent("agent", "main").execute("slow")
                )
                while not factory.instances:
                    await asyncio.sleep(0)
                await asyncio.to_thread(factory.instances[0].started.wait, 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertTrue(factory.instances[0].closed)

    async def test_execute_wrapper_does_not_replay_failure(self):
        agent = SimpleNamespace(
            execute=AsyncMock(
                return_value=ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message="failed",
                )
            )
        )
        with self.assertRaisesRegex(DeepSeekHarnessError, "failed"):
            await execute_deepseek(agent, "query", None)
        agent.execute.assert_awaited_once()

    async def test_execute_wrapper_marks_partial_result_incomplete(self):
        agent = SimpleNamespace(
            execute=AsyncMock(
                return_value=ExecutionResult(
                    success=True,
                    content="partial",
                    stop_reason="max-tokens",
                )
            )
        )
        _, incomplete = await execute_deepseek(agent, "query", None)
        self.assertTrue(incomplete)


class DeepSeekEventTests(unittest.TestCase):
    def test_usage_is_extracted_from_session_events(self):
        self.assertEqual(
            _extract_usage(_events()),
            {"inputTokens": 14, "outputTokens": 3},
        )


class DeepSeekWorkspaceAndConfigTests(unittest.TestCase):
    def test_config_uses_official_provider_fields_without_conversion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yml"
            config_path.write_text(
                """
model: deepseek-v4-flash
provider: primary-gateway
tools: false
nonstream: [primary-gateway]
providers:
  primary-gateway:
    displayName: Primary
    api: openai-completions
    baseURL: https://primary.example/v1
    headers:
      Authorization: Bearer primary-secret
    models:
      - id: deepseek-v4-flash
        contextWindow: 128000
        maxTokens: 8192
  backup-gateway:
    api: openai-completions
    baseURL: https://backup.example/v1
    headers:
      Authorization: Bearer backup-secret
    models:
      - id: deepseek-v4-flash
""".strip(),
                encoding="utf-8",
            )

            client = asyncio.run(
                build_deepseek_client(
                    session_root=temp_dir,
                    cordis_path=str(CORDIS_PATH),
                    config_path=str(config_path),
                )
            )

            self.assertEqual(client.config.model, "deepseek-v4-flash")
            self.assertEqual(client.config.provider, "primary-gateway")
            self.assertFalse(client.config.tools)
            self.assertEqual(client.config.nonstream, ["primary-gateway"])
            self.assertEqual(
                set(client.config.providers),
                {"primary-gateway", "backup-gateway"},
            )
            self.assertEqual(
                client.config.providers["primary-gateway"]["models"][0][
                    "contextWindow"
                ],
                128000,
            )
            self.assertEqual(
                client.config.providers["primary-gateway"]["headers"][
                    "Authorization"
                ],
                "Bearer primary-secret",
            )
            self.assertEqual(
                client.config.providers["backup-gateway"]["headers"][
                    "Authorization"
                ],
                "Bearer backup-secret",
            )

    def test_file_defaults_and_provider_config_reach_sdk_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = DeepSeekConfig(
                model="model-a",
                provider="primary-gateway",
                tools=False,
                nonstream=["primary-gateway"],
                providers={
                    "primary-gateway": {
                        "api": "openai-completions",
                        "baseURL": "https://primary.example/v1",
                        "headers": {
                            "Authorization": "Bearer primary-secret"
                        },
                        "models": [{"id": "model-a"}],
                    }
                },
            )
            factory = _HarnessFactory()
            client = DeepSeekClient(
                session_root=root / "session-logs",
                cordis_path=CORDIS_PATH,
                harness_factory=factory,
                config=config,
            )
            workspace_manager = DeepSeekWorkspaceManager(str(root / "workspaces"))
            agent_manager = DeepSeekAgentManager(client, workspace_manager)
            agent_config = SimpleNamespace(
                name="main", model=None, system_prompt="system"
            )

            async def run():
                await agent_manager.setup_agent(agent_config)
                async with client:
                    await client.get_agent("main", "session").execute("query")

            asyncio.run(run())
            call = factory.calls[0]
            self.assertEqual(call["provider"], "primary-gateway")
            self.assertEqual(call["model"], "model-a")
            self.assertEqual(call["env"]["DSH_HARNESS_TOOLS_ENABLED"], "0")
            self.assertEqual(call["env"]["DSH_SYSTEM_PROMPT"], "system")
            routed = json.loads(call["env"]["DSH_LLM_PI_AI_PROVIDERS"])
            self.assertRegex(
                routed["primary-gateway"]["baseURL"],
                r"^http://127\.0\.0\.1:\d+/primary-gateway$",
            )
            self.assertEqual(
                routed["primary-gateway"]["headers"]["Authorization"],
                "Bearer primary-secret",
            )

    def test_nonstream_bridge_converts_completion_to_standard_sse(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length)))
                body = json.dumps(
                    {
                        "id": "completion-1",
                        "created": 1,
                        "model": "model-a",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "done",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()

        async def run():
            bridge = DeepSeekNonstreamBridge(
                {"gateway": f"http://127.0.0.1:{upstream.server_port}/v1"}
            )
            await bridge.start()
            try:
                response = await asyncio.to_thread(
                    requests.post,
                    bridge.base_url("gateway") + "/chat/completions",
                    json={
                        "model": "model-a",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                    timeout=5,
                )
                return response
            finally:
                await bridge.close()

        try:
            response = asyncio.run(run())
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

        self.assertEqual(response.status_code, 200)
        self.assertIn('"content":"done"', response.text)
        self.assertIn("data: [DONE]", response.text)
        self.assertFalse(received[0]["stream"])
        self.assertNotIn("stream_options", received[0])

    def test_agent_files_and_skills_use_native_project_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent_source = root / "agent-source"
            skill_source = root / "skills/demo-skill"
            agent_source.mkdir()
            skill_source.mkdir(parents=True)
            (agent_source / "AGENTS.md").write_text("agent", encoding="utf-8")
            (skill_source / "SKILL.md").write_text("skill", encoding="utf-8")
            manager = DeepSeekWorkspaceManager(str(root / "workspaces"))

            manager.setup_agent_files(
                agent_name="demo",
                config_files=["AGENTS.md"],
                skill_base_dir=str(root / "skills"),
                agent_skills=["demo-skill"],
                agent_dir=str(agent_source),
            )

            workspace = manager.get_agent_template_workspace("demo")
            self.assertTrue((workspace / "AGENTS.md").is_file())
            self.assertTrue(
                (workspace / ".agents/skills/demo-skill/SKILL.md").is_file()
            )

    def test_agent_manager_uses_task_provider_and_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = _client(root, _HarnessFactory())
            manager = DeepSeekWorkspaceManager(str(root / "workspaces"))
            agent_manager = DeepSeekAgentManager(client, manager)
            config = SimpleNamespace(
                name="main",
                model="primary-gateway/deepseek-v4-pro",
                system_prompt="system",
            )

            asyncio.run(agent_manager.setup_agent(config))

            defaults = client._agent_defaults["main"]
            self.assertEqual(defaults.model, "deepseek-v4-pro")
            self.assertEqual(defaults.model_provider, "primary-gateway")

    def test_shared_configs_route_to_deepseek_workspace(self):
        config_names = (
            "config_simple.json",
            "config_user.json",
            "config_simple_eval.json",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "deepseek-workspace"
            with (
                patch.dict(
                    os.environ,
                    {"DEEPSEEK_HARNESS_WORKSPACE": str(workspace_root)},
                ),
                patch(
                    "src.dsh_client.DeepSeekWorkspaceManager"
                ) as manager_class,
            ):
                workspace_manager = manager_class.return_value
                for config_name in config_names:
                    with self.subTest(config=config_name):
                        config = ConfigLoader.load_from_file(
                            str(PROJECT_ROOT / "configs" / config_name)
                        )
                        config.harness_type = "deepseek"
                        automation = HarnessAutomation(config)
                        manager_class.assert_called_once_with(
                            str(workspace_root.resolve())
                        )
                        self.assertIs(
                            automation.workspace_manager, workspace_manager
                        )
                        manager_class.reset_mock()

    def test_cordis_enables_system_prompt_and_skills(self):
        content = CORDIS_PATH.read_text(encoding="utf-8")
        self.assertIn("DSH_SYSTEM_PROMPT", content)
        self.assertIn("DSH_HARNESS_TOOLS_ENABLED", content)
        self.assertIn("disabled: !!js", content)
        self.assertIn("toolJobs: !!js", content)
        self.assertIn("@deepseek-ai/dsh-llm-pi-ai", content)
        self.assertIn("DSH_LLM_PI_AI_PROVIDERS", content)


if __name__ == "__main__":
    unittest.main()
