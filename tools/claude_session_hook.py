"""Claude 全局 hook：把各会话事件写入共享状态文件。"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

MONITOR_DIR = Path(os.getenv("CLAUDE_SESSION_MONITOR_DIR", "C:/Users/wmh/.claude/session-monitor"))
EVENT_LOG = MONITOR_DIR / "events.jsonl"
SNAPSHOT_FILE = MONITOR_DIR / "sessions.json"
MAX_EVENTS = 200


QUESTION_MARKERS = [
    "请选择",
    "请确认",
    "需要你",
    "告诉我",
    "would you like",
    "do you want",
    "please choose",
    "please confirm",
]


def _safe_load_stdin() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def _now() -> float:
    return time.time()


def _norm_path(value: str) -> str:
    return os.path.normpath(value or "").replace("\\", "/")


def _session_id(data: dict[str, Any]) -> str:
    for key in ("session_id", "sessionId"):
        if data.get(key):
            return str(data[key])
    return ""


def _cwd(data: dict[str, Any]) -> str:
    for key in ("cwd", "working_directory", "workingDirectory"):
        if data.get(key):
            return _norm_path(str(data[key]))
    return _norm_path(os.getcwd())


def _event_name(data: dict[str, Any]) -> str:
    name = str(data.get("hook_event_name") or data.get("hookEventName") or data.get("event") or "unknown")
    tool = str(data.get("tool_name") or data.get("toolName") or "")
    if tool:
        return f"{name}:{tool}"
    return name


def _extract_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("prompt", "stopReason", "reason", "systemMessage"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "prompt"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}={value.strip()}")
                break
    if not parts and data:
        try:
            parts.append(json.dumps(data, ensure_ascii=False)[:500])
        except Exception:
            parts.append(str(data)[:500])
    return " | ".join(parts)[:1000]


def _looks_waiting(event_name: str, text: str) -> bool:
    low_event = event_name.lower()
    low_text = text.lower()
    if "notification" in low_event:
        return True
    if "?" in text or "？" in text:
        return True
    return any(marker in low_text for marker in QUESTION_MARKERS)


def _parse_options(text: str) -> list[str]:
    options: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(\d+[\).]|[A-Da-d][\).]|[-*•])\s+", line):
            cleaned = re.sub(r"^(\d+[\).]|[A-Da-d][\).]|[-*•])\s+", "", line).strip()
            if cleaned and cleaned not in options:
                options.append(cleaned[:80])
    return options[:4] if 2 <= len(options) <= 4 else []


def _extract_transcript_text(payload: dict[str, Any]) -> str:
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-80:]
    except Exception:
        return ""
    for raw in reversed(lines):
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if item.get("type") == "assistant":
            message = item.get("message", {})
            content = message.get("content", [])
            parts = []
            for block in content:
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        parts.append(text)
            if parts:
                return "\n\n".join(parts).strip()[:2000]
        if item.get("type") == "result":
            result = (item.get("result") or "").strip()
            if result:
                return result[:2000]
    return ""


def _load_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_FILE.exists():
        return {"sessions": {}}
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sessions": {}}


def _save_snapshot(snapshot: dict[str, Any]):
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_event(entry: dict[str, Any]):
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _trim_events(snapshot: dict[str, Any], session: dict[str, Any], entry: dict[str, Any]):
    events = session.setdefault("events", [])
    events.append(entry)
    if len(events) > MAX_EVENTS:
        session["events"] = events[-MAX_EVENTS:]
    snapshot["updated_at"] = entry["timestamp"]


def main():
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    payload = _safe_load_stdin()
    ts = _now()
    session_id = _session_id(payload)
    cwd = _cwd(payload)
    event_name = _event_name(payload)
    text = _extract_text(payload)
    transcript_text = _extract_transcript_text(payload)
    if transcript_text:
        text = transcript_text

    entry = {
        "timestamp": ts,
        "session_id": session_id,
        "cwd": cwd,
        "event": event_name,
        "text": text,
    }
    _append_event(entry)

    if not session_id:
        print(json.dumps({"ok": True}))
        return

    snapshot = _load_snapshot()
    sessions = snapshot.setdefault("sessions", {})
    session = sessions.setdefault(
        session_id,
        {
            "session_id": session_id,
            "cwd": cwd,
            "created_at": ts,
            "updated_at": ts,
            "status": "active",
            "events": [],
        },
    )
    session["cwd"] = cwd or session.get("cwd", "")
    session["updated_at"] = ts
    session["last_event"] = event_name
    session["last_text"] = text
    session["choice_options"] = _parse_options(text)
    session["waiting"] = _looks_waiting(event_name, text)
    if session["waiting"]:
        session["waiting_reason"] = text[:300]
    elif event_name.lower().startswith("stop"):
        session["waiting_reason"] = ""
    if event_name.lower().startswith("stop"):
        session["status"] = "stopped"
        session["stopped_at"] = ts
    else:
        session["status"] = "active"
    _trim_events(snapshot, session, entry)
    _save_snapshot(snapshot)
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
