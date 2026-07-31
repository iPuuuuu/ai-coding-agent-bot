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
        lines.append("全局 Claude 会话：")
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
    for session_id in _state.sessions:
        if session_id.startswith(prefix):
            return session_id
    for session_id in _load_global_sessions():
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
        return ["暂无全局 Claude 会话记录。"]
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


def _format_waiting_message(session_id: str, item: dict) -> str:
    cwd = _project_label(item.get("cwd", ""))
    reason = str(item.get("waiting_reason") or item.get("last_text") or "Claude 正在等待你的输入").strip()
    return (
        f"⚠️ Claude 会话需要你操作\n"
        f"会话：{session_id[:8]}\n"
        f"项目：{cwd}\n\n"
        f"{reason[:1200]}"
    )


def _adopt_global_session(session_id: str) -> ManagedSession | None:
    sessions = _load_global_sessions()
    item = sessions.get(session_id)
    if not item:
        return None
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
            prompt_text=str(item.get("waiting_reason") or item.get("last_text") or "Claude 正在等待你的回复"),
            options=list(item.get("choice_options") or []),
            source="buttons" if item.get("choice_options") else "text",
        )
    return session


_state = RuntimeState()
for _path in config.DEFAULT_PROJECTS:
    _state.ensure_project(_path)
_state.set_current_project(config.DEFAULT_PROJECT_DIR)
_task_lock = asyncio.Lock()
_current_task: Optional[asyncio.Task] = None
_seen_global_event_offset = 0
_last_waiting_notice: dict[str, str] = {}


def _authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == config.ALLOWED_CHAT_ID)


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
        if not session_id:
            continue
        item = sessions.get(session_id, {})
        if not item.get("waiting"):
            continue
        reason = str(item.get("waiting_reason", "") or item.get("last_text", "")).strip()
        if not reason:
            continue
        if _last_waiting_notice.get(session_id) == reason:
            continue
        _last_waiting_notice[session_id] = reason
        message = _format_waiting_message(session_id, item)
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


async def _run_agent(prompt: str, project_path: str, session_id: str):
    global _current_task
    snap = _state.ensure_project(project_path)
    try:
        outcome = await agent_runner.run_turn(prompt, project_path, session_id=session_id or None)
        _state.append_events(project_path, outcome.events)
        if outcome.session_id:
            session = _state.bind_session(outcome.session_id, project_path, initial_prompt=prompt)
            session.last_prompt = prompt.strip() or session.last_prompt
            session.mode = "running"
            session.last_event = outcome.final_text[:120] if outcome.final_text else session.last_event
            session.updated_at = time.time()
            _state.current_session_id = outcome.session_id
        active_session_id = outcome.session_id or session_id
        if outcome.state == "waiting":
            pending = PendingInteraction(
                project_path=project_path,
                project_label=snap.label,
                session_id=active_session_id,
                prompt_text=outcome.waiting_text or "Claude 正在等待你的回复",
                options=outcome.choice_options,
                source="buttons" if outcome.choice_options else "text",
            )
            _state.mark_waiting(pending)
            if pending.options:
                choice = await channel.ask_choice(
                    f"{pending.project_label} 会话 {pending.session_id[:8]} 有选项要你选：\n\n{pending.prompt_text}",
                    pending.options,
                    timeout=config.APPROVAL_TIMEOUT,
                )
                if choice is None:
                    _state.mark_waiting(pending)
                    await channel.send_text(
                        f"{pending.project_label} 会话 {pending.session_id[:8]} 选择超时，你也可以直接发文字继续。"
                    )
                else:
                    snap.add_entry("user/choice", choice, pending.session_id)
                    _state.mark_running(f"收到你的选择：{choice}", pending.project_path, pending.session_id)
                    await channel.send_text(
                        f"你选择了：{choice}\n继续处理 {pending.project_label} / {pending.session_id[:8]}…"
                    )
                    _current_task = asyncio.create_task(_run_agent(choice, pending.project_path, pending.session_id))
                    return
            else:
                await channel.send_text(
                    f"{pending.project_label} 会话 {pending.session_id[:8]} 正在等你回复。"
                )
        elif _state.stop_requested:
            _state.mark_done("任务已停止")
            await channel.send_text(f"{snap.label} 已停止。")
        else:
            _state.mark_done("任务完成")
    except asyncio.CancelledError:
        _state.mark_done("任务已取消")
        await channel.send_text(f"{snap.label} 已取消。")
        raise
    except Exception as e:
        log.exception("run_agent failed")
        _state.mark_error(f"Agent 运行出错：{type(e).__name__}: {e}")
        await channel.send_text(f"{snap.label} 出错：{type(e).__name__}: {e}")
    finally:
        if _current_task is not None and _current_task.done():
            _current_task = None


# ---------------- 命令 ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    projects = "\n".join(f"- {snap.label}: {snap.path}" for snap in _state.projects.values())
    await update.message.reply_text(
        "Claude 托管会话机器人已上线。\n\n"
        f"当前项目：{_state.current_project_label}\n"
        "监控项目：\n"
        f"{projects}\n\n"
        "直接发任务即可继续当前托管会话；/new 让下一条消息新开会话；/sessions 看会话；/use 切换会话；/status 看项目面板。"
    )


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
        f"已标记：下一条发给 {_state.current_project_label} 的消息将新开 Claude 会话。"
    )


async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    text = _state.session_summary() + "\n\n全局 Claude 会话：\n" + "\n".join(_format_global_sessions())
    await update.message.reply_text(text)


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
    await update.message.reply_text(_state.summary())


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


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(
        "直接发文字 = 继续当前项目的当前会话（没有会话则新建）\n"
        "/new 让下一条消息强制新开会话\n"
        "/sessions 查看托管会话\n"
        "/use <session_id前8位> 切换会话\n"
        "/project <bot|doctor-wang|路径> 切项目\n"
        "/status 查看项目面板 + 最近窗口\n"
        "/stop 停止当前任务\n"
        "如果 Claude 给出几个选项，我会优先发按钮让你点。"
    )


# ---------------- 按钮回调 ----------------

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    query = update.callback_query
    await query.answer()
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
    while True:
        try:
            await _poll_global_sessions(None)
        except Exception:
            log.exception("global session poll failed")
        await asyncio.sleep(3)


async def _post_init(app: Application):
    asyncio.create_task(_background_poll_loop(app))


# ---------------- 启动 ----------------

def main():
    os.makedirs("logs", exist_ok=True)
    missing = config.validate()
    if missing:
        raise SystemExit(config.format_missing_config(missing))
    for path in config.DEFAULT_PROJECTS:
        os.makedirs(path, exist_ok=True)

    app = Application.builder().token(config.TELEGRAM_TOKEN).post_init(_post_init).build()

    channel.init(app.bot, config.ALLOWED_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("机器人启动，工作目录=%s", _state.current_cwd)
    print("机器人已启动，去 Telegram 给它发消息吧。Ctrl+C 退出。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
