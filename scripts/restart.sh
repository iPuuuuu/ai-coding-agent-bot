#!/usr/bin/env bash
# Restart the bot.
set -euo pipefail
cd "$(dirname "$0")/.."
"$(dirname "$0")/stop.sh" || true
"$(dirname "$0")/run.sh"
