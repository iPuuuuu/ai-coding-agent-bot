from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.claude_session_monitor import scan_sessions


class ClaudeTranscriptMonitorTest(unittest.TestCase):
    def test_scans_session_metadata_without_subagent_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "-Users-demo" / "abc.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                "\n".join([
                    json.dumps({"type": "user", "message": {"role": "user", "content": "修复登录"}, "sessionId": "abc", "cwd": "/tmp/demo"}),
                    json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "我会检查项目"}]}, "sessionId": "abc"}),
                ]),
                encoding="utf-8",
            )
            sub = root / "-Users-demo" / "subagents" / "agent-x.jsonl"
            sub.parent.mkdir(parents=True)
            sub.write_text(json.dumps({"sessionId": "agent-x"}), encoding="utf-8")
            rows = scan_sessions(root)
            self.assertIn("abc", rows)
            self.assertNotIn("agent-x", rows)
            self.assertEqual(rows["abc"]["source"], "claude")
            self.assertEqual(rows["abc"]["cwd"], "/tmp/demo")
            self.assertFalse(rows["abc"]["controllable"])


if __name__ == "__main__":
    unittest.main()
