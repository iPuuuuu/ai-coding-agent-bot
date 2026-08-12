# Claude / Codex 会话监控

飞书机器人启动后会以本机只读方式持续监控两类会话：

- **Claude**：由项目内 `.claude/settings.json` 已配置的 Hook 写入
  `~/.claude/session-monitor/sessions.json`。
- **Codex**：扫描 `~/.codex/sessions/**/rollout-*.jsonl` 的元数据与事件类型。

监控器不会向飞书上传提示词、模型回复、工具输入、密钥或完整会话正文。飞书中只显示会话短 ID、来源、项目路径末级目录、状态、最后事件类型和最近活动时间。

## 飞书命令

- `/monitor`：统一总览所有本机 Claude 与 Codex 会话，活动会话排在前面。
- `/sessions`：显示本服务托管会话，并额外发送统一监控总览。
- `/status`：当前机器人状态加统一监控总览。

## 自动提醒规则

- **bot 托管的 Claude 会话**：Claude 的实时回复会直接转发到飞书；如果同一回合 assistant 文本和 result 文本重复，机器人会自动去重，避免重复提醒。
- **需要输入 / 需要选择**：
  - 托管会话会直接在飞书发文字或交互按钮；
  - 其他本机 Claude 会话会通过 Hook 写入 `sessions.json` / `events.jsonl`，再由机器人轮询并主动提醒。
- **Codex 会话**：仅推送状态变化（如运行中、已完成、疑似中断），不会把 rollout 正文当作回复转发。
- **已被当前 bot 托管的 Claude 会话**：轮询监控会跳过重复等待提醒，避免实时流和本机监控同时提醒同一问题。

服务运行时，状态发生转换会主动推送飞书消息，例如：

- `运行中`：检测到 Codex 的 `task_started`；
- `已完成`：检测到 Codex 的 `task_complete`；
- `已停止`：检测到 Codex 的 `turn_aborted`；
- `疑似中断`：任务开始后连续三分钟没有新的本地活动；
- `等待输入`：Claude Hook 报告等待用户确认或输入。

首次启动仅建立当前会话状态基线，避免把历史会话一次性刷屏。随后的状态变化会自动推送；需要立即查看当前正在运行的任务时，直接发送 `/monitor`。

## 可调配置

`.env` 可选配置：

```env
CODEX_SESSIONS_DIR=/Users/a1234/.codex/sessions
SESSION_MONITOR_POLL_SECONDS=5
```

默认每 5 秒扫描一次。Codex 会话目录位于非默认位置时，才需要配置 `CODEX_SESSIONS_DIR`。
