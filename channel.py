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
_feishu = None         # FeishuTransport 实例（避免 channel 依赖飞书 SDK）

_pending: dict[str, dict] = {}
_last_status_text = ""
_last_status_at = 0.0


def init(bot, chat_id: int):
    global _bot, _chat_id, _feishu
    _bot = bot
    _chat_id = chat_id
    _feishu = None


def init_feishu(transport):
    """Inject the Feishu implementation of the outbound channel."""
    global _bot, _chat_id, _feishu
    _bot = None
    _chat_id = None
    _feishu = transport


# ---------------- 基础发送 ----------------

async def send_text(text: str):
    """发文字。Telegram 单条上限 4096 字符，自动分段。"""
    if not text:
        return
    text = text.strip()
    if _feishu is not None:
        await _feishu.send_text(text)
        return
    if not _bot:
        return
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
    if _feishu is not None:
        await _feishu.send_photo(path, caption)
        return
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
    if (not _bot and _feishu is None) or not options:
        return None

    choice_id = uuid.uuid4().hex[:8]
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[choice_id] = {"future": fut, "options": options}

    if _feishu is not None:
        await _feishu.send_choice(text, options, choice_id)
    else:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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


async def send_monitor_choices(text: str, choices: list[tuple[str, str]]):
    """Send non-blocking session-detail buttons.

    Unlike ``ask_choice``, these buttons do not create a pending approval or
    wait for a result: their callback only opens a monitor detail view.
    """
    if not choices or (not _bot and _feishu is None):
        return
    if _feishu is not None:
        await _feishu.send_monitor_choices(text, choices)
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label[:48], callback_data=data)] for label, data in choices]
    )
    await _bot.send_message(chat_id=_chat_id, text=text[:3500], reply_markup=keyboard)


async def send_command_menu():
    """Send a non-blocking dashboard of common bot actions."""
    choices = [
        ("📊 当前状态", "dashboard:status"),
        ("🖥️ 会话监控", "dashboard:monitor"),
        ("💬 托管会话", "dashboard:sessions"),
        ("📁 切换项目", "dashboard:project"),
        ("➕ 新建会话", "dashboard:new"),
        ("⏹️ 停止任务", "dashboard:stop"),
    ]
    if not _bot and _feishu is None:
        return
    if _feishu is not None:
        await _feishu.send_command_menu(choices)
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in choices[index:index + 2]]
        for index in range(0, len(choices), 2)
    ])
    await _bot.send_message(chat_id=_chat_id, text="快捷操作：", reply_markup=keyboard)


async def send_project_choices(choices: list[tuple[str, str]]):
    if not choices or (not _bot and _feishu is None):
        return
    if _feishu is not None:
        await _feishu.send_project_choices(choices)
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label[:48], callback_data=data)] for label, data in choices]
    )
    await _bot.send_message(chat_id=_chat_id, text="请选择要切换的项目：", reply_markup=keyboard)
