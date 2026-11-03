from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.harness_adapter import JiuwenSwarmAdapter, get_adapter


def _write_session(
    root: Path,
    session_id: str,
    project_dir: str,
    last_message_at: float,
    events: list[dict],
) -> Path:
    session = (
        root / "service_default" / "agent_default" / "agent"
        / "sessions" / session_id
    )
    session.mkdir(parents=True)
    (session / "metadata.json").write_text(json.dumps({
        "project_dir": project_dir,
        "last_message_at": last_message_at,
    }), encoding="utf-8")
    history = session / "history.json"
    history.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return history


class JiuwenSwarmAdapterTest(unittest.TestCase):
    def test_registry_supports_both_harness_names(self):
        self.assertIsInstance(get_adapter("jiuwenswarm"), JiuwenSwarmAdapter)
        self.assertIsInstance(get_adapter("jiuwenswarm-dev-stable"), JiuwenSwarmAdapter)

    def test_selects_latest_matching_agent_and_analyzes_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(
                root, "older", "/workspace/jiuwenclaw_workspace", 10,
                [{"event_type": "chat.tool_call"}],
            )
            latest = _write_session(
                root, "latest", "/workspace/jiuwenclaw_workspace", 20,
                [
                    {
                        "event_type": "chat.usage_metadata",
                        "metadata": {"usage_metadata": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "reasoning_tokens": 2,
                        }},
                    },
                    {"event_type": "chat.final", "content": "先读取文件"},
                    {"event_type": "chat.tool_call"},
                    {"event_type": "chat.tool_result"},
                    {
                        "event_type": "chat.usage_metadata",
                        "metadata": {"usage_metadata": {
                            "input_tokens": 200,
                            "output_tokens": 20,
                        }},
                    },
                    {"event_type": "chat.final", "content": "最终答复"},
                ],
            )
            _write_session(
                root, "evaluator", "/workspace/jiuwenclaw_workspace-evaluator", 30,
                [{"event_type": "chat.final", "content": "evaluation"}],
            )
            adapter = JiuwenSwarmAdapter()
            with patch.object(adapter, "_sessions_root", return_value=root):
                self.assertEqual(adapter.find_trajectory("main"), latest)
                metrics = adapter.analyze(latest, "main")

            self.assertEqual(metrics, {
                "tool_calls": 1,
                "plain_rounds": 1,
                "assistant_rounds": 2,
                "input_tokens": 300,
                "output_tokens": 30,
                "reasoning_tokens": 2,
                "total_tokens": 332,
            })

    def test_evaluator_verdict_from_chat_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_session(
                root, "evaluator", "/workspace/jiuwenclaw_workspace-evaluator", 30,
                [{
                    "event_type": "chat.final",
                    "content": "```json\n{\"task_declared_complete\": true, "
                               "\"inclination\": \"accept\", \"completion\": 0.75}\n```",
                }],
            )
            adapter = JiuwenSwarmAdapter()
            self.assertEqual(adapter.extract_eval_traj_verdict(history), (True, 0.75))


if __name__ == "__main__":
    unittest.main()
