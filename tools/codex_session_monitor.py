"""Read-only discovery of local Codex CLI sessions.

Codex stores every rollout as JSONL under ``~/.codex/sessions``.  This module
only reads the metadata and event *types* needed for monitoring; it never
copies prompts, model replies, tool input, or any secret from a rollout.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
ACTIVE_GRACE_SECONDS = 180


def _as_timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            # Codex records RFC3339 timestamps, including a terminal Z.
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _read_rollout(path: Path) -> dict[str, Any] | None:
    """Return a privacy-preserving status summary for one Codex rollout."""
    try:
        modified_at = path.stat().st_mtime
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    session_id = ""
    cwd = ""
    title = ""
    created_at = modified_at
    last_event = ""
    last_event_at = modified_at
    last_task_event = ""

    for raw in lines:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        event_type = str(payload.get("type") or "")
        timestamp = _as_timestamp(item.get("timestamp"), modified_at)

        if item.get("type") == "session_meta":
            session_id = str(payload.get("session_id") or payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            title = str(payload.get("thread_name") or title)
            created_at = _as_timestamp(payload.get("timestamp") or item.get("timestamp"), created_at)
        elif item.get("type") == "event_msg" and event_type:
            # Deliberately retain only the event label, never event text.
            last_event = event_type
            last_event_at = timestamp
            if event_type in {"task_started", "task_complete", "turn_aborted"}:
                last_task_event = event_type

    if not session_id:
        # The filename ends in the thread id on supported Codex versions.
        session_id = path.stem.rsplit("-", 5)[-1] if path.stem else path.name

    now = time.time()
    age = max(0.0, now - modified_at)
    if last_task_event == "task_complete":
        status = "completed"
    elif last_task_event == "turn_aborted":
        status = "stopped"
    elif last_task_event == "task_started":
        status = "running" if age <= ACTIVE_GRACE_SECONDS else "stale"
    else:
        status = "unknown"

    return {
        "source": "codex",
        "session_id": session_id,
        "cwd": cwd,
        "title": title,
        "created_at": created_at,
        "updated_at": modified_at,
        "status": status,
        "last_event": last_event or "unknown",
        "last_event_at": last_event_at,
        "activity_age_seconds": int(age),
        "rollout_path": str(path),
    }


def scan_sessions(sessions_dir: str | os.PathLike[str] | None = None, limit: int = 60) -> dict[str, dict[str, Any]]:
    """Scan recent Codex rollouts and return session id -> summary.

    ``limit`` bounds work on machines with long local history while still
    always considering the most recently changed rollouts, including active
    ones.
    """
    root = Path(sessions_dir) if sessions_dir else DEFAULT_SESSIONS_DIR
    if not root.exists():
        return {}
    try:
        files = sorted(root.rglob("rollout-*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return {}

    sessions: dict[str, dict[str, Any]] = {}
    for path in files:
        summary = _read_rollout(path)
        if summary:
            sessions[summary["session_id"]] = summary
    return sessions
