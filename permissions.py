"""
permissions.py —— 灵魂所在：can_use_tool 三档分级回调。

这个回调决定 Agent 每一步操作是"自己干 / 问你 / 直接拒"。
它就是"不卡住、但需要审核时主动问我"这个需求的实现核心。

签名与返回值遵循 claude-agent-sdk 的权限回调协议：
    async def can_use_tool(tool_name, tool_input, context) -> PermissionResult
返回 PermissionResultAllow() 或 PermissionResultDeny(message=...)。

⚠️ 若你装的 SDK 版本里这两个类名/签名不同，用
    py -3.11 -c "import claude_agent_sdk; print(dir(claude_agent_sdk))"
核对后微调本文件的 import 与返回构造即可，其余逻辑不用动。
"""
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

import channel
import config


def _matches(cmd: str, keywords: list[str]) -> bool:
    low = cmd.lower()
    return any(k.lower() in low for k in keywords)


async def can_use_tool(tool_name, tool_input, context):
    tool_input = tool_input or {}

    # 🟢 绿灯：只读 + 项目内编辑，永远放行，不打扰你
    if tool_name in config.GREEN_TOOLS:
        return PermissionResultAllow()

    # Bash 是唯一需要精细判断的高危工具
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") or ""

        # 🔴 红灯：命中即拒，连问都不问
        if _matches(cmd, config.RED_KEYWORDS):
            await channel.send_text(f"🔴 已拦截高危命令：\n{cmd}")
            return PermissionResultDeny(message="高危命令，安全策略禁止执行。")

        # 🟡 黄灯：发 Telegram 问你（带超时兜底，不会永久卡死）
        if _matches(cmd, config.YELLOW_KEYWORDS):
            ok = await channel.ask_approval(
                text=f"🟡 需要你确认是否执行：\n\n{cmd}",
                timeout=config.APPROVAL_TIMEOUT,
                timeout_action=config.APPROVAL_TIMEOUT_ACTION,
            )
            if ok:
                return PermissionResultAllow()
            return PermissionResultDeny(message="你拒绝了这条命令，请换个方式或跳过这一步。")

        # 其余 Bash（跑测试、git status/diff、ls 等）默认放行
        return PermissionResultAllow()

    # 其它工具（含自定义截图 MCP 工具）默认放行
    return PermissionResultAllow()
