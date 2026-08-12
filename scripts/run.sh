#!/usr/bin/env bash
# Start the bot as a background process (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

mkdir -p logs

if [ -f logs/bot.pid ]; then
  EXISTING="$(cat logs/bot.pid)"
  if kill -0 "$EXISTING" 2>/dev/null; then
    echo "bot already running (PID $EXISTING). 停止请用 scripts/stop.sh"
    exit 1
  fi
  rm -f logs/bot.pid
fi

nohup "$PYTHON" bot.py >> logs/bot.out.log 2>&1 &
echo $! > logs/bot.pid
echo "bot started, PID $(cat logs/bot.pid)"
echo "输出日志：logs/bot.out.log（运行日志 logs/bot.log）"
