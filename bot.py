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
import shutil
import time
from dataclasses import dataclass, field
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
from tools.codex_session_monitor import scan_sessions as scan_codex_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join("logs", "bot.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")


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
    start_new_session_next: bool = False

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

    def bind_session(self, session_id: str, project_path: str, initial_prompt: str = "") -> ManagedSession:
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
        self.current_session_id = session_id
        return session

    def get_current_session(self) -> Optional[ManagedSession]:
        if self.current_session_id:
            session = self.sessions.get(self.current_session_id)
            if session is not None:
                return session
        session_id = self.project_last_session.get(self.current_cwd, "")
        if session_id:
            return self.sessions.get(session_id)
        return None

    def choose_session_for_prompt(self, force_new: bool) -> str:
        if force_new:
            return ""
        pending = self.pending
        if pending is not None and self.mode == "waiting_user_reply":
            return pending.session_id
        session = self.get_current_session()
        return session.session_id if session else ""

    def start_task(self, prompt: str, project_path: str, session_id: str, force_new: bool):
        norm = _norm_path(project_path)
        snap = self.ensure_project(norm)
        self.task_counter += 1
        self.current_task_id = self.task_counter
        self.current_cwd = norm
        self.current_project_label = snap.label
        self.current_session_id = session_id
        self.active_prompt = prompt.strip()
        self.started_at = time.time()
        self.mode = "running"
        self.stop_requested = False
        self.waiting_reason = ""
        self.last_error = ""
        self.pending = None
        self.start_new_session_next = False
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
        self.pending = pending
        self.current_cwd = pending.project_path
        self.current_project_label = pending.project_label
        self.current_session_id = pending.session_id
        self.waiting_reason = pending.prompt_text[:120]
        self.last_event = f"{pending.project_label} 等待你的回复"
        snap.mode = "waiting_user_reply"
        snap.waiting_reason = self.waiting_reason
        snap.last_event = self.last_event
        snap.last_active_at = time.time()
        self.bind_session(pending.session_id, pending.project_path)
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
        self.current_cwd = target_path
        self.current_project_label = snap.label
        if session_id:
            self.current_session_id = session_id
        snap.mode = "running"
        snap.waiting_reason = ""
        snap.last_event = event
        snap.last_active_at = time.time()
        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            session.mode = "running"
            session.last_event = event
            session.updated_at = time.time()

    def mark_done(self, event: str = "任务完成"):
        snap = self.ensure_project(self.current_cwd)
        self.mode = "idle"
        self.last_event = event
        self.waiting_reason = ""
        self.active_prompt = ""
        self.started_at = None
        self.stop_requested = False
        self.current_task_id = None
        self.pending = None
        self.start_new_session_next = False
        snap.mode = "idle"
        snap.last_event = event
        snap.waiting_reason = ""
        snap.active_prompt = ""
        snap.last_active_at = time.time()
        snap.add_entry("system", event, self.current_session_id)
        session = self.get_current_session()
        if session is not None:
            session.mode = "idle"
            session.last_event = event
            session.updated_at = time.time()

    def mark_error(self, error: str):
        snap = self.ensure_project(self.current_cwd)
        self.mode = "idle"
        self.last_error = error
        self.last_event = error
        self.waiting_reason = ""
        self.active_prompt = ""
        self.started_at = None
        self.stop_requested = False
        self.current_task_id = None
        self.pending = None
        self.start_new_session_next = False
        snap.mode = "idle"
        snap.last_error = error
        snap.last_event = error
        snap.waiting_reason = ""
        snap.active_prompt = ""
        snap.last_active_at = time.time()
        snap.add_entry("error", error, self.current_session_id)
        session = self.get_current_session()
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

    def append_events(self, project_path: str, events: list[agent_runner.MirrorEvent]):
        snap = self.ensure_project(project_path)
        for event in events:
            snap.add_entry(event.kind, event.text, event.session_id)
            if event.session_id:
                self.bind_session(event.session_id, project_path)
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
                label = entry.session_id[:8] if entry.session_id else "-"
                lines.append(f"- [{entry.kind}/{label}] {entry.text[:100]}")
        return "\n".join(lines)


def _norm_path(path: str) -> str:
    return os.path.normpath(path).replace("\\", "/")


def _project_label(path: str) -> str:
    base = os.path.basename(_norm_path(path))
    return base or _norm_path(path)


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


def _load_global_sessions() -> dict[str, dict]:
    path = config.SESSION_SNAPSHOT_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    sessions = data.get("sessions", {})
    return sessions if isinstance(sessions, dict) else {}


def _format_global_sessions(limit: int = 8) -> list[str]:
    sessions = _load_global_sessions()
    if not sessions:
        return ["暂无全局 Codex 会话记录。"]
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
    """Read local Codex rollout metadata without exposing rollout contents."""
    try:
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
        record["monitor_key"] = f"claude:{record.get('session_id', '')}"
        records.append(record)
    for item in _load_codex_sessions().values():
        record = dict(item)
        record["source"] = "codex"
        record["monitor_key"] = f"codex:{record.get('session_id', '')}"
        records.append(record)
    return sorted(records, key=lambda item: float(item.get("updated_at", 0) or 0), reverse=True)


def _format_monitor_row(item: dict) -> str:
    source = str(item.get("source", "?")).title()
    session_id = str(item.get("session_id", ""))[:8] or "-"
    status = _session_status_label(str(item.get("status", "unknown")))
    project = _project_label(str(item.get("cwd", ""))) if item.get("cwd") else "-"
    return f"[{source}] {session_id} | {status} | {project}"


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
        f"项目：{project}",
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
        f"会话：{session_id[:8]}\n"
        f"项目：{cwd}\n\n"
        f"{reason[:1200]}"
    )


def _adopt_global_session(session_id: str) -> ManagedSession | None:
    sessions = _load_global_sessions()
    item = sessions.get(session_id)
    if item:
        cwd = _norm_path(item.get("cwd", config.DEFAULT_PROJECT_DIR) or config.DEFAULT_PROJECT_DIR)
        _state.ensure_project(cwd)
        session = _state.bind_session(session_id, cwd)
        session.title = str(item.get("last_text") or item.get("last_event") or session.title)[:48] or session.title
        session.mode = "waiting_user_reply" if item.get("waiting") else item.get("status", "active")
        session.last_event = str(item.get("last_event", session.last_event))
        session.updated_at = float(item.get("updated_at", time.time()) or time.time())
        if item.get("waiting"):
            _state.pending = PendingInteraction(
                project_path=cwd,
                project_label=_project_label(cwd),
                session_id=session_id,
                prompt_text=str(item.get("waiting_reason") or item.get("last_text") or "Codex 正在等待你的回复"),
                options=list(item.get("choice_options") or []),
                source="buttons" if item.get("choice_options") else "text",
            )
        return session

    # Adopt a local Codex session (created by this bot or another Codex CLI).
    codex_item = _load_codex_sessions().get(session_id)
    if codex_item:
        cwd = _norm_path(codex_item.get("cwd") or config.DEFAULT_PROJECT_DIR)
        _state.ensure_project(cwd)
        session = _state.bind_session(session_id, cwd)
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
_task_lock = asyncio.Lock()
_current_task: Optional[asyncio.Task] = None
_seen_global_event_offset = 0
_last_waiting_notice: dict[str, str] = {}
_external_session_states: dict[str, str] = {}
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
            choice = await channel.ask_choice(message, options, timeout=config.APPROVAL_TIMEOUT)
            if choice is not None:
                adopted = _adopt_global_session(session_id)
                if adopted is not None:
                    _state.pending = PendingInteraction(
                        project_path=adopted.project_path,
                        project_label=adopted.project_label,
                        session_id=session_id,
                        prompt_text=reason,
                        options=options,
                        source="buttons",
                    )
                    _state.set_current_project(adopted.project_path)
                    _state.current_session_id = session_id
                    _state.mark_running(f"收到你的选择：{choice}", adopted.project_path, session_id)
                    asyncio.create_task(_run_agent(choice, adopted.project_path, session_id))
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

    current: dict[str, str] = {}
    for source, session_id, item in snapshots:
        if not session_id:
            continue
        key = f"{source}:{session_id}"
        status = str(item.get("status", "unknown"))
        current[key] = status
        previous = _external_session_states.get(key)
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
    _external_session_monitor_ready = True


async def _run_agent(prompt: str, project_path: str, session_id: str):
    global _current_task
    snap = _state.ensure_project(project_path)
    started_at = time.time()
    heartbeat = asyncio.create_task(_heartbeat_loop(snap, prompt, started_at))
    try:
        outcome = await agent_runner.run_turn(prompt, project_path, session_id=session_id or None)
        _state.append_events(project_path, outcome.events)
        active_session_id = outcome.session_id or session_id
        if outcome.session_id:
            session = _state.bind_session(outcome.session_id, project_path, initial_prompt=prompt)
            session.last_prompt = prompt.strip() or session.last_prompt
            session.mode = "running"
            session.last_event = outcome.final_text[:120] if outcome.final_text else session.last_event
            session.updated_at = time.time()
            _state.current_session_id = outcome.session_id
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
                },
            )
            if pending.options:
                choice = await channel.ask_choice(waiting_message, pending.options, timeout=config.APPROVAL_TIMEOUT)
                if choice is None:
                    _state.mark_waiting(pending)
                    await channel.send_text(
                        f"{pending.project_label} 会话 {pending.session_id[:8]} 选择超时，你也可以直接发文字继续。"
                    )
                else:
                    snap.add_entry("user/choice", choice, pending.session_id)
                    _state.mark_running(f"收到你的选择：{choice}", pending.project_path, pending.session_id)
                    _prune_managed_waiting_notice(pending.session_id)
                    await channel.send_text(
                        f"你选择了：{choice}\n继续处理 {pending.project_label} / {pending.session_id[:8]}…"
                    )
                    _current_task = asyncio.create_task(_run_agent(choice, pending.project_path, pending.session_id))
                    return
            else:
                await channel.send_text(waiting_message)
        elif _state.stop_requested:
            _prune_managed_waiting_notice(active_session_id)
            _state.mark_done("任务已停止")
            await channel.send_text(f"⏹️ {snap.label} 已停止（用时 {int(time.time() - started_at)}s）。")
        else:
            _prune_managed_waiting_notice(active_session_id)
            _state.mark_done("任务完成")
            await channel.send_text(f"✅ {snap.label} 任务完成（用时 {int(time.time() - started_at)}s）。")
    except asyncio.CancelledError:
        _prune_managed_waiting_notice(session_id)
        _state.mark_done("任务已取消")
        await channel.send_text(f"{snap.label} 已取消。")
        raise
    except Exception as e:
        log.exception("run_agent failed")
        _prune_managed_waiting_notice(session_id)
        _state.mark_error(f"Agent 运行出错：{type(e).__name__}: {e}")
        await channel.send_text(f"{snap.label} 出错：{type(e).__name__}: {e}")
    finally:
        heartbeat.cancel()
        if _current_task is not None and _current_task.done():
            _current_task = None


async def _heartbeat_loop(snap: ProjectSnapshot, prompt: str, started_at: float):
    """Periodically report that a long task is still alive."""
    interval = config.HEARTBEAT_SECONDS
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        elapsed = int(time.time() - started_at)
        await channel.send_status(
            f"⏳ {snap.label} 任务仍在执行… 已运行 {elapsed}s\n"
            f"内容：{prompt[:80]}"
        )


# ---------------- 命令 ----------------

async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if not ctx.args:
        choices = "\n".join(f"- {snap.label}: {snap.path}" for snap in _state.projects.values())
        await update.message.reply_text(
            f"当前项目：{_state.current_project_label}\n可选项目：\n{choices}\n用法：/project bot 或 /project C:/code/myapp"
        )
        return
    if _state.mode == "running":
        await update.message.reply_text("当前有任务在执行，先 /stop 或等它结束。")
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
    session = _state.get_current_session()
    session_text = session.short_id if session else "无"
    await update.message.reply_text(
        f"已切到：{_state.current_project_label}\n路径：{_state.current_cwd}\n当前会话：{session_text}"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if _state.mode == "running":
        await update.message.reply_text("当前任务正在执行，等结束后再新开会话。")
        return
    _state.start_new_session_next = True
    await update.message.reply_text(
        f"已标记：下一条发给 {_state.current_project_label} 的消息将新开 Codex 会话。"
    )


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if not ctx.args:
        await update.message.reply_text("用法：/use <session_id前8位>")
        return
    if _state.mode == "running":
        await update.message.reply_text("当前任务在执行，先 /stop 或等结束后再切会话。")
        return
    session_id = _resolve_session_id(ctx.args[0])
    if not session_id:
        await update.message.reply_text("没找到这个会话。先用 /sessions 看可用会话。")
        return
    session = _state.sessions.get(session_id)
    if session is None:
        session = _adopt_global_session(session_id)
    if session is None:
        await update.message.reply_text("这个会话目前只能看到摘要，暂时还不能接管。")
        return
    _state.set_current_project(session.project_path)
    _state.current_session_id = session_id
    _state.start_new_session_next = False
    await update.message.reply_text(
        f"已切到会话：{session.short_id}\n项目：{session.project_label}\n标题：{session.title}"
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
    await update.message.reply_text(
        "Codex 会话机器人已上线（旧 Claude 会话仅保留只读监控）。\n"
        "下方卡片可直接查看状态、监控会话、切换项目或新建会话。\n"
        "发送 /health 可检查本机环境；完整命令见 /help。"
    )
    await channel.send_command_menu()


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(
        "常用操作请直接点击下方卡片。\n"
        "也可发送 /monitor、/status、/sessions、/project、/new、/use、/stop、/health。"
    )
    await channel.send_command_menu()


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
        f"沙箱：{config.CODEX_SANDBOX}",
        f"项目数：{len(_state.projects)}",
        f"心跳间隔：{config.HEARTBEAT_SECONDS}s",
    ]
    if config.CODEX_MODEL:
        lines.append(f"Codex 模型：{config.CODEX_MODEL}")
    await update.message.reply_text("\n".join(lines))


async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show managed sessions plus the unified local Claude/Codex monitor."""
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(_state.session_summary() + "\n\n" + _format_session_overview())


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
    global _current_task
    if action == "status":
        await query.edit_message_text(_state.summary() + "\n\n" + _format_session_overview())
    elif action == "monitor":
        await query.edit_message_text(_format_session_overview())
        await channel.send_monitor_choices("点击一个会话，查看详情：", _monitor_button_choices())
    elif action == "sessions":
        await query.edit_message_text(_state.session_summary() + "\n\n" + _format_session_overview())
    elif action == "project":
        choices = [(snap.label, f"project:{snap.label}") for snap in _state.projects.values()]
        await query.edit_message_text(f"当前项目：{_state.current_project_label}\n请选择要切换的项目：")
        await channel.send_project_choices(choices)
    elif action == "new":
        if _state.mode == "running":
            await query.edit_message_text("当前任务仍在执行；结束后再新建会话。")
        else:
            _state.start_new_session_next = True
            await query.edit_message_text("已标记：你下一条普通消息将创建一个新的 Codex 会话。")
    elif action == "stop":
        if _state.mode == "idle" or _current_task is None:
            await query.edit_message_text("当前没有正在执行的机器人任务。")
        else:
            _state.request_stop()
            agent_runner.request_stop()
            if not _current_task.done():
                _current_task.cancel()
            await query.edit_message_text(f"已请求停止：{_state.current_project_label}")
    else:
        await query.edit_message_text("未知操作。请重新发送 /start 打开快捷面板。")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    global _current_task
    if _state.mode == "idle" or _current_task is None:
        await update.message.reply_text("当前没有正在执行的任务。")
        return
    _state.request_stop()
    agent_runner.request_stop()
    if not _current_task.done():
        _current_task.cancel()
    await update.message.reply_text(f"已请求停止：{_state.current_project_label}")


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
    if query.data.startswith("project:"):
        if _state.mode == "running":
            await query.edit_message_text("当前有任务正在执行，请先停止或等待它结束。")
            return
        label = query.data.split(":", 1)[1].strip().lower()
        target = next((snap.path for snap in _state.projects.values() if snap.label.lower() == label), "")
        if not target:
            await query.edit_message_text("该项目已不在可切换列表中。请重新发送 /start。")
            return
        _state.set_current_project(target)
        session = _state.get_current_session()
        session_label = session.short_id if session else "无"
        await query.edit_message_text(f"已切换到：{_state.current_project_label}\n当前会话：{session_label}")
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

    pending = _state.pending
    if pending is None:
        await query.edit_message_text(query.message.text + "\n\n（没有待处理的问题了）")
        return

    snap = _state.ensure_project(pending.project_path)
    snap.add_entry("user/choice", choice, pending.session_id)
    _state.set_current_project(pending.project_path)
    _state.current_session_id = pending.session_id
    _state.mark_running(f"收到你的选择：{choice}", pending.project_path, pending.session_id)
    await query.edit_message_text(query.message.text + f"\n\n你选择了：{choice}")

    global _current_task
    _current_task = asyncio.create_task(_run_agent(choice, pending.project_path, pending.session_id))


# ---------------- 普通消息 = 新任务 / 继续回答 ----------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return

    global _current_task
    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    async with _task_lock:
        if _state.mode == "running":
            await update.message.reply_text("当前任务还在执行。等它结束，或 /stop 后再发新任务。")
            return

        pending = _state.pending
        is_followup = _state.mode == "waiting_user_reply" and pending is not None
        if is_followup:
            session_id = pending.session_id
            target_path = pending.project_path
            _state.set_current_project(target_path)
            _state.current_session_id = session_id
            _state.ensure_project(target_path).add_entry("user", prompt, session_id)
            _state.mark_running(f"收到你的回复，继续处理 {pending.project_label}", target_path, session_id)
            await update.message.reply_text(
                f"继续处理：{pending.project_label} / {session_id[:8]}"
            )
        else:
            target_path = _state.current_cwd
            force_new = _state.start_new_session_next
            session_id = _state.choose_session_for_prompt(force_new)
            if session_id and session_id not in _state.sessions:
                adopted = _adopt_global_session(session_id)
                if adopted is not None:
                    target_path = adopted.project_path
                    _state.current_cwd = adopted.project_path
                    _state.current_project_label = adopted.project_label
            _state.start_task(prompt, target_path, session_id, force_new)
            action = "新会话" if force_new or not session_id else f"继续会话 {session_id[:8]}"
            await update.message.reply_text(
                f"已开始任务 #{_state.current_task_id}\n"
                f"项目：{_state.current_project_label}\n"
                f"模式：{action}\n"
                f"内容：{prompt[:120]}"
            )

        _current_task = asyncio.create_task(_run_agent(prompt, target_path, session_id))


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


# ---------------- 飞书适配入口 ----------------

class _FeishuChat:
    def __init__(self, open_id: str):
        self.id = open_id


class _FeishuMessage:
    def __init__(self, text: str, transport):
        self.text = text
        self._transport = transport

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
    def __init__(self, open_id: str, transport, text: str = "", callback_data: str = ""):
        self.effective_chat = _FeishuChat(open_id)
        self.message = _FeishuMessage(text, transport) if not callback_data else None
        self.callback_query = _FeishuQuery(callback_data, transport) if callback_data else None


class _FeishuContext:
    def __init__(self, args: list[str] | None = None):
        self.args = args or []


async def dispatch_feishu_text(open_id: str, chat_id: str, text: str, transport):
    """Translate a Feishu text event into the existing command/message core."""
    transport.set_chat_id(chat_id)
    update = _FeishuUpdate(open_id, transport, text=text)
    if not text:
        return
    command, _, remainder = text.partition(" ")
    handlers = {
        "/start": cmd_start, "/help": cmd_help, "/project": cmd_project,
        "/new": cmd_new, "/sessions": cmd_sessions, "/use": cmd_use,
        "/status": cmd_status, "/monitor": cmd_monitor, "/stop": cmd_stop,
        "/health": cmd_health,
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
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("机器人启动，工作目录=%s", _state.current_cwd)
    print("机器人已启动，去 Telegram 给它发消息吧。Ctrl+C 退出。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
