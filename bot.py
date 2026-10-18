"""
bot.py —— 主入口：Telegram 桥接层。

职责：
- 收你的文字消息 → 交给 Claude 跑（新会话 / 继续已有会话）
- 处理按钮选择 → 继续对应的托管会话
- 命令：/start /project /status /stop /help /new /sessions /use
- 白名单：只有你的 chat_id 能指挥

运行：  py -3.11 bot.py
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import agent_runner
import channel
import config
import single_instance
import web_dashboard
from tools.codex_session_monitor import scan_sessions as scan_codex_sessions
from tools.claude_session_monitor import scan_sessions as scan_claude_transcripts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join("logs", "bot.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("bot")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "state.json")


@dataclass
class TranscriptEntry:
    kind: str
    text: str
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class PendingInteraction:
    project_path: str
    project_label: str
    session_id: str
    prompt_text: str
    options: list[str] = field(default_factory=list)
    source: str = "text"  # text | buttons
    engine: str = "codex"  # codex | claude
    controllable: bool = True


@dataclass
class QueuedTask:
    prompt: str
    project_path: str
    session_id: str
    force_new: bool = False
    task_number: int = 0
    queued_at: float = field(default_factory=time.time)
    # Which run this task targets: the session id for an existing session, or
    # a ``new:<n>`` key while a fresh session is still being created.
    session_key: str = ""


@dataclass
class ManagedSession:
    session_id: str
    project_path: str
    project_label: str
    title: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_prompt: str = ""
    last_event: str = "新会话"
    mode: str = "idle"

    @property
    def short_id(self) -> str:
        return self.session_id[:8] if self.session_id else "new"

    def display_name(self) -> str:
        return f"{self.project_label}/{self.short_id}"


@dataclass
class ProjectSnapshot:
    path: str
    label: str
    mode: str = "idle"
    last_event: str = "空闲"
    last_error: str = ""
    waiting_reason: str = ""
    active_prompt: str = ""
    last_task_id: Optional[int] = None
    last_active_at: Optional[float] = None
    transcript: list[TranscriptEntry] = field(default_factory=list)

    def add_entry(self, kind: str, text: str, session_id: str = ""):
        text = text.strip()
        if not text:
            return
        self.transcript.append(TranscriptEntry(kind=kind, text=text, session_id=session_id))
        if len(self.transcript) > 80:
            self.transcript = self.transcript[-80:]

    def summary_line(self, is_active: bool = False) -> str:
        marker = "👉" if is_active else "•"
        wait = f" | 等待：{self.waiting_reason[:24]}" if self.waiting_reason else ""
        err = f" | 错误：{self.last_error[:24]}" if self.last_error else ""
        task = f" | 任务#{self.last_task_id}" if self.last_task_id else ""
        return f"{marker} {self.label}: {self.mode}{task} | {self.last_event[:32]}{wait}{err}"


@dataclass
class RuntimeState:
    current_cwd: str = config.DEFAULT_PROJECT_DIR
    current_session_id: str = ""
    mode: str = "idle"
    active_prompt: str = ""
    started_at: Optional[float] = None
    last_event: str = "空闲"
    last_error: str = ""
    waiting_reason: str = ""
    stop_requested: bool = False
    task_counter: int = 0
    current_task_id: Optional[int] = None
    current_project_label: str = ""
    projects: dict[str, ProjectSnapshot] = field(default_factory=dict)
    sessions: dict[str, ManagedSession] = field(default_factory=dict)
    project_last_session: dict[str, str] = field(default_factory=dict)
    pending: Optional[PendingInteraction] = None
    # One pending interaction per session: several sessions may be waiting for
    # user input at the same time.  ``pending`` above mirrors the focused one.
    pending_by_session: dict[str, PendingInteraction] = field(default_factory=dict)
    start_new_session_next: bool = False
    new_session_project: str = ""
    new_session_expires_at: float = 0.0
    message_bindings: dict[str, dict] = field(default_factory=dict)
    task_queue: list[QueuedTask] = field(default_factory=list)
    active_task: Optional[dict] = None

    def ensure_project(self, path: str) -> ProjectSnapshot:
        norm = _norm_path(path)
        snapshot = self.projects.get(norm)
        if snapshot is None:
            snapshot = ProjectSnapshot(path=norm, label=_project_label(norm))
            self.projects[norm] = snapshot
        return snapshot

    def set_current_project(self, path: str):
        norm = _norm_path(path)
        snap = self.ensure_project(norm)
        self.current_cwd = norm
        self.current_project_label = snap.label
        session_id = self.project_last_session.get(norm, "")
        if session_id and session_id in self.sessions:
            self.current_session_id = session_id
        self.last_event = f"切换项目：{snap.label}"
        snap.last_event = self.last_event
        snap.last_active_at = time.time()
        snap.add_entry("system", self.last_event)

    def begin_new_session(self, project_path: str, ttl_seconds: int = 600) -> None:
        """Enter an explicit, expiring new-session draft state."""
        norm = _norm_path(project_path)
        self.set_current_project(norm)
        self.current_session_id = ""
        self.start_new_session_next = True
        self.new_session_project = norm
        self.new_session_expires_at = time.time() + ttl_seconds

    def cancel_new_session(self) -> None:
        self.start_new_session_next = False
        self.new_session_project = ""
        self.new_session_expires_at = 0.0

    def new_session_pending(self) -> bool:
        if not self.start_new_session_next:
            return False
        if not self.new_session_project or self.new_session_expires_at <= time.time():
            self.cancel_new_session()
            return False
        return True

    def bind_message(self, message_key: str, session_id: str, project_path: str, message_type: str) -> None:
        if not message_key or not session_id:
            return
        self.message_bindings[message_key] = {
            "session_id": session_id,
            "project_path": _norm_path(project_path),
            "message_type": message_type,
            "created_at": time.time(),
        }
        if len(self.message_bindings) > 1000:
            oldest = sorted(
                self.message_bindings,
                key=lambda key: float(self.message_bindings[key].get("created_at", 0)),
            )[: len(self.message_bindings) - 1000]
            for key in oldest:
                self.message_bindings.pop(key, None)

    def bind_session(self, session_id: str, project_path: str, initial_prompt: str = "", set_focus: bool = True) -> ManagedSession:
        norm = _norm_path(project_path)
        snap = self.ensure_project(norm)
        session = self.sessions.get(session_id)
        if session is None:
            title = (initial_prompt or "新会话").strip()[:48] or "新会话"
            session = ManagedSession(
                session_id=session_id,
                project_path=norm,
                project_label=snap.label,
                title=title,
            )
            self.sessions[session_id] = session
        session.project_path = norm
        session.project_label = snap.label
        if initial_prompt and not session.last_prompt:
            session.last_prompt = initial_prompt.strip()
        session.updated_at = time.time()
        self.project_last_session[norm] = session_id
        if set_focus:
            self.current_session_id = session_id
        return session

    def set_pending(self, pending: PendingInteraction) -> None:
        """Register a waiting interaction for one session.

        Several sessions may wait for input at once; ``pending`` (the field)
        always mirrors the most recently registered one for display purposes.
        """
        self.pending_by_session[pending.session_id] = pending
        self.pending = pending

    def clear_pending(self, session_key: str) -> None:
        self.pending_by_session.pop(session_key, None)
        if self.pending is not None and self.pending.session_id == session_key:
            self.pending = None

    def get_current_session(self) -> Optional[ManagedSession]:
        if self.current_session_id:
            session = self.sessions.get(self.current_session_id)
            if session is not None:
                return session
        session_id = self.project_last_session.get(self.current_cwd, "")
        if session_id:
            return self.sessions.get(session_id)
        return None

    def choose_session_for_prompt(self, force_new: bool, target_session_id: str = "") -> str:
        """Decide which session a new message continues.

        ``target_session_id`` is the explicit @-routed session (empty = the
        focused session).  Returns "" when a brand-new session must be created.
        """
        if force_new:
            return ""
        target = target_session_id or self.current_session_id
        if target:
            return target
        session = self.get_current_session()
        return session.session_id if session else ""

    def start_task(
        self,
        prompt: str,
        project_path: str,
        session_id: str,
        force_new: bool,
        session_key: str = "",
        select_target: bool = True,
    ):
        norm = _norm_path(project_path)
        snap = self.ensure_project(norm)
        self.task_counter += 1
        self.current_task_id = self.task_counter
        if select_target:
            self.current_cwd = norm
            self.current_project_label = snap.label
            self.current_session_id = session_id
        self.active_prompt = prompt.strip()
        self.started_at = time.time()
        self.mode = "running"
        self.stop_requested = False
        self.waiting_reason = ""
        self.last_error = ""
        if select_target or force_new:
            self.cancel_new_session()
        self.active_task = {
            "prompt": self.active_prompt,
            "project_path": norm,
            "session_id": session_id or "",
            "session_key": session_key or session_id or "",
            "task_id": self.current_task_id,
        }
        action = "新会话" if force_new or not session_id else f"继续会话 {session_id[:8]}"
        self.last_event = f"{snap.label} 开始处理（{action}）"
        snap.mode = "running"
        snap.last_event = self.last_event
        snap.last_error = ""
        snap.waiting_reason = ""
        snap.active_prompt = self.active_prompt
        snap.last_task_id = self.current_task_id
        snap.last_active_at = self.started_at
        snap.add_entry("user", self.active_prompt, session_id)
        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            session.last_prompt = self.active_prompt
            session.updated_at = self.started_at
            session.mode = "running"
            session.last_event = self.last_event

    def mark_waiting(self, pending: PendingInteraction):
        snap = self.ensure_project(pending.project_path)
        self.mode = "waiting_user_reply"
        self.set_pending(pending)
        self.waiting_reason = pending.prompt_text[:120]
        self.last_event = f"{pending.project_label} 等待你的回复"
        snap.mode = "waiting_user_reply"
        snap.waiting_reason = self.waiting_reason
        snap.last_event = self.last_event
        snap.last_active_at = time.time()
        self.bind_session(pending.session_id, pending.project_path, set_focus=False)
        session = self.sessions[pending.session_id]
        session.mode = "waiting_user_reply"
        session.last_event = self.last_event
        session.updated_at = time.time()

    def mark_running(self, event: str, project_path: Optional[str] = None, session_id: str = ""):
        target_path = _norm_path(project_path or self.current_cwd)
        snap = self.ensure_project(target_path)
        self.mode = "running"
        self.waiting_reason = ""
        self.last_event = event
        if session_id:
            self.clear_pending(session_id)
        snap.mode = "running"
        snap.waiting_reason = ""
        snap.last_event = event
        snap.last_active_at = time.time()
        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            session.mode = "running"
            session.last_event = event
            session.updated_at = time.time()

    def mark_done(self, event: str = "任务完成", project_path: str = "", session_id: str = ""):
        target_path = _norm_path(project_path or self.current_cwd)
        snap = self.ensure_project(target_path)
        self.mode = "idle"
        self.last_event = event
        self.waiting_reason = ""
        self.active_prompt = ""
        self.started_at = None
        self.stop_requested = False
        self.current_task_id = None
        self.active_task = None
        snap.mode = "idle"
        snap.last_event = event
        snap.waiting_reason = ""
        snap.active_prompt = ""
        snap.last_active_at = time.time()
        target_session_id = session_id or self.current_session_id
        snap.add_entry("system", event, target_session_id)
        session = self.sessions.get(target_session_id)
        if session is not None:
            session.mode = "idle"
            session.last_event = event
            session.updated_at = time.time()

    def mark_error(self, error: str, project_path: str = "", session_id: str = ""):
        target_path = _norm_path(project_path or self.current_cwd)
        snap = self.ensure_project(target_path)
        self.mode = "idle"
        self.last_error = error
        self.last_event = error
        self.waiting_reason = ""
        self.active_prompt = ""
        self.started_at = None
        self.stop_requested = False
        self.current_task_id = None
        self.active_task = None
        snap.mode = "idle"
        snap.last_error = error
        snap.last_event = error
        snap.waiting_reason = ""
        snap.active_prompt = ""
        snap.last_active_at = time.time()
        target_session_id = session_id or self.current_session_id
        snap.add_entry("error", error, target_session_id)
        session = self.sessions.get(target_session_id)
        if session is not None:
            session.mode = "idle"
            session.last_event = error
            session.updated_at = time.time()

    def request_stop(self):
        snap = self.ensure_project(self.current_cwd)
        self.stop_requested = True
        self.last_event = "用户请求停止当前任务"
        snap.last_event = self.last_event
        snap.add_entry("system", self.last_event, self.current_session_id)
        session = self.get_current_session()
        if session is not None:
            session.last_event = self.last_event
            session.updated_at = time.time()

    def enqueue_task(self, prompt: str, project_path: str, session_id: str, force_new: bool, session_key: str = "") -> QueuedTask:
        """Add a task to the FIFO queue while its session is busy."""
        self.task_counter += 1
        task = QueuedTask(
            prompt=prompt.strip(),
            project_path=_norm_path(project_path),
            session_id=session_id,
            force_new=force_new,
            task_number=self.task_counter,
            session_key=session_key or session_id or "",
        )
        self.task_queue.append(task)
        self.last_event = f"任务 #{task.task_number} 已排队"
        snap = self.ensure_project(task.project_path)
        snap.last_event = self.last_event
        snap.add_entry("system", self.last_event, session_id)
        return task

    def pop_next_queued(self) -> QueuedTask | None:
        return self.task_queue.pop(0) if self.task_queue else None

    def clear_queue(self) -> int:
        count = len(self.task_queue)
        self.task_queue.clear()
        if count:
            self.last_event = f"已清空队列（移除 {count} 个任务）"
        return count

    def append_events(self, project_path: str, events: list[agent_runner.MirrorEvent]):
        snap = self.ensure_project(project_path)
        for event in events:
            snap.add_entry(event.kind, event.text, event.session_id)
            if event.session_id:
                # Register the session without stealing focus: several sessions
                # may emit events at the same time under the parallel model.
                self.bind_session(event.session_id, project_path, set_focus=False)
                session = self.sessions[event.session_id]
                session.last_event = event.text[:120] or event.kind
                session.updated_at = time.time()
                snap.last_active_at = time.time()

    def session_summary(self) -> str:
        if not self.sessions:
            return "暂无托管会话。\n发送任务会自动新建，或先用 /new 标记下一条消息开新会话。"
        lines = ["托管会话："]
        current_id = self.current_session_id
        items = sorted(self.sessions.values(), key=lambda item: item.updated_at, reverse=True)[:8]
        for session in items:
            marker = "👉" if session.session_id == current_id else "•"
            lines.append(
                f"{marker} {session.short_id} | {session.project_label} | {session.mode} | {session.title[:28]}"
            )
            lines.append(f"  工作区：{session.project_path}")
        lines.append("用 /use <session_id前8位> 切换；/new 让下一条消息强制开新会话。")
        return "\n".join(lines)

    def summary(self) -> str:
        duration = "-"
        if self.started_at:
            duration = f"{int(time.time() - self.started_at)}s"
        task_label = f"#{self.current_task_id}" if self.current_task_id else "无"
        prompt = self.active_prompt[:80] if self.active_prompt else "-"
        wait = self.waiting_reason or "-"
        current_session = self.get_current_session()
        session_label = current_session.short_id if current_session else "无"
        lines = [
            f"当前项目：{self.current_project_label or _project_label(self.current_cwd)}",
            f"当前会话：{session_label}",
            f"状态：{self.mode} | 当前任务：{task_label} | 持续：{duration}",
            f"任务内容：{prompt}",
            f"等待原因：{wait}",
            "项目面板：",
        ]
        current_norm = _norm_path(self.current_cwd)
        for path in sorted(self.projects):
            lines.append(self.projects[path].summary_line(is_active=(path == current_norm)))
        if self.sessions:
            lines.append(self.session_summary())
        lines.append("全局 Codex 会话：")
        lines.extend(_format_global_sessions())
        snap = self.ensure_project(self.current_cwd)
        if snap.transcript:
            lines.append("最近窗口：")
            for entry in snap.transcript[-6:]:
                if entry.text.startswith("事件："):
                    continue
                label = entry.session_id[:8] if entry.session_id else "-"
                lines.append(f"- [{entry.kind}/{label}] {entry.text[:100]}")
        return "\n".join(lines)


def _norm_path(path: str) -> str:
    return os.path.normpath(path).replace("\\", "/")


def _project_label(path: str) -> str:
    base = os.path.basename(_norm_path(path))
    return base or _norm_path(path)


def _dump_state(state: RuntimeState) -> dict:
    """Serialize managed sessions, project windows and focus for restart."""
    return {
        "version": 2,
        "current_cwd": state.current_cwd,
        "current_session_id": state.current_session_id,
        "new_session_project": state.new_session_project,
        "new_session_expires_at": state.new_session_expires_at,
        "message_bindings": dict(state.message_bindings),
        "project_last_session": dict(state.project_last_session),
        "task_counter": state.task_counter,
        "task_queue": [
            {
                "prompt": task.prompt,
                "project_path": task.project_path,
                "session_id": task.session_id,
                "force_new": task.force_new,
                "task_number": task.task_number,
                "queued_at": task.queued_at,
                "session_key": task.session_key,
            }
            for task in state.task_queue
        ],
        "active_task": dict(state.active_task) if state.active_task else None,
        "sessions": [
            {
                "session_id": session.session_id,
                "project_path": session.project_path,
                "project_label": session.project_label,
                "title": session.title,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_prompt": session.last_prompt,
                "last_event": session.last_event,
                "mode": session.mode,
            }
            for session in state.sessions.values()
        ],
        "projects": [
            {
                "path": snap.path,
                "label": snap.label,
                "last_event": snap.last_event,
                "last_error": snap.last_error,
                "last_active_at": snap.last_active_at,
                "transcript": [
                    {
                        "kind": entry.kind,
                        "text": entry.text,
                        "session_id": entry.session_id,
                        "timestamp": entry.timestamp,
                    }
                    for entry in snap.transcript
                ],
            }
            for snap in state.projects.values()
        ],
    }


def _apply_state(state: RuntimeState, data: dict) -> None:
    """Restore a previously dumped state into ``state`` (best-effort)."""
    if not isinstance(data, dict):
        return

    for item in data.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        try:
            session_id = str(item["session_id"])
        except (KeyError, TypeError):
            continue
        session = ManagedSession(
            session_id=session_id,
            project_path=_norm_path(item.get("project_path", config.DEFAULT_PROJECT_DIR)),
            project_label=str(item.get("project_label") or _project_label(item.get("project_path", ""))),
            title=str(item.get("title") or "新会话")[:48],
            created_at=float(item.get("created_at", time.time()) or time.time()),
            updated_at=float(item.get("updated_at", time.time()) or time.time()),
            last_prompt=str(item.get("last_prompt", "")),
            last_event=str(item.get("last_event") or "重启恢复"),
            mode="idle",  # never restore a running/waiting session as live
        )
        state.sessions[session_id] = session

    state.project_last_session = {
        str(k): str(v)
        for k, v in data.get("project_last_session", {}).items()
        if str(v) in state.sessions
    }

    state.task_counter = max(state.task_counter, int(data.get("task_counter") or 0))
    for item in data.get("task_queue") or []:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        state.task_queue.append(
            QueuedTask(
                prompt=prompt,
                project_path=_norm_path(item.get("project_path") or state.current_cwd),
                session_id=str(item.get("session_id") or ""),
                force_new=bool(item.get("force_new")),
                task_number=int(item.get("task_number") or 0),
                queued_at=float(item.get("queued_at", time.time()) or time.time()),
                session_key=str(item.get("session_key") or item.get("session_id") or ""),
            )
        )

    # If the bot died mid-task, resume that task first after restart.
    active_task = data.get("active_task")
    if isinstance(active_task, dict):
        prompt = str(active_task.get("prompt") or "").strip()
        if prompt:
            task_id = int(active_task.get("task_id") or 0)
            state.task_counter = max(state.task_counter, task_id)
            session_id = str(active_task.get("session_id") or "")
            state.task_queue.insert(
                0,
                QueuedTask(
                    prompt=prompt,
                    project_path=_norm_path(active_task.get("project_path") or state.current_cwd),
                    session_id=session_id,
                    force_new=False,
                    task_number=max(task_id, state.task_counter),
                    queued_at=time.time(),
                    session_key=str(active_task.get("session_key") or session_id or f"new:r{task_id}"),
                ),
            )

    for item in data.get("projects") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        snap = state.ensure_project(path)
        snap.last_event = str(item.get("last_event") or "重启恢复")
        snap.last_error = str(item.get("last_error") or "")
        snap.last_active_at = item.get("last_active_at")
        for entry in item.get("transcript", [])[-80:]:
            if not isinstance(entry, dict):
                continue
            snap.add_entry(
                str(entry.get("kind") or "system"),
                str(entry.get("text") or ""),
                str(entry.get("session_id") or ""),
            )

    if data.get("current_cwd") in state.projects:
        state.set_current_project(str(data["current_cwd"]))
    current_session_id = str(data.get("current_session_id") or "")
    if current_session_id in state.sessions:
        state.current_session_id = current_session_id
        state.sessions[current_session_id].mode = "idle"
        state.sessions[current_session_id].last_event += "（重启恢复）"
    new_session_project = str(data.get("new_session_project") or "")
    new_session_expires_at = float(data.get("new_session_expires_at") or 0)
    if new_session_project and new_session_expires_at > time.time():
        state.start_new_session_next = True
        state.new_session_project = _norm_path(new_session_project)
        state.new_session_expires_at = new_session_expires_at
    else:
        state.cancel_new_session()

    cutoff = time.time() - 90 * 24 * 60 * 60
    bindings = data.get("message_bindings") or {}
    if isinstance(bindings, dict):
        state.message_bindings = {
            str(key): dict(value)
            for key, value in bindings.items()
            if isinstance(value, dict)
            and float(value.get("created_at", 0) or 0) >= cutoff
            and str(value.get("session_id") or "")
        }


def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_dump_state(_state), fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception("state save failed")


def _load_state() -> None:
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        log.warning("state file unreadable, starting fresh", exc_info=True)
        return
    _apply_state(_state, data)
    log.info("已从 %s 恢复 %d 个托管会话", STATE_FILE, len(_state.sessions))


async def _autosave_loop() -> None:
    interval = config.STATE_AUTOSAVE_SECONDS
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        _save_state()


def _resolve_session_id(prefix: str) -> str:
    prefix = prefix.strip()
    if not prefix:
        return ""
    source = ""
    if ":" in prefix:
        source, prefix = prefix.split(":", 1)
        if source not in {"claude", "codex"}:
            return ""
        prefix = prefix.strip()
    if not prefix:
        return ""
    for session_id in _state.sessions:
        if (not source or source == "codex") and session_id.startswith(prefix):
            return session_id
    if source in {"", "claude"}:
        for session_id in _load_global_sessions():
            if str(session_id).startswith(prefix):
                return str(session_id)
    if source in {"", "codex"}:
        for session_id in _load_codex_sessions():
            if str(session_id).startswith(prefix):
                return str(session_id)
    return ""


def _claude_hook_registered() -> bool:
    path = os.path.join(config.HOME, ".claude", "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return config.GLOBAL_HOOK_SCRIPT in json.dumps(data.get("hooks", {}), ensure_ascii=False)
    except (OSError, ValueError, TypeError):
        return False


def _load_global_sessions() -> dict[str, dict]:
    """Load Claude hook state and merge transcript-scan fallback records."""
    sessions: dict[str, dict] = {}
    if not config.CODEX_REMOTE:
        path = config.SESSION_SNAPSHOT_FILE
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                snapshot = data.get("sessions", {})
                if isinstance(snapshot, dict):
                    sessions.update(snapshot)
            except Exception:
                log.warning("Claude hook snapshot unreadable; using transcript fallback", exc_info=True)
        try:
            fallback = scan_claude_transcripts(
                config.CLAUDE_PROJECTS_DIR,
                limit=config.CLAUDE_TRANSCRIPT_SCAN_LIMIT,
            )
        except Exception:
            log.exception("Claude transcript scan failed")
            fallback = {}
        for session_id, record in fallback.items():
            existing = sessions.get(session_id)
            if existing:
                merged = dict(record)
                merged.update(existing)
                merged["source"] = "claude"
                merged["controllable"] = False
                sessions[session_id] = merged
            else:
                sessions[session_id] = record
    return sessions


def _format_global_sessions(limit: int = 8) -> list[str]:
    sessions = _load_global_sessions()
    if not sessions:
        return ["暂无 Claude 会话记录。"]
    items = sorted(
        sessions.values(),
        key=lambda item: item.get("updated_at", 0),
        reverse=True,
    )[:limit]
    lines = []
    for item in items:
        cwd = _project_label(item.get("cwd", ""))
        session_id = str(item.get("session_id", ""))[:8] or "-"
        status = item.get("status", "unknown")
        waiting = " | waiting" if item.get("waiting") else ""
        last_event = str(item.get("last_event", ""))[:28]
        lines.append(f"• {session_id} | {cwd} | {status}{waiting} | {last_event}")
    return lines


def _load_codex_sessions() -> dict[str, dict]:
    """Read local Codex rollout metadata without exposing rollout contents.

    In remote mode (bot on a server, Codex on the home computer) the scan runs
    over ssh on the machine that hosts the rollouts.
    """
    try:
        if config.CODEX_REMOTE:
            return scan_codex_sessions.scan_sessions_remote(
                sessions_dir=config.CODEX_REMOTE_SESSIONS_DIR,
                ssh_target=config.CODEX_SSH_TARGET,
                extra_args=config.CODEX_SSH_EXTRA_ARGS,
            )
        return scan_codex_sessions(config.CODEX_SESSIONS_DIR)
    except Exception:
        log.exception("codex session scan failed")
        return {}


def _session_status_label(status: str) -> str:
    labels = {
        "running": "运行中", "active": "活动中", "waiting": "等待输入",
        "waiting_user_reply": "等待回复", "completed": "已完成",
        "stopped": "已停止", "stale": "疑似中断", "unknown": "未知",
    }
    return labels.get(status, status or "未知")


def _format_all_sessions(limit: int = 20) -> str:
    """Unified, privacy-preserving overview of Claude and Codex sessions."""
    rows: list[tuple[float, str, str]] = []
    for item in _load_global_sessions().values():
        session_id = str(item.get("session_id", ""))
        status = "waiting" if item.get("waiting") else str(item.get("status", "unknown"))
        project = _project_label(str(item.get("cwd", "")))
        event = str(item.get("last_event", ""))[:42]
        rows.append((float(item.get("updated_at", 0) or 0), status, f"[Claude] {session_id[:8] or '-'} | {_session_status_label(status)} | {project} | {event}"))
    for item in _load_codex_sessions().values():
        session_id = str(item.get("session_id", ""))
        status = str(item.get("status", "unknown"))
        project = _project_label(str(item.get("cwd", ""))) if item.get("cwd") else "-"
        event = str(item.get("last_event", ""))[:42]
        rows.append((float(item.get("updated_at", 0) or 0), status, f"[Codex] {session_id[:8] or '-'} | {_session_status_label(status)} | {project} | {event}"))
    if not rows:
        return "暂无本机 Claude 或 Codex 会话记录。"
    rows.sort(key=lambda item: item[0], reverse=True)
    active_count = sum(status in {"running", "active", "waiting", "waiting_user_reply"} for _, status, _ in rows)
    lines = ["会话监控（按最近活动排序）：", f"活动/等待会话：{active_count}"]
    lines.extend(f"• {line}" for _, _, line in rows[:limit])
    if len(rows) > limit:
        lines.append(f"……另有 {len(rows) - limit} 个较早会话未显示")
    return "\n".join(lines)


def _collect_external_sessions() -> list[dict]:
    """Return Claude/Codex summaries in one shape, newest first."""
    records: list[dict] = []
    for item in _load_global_sessions().values():
        record = dict(item)
        record["source"] = "claude"
        record["status"] = "waiting" if item.get("waiting") else str(item.get("status", "unknown"))
        record["controllable"] = False
        record["monitor_key"] = f"claude:{record.get('session_id', '')}"
        records.append(record)
    for item in _load_codex_sessions().values():
        record = dict(item)
        record["source"] = "codex"
        record["monitor_key"] = f"codex:{record.get('session_id', '')}"
        records.append(record)
    by_id = {str(item.get("session_id", "")): item for item in records}
    for session in _state.sessions.values():
        record = by_id.get(session.session_id)
        if record is None:
            record = {
                "source": "codex",
                "session_id": session.session_id,
                "status": session.mode,
                "cwd": session.project_path,
                "last_event": session.last_event,
                "updated_at": session.updated_at,
                "monitor_key": f"codex:{session.session_id}",
            }
            records.append(record)
            by_id[session.session_id] = record
        record["title"] = session.title
        record["cwd"] = session.project_path
        record["managed"] = True
        if session.session_id in _state.pending_by_session:
            record["status"] = "waiting_user_reply"
        elif session.session_id in _running_tasks and not _running_tasks[session.session_id].done():
            record["status"] = "running"
    return sorted(records, key=lambda item: float(item.get("updated_at", 0) or 0), reverse=True)


def _session_name(session_id: str) -> str:
    session = _state.sessions.get(session_id)
    if session is not None:
        return session.title or session.project_label
    item = _find_session_record(session_id)
    return str((item or {}).get("title") or (item or {}).get("last_text") or session_id[:8])[:48]


def _bind_message_refs(refs, session_id: str, project_path: str, message_type: str) -> None:
    for ref in refs or []:
        _state.bind_message(str(ref), session_id, project_path, message_type)


def _reply_message_key(update: Update) -> str:
    message = getattr(update, "message", None)
    explicit = str(getattr(message, "reply_message_key", "") or "")
    if explicit:
        return explicit
    replied = getattr(message, "reply_to_message", None)
    message_id = getattr(replied, "message_id", "")
    chat_id = getattr(getattr(update, "effective_chat", None), "id", "")
    if message_id != "" and chat_id != "":
        return f"telegram:{chat_id}:{message_id}"
    return ""


def _format_home() -> str:
    session = _state.sessions.get(_state.current_session_id)
    if session is None:
        session_line = "💬 当前会话：未选择"
        status_line = "⚪ 请选择会话或新建会话"
        progress = ""
    else:
        status = _session_status_label(session.mode)
        session_line = f"💬 当前会话：{session.title}"
        status_line = f"{('🟡' if session.mode == 'waiting_user_reply' else '🟢' if session.mode == 'running' else '⚪')} {status}"
        progress = f"\n最近进展：{session.last_event[:80]}" if session.last_event else ""
    draft = ""
    if _state.new_session_pending():
        draft = f"\n\n✍️ 正在新建会话：下一条消息将在 {_project_label(_state.new_session_project)} 中创建新会话。"
    return (
        "🤖 Coding Agent\n\n"
        f"📁 当前工作区：{_state.current_project_label or _project_label(_state.current_cwd)}\n"
        f"{session_line}\n{status_line}{progress}{draft}\n\n"
        "请选择下方操作；直接发送消息时会投递到上面显示的目标。"
    )


def _format_monitor_row(item: dict) -> str:
    source = str(item.get("source", "?")).title()
    session_id = str(item.get("session_id", ""))[:8] or "-"
    status = _session_status_label(str(item.get("status", "unknown")))
    workspace = str(item.get("cwd", "")) or "-"
    return f"[{source}] {session_id} | {status} | 工作区：{workspace}"


def _format_session_overview() -> str:
    """Compact default view: actionable sessions then a few recent ones."""
    records = _collect_external_sessions()
    active_states = {"running", "active", "waiting", "waiting_user_reply"}
    active = [item for item in records if item.get("status") in active_states]
    recent = [item for item in records if item.get("status") not in active_states][:5]
    shown = active + recent
    if not shown:
        return "暂无本机 Claude 或 Codex 会话记录。"
    lines = ["会话监控："]
    if active:
        lines.append(f"正在运行/等待：{len(active)} 个")
        lines.extend(f"🟢 {_format_monitor_row(item)}" for item in active)
    else:
        lines.append("当前没有运行或等待输入的会话。")
    if recent:
        lines.append("最近会话：")
        lines.extend(f"• {_format_monitor_row(item)}" for item in recent)
    lines.append("查看某个会话详情：/monitor codex:会话前8位 或 /monitor claude:会话前8位")
    lines.append("查看全部历史会话：/monitor all")
    return "\n".join(lines)


def _monitor_button_choices() -> list[tuple[str, str]]:
    """Build stable callbacks for the sessions shown in the compact overview."""
    records = _collect_external_sessions()
    active_states = {"running", "active", "waiting", "waiting_user_reply"}
    visible = [item for item in records if item.get("status") in active_states]
    visible.extend(item for item in records if item.get("status") not in active_states and item not in visible)
    visible = visible[:12]
    choices = []
    for item in visible:
        source = str(item.get("source", ""))
        session_id = str(item.get("session_id", ""))
        if not source or not session_id:
            continue
        label = f"{source.title()} {session_id[:8]} · {_session_status_label(str(item.get('status', 'unknown')))}"
        choices.append((label, f"monitor:{source}:{session_id}"))
    return choices


def _resolve_monitored_session(selector: str) -> dict | None:
    selector = selector.strip().lower()
    if not selector:
        return None
    source = ""
    prefix = selector
    if ":" in selector:
        source, prefix = selector.split(":", 1)
    candidates = [
        item for item in _collect_external_sessions()
        if (not source or item.get("source") == source)
        and str(item.get("session_id", "")).lower().startswith(prefix)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _format_monitored_session_detail(item: dict) -> str:
    updated_at = float(item.get("updated_at", 0) or 0)
    age = max(0, int(time.time() - updated_at)) if updated_at else 0
    source = str(item.get("source", "?")).title()
    session_id = str(item.get("session_id", ""))
    project = str(item.get("cwd", "")) or "-"
    event = str(item.get("last_event", "")) or "-"
    lines = [
        f"{source} 会话详情",
        f"会话：{session_id}",
        f"状态：{_session_status_label(str(item.get('status', 'unknown')))}",
        f"工作区：{project}",
        f"最后事件：{event[:160]}",
        f"最后活动：{age}s 前",
    ]
    if item.get("waiting_reason"):
        lines.append(f"等待原因：{str(item['waiting_reason'])[:400]}")
    if str(item.get("source")) == "claude":
        lines.append("Claude 会话为只读监控；如需继续，请回到原 Claude 终端操作。")
    else:
        lines.append("如需让本机器人继续该 Codex 会话，可使用 /use <会话前8位>。")
    return "\n".join(lines)


def _format_waiting_message(session_id: str, item: dict) -> str:
    source = str(item.get("source") or "codex")
    source_label = "Codex" if source != "claude" else "Claude"
    cwd = _project_label(item.get("cwd", ""))
    notification_type = str(item.get("notification_type") or "").strip()
    reason = str(item.get("waiting_reason") or item.get("last_text") or "Codex 正在等待你的输入").strip()
    low_reason = reason.lower()
    if (
        not reason
        or low_reason == "claude is waiting for your input"
        or (reason.startswith("{") and '"session_id"' in reason and '"message"' in reason)
    ):
        if notification_type == "permission_prompt":
            reason = "Codex 正在等待权限确认，请选择按钮或直接回复。"
        else:
            reason = "Codex 正在等待你的输入，但这次还没从 transcript 里提取到更具体的问题内容。"
    return (
        f"⚠️ {source_label} 会话需要你操作\n"
        f"会话：{_session_name(session_id)}\n"
        f"项目：{cwd}\n\n"
        f"{reason[:1200]}"
    )


def _adopt_global_session(session_id: str, set_focus: bool = True) -> ManagedSession | None:
    sessions = _load_global_sessions()
    item = sessions.get(session_id)
    if item:
        cwd = _norm_path(item.get("cwd", config.DEFAULT_PROJECT_DIR) or config.DEFAULT_PROJECT_DIR)
        _state.ensure_project(cwd)
        session = _state.bind_session(session_id, cwd, set_focus=set_focus)
        session.title = str(item.get("last_text") or item.get("last_event") or session.title)[:48] or session.title
        session.mode = "waiting_user_reply" if item.get("waiting") else item.get("status", "active")
        session.last_event = str(item.get("last_event", session.last_event))
        session.updated_at = float(item.get("updated_at", time.time()) or time.time())
        if item.get("waiting"):
            _state.set_pending(
                PendingInteraction(
                    project_path=cwd,
                    project_label=_project_label(cwd),
                    session_id=session_id,
                    prompt_text=str(item.get("waiting_reason") or item.get("last_text") or "Codex 正在等待你的回复"),
                    options=list(item.get("choice_options") or []),
                    source="buttons" if item.get("choice_options") else "text",
                )
            )
        return session

    # Adopt a local Codex session (created by this bot or another Codex CLI).
    codex_item = _load_codex_sessions().get(session_id)
    if codex_item:
        cwd = _norm_path(codex_item.get("cwd") or config.DEFAULT_PROJECT_DIR)
        _state.ensure_project(cwd)
        session = _state.bind_session(session_id, cwd, set_focus=set_focus)
        session.title = str(codex_item.get("title") or codex_item.get("last_event") or session.title)[:48] or session.title
        session.mode = str(codex_item.get("status", "unknown"))
        session.last_event = str(codex_item.get("last_event") or session.last_event)
        session.updated_at = float(codex_item.get("updated_at", time.time()) or time.time())
        return session
    return None


_state = RuntimeState()
for _path in config.DEFAULT_PROJECTS:
    _state.ensure_project(_path)
_state.set_current_project(config.DEFAULT_PROJECT_DIR)
_load_state()
_task_lock = asyncio.Lock()
# One running asyncio task per session_key (session id, or ``new:<n>`` while a
# fresh session is being created).  Several sessions run in parallel, bounded by
# config.MAX_PARALLEL_TASKS.
_running_tasks: dict[str, asyncio.Task] = {}
# choice_id -> session_key, so a button click knows which session it belongs to
# even when several sessions are waiting for input at the same time.
_choice_session: dict[str, str] = {}
# project path (normalized) -> number of active runs, for per-project display.
_active_runs_by_project: dict[str, int] = {}
_seen_global_event_offset = 0
_last_waiting_notice: dict[str, str] = {}
_external_session_states: dict[str, str] = {}
_external_message_cursors: dict[str, str] = {}
_external_session_monitor_ready = False


def _session_is_managed(session_id: str) -> bool:
    return bool(session_id) and session_id in _state.sessions


def _remember_waiting_notice(session_id: str, reason: str) -> bool:
    reason = reason.strip()
    if not session_id or not reason:
        return False
    previous = _last_waiting_notice.get(session_id)
    _last_waiting_notice[session_id] = reason
    return previous != reason


def _prune_managed_waiting_notice(session_id: str) -> None:
    if session_id:
        _last_waiting_notice.pop(session_id, None)


def _refresh_global_mode() -> None:
    """Recompute the aggregate display mode from the live run table."""
    if any(not task.done() for task in _running_tasks.values()):
        _state.mode = "running"
    elif _state.pending is not None:
        _state.mode = "waiting_user_reply"
    else:
        _state.mode = "idle"


_TARGET_PREFIX_RE = re.compile(r"^@([0-9a-zA-Z\-]{3,})(?:\s+(.*))?$", re.S)


def _resolve_target(text: str) -> tuple[str, str, bool]:
    """Parse "@<会话前8位> 消息" routing.

    Returns ``(target_session_id, remainder, routed)``.  ``routed`` is False for
    ordinary messages, which then go to the focused session.
    """
    m = _TARGET_PREFIX_RE.match(text.strip())
    if not m:
        return "", text, False
    prefix = m.group(1)
    remainder = (m.group(2) or "").strip()
    if not prefix:
        return "", text, False
    session_id = _resolve_session_id(prefix)
    if not session_id:
        return "", text, False
    return session_id, remainder, True


def _output_tag(session_id: str) -> str:
    """Prefix agent output with its session when it could be confused with
    another one (multiple runs active, or the run is not the focused session)."""
    if not session_id:
        return ""
    active = [key for key, task in _running_tasks.items() if not task.done()]
    if session_id == _state.current_session_id and len(active) <= 1:
        return ""
    name = _session_name(session_id)
    if name == session_id[:8]:
        return f"[会话 {name}]"
    return f"[会话：{name}]"


def _start_run(prompt: str, project_path: str, session_key: str, session_id: str) -> asyncio.Task:
    """Create and register the asyncio task that runs one agent turn."""
    task = asyncio.create_task(_run_agent(prompt, project_path, session_key, session_id))
    _running_tasks[session_key] = task
    _refresh_global_mode()
    return task


def _stop_session_run(session_id: str) -> bool:
    """Stop the running task of one session (or dismiss its pending wait).

    Returns True when something was stopped/dismissed.
    """
    stopped = False
    task = _running_tasks.get(session_id)
    if task is not None and not task.done():
        agent_runner.request_stop(session_id)
        task.cancel()
        stopped = True
    if _state.pending_by_session.get(session_id) is not None:
        _state.clear_pending(session_id)
        _refresh_global_mode()
        stopped = True
    return stopped


def _stop_all_runs() -> int:
    """Stop every active run, dismiss all pending waits and clear the queue.

    Returns the number of stopped/dismissed/removed items.
    """
    agent_runner.request_stop(None)
    count = 0
    for key, task in list(_running_tasks.items()):
        if not task.done():
            task.cancel()
            count += 1
    for key in list(_state.pending_by_session):
        _state.clear_pending(key)
        count += 1
    count += len(_state.task_queue)
    _state.task_queue.clear()
    _refresh_global_mode()
    return count


def _external_session_blocked(session_id: str) -> str:
    """Return a reason string when an external (non-managed) Codex session
    cannot be adopted right now, e.g. it is still running in another terminal."""
    if session_id in _state.sessions:
        return ""
    item = _load_codex_sessions().get(session_id)
    if item and str(item.get("status", "")) == "running":
        return (
            "该 Codex 会话正在本机别处运行（可能由另一个终端开启），"
            "请先在那里停止它，再回来接管。"
        )
    return ""


def _is_controllable_session(session_id: str) -> bool:
    """A session can be driven by this bot if it is managed or a local Codex
    rollout.  Legacy Claude sessions stay read-only (no ``codex exec resume``)."""
    if session_id in _state.sessions:
        return True
    return _load_codex_sessions().get(session_id) is not None


def _find_session_record(session_id: str) -> dict | None:
    for item in _collect_external_sessions():
        if str(item.get("session_id", "")) == session_id:
            return item
    return None


def _workspace_choices(action: str) -> list[tuple[str, str]]:
    """Build compact, unambiguous workspace callbacks for phone cards."""
    choices = [
        (f"{snap.label} · {snap.path}", f"workspace:{action}:{index}")
        for index, snap in enumerate(_state.projects.values())
    ]
    return choices


def _resolve_workspace_index(raw_index: str) -> str:
    try:
        index = int(raw_index)
    except ValueError:
        return ""
    paths = [snap.path for snap in _state.projects.values()]
    return paths[index] if 0 <= index < len(paths) else ""


async def _send_new_session_workspace_picker(reply) -> None:
    await reply(
        f"新建 Codex 会话\n当前工作区：{_state.current_cwd}\n请选择新会话所在的工作区。"
    )
    await channel.send_project_choices(
        _workspace_choices("new"),
        title="选择新会话工作区",
        prompt="请选择新会话所在的工作区：",
    )


def _build_session_cards(project_path: str = "") -> list[dict]:
    """Build one interactive card per visible session for ``/sessions``."""
    cards: list[dict] = []
    records = _collect_external_sessions()
    if project_path:
        target_path = _norm_path(project_path)
        records = [item for item in records if _norm_path(str(item.get("cwd") or "")) == target_path]

    def priority(item: dict) -> tuple[int, float]:
        status = str(item.get("status") or "")
        session_id = str(item.get("session_id") or "")
        if status in {"waiting", "waiting_user_reply"}:
            rank = 0
        elif status in {"running", "active"}:
            rank = 1
        elif session_id == _state.current_session_id:
            rank = 2
        else:
            rank = 3
        return rank, -float(item.get("updated_at", 0) or 0)

    for item in sorted(records, key=priority)[:14]:
        source = str(item.get("source", "?"))
        session_id = str(item.get("session_id", ""))
        if not session_id:
            continue
        status = str(item.get("status", "unknown"))
        status_label = _session_status_label(status)
        workspace = str(item.get("cwd", "")) or "-"
        project = _project_label(workspace) if workspace != "-" else "-"
        event = str(item.get("last_event", ""))[:40] or "-"
        managed = session_id in _state.sessions
        is_focus = session_id == _state.current_session_id
        live_task = session_id in _running_tasks and not _running_tasks[session_id].done()
        running = status in {"running", "active"} or live_task
        waiting = status in {"waiting", "waiting_user_reply"} or session_id in _state.pending_by_session
        icon = "👉" if is_focus else ("🟢" if running else ("🟡" if waiting else "•"))
        display_title = str(item.get("title") or item.get("last_text") or event or f"会话 {session_id[:8]}")[:48]
        title = f"{icon} {display_title} · {status_label}"
        subtitle = f"📁 {project}\n最近进展：{event}"
        if managed:
            subtitle += " | 托管"
        primary = "回复" if waiting else ("查看进度" if running else "继续")
        primary_action = "detail" if running else "focus"
        buttons: list[tuple[str, str]] = [(primary, f"sess:{primary_action}:{session_id}")]
        if running and not is_focus:
            buttons.append(("设为当前", f"sess:focus:{session_id}"))
        buttons.append(("查看详情", f"sess:detail:{session_id}"))
        if running:
            buttons.append(("停止", f"sess:stop:{session_id}"))
        if not managed and status != "running":
            buttons.append(("接管", f"sess:adopt:{session_id}"))
        cards.append({"title": title, "subtitle": subtitle, "buttons": buttons})
    return cards


def _authorized(update: Update) -> bool:
    """Check the sender identity for both Telegram and Feishu adapters."""
    if not update.effective_chat:
        return False
    if config.BOT_CHANNEL == "feishu":
        return str(update.effective_chat.id) == config.ALLOWED_FEISHU_OPEN_ID
    return update.effective_chat.id == config.ALLOWED_CHAT_ID


async def _send_unauthorized(update: Update):
    if update.message:
        await update.message.reply_text("⛔ 未授权。")
    elif update.callback_query:
        await update.callback_query.answer("⛔ 未授权", show_alert=True)


async def _poll_global_sessions(context: ContextTypes.DEFAULT_TYPE):
    global _seen_global_event_offset
    path = config.SESSION_EVENT_LOG
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            fh.seek(_seen_global_event_offset)
            new_lines = fh.readlines()
            _seen_global_event_offset = fh.tell()
    except Exception:
        return
    if not new_lines:
        return

    sessions = _load_global_sessions()
    for raw in new_lines[-20:]:
        try:
            event = json.loads(raw)
        except Exception:
            continue
        session_id = str(event.get("session_id", ""))
        if not session_id or _session_is_managed(session_id):
            continue
        item = sessions.get(session_id, {})
        if not item.get("waiting"):
            continue
        reason = str(item.get("waiting_reason", "") or item.get("last_text", "")).strip()
        if not reason or not _remember_waiting_notice(session_id, reason):
            continue
        message = _format_waiting_message(session_id, {**item, "source": "claude"})
        options = item.get("choice_options") or []
        if options:
            # Claude is monitored read-only.  Never adopt it into the Codex
            # runner: its session id belongs to a different engine.
            await channel.send_text(
                message + "\n\nClaude 会话当前仅支持只读监控，请在原 Claude 终端中选择或回复。"
            )
        else:
            await channel.send_text(message)


async def _poll_external_session_status():
    """Notify Feishu/Telegram only when a local external-session state changes."""
    global _external_session_monitor_ready
    snapshots: list[tuple[str, str, dict]] = []
    for item in _load_global_sessions().values():
        status = "waiting" if item.get("waiting") else str(item.get("status", "unknown"))
        snapshots.append(("Claude", str(item.get("session_id", "")), {**item, "status": status}))
    for item in (await asyncio.to_thread(_load_codex_sessions)).values():
        snapshots.append(("Codex", str(item.get("session_id", "")), item))
    # 供 Web 看板读取（避免看板自行触发远端 SSH 扫描）
    web_dashboard.set_scan_cache([item for _, _, item in snapshots])

    current: dict[str, str] = {}
    current_message_cursors: dict[str, str] = {}
    owned_session_ids = agent_runner.active_session_ids()
    for source, session_id, item in snapshots:
        if not session_id:
            continue
        key = f"{source}:{session_id}"
        status = str(item.get("status", "unknown"))
        current[key] = status
        messages = item.get("messages") if source == "Codex" else []
        if isinstance(messages, list) and messages:
            message_ids = [str(message.get("id") or "") for message in messages if isinstance(message, dict)]
            previous_message_id = _external_message_cursors.get(key, "")
            start = len(messages)
            if session_id in owned_session_ids or not _external_session_monitor_ready:
                # Seed/advance the cursor without sending. The runner forwards
                # its own live output and startup must never replay history.
                start = len(messages)
            elif previous_message_id and previous_message_id in message_ids:
                start = message_ids.index(previous_message_id) + 1
            else:
                start = max(0, len(messages) - 6)
            for message in messages[start:]:
                if isinstance(message, dict):
                    await _forward_external_codex_message(session_id, item, message)
            current_message_cursors[key] = message_ids[-1] if message_ids else ""
        previous = _external_session_states.get(key)
        if (source == "Codex" and previous in {"running", "active"}
                and status not in {"running", "active"} and isinstance(messages, list)):
            last_assistant = next(
                (message for message in reversed(messages)
                 if isinstance(message, dict) and message.get("role") == "assistant"),
                None,
            )
            if (last_assistant is not None
                    and str(last_assistant.get("id") or "") == previous_message_id):
                await _forward_external_codex_message(session_id, item, last_assistant)
        if _session_is_managed(session_id) or session_id in owned_session_ids:
            # The bot reports lifecycle changes for its own sessions directly.
            continue
        if not _external_session_monitor_ready or previous == status:
            continue
        project = _project_label(str(item.get("cwd", ""))) if item.get("cwd") else "-"
        event = str(item.get("last_event", ""))[:80] or "-"
        icons = {"running": "🟢", "active": "🟢", "waiting": "🟡", "completed": "✅", "stopped": "⏹️", "stale": "⚠️"}
        await channel.send_text(
            f"{icons.get(status, 'ℹ️')} {source} 会话状态变化\n"
            f"会话：{session_id[:8]}\n状态：{_session_status_label(status)}\n项目：{project}\n事件：{event}"
        )
    _external_session_states.clear()
    _external_session_states.update(current)
    _external_message_cursors.clear()
    _external_message_cursors.update(current_message_cursors)
    _external_session_monitor_ready = True


async def _forward_external_codex_message(session_id: str, item: dict, message: dict) -> None:
    """Mirror one new external Codex conversation message to the phone."""
    text = str(message.get("text") or "").strip()
    role = str(message.get("role") or "")
    if not text or role not in {"user", "assistant"}:
        return
    tag = f"[Codex {session_id[:8]}]"
    if role == "user":
        await channel.send_text(f"你：{text}", tag=tag)
        return

    options = agent_runner._parse_options(text)
    needs_reply = bool(options or agent_runner._looks_like_question(text))
    if not needs_reply:
        await channel.send_text(text, tag=tag)
        return
    if str(item.get("status") or "") == "running":
        await channel.send_text(
            text + "\n\n该会话仍在原 Codex 终端运行；内容已同步，待本轮结束后可从 bot 接管继续。",
            tag=tag,
        )
        return
    if not _remember_waiting_notice(session_id, text):
        return
    session = _adopt_global_session(session_id)
    if session is None:
        await channel.send_text(text + "\n\n该会话需要回复，但目前无法远程接管。", tag=tag)
        return
    pending = PendingInteraction(
        project_path=session.project_path,
        project_label=session.project_label,
        session_id=session_id,
        prompt_text=text,
        options=options,
        source="buttons" if options else "text",
    )
    _state.mark_waiting(pending)
    waiting_message = _format_waiting_message(session_id, {
        "cwd": session.project_path,
        "waiting_reason": text,
        "last_text": text,
        "source": "codex",
    })
    if options:
        choice_id = await channel.send_choice_no_wait(
            waiting_message,
            options,
            title=f"Codex {session_id[:8]} 需要你的选择",
        )
        if choice_id is not None:
            _choice_session[choice_id] = session_id
            asyncio.create_task(_expire_waiting(pending, choice_id, config.APPROVAL_TIMEOUT, session_id))
    else:
        await channel.send_text(waiting_message, tag=tag)


async def _run_agent(prompt: str, project_path: str, session_key: str, session_id: str):
    """Run one Codex turn for a session and handle the outcome.

    ``session_key`` is the run identity (session id, or ``new:...`` for a fresh
    session); ``session_id`` is the known session id or "" when creating one.
    """
    snap = _state.ensure_project(project_path)
    started_at = time.time()
    tag = _output_tag(session_id or session_key)
    heartbeat = asyncio.create_task(_heartbeat_loop(snap, prompt, started_at, session_key, tag))
    active_project = _norm_path(project_path)
    _active_runs_by_project[active_project] = _active_runs_by_project.get(active_project, 0) + 1
    try:
        outcome = await agent_runner.run_turn(prompt, project_path, session_id=session_id or None, run_key=session_key, tag=tag)
        _state.append_events(project_path, outcome.events)
        active_session_id = outcome.session_id or session_id
        if outcome.session_id:
            session = _state.bind_session(outcome.session_id, project_path, initial_prompt=prompt, set_focus=False)
            session.last_prompt = prompt.strip() or session.last_prompt
            session.mode = "running"
            session.last_event = outcome.final_text[:120] if outcome.final_text else session.last_event
            session.updated_at = time.time()
            # Focus follows activity only when this run was already the focused
            # target, or it created a brand-new session for the current project.
            if _state.current_session_id == active_session_id or session_key.startswith("new:"):
                _state.current_session_id = active_session_id
        if outcome.state == "waiting":
            pending = PendingInteraction(
                project_path=project_path,
                project_label=snap.label,
                session_id=active_session_id,
                prompt_text=outcome.waiting_text or "Codex 正在等待你的回复",
                options=outcome.choice_options,
                source="buttons" if outcome.choice_options else "text",
            )
            _state.mark_waiting(pending)
            _remember_waiting_notice(pending.session_id, pending.prompt_text)
            waiting_message = _format_waiting_message(
                pending.session_id,
                {
                    "cwd": pending.project_path,
                    "waiting_reason": pending.prompt_text,
                    "last_text": pending.prompt_text,
                    "notification_type": "",
                    "source": "codex",
                },
            )
            if pending.options:
                # Non-blocking: the card is sent and this task returns.  A
                # button click (on_button) or a text reply (on_message) will
                # continue the same session; the expiry task invalidates the
                # buttons after APPROVAL_TIMEOUT.
                choice_id = await channel.send_choice_no_wait(
                    waiting_message,
                    pending.options,
                    title=f"「{_session_name(pending.session_id)}」需要你的选择",
                )
                if choice_id is not None:
                    _choice_session[choice_id] = pending.session_id
                    _bind_message_refs(
                        channel.choice_message_refs(choice_id),
                        pending.session_id,
                        pending.project_path,
                        "choice",
                    )
                    asyncio.create_task(
                        _expire_waiting(pending, choice_id, config.APPROVAL_TIMEOUT, pending.session_id)
                    )
            else:
                refs = await channel.send_text(waiting_message, tag=tag)
                _bind_message_refs(refs, pending.session_id, pending.project_path, "waiting")
        elif _state.stop_requested:
            _prune_managed_waiting_notice(active_session_id)
            _state.mark_done("任务已停止", project_path, active_session_id)
            refs = await channel.send_text(
                f"⏹️「{_session_name(active_session_id)}」已停止（用时 {int(time.time() - started_at)}s）。",
                tag=tag,
            )
            _bind_message_refs(refs, active_session_id, project_path, "stopped")
        else:
            _prune_managed_waiting_notice(active_session_id)
            _state.mark_done("任务完成", project_path, active_session_id)
            if (outcome.final_text and outcome.final_text != outcome.waiting_text
                    and agent_runner._should_forward_reply(active_session_id or session_key, outcome.final_text)):
                refs = await channel.send_text(outcome.final_text, tag=tag)
                _bind_message_refs(refs, active_session_id, project_path, "assistant")
            refs = await channel.send_text(
                f"✅「{_session_name(active_session_id)}」任务完成（用时 {int(time.time() - started_at)}s）。",
                tag=tag,
            )
            _bind_message_refs(refs, active_session_id, project_path, "completed")
    except asyncio.CancelledError:
        _prune_managed_waiting_notice(session_id)
        _state.mark_done("任务已取消", project_path, session_id)
        refs = await channel.send_text(f"⏹️「{_session_name(session_id)}」已取消。", tag=tag)
        _bind_message_refs(refs, session_id, project_path, "cancelled")
        raise
    except Exception as e:
        log.exception("run_agent failed")
        _prune_managed_waiting_notice(session_id)
        _state.mark_error(f"Agent 运行出错：{type(e).__name__}: {e}", project_path, session_id)
        refs = await channel.send_text(
            f"🔴「{_session_name(session_id)}」执行失败\n原因：{type(e).__name__}: {e}\n"
            "会话上下文已保留，可以稍后继续。",
            tag=tag,
        )
        _bind_message_refs(refs, session_id, project_path, "error")
    finally:
        heartbeat.cancel()
        _running_tasks.pop(session_key, None)
        _active_runs_by_project[active_project] = max(0, _active_runs_by_project.get(active_project, 0) - 1)
        if _active_runs_by_project.get(active_project, 0) <= 0:
            snap.mode = "idle"
        _refresh_global_mode()
        await _drain_queues()


async def _heartbeat_loop(snap: ProjectSnapshot, prompt: str, started_at: float, run_key: str, tag: str = ""):
    """Periodically report that a long task is still alive."""
    interval = config.HEARTBEAT_SECONDS
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        elapsed = int(time.time() - started_at)
        lines = [f"⏳ {snap.label} 任务执行中… 已运行 {elapsed}s"]
        latest = agent_runner.last_agent_text(run_key)
        if latest:
            lines.append(f"最近：{latest[:120]}")
        else:
            lines.append(f"任务：{prompt[:80]}")
        await channel.send_status("\n".join(lines), tag=tag)


async def _expire_waiting(pending: PendingInteraction, choice_id: str | None, timeout: int, session_key: str):
    """Invalidate choice buttons after the approval timeout."""
    await asyncio.sleep(timeout)
    if choice_id:
        channel.expire_choice(choice_id)
    if _state.pending_by_session.get(session_key) is pending:
        await channel.send_text(
            f"⏰ 选择超时（{timeout}s）。按钮已失效，你也可以直接发文字继续。"
        )


async def _drain_queues() -> None:
    """Start queued tasks while parallel slots are free.

    Global FIFO order is preserved per session: a task only starts when its
    session has no active run and is not waiting for user input.
    """
    while True:
        running_keys = {key for key, task in _running_tasks.items() if not task.done()}
        waiting_keys = set(_state.pending_by_session.keys())
        if len(running_keys) >= config.MAX_PARALLEL_TASKS:
            return
        next_index = None
        for index, queued in enumerate(_state.task_queue):
            if queued.session_key in running_keys or queued.session_key in waiting_keys:
                continue
            next_index = index
            break
        if next_index is None:
            return
        queued = _state.task_queue.pop(next_index)
        _state.start_task(
            queued.prompt,
            queued.project_path,
            queued.session_id,
            queued.force_new,
            session_key=queued.session_key,
        )
        snap = _state.ensure_project(queued.project_path)
        session_hint = (
            f"会话：{queued.session_key[:8]}\n"
            if queued.session_key and not queued.session_key.startswith("new:")
            else ""
        )
        await channel.send_text(
            f"▶️ 开始队列任务 #{queued.task_number}：{queued.prompt[:80]}\n"
            f"项目：{snap.label}\n{session_hint}"
        )
        _start_run(queued.prompt, queued.project_path, queued.session_key, queued.session_id)


# ---------------- 命令 ----------------

async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if not ctx.args:
        await update.message.reply_text(
            f"📁 当前工作区：{_state.current_project_label or _project_label(_state.current_cwd)}\n"
            "请选择要切换到的工作区："
        )
        await channel.send_project_choices(_workspace_choices("switch"), title="切换工作区")
        return
    raw = " ".join(ctx.args).strip()
    target = None
    for snap in _state.projects.values():
        if raw.lower() in {snap.label.lower(), snap.path.lower()}:
            target = snap.path
            break
    if target is None:
        target = raw
    if not os.path.isdir(target):
        await update.message.reply_text(f"目录不存在：{target}")
        return
    _state.ensure_project(target)
    _state.set_current_project(target)
    suggested_id = _state.project_last_session.get(_state.current_cwd, "")
    session = _state.sessions.get(suggested_id)
    _state.current_session_id = ""
    _state.cancel_new_session()
    await update.message.reply_text(
        f"✓ 已切换工作区「{_state.current_project_label}」\n"
        f"最近会话：{session.title if session else '无'}\n"
        "请选择继续最近会话、选择其他会话或新建会话。"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await _send_new_session_workspace_picker(update.message.reply_text)


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if not ctx.args:
        await update.message.reply_text("用法：/use <session_id前8位>（或 /focus，两者等价）")
        return
    session_id = _resolve_session_id(ctx.args[0])
    if not session_id:
        await update.message.reply_text("没找到这个会话。先用 /sessions 看可用会话。")
        return
    if not _is_controllable_session(session_id):
        await update.message.reply_text("该会话是旧 Claude 只读会话，无法用 Codex 继续。请回到原终端操作。")
        return
    session = _state.sessions.get(session_id)
    if session is None:
        blocked = _external_session_blocked(session_id)
        if blocked:
            await update.message.reply_text(blocked)
            return
        session = _adopt_global_session(session_id)
    if session is None:
        await update.message.reply_text("这个会话目前只能看到摘要，暂时还不能接管。")
        return
    _state.set_current_project(session.project_path)
    _state.current_session_id = session_id
    _state.cancel_new_session()
    await update.message.reply_text(
        f"✓ 已设为当前会话「{session.title}」\n"
        f"📁 工作区：{session.project_label}\n"
        "直接发送消息即可继续。"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(_state.summary() + "\n\n" + _format_session_overview())


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Welcome message followed by a clickable dashboard."""
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(_format_home())
    await channel.send_command_menu(has_current_session=bool(_state.current_session_id))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(
        "常用操作请直接点击下方按钮，不需要输入会话编号。\n"
        "命令：/monitor /status /sessions /project /new /use /stop /queue /health\n"
        "直接回复某条 Bot 消息，会自动回到那条消息所属的会话。\n"
        "高级用法：@会话前8位 消息；/stop all 停止全部任务。"
    )
    await channel.send_command_menu(has_current_session=bool(_state.current_session_id))


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cheap local environment check (no subprocess calls)."""
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    mode = _state.mode
    if mode == "running":
        mode_text = f"忙碌（任务 #{_state.current_task_id}）"
    elif mode == "waiting_user_reply":
        mode_text = "等待你的回复"
    else:
        mode_text = "空闲"
    if config.CODEX_REMOTE:
        codex_ok = f"远端（{config.CODEX_SSH_TARGET}）"
    else:
        codex_ok = "可用" if shutil.which("codex") else "未找到 codex 命令"
    codex_dir_ok = "存在" if os.path.isdir(config.CODEX_SESSIONS_DIR) else "不存在"
    claude_dir_ok = "存在" if os.path.isdir(config.SESSION_MONITOR_DIR) else "不存在"
    lines = [
        f"状态：{mode_text}",
        f"通道：{config.BOT_CHANNEL}",
        f"工作目录：{_state.current_cwd}",
        f"Codex CLI：{codex_ok}",
        f"Codex 会话目录：{codex_dir_ok}",
        f"Claude 监控目录：{claude_dir_ok}",
        f"Claude Hook：{'已注册' if _claude_hook_registered() else '未注册（已启用 transcript 扫描兜底）'}",
        f"Claude transcript 目录：{'存在' if os.path.isdir(config.CLAUDE_PROJECTS_DIR) else '不存在'}",
        f"Web 看板：{'已启用' if config.DASHBOARD_ENABLED else '未启用'}（{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}）",
        f"沙箱：{config.CODEX_SANDBOX}",
        f"项目数：{len(_state.projects)}",
        f"心跳间隔：{config.HEARTBEAT_SECONDS}s",
    ]
    if config.CODEX_MODEL:
        lines.append(f"Codex 模型：{config.CODEX_MODEL}")
    await update.message.reply_text("\n".join(lines))


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show / clear the pending task queue."""
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if ctx.args and ctx.args[0].lower() in {"clear", "清空"}:
        removed = _state.clear_queue()
        await update.message.reply_text(f"已清空队列（移除 {removed} 个任务）。")
        return
    if not _state.task_queue:
        await update.message.reply_text("队列为空，当前没有排队中的任务。")
        return
    lines = [f"任务队列（共 {len(_state.task_queue)} 个）："]
    for index, task in enumerate(_state.task_queue, start=1):
        session_hint = task.session_key[:8] if task.session_key and not task.session_key.startswith("new:") else "新会话"
        lines.append(
            f"{index}. #{task.task_number} | {session_hint} | {_project_label(task.project_path)} | {task.prompt[:60]}"
        )
    lines.append("用法：/queue clear 清空队列。")
    await update.message.reply_text("\n".join(lines))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    arg = " ".join(ctx.args).strip().lower()
    if arg in {"all", "全部"}:
        stopped = _stop_all_runs()
        await update.message.reply_text(f"已请求停止 {stopped} 个正在运行/等待的任务。")
        return
    if arg:
        session_id = _resolve_session_id(arg)
        if not session_id:
            await update.message.reply_text(f"找不到会话「{arg}」。发送 /sessions 查看列表。")
            return
    else:
        session_id = _state.current_session_id
    if not session_id:
        await update.message.reply_text("当前没有正在执行的任务。")
        return
    if _stop_session_run(session_id):
        await update.message.reply_text(f"已请求停止：会话 {session_id[:8]}")
    else:
        await update.message.reply_text(f"会话 {session_id[:8]} 当前没有正在执行的任务。")


async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Managed sessions plus per-session action cards for every local session."""
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(
        f"选择要继续的会话\n📁 当前工作区：{_state.current_project_label or _project_label(_state.current_cwd)}"
    )
    cards = _build_session_cards()
    if cards:
        await channel.send_session_cards(
            "会话已按需要处理、运行中和最近更新排序。", cards
        )
    else:
        await update.message.reply_text("暂无本机会话记录。")


async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    selector = " ".join(ctx.args).strip()
    if not selector:
        await update.message.reply_text(_format_session_overview())
        await channel.send_monitor_choices("点击一个会话，查看详情：", _monitor_button_choices())
        return
    if selector.lower() == "all":
        await update.message.reply_text(_format_all_sessions(limit=40))
        return
    item = _resolve_monitored_session(selector)
    if item is None:
        await update.message.reply_text(
            "未找到唯一匹配的会话。请从 /monitor 列表复制来源和前 8 位 ID，"
            "例如：/monitor codex:019fbd90"
        )
        return
    await update.message.reply_text(_format_monitored_session_detail(item))


async def _handle_dashboard_action(action: str, query):
    """Run a common action from an inline/card button without fake messages."""
    if action == "continue":
        session = _state.sessions.get(_state.current_session_id)
        if session is None:
            await query.edit_message_text("当前还没有选择会话。请选择已有会话，或新建一个会话。")
            cards = _build_session_cards()
            if cards:
                await channel.send_session_cards("选择一个会话继续：", cards)
            else:
                await _send_new_session_workspace_picker(channel.send_text)
        else:
            await query.edit_message_text(
                f"💬 当前会话：「{session.title}」\n"
                f"📁 工作区：{session.project_label}\n"
                f"状态：{_session_status_label(session.mode)}\n"
                f"最近进展：{session.last_event[:120]}\n\n"
                "直接发送消息即可继续这个会话。"
            )
    elif action == "status":
        await query.edit_message_text(_state.summary() + "\n\n" + _format_session_overview())
    elif action == "monitor":
        await query.edit_message_text(_format_session_overview())
        await channel.send_monitor_choices("点击一个会话，查看详情：", _monitor_button_choices())
    elif action == "sessions":
        await query.edit_message_text(
            f"选择要继续的会话\n📁 当前工作区：{_state.current_project_label or _project_label(_state.current_cwd)}"
        )
        cards = _build_session_cards()
        if cards:
            await channel.send_session_cards(
                "会话已按需要处理、运行中和最近更新排序。", cards
            )
    elif action == "active":
        cards = [card for card in _build_session_cards() if "运行中" in card["title"] or "等待回复" in card["title"]]
        await query.edit_message_text(
            f"当前有 {len(cards)} 个运行中或等待处理的会话。" if cards else "当前没有运行中或等待处理的会话。"
        )
        if cards:
            await channel.send_session_cards("运行与待处理会话：", cards)
    elif action == "project":
        await query.edit_message_text(f"当前工作区：{_state.current_cwd}\n请选择要切换的工作区：")
        await channel.send_project_choices(_workspace_choices("switch"), title="切换工作区")
    elif action == "queue":
        if not _state.task_queue:
            await query.edit_message_text("队列为空，当前没有排队中的任务。")
        else:
            lines = [f"任务队列（共 {len(_state.task_queue)} 个）："]
            for index, task in enumerate(_state.task_queue, start=1):
                session_hint = task.session_key[:8] if task.session_key and not task.session_key.startswith("new:") else "新会话"
                lines.append(
                    f"{index}. #{task.task_number} | {session_hint} | {_project_label(task.project_path)} | {task.prompt[:60]}"
                )
            await query.edit_message_text("\n".join(lines))
    elif action == "new":
        await _send_new_session_workspace_picker(query.edit_message_text)
    elif action == "help":
        await query.edit_message_text(
            "直接发送消息会进入当前会话；回复某条 Bot 消息会进入那条消息所属会话。\n"
            "使用「选择会话」「新建会话」「切换工作区」即可完成日常操作。\n"
            "发送 /help 可查看兼容命令。"
        )
    elif action == "stop":
        if _stop_session_run(_state.current_session_id):
            await query.edit_message_text(f"已请求停止：{_state.current_project_label}")
        else:
            await query.edit_message_text("当前焦点会话没有正在执行的任务。")
    else:
        await query.edit_message_text("未知操作。请重新发送 /start 打开快捷面板。")


# ---------------- 按钮回调 ----------------


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    query = update.callback_query
    await query.answer()
    if query.data.startswith("dashboard:"):
        await _handle_dashboard_action(query.data.split(":", 1)[1], query)
        return
    if query.data.startswith("sess:"):
        await _handle_session_action(query)
        return
    if query.data.startswith("workspace:"):
        try:
            _, action, raw_index = query.data.split(":", 2)
        except ValueError:
            await query.edit_message_text("工作区按钮数据无效。请重新打开快捷面板。")
            return
        if action == "cancel":
            _state.cancel_new_session()
            await query.edit_message_text("已取消。当前选择未改变。")
            return
        target = _resolve_workspace_index(raw_index)
        if not target:
            await query.edit_message_text("该工作区已不在列表中。请重新打开快捷面板。")
            return
        _state.set_current_project(target)
        if action == "new":
            _state.begin_new_session(target)
            await query.edit_message_text(
                "✍️ 已准备新会话\n"
                f"📁 工作区：{_state.current_project_label}\n"
                f"路径：{_state.current_cwd}\n\n"
                "请直接发送第一条任务。发送前不会创建空会话；10 分钟内未发送将自动取消。"
            )
            return
        if action != "switch":
            await query.edit_message_text("未知的工作区操作。请重新打开快捷面板。")
            return
        suggested_id = _state.project_last_session.get(_state.current_cwd, "")
        session = _state.sessions.get(suggested_id)
        _state.current_session_id = ""
        _state.cancel_new_session()
        await query.edit_message_text(
            f"✓ 已切换工作区「{_state.current_project_label}」\n"
            f"最近会话：{session.title if session else '无'}\n"
            "请选择继续最近会话、选择其他会话或新建会话。"
        )
        return
    if query.data.startswith("project:"):
        # Keep old cards usable after deploying this callback format.
        label = query.data.split(":", 1)[1].strip().lower()
        target = next((snap.path for snap in _state.projects.values() if snap.label.lower() == label), "")
        if not target:
            await query.edit_message_text("该工作区已不在列表中。请重新打开快捷面板。")
            return
        _state.set_current_project(target)
        suggested_id = _state.project_last_session.get(_state.current_cwd, "")
        session = _state.sessions.get(suggested_id)
        _state.current_session_id = ""
        _state.cancel_new_session()
        await query.edit_message_text(
            f"✓ 已切换工作区「{_state.current_project_label}」\n"
            f"最近会话：{session.title if session else '无'}\n"
            "请选择继续最近会话、选择其他会话或新建会话。"
        )
        return
    if query.data.startswith("monitor:"):
        try:
            _, source, session_id = query.data.split(":", 2)
        except ValueError:
            await query.edit_message_text("会话按钮数据无效。请重新发送 /monitor。")
            return
        item = _resolve_monitored_session(f"{source}:{session_id}")
        if item is None:
            await query.edit_message_text("该会话已不在本机监控列表中。请重新发送 /monitor。")
            return
        await query.edit_message_text(_format_monitored_session_detail(item))
        return
    choice = channel.resolve_approval(query.data)
    if choice is None:
        await query.edit_message_text(query.message.text + "\n\n（已失效/超时）")
        return

    # Map the button back to the session that is waiting for this choice.
    try:
        choice_id = query.data.split(":", 2)[1]
    except (ValueError, IndexError):
        choice_id = ""
    session_key = _choice_session.get(choice_id, "")
    pending = _state.pending_by_session.get(session_key) if session_key else None
    if pending is None:
        await query.edit_message_text(query.message.text + "\n\n（没有待处理的问题了）")
        return

    snap = _state.ensure_project(pending.project_path)
    snap.add_entry("user/choice", choice, pending.session_id)
    _state.mark_running(f"收到你的选择：{choice}", pending.project_path, pending.session_id)
    await query.edit_message_text(
        query.message.text
        + f"\n\n✓ 已回复到「{_session_name(pending.session_id)}」：{choice}"
    )

    _start_run(choice, pending.project_path, pending.session_id, pending.session_id)


async def _handle_session_action(query):
    """Handle a ``sess:<action>:<session_id>`` button from the session cards."""
    try:
        _, action, session_id = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_text("会话按钮数据无效。请重新发送 /sessions。")
        return
    if action == "detail":
        item = _find_session_record(session_id)
        if item is None:
            await query.edit_message_text("该会话已不在本机列表中。请重新发送 /sessions。")
            return
        await query.edit_message_text(_format_monitored_session_detail(item))
        return
    if action == "focus":
        if not _is_controllable_session(session_id):
            await query.edit_message_text("该会话是旧 Claude 只读会话，无法设为控制焦点。")
            return
        session = _state.sessions.get(session_id)
        if session is None:
            session = _adopt_global_session(session_id)
        if session is None:
            await query.edit_message_text("该会话暂时无法设为焦点。")
            return
        _state.set_current_project(session.project_path)
        _state.current_session_id = session_id
        _state.cancel_new_session()
        await query.edit_message_text(
            f"✓ 已设为当前会话「{session.title}」\n"
            f"📁 工作区：{session.project_label}\n"
            f"状态：{_session_status_label(session.mode)}\n\n"
            "直接发送消息即可继续这个会话。"
        )
        return
    if action == "stop":
        if _stop_session_run(session_id):
            await query.edit_message_text(f"已请求停止会话 {session_id[:8]}。")
        else:
            await query.edit_message_text(f"会话 {session_id[:8]} 当前没有正在执行的任务。")
        return
    if action == "adopt":
        if not _is_controllable_session(session_id):
            await query.edit_message_text("该会话是旧 Claude 只读会话，无法接管控制。")
            return
        blocked = _external_session_blocked(session_id)
        if blocked:
            await query.edit_message_text(blocked)
            return
        session = _adopt_global_session(session_id)
        if session is None:
            await query.edit_message_text("该会话暂时无法接管。")
            return
        _state.set_current_project(session.project_path)
        _state.current_session_id = session_id
        _state.cancel_new_session()
        await query.edit_message_text(
            f"✓ 已接管并设为当前会话「{session.title}」\n"
            f"📁 工作区：{session.project_label}\n"
            "直接发送消息即可继续。"
        )
        return
    await query.edit_message_text("未知的会话操作。请重新发送 /sessions。")


# ---------------- 普通消息 = 新任务 / 继续回答 ----------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return

    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    async with _task_lock:
        # ---- 路由优先级：回复绑定 > @前缀 > 显式新建态 > 当前会话 ----
        reply_key = _reply_message_key(update)
        reply_binding = _state.message_bindings.get(reply_key) if reply_key else None
        reply_routed = reply_binding is not None
        if reply_key and reply_binding is None:
            await update.message.reply_text(
                "这条历史消息已无法定位到原会话，为避免发错，我没有发送。\n"
                "请从会话列表选择目标，或新建会话。"
            )
            cards = _build_session_cards()
            if cards:
                await channel.send_session_cards("请选择目标会话：", cards)
            return

        routed_session_id, remainder, routed = _resolve_target(prompt)
        if reply_routed:
            routed_session_id = str(reply_binding.get("session_id") or "")
            target_path = _norm_path(str(reply_binding.get("project_path") or _state.current_cwd))
            routed = True
        elif routed:
            prompt = remainder
            target_path = _state.current_cwd
        else:
            target_path = _state.current_cwd

        if routed:
            if not _is_controllable_session(routed_session_id):
                await update.message.reply_text(
                    "该会话是旧 Claude 只读会话，无法用 Codex 继续。请回到原终端操作。"
                )
                return
            if routed_session_id not in _state.sessions:
                blocked = _external_session_blocked(routed_session_id)
                if blocked:
                    await update.message.reply_text(blocked)
                    return
                adopted = _adopt_global_session(routed_session_id, set_focus=not reply_routed)
                if adopted is None:
                    await update.message.reply_text("这个会话目前只能查看摘要，暂时还不能接管。")
                    return
            target_path = _state.sessions[routed_session_id].project_path
            if not reply_routed:
                _state.cancel_new_session()
                _state.set_current_project(target_path)
                _state.current_session_id = routed_session_id
            if not prompt:
                # "@<id>" 单独一条 = 仅切换焦点
                session = _state.sessions[routed_session_id]
                await update.message.reply_text(
                    f"✓ 已设为当前会话「{session.title}」\n"
                    f"📁 工作区：{session.project_label}\n"
                    "直接发送消息即可继续。"
                )
                return
        else:
            target_session_id = _state.current_session_id or ""
            if target_session_id and target_session_id not in _state.sessions:
                # 焦点会话不在托管表（如状态文件异常）：尝试重新接管
                adopted = _adopt_global_session(target_session_id)
                if adopted is not None:
                    _state.set_current_project(adopted.project_path)
                else:
                    target_session_id = ""
        if routed:
            target_session_id = routed_session_id

        force_new = _state.new_session_pending()
        if not target_session_id and not force_new:
            await update.message.reply_text(
                "还没有明确发送目标。请选择一个会话，或先新建会话；这条消息尚未发送。"
            )
            cards = _build_session_cards()
            if cards:
                await channel.send_session_cards("要把这条消息发到哪里？", cards)
            else:
                await _send_new_session_workspace_picker(channel.send_text)
            return
        if force_new:
            target_path = _state.new_session_project

        # ---- 目标会话正在等待回复 → 作为答复继续它 ----
        pending = _state.pending_by_session.get(target_session_id) if target_session_id else None
        if pending is not None:
            _prune_managed_waiting_notice(pending.session_id)
            session_id = pending.session_id
            _state.clear_pending(session_id)
            _state.ensure_project(target_path).add_entry("user", prompt, session_id)
            _state.mark_running(f"收到你的回复，继续处理 {pending.project_label}", target_path, session_id)
            await update.message.reply_text(
                f"✓ 已回复到「{_session_name(session_id)}」\n📁 {pending.project_label}"
            )
            _start_run(prompt, target_path, session_id, session_id)
            return

        # ---- 目标会话忙 → 排入该会话队列 ----
        target_running = target_session_id in _running_tasks and not _running_tasks[target_session_id].done()
        if target_running:
            force_new = False
            queued = _state.enqueue_task(
                prompt, target_path, target_session_id, force_new, session_key=target_session_id
            )
            await update.message.reply_text(
                f"🕓 已加入「{_session_name(target_session_id)}」队列。\n"
                f"队列位置：{len(_state.task_queue)} | 任务编号：#{queued.task_number}\n"
                f"工作区：{target_path}\n"
                f"内容：{prompt[:100]}\n"
                "可用 /queue 查看，/queue clear 清空。"
            )
            return

        # ---- 空闲 → 直接开始 ----
        session_id = "" if force_new else target_session_id
        _state.start_task(prompt, target_path, session_id, force_new, select_target=not reply_routed)
        session_key = session_id or f"new:{_state.current_task_id}"
        action = "新会话" if force_new or not session_id else f"继续「{_session_name(session_id)}」"
        await update.message.reply_text(
            f"✓ 已开始任务 #{_state.current_task_id}\n"
            f"📁 {_project_label(target_path)}\n"
            f"💬 {action}\n"
            f"内容：{prompt[:120]}"
        )
        _start_run(prompt, target_path, session_key, session_id)


async def _background_poll_loop(app: Application):
    await background_poll_loop()


async def background_poll_loop():
    """Shared session-monitor polling loop used by Telegram and Feishu."""
    while True:
        try:
            await _poll_external_session_status()
            await _poll_global_sessions(None)
        except Exception:
            log.exception("global session poll failed")
        await asyncio.sleep(config.SESSION_MONITOR_POLL_SECONDS)


async def _post_init(app: Application):
    asyncio.create_task(background_poll_loop())
    asyncio.create_task(_autosave_loop())
    asyncio.create_task(_drain_queues())


# ---------------- 飞书适配入口 ----------------

class _FeishuChat:
    def __init__(self, open_id: str):
        self.id = open_id


class _FeishuMessage:
    def __init__(self, text: str, transport, chat_id: str = "", reply_message_id: str = ""):
        self.text = text
        self._transport = transport
        self.reply_message_key = (
            f"feishu:{chat_id}:{reply_message_id}" if chat_id and reply_message_id else ""
        )

    async def reply_text(self, text: str):
        await self._transport.send_text(text)


class _FeishuQuery:
    def __init__(self, data: str, transport):
        self.data = data
        self._transport = transport
        self.message = type("Message", (), {"text": "飞书交互卡片"})()

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text: str):
        # Card updates need a message id. Sending a confirmation is clearer and
        # works for both private chats and group chats without extra state.
        await self._transport.send_text(text)


class _FeishuUpdate:
    def __init__(
        self,
        open_id: str,
        transport,
        text: str = "",
        callback_data: str = "",
        chat_id: str = "",
        reply_message_id: str = "",
    ):
        self.effective_chat = _FeishuChat(open_id)
        self.message = _FeishuMessage(text, transport, chat_id, reply_message_id) if not callback_data else None
        self.callback_query = _FeishuQuery(callback_data, transport) if callback_data else None


class _FeishuContext:
    def __init__(self, args: list[str] | None = None):
        self.args = args or []


async def dispatch_feishu_text(
    open_id: str,
    chat_id: str,
    text: str,
    transport,
    reply_message_id: str = "",
):
    """Translate a Feishu text event into the existing command/message core."""
    transport.set_chat_id(chat_id)
    # Strip Feishu's own mention placeholders ("@_user_1 …") so that an
    # @-mention of the bot does not shadow the "@会话前8位" routing prefix.
    text = re.sub(r"@_user_\d+\s*", "", text).strip()
    update = _FeishuUpdate(
        open_id,
        transport,
        text=text,
        chat_id=chat_id,
        reply_message_id=reply_message_id,
    )
    if not text:
        return
    command, _, remainder = text.partition(" ")
    handlers = {
        "/start": cmd_start, "/help": cmd_help, "/project": cmd_project,
        "/new": cmd_new, "/sessions": cmd_sessions, "/use": cmd_use,
        "/focus": cmd_use, "/status": cmd_status, "/monitor": cmd_monitor,
        "/stop": cmd_stop, "/queue": cmd_queue, "/health": cmd_health,
    }
    handler = handlers.get(command.lower())
    if handler:
        await handler(update, _FeishuContext(remainder.split() if remainder else []))
    else:
        await on_message(update, _FeishuContext())


async def dispatch_feishu_button(open_id: str, callback_data: str, transport):
    """Translate a Feishu interactive-card button click into the shared flow."""
    if not callback_data:
        return
    await on_button(_FeishuUpdate(open_id, transport, callback_data=callback_data), _FeishuContext())


# ---------------- 启动 ----------------

def main():
    os.makedirs("logs", exist_ok=True)
    try:
        guard = single_instance.acquire()
    except single_instance.SingleInstanceError as exc:
        raise SystemExit(str(exc)) from exc
    import atexit

    atexit.register(guard.release)
    missing = config.validate()
    if missing:
        raise SystemExit(config.format_missing_config(missing))
    web_dashboard.start_dashboard()
    if not config.CODEX_REMOTE:
        # 远程模式下 DEFAULT_PROJECTS 是 Codex 机器（家里电脑）上的路径，
        # 不在云端 bot 所在机器创建。
        for path in config.DEFAULT_PROJECTS:
            os.makedirs(path, exist_ok=True)

    if config.BOT_CHANNEL == "feishu":
        from feishu_bridge import run as run_feishu
        log.info("启动飞书长连接机器人，工作目录=%s", _state.current_cwd)
        run_feishu()
        return

    app = Application.builder().token(config.TELEGRAM_TOKEN).post_init(_post_init).build()

    channel.init(app.bot, config.ALLOWED_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("focus", cmd_use))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("机器人启动，工作目录=%s", _state.current_cwd)
    print("机器人已启动，去 Telegram 给它发消息吧。Ctrl+C 退出。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
