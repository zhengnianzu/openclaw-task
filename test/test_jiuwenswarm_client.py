"""JiuwenSwarm 多轮结束信号与项目技能的最小回归验证。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.config import AgentConfigItem, AutomationConfig
from src.jiuwenswarm_client import JiuwenSwarmClient, JiuwenSwarmWorkspaceManager
from harness_automation import HarnessAutomation


class JiuwenSwarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = JiuwenSwarmClient("ws://localhost/tui", "code.normal")
        self.agent = self.client.get_agent("main", "test")

    def event(self, name, **payload):
        return {"type": "event", "event": name,
                "payload": {"session_id": self.agent.session_key, **payload}}

    async def test_late_previous_turn_status_does_not_end_current_turn(self):
        ws = AsyncMock()
        ws.receive_json.side_effect = [
            self.event("chat.processing_status", request_id="previous", is_processing=False),
            self.event("chat.delta", content="第二轮回复"),
            self.event("chat.final", content=""),
        ]
        result = await self.agent._receive(ws, "current")
        self.assertEqual(result.content, "第二轮回复")

    async def test_team_waits_for_current_turn_end(self):
        self.client.mode = "team.code.normal"
        ws = AsyncMock()
        ws.receive_json.side_effect = [
            self.event("chat.final", content="团队阶段结果"),
            self.event("chat.processing_status", request_id="previous", is_processing=False),
            self.event("chat.delta", content="，任务完成"),
            self.event("chat.processing_status", request_id="current", is_processing=False),
        ]
        result = await self.agent._receive(ws, "current")
        self.assertEqual(result.content, "团队阶段结果，任务完成")

    async def test_legacy_status_without_request_id_still_completes(self):
        ws = AsyncMock()
        ws.receive_json.side_effect = [
            self.event("chat.delta", content="旧版回复"),
            self.event("chat.processing_status", is_processing=False),
        ]
        result = await self.agent._receive(ws, "current")
        self.assertEqual(result.content, "旧版回复")

    async def test_harness_names_reuse_dispatch_and_keep_their_workspace(self):
        for name, workspace in (("jiuwenswarm", "workspace"), ("jiuwenswarm-dev-stable", "jiuwenclaw_workspace")):
            with self.subTest(harness=name):
                automation = HarnessAutomation(AutomationConfig(harness_type=name))
                self.assertIsInstance(automation.workspace_manager, JiuwenSwarmWorkspaceManager)
                self.assertEqual(automation.workspace_manager.base_dir.name, workspace)
                automation._run_jiuwenswarm = AsyncMock(return_value={"success": True})
                self.assertEqual(await automation.run(), {"success": True})
                automation._run_jiuwenswarm.assert_awaited_once()

    def test_only_declared_project_skills_are_injected_on_first_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            self.client.workspace_manager = JiuwenSwarmWorkspaceManager(directory)
            self.client.setup_agent(AgentConfigItem(name="main", skills=["category/review"]))
            workspace = Path(directory).resolve()
            for name in ("review", "other"):
                path = workspace / "skills" / name
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text(f"技能正文 {name}", encoding="utf-8")
            prompt = self.agent._prompt("请检查")
            self.assertIn("技能正文 review", prompt)
            self.assertIn(str(workspace / "skills" / "review"), prompt)
            self.assertNotIn("技能正文 other", prompt)
            self.agent._initialized = True
            self.assertEqual(self.agent._prompt("继续"), "继续")


if __name__ == "__main__":
    unittest.main()
