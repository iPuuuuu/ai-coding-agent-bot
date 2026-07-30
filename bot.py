"""
bot.py —— 主入口：Telegram 桥接层。

职责：
- 收你的文字消息 → 交给 Agent 跑（run_turn）
- 处理 ✅/❌ 审批按钮 → 唤醒挂起的审批
- 命令：/start /project /status /help
- 白名单：只有你的 chat_id 能指挥

运行：  py -3.11 bot.py
"""
import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import channel
import config
import agent_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join("logs", "bot.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")

# 运行时状态
_current_cwd = config.DEFAULT_PROJECT_DIR
_busy = False          # 同一时刻只跑一个任务，避免混乱
_busy_lock = asyncio.Lock()


def _authorized(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == config.ALLOWED_CHAT_ID


# ---------------- 命令 ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "🤖 编程助手已上线。\n"
        f"当前项目目录：{_current_cwd}\n\n"
        "直接发指令即可，例如：\n"
        "  写个 Python 快排并测试\n"
        "  把 workspace 里的网页跑起来截图给我看\n\n"
        "命令：/project <路径> 切换目录  /status 状态  /help 帮助"
    )


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    global _current_cwd
    if not ctx.args:
        await update.message.reply_text(f"当前项目目录：{_current_cwd}\n用法：/project D:/code/myapp")
        return
    path = " ".join(ctx.args)
    if not os.path.isdir(path):
        await update.message.reply_text(f"❌ 目录不存在：{path}")
        return
    _current_cwd = path
    await update.message.reply_text(f"✅ 已切换项目目录：{path}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        f"目录：{_current_cwd}\n状态：{'⏳ 正在跑任务' if _busy else '💤 空闲'}"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "直接发文字 = 给 Agent 的指令。\n"
        "/project <路径>  切换工作目录\n"
        "/status  查看状态\n"
        "遇到黄灯操作（装依赖/删文件/push 等）会弹 ✅/❌ 按钮问你。"
    )


# ---------------- 审批按钮回调 ----------------

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    query = update.callback_query
    await query.answer()
    result = channel.resolve_approval(query.data)
    if result is None:
        await query.edit_message_text(query.message.text + "\n\n（已失效/超时）")
    else:
        await query.edit_message_text(query.message.text + f"\n\n{'✅ 你点了通过' if result else '❌ 你点了拒绝'}")


# ---------------- 普通消息 = 给 Agent 的指令 ----------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        await update.message.reply_text("⛔ 未授权。")
        return
    global _busy
    if _busy:
        await update.message.reply_text("⏳ 正在跑上一个任务，等它结束再发。")
        return

    prompt = update.message.text
    async with _busy_lock:
        _busy = True
        await update.message.reply_text("🚀 开始处理…")
        try:
            await agent_runner.run_turn(prompt, _current_cwd)
        finally:
            _busy = False


# ---------------- 启动 ----------------

def main():
    os.makedirs("logs", exist_ok=True)
    missing = config.validate()
    if missing:
        raise SystemExit(f"❌ .env 缺少必填项：{', '.join(missing)}（参考 .env.example）")
    os.makedirs(config.DEFAULT_PROJECT_DIR, exist_ok=True)

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    # 把 Telegram 发送能力注入 channel，供 permissions/tools 使用
    channel.init(app.bot, config.ALLOWED_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("机器人启动，工作目录=%s", _current_cwd)
    print("🤖 机器人已启动，去 Telegram 给它发消息吧。Ctrl+C 退出。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
