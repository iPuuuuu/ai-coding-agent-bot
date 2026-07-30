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

# 让 SDK 能读到 key（SDK 会读环境变量 ANTHROPIC_API_KEY）
if ANTHROPIC_API_KEY:
    os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

# ============ 权限三档分级规则（permissions.py 用）============

# 🟢 绿灯：这些工具永远自动放行（只读 + 项目内编辑）
GREEN_TOOLS = {"Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit", "TodoWrite"}

# 🔴 红灯：Bash 命令里出现这些关键词 → 直接拒绝，连问都不问
RED_KEYWORDS = [
    "rm -rf /", "rm -rf ~", ":(){", "mkfs", "format ", "del /f /s",
    "shutdown", "reg delete", "diskpart", "> /dev/sda",
]

# 🟡 黄灯：Bash 命令里出现这些关键词 → 发 Telegram 问你（带超时默认）
YELLOW_KEYWORDS = [
    "rm ", "rmdir", "del ", "mv ", "move ",           # 删除/移动
    "pip install", "npm install", "npm i ", "yarn add", "conda install", "apt ", "choco ",  # 装依赖
    "git push", "git reset --hard", "git clean",       # 有破坏性的 git
    "curl ", "wget ", "Invoke-WebRequest",             # 联网下载
    "chmod", "chown", "runas", "sudo ",                # 权限相关
]


def validate() -> list[str]:
    """启动时检查必填项，返回缺失项列表。"""
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not ALLOWED_CHAT_ID:
        missing.append("ALLOWED_CHAT_ID")
    return missing
