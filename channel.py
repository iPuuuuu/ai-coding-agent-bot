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

_pending: dict[str, asyncio.Future] = {}
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
    if len(text) > 1200:
        text = text[:1200].rstrip() + "\n..."
    for i in range(0, len(text), 1200):
        chunk = text[i:i + 1200]
        try:
            await _bot.send_message(chat_id=_chat_id, text=chunk)
        except Exception:
            pass


async def send_status(text: str, min_interval: float = 2.5):
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


# ---------------- 🟡 黄灯审批：发按钮问你，挂起等你点 ----------------

async def ask_approval(text: str, timeout: int, timeout_action: str) -> bool:
    """
    给你发一条带 ✅/❌ 按钮的消息，然后挂起当前协程等你点。
    - 你点了 → 返回对应布尔值
    - 超时没点 → 按 timeout_action（"allow"/"deny"）返回
    这是"不卡死"的关键：永远有超时兜底。
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    approval_id = uuid.uuid4().hex[:8]
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[approval_id] = fut

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 通过", callback_data=f"ok:{approval_id}"),
        InlineKeyboardButton("❌ 拒绝", callback_data=f"no:{approval_id}"),
    ]])
    await _bot.send_message(chat_id=_chat_id, text=text, reply_markup=keyboard)

    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(approval_id, None)
        default = (timeout_action == "allow")
        await send_text(f"⏰ 审批超时（{timeout}s），按默认动作：{'通过' if default else '拒绝'}")
        return default


def resolve_approval(callback_data: str) -> bool | None:
    """
    Telegram 按钮回调触发时调用。返回：
    - True/False：成功唤醒对应的挂起审批
    - None：这个审批 id 不存在（可能已超时）
    """
    try:
        action, approval_id = callback_data.split(":", 1)
    except ValueError:
        return None
    fut = _pending.pop(approval_id, None)
    if fut is None or fut.done():
        return None
    result = (action == "ok")
    fut.set_result(result)
    return result
