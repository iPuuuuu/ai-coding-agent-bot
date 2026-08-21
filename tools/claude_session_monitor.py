"""Read-only discovery of Claude Code transcript sessions.

Claude Code writes JSONL transcripts below ``~/.claude/projects``.  The hook
collector is the preferred real-time source, but this scanner is deliberately
independent and provides a useful startup/failure fallback.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
ACTIVE_GRACE_SECONDS = 180


def _timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return fallback
    return fallback


def _text_from_message(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _read_session(path: Path) -> dict[str, Any] | None:
    try:
        mtime = path.stat().st_mtime
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    session_id = ""
    cwd = ""
    title = ""
    last_prompt = ""
    last_event = ""
    last_event_at = mtime
    created_at = mtime
    saw_stop = False
    waiting = False
    waiting_reason = ""
    for raw in lines[-240:]:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        session_id = str(item.get("sessionId") or item.get("session_id") or session_id)
        timestamp = _timestamp(item.get("timestamp"), mtime)
        created_at = min(created_at, timestamp)
        if item.get("type") == "user":
            message = item.get("message")
            text = _text_from_message(message)
            if text:
                last_prompt = text[:500]
                if not title:
                    title = text[:80]
        if item.get("type") == "last-prompt":
            last_prompt = str(item.get("prompt") or item.get("content") or last_prompt)[:500]
        if item.get("cwd"):
            cwd = str(item["cwd"])
        origin = item.get("origin")
        if isinstance(origin, dict) and origin.get("cwd"):
            cwd = str(origin["cwd"])
        if item.get("hookEvent"):
            last_event = str(item.get("hookEvent"))
            last_event_at = timestamp
        if item.get("type") in {"tool_result", "result"}:
            last_event = str(item.get("type"))
            last_event_at = timestamp
        attachment = item.get("attachment")
        if isinstance(attachment, dict) and attachment.get("hookEvent"):
            last_event = str(attachment["hookEvent"])
            last_event_at = timestamp
        if item.get("type") == "stop_hook_summary" or str(item.get("hookEvent") or "").lower() == "stop":
            saw_stop = True
        text = _text_from_message(item.get("message"))
        low = text.lower()
        if "waiting for your input" in low or "please choose" in low or "请确认" in text or "请选择" in text:
            waiting = True
            waiting_reason = text[:500]
    if not session_id:
        session_id = path.stem
    age = max(0.0, time.time() - mtime)
    if saw_stop:
        status = "completed"
    elif age <= ACTIVE_GRACE_SECONDS:
        status = "active"
    else:
        status = "stale"
    return {
        "source": "claude",
        "session_id": session_id,
        "cwd": cwd,
        "title": title or last_event or "Claude 会话",
        "created_at": created_at,
        "updated_at": mtime,
        "status": "waiting" if waiting and status == "active" else status,
        "last_event": last_event or "transcript_updated",
        "last_event_at": last_event_at,
        "activity_age_seconds": int(age),
        "last_prompt": last_prompt,
        "waiting": waiting and status == "active",
        "waiting_reason": waiting_reason,
        "transcript_path": str(path),
        "controllable": False,
    }


def scan_sessions(projects_dir: str | os.PathLike[str] | None = None, limit: int = 120) -> dict[str, dict[str, Any]]:
    root = Path(projects_dir) if projects_dir else DEFAULT_PROJECTS_DIR
    if not root.exists():
        return {}
    try:
        files = [p for p in root.rglob("*.jsonl") if "/subagents/" not in p.as_posix()]
        files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return {}
    sessions: dict[str, dict[str, Any]] = {}
    for path in files:
        record = _read_session(path)
        if record:
            sessions[record["session_id"]] = record
    return sessions
