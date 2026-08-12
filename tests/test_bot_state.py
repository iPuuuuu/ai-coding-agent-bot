"""Unit tests for the bot task queue and state persistence."""
from __future__ import annotations

import unittest

import bot


class TaskQueueTest(unittest.TestCase):
    def setUp(self):
        self.state = bot.RuntimeState()
        self.state.ensure_project("/tmp/p1")
        self.state.set_current_project("/tmp/p1")

    def test_enqueue_pop_clear(self):
        first = self.state.enqueue_task("任务A", "/tmp/p1", "sess-1", False)
        second = self.state.enqueue_task("任务B", "/tmp/p1", "sess-1", True)
        self.assertEqual(first.task_number, 1)
        self.assertEqual(second.task_number, 2)
        self.assertEqual(second.force_new, True)
        self.assertEqual(len(self.state.task_queue), 2)

        popped = self.state.pop_next_queued()
        self.assertIsNotNone(popped)
        self.assertEqual(popped.prompt, "任务A")
        self.assertEqual(popped.session_id, "sess-1")

        removed = self.state.clear_queue()
        self.assertEqual(removed, 1)
        self.assertEqual(self.state.pop_next_queued(), None)


class StatePersistenceTest(unittest.TestCase):
    def test_dump_apply_round_trip(self):
        source = bot.RuntimeState()
        source.ensure_project("/tmp/p1")
        source.set_current_project("/tmp/p1")
        session = source.bind_session("sess-123", "/tmp/p1", "初始任务")
        session.mode = "running"
        session.last_event = "正在运行"
        snap = source.projects[bot._norm_path("/tmp/p1")]
        snap.add_entry("user", "帮我修 bug", "sess-123")
        snap.add_entry("assistant/text", "已修复", "sess-123")
        source.current_session_id = "sess-123"

        data = bot._dump_state(source)
        restored = bot.RuntimeState()
        bot._apply_state(restored, data)

        self.assertIn("sess-123", restored.sessions)
        self.assertEqual(restored.sessions["sess-123"].title, "初始任务")
        self.assertEqual(restored.sessions["sess-123"].mode, "idle")  # never restore as live
        self.assertEqual(restored.current_session_id, "sess-123")
        self.assertEqual(restored.project_last_session.get("/tmp/p1"), "sess-123")
        texts = [entry.text for entry in restored.projects[bot._norm_path("/tmp/p1")].transcript]
        self.assertIn("帮我修 bug", texts)
        self.assertIn("已修复", texts)

    def test_apply_ignores_garbage(self):
        restored = bot.RuntimeState()
        bot._apply_state(restored, {"sessions": "not-a-list", "projects": None})
        self.assertEqual(len(restored.sessions), 0)


if __name__ == "__main__":
    unittest.main()
