"""Unit tests for the multi-session parallel control features."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import bot


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.sid = "abcdef1234567890"
        self._sessions = dict(bot._state.sessions)
        bot._state.sessions[self.sid] = bot.ManagedSession(
            session_id=self.sid, project_path="/tmp/p", project_label="p", title="测试会话"
        )

    def tearDown(self):
        bot._state.sessions.clear()
        bot._state.sessions.update(self._sessions)

    def test_at_prefix_routes_to_session(self):
        target, remainder, routed = bot._resolve_target("@abcdef12 继续修复")
        self.assertTrue(routed)
        self.assertEqual(target, self.sid)
        self.assertEqual(remainder, "继续修复")

    def test_at_prefix_only_is_focus_request(self):
        target, remainder, routed = bot._resolve_target("@abcdef12")
        self.assertTrue(routed)
        self.assertEqual(target, self.sid)
        self.assertEqual(remainder, "")

    def test_plain_message_not_routed(self):
        target, remainder, routed = bot._resolve_target("普通消息 hello")
        self.assertFalse(routed)
        self.assertEqual(target, "")
        self.assertEqual(remainder, "普通消息 hello")

    def test_unknown_at_prefix_falls_back_to_plain(self):
        target, remainder, routed = bot._resolve_target("@nope123 消息")
        self.assertFalse(routed)
        self.assertEqual(remainder, "@nope123 消息")

    def test_at_prefix_requires_three_chars(self):
        target, remainder, routed = bot._resolve_target("@ab 消息")
        self.assertFalse(routed)
        self.assertEqual(remainder, "@ab 消息")


class PendingBySessionTest(unittest.TestCase):
    def test_set_and_clear_per_session(self):
        state = bot.RuntimeState()
        p1 = bot.PendingInteraction(project_path="/tmp/a", project_label="a", session_id="sess-1", prompt_text="问题1")
        p2 = bot.PendingInteraction(project_path="/tmp/b", project_label="b", session_id="sess-2", prompt_text="问题2")
        state.set_pending(p1)
        state.set_pending(p2)
        self.assertIn("sess-1", state.pending_by_session)
        self.assertIn("sess-2", state.pending_by_session)
        # ``pending`` mirrors the most recently registered one.
        self.assertIs(state.pending, p2)

        state.clear_pending("sess-1")
        self.assertNotIn("sess-1", state.pending_by_session)
        self.assertIs(state.pending, p2)  # unaffected
        state.clear_pending("sess-2")
        self.assertIsNone(state.pending)


class StopSessionTest(unittest.TestCase):
    def test_stop_dismisses_pending(self):
        bot._state.pending_by_session.clear()
        bot._state.pending = None
        pending = bot.PendingInteraction(project_path="/tmp/a", project_label="a", session_id="sess-stop", prompt_text="问题")
        bot._state.set_pending(pending)
        self.assertTrue(bot._stop_session_run("sess-stop"))
        self.assertNotIn("sess-stop", bot._state.pending_by_session)

    def test_stop_idle_session_returns_false(self):
        self.assertFalse(bot._stop_session_run("sess-不存在"))


class OutputTagTest(unittest.TestCase):
    def setUp(self):
        self._tasks = dict(bot._running_tasks)
        self._focus = bot._state.current_session_id
        bot._running_tasks.clear()

    def tearDown(self):
        bot._running_tasks.clear()
        bot._running_tasks.update(self._tasks)
        bot._state.current_session_id = self._focus

    def test_no_tag_when_focused_and_alone(self):
        bot._state.current_session_id = "abc"
        self.assertEqual(bot._output_tag("abc"), "")

    def test_tag_when_not_focused(self):
        bot._state.current_session_id = "zzz"
        self.assertEqual(bot._output_tag("abc"), "[会话 abc]")

    def test_tag_when_parallel_runs_active(self):
        async def scenario():
            bot._state.current_session_id = "abc"
            bot._running_tasks["abc"] = asyncio.create_task(asyncio.sleep(5))
            bot._running_tasks["def"] = asyncio.create_task(asyncio.sleep(5))
            try:
                return bot._output_tag("abc")
            finally:
                for task in bot._running_tasks.values():
                    task.cancel()

        self.assertEqual(asyncio.run(scenario()), "[会话 abc]")


class DrainQueueTest(unittest.TestCase):
    def setUp(self):
        self._limit = bot.config.MAX_PARALLEL_TASKS
        self._queue = list(bot._state.task_queue)
        self._tasks = dict(bot._running_tasks)
        bot._state.task_queue.clear()
        bot._running_tasks.clear()
        bot.config.MAX_PARALLEL_TASKS = 2

    def tearDown(self):
        bot.config.MAX_PARALLEL_TASKS = self._limit
        for task in bot._running_tasks.values():
            if not task.done():
                task.cancel()
        bot._state.task_queue.clear()
        bot._state.task_queue.extend(self._queue)
        bot._running_tasks.clear()
        bot._running_tasks.update(self._tasks)

    def _enqueue(self, key: str, prompt: str):
        bot._state.task_queue.append(
            bot.QueuedTask(prompt=prompt, project_path="/tmp/p", session_id=key, session_key=key, task_number=1)
        )

    async def _drain_with_recorder(self):
        started: list[str] = []

        def fake_start_run(prompt, project_path, session_key, session_id):
            started.append(session_key)
            # Register the run so the parallel limit is enforced like the real one.
            bot._running_tasks[session_key] = asyncio.create_task(asyncio.sleep(30))
            return bot._running_tasks[session_key]

        async def fake_send_text(text, tag=""):
            pass

        with mock.patch.object(bot, "_start_run", fake_start_run), \
             mock.patch.object(bot.channel, "send_text", side_effect=fake_send_text):
            await bot._drain_queues()
        return started

    def test_respects_parallel_limit(self):
        for key in ("s1", "s2", "s3", "s4"):
            self._enqueue(key, f"任务{key}")
        started = asyncio.run(self._drain_with_recorder())
        self.assertEqual(started, ["s1", "s2"])
        self.assertEqual(len(bot._state.task_queue), 2)

    def test_skips_running_and_waiting_sessions(self):
        async def scenario():
            self._enqueue("s1", "s1任务")
            self._enqueue("s2", "s2任务")
            self._enqueue("s3", "s3任务")
            # s1 already has an active run; s2 is waiting for user input.
            bot._running_tasks["s1"] = asyncio.create_task(asyncio.sleep(30))
            bot._state.pending_by_session["s2"] = bot.PendingInteraction(
                project_path="/tmp/p", project_label="p", session_id="s2", prompt_text="等回复"
            )
            return await self._drain_with_recorder()

        started = asyncio.run(scenario())
        self.assertEqual(started, ["s3"])
        # s1/s2 tasks stay queued behind their busy sessions.
        self.assertEqual([t.session_key for t in bot._state.task_queue], ["s1", "s2"])


class SessionCardsTest(unittest.TestCase):
    def test_build_cards_includes_actions(self):
        fake_codex = {
            "codex-111111111111": {
                "source": "codex", "session_id": "codex-111111111111",
                "status": "completed", "cwd": "/tmp/proj", "last_event": "task_complete",
                "updated_at": 1,
            },
        }
        with mock.patch.object(bot, "_load_codex_sessions", return_value=fake_codex), \
             mock.patch.object(bot, "_load_global_sessions", return_value={}):
            bot._state.sessions.clear()
            bot._state.current_session_id = ""
            cards = bot._build_session_cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertIn("codex-11", card["title"])
        self.assertIn("已完成", card["title"])
        labels = [label for label, _ in card["buttons"]]
        self.assertIn("详情", labels)
        self.assertIn("焦点", labels)
        self.assertIn("接管", labels)
        self.assertNotIn("停止", labels)  # not running


class RunLifecycleTest(unittest.TestCase):
    """End-to-end scheduling: _start_run -> _run_agent -> outcome handling."""

    def setUp(self):
        self._tasks = dict(bot._running_tasks)
        self._queue = list(bot._state.task_queue)
        bot._running_tasks.clear()
        bot._state.task_queue.clear()
        bot._state.pending_by_session.clear()
        bot._state.sessions.clear()

    def tearDown(self):
        for task in bot._running_tasks.values():
            if not task.done():
                task.cancel()
        bot._running_tasks.clear()
        bot._running_tasks.update(self._tasks)
        bot._state.task_queue.clear()
        bot._state.task_queue.extend(self._queue)
        bot._state.pending_by_session.clear()

    def test_completed_run_binds_session_and_pops_task(self):
        async def fake_run_turn(prompt, cwd, session_id=None, run_key=None, tag=""):
            return bot.agent_runner.TurnOutcome(
                session_id="sess-x",
                final_text="完成",
                events=[
                    bot.agent_runner.MirrorEvent(kind="system/init", text="session=sess-x", session_id="sess-x"),
                    bot.agent_runner.MirrorEvent(kind="assistant/text", text="完成", session_id="sess-x"),
                ],
            )

        with mock.patch.object(bot.agent_runner, "run_turn", side_effect=fake_run_turn):
            async def run():
                bot._state.set_current_project("/tmp/p")
                await bot._start_run("任务", "/tmp/p", "sess-x", "sess-x")

            asyncio.run(run())

        self.assertNotIn("sess-x", bot._running_tasks)  # removed in finally
        self.assertIn("sess-x", bot._state.sessions)    # bound by append_events
        self.assertEqual(bot._state.sessions["sess-x"].mode, "idle")
        self.assertNotIn("sess-x", bot._state.pending_by_session)

    def test_waiting_run_registers_pending_for_session(self):
        async def fake_run_turn(prompt, cwd, session_id=None, run_key=None, tag=""):
            return bot.agent_runner.TurnOutcome(
                session_id="sess-y",
                final_text="请选择：\n1. 方案A\n2. 方案B",
                waiting_text="请选择：\n1. 方案A\n2. 方案B",
                choice_options=["方案A", "方案B"],
                state="waiting",
                events=[],
            )

        with mock.patch.object(bot.agent_runner, "run_turn", side_effect=fake_run_turn):
            async def run():
                bot._state.set_current_project("/tmp/p")
                await bot._start_run("任务", "/tmp/p", "sess-y", "sess-y")

            asyncio.run(run())

        pending = bot._state.pending_by_session.get("sess-y")
        self.assertIsNotNone(pending)
        self.assertEqual(pending.options, ["方案A", "方案B"])
        self.assertEqual(bot._state.sessions["sess-y"].mode, "waiting_user_reply")


if __name__ == "__main__":
    unittest.main()
