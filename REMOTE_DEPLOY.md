# 远程架构部署说明（bot 常驻云端，codex 留在家里电脑）

适用场景：你在外面，用手机指挥**家里电脑**上的 Codex AI 干活，并实时查看会话状态、参与决策。

```text
手机(飞书) ──► 云端 ECS 上的 bot（24/7 在线）
                  │  （SSH 反向隧道，家庭网络无需公网 IP）
                  ▼
            家里电脑（Mac）上的 codex exec ──► 你的项目
```

- 飞书长连接、任务调度、会话卡片、`@会话前8位` 路由都跑在云端 bot 上；
- Codex CLI 留在家里电脑（API 密钥不离开家）；
- 家里电脑必须开机并保持隧道在线（launchd 守护，断线自动重连）；
- 云端 bot 通过隧道 SSH 回家里电脑执行 `codex exec` / `codex exec resume`，
  会话元数据也经隧道远程扫描（`/monitor`、`/sessions` 依然可用）。

## 一、家里电脑（Mac）一次性准备

1. **开启远程登录**（隧道要连回 Mac 的 sshd）：
   - 系统设置 → 通用 → 共享 → 远程登录 → 开启；或
   - `sudo systemsetup -setremotelogin on`
2. **允许云端登录 Mac**：把云端生成的公钥追加到 `~/.ssh/authorized_keys`（见第三节第 2 步）。
3. **安装反向隧道**（Mac 开机自启、断线自动重连）：

   ```bash
   cd ~/ai-coding-agent-bot
   scripts/install_mac_tunnel.sh 120.55.186.220 2200
   # 查看状态：
   launchctl print gui/$(id -u)/com.a1234.ai-coding-agent-tunnel | head -20
   tail -5 logs/tunnel.err.log
   ```

## 二、云端服务器（ECS）一次性准备

1. 安装 Python 3.11+、同步代码、部署 bot：

   ```bash
   rsync -az --exclude='.git' --exclude='logs' --exclude='.venv' \
     ./ root@120.55.186.220:/srv/ai-coding-agent-bot/
   ssh root@120.55.186.220 'bash /srv/ai-coding-agent-bot/scripts/deploy_ecs.sh'
   ```

2. 生成云端→家里的 SSH 密钥，并放行到 Mac：

   ```bash
   ssh root@120.55.186.220 'ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" -q && cat /root/.ssh/id_ed25519.pub'
   # 把输出追加到家里 Mac 的 ~/.ssh/authorized_keys（chmod 600）
   ```

3. 填写 `/etc/ai-coding-agent-bot.env`（飞书凭据、家里用户名/路径、隧道端口），然后：

   ```bash
   ssh root@120.55.186.220 'systemctl restart ai-coding-agent-bot && journalctl -u ai-coding-agent-bot -n 50 --no-pager'
   ```

## 三、验证链路

```bash
# 1) 隧道在线（在云端执行，应输出 hello）
ssh root@120.55.186.220 'ssh -p 2200 USER@localhost "echo hello"'

# 2) 远端 codex 可用
ssh root@120.55.186.220 'ssh -p 2200 USER@localhost "codex --version"'

# 3) 手机给飞书 bot 发 /health → 应显示“远端（-p 2200 USER@localhost）”
# 4) 发 /sessions → 能看到家里电脑的 Codex 会话列表与操作卡片
```

## 四、Web 看板（浏览器/手机查看会话状态）

bot 内嵌一个只读看板（零第三方依赖），随 bot 启动：

```env
# /etc/ai-coding-agent-bot.env 追加：
DASHBOARD_ENABLED=1
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
DASHBOARD_TOKEN=<一个随机长串，用于鉴权>
```

- 访问：`http://ECS_IP:8080/?token=<你的token>`（深色主题、手机自适应、5 秒自动刷新）
- 页面展示：运行中/等待决策/队列统计、每个会话的状态卡片（项目/短ID/状态徽标/最后事件）、点击卡片看最近窗口
- 数据来源：云端 bot 的会话缓存（经隧道远程扫描家里电脑的 `~/.codex/sessions`）
- 安全：无 token 时看板只监听 `127.0.0.1`；配了 token 才对外监听，API 全部要求 `?token=` 或 `X-Dashboard-Token` 头
- 提示：云服务器安全组需放行 `DASHBOARD_PORT`（如 8080）；不建议把 token 写进书签，可加在 URL 上（页面会记住到 localStorage）

## 五、故障排查

| 现象 | 处理 |
|---|---|
| `/health` 显示远端不可用 | `ssh root@ECS 'ssh -p 2200 USER@localhost "codex --version"'` 逐段定位；检查 Mac 隧道 `tail -5 ~/ai-coding-agent-bot/logs/tunnel.err.log` |
| 隧道反复掉线 | 检查 ECS 侧 `ss -tlnp | grep 2200` 是否监听 127.0.0.1:2200；Mac 侧 launchd KeepAlive 会自动重连 |
| 任务超时无响应 | codex 执行 ssh 已带 ServerAliveInterval=30 心跳；若网络抖动导致中断，`/stop` 后重发任务 |

## 六、安全说明

- 隧道只绑云端 `127.0.0.1:2200`，不对公网开放；
- 云端→家里的 SSH 用独立密钥（`/root/.ssh/id_ed25519`），只允许登录家里电脑；
- 飞书白名单（`ALLOWED_FEISHU_OPEN_ID`）不变，仍只有你能指挥；
- 家里电脑的 codex API 密钥不离开家；云端不存任何 AI 密钥。
