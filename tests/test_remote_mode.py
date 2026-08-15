"""Unit tests for the remote-execution mode (bot on a server, Codex at home)."""
from __future__ import annotations

import unittest
from unittest import mock

import agent_runner
import config
import tools.codex_session_monitor as csm


class RemoteCommandTest(unittest.TestCase):
    def setUp(self):
        self._remote = config.CODEX_REMOTE
        self._target = config.CODEX_SSH_TARGET
        self._extra = config.CODEX_SSH_EXTRA_ARGS
        config.CODEX_REMOTE = True
        config.CODEX_SSH_TARGET = "-p 2200 a1234@localhost"
        config.CODEX_SSH_EXTRA_ARGS = "-i /root/.ssh/id_ed25519"

    def tearDown(self):
        config.CODEX_REMOTE = self._remote
        config.CODEX_SSH_TARGET = self._target
        config.CODEX_SSH_EXTRA_ARGS = self._extra

    def test_remote_command_wraps_codex_argv(self):
        codex_argv = ["codex", "exec", "--json", "-C", "/Users/a1234/proj", "任务：加\n个换行 \"引号\" 与 $符号"]
        cmd = agent_runner.build_remote_command(codex_argv, cwd="/Users/a1234/proj")
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("-p", cmd)
        self.assertIn("2200", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("a1234@localhost", cmd)
        # The last element is the remote shell script.
        script = cmd[-1]
        self.assertIn("cd /Users/a1234/proj", script)
        self.assertIn("codex exec --json", script)
        # Prompt round-trips: single-quoted inside the script.
        self.assertIn("'任务：加", script)

    def test_remote_command_without_cwd(self):
        codex_argv = ["codex", "--version"]
        cmd = agent_runner.build_remote_command(codex_argv)
        self.assertNotIn("cd ", cmd[-1])


class RemoteScanTest(unittest.TestCase):
    def test_empty_target_returns_empty(self):
        self.assertEqual(csm.scan_sessions_remote(ssh_target=""), {})

    def test_remote_script_is_valid_python(self):
        compile(csm._REMOTE_SCAN_SCRIPT, "<remote-scan>", "exec")

    def test_parses_ndjson_output(self):
        import json
        fake_row = {
            "source": "codex", "session_id": "abc123456789", "cwd": "/Users/a1234/p",
            "title": "", "created_at": 1.0, "updated_at": 2.0, "status": "completed",
            "last_event": "task_complete", "last_event_at": 2.0,
            "activity_age_seconds": 0, "rollout_path": "/Users/a1234/.codex/sessions/x/rollout-1.jsonl",
        }
        payload = json.dumps(fake_row).encode()

        with mock.patch("subprocess.run") as run:
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = payload
            run.return_value = proc
            sessions = csm.scan_sessions_remote(ssh_target="-p 2200 a1234@localhost")
        self.assertEqual(sessions["abc123456789"]["status"], "completed")
        self.assertEqual(sessions["abc123456789"]["cwd"], "/Users/a1234/p")

    def test_failed_ssh_returns_empty(self):
        with mock.patch("subprocess.run") as run:
            proc = mock.Mock()
            proc.returncode = 1
            proc.stdout = b""
            run.return_value = proc
            sessions = csm.scan_sessions_remote(ssh_target="-p 2200 a1234@localhost")
        self.assertEqual(sessions, {})


if __name__ == "__main__":
    unittest.main()
