"""
agent_runner.py —— 封装一次 Agent 对话回合。

每收到你的一条指令就调用 run_turn()：
- 打开一个 ClaudeSDKClient（用 continue_conversation 保持上下文连续，实现多轮对话）
- 挂上 can_use_tool 三档审批回调 + 截图 MCP 工具
- 流式地把 Agent 的文本/工具动作/结果转发到 Telegram

如果本机还没安装 claude-agent-sdk，会优雅报错并告诉你下一步怎么装。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import channel
from permissions import can_use_tool
from tools.screenshot import SCREENSHOT_TOOL_NAMES, screenshot_server

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )
    SDK_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - 依赖未安装时走这里
    AssistantMessage = ClaudeAgentOptions = ClaudeSDKClient = None
    ResultMessage = TextBlock = ToolUseBlock = None
    SDK_IMPORT_ERROR = exc

SYSTEM_PROMPT = """你是一个常驻在用户家里 Windows 电脑上的编程监督助手，通过 Telegram 和用户沟通。
原则：
1. 尽量自主完成任务：读代码、写代码、跑测试、修 bug，并把阶段性进展同步给用户。
2. 你是在监督/驱动 AI 写代码；用户只有手机 Telegram，所以要主动汇报当前进度、下一步、阻塞点。
3. 只有遇到真正需要用户拍板的产品决策，或权限系统要求审批的风险操作时，才停下来等用户。
4. 完成网页或可视化项目后，主动用 screenshot 工具截图给用户看效果。
5. 回复精炼，适合手机阅读；关键结论优先，不要长篇空话。
6. 如果你需要用户补充信息，请明确列出你需要什么，并以问句结尾。
"""

_seen_cwds: set[str] = set()
_stop_requested = False


@dataclass
class TurnOutcome:
    state: str = "completed"  # completed | waiting


class AgentRunnerError(RuntimeError):
    pass


def request_stop():
    global _stop_requested
    _stop_requested = True


def _reset_stop_flag():
    global _stop_requested
    _stop_requested = False


def _build_options(cwd: str, first: bool):
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        cwd=cwd,
        permission_mode="acceptEdits",
        can_use_tool=can_use_tool,
        continue_conversation=not first,
        mcp_servers={"screenshot": screenshot_server},
        allowed_tools=SCREENSHOT_TOOL_NAMES,
    )


async def run_turn(prompt: str, cwd: str, is_followup: bool = False) -> str:
    """跑一回合，把过程流式发到 Telegram。返回 completed/waiting。"""
    if SDK_IMPORT_ERROR is not None:
        raise AgentRunnerError(
            "本机未安装 claude-agent-sdk。先执行：py -3.11 -m pip install -r requirements.txt"
        ) from SDK_IMPORT_ERROR

    _reset_stop_flag()
    first = cwd not in _seen_cwds
    _seen_cwds.add(cwd)
    options = _build_options(cwd, first)
    outcome = TurnOutcome()

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if _stop_requested:
                raise asyncio.CancelledError()
            maybe_waiting = await _forward(msg, is_followup=is_followup)
            if maybe_waiting:
                outcome.state = "waiting"

    return outcome.state


async def _forward(msg: Any, is_followup: bool = False) -> bool:
    """把一条 SDK 消息转成人话发到 Telegram。返回是否看起来在等用户。"""
    waiting = False

    if AssistantMessage is not None and isinstance(msg, AssistantMessage):
        for block in getattr(msg, "content", []):
            if TextBlock is not None and isinstance(block, TextBlock):
                text = (getattr(block, "text", "") or "").strip()
                if text:
                    await channel.send_text(text)
                    if _looks_like_question(text):
                        waiting = True
            elif ToolUseBlock is not None and isinstance(block, ToolUseBlock):
                tool_name = getattr(block, "name", "工具")
                await channel.send_status(f"🔧 正在使用工具：{tool_name}")

    elif ResultMessage is not None and isinstance(msg, ResultMessage):
        cost = getattr(msg, "total_cost_usd", None)
        tail = f"（本轮花费约 ${cost:.4f}）" if cost else ""
        await channel.send_text(f"✅ 本轮处理结束 {tail}")

    else:
        text = str(msg).strip()
        if text:
            await channel.send_text(text)
            if _looks_like_question(text):
                waiting = True

    return waiting


def _looks_like_question(text: str) -> bool:
    low = text.lower()
    question_mark = "?" in text or "？" in text
    ask_words = [
        "你希望",
        "请选择",
        "请告诉我",
        "你要",
        "which",
        "would you like",
        "do you want",
        "please tell me",
        "需要你",
        "告诉我",
    ]
    return question_mark or any(word in low for word in ask_words)
