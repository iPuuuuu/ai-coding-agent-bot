#!/usr/bin/env bash
# Show whether the bot is running.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f logs/bot.pid ]; then
  echo "bot: 未在运行"
  exit 0
fi
PID="$(cat logs/bot.pid)"
if kill -0 "$PID" 2>/dev/null; then
  echo "bot: 运行中（PID ${PID}）"
  ps -o pid,lstart,etime,command -p "$PID" | tail -n +2
else
  echo "bot: 未在运行（pid 文件过期）"
fi
