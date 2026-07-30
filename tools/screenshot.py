"""
tools/screenshot.py —— 自定义 MCP 工具：让 Agent 能截图并直接发给你。

两个工具：
- take_desktop_screenshot：截整个桌面（看 GUI 程序、终端跑的结果）
- screenshot_web(url)     ：无头浏览器打开网页并截图（看 Web 项目效果）

截完图后，工具直接通过 channel.send_photo 把图片推给你，
所以你在 Telegram 里会直接收到图片，而不用 Agent 再费劲描述。

若本机尚未安装 claude-agent-sdk，这个文件也能被 import，
agent_runner 会在真正启动 Agent 时给出依赖提示。
"""
from __future__ import annotations

import os
import time

import channel

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    SDK_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - 依赖未安装时走这里
    SDK_IMPORT_ERROR = exc

    def tool(name, description, schema):
        def decorator(fn):
            fn._tool_name = name
            fn._tool_description = description
            fn._tool_schema = schema
            return fn
        return decorator

    def create_sdk_mcp_server(name, version, tools):
        return {"name": name, "version": version, "tools": tools}

_SHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "shots")
os.makedirs(_SHOT_DIR, exist_ok=True)


def _new_path(prefix: str) -> str:
    return os.path.join(_SHOT_DIR, f"{prefix}_{int(time.time())}.png")


@tool("take_desktop_screenshot", "截取当前整个桌面并发给用户，用于展示 GUI 程序/终端运行结果", {})
async def take_desktop_screenshot(args):
    try:
        import mss
    except Exception as e:
        return {"content": [{"type": "text", "text": f"桌面截图不可用：{e}"}]}

    try:
        path = _new_path("desktop")
        with mss.mss() as sct:
            sct.shot(mon=-1, output=path)
        await channel.send_photo(path, caption="🖥️ 桌面截图")
        return {"content": [{"type": "text", "text": f"已截取桌面并发送给用户：{path}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"桌面截图失败：{e}"}]}


@tool(
    "screenshot_web",
    "用无头浏览器打开一个网址并截图发给用户，用于展示 Web 项目页面效果",
    {"url": str},
)
async def screenshot_web(args):
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return {"content": [{"type": "text", "text": f"网页截图不可用：{e}。如未安装浏览器，先运行：py -3.11 -m playwright install chromium"}]}

    url = args.get("url", "")
    if not url:
        return {"content": [{"type": "text", "text": "缺少 url 参数"}]}
    if not url.startswith("http"):
        url = "http://" + url

    path = _new_path("web")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.screenshot(path=path, full_page=True)
            await browser.close()
        await channel.send_photo(path, caption=f"🌐 网页截图：{url}")
        return {"content": [{"type": "text", "text": f"已截取 {url} 并发送给用户：{path}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"网页截图失败：{e}"}]}


screenshot_server = create_sdk_mcp_server(
    name="screenshot",
    version="1.0.0",
    tools=[take_desktop_screenshot, screenshot_web],
)

SCREENSHOT_TOOL_NAMES = [
    "mcp__screenshot__take_desktop_screenshot",
    "mcp__screenshot__screenshot_web",
]
