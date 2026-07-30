"""
permissions.py —— 风险规则与审批辅助。

在 Claude CLI 路线下，这个模块不再依赖 SDK 回调对象，
而是统一维护：
- 哪些操作算高风险
- 应该如何向 Claude 和用户描述这些规则
"""
from __future__ import annotations

import config


def build_supervisor_rules_text() -> str:
    yellow = "；".join(config.YELLOW_KEYWORDS)
    red = "；".join(config.RED_KEYWORDS)
    return (
        "- 以下操作视为需要先问用户再继续的高风险操作："
        f"{yellow}。\n"
        "- 以下操作视为禁止操作，不要执行："
        f"{red}。\n"
        "- 如果你准备做高风险操作，请先用自然语言明确告诉用户："
        "你要执行什么、为什么要执行、在哪个目录执行，并等待用户回复。"
    )
