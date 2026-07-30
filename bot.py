"""
bot.py —— 主入口：Telegram 桥接层。

职责：
- 收你的文字消息 → 交给 Agent 跑（新任务 / 继续回答）
- 处理 ✅/❌ 审批按钮 → 唤醒挂起的审批
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
class RuntimeState:
    current_cwd: str = config.DEFAULT_PROJECT_DIR
    mode: str = "idle"  # idle | running | waiting_user_reply
    active_prompt: str = ""
    started_at: Optional[float] = None
    last_event: str = "空闲"
    last_error: str = ""
    waiting_reason: str = ""
    stop_requested: bool = False
    task_counter: int = 0
    current_task_id: Optional[int] = None
    history: list[str] = field(default_factory=list)

    def start_task(self, prompt: str):
        self.task_counter += 1
        self.current_task_id = self.task_counter
        self.active_prompt = prompt.strip()
        self.started_at = time.time()
        self.mode = "running"
        self.stop_requested = False
        self.waiting_reason = ""
        self.last_error = ""
        self.last_event = "Agent 开始处理任务"
        self._push_history(f"任务 #{self.current_task_id} 开始：{self.active_prompt[:120]}")

    def mark_waiting(self, reason: str):
        self.mode = "waiting_user_reply"
        self.waiting_reason = reason.strip() or "Agent 等待你的回复"
        self.last_event = self.waiting_reason
        self._push_history(self.waiting_reason)

    def mark_running(self, event: str):
        self.mode = "running"
        self.waiting_reason = ""
        self.last_event = event
        self._push_history(event)

    def mark_done(self, event: str = "任务完成"):
        self.mode = "idle"
        self.last_event = event
        self.waiting_reason = ""
        self.active_prompt = ""
        self.started_at = None
        self.stop_requested = False
        self.current_task_id = None
        self._push_history(event)

    def mark_error(self, error: str):
        self.mode = "idle"
        self.last_error = error
        self.last_event = error
        self.waiting_reason = ""
        self.active_prompt = ""
        self.started_at = None
        self.stop_requested = False
        self.current_task_id = None
        self._push_history(error)

    def request_stop(self):
        self.stop_requested = True
        self.last_event = "用户请求停止当前任务"
        self._push_history(self.last_event)

    def summary(self) -> str:
        duration = "-"
        if self.started_at:
            duration = f"{int(time.time() - self.started_at)}s"
        task_label = f"#{self.current_task_id}" if self.current_task_id else "无"
        prompt = self.active_prompt[:120] if self.active_prompt else "-"
        recent = "\n".join(f"- {x}" for x in self.history[-5:]) or "- 无"
        wait = self.waiting_reason or "-"
        err = self.last_error or "-"
        return (
            f"📂 当前项目：{self.current_cwd}\n"
            f"🧭 状态：{self.mode}\n"
            f"🆔 当前任务：{task_label}\n"
            f"⏱️ 持续时间：{duration}\n"
            f"📝 当前任务内容：{prompt}\n"
            f"🔔 最近事件：{self.last_event}\n"
            f"💬 等待原因：{wait}\n"
            f"⚠️ 最近错误：{err}\n"
            f"📜 最近记录：\n{recent}"
        )

    def _push_history(self, text: str):
        text = text.strip()
        if text:
            self.history.append(text)
            if len(self.history) > 20:
                self.history = self.history[-20:]


_state = RuntimeState()
_task_lock = asyncio.Lock()
_current_task: Optional[asyncio.Task] = None


def _authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == config.ALLOWED_CHAT_ID)


async def _send_unauthorized(update: Update):
    if update.message:
        await update.message.reply_text("⛔ 未授权。")
    elif update.callback_query:
        await update.callback_query.answer("⛔ 未授权", show_alert=True)


async def _run_agent(prompt: str, is_followup: bool):
    try:
        outcome = await agent_runner.run_turn(prompt, _state.current_cwd, is_followup=is_followup)
        if outcome == "waiting":
            _state.mark_waiting("Agent 需要你补充信息，直接回复下一条消息即可继续。")
            await channel.send_text("💬 Agent 需要你补充信息，直接回复我继续即可。")
        elif _state.stop_requested:
            _state.mark_done("任务已停止")
            await channel.send_text("🛑 当前任务已停止。")
        else:
            _state.mark_done("任务完成")
    except asyncio.CancelledError:
        _state.mark_done("任务已取消")
        await channel.send_text("🛑 当前任务已取消。")
        raise
    except Exception as e:
        log.exception("run_agent failed")
        _state.mark_error(f"Agent 运行出错：{type(e).__name__}: {e}")
        await channel.send_text(f"⚠️ Agent 运行出错：{type(e).__name__}: {e}")
    finally:
        global _current_task
        _current_task = None


# ---------------- 命令 ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(
        "🤖 编程监督机器人已上线。\n\n"
        "它会在这台电脑上监督/驱动 AI 写代码，把进度、审批、结果、截图发回 Telegram。\n\n"
        f"当前项目目录：{_state.current_cwd}\n"
        f"当前状态：{_state.mode}\n\n"
        "你可以这样用：\n"
        "1. /project C:/你的项目路径  切到某个项目\n"
        "2. 直接发任务，例如：\n"
        "   帮我看下这个项目怎么启动\n"
        "   修复测试失败并告诉我改了什么\n"
        "   把项目跑起来截图给我看\n"
        "3. 遇到装依赖 / 删除 / push 等操作，我会弹 ✅/❌ 给你审批\n"
        "4. 如果我问你问题，直接回复那条消息即可继续\n"
        "5. /status 看状态，/stop 停止当前任务\n"
    )


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    if not ctx.args:
        await update.message.reply_text(
            f"当前项目目录：{_state.current_cwd}\n用法：/project C:/code/myapp"
        )
        return
    if _state.mode == "running":
        await update.message.reply_text("⏳ 当前有任务正在执行，先 /stop 或等它结束后再切目录。")
        return
    path = " ".join(ctx.args)
    if not os.path.isdir(path):
        await update.message.reply_text(f"❌ 目录不存在：{path}")
        return
    _state.current_cwd = path
    _state.last_event = f"切换项目目录：{path}"
    _state._push_history(_state.last_event)
    await update.message.reply_text(
        f"✅ 已切换项目目录：{path}\n"
        "现在你可以直接发任务，例如：分析这个项目怎么启动。"
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
        await update.message.reply_text("💤 当前没有正在执行的任务。")
        return
    _state.request_stop()
    agent_runner.request_stop()
    if not _current_task.done():
        _current_task.cancel()
    await update.message.reply_text("🛑 已请求停止当前任务，稍等我清理现场。")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    await update.message.reply_text(
        "📘 使用说明\n\n"
        "直接发文字 = 给 AI 的任务，例如：\n"
        "- 看下这个仓库结构\n"
        "- 修复测试失败\n"
        "- 把项目跑起来截图给我看\n\n"
        "控制命令：\n"
        "/project <路径>  切换工作目录\n"
        "/status          查看当前任务/状态\n"
        "/stop            停止当前任务\n"
        "/help            查看帮助\n\n"
        "交互规则：\n"
        "- 正在运行时发新任务，我会提醒你先等待或 /stop\n"
        "- 我如果需要你拍板，会发 ✅/❌ 审批按钮\n"
        "- 我如果问你问题，直接回复下一条文字即可继续当前任务"
    )


# ---------------- 审批按钮回调 ----------------

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await _send_unauthorized(update)
        return
    query = update.callback_query
    await query.answer()
    result = channel.resolve_approval(query.data)
    if result is None:
        await query.edit_message_text(query.message.text + "\n\n（已失效/超时）")
    else:
        action_text = "✅ 你点了通过，任务继续执行" if result else "❌ 你点了拒绝，任务将跳过或停止该步骤"
        _state.last_event = action_text
        _state._push_history(action_text)
        await query.edit_message_text(query.message.text + f"\n\n{action_text}")


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
            await update.message.reply_text("⏳ 当前任务还在执行中。先等它结束，或发送 /stop 中止后再发新任务。")
            return

        is_followup = _state.mode == "waiting_user_reply"
        if is_followup:
            _state.mark_running("收到你的补充回复，继续让 Agent 处理")
            await update.message.reply_text("🔁 已收到你的回复，继续处理当前任务…")
        else:
            _state.start_task(prompt)
            await update.message.reply_text(
                f"🚀 已开始任务 #{_state.current_task_id}\n"
                f"📂 目录：{_state.current_cwd}\n"
                f"📝 内容：{prompt[:200]}"
            )

        _current_task = asyncio.create_task(_run_agent(prompt, is_followup=is_followup))


# ---------------- 启动 ----------------

def main():
    os.makedirs("logs", exist_ok=True)
    missing = config.validate()
    if missing:
        raise SystemExit(config.format_missing_config(missing))
    os.makedirs(config.DEFAULT_PROJECT_DIR, exist_ok=True)

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
