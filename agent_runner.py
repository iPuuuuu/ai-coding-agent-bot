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
import re
import shutil
import time
from dataclasses import dataclass, field
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
4. 你的输出会被转发到 Telegram，尽量保持结构清楚。
5. 如果需要用户做选择，请尽量列出清楚的候选项（例如 1. / 2. / 3.）。
6. 如果你需要用户补充信息，请直接提问。
7. 如果你判断项目适合运行后截图，请在结论里明确说出建议的启动命令或可访问地址。

风险规则：
{rules}
"""

_stop_requested = False
_last_session_id_by_cwd: dict[str, str] = {}
_active_process: Optional[asyncio.subprocess.Process] = None


@dataclass
class MirrorEvent:
    kind: str
    text: str
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class TurnOutcome:
    state: str = "completed"
    final_text: str = ""
    session_id: str = ""
    waiting_text: str = ""
    choice_options: list[str] = field(default_factory=list)
    events: list[MirrorEvent] = field(default_factory=list)


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


async def run_turn(prompt: str, cwd: str, is_followup: bool = False) -> TurnOutcome:
    """跑一回合，把过程镜像发到 Telegram。"""
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
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            waiting = await _handle_stream_line(line, outcome)
            if waiting:
                outcome.state = "waiting"

        rc = await proc.wait()
        if _stop_requested:
            raise asyncio.CancelledError()
        if rc != 0:
            raise AgentRunnerError(outcome.final_text or f"Claude CLI 退出码 {rc}")

        if outcome.session_id:
            _last_session_id_by_cwd[cwd] = outcome.session_id

        await _maybe_send_visual_help(prompt, cwd)
        return outcome
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
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        outcome.events.append(MirrorEvent(kind="raw/non_json", text=line, session_id=outcome.session_id))
        await channel.send_text(line)
        return False

    event_type = event.get("type")
    if event_type == "system" and event.get("subtype") == "init":
        outcome.session_id = event.get("session_id", "")
        outcome.events.append(MirrorEvent(kind="system/init", text=f"session={outcome.session_id}", session_id=outcome.session_id))
        await channel.send_status(f"会话已建立：{outcome.session_id[:8]}")
        return False

    if event_type == "assistant":
        message = event.get("message", {})
        session_id = event.get("session_id", outcome.session_id)
        text = _extract_text(message)
        thinking = _extract_thinking(message)
        if thinking:
            outcome.events.append(MirrorEvent(kind="assistant/thinking", text=thinking, session_id=session_id))
            await channel.send_status(f"思考中：{thinking[:120]}")
        if text:
            outcome.events.append(MirrorEvent(kind="assistant/text", text=text, session_id=session_id))
            await channel.send_text(text)
            options = _parse_options(text)
            if options:
                outcome.waiting_text = text
                outcome.choice_options = options
                return True
            if _looks_like_question(text):
                outcome.waiting_text = text
                return True
        return False

    if event_type == "result":
        session_id = event.get("session_id", outcome.session_id)
        outcome.session_id = session_id
        result = (event.get("result") or "").strip()
        subtype = event.get("subtype") or ""
        kind = f"result/{subtype or 'unknown'}"
        if result:
            outcome.final_text = result
            outcome.events.append(MirrorEvent(kind=kind, text=result, session_id=session_id))
            if subtype == "success":
                await channel.send_text(result)
                options = _parse_options(result)
                if options:
                    outcome.waiting_text = result
                    outcome.choice_options = options
                    return True
                if _looks_like_question(result):
                    outcome.waiting_text = result
                    return True
            else:
                raise AgentRunnerError(result)
        elif subtype != "success":
            raise AgentRunnerError("Claude CLI 执行失败")
        return False

    summary = _summarize_event(event)
    if summary:
        outcome.events.append(MirrorEvent(kind=f"system/{event_type}", text=summary, session_id=outcome.session_id))
        await channel.send_status(summary)
    return False


async def _maybe_send_visual_help(prompt: str, cwd: str):
    if not any(word in prompt for word in ["截图", "跑起来", "运行", "效果", "网页"]):
        return
    hint = detect_run_hint(cwd)
    if hint:
        await channel.send_text(
            f"可尝试运行：`{hint.command}`"
            + (f"\n地址：{hint.url_hint}" if hint.url_hint else "")
        )
    await maybe_handle_visual_request(prompt)


def _extract_text(message: dict) -> str:
    parts = []
    for block in message.get("content", []):
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_thinking(message: dict) -> str:
    parts = []
    for block in message.get("content", []):
        if block.get("type") == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _parse_options(text: str) -> list[str]:
    options = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(\d+[\).]|[A-Da-d][\).]|[-*•])\s+", line):
            cleaned = re.sub(r"^(\d+[\).]|[A-Da-d][\).]|[-*•])\s+", "", line).strip()
            if cleaned:
                options.append(cleaned[:60])
    deduped = []
    for option in options:
        if option not in deduped:
            deduped.append(option)
    return deduped[:4] if 2 <= len(deduped) <= 4 else []


def _summarize_event(event: dict) -> str:
    event_type = event.get("type")
    if event_type == "system":
        subtype = event.get("subtype")
        if subtype in {"init", "thinking_tokens"}:
            return ""
        if subtype == "thinking":
            return "Claude 正在思考…"
        if subtype:
            return f"系统事件：{subtype}"
    if event_type in {"assistant", "result", "user"}:
        return ""
    if event_type:
        return f"事件：{event_type}"
    return ""


def _looks_like_question(text: str) -> bool:
    low = text.lower()
    if "?" in text or "？" in text:
        return True
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
        "请回复我",
        "需要你决定",
    ]
    return any(word in low for word in ask_words)


def _claude_command() -> str:
    if os.name != "nt":
        return shutil.which("claude") or "claude"

    cmd_path = shutil.which("claude.cmd")
    if cmd_path:
        exe_path = os.path.join(
            os.path.dirname(cmd_path),
            "node_modules",
            "@anthropic-ai",
            "claude-code",
            "bin",
            "claude.exe",
        )
        if os.path.exists(exe_path):
            return exe_path
        return cmd_path

    return "claude.cmd"
