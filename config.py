"""集中读取配置和风险规则。所有可调项都在这里。"""
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_PROJECT_DIR = os.getenv("DEFAULT_PROJECT_DIR", os.getcwd())
APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT", "300"))
APPROVAL_TIMEOUT_ACTION = os.getenv("APPROVAL_TIMEOUT_ACTION", "deny").lower()

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
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not ALLOWED_CHAT_ID:
        missing.append("ALLOWED_CHAT_ID")
    return missing


def format_missing_config(missing: list[str]) -> str:
    fields = ", ".join(missing)
    return (
        f"❌ .env 缺少必填项：{fields}\n"
        "请先复制 .env.example 为 .env，并填写 Telegram token 与你的 chat_id。\n"
        "如果还没拿到 chat_id：先用手机给 bot 发条消息，再打开\n"
        "https://api.telegram.org/bot<你的TOKEN>/getUpdates\n"
        "在返回 JSON 里找 chat.id。"
    )
