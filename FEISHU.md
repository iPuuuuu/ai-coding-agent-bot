# 飞书使用说明

本服务可通过飞书机器人控制同一套 Codex 托管会话。飞书通道采用官方长连接接收事件，因此不需要公网 IP、反向代理或 Webhook URL。

## 飞书开放平台配置

1. 创建“企业自建应用”，并在“应用能力”中启用机器人。
2. 记录应用的 `App ID` 和 `App Secret`。
3. 在“权限管理”申请并发布以下权限：
   - `im:message`
   - `im:message:send_as_bot`
   - `im:resource`
4. 在“事件与回调”选择“使用长连接接收事件”，订阅：
   - `im.message.receive_v1`
   - `card.action.trigger`
5. 发布应用版本，并将机器人添加到你的私聊或目标群。

## 本地配置和启动

安装更新后的依赖：

```powershell
py -3.11 -m pip install -r requirements.txt
```

在 `.env` 设置：

```env
BOT_CHANNEL=feishu
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# First startup: leave empty; no one will be authorized.
ALLOWED_FEISHU_OPEN_ID=
```

`ALLOWED_FEISHU_OPEN_ID` 是唯一被允许发送命令的用户 Open ID；它不是群 ID，也不是 union_id。首次启动可以留空：此时所有消息都会被拒绝（安全默认），但 `logs/bot.log` 会记录发送者的 `open_id`。将该值填回 `.env` 后重启。配置完成后运行：

```powershell
py -3.11 bot.py
```

macOS 上建议用项目脚本托管（nohup 后台 + 单实例保护）：

```bash
scripts/run.sh       # 启动
scripts/status.sh    # 查看状态
scripts/stop.sh      # 停止
scripts/stop_all.sh  # 清理历史上遗留的多实例
```

机器人带有单实例保护：重复启动会被拒绝，避免多个实例抢同一个飞书长连接造成反复断线。

## 自动提醒与交互

- bot 托管的 Codex 会话回复会实时推送到飞书。
- 如果 Codex 进入“等待你输入 / 等待你选择”的状态，飞书会主动收到提醒。
- Codex 给出 2–4 个候选项时，机器人会发送交互卡片；点击按钮即可继续原会话。
- 同一会话如果既出现在实时托管流、又出现在本机监控轮询中，机器人会自动去重，避免重复提醒。
- Codex 会话目前仅做状态提醒，不转发 rollout 正文。

## Claude 本机会话监控前提

飞书主动提醒“其他本机 Claude 会话”依赖项目内 `.claude/settings.json` 注册的 `tools/claude_session_hook.py`。该 Hook 会把最小化后的会话状态写入：

- `~/.claude/session-monitor/events.jsonl`
- `~/.claude/session-monitor/sessions.json`

机器人会轮询这些文件，并在检测到等待输入、权限确认或可选按钮时主动通知飞书。

如果你的 Claude Code 还存在用户级或全局级 Hook 设置，请确认不会覆盖项目内配置；若项目 Hook 没生效，可先查看 `MONITORING.md` 的监控说明，再检查本机 Claude 设置。

## 说明

- 图片/截图会作为飞书图片发送。
- 长回复会自动分段。
- 可用命令：`/start /help /status /monitor /sessions /use /new /project /stop /health`。
- 当前服务的会话状态与原 Telegram 实现一致，是全局单会话执行模型。在群聊里，输出会发往最近一次收到授权用户消息的会话；建议优先使用私聊。若要多群并行且会话完全隔离，需要进一步将 `RuntimeState` 按 `chat_id` 拆分。
- 保留 `BOT_CHANNEL=telegram` 或不配置该项时，原 Telegram 行为不受影响。
