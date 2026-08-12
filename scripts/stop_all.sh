#!/usr/bin/env bash
# Stop every local bot.py instance.  Use this to clean up stale duplicates
# left over from before the single-instance guard existed.
set -euo pipefail
cd "$(dirname "$0")/.."

PIDS="$(pgrep -f 'bot\.py$' || true)"
if [ -z "$PIDS" ]; then
  echo "当前没有运行中的 bot.py 实例。"
  rm -f logs/bot.pid
  exit 0
fi

echo "将停止以下 bot.py 实例："
ps -o pid,lstart,command -p "$(echo "$PIDS" | tr '\n' ',' | sed 's/,$//')" | sed -n '1,30p'
kill $PIDS
sleep 1
REMAIN="$(pgrep -f 'bot\.py$' || true)"
if [ -n "$REMAIN" ]; then
  echo "仍有实例未退出，强制结束：$REMAIN"
  kill -9 $REMAIN || true
fi
rm -f logs/bot.pid
echo "已停止全部 bot.py 实例。"
