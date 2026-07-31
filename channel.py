"""
channel.py —— 机器人与 Telegram 之间的"信道"。

这是把 Telegram（嘴和耳朵）和 Claude Agent SDK（大脑和手）缝起来的中间层。
permissions.py 和 tools/ 都从这里拿"发消息 / 发图 / 问用户"的能力，
而不直接依赖 bot.py，避免循环 import。

bot.py 启动时调用 init() 把 Bot 对象和你的 chat_id 注入进来。
"""
import asyncio
import time
import uuid

_bot = None            # telegram.Bot 实例
_chat_id = None        # 你的 chat_id（只给你发）

_pending: dict[str, dict] = {}
_last_status_text = ""
_last_status_at = 0.0


def init(bot, chat_id: int):
    global _bot, _chat_id
    _bot = bot
    _chat_id = chat_id


# ---------------- 基础发送 ----------------

async def send_text(text: str):
    """发文字。Telegram 单条上限 4096 字符，自动分段。"""
    if not _bot or not text:
        return
    text = text.strip()
    for i in range(0, len(text), 3500):
        chunk = text[i:i + 3500]
        try:
            await _bot.send_message(chat_id=_chat_id, text=chunk)
        except Exception:
            pass


async def send_status(text: str, min_interval: float = 1.5):
    """发阶段性状态，做一点节流，避免工具消息刷屏。"""
    global _last_status_text, _last_status_at
    now = time.time()
    if text == _last_status_text and (now - _last_status_at) < min_interval:
        return
    _last_status_text = text
    _last_status_at = now
    await send_text(text)


async def send_photo(path: str, caption: str = ""):
    """把一张图片（截图）发给你。"""
    if not _bot:
        return
    try:
        with open(path, "rb") as f:
            await _bot.send_photo(chat_id=_chat_id, photo=f, caption=caption[:1000])
    except Exception as e:
        await send_text(f"⚠️ 截图发送失败：{e}")


# ---------------- 交互按钮 ----------------

async def ask_choice(text: str, options: list[str], timeout: int) -> str | None:
    """发多选按钮，等待用户点选；超时返回 None。"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not _bot or not options:
        return None

    choice_id = uuid.uuid4().hex[:8]
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[choice_id] = {"future": fut, "options": options}

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label[:32], callback_data=f"pick:{choice_id}:{idx}")] for idx, label in enumerate(options)]
    )
    await _bot.send_message(chat_id=_chat_id, text=text, reply_markup=keyboard)

    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(choice_id, None)
        await send_text(f"⏰ 选择超时（{timeout}s）")
        return None


async def ask_approval(text: str, timeout: int, timeout_action: str) -> bool:
    """兼容旧接口：发二元按钮并返回布尔值。"""
    choice = await ask_choice(text, ["✅ 通过", "❌ 拒绝"], timeout)
    if choice is None:
        return timeout_action == "allow"
    return choice.startswith("✅")


def resolve_approval(callback_data: str) -> str | None:
    """Telegram 按钮回调触发时调用，返回被选中的选项文本。"""
    try:
        kind, pending_id, raw_index = callback_data.split(":", 2)
    except ValueError:
        return None
    if kind != "pick":
        return None
    item = _pending.pop(pending_id, None)
    if not item:
        return None
    fut: asyncio.Future = item["future"]
    options: list[str] = item["options"]
    try:
        index = int(raw_index)
    except ValueError:
        index = -1
    if fut.done() or not (0 <= index < len(options)):
        return None
    choice = options[index]
    fut.set_result(choice)
    return choice
