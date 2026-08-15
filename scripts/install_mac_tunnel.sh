#!/usr/bin/env bash
# 在家里电脑（Mac）上安装反向隧道 launchd 服务：
#   Mac sshd(22) 反代到云端服务器 localhost:PORT（默认 2200），云端 bot 经此遥控家里 codex。
# 用法：  scripts/install_mac_tunnel.sh [ECS_HOST] [PORT]
set -euo pipefail
cd "$(dirname "$0")/.."

ECS_HOST="${1:-120.55.186.220}"
PORT="${2:-2200}"
MAC_USER="$(whoami)"
PROJECT_DIR="$(pwd)"
PLIST_SRC="scripts/com.a1234.ai-coding-agent-tunnel.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.a1234.ai-coding-agent-tunnel.plist"

if [ ! -f "$PLIST_SRC" ]; then
  echo "缺少模板：$PLIST_SRC" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" logs

sed -e "s|__ECS_HOST__|$ECS_HOST|g" \
    -e "s|__MAC_USER__|$MAC_USER|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PLIST_SRC" > "$PLIST_DST"

echo "== 卸载旧服务（如有）=="
launchctl bootout "gui/$(id -u)/com.a1234.ai-coding-agent-tunnel" 2>/dev/null || true

echo "== 安装并启动隧道服务 =="
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl kickstart -k "gui/$(id -u)/com.a1234.ai-coding-agent-tunnel"

echo "✅ 隧道服务已安装：$PLIST_DST"
echo "   本地查看：launchctl print gui/$(id -u)/com.a1234.ai-coding-agent-tunnel | head"
echo "   云端验证（在 ECS 上执行）：ssh -p $PORT $MAC_USER@localhost 'echo tunnel-ok'"
