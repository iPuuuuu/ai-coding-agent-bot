"""
agent_runner.py —— 封装一次 Claude Code CLI 对话回合。

每收到你的一条指令就调用 run_turn()：
- 启动本机 claude CLI 子进程
- 流式读取 stream-json 输出
- 把文本/状态/结果转发到 Telegram
- 支持停止当前任务

这条路线不再依赖 claude-agent-sdk，避免 pip 依赖链阻塞。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import Optional

import channel
from permissions import build_supervisor_rules_text
from tools.run_project import detect_run_hint
from tools.screenshot import maybe_handle_visual_request

SYSTEM_PROMPT = """你是一个常驻在用户家里 Windows 电脑上的编程监督助手，通过 Telegram 和用户沟通。
原则：
1. 你运行在 Claude Code CLI 中，可以读写代码、运行命令、修改项目。
2. 用户只有手机 Telegram，所以你要主动汇报当前进度、下一步和阻塞点。
3. 如果任务涉及高风险操作（例如安装依赖、联网下载、删除文件、git push），不要直接执行；先明确向用户提问，等待用户回复后再继续。
4. 回答保持精炼，适合手机阅读。
5. 如果你需要用户补充信息，请明确提出问题，并以问句结尾。
6. 如果你判断项目适合运行后截图，请在结论里明确说出建议的启动命令或可访问地址。

风险规则：
{rules}
"""

_stop_requested = False
_last_session_id_by_cwd: dict[str, str] = {}
_active_process: Optional[asyncio.subprocess.Process] = None


def _claude_command() -> str:
    command_name = "claude.cmd" if os.name == "nt" else "claude"
    resolved = shutil.which(command_name)
    return resolved or command_name


@dataclass
class TurnOutcome:
    state: str = "completed"
    final_text: str = ""
    session_id: str = ""


class AgentRunnerError(RuntimeError):
    pass


def request_stop():
    global _stop_requested
    _stop_requested = True
    proc = _active_process
    if proc and proc.returncode is None:
        proc.terminate()


def _reset_stop_flag():
    global _stop_requested
    _stop_requested = False


async def run_turn(prompt: str, cwd: str, is_followup: bool = False) -> str:
    """跑一回合，把过程流式发到 Telegram。返回 completed/waiting。"""
    await _ensure_claude_available()
    _reset_stop_flag()

    outcome = TurnOutcome()
    system_prompt = SYSTEM_PROMPT.format(rules=build_supervisor_rules_text())
    command = _claude_command()

    cmd = [
        command,
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--permission-mode",
        "dontAsk",
        "--append-system-prompt",
        system_prompt,
        prompt,
    ]

    session_id = _last_session_id_by_cwd.get(cwd)
    if is_followup and session_id:
        cmd[1:1] = ["--resume", session_id]

    global _active_process
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise AgentRunnerError(f"启动 Claude CLI 失败：找不到可执行文件 {command}") from exc
    _active_process = proc

    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            if _stop_requested:
                raise asyncio.CancelledError()
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            waiting = await _handle_stream_line(line, outcome)
            if waiting:
                outcome.state = "waiting"

        rc = await proc.wait()
        if _stop_requested:
            raise asyncio.CancelledError()
        if rc != 0:
            raise AgentRunnerError(f"Claude CLI 退出码 {rc}")

        if outcome.session_id:
            _last_session_id_by_cwd[cwd] = outcome.session_id

        await _maybe_send_visual_help(prompt, cwd)
        return outcome.state
    finally:
        _active_process = None


async def _ensure_claude_available():
    command = _claude_command()
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AgentRunnerError(f"本机 Claude CLI 不可用：找不到可执行文件 {command}") from exc
    out, err = await proc.communicate()
    if proc.returncode != 0:
        detail = (err or out).decode("utf-8", errors="replace").strip()
        raise AgentRunnerError(f"本机 Claude CLI 不可用：{detail or f'无法执行 {command}'}")


async def _handle_stream_line(line: str, outcome: TurnOutcome) -> bool:
    waiting = False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        await channel.send_text(line)
        return _looks_like_question(line)

    event_type = event.get("type")
    if event_type == "system" and event.get("subtype") == "init":
        outcome.session_id = event.get("session_id", "")
        return False

    if event_type == "assistant":
        message = event.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    outcome.final_text = text
                    await channel.send_text(text)
                    if _looks_like_question(text):
                        waiting = True
        return waiting

    if event_type == "result":
        outcome.session_id = event.get("session_id", outcome.session_id)
        result = (event.get("result") or "").strip()
        if result and result != outcome.final_text:
            outcome.final_text = result
            await channel.send_text(result)
        if event.get("subtype") == "success":
            await channel.send_text("✅ 本轮处理结束")
        else:
            raise AgentRunnerError(result or "Claude CLI 执行失败")
        return _looks_like_question(result)

    if event_type == "user":
        return False

    summary = _summarize_event(event)
    if summary:
        await channel.send_status(summary)
    return False


async def _maybe_send_visual_help(prompt: str, cwd: str):
    if not any(word in prompt for word in ["截图", "跑起来", "运行", "效果", "网页"]):
        return
    hint = detect_run_hint(cwd)
    if hint:
        await channel.send_text(
            f"💡 我检测到这个项目可能可以这样运行：`{hint.command}`"
            + (f"\n可能地址：{hint.url_hint}" if hint.url_hint else "")
            + (f"\n依据：{hint.reason}" if hint.reason else "")
        )
    await maybe_handle_visual_request(prompt)


def _summarize_event(event: dict) -> str:
    event_type = event.get("type")
    if event_type == "system":
        subtype = event.get("subtype")
        if subtype and subtype != "init":
            return f"ℹ️ {subtype}"
    if event_type and event_type not in {"assistant", "result", "user"}:
        return f"🔧 事件：{event_type}"
    return ""


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
