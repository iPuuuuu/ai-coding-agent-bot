# AI Coding Agent 机器人

让电脑常驻一个 **Codex 托管会话机器人**：你只用手机上的 Telegram / 飞书就能给电脑上的 Codex 下编程任务、看实时输出、点按钮继续会话，或者接管本机已经存在的 Codex 会话。

> 执行引擎是本机安装的 **Codex CLI**（`codex`，当前版本 0.147.0）。
> 旧的 Claude 会话只保留只读监控兼容。

---

## 功能

- 手机发消息 = 给当前项目下任务；已有会话则继续，没有则自动新建
- **多会话并行**：多个 Codex 会话可同时各自跑任务（`MAX_PARALLEL_TASKS` 可配），互不阻塞
- **「@会话前8位 消息」直接指挥任意会话**：不切焦点也能给指定会话下任务/继续对话
- 每个会话独立排队、独立等待回复；`/sessions` 出每会话操作卡片（详情/焦点/停止/接管）
- 接管**本机任意 Codex 会话**（包括在别处手动开的），运行中的会提示先停止
- Codex 回复**实时转发**到手机（自动去重，避免同一条输出刷屏；并行时按会话标注来源）
- Codex 给出 2–4 个候选项时自动转成**交互按钮**，点击后继续原会话
- 需要你补充信息时直接转发问题，你回复一句就继续同一会话
- `/new` 强制下一条消息新开会话；`/use` / `/focus <前8位>` 切换 / 接管会话
- `/monitor` 只读监控本机 Codex + 旧 Claude 会话，状态变化主动提醒
- 长任务每隔 `HEARTBEAT_SECONDS` 发一次心跳，完成/停止/出错都会通知
- 任务忙时新消息自动**排队**（`/queue` 查看/清空），跑完一个自动接下一个
- 托管会话与最近窗口记录自动持久化（`logs/state.json`），重启不丢
- 桌面截图（mss）与网页截图（Playwright）能力，跑项目命令提示
- 单实例保护：重复启动会被拒绝，不再出现多个 bot 抢同一个飞书长连接

## 快速开始（macOS）

```bash
cd ~/ai-coding-agent-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env   # 然后填写 .env
```

启动 / 停止 / 重启 / 查看状态：

```bash
scripts/run.sh
scripts/status.sh
scripts/stop.sh
scripts/restart.sh
```

`scripts/run.sh` 会以 nohup 后台运行，日志写入 `logs/bot.log`（运行输出 `logs/bot.out.log`）。如果机器上还残留着旧的多实例，先执行一次 `scripts/stop_all.sh` 清理。

开机自启 / 崩溃自动重启（macOS launchd，推荐）：

```bash
scripts/install_launchd.sh      # 注册并启动服务
scripts/uninstall_launchd.sh    # 卸载服务
```

服务日志在 `logs/bot.out.log` / `logs/bot.err.log`；进程由 launchd 托管，完全脱离终端，重启电脑后自动拉起。

## 配置（.env）

| 变量 | 说明 |
|---|---|
| `BOT_CHANNEL` | `telegram` 或 `feishu`（推荐飞书，走长连接） |
| `TELEGRAM_TOKEN` / `ALLOWED_CHAT_ID` | Telegram 通道必填 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `ALLOWED_FEISHU_OPEN_ID` | 飞书通道必填 |
| `DEFAULT_PROJECT_DIR` | 默认工作目录 |
| `MONITORED_PROJECTS` | 逗号分隔的监控/可切换项目 |
| `PROJECTS_ROOTS` | 货架目录，一级子目录自动成为 `/project` 选项 |
| `CODEX_SANDBOX` | `workspace-write`（推荐）或 `read-only` |
| `CODEX_MODEL` | 可选，强制指定 Codex 模型；留空用 `~/.codex/config.toml` |
| `APPROVAL_TIMEOUT` / `APPROVAL_TIMEOUT_ACTION` | 等待选择超时与默认动作（`deny`/`allow`） |
| `HEARTBEAT_SECONDS` | 长任务心跳间隔，0 关闭 |
| `STATE_AUTOSAVE_SECONDS` | 会话/窗口记录自动保存间隔，0 关闭（默认 30s） |
| `MAX_PARALLEL_TASKS` | 同时并行运行的 Codex 会话数上限（默认 3，1=旧串行行为） |
| `CODEX_SESSIONS_DIR` / `SESSION_MONITOR_DIR` | 本机会话监控目录，一般不用改 |

完整字段见 `.env.example`。`.env` 已被 `.gitignore` 忽略，不会提交。

## 手机上的命令

| 命令 | 作用 |
|---|---|
| `/start` | 欢迎信息 + 快捷操作卡片 |
| `/help` | 命令速览 |
| `/status` | 当前项目/会话/任务状态 + 项目面板 + 监控总览 |
| `/monitor` | 本机 Codex/Claude 会话总览；`/monitor all` 看全部；`/monitor codex:<id>` 看详情 |
| `/sessions` | 托管会话列表 + **每个会话的操作卡片**（详情/焦点/停止/接管） |
| `@会话前8位 消息` | 直接指挥指定会话（不用切焦点）；`@会话前8位` 单独一条 = 仅切换焦点 |
| `/use <前8位>` / `/focus <前8位>` | 切换 / 接管会话（等价，均设为焦点会话） |
| `/new` | 下一条消息强制新开会话 |
| `/project` | 切换项目（支持简称或完整路径） |
| `/stop` | 停止焦点会话的任务；`/stop <前8位>` 停止指定会话；`/stop all` 全部停止并清空队列 |
| `/queue` | 查看任务队列；`/queue clear` 清空队列 |
| `/health` | 本机环境体检（Codex 可用性、监控目录、沙箱等） |

## 它是怎么工作的

```text
手机(Telegram/飞书) ──► bot.py ──► codex exec --sandbox workspace-write --json -C <项目> <任务>
        ▲                                   │
        │◄────────── 实时输出 / 按钮 / 等待你回复
        │                                   ▼
        └───────────── 你的回复 ──► codex exec resume <session_id> --json
```

关键点：

- 新会话：`codex exec --sandbox workspace-write --json -C <项目> <提示词>`
- 继续会话：`codex exec resume <session_id> --json <提示词>`（在项目目录内执行，通过信任目录检查；会话继承自己创建时的沙箱设置）
- **并行调度**：每个会话一个独立 asyncio 任务 + 一个 `codex` 子进程；总并行数受 `MAX_PARALLEL_TASKS` 限制；任务忙时消息进入全局 FIFO 队列，但只有当该会话当前空闲且有空闲并行槽时才会被拉起（保证同一会话内的先后顺序）
- **会话寻址**：普通消息发给「焦点会话」（最近活动/手动指定的会话）；以 `@会话前8位` 开头的消息发给指定会话；每个会话可以同时处于运行 / 等待回复等不同状态
- 解析 JSONL 事件流（`thread.started` / `item.completed` / `turn.completed`），只把 agent 的回复转发出去，不转发工具输入和敏感载荷
- 检测到“问题 / 候选项”就进入等待状态，等你的回复或按钮选择（多个会话可同时等待）
- 会话/项目面板/最近窗口每 30 秒保存到 `logs/state.json`，重启后自动恢复（含各会话队列）

## 安全边界

- 白名单：只有 `ALLOWED_FEISHU_OPEN_ID` / `ALLOWED_CHAT_ID` 能指挥，其余一律拒绝
- 沙箱：Codex 默认在 `workspace-write` 下运行，项目内可读写，项目外只读
- 高风险操作（装依赖、删文件、push、联网等）由系统提示词要求 agent **先用自然语言问你**再动手；涉及沙箱外权限的命令仍会被沙箱拦截
- 接管外部 Codex 会话有保护：正在别处运行（终端手动开的）的会话会拒绝接管，提示先停止；旧 Claude 会话只读，不可用 Codex 继续
- 监控只读取会话元数据（ID/状态/最近活动），不读取、不转发 rollout 正文或密钥
- 飞书日志只记录 open_id / chat_id / 事件类型，不再打印原始事件全文

## 排障

- **提示“已有 bot.py 实例在运行”**：执行 `scripts/stop_all.sh` 清理旧实例，再 `scripts/run.sh`
- **任务报“Codex CLI 退出码”**：先 `codex --version` 确认 CLI 可用；再 `scripts/status.sh` 看日志 `logs/bot.out.log`
- **需要联网/安装依赖被沙箱拦截**：这是 `workspace-write` 的预期行为；可让 agent 给出命令，你在本机终端执行，或自行调整沙箱策略（不推荐 `danger-full-access`）
- **飞书收不到消息**：确认 `.env` 的 `ALLOWED_FEISHU_OPEN_ID` 与 `FEISHU_APP_ID/SECRET`，检查 `logs/bot.log` 是否有长连接错误

## 相关文档

- [远程架构部署（bot 常驻云端 ECS、codex 留在家里电脑）](REMOTE_DEPLOY.md)
- [飞书接入说明](FEISHU.md)
- [会话监控说明](MONITORING.md)
- [开发计划与历史](DEVELOPMENT_PLAN.md)

## 两种运行形态

- **本地形态（默认）**：bot 与 Codex 在同一台机器（家里电脑）上跑，`scripts/run.sh` 或 launchd 常驻。
- **远程形态**：bot 常驻云端 ECS（24/7 在线），Codex 留在家里电脑，两者经 SSH 反向隧道连接（`CODEX_REMOTE=1` + `CODEX_SSH_TARGET`）。你在外面随时指挥，家里电脑需开机且隧道在线。详见 [REMOTE_DEPLOY.md](REMOTE_DEPLOY.md)。
