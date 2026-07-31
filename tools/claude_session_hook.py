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

IGNORED_NOTIFICATION_TYPES = {"idle_prompt"}
GENERIC_WAIT_MESSAGES = {
    "claude is waiting for your input",
    "waiting for your input",
}


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


def _transcript_path(data: dict[str, Any]) -> str:
    value = data.get("transcript_path") or data.get("transcriptPath") or ""
    return str(value)


def _notification_type(data: dict[str, Any]) -> str:
    return str(data.get("notification_type") or data.get("notificationType") or "")


def _event_name(data: dict[str, Any]) -> str:
    name = str(data.get("hook_event_name") or data.get("hookEventName") or data.get("event") or "unknown")
    tool = str(data.get("tool_name") or data.get("toolName") or "")
    if tool:
        return f"{name}:{tool}"
    return name


def _extract_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("message", "prompt", "stopReason", "reason", "systemMessage"):
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


def _clean_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if text.startswith("{") and '"session_id"' in text and '"message"' in text:
        return ""
    if text.lower() in GENERIC_WAIT_MESSAGES:
        return ""
    return text[:2000]


def _looks_waiting(event_name: str, notification_type: str, text: str) -> bool:
    low_event = event_name.lower()
    low_notification = notification_type.lower()
    low_text = text.lower()
    if low_notification in IGNORED_NOTIFICATION_TYPES:
        return False
    if low_notification == "permission_prompt":
        return True
    if "notification" in low_event and text.strip() and low_text not in GENERIC_WAIT_MESSAGES:
        return True
    if low_text in GENERIC_WAIT_MESSAGES:
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
            if cleaned and len(cleaned) <= 80 and cleaned not in options:
                options.append(cleaned)
    return options[:4] if 2 <= len(options) <= 4 else []


def _extract_transcript_text(transcript_path: str) -> str:
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-160:]
    except Exception:
        return ""

    candidates: list[str] = []
    for raw in reversed(lines):
        try:
            item = json.loads(raw)
        except Exception:
            continue

        item_type = item.get("type")
        text_parts: list[str] = []

        if item_type == "assistant":
            message = item.get("message", {})
            content = message.get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        text_parts.append(text)
        elif item_type == "result":
            result = (item.get("result") or "").strip()
            if result:
                text_parts.append(result)
        else:
            message = item.get("message") or {}
            if isinstance(message, dict):
                content = message.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                text_parts.append(text)

        merged = _clean_text("\n\n".join(text_parts).strip())
        if not merged:
            continue
        if _parse_options(merged) or "?" in merged or "？" in merged or any(marker in merged.lower() for marker in QUESTION_MARKERS):
            return merged
        candidates.append(merged)

    return candidates[0][:2000] if candidates else ""


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
    transcript_path = _transcript_path(payload)
    notification_type = _notification_type(payload)
    event_name = _event_name(payload)
    text = _clean_text(_extract_text(payload))
    transcript_text = _extract_transcript_text(transcript_path)
    if transcript_text:
        text = transcript_text

    waiting = _looks_waiting(event_name, notification_type, text)
    choice_options = _parse_options(text) if waiting else []
    if notification_type == "permission_prompt" and not text:
        text = "Claude 正在等待权限确认，请在电脑端或 Telegram 上继续操作。"
        waiting = True

    entry = {
        "timestamp": ts,
        "session_id": session_id,
        "cwd": cwd,
        "event": event_name,
        "text": text,
        "notification_type": notification_type,
        "transcript_path": transcript_path,
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
    session["notification_type"] = notification_type
    session["transcript_path"] = transcript_path
    session["choice_options"] = choice_options
    session["waiting"] = waiting
    if session["waiting"]:
        session["waiting_reason"] = text[:300] if text else "Claude 正在等待你的输入"
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
