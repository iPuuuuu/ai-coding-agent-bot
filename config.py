"""集中读取配置和风险规则。所有可调项都在这里。"""
import os
from dotenv import load_dotenv

load_dotenv()

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
DEFAULT_PROJECT_DIR = os.getenv("DEFAULT_PROJECT_DIR", os.getcwd())
DEFAULT_PROJECTS = [
    path.strip()
    for path in os.getenv(
        "MONITORED_PROJECTS",
        "C:/Users/wmh/Desktop/bot,C:/Users/wmh/Desktop/doctor-wang",
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
SESSION_MONITOR_DIR = os.getenv(
    "SESSION_MONITOR_DIR",
    "C:/Users/wmh/.claude/session-monitor",
)
SESSION_EVENT_LOG = os.path.join(SESSION_MONITOR_DIR, "events.jsonl")
SESSION_SNAPSHOT_FILE = os.path.join(SESSION_MONITOR_DIR, "sessions.json")
GLOBAL_HOOK_SCRIPT = os.path.join(DEFAULT_PROJECT_DIR, "tools", "claude_session_hook.py")
CODEX_SESSIONS_DIR = os.getenv(
    "CODEX_SESSIONS_DIR",
    os.path.join(os.path.expanduser("~"), ".codex", "sessions"),
)
SESSION_MONITOR_POLL_SECONDS = float(os.getenv("SESSION_MONITOR_POLL_SECONDS", "5"))

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
