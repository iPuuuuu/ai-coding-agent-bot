"""集中读取配置和风险规则。所有可调项都在这里。"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
# `telegram` (the historical default) or `feishu`.  Feishu uses the official
# long-connection gateway, so it does not need a public webhook address.
BOT_CHANNEL = os.getenv("BOT_CHANNEL", "telegram").strip().lower()
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
# The open_id of the Feishu account allowed to operate this bot.  It is
# intentionally different from a chat_id: an open_id identifies the sender.
ALLOWED_FEISHU_OPEN_ID = os.getenv("ALLOWED_FEISHU_OPEN_ID", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Codex engine settings.  The sandbox restricts what the agent's generated
# shell commands may touch; workspace-write allows file edits and tests inside
# the project while keeping the rest of the machine read-only.
def _resolve_sandbox_mode() -> str:
    """CODEX_SANDBOX may leak in from the ambient shell environment (e.g. a
    parent Codex session sets it to an implementation name like "seatbelt").
    Only the values the CLI accepts are meaningful; anything else falls back
    to the safe default so the bot can still start.
    """
    value = os.getenv("CODEX_SANDBOX", "workspace-write").strip().lower()
    if value in {"read-only", "workspace-write"}:
        return value
    if value:
        print(
            f"[config] 警告：CODEX_SANDBOX={value!r} 不是有效值，已回退为 workspace-write",
            file=sys.stderr,
        )
    return "workspace-write"


CODEX_SANDBOX = _resolve_sandbox_mode()
CODEX_MODEL = os.getenv("CODEX_MODEL", "").strip()

# ---- 远程执行模式 ----
# 场景：bot 常驻云端服务器（如阿里云 ECS，24/7 在线），而 Codex CLI 安装在
# 家里的电脑上。此时设 CODEX_REMOTE=1，并把 CODEX_SSH_TARGET 指向能到达
# Codex 机器的 ssh 目标（通常经反向隧道，如 "-p 2200 a1234@localhost"）。
# Codex 命令会通过 ssh 在远端执行，会话监控也会改为远端扫描。
CODEX_REMOTE = os.getenv("CODEX_REMOTE", "0").strip().lower() in {"1", "true", "yes", "on"}
CODEX_SSH_TARGET = os.getenv("CODEX_SSH_TARGET", "").strip()
CODEX_SSH_EXTRA_ARGS = os.getenv("CODEX_SSH_EXTRA_ARGS", "").strip()
# 远端 ~/.codex/sessions 所在路径（默认远端 $HOME/.codex/sessions）
CODEX_REMOTE_SESSIONS_DIR = os.getenv("CODEX_REMOTE_SESSIONS_DIR", "").strip()
# 远端 codex 可执行文件路径（不填则按 PATH + 常见安装位置探测）
CODEX_REMOTE_BIN = os.getenv("CODEX_REMOTE_BIN", "").strip()

DEFAULT_PROJECT_DIR = os.getenv("DEFAULT_PROJECT_DIR", "") or PROJECT_ROOT
DEFAULT_PROJECTS = [
    path.strip()
    for path in os.getenv(
        "MONITORED_PROJECTS",
        DEFAULT_PROJECT_DIR,
    ).split(",")
    if path.strip()
]
# Treat configured roots as project shelves: each direct child directory is a
# selectable project, while Claude/Codex session monitoring itself remains
# recursive because it follows the local session metadata rather than paths.
PROJECTS_ROOTS = [
    path.strip()
    for path in os.getenv("PROJECTS_ROOTS", "").split(",")
    if path.strip()
]
for root in PROJECTS_ROOTS:
    try:
        children = [entry.path for entry in os.scandir(root) if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        children = []
    for child in sorted(children, key=str.lower):
        if child not in DEFAULT_PROJECTS:
            DEFAULT_PROJECTS.append(child)
if DEFAULT_PROJECT_DIR not in DEFAULT_PROJECTS:
    DEFAULT_PROJECTS.insert(0, DEFAULT_PROJECT_DIR)

APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT", "300"))
APPROVAL_TIMEOUT_ACTION = os.getenv("APPROVAL_TIMEOUT_ACTION", "deny").lower()
# How many Codex sessions may run tasks at the same time.  Each session gets
# its own `codex exec` subprocess; a value of 1 keeps the old serial behavior.
def _resolve_parallel_limit() -> int:
    try:
        limit = int(os.getenv("MAX_PARALLEL_TASKS", "3"))
    except ValueError:
        limit = 3
    return max(1, min(limit, 8))


MAX_PARALLEL_TASKS = _resolve_parallel_limit()
# Long-running task heartbeat interval in seconds; 0 disables the heartbeat.
# 120s keeps the phone feed readable: one short progress line every 2 minutes.
HEARTBEAT_SECONDS = float(os.getenv("HEARTBEAT_SECONDS", "120"))
# Autosave interval for sessions/transcripts (seconds); 0 disables autosave.
STATE_AUTOSAVE_SECONDS = float(os.getenv("STATE_AUTOSAVE_SECONDS", "30"))

SESSION_MONITOR_DIR = os.getenv(
    "SESSION_MONITOR_DIR",
    os.path.join(HOME, ".claude", "session-monitor"),
)
SESSION_EVENT_LOG = os.path.join(SESSION_MONITOR_DIR, "events.jsonl")
SESSION_SNAPSHOT_FILE = os.path.join(SESSION_MONITOR_DIR, "sessions.json")
GLOBAL_HOOK_SCRIPT = os.path.join(DEFAULT_PROJECT_DIR, "tools", "claude_session_hook.py")
CODEX_SESSIONS_DIR = os.getenv(
    "CODEX_SESSIONS_DIR",
    os.path.join(HOME, ".codex", "sessions"),
)
SESSION_MONITOR_POLL_SECONDS = float(os.getenv("SESSION_MONITOR_POLL_SECONDS", "5"))
CLAUDE_PROJECTS_DIR = os.getenv(
    "CLAUDE_PROJECTS_DIR",
    os.path.join(HOME, ".claude", "projects"),
)
CLAUDE_TRANSCRIPT_SCAN_LIMIT = int(os.getenv("CLAUDE_TRANSCRIPT_SCAN_LIMIT", "120"))

# ---- Web 看板（只读）----
# 在浏览器/手机上查看家里电脑的会话状态。token 必填才会对外监听；
# 不填 token 时仅绑定 127.0.0.1 防止裸奔。
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0").strip()
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()

if ANTHROPIC_API_KEY:
    os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

# ============ 权限三档分级规则（permissions.py 用）============

GREEN_TOOLS = {"Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit"}

RED_KEYWORDS = [
    "rm -rf /", "rm -rf ~", ":(){", "mkfs", "format ", "del /f /s",
    "shutdown", "reg delete", "diskpart", "> /dev/sda",
]

YELLOW_KEYWORDS = [
    "rm ", "rmdir", "del ", "mv ", "move ",
    "pip install", "npm install", "npm i ", "yarn add", "conda install", "apt ", "choco ",
    "git push", "git reset --hard", "git clean",
    "curl ", "wget ", "Invoke-WebRequest",
    "chmod", "chown", "runas", "sudo ",
]


def validate() -> list[str]:
    """启动时检查必填项，返回缺失项列表。"""
    missing = []
    if BOT_CHANNEL == "telegram":
        if not TELEGRAM_TOKEN:
            missing.append("TELEGRAM_TOKEN")
        if not ALLOWED_CHAT_ID:
            missing.append("ALLOWED_CHAT_ID")
    elif BOT_CHANNEL == "feishu":
        if not FEISHU_APP_ID:
            missing.append("FEISHU_APP_ID")
        if not FEISHU_APP_SECRET:
            missing.append("FEISHU_APP_SECRET")
    else:
        missing.append("BOT_CHANNEL (telegram or feishu)")
    if CODEX_SANDBOX not in {"read-only", "workspace-write"}:
        missing.append("CODEX_SANDBOX (仅支持 read-only 或 workspace-write)")
    return missing


def format_missing_config(missing: list[str]) -> str:
    fields = ", ".join(missing)
    if BOT_CHANNEL == "feishu":
        return (
            f"❌ .env 缺少飞书必填项：{fields}\n"
            "请在飞书开放平台创建并发布企业自建应用，并填写 FEISHU_APP_ID、"
            "FEISHU_APP_SECRET。授权用户配置方法详见 FEISHU.md。"
        )
    return (
        f"❌ .env 缺少必填项：{fields}\n"
        "请先复制 .env.example 为 .env，并按所选 BOT_CHANNEL 填写对应配置。\n"
        "如果还没拿到 chat_id：先用手机给 bot 发条消息，再打开\n"
        "https://api.telegram.org/bot<你的TOKEN>/getUpdates\n"
        "在返回 JSON 里找 chat.id。"
    )
