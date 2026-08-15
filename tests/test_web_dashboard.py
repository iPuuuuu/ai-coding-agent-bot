"""Unit tests for the read-only Web dashboard (HTTP + auth + payload shape)."""
from __future__ import annotations

import json
import unittest
from unittest import mock

import config
import web_dashboard


class DashboardAuthTest(unittest.TestCase):
    def setUp(self):
        self._token = config.DASHBOARD_TOKEN
        config.DASHBOARD_TOKEN = "secret-token"

    def tearDown(self):
        config.DASHBOARD_TOKEN = self._token

    class _FakeHandler:
        def __init__(self, path, addr=("203.0.113.5", 1234)):
            self.path = path
            self.client_address = addr
            self.headers = {}

    def test_query_token_authorized(self):
        handler = self._FakeHandler("/api/overview?token=secret-token")
        self.assertTrue(web_dashboard._authorized(handler))

    def test_wrong_token_rejected(self):
        handler = self._FakeHandler("/api/overview?token=wrong")
        self.assertFalse(web_dashboard._authorized(handler))

    def test_header_token_authorized(self):
        handler = self._FakeHandler("/api/overview")
        handler.headers = {"X-Dashboard-Token": "secret-token"}
        self.assertTrue(web_dashboard._authorized(handler))

    def test_no_token_binds_localhost_only(self):
        config.DASHBOARD_TOKEN = ""
        local = self._FakeHandler("/api/overview", addr=("127.0.0.1", 1))
        remote = self._FakeHandler("/api/overview", addr=("203.0.113.5", 1))
        self.assertTrue(web_dashboard._authorized(local))
        self.assertFalse(web_dashboard._authorized(remote))


class DashboardPayloadTest(unittest.TestCase):
    def setUp(self):
        self._sessions = dict(self._import_bot()._state.sessions)

    def tearDown(self):
        self._import_bot()._state.sessions.clear()
        self._import_bot()._state.sessions.update(self._sessions)

    @staticmethod
    def _import_bot():
        import bot
        return bot

    def test_overview_shape(self):
        bot = self._import_bot()
        bot._state.sessions.clear()
        bot._state.sessions["sess-1234567890"] = bot.ManagedSession(
            session_id="sess-1234567890", project_path="/tmp/p", project_label="p", title="看板测试"
        )
        web_dashboard.set_scan_cache([])
        payload = web_dashboard._overview_payload()
        self.assertIn("sessions", payload)
        self.assertIn("parallel", payload)
        self.assertIn("pending", payload)
        self.assertIn("queue", payload)
        sids = [s["session_id"] for s in payload["sessions"]]
        self.assertIn("sess-1234567890", sids)
        session = next(s for s in payload["sessions"] if s["session_id"] == "sess-1234567890")
        self.assertEqual(session["short_id"], "sess-123")
        self.assertTrue(session["managed"])
        self.assertEqual(session["project"], "p")

    def test_session_detail(self):
        bot = self._import_bot()
        bot._state.sessions.clear()
        bot._state.sessions["sess-abcdef"] = bot.ManagedSession(
            session_id="sess-abcdef", project_path="/tmp/p", project_label="p",
            title="详情测试", last_prompt="帮我改代码",
        )
        snap = bot._state.ensure_project("/tmp/p")
        snap.add_entry("user", "帮我改代码", "sess-abcdef")
        snap.add_entry("assistant/text", "好的，已修复", "sess-abcdef")
        detail = web_dashboard._session_detail("sess-abcdef")
        self.assertEqual(detail["title"], "详情测试")
        self.assertEqual(detail["last_prompt"], "帮我改代码")
        texts = [m["text"] for m in detail["transcript"]]
        self.assertIn("好的，已修复", texts)
        self.assertIsNone(web_dashboard._session_detail("不存在"))

    def test_page_contains_core_elements(self):
        self.assertIn("api/overview", web_dashboard._PAGE)
        self.assertIn("api/session", web_dashboard._PAGE)
        self.assertIn("setInterval", web_dashboard._PAGE)


if __name__ == "__main__":
    unittest.main()
