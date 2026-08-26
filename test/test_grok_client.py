"""Grok Build headless harness 的核心协议和配置回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness_automation import HarnessAutomation
from src.config import AgentModelConfig, ConfigLoader
from src.grok_client import (
    ExecutionOptions,
    ExecutionResult,
    GrokAgentManager,
    GrokClient,
    GrokHarnessError,
    GrokWorkspaceManager,
    _parse_headless_output,
    build_grok_client,
    make_grok_execute_with_retry,
)


def _json_result(text: str, session_id: str, stop_reason: str = "end_turn"):
    return json.dumps(
        {
            "text": text,
            "stopReason": stop_reason,
            "sessionId": session_id,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    ).encode("utf-8")


class _FakeProcess:
    _next_pid = 10000

    def __init__(self, stdout=b"", stderr=b"", returncode=0, block=False):
        self._stdout = stdout
        self._stderr = stderr
        self._final_returncode = returncode
        self._block = block
        self._never = asyncio.Event()
        self.returncode = None
        self.pid = type(self)._next_pid
        type(self)._next_pid += 1
        self.kill_calls = 0
        self.terminate_calls = 0

    async def communicate(self):
        if self._block:
            await self._never.wait()
        self.returncode = self._final_returncode
        return self._stdout, self._stderr

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15

    async def wait(self):
        return self.returncode


def _register(
    client: GrokClient,
    workspace: Path,
    *,
    manager: GrokWorkspaceManager | None = None,
):
    client.workspace_manager = manager
    client.register_agent_defaults(
        "agent",
        system_prompt="be brief",
        model="test-alias",
        model_provider=None,
        cwd=workspace,
    )


class GrokHeadlessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_client_does_not_install_or_write_grok_home(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "grok-home"
            client = await build_grok_client(str(home))
            self.assertEqual(client.command, "grok")
            self.assertEqual(client.grok_home, home.resolve())
            self.assertFalse(home.exists())

    async def test_client_only_checks_preinstalled_command_on_enter(self):
        client = await build_grok_client()
        with (
            patch("src.grok_client.shutil.which", return_value="grok-test"),
            patch(
                "src.grok_client.asyncio.create_subprocess_exec"
            ) as spawn,
        ):
            entered = await client.__aenter__()

        self.assertIs(entered, client)
        self.assertEqual(client.command, "grok-test")
        spawn.assert_not_called()

    async def test_client_requires_preinstalled_grok_on_enter(self):
        client = await build_grok_client()
        with patch("src.grok_client.shutil.which", return_value=None):
            with self.assertRaisesRegex(GrokHarnessError, "预装"):
                await client.__aenter__()

    async def test_client_resolves_relative_executable(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            executable = Path(temp_dir) / "grok-test"
            executable.write_text("test", encoding="utf-8")
            client = GrokClient(
                command=os.path.relpath(executable, start=Path.cwd())
            )
            with patch("src.grok_client.shutil.which", return_value=None):
                await client.__aenter__()
            self.assertEqual(client.command, str(executable.resolve()))

    async def test_session_resume_and_workspace_isolation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = GrokWorkspaceManager(str(root / "workspaces"))
            workspace = manager.get_agent_template_workspace("agent")
            (workspace / "INPUT.md").write_text("input", encoding="utf-8")

            processes = [
                _FakeProcess(_json_result("first", "session-1")),
                _FakeProcess(_json_result("second", "session-1")),
                _FakeProcess(_json_result("other", "session-2")),
                _FakeProcess(_json_result("back", "session-1")),
            ]
            calls = []

            async def fake_spawn(*args, **kwargs):
                prompt_index = args.index("--prompt-file") + 1
                prompt_path = Path(args[prompt_index])
                calls.append(
                    {
                        "args": args,
                        "kwargs": kwargs,
                        "prompt_path": prompt_path,
                        "prompt": prompt_path.read_text(encoding="utf-8"),
                    }
                )
                return processes.pop(0)

            client = GrokClient("grok-test", root / "grok-home")
            _register(client, workspace, manager=manager)
            main = client.get_agent("agent", "main/session")
            other = client.get_agent("agent", "other")

            with patch(
                "src.grok_client.asyncio.create_subprocess_exec", fake_spawn
            ):
                first = await main.execute("first query")
                second = await main.execute("second query")
                third = await other.execute("other query")
                fourth = await main.execute("back query")

            self.assertEqual(
                [first.content, second.content, third.content, fourth.content],
                ["first", "second", "other", "back"],
            )
            self.assertEqual(first.stop_reason, "complete")
            self.assertEqual(first.usage["input_tokens"], 10)
            self.assertEqual(
                [call["prompt"] for call in calls],
                ["first query", "second query", "other query", "back query"],
            )
            self.assertTrue(
                all(not call["prompt_path"].exists() for call in calls)
            )

            first_args = calls[0]["args"]
            second_args = calls[1]["args"]
            other_args = calls[2]["args"]
            self.assertIn("--always-approve", first_args)
            self.assertIn("--no-memory", first_args)
            self.assertIn("--model", first_args)
            self.assertIn("--rules", first_args)
            self.assertNotIn("--sandbox", first_args)
            self.assertNotIn("--resume", first_args)
            self.assertEqual(
                second_args[second_args.index("--resume") + 1], "session-1"
            )
            self.assertNotIn("--model", second_args)
            self.assertNotIn("--rules", second_args)
            self.assertNotIn("--resume", other_args)

            first_cwd = Path(calls[0]["kwargs"]["cwd"])
            other_cwd = Path(calls[2]["kwargs"]["cwd"])
            back_cwd = Path(calls[3]["kwargs"]["cwd"])
            self.assertNotEqual(first_cwd, other_cwd)
            self.assertEqual(first_cwd, back_cwd)
            self.assertTrue((first_cwd / "INPUT.md").is_file())
            self.assertEqual(manager.get_agent_workspace("agent"), first_cwd)
            self.assertEqual(
                calls[0]["kwargs"]["env"]["GROK_HOME"],
                str(client.grok_home),
            )

    async def test_nonzero_error_returns_original_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "agent"
            workspace.mkdir()
            process = _FakeProcess(
                json.dumps(
                    {"type": "error", "message": "api_key=sk-secret12345"}
                ).encode(),
                stderr=b"provider error",
                returncode=1,
            )

            async def fake_spawn(*args, **kwargs):
                return process

            client = GrokClient("grok-test", Path(temp_dir) / "home")
            _register(client, workspace)
            with patch(
                "src.grok_client.asyncio.create_subprocess_exec", fake_spawn
            ):
                result = await client.get_agent("agent", "main").execute(
                    "query"
                )

            self.assertFalse(result.success)
            self.assertEqual(result.error_message, "api_key=sk-secret12345")

    async def test_timeout_terminates_process_without_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "agent"
            workspace.mkdir()
            process = _FakeProcess(block=True)
            spawn_calls = 0

            async def fake_spawn(*args, **kwargs):
                nonlocal spawn_calls
                spawn_calls += 1
                return process

            client = GrokClient("grok-test", Path(temp_dir) / "home")
            _register(client, workspace)
            with patch(
                "src.grok_client.asyncio.create_subprocess_exec", fake_spawn
            ):
                result = await client.get_agent("agent", "main").execute(
                    "slow", ExecutionOptions(timeout_seconds=0.01)
                )

            self.assertFalse(result.success)
            self.assertEqual(result.stop_reason, "timeout")
            self.assertEqual(spawn_calls, 1)
            self.assertEqual(process.terminate_calls, 1)

    async def test_cancellation_terminates_process_and_propagates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "agent"
            workspace.mkdir()
            process = _FakeProcess(block=True)

            async def fake_spawn(*args, **kwargs):
                return process

            client = GrokClient("grok-test", Path(temp_dir) / "home")
            _register(client, workspace)
            with patch(
                "src.grok_client.asyncio.create_subprocess_exec", fake_spawn
            ):
                task = asyncio.create_task(
                    client.get_agent("agent", "main").execute("slow")
                )
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertEqual(process.terminate_calls, 1)

    async def test_execute_wrapper_does_not_replay_failure(self):
        class ErrorAgent:
            def __init__(self):
                self.calls = 0

            async def execute(self, query, options=None):
                self.calls += 1
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message="failed",
                )

        agent = ErrorAgent()
        execute_once = make_grok_execute_with_retry(SimpleNamespace())
        with self.assertRaisesRegex(GrokHarnessError, "failed"):
            await execute_once(agent, "query", None)
        self.assertEqual(agent.calls, 1)

    async def test_execute_wrapper_marks_only_noncomplete_result_incomplete(self):
        execute_once = make_grok_execute_with_retry(SimpleNamespace())
        complete_agent = SimpleNamespace(
            execute=AsyncMock(
                return_value=ExecutionResult(
                    success=True,
                    content="ok",
                    stop_reason="complete",
                )
            )
        )
        partial_agent = SimpleNamespace(
            execute=AsyncMock(
                return_value=ExecutionResult(
                    success=True,
                    content="partial",
                    stop_reason="max_turns",
                )
            )
        )

        _, complete_incomplete = await execute_once(
            complete_agent, "query", None
        )
        _, partial_incomplete = await execute_once(
            partial_agent, "query", None
        )
        self.assertFalse(complete_incomplete)
        self.assertTrue(partial_incomplete)


class GrokParserTests(unittest.TestCase):
    def test_parser_requires_single_json_object(self):
        with self.assertRaisesRegex(GrokHarnessError, "合法"):
            _parse_headless_output("warning\n{}")
        with self.assertRaisesRegex(GrokHarnessError, "不是对象"):
            _parse_headless_output("[]")
        with self.assertRaisesRegex(GrokHarnessError, "stopReason"):
            _parse_headless_output('{"text":"ok","sessionId":"session"}')


class GrokWorkspaceAndConfigTests(unittest.TestCase):
    def test_agent_files_and_skills_use_grok_project_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent_source = root / "agent-source"
            skill_source = root / "skills" / "demo-skill"
            agent_source.mkdir()
            skill_source.mkdir(parents=True)
            (agent_source / "AGENTS.md").write_text("agent", encoding="utf-8")
            (skill_source / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: test\n---\n",
                encoding="utf-8",
            )
            manager = GrokWorkspaceManager(str(root / "workspaces"))

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

    def test_agent_manager_uses_model_alias_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = GrokClient()
            workspace_manager = GrokWorkspaceManager(temp_dir)
            override = AgentModelConfig(
                model="custom-alias",
                provider="metadata-only",
                base_url="https://ignored.invalid",
                api_key="not-written",
            )
            manager = GrokAgentManager(
                client, workspace_manager, {"main": override}
            )
            config = SimpleNamespace(
                name="main", model="original-alias", system_prompt=None
            )

            with self.assertLogs("harness_automation", level="WARNING"):
                asyncio.run(manager.setup_agent(config))

            defaults = client._agent_defaults["main"]
            self.assertEqual(defaults.model, "custom-alias")
            self.assertIsNone(defaults.model_provider)

    def test_shared_configs_use_grok_workspace(self):
        config_names = (
            "config_simple.json",
            "config_user.json",
            "config_simple_eval.json",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "grok-home"
            workspace_root = Path(temp_dir) / "grok-workspace"
            with (
                patch.dict(
                    os.environ,
                    {
                        "GROK_HOME": str(home),
                        "GROK_HARNESS_WORKSPACE": str(workspace_root),
                    },
                ),
                patch("src.grok_client.GrokWorkspaceManager") as manager_class,
            ):
                workspace_manager = manager_class.return_value
                for config_name in config_names:
                    with self.subTest(config=config_name):
                        config = ConfigLoader.load_from_file(
                            str(PROJECT_ROOT / "configs" / config_name)
                        )
                        config.harness_type = "grok"
                        automation = HarnessAutomation(config)

                        manager_class.assert_called_once_with(
                            str(workspace_root.resolve())
                        )
                        self.assertIs(
                            automation.workspace_manager, workspace_manager
                        )
                        manager_class.reset_mock()


if __name__ == "__main__":
    unittest.main()
