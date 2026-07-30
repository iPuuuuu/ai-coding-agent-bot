"""
agent_runner.py —— 封装一次 Agent 对话回合。

每收到你的一条指令就调用 run_turn()：
- 打开一个 ClaudeSDKClient（用 continue_conversation 保持上下文连续，实现多轮对话）
- 挂上 can_use_tool 三档审批回调 + 截图 MCP 工具
- 流式地把 Agent 的思考/工具调用/结果转发到 Telegram

⚠️ 消息类型名（AssistantMessage/TextBlock/ToolUseBlock/ResultMessage）以你装的
   claude-agent-sdk 为准。若 import 报错，用 dir(claude_agent_sdk) 核对后微调。
"""
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

import channel
import config
from permissions import can_use_tool
from tools.screenshot import screenshot_server, SCREENSHOT_TOOL_NAMES

SYSTEM_PROMPT = """你是一个常驻在用户家里 Windows 电脑上的编程助手，通过 Telegram 和用户沟通。
原则：
1. 尽量自主完成任务：写代码、跑测试、修 bug，不要每一步都停下来等确认。
2. 只有遇到真正需要用户拍板的产品决策（例如"要方案 A 还是 B"）时，才明确地提出问题并停下来等回复。
3. 完成一个网页或可视化项目后，主动用 screenshot 工具截图给用户看效果。
4. 回复精炼，适配手机小屏阅读。
"""

# 记录哪些工作目录已经开过对话，用于决定是否 continue_conversation
_seen_cwds: set[str] = set()


def _build_options(cwd: str, first: bool) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        cwd=cwd,
        permission_mode="acceptEdits",      # 高频的改代码默认放行，只有黄灯走 Telegram
        can_use_tool=can_use_tool,           # 三档分级审批
        continue_conversation=not first,     # 非首轮 → 接着上次的上下文聊
        mcp_servers={"screenshot": screenshot_server},
        allowed_tools=SCREENSHOT_TOOL_NAMES,  # 允许自定义截图工具（内置工具默认可用）
    )


async def run_turn(prompt: str, cwd: str):
    """跑一回合，把过程流式发到 Telegram。"""
    first = cwd not in _seen_cwds
    _seen_cwds.add(cwd)
    options = _build_options(cwd, first)

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                await _forward(msg)
    except Exception as e:
        await channel.send_text(f"⚠️ Agent 运行出错：{type(e).__name__}: {e}")


async def _forward(msg):
    """把一条 SDK 消息转成人话发到 Telegram。"""
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text.strip():
                await channel.send_text(block.text)
            elif isinstance(block, ToolUseBlock):
                # 让你能看到它正在干什么，透明可控
                await channel.send_text(f"🔧 {block.name}")
    elif isinstance(msg, ResultMessage):
        cost = getattr(msg, "total_cost_usd", None)
        tail = f"（本轮花费约 ${cost:.4f}）" if cost else ""
        await channel.send_text(f"✅ 完成 {tail}")
