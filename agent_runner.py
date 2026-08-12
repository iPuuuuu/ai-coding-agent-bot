"""
agent_runner.py —— 封装一次 Codex CLI 对话回合。

bot 端现在显式托管 Codex session：
- 只有明确创建时才新开 Codex 会话
- 后续继续对话时，始终 resume 到指定 session
- 不再依赖“按 cwd 猜最近会话”的隐式行为
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
import config
from permissions import build_supervisor_rules_text
from tools.run_project import detect_run_hint
from tools.screenshot import maybe_handle_visual_request

SYSTEM_PROMPT = """你是一个常驻在用户电脑上的编程监督助手，通过 Telegram/飞书和用户沟通。
原则：
1. 你运行在 Codex CLI 中，可以读写代码、运行命令、修改项目。
2. 用户只有手机，所以你要主动汇报，但务必**简短**：每次只发 1-3 行有信息量的内容
   （结论 / 里程碑 / 阻塞点 / 需要用户决策的问题），不要输出思考过程流水账，
   不要复述你正在做的步骤，不要发多个几乎相同的过程性消息。
3. 如果任务涉及高风险操作（例如安装依赖、联网下载、删除文件、git push），不要直接执行；先明确向用户提问，等待用户回复后再继续。
4. 你的输出会被转发到手机，尽量保持结构清楚、简短。
5. 如果需要用户做选择，请尽量列出清楚的候选项（例如 1. / 2. / 3.）。
6. 如果你需要用户补充信息，请直接提问。
7. 如果你判断项目适合运行后截图，请在结论里明确说出建议的启动命令或可访问地址。

风险规则：
{rules}
"""

_stop_requested = False
_active_process: Optional[asyncio.subprocess.Process] = None
_last_sent_reply_by_session: dict[str, str] = {}
_last_agent_text = ""


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


def last_agent_text() -> str:
    """Latest agent output text, used by the heartbeat progress message."""
    return _last_agent_text


def _set_last_agent_text(text: str) -> None:
    global _last_agent_text
    if text:
        _last_agent_text = text.strip()


async def run_turn(prompt: str, cwd: str, session_id: str | None = None) -> TurnOutcome:
    """跑一回合；session_id 为空时新建会话，否则继续指定会话。"""
    await _ensure_codex_available()
    _reset_stop_flag()
    if session_id:
        _clear_forwarded_reply(session_id)

    outcome = TurnOutcome(session_id=session_id or "")
    system_prompt = SYSTEM_PROMPT.format(rules=build_supervisor_rules_text())

    effective_prompt = f"{system_prompt}\n\n当前用户任务：\n{prompt}"
    cmd = build_codex_command(effective_prompt, cwd, session_id)

    global _active_process
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise AgentRunnerError(f"启动 Codex CLI 失败：找不到可执行文件 {cmd[0]}") from exc
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
            raise AgentRunnerError(outcome.final_text or f"Codex CLI 退出码 {rc}")

        await _maybe_send_visual_help(prompt, cwd)
        return outcome
    finally:
        _active_process = None


def build_codex_command(prompt: str, cwd: str, session_id: str | None = None) -> list[str]:
    """Build the exact Codex CLI argv for one turn.

    Verified against Codex CLI 0.147.0:

    - New session:
        codex exec --sandbox <mode> --json -C <cwd> --skip-git-repo-check <prompt>
    - Resumed session (must run with cwd inside the project so the trusted
      directory check passes; resume inherits the session's own sandbox):
        codex exec resume <session_id> --json --skip-git-repo-check <prompt>

    Older CLI versions accepted `--ask-for-approval never`; that flag no longer
    exists and must not be passed.
    """
    command = _codex_command()
    if session_id:
        cmd = [command, "exec", "resume", session_id, "--json", "--skip-git-repo-check"]
    else:
        cmd = [
            command,
            "exec",
            "--sandbox",
            config.CODEX_SANDBOX,
            "--json",
            "-C",
            cwd,
            "--skip-git-repo-check",
        ]
    if config.CODEX_MODEL:
        cmd += ["-m", config.CODEX_MODEL]
    cmd.append(prompt)
    return cmd


async def _ensure_codex_available():
    command = _codex_command()
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AgentRunnerError(f"本机 Codex CLI 不可用：找不到可执行文件 {command}") from exc
    out, err = await proc.communicate()
    if proc.returncode != 0:
        detail = (err or out).decode("utf-8", errors="replace").strip()
        raise AgentRunnerError(f"本机 Codex CLI 不可用：{detail or f'无法执行 {command}'}")


async def _handle_stream_line(line: str, outcome: TurnOutcome) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        # Codex prints this banner on stderr/stdout when stdin is not a TTY;
        # it is noise, not an agent reply.
        if line.strip() == "Reading additional input from stdin...":
            return False
        outcome.events.append(MirrorEvent(kind="raw/non_json", text=line, session_id=outcome.session_id))
        await channel.send_text(line)
        return False

    event_type = event.get("type")
    # Codex `exec --json` emits typed thread/item/turn events rather than the
    # Legacy Claude stream-json envelope (kept for old monitored sessions).
    # Codex events are handled above. Keep only user-facing agent text and stable
    # lifecycle markers; never forward tool input or sensitive payloads.
    if event_type == "thread.started":
        outcome.session_id = str(event.get("thread_id") or outcome.session_id)
        outcome.events.append(MirrorEvent(kind="system/init", text=f"session={outcome.session_id}", session_id=outcome.session_id))
        await channel.send_status(f"Codex 会话已建立：{outcome.session_id[:8]}")
        return False
    if event_type == "item.completed":
        item = event.get("item") or {}
        if item.get("type") in {"agent_message", "assistant_message"}:
            text = str(item.get("text") or "").strip()
            if text:
                _set_last_agent_text(text)
                outcome.final_text = text
                outcome.events.append(MirrorEvent(kind="assistant/text", text=text, session_id=outcome.session_id))
                options = _parse_options(text)
                if options:
                    outcome.waiting_text, outcome.choice_options = text, options
                    return True
                if _looks_like_question(text):
                    outcome.waiting_text = text
                    return True
        return False
    if event_type in {"turn.completed", "turn.failed", "error"}:
        if event_type != "turn.completed":
            detail = str(event.get("error") or event.get("message") or "Codex CLI 执行失败")
            raise AgentRunnerError(detail)
        return False
    if event_type == "event_msg":
        payload = event.get("payload") or {}
        return False
    if event_type == "system" and event.get("subtype") == "init":
        outcome.session_id = event.get("session_id", outcome.session_id)
        outcome.events.append(MirrorEvent(kind="system/init", text=f"session={outcome.session_id}", session_id=outcome.session_id))
        await channel.send_status(f"会话已建立：{outcome.session_id[:8]}")
        return False

    if event_type == "assistant":
        message = event.get("message", {})
        session_id = event.get("session_id", outcome.session_id)
        text = _extract_text(message)
        if text:
            _set_last_agent_text(text)
            outcome.events.append(MirrorEvent(kind="assistant/text", text=text, session_id=session_id))
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
            raise AgentRunnerError("Codex CLI 执行失败")
        return False

    # Unrecognized internal events (item.started etc.) are ignored entirely:
    # they are neither forwarded to the phone nor kept in the transcript.
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


def _reply_fingerprint(text: str) -> str:
    return text.strip()[:400]


def _should_forward_reply(session_id: str, text: str) -> bool:
    fingerprint = _reply_fingerprint(text)
    if not fingerprint:
        return False
    previous = _last_sent_reply_by_session.get(session_id)
    _last_sent_reply_by_session[session_id] = fingerprint
    return previous != fingerprint


def _clear_forwarded_reply(session_id: str) -> None:
    if session_id:
        _last_sent_reply_by_session.pop(session_id, None)


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
            return "Codex 正在思考…"
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


def _codex_command() -> str:
    return shutil.which("codex") or ("codex.cmd" if os.name == "nt" else "codex")
