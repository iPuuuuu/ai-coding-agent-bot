"""
tools/screenshot.py —— 本地截图辅助能力。

两个能力：
- take_desktop_screenshot：截整个桌面（看 GUI 程序、终端跑的结果）
- screenshot_web(url)     ：无头浏览器打开网页并截图（看 Web 项目效果）

这条路线不再依赖 claude-agent-sdk 的 MCP/tool 注册；
截图由 Python 侧直接调用，再通过 Telegram 发给你。
"""
from __future__ import annotations

import os
import time

import channel

_SHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "shots")
os.makedirs(_SHOT_DIR, exist_ok=True)


def _new_path(prefix: str) -> str:
    return os.path.join(_SHOT_DIR, f"{prefix}_{int(time.time())}.png")


async def take_desktop_screenshot() -> str:
    try:
        import mss
    except Exception as e:
        raise RuntimeError(f"桌面截图不可用：{e}") from e

    path = _new_path("desktop")
    with mss.mss() as sct:
        sct.shot(mon=-1, output=path)
    await channel.send_photo(path, caption="🖥️ 桌面截图")
    return path


async def screenshot_web(url: str) -> str:
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        raise RuntimeError(
            f"网页截图不可用：{e}。如未安装浏览器，先运行：py -3.11 -m playwright install chromium"
        ) from e

    if not url:
        raise RuntimeError("缺少 url 参数")
    if not url.startswith("http"):
        url = "http://" + url

    path = _new_path("web")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.screenshot(path=path, full_page=True)
        await browser.close()

    await channel.send_photo(path, caption=f"🌐 网页截图：{url}")
    return path


async def maybe_handle_visual_request(prompt: str):
    text = prompt.lower()
    if "截图" in prompt or "screenshot" in text:
        if any(word in prompt for word in ["网页", "web", "http://", "https://"]):
            for token in prompt.split():
                if token.startswith("http://") or token.startswith("https://"):
                    await screenshot_web(token)
                    return
        # 没给出网页地址时，先尝试桌面截图，失败再忽略
        try:
            await take_desktop_screenshot()
        except Exception:
            pass
