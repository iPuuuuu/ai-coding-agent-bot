"""End-to-end smoke test of the Feishu adapter dispatch path (no network).

Exercises command routing, @-prefix session routing and the session cards
through the real bot handlers with a stub transport.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import bot
import channel
import config


class StubTransport:
    """Minimal FeishuTransport stand-in recording outbound calls."""

    def __init__(self):
        self.chat_id = ""
        self.sent: list[str] = []
        self.cards: list[dict] = []

    def set_chat_id(self, chat_id: str) -> None:
        self.chat_id = chat_id

    async def send_text(self, text: str, chat_id: str = "") -> None:
        self.sent.append(text)

    async def send_choice(self, text, options, choice_id, title="需要你的选择") -> None:
        self.cards.append({"kind": "choice", "title": title, "options": options})

    async def send_monitor_choices(self, text, choices) -> None:
        self.cards.append({"kind": "monitor", "choices": choices})

    async def send_command_menu(self, choices) -> None:
        self.cards.append({"kind": "menu", "choices": choices})

    async def send_project_choices(self, choices, title="选择工作区") -> None:
        self.cards.append({"kind": "project", "title": title, "choices": choices})

    async def send_session_cards(self, heading, sessions) -> None:
        self.cards.append({"kind": "sessions", "heading": heading, "sessions": sessions})

    async def send_photo(self, path, caption="") -> None:
        self.cards.append({"kind": "photo", "path": path})


class FeishuDispatchSmokeTest(unittest.TestCase):
    def setUp(self):
        self._allowed = config.ALLOWED_FEISHU_OPEN_ID
        config.ALLOWED_FEISHU_OPEN_ID = "ou_test_open_id"
        self._sessions = dict(bot._state.sessions)
        self._queue = list(bot._state.task_queue)
        self._tasks = dict(bot._running_tasks)
        self._current_cwd = bot._state.current_cwd
        self._current_session_id = bot._state.current_session_id
        self._start_new_session_next = bot._state.start_new_session_next
        bot._state.sessions.clear()
        bot._state.task_queue.clear()
        bot._running_tasks.clear()
        bot._state.pending_by_session.clear()
        self.sid = "deadbeef12345678"
        bot._state.sessions[self.sid] = bot.ManagedSession(
            session_id=self.sid, project_path="/tmp/p", project_label="p", title="冒烟会话"
        )

    def tearDown(self):
        config.ALLOWED_FEISHU_OPEN_ID = self._allowed
        channel.init_feishu(None)
        bot._state.sessions.clear()
        bot._state.sessions.update(self._sessions)
        bot._state.task_queue.clear()
        bot._state.task_queue.extend(self._queue)
        bot._running_tasks.clear()
        bot._running_tasks.update(self._tasks)
        bot._state.pending_by_session.clear()
        bot._state.set_current_project(self._current_cwd)
        bot._state.current_session_id = self._current_session_id
        bot._state.start_new_session_next = self._start_new_session_next

    def _dispatch(self, text: str) -> StubTransport:
        transport = StubTransport()
        channel.init_feishu(transport)
        asyncio.run(bot.dispatch_feishu_text("ou_test_open_id", "oc_test_chat", text, transport))
        return transport

    def _dispatch_button(self, callback_data: str) -> StubTransport:
        transport = StubTransport()
        transport.set_chat_id("oc_test_chat")
        channel.init_feishu(transport)
        asyncio.run(bot.dispatch_feishu_button("ou_test_open_id", callback_data, transport))
        return transport

    def test_sessions_command_sends_cards(self):
        transport = self._dispatch("/sessions")
        self.assertTrue(transport.sent, "expected a text summary")
        self.assertTrue(any(card["kind"] == "sessions" for card in transport.cards))
        session_card = next(card for card in transport.cards if card["kind"] == "sessions")
        self.assertTrue(session_card["sessions"])

    def test_help_command_sends_menu(self):
        transport = self._dispatch("/help")
        self.assertTrue(any(card["kind"] == "menu" for card in transport.cards))

    def test_new_command_asks_for_workspace(self):
        transport = self._dispatch("/new")
        picker = next(card for card in transport.cards if card["kind"] == "project")
        self.assertEqual(picker["title"], "选择新会话工作区")
        self.assertTrue(all(data.startswith("workspace:new:") for _, data in picker["choices"]))
        self.assertTrue(any("请选择新会话所在的工作区" in text for text in transport.sent))

    def test_workspace_button_forces_new_session_in_selected_path(self):
        path = next(iter(bot._state.projects.values())).path
        transport = self._dispatch_button("workspace:new:0")
        self.assertEqual(bot._state.current_cwd, path)
        self.assertEqual(bot._state.current_session_id, "")
        self.assertTrue(bot._state.start_new_session_next)
        self.assertTrue(any(path in text for text in transport.sent))

        started: list[tuple[str, str, str]] = []

        def fake_start_run(prompt, project_path, session_key, session_id):
            started.append((project_path, session_key, session_id))

        with mock.patch.object(bot, "_start_run", fake_start_run):
            self._dispatch("开始实现")
        self.assertEqual(started[0][0], path)
        self.assertTrue(started[0][1].startswith("new:"))
        self.assertEqual(started[0][2], "")

    def test_unauthorized_rejected(self):
        transport = StubTransport()
        asyncio.run(bot.dispatch_feishu_text("ou_evil", "oc_test_chat", "/sessions", transport))
        self.assertTrue(any("未授权" in text for text in transport.sent))

    def test_at_routing_plain_message_does_not_start_run(self):
        # A plain message to the focused session would call _start_run, which we
        # stub; the @-routed message to an existing session must reach it too.
        bot._state.current_session_id = self.sid
        bot._state.current_cwd = "/tmp/p"
        started: list[str] = []

        def fake_start_run(prompt, project_path, session_key, session_id):
            started.append((prompt, session_key))

        with mock.patch.object(bot, "_start_run", fake_start_run):
            transport = self._dispatch(f"@{self.sid[:8]} 帮我改一下配置")
        self.assertEqual(started, [("帮我改一下配置", self.sid)])
        self.assertTrue(any("已开始任务" in text for text in transport.sent))

    def test_mention_placeholder_stripped(self):
        bot._state.current_session_id = self.sid
        bot._state.current_cwd = "/tmp/p"
        started: list[str] = []

        def fake_start_run(prompt, project_path, session_key, session_id):
            started.append(prompt)

        with mock.patch.object(bot, "_start_run", fake_start_run):
            self._dispatch("@_user_1 你好")
        self.assertEqual(started, ["你好"])


if __name__ == "__main__":
    unittest.main()
