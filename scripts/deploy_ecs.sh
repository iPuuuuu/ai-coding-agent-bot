#!/usr/bin/env bash
# 在云端服务器（ECS，Ubuntu 24.04）上部署 ai-coding-agent-bot（bot 常驻云端，
# Codex 留在家里电脑，经反向隧道遥控）。
#
# 用法：
#   1. 把仓库代码放到服务器（rsync 或 git clone），默认 /srv/ai-coding-agent-bot
#   2. bash scripts/deploy_ecs.sh [/srv/ai-coding-agent-bot]
#   3. 按提示填写 /etc/ai-coding-agent-bot.env（飞书凭据 + 隧道目标）
#   4. systemctl start ai-coding-agent-bot
set -euo pipefail

PROJECT_DIR="${1:-/srv/ai-coding-agent-bot}"
ENV_FILE="/etc/ai-coding-agent-bot.env"
SERVICE_NAME="ai-coding-agent-bot"

if [ ! -f "$PROJECT_DIR/bot.py" ]; then
  echo "找不到 $PROJECT_DIR/bot.py。先同步代码，例如："
  echo "  rsync -az --exclude='.git' --exclude='logs' --exclude='.venv' ./ root@ECS:/srv/ai-coding-agent-bot/"
  exit 1
fi

echo "== 1. Python venv + 服务端依赖 =="
cd "$PROJECT_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-server.txt
echo "   依赖就绪"

echo "== 2. 生成 /etc/ai-coding-agent-bot.env =="
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'ENV'
# ai-coding-agent-bot 云端部署配置（600 权限，绝不入库）
BOT_CHANNEL=feishu
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALLOWED_FEISHU_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 家里电脑上的 Codex 会话/项目路径（这些路径存在于家里电脑，不在服务器上）
CODEX_REMOTE=1
CODEX_SSH_TARGET=-p 2200 USER@localhost
CODEX_SSH_EXTRA_ARGS=-i /root/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new
CODEX_REMOTE_SESSIONS_DIR=/Users/USER/.codex/sessions
DEFAULT_PROJECT_DIR=/Users/USER/ai-coding-agent-bot
MONITORED_PROJECTS=/Users/USER/ai-coding-agent-bot,/Users/USER/code/doctor-wang

# 可选调优
# MAX_PARALLEL_TASKS=3
# HEARTBEAT_SECONDS=120
# SESSION_MONITOR_POLL_SECONDS=5
ENV
  chmod 600 "$ENV_FILE"
  echo "   已生成模板 $ENV_FILE，请填入飞书凭据与家里电脑用户名（USER）后重启服务"
else
  echo "   $ENV_FILE 已存在，保留"
fi

echo "== 3. 安装 systemd 服务 =="
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/scripts/ai-coding-agent-bot.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
echo "   服务单元已安装：$SERVICE_NAME"

echo
echo "✅ 部署完成。接下来："
echo "   1) 编辑 $ENV_FILE 填入真实值"
echo "   2) 在家里电脑执行 scripts/install_mac_tunnel.sh 保持隧道在线"
echo "   3) 验证隧道：ssh -p 2200 USER@localhost 'echo ok'"
echo "   4) systemctl restart $SERVICE_NAME"
echo "   5) 日志：journalctl -u $SERVICE_NAME -f"
