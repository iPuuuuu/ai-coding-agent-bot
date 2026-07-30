"""
tools/screenshot.py —— 自定义 MCP 工具：让 Agent 能截图并直接发给你。

两个工具：
- take_desktop_screenshot：截整个桌面（看 GUI 程序、终端跑的结果）
- screenshot_web(url)     ：无头浏览器打开网页并截图（看 Web 项目效果）

截完图后，工具直接通过 channel.send_photo 把图片推给你，
所以你在 Telegram 里会直接收到图片，而不用 Agent 再费劲描述。

⚠️ @tool / create_sdk_mcp_server 的具体签名以你装的 claude-agent-sdk 为准。
"""
import os
import time

from claude_agent_sdk import tool, create_sdk_mcp_server

import channel

_SHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "shots")
os.makedirs(_SHOT_DIR, exist_ok=True)


def _new_path(prefix: str) -> str:
    # 用时间戳命名避免覆盖
    return os.path.join(_SHOT_DIR, f"{prefix}_{int(time.time())}.png")


@tool("take_desktop_screenshot", "截取当前整个桌面并发给用户，用于展示 GUI 程序/终端运行结果", {})
async def take_desktop_screenshot(args):
    import mss

    path = _new_path("desktop")
    with mss.mss() as sct:
        sct.shot(mon=-1, output=path)  # mon=-1 = 所有显示器拼成一张
    await channel.send_photo(path, caption="🖥️ 桌面截图")
    return {"content": [{"type": "text", "text": f"已截取桌面并发送给用户：{path}"}]}


@tool(
    "screenshot_web",
    "用无头浏览器打开一个网址并截图发给用户，用于展示 Web 项目页面效果",
    {"url": str},
)
async def screenshot_web(args):
    from playwright.async_api import async_playwright

    url = args.get("url", "")
    if not url:
        return {"content": [{"type": "text", "text": "缺少 url 参数"}]}
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
    return {"content": [{"type": "text", "text": f"已截取 {url} 并发送给用户：{path}"}]}


# 打包成一个进程内 MCP server，交给 ClaudeAgentOptions.mcp_servers
screenshot_server = create_sdk_mcp_server(
    name="screenshot",
    version="1.0.0",
    tools=[take_desktop_screenshot, screenshot_web],
)

# 供 allowed_tools 使用的完整工具名（格式：mcp__<server>__<tool>）
SCREENSHOT_TOOL_NAMES = [
    "mcp__screenshot__take_desktop_screenshot",
    "mcp__screenshot__screenshot_web",
]
