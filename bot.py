"""
bot.py —— 主入口：Telegram 桥接层。

职责：
- 收你的文字消息 → 交给 Agent 跑（新任务 / 继续回答）
- 处理按钮选择 → 唤醒挂起的交互
- 命令：/start /project /status /stop /help
- 白名单：只有你的 chat_id 能指挥

运行：  py -3.11 bot.py
"""
import asyncio
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
    pending: Optional[PendingInteraction] = None

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
        self.last_event = f"切换项目：{snap.label}"
        snap.last_event = self.last_event
        snap.last_active_at = time.time()
        snap.add_entry("system", self.last_event)

    def start_task(self, prompt: str):
        self.task_counter += 1
        self.current_task_id = self.task_counter
        self.active_prompt = prompt.strip()
        self.started_at = time.time()
        self.mode = "running"
        self.stop_requested = False
        self.waiting_reason = ""
        self.last_error = ""
        self.pending = None
        snap = self.ensure_project(self.current_cwd)
        self.current_project_label = snap.label
        self.last_event = f"{snap.label} 开始处理"
        snap.mode = "running"
        snap.last_event = self.last_event
        snap.last_error = ""
        snap.waiting_reason = ""
        snap.active_prompt = self.active_prompt
        snap.last_task_id = self.current_task_id
        snap.last_active_at = self.started_at
        snap.add_entry("user", self.active_prompt)

    def mark_waiting(self, pending: PendingInteraction):
        snap = self.ensure_project(pending.project_path)
        self.mode = "waiting_user_reply"
        self.pending = pending
        self.current_cwd = pending.project_path
        self.current_project_label = pending.project_label
        self.waiting_reason = pending.prompt_text[:120]
        self.last_event = f"{pending.project_label} 等待你的回复"
        snap.mode = "waiting_user_reply"
        snap.waiting_reason = self.waiting_reason
        snap.last_event = self.last_event
        snap.last_active_at = time.time()

    def mark_running(self, event: str):
        snap = self.ensure_project(self.current_cwd)
        self.mode = "running"
        self.waiting_reason = ""
        self.last_event = event
        snap.mode = "running"
        snap.waiting_reason = ""
        snap.last_event = event
        snap.last_active_at = time.time()

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
        snap.mode = "idle"
        snap.last_event = event
        snap.waiting_reason = ""
        snap.active_prompt = ""
        snap.last_active_at = time.time()
        snap.add_entry("system", event)

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
        snap.mode = "idle"
        snap.last_error = error
        snap.last_event = error
        snap.waiting_reason = ""
        snap.active_prompt = ""
        snap.last_active_at = time.time()
        snap.add_entry("error", error)

    def request_stop(self):
        snap = self.ensure_project(self.current_cwd)
        self.stop_requested = True
        self.last_event = "用户请求停止当前任务"
        snap.last_event = self.last_event
        snap.add_entry("system", self.last_event)

    def append_events(self, project_path: str, events: list[agent_runner.MirrorEvent]):
        snap = self.ensure_project(project_path)
        for event in events:
            snap.add_entry(event.kind, event.text, event.session_id)
            if event.session_id:
                snap.last_active_at = time.time()

    def summary(self) -> str:
        duration = "-"
        if self.started_at:
            duration = f"{int(time.time() - self.started_at)}s"
        task_label = f"#{self.current_task_id}" if self.current_task_id else "无"
        prompt = self.active_prompt[:80] if self.active_prompt else "-"
        wait = self.waiting_reason or "-"
        lines = [
            f"当前项目：{self.current_project_label or _project_label(self.current_cwd)}",
            f"状态：{self.mode} | 当前任务：{task_label} | 持续：{duration}",
            f"任务内容：{prompt}",
            f"等待原因：{wait}",
            "项目面板：",
        ]
        current_norm = _norm_path(self.current_cwd)
        for path in sorted(self.projects):
            lines.append(self.projects[path].summary_line(is_active=(path == current_norm)))
        snap = self.ensure_project(self.current_cwd)
        if snap.transcript:
            lines.append("最近窗口：")
            for entry in snap.transcript[-6:]:
                lines.append(f"- [{entry.kind}] {entry.text[:100]}")
        return "\n".join(lines)


def _norm_path(path: str) -> str:
    return os.path.normpath(path).replace("\\", "/")


def _project_label(path: str) -> str:
    base = os.path.basename(_norm_path(path))
    return base or _norm_path(path)


_state = RuntimeState()
for _path in config.DEFAULT_PROJECTS:
    _state.ensure_project(_path)
_state.set_current_project(config.DEFAULT_PROJECT_DIR)
_task_lock = asyncio.Lock()
_current_task: Optional[asyncio.Task] = None


def _authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == config.ALLOWED_CHAT_ID)


async def _send_unauthorized(update: Update):
    if update.message:
        await update.message.reply_text("⛔ 未授权。")
    elif update.callback_query:
        await update.callback_query.answer("⛔ 未授权", show_alert=True)


async def _run_agent(prompt: str, is_followup: bool, project_path: str):
    global _current_task
    snap = _state.ensure_project(project_path)
    try:
        outcome = await agent_runner.run_turn(prompt, project_path, is_followup=is_followup)
        _state.append_events(project_path, outcome.events)
        if outcome.state == "waiting":
            pending = PendingInteraction(
                project_path=project_path,
                project_label=snap.label,
                session_id=outcome.session_id,
                prompt_text=outcome.waiting_text or "Claude 正在等待你的回复",
                options=outcome.choice_options,
                source="buttons" if outcome.choice_options else "text",
            )
            _state.mark_waiting(pending)
            if pending.options:
                choice = await channel.ask_choice(
                    f"{pending.project_label} 有选项要你选：\n\n{pending.prompt_text}",
                    pending.options,
                    timeout=config.APPROVAL_TIMEOUT,
                )
                if choice is None:
                    _state.mark_waiting(pending)
                    await channel.send_text(f"{pending.project_label} 选择超时，你也可以直接发文字继续。")
                else:
                    snap.add_entry("user/choice", choice, pending.session_id)
                    _state.mark_running(f"收到你的选择：{choice}")
                    await channel.send_text(f"你选择了：{choice}\n继续处理 {pending.project_label}…")
                    _current_task = asyncio.create_task(_run_agent(choice, True, project_path))
                    return
            else:
                await channel.send_text(f"{pending.project_label} 正在等你回复。")
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
        "Claude 窗口镜像机器人已上线。\n\n"
        f"当前项目：{_state.current_project_label}\n"
        "监控项目：\n"
        f"{projects}\n\n"
        "直接发任务即可；/status 看项目面板和最近窗口；/project 切换项目；/stop 停止当前任务。"
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
    await update.message.reply_text(f"已切到：{_state.current_project_label}\n路径：{_state.current_cwd}")


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
        "直接发文字 = 给当前项目下任务\n"
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
    _state.mark_running(f"收到你的选择：{choice}")
    await query.edit_message_text(query.message.text + f"\n\n你选择了：{choice}")

    global _current_task
    _current_task = asyncio.create_task(_run_agent(choice, True, pending.project_path))


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
            _state.set_current_project(pending.project_path)
            _state.ensure_project(pending.project_path).add_entry("user", prompt, pending.session_id)
            _state.mark_running(f"收到你的回复，继续处理 {pending.project_label}")
            await update.message.reply_text(f"继续处理：{pending.project_label}")
            target_path = pending.project_path
        else:
            _state.start_task(prompt)
            await update.message.reply_text(
                f"已开始任务 #{_state.current_task_id}\n"
                f"项目：{_state.current_project_label}\n"
                f"内容：{prompt[:120]}"
            )
            target_path = _state.current_cwd

        _current_task = asyncio.create_task(_run_agent(prompt, is_followup=is_followup, project_path=target_path))


# ---------------- 启动 ----------------

def main():
    os.makedirs("logs", exist_ok=True)
    missing = config.validate()
    if missing:
        raise SystemExit(config.format_missing_config(missing))
    for path in config.DEFAULT_PROJECTS:
        os.makedirs(path, exist_ok=True)

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    channel.init(app.bot, config.ALLOWED_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("机器人启动，工作目录=%s", _state.current_cwd)
    print("机器人已启动，去 Telegram 给它发消息吧。Ctrl+C 退出。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
