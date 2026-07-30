"""
permissions.py —— 灵魂所在：can_use_tool 三档分级回调。

这个回调决定 Agent 每一步操作是"自己干 / 问你 / 直接拒"。
它就是"不卡住、但需要审核时主动问我"这个需求的实现核心。

若本机还未安装 claude-agent-sdk，本文件仍可被 import，
只是实际跑 Agent 前会由 agent_runner 给出更友好的错误提示。
"""
from __future__ import annotations

try:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
except Exception:  # pragma: no cover - 依赖未安装时用兜底类保持 import 可用
    class PermissionResultAllow:  # type: ignore[override]
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class PermissionResultDeny:  # type: ignore[override]
        def __init__(self, message: str = ""):
            self.message = message

import channel
import config


def _matches(cmd: str, keywords: list[str]) -> bool:
    low = cmd.lower()
    return any(k.lower() in low for k in keywords)


async def can_use_tool(tool_name, tool_input, context):
    tool_input = tool_input or {}

    if tool_name in config.GREEN_TOOLS:
        return PermissionResultAllow()

    if tool_name == "Bash":
        cmd = tool_input.get("command", "") or ""
        cwd = tool_input.get("cwd", "(当前目录)")

        if _matches(cmd, config.RED_KEYWORDS):
            await channel.send_text(f"🔴 已拦截高危命令：\n目录：{cwd}\n命令：{cmd}")
            return PermissionResultDeny(message="高危命令，安全策略禁止执行。")

        if _matches(cmd, config.YELLOW_KEYWORDS):
            ok = await channel.ask_approval(
                text=(
                    "🟡 需要你确认是否执行风险操作\n\n"
                    f"目录：{cwd}\n"
                    f"命令：{cmd}\n\n"
                    f"超时：{config.APPROVAL_TIMEOUT}s\n"
                    f"默认动作：{config.APPROVAL_TIMEOUT_ACTION}"
                ),
                timeout=config.APPROVAL_TIMEOUT,
                timeout_action=config.APPROVAL_TIMEOUT_ACTION,
            )
            if ok:
                await channel.send_status("✅ 已通过审批，继续执行。")
                return PermissionResultAllow()
            await channel.send_status("❌ 你拒绝了该风险操作。")
            return PermissionResultDeny(message="你拒绝了这条命令，请换个方式或跳过这一步。")

        return PermissionResultAllow()

    return PermissionResultAllow()
