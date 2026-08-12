#!/usr/bin/env bash
# Stop the bot started by scripts/run.sh (uses logs/bot.pid).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f logs/bot.pid ]; then
  echo "没有 pid 文件，bot 可能未通过 scripts/run.sh 启动。"
  echo "如需清理所有 bot.py 实例，请执行 scripts/stop_all.sh"
  exit 0
fi

PID="$(cat logs/bot.pid)"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "bot 未在运行（pid 文件过期），已清理。"
  rm -f logs/bot.pid
  exit 0
fi

kill "$PID"
for _ in $(seq 1 20); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$PID" 2>/dev/null; then
  echo "正常退出超时，强制结束 PID $PID"
  kill -9 "$PID" || true
fi
rm -f logs/bot.pid
echo "bot 已停止（PID $PID）"
