#!/usr/bin/env python3
"""Report whether the user Claude settings contain this project's hook."""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = str(PROJECT_ROOT / "tools" / "claude_session_hook.py")
SETTINGS = Path.home() / ".claude" / "settings.json"


def main() -> int:
    if not SETTINGS.exists():
        print(f"未找到 Claude settings：{SETTINGS}")
        return 1
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Claude settings 无法解析：{exc}")
        return 2
    raw = json.dumps(data.get("hooks", {}), ensure_ascii=False)
    ok = HOOK in raw
    print(f"Claude settings：{SETTINGS}")
    print(f"监控脚本：{HOOK}")
    print("Hook 状态：已注册" if ok else "Hook 状态：未注册")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
