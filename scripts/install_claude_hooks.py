#!/usr/bin/env python3
"""Install the project's Claude Code monitoring hook into user settings.

The script preserves unrelated settings and writes atomically. Run it with the
same user that launches Claude Code, then use check_claude_hooks.py to verify.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / "tools" / "claude_session_hook.py"
SETTINGS = Path.home() / ".claude" / "settings.json"

EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]


def main() -> int:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"无法解析 {SETTINGS}: {exc}", file=sys.stderr)
            return 2
    if not isinstance(data, dict):
        print(f"{SETTINGS} 不是 JSON 对象", file=sys.stderr)
        return 2
    hooks = data.setdefault("hooks", {})
    command = f'python3 "{HOOK}"'
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        if not any(command in json.dumps(item, ensure_ascii=False) for item in entries):
            entries.append({"hooks": [{"type": "command", "command": command}]})
    fd, temp_name = tempfile.mkstemp(prefix="settings.", suffix=".json", dir=str(SETTINGS.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if SETTINGS.exists():
            shutil.copymode(SETTINGS, temp_name)
        os.replace(temp_name, SETTINGS)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(f"已安装 Claude 会话监控 Hook：{SETTINGS}")
    print(f"监控脚本：{HOOK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
