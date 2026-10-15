"""Tests for privacy-scoped Codex rollout message mirroring."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.codex_session_monitor import _read_rollout


class RolloutMessageTest(unittest.TestCase):
    def test_extracts_only_user_visible_messages(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "sess-1", "cwd": "/tmp/p"}},
            {"timestamp": 1, "type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "Text", "text": "请修复"}]},
            }},
            {"timestamp": 2, "type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "Reasoning", "id": "r1", "raw_content": "不能转发"},
            }},
            {"timestamp": 3, "type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "id": "c1", "command": "secret"},
            }},
            {"timestamp": 4, "type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "AgentMessage", "id": "a1", "content": [{"type": "Text", "text": "已经修复"}]},
            }},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-test.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            summary = _read_rollout(path)

        self.assertIsNotNone(summary)
        self.assertEqual(
            [(item["id"], item["role"], item["text"]) for item in summary["messages"]],
            [("u1", "user", "请修复"), ("a1", "assistant", "已经修复")],
        )


if __name__ == "__main__":
    unittest.main()
