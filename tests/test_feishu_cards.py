"""Unit tests for Feishu interactive-card JSON generation (no network)."""
from __future__ import annotations

import asyncio
import json
import unittest

from feishu_bridge import FeishuTransport


def _capture(transport: FeishuTransport) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []

    def fake_create(chat_id: str, msg_type: str, content: str) -> None:
        captured.append((msg_type, json.loads(content)))

    transport._create_message = fake_create  # type: ignore[method-assign]
    return captured


class SessionCardsJsonTest(unittest.TestCase):
    def test_session_cards_are_valid_cards(self):
        transport = FeishuTransport(client=None)
        transport.chat_id = "oc_test"
        captured = _capture(transport)

        sessions = [
            {
                "title": "Codex deadbeef · 运行中",
                "subtitle": "doctor-wang | 正在跑测试",
                "buttons": [("详情", "sess:detail:deadbeef"), ("停止", "sess:stop:deadbeef")],
            },
            {
                "title": "Codex cafebabe · 已完成",
                "subtitle": "bot | task_complete",
                "buttons": [("详情", "sess:detail:cafebabe"), ("焦点", "sess:focus:cafebabe")],
            },
        ]
        asyncio.run(transport.send_session_cards("标题", sessions))

        self.assertEqual(len(captured), 3)  # two cards + heading text
        cards = [content for kind, content in captured if kind == "interactive"]
        self.assertEqual(len(cards), 2)
        first = cards[0]
        self.assertEqual(first["header"]["title"]["content"], "Codex deadbeef · 运行中")
        # The subtitle is a markdown element; buttons become action rows.
        actions = [el for el in first["elements"] if el["tag"] == "action"]
        labels = [btn["text"]["content"] for row in actions for btn in row["actions"]]
        self.assertEqual(labels, ["详情", "停止"])
        # callback data is preserved on the button values
        values = [btn["value"]["callback_data"] for row in actions for btn in row["actions"]]
        self.assertEqual(values, ["sess:detail:deadbeef", "sess:stop:deadbeef"])

    def test_choice_card_has_session_title(self):
        transport = FeishuTransport(client=None)
        transport.chat_id = "oc_test"
        captured = _capture(transport)

        asyncio.run(transport.send_choice("请选择方案", ["方案A", "方案B"], "abc12345", title="会话 deadbeef 需要你的选择"))
        kind, card = captured[0]
        self.assertEqual(kind, "interactive")
        self.assertEqual(card["header"]["title"]["content"], "会话 deadbeef 需要你的选择")
        action = next(el for el in card["elements"] if el["tag"] == "action")
        self.assertEqual(len(action["actions"]), 2)
        self.assertEqual(action["actions"][0]["value"]["callback_data"], "pick:abc12345:0")


if __name__ == "__main__":
    unittest.main()
