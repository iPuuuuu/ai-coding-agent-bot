#!/usr/bin/env bash
# Stop and remove the macOS launchd service for the bot.
set -euo pipefail

LABEL="com.a1234.ai-coding-agent-bot"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$DEST" 2>/dev/null || true
rm -f "$DEST"
echo "launchd 服务已卸载：$LABEL"
