"""Feishu/Lark long-connection adapter for the shared bot conversation core.

This module deliberately contains all lark-oapi specifics.  ``bot.py`` keeps
ownership of sessions, task scheduling and authorization rules; incoming
Feishu events are represented by the small Telegram-compatible objects there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import config

log = logging.getLogger("feishu")


class FeishuTransport:
    def __init__(self, client: Any):
        self.client = client
        self.chat_id = ""

    def set_chat_id(self, chat_id: str) -> None:
        if chat_id:
            self.chat_id = chat_id

    async def send_text(self, text: str, chat_id: str = "") -> None:
        target = chat_id or self.chat_id
        if not target or not text:
            return
        # Feishu text messages have a 150 KiB content limit.  A smaller chunk
        # also keeps long Claude output readable in the mobile client.
        for start in range(0, len(text), 12_000):
            content = json.dumps({"text": text[start:start + 12_000]})
            await asyncio.to_thread(self._create_message, target, "text", content)

    async def send_choice(self, text: str, options: list[str], choice_id: str) -> None:
        target = self.chat_id
        if not target:
            return
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "需要你的选择"}},
            "elements": [
                {"tag": "markdown", "content": text[:12_000]},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label[:80]},
                            "type": "primary" if index == 0 else "default",
                            "value": {"callback_data": f"pick:{choice_id}:{index}"},
                        }
                        for index, label in enumerate(options)
                    ],
                },
            ],
        }
        await asyncio.to_thread(self._create_message, target, "interactive", json.dumps(card))

    async def send_monitor_choices(self, text: str, choices: list[tuple[str, str]]) -> None:
        """Send session buttons in small cards for readable mobile layouts."""
        target = self.chat_id
        if not target:
            return
        for offset in range(0, len(choices), 5):
            group = choices[offset:offset + 5]
            elements = []
            if offset == 0 and text:
                elements.append({"tag": "markdown", "content": text[:8_000]})
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label[:80]},
                        "type": "primary" if index == 0 and offset == 0 else "default",
                        "value": {"callback_data": callback_data},
                    }
                    for index, (label, callback_data) in enumerate(group)
                ],
            })
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "选择要查看的会话"}},
                "elements": elements,
            }
            await asyncio.to_thread(self._create_message, target, "interactive", json.dumps(card))

    async def send_command_menu(self, choices: list[tuple[str, str]]) -> None:
        target = self.chat_id
        if not target:
            return
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "Claude / Codex 控制台"}},
            "elements": [
                {"tag": "markdown", "content": "点击按钮即可查看状态或操作，无需手动输入命令。"},
                *[
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": label},
                                "type": "primary" if offset == 0 and index == 0 else "default",
                                "value": {"callback_data": callback_data},
                            }
                            for index, (label, callback_data) in enumerate(choices[offset:offset + 3])
                        ],
                    }
                    for offset in range(0, len(choices), 3)
                ],
            ],
        }
        await asyncio.to_thread(self._create_message, target, "interactive", json.dumps(card))

    async def send_project_choices(self, choices: list[tuple[str, str]]) -> None:
        target = self.chat_id
        if not target:
            return
        for offset in range(0, len(choices), 5):
            group = choices[offset:offset + 5]
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "选择要切换的项目"}},
                "elements": [{
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label[:80]},
                            "type": "primary" if offset == 0 and index == 0 else "default",
                            "value": {"callback_data": callback_data},
                        }
                        for index, (label, callback_data) in enumerate(group)
                    ],
                }],
            }
            await asyncio.to_thread(self._create_message, target, "interactive", json.dumps(card))

    async def send_photo(self, path: str, caption: str = "") -> None:
        target = self.chat_id
        if not target:
            return
        try:
            image_key = await asyncio.to_thread(self._upload_image, path)
            await asyncio.to_thread(
                self._create_message, target, "image", json.dumps({"image_key": image_key})
            )
            if caption:
                await self.send_text(caption)
        except Exception as exc:
            await self.send_text(f"⚠️ 图片发送失败：{exc}")

    def _create_message(self, chat_id: str, msg_type: str, content: str) -> None:
        import lark_oapi as lark

        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"Feishu send failed: code={response.code}, msg={response.msg}")

    def _upload_image(self, path: str) -> str:
        import lark_oapi as lark

        request = (
            lark.im.v1.CreateImageRequest.builder()
            .request_body(
                lark.im.v1.CreateImageRequestBody.builder()
                .image_type("message")
                .image(Path(path))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.image.create(request)
        if not response.success():
            raise RuntimeError(f"Feishu image upload failed: code={response.code}, msg={response.msg}")
        return response.data.image_key


def run() -> None:
    """Start the Feishu WebSocket client and forward events into ``bot``."""
    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise SystemExit("缺少飞书依赖。请执行：py -3.11 -m pip install -r requirements.txt") from exc

    import bot
    import channel

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = lark.Client.builder().app_id(config.FEISHU_APP_ID).app_secret(config.FEISHU_APP_SECRET).build()
    transport = FeishuTransport(client)
    channel.init_feishu(transport)

    def schedule(coro):
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        def report(result):
            try:
                result.result()
            except Exception:
                log.exception("Feishu event handler failed")
        future.add_done_callback(report)

    def on_message(data: Any) -> None:
        event = data.event
        message = event.message
        open_id = getattr(getattr(event.sender, "sender_id", None), "open_id", "")
        chat_id = getattr(message, "chat_id", "")
        log.info(
            "Feishu message event received: type=%s open_id=%s chat_id=%s",
            getattr(message, "message_type", ""),
            open_id or "-",
            chat_id or "-",
        )
        if message.message_type != "text":
            log.info("Ignored non-text Feishu message: type=%s", message.message_type)
            return
        try:
            text = json.loads(message.content).get("text", "").strip()
        except (TypeError, json.JSONDecodeError):
            log.warning("Failed to parse Feishu text content: %r", getattr(message, "content", None))
            return
        log.info("Feishu text payload parsed: %r", text[:200])
        if open_id != config.ALLOWED_FEISHU_OPEN_ID:
            log.warning("Ignored Feishu message from open_id=%s", open_id)
            return
        schedule(bot.dispatch_feishu_text(open_id, chat_id, text, transport))

    def on_card_action(data: Any) -> dict:
        event = data.event
        action = event.action
        value = getattr(action, "value", {}) or {}
        callback_data = value.get("callback_data", "") if isinstance(value, dict) else ""
        open_id = event.operator.open_id
        log.info(
            "Feishu card action received: open_id=%s callback_data=%s",
            open_id,
            callback_data[:40],
        )
        if open_id != config.ALLOWED_FEISHU_OPEN_ID:
            log.warning("Ignored Feishu card action from open_id=%s", open_id)
            return
        schedule(bot.dispatch_feishu_button(open_id, callback_data, transport))
        return {"toast": {"type": "info", "content": "已收到，正在继续处理"}}

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(lambda data: log.info("Feishu chat-enter event received"))
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws_client = lark.ws.Client(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, event_handler=handler)

    async def serve() -> None:
        asyncio.create_task(bot.background_poll_loop())
        asyncio.create_task(bot._autosave_loop())
        # Run the WebSocket client in a daemon thread so a Ctrl+C / SIGTERM
        # can actually terminate the process instead of hanging on shutdown.
        worker = threading.Thread(target=ws_client.start, daemon=True, name="feishu-ws")
        worker.start()
        while worker.is_alive():
            await asyncio.sleep(1)
        log.error("Feishu 长连接已退出，进程即将结束")

    log.info("Feishu bridge started; waiting for long-connection events")
    loop.run_until_complete(serve())
