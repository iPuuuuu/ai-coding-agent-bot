"""Unit tests for the Codex agent runner (no network, no model calls)."""
from __future__ import annotations

import asyncio
import unittest

import agent_runner
import channel


class BuildCommandTest(unittest.TestCase):
    def setUp(self):
        # Pin the sandbox value so the tests do not depend on ambient env.
        self._sandbox = agent_runner.config.CODEX_SANDBOX
        agent_runner.config.CODEX_SANDBOX = "workspace-write"

    def tearDown(self):
        agent_runner.config.CODEX_SANDBOX = self._sandbox

    def test_new_session_command(self):
        cmd = agent_runner.build_codex_command("hello", "/tmp/proj", None)
        self.assertTrue(cmd[0].endswith("codex"))
        self.assertEqual(cmd[1:4], ["exec", "--sandbox", "workspace-write"])
        self.assertIn("workspace-write", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("-C", cmd)
        self.assertIn("/tmp/proj", cmd)
        self.assertTrue(cmd[-1].endswith("hello"))
        self.assertNotIn("--ask-for-approval", cmd)

    def test_resume_command(self):
        cmd = agent_runner.build_codex_command("continue", "/tmp/proj", "abc-123")
        self.assertTrue(cmd[0].endswith("codex"))
        self.assertEqual(cmd[1:5], ["exec", "resume", "abc-123", "--json"])
        self.assertIn("--json", cmd)
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("-C", cmd)
        self.assertEqual(cmd[-1], "continue")

    def test_model_flag_when_configured(self):
        original = agent_runner.config.CODEX_MODEL
        try:
            agent_runner.config.CODEX_MODEL = "gpt-test"
            cmd = agent_runner.build_codex_command("hi", "/tmp/proj", None)
            self.assertIn("-m", cmd)
            self.assertIn("gpt-test", cmd)
        finally:
            agent_runner.config.CODEX_MODEL = original


class ParseOptionsTest(unittest.TestCase):
    def test_numbered_options(self):
        text = "请选择：\n1. 先查日志\n2. 直接修配置\n3. 先跑测试"
        self.assertEqual(
            agent_runner._parse_options(text),
            ["先查日志", "直接修配置", "先跑测试"],
        )

    def test_letter_and_bullet_options(self):
        self.assertEqual(
            agent_runner._parse_options("a) 方案A\nb) 方案B\nc) 方案C\nd) 方案D"),
            ["方案A", "方案B", "方案C", "方案D"],
        )

    def test_too_few_or_too_many_options(self):
        self.assertEqual(agent_runner._parse_options("1. 只有一个"), [])
        self.assertEqual(agent_runner._parse_options("1. a\n2. b\n3. c\n4. d\n5. e"), [])

    def test_dedupe(self):
        self.assertEqual(
            agent_runner._parse_options("1. 同样\n2. 同样\n3. 不同"),
            ["同样", "不同"],
        )


class QuestionDetectionTest(unittest.TestCase):
    def test_question_marks(self):
        self.assertTrue(agent_runner._looks_like_question("你希望怎么做？"))
        self.assertTrue(agent_runner._looks_like_question("Which option do you want?"))

    def test_ask_phrases(self):
        self.assertTrue(agent_runner._looks_like_question("请选择一种方式"))
        self.assertTrue(agent_runner._looks_like_question("需要你补充一下需求"))

    def test_plain_statement(self):
        self.assertFalse(agent_runner._looks_like_question("已完成重构，测试通过。"))


class ForwardDedupeTest(unittest.TestCase):
    def test_duplicate_reply_suppressed(self):
        agent_runner._clear_forwarded_reply("sess-1")
        self.assertTrue(agent_runner._should_forward_reply("sess-1", "同一段回复"))
        self.assertFalse(agent_runner._should_forward_reply("sess-1", "同一段回复"))
        self.assertTrue(agent_runner._should_forward_reply("sess-1", "新的回复"))


class HandleStreamLineTest(unittest.TestCase):
    def test_verified_codex_event_stream(self):
        # Replay the exact JSONL a real Codex CLI 0.147.0 emitted.
        lines = [
            '{"type":"thread.started","thread_id":"019ff5ea-6df9-74a0-baa5-a3239495713b"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":1}}',
        ]

        async def run():
            outcome = agent_runner.TurnOutcome()
            for raw in lines:
                await agent_runner._handle_stream_line(raw, outcome)
            return outcome

        outcome = asyncio.run(run())
        self.assertEqual(outcome.session_id, "019ff5ea-6df9-74a0-baa5-a3239495713b")
        self.assertEqual(outcome.final_text, "OK")
        self.assertEqual(outcome.state, "completed")
        self.assertTrue(any(event.kind == "system/init" for event in outcome.events))
        self.assertTrue(any(event.kind == "assistant/text" for event in outcome.events))

    def test_waiting_detected_from_options(self):
        line = (
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message",'
            '"text":"请选择：\\n1. 方案A\\n2. 方案B"}}'
        )

        async def run():
            outcome = agent_runner.TurnOutcome()
            waiting = await agent_runner._handle_stream_line(line, outcome)
            return waiting, outcome

        waiting, outcome = asyncio.run(run())
        self.assertTrue(waiting)
        self.assertEqual(outcome.choice_options, ["方案A", "方案B"])

    def test_turn_failed_raises(self):
        line = '{"type":"turn.failed","error":"boom"}'

        async def run():
            outcome = agent_runner.TurnOutcome()
            await agent_runner._handle_stream_line(line, outcome)

        with self.assertRaises(agent_runner.AgentRunnerError):
            asyncio.run(run())


class StreamNoiseTest(unittest.TestCase):
    """Internal Codex events must never be forwarded to the phone."""

    def test_internal_events_not_forwarded(self):
        sent: list[str] = []
        original = channel.send_status

        async def fake_send_status(text: str):
            sent.append(text)

        channel.send_status = fake_send_status
        try:
            async def run():
                outcome = agent_runner.TurnOutcome()
                for raw in (
                    '{"type":"item.started"}',
                    '{"type":"turn.started"}',
                    '{"type":"item.completed","item":{"id":"x","type":"reasoning","summary":[],"content":[]}}',
                ):
                    await agent_runner._handle_stream_line(raw, outcome)
            asyncio.run(run())
        finally:
            channel.send_status = original
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
