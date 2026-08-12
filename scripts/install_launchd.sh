#!/usr/bin/env bash
# Install/refresh the macOS launchd service for the bot.
# Gives auto-start on login and automatic restart after a crash.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.a1234.ai-coding-agent-bot"
SOURCE="$PROJECT_DIR/scripts/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$SOURCE" ]; then
  echo "找不到 plist 模板：$SOURCE"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SOURCE" "$DEST"

# Remove any previous instance of the service, then install and start it.
launchctl bootout "gui/$(id -u)" "$DEST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "launchd 服务已安装并启动：$LABEL"
echo "查看状态：launchctl print gui/$(id -u)/$LABEL | head -20"
echo "卸载：scripts/uninstall_launchd.sh"
