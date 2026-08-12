# AI Coding Agent 机器人 — 开发计划

> 常驻家里 Win10 电脑上的 AI Coding Agent：通过 Telegram 接收指令，后台自主写代码、跑测试，遇到关键决策才主动询问你（不卡住），并能运行代码、截图发回给你查看。

**技术组合**：Telegram + Claude Agent SDK + Web/桌面截图

## 现状（2026-08 更新）

- 执行引擎已从 Claude 切换为**本机 Codex CLI**（`codex exec --json` / `codex exec resume`），运行平台为 macOS。
- 阶段 1–4 的核心能力已落地：收指令、实时输出转发、选项按钮、等待回复继续、`/new` `/use` `/project` `/status` `/stop` `/monitor`、日志落盘、截图与跑项目提示。
- 补充完善：Codex 0.147.0 命令兼容（移除已不存在的 `--ask-for-approval`）、单实例保护、长任务心跳、任务完成/停止/出错通知、`/health` 体检、`/use` 接管本机任意 Codex 会话、启动/停止/清理脚本。
- 主要取舍：保持 `workspace-write` 沙箱（项目内可写、项目外只读），高风险操作靠系统提示词先问用户；需要联网/装依赖的命令会被沙箱拦截，属于预期行为。
- 尚未实现：多项目并行/任务队列、定时任务、语音转文字、按 chat_id 完全隔离的多群并行。

---

## 1. 总体架构

```
┌─────────────┐   你发指令/审批    ┌──────────────────────────────────────┐
│  Telegram   │ ◀───────────────▶ │        家里的 Win10 电脑 (常驻)          │
│  手机 App    │   截图/日志/提问   │                                        │
└─────────────┘                   │  ┌────────────────────────────────┐   │
                                  │  │  bot.py  (Telegram Bridge)      │   │
                                  │  │  - 收指令 → 丢给 Agent          │   │
                                  │  │  - Agent 要审批 → 发按钮问你     │   │
                                  │  │  - 收结果/截图 → 发回给你        │   │
                                  │  └───────────────┬────────────────┘   │
                                  │                  │                     │
                                  │  ┌───────────────▼────────────────┐   │
                                  │  │  Claude Agent SDK (agent core)  │   │
                                  │  │  - can_use_tool 回调 = 审批路由  │   │
                                  │  │  - 自带 Read/Write/Bash/Grep... │   │
                                  │  │  - 自定义 MCP 工具: 截图/跑项目  │   │
                                  │  └───────────────┬────────────────┘   │
                                  │                  ▼                     │
                                  │        你的代码项目 / 终端 / 浏览器      │
                                  └──────────────────────────────────────┘
```

一句话：**Telegram Bridge 是"嘴和耳朵"，Claude Agent SDK 是"大脑和手"，`can_use_tool` 回调是把两者缝起来的关键钩子。**

## 2. 技术选型（都是 Windows 上好装的）

| 模块 | 选型 | 理由 |
|---|---|---|
| 语言 | **Python 3.11+** | SDK、Telegram 库、截图库都最成熟 |
| 消息渠道 | **python-telegram-bot** (v21+) | 异步、支持按钮(InlineKeyboard)、发图/发文件 |
| Agent 核心 | **claude-agent-sdk** | 原生 `can_use_tool` 权限回调 = 你要的"该问就问、不该问就自己干" |
| Web 截图 | **Playwright** | 自动开无头浏览器截项目页面 |
| 桌面截图 | **mss** (或 `Pillow.ImageGrab`) | 截整个桌面/窗口，看 GUI 程序、终端跑的结果 |
| 常驻 | **NSSM**（把脚本注册成 Windows 服务）或 任务计划开机自启 | 关机重启也能自动拉起 |
| 配置/密钥 | **.env** + `python-dotenv` | Telegram token、Anthropic key、白名单 chat_id |

## 3. ⭐ 核心设计：怎么做到"不卡住，但需要审核时主动问我"

这是整个项目的灵魂，靠 SDK 的 `can_use_tool` 回调 + 三档分级：

**分级策略（在回调里判断）：**

1. **绿灯 · 自动放行**（不打扰你）：`Read` / `Grep` / `Glob` / `Write`/`Edit`（仅限当前项目目录内）、跑测试、`git status/diff/add/commit`。
2. **黄灯 · 发 Telegram 问你 + 带超时默认**：`Bash` 里出现装依赖、删文件、访问网络、`git push`、改项目目录外的东西。→ 发一条带 `✅通过 / ❌拒绝` 按钮的消息给你。**设 5 分钟超时**：超时没回就按预设默认（建议默认"拒绝"），**这样即使你在忙 Agent 也不会永久卡死**，它会跳过这步继续做别的或告诉你被拦了。
3. **红灯 · 永远拒绝**：`rm -rf`、格式化磁盘、改系统目录、读密钥文件等。直接拒，连问都不问。

配合 `permission_mode="acceptEdits"`，让改代码这种高频操作默认不打扰，只有黄灯才走 Telegram。这样你从"每一步都要审"变成"只有关键决策才 ping 你一下"。

**另外**：当 Agent 本身遇到需要产品决策的问题（不是权限，是"这个功能你想要 A 还是 B"），它会直接把问题写在输出里，Bridge 检测到 Agent 停下等待，就把问题原样转发给你，你回一句它继续 —— 这靠 SDK 的多轮会话（`ClaudeSDKClient`）实现。

## 4. 分阶段开发计划（建议顺序）

### 阶段 0 · 环境准备（~0.5 天）
- [ ] 装 Python、`pip install claude-agent-sdk python-telegram-bot python-dotenv mss playwright`，`playwright install chromium`
- [ ] 找 **@BotFather** 建 Telegram bot，拿 token；给自己发条消息拿到你的 `chat_id`（用于白名单，防止别人指挥你的电脑）
- [ ] 配好 `ANTHROPIC_API_KEY`（或你的 Claude 订阅登录方式）
- [ ] 跑通 SDK 最小示例：`query("列出当前目录文件")` 能返回

### 阶段 1 · MVP：能收指令、能回话（~1 天）
- [ ] `bot.py`：Telegram 收到文字 → 调 `ClaudeSDKClient` → 把 Agent 的文字输出流式发回 Telegram
- [ ] 加白名单（只有你的 chat_id 能用）
- [ ] `permission_mode="acceptEdits"`，先让它能在一个测试项目里写代码、跑测试
- ✅ **里程碑**：手机发"写个 Python 快排并测试"，它能写完、跑测试、把结果发回来

### 阶段 2 · 审批不卡住（~1 天）— 最关键
- [ ] 实现 `can_use_tool` 三档分级
- [ ] 黄灯 → 发 InlineKeyboard 按钮消息，用 `asyncio.Future` 挂起等你点，点了 resolve
- [ ] 加超时默认，防永久卡死
- ✅ **里程碑**：让它做个需要装依赖的任务，手机上弹出"是否允许 pip install requests？✅/❌"，你点一下它继续

### 阶段 3 · 运行 + 截图（~1 天）
- [ ] 自定义 MCP 工具 `take_desktop_screenshot()`（mss 截屏 → 存 png）
- [ ] 自定义 MCP 工具 `screenshot_web(url)`（Playwright 打开页面截图）
- [ ] Bridge 检测到 Agent 产出了图片文件 → `send_photo` 发给你
- [ ] `/run` 类指令：让它跑项目并截图给你看效果
- ✅ **里程碑**：发"把这个网页项目跑起来截图给我看"，收到实际渲染截图

### 阶段 4 · 常驻 + 好用（~0.5 天）
- [ ] 用 **NSSM** 注册成 Windows 服务，开机自启、崩溃自动重启
- [ ] 加几个便捷命令：`/project <路径>` 切换工作目录、`/stop` 中止当前任务、`/status` 看它在干嘛
- [ ] 日志落盘（出错能回溯）
- ✅ **里程碑**：重启电脑后不用管，手机随时能指挥

### 阶段 5 · 可选增强（想到再加）
- 长任务进度心跳（每 N 秒发个"还在跑第 3 步…"）
- 多项目并行 / 任务队列
- 定时任务（如"每晚跑一次回归测试"）
- 语音转文字下指令

## 5. 建议目录结构

```
bot/
├── .env                    # TELEGRAM_TOKEN / ANTHROPIC_API_KEY / ALLOWED_CHAT_ID
├── bot.py                  # 入口：Telegram Bridge
├── agent_runner.py         # 封装 ClaudeSDKClient 会话
├── permissions.py          # can_use_tool 三档分级逻辑
├── tools/
│   ├── screenshot.py       # 桌面/Web 截图 MCP 工具
│   └── run_project.py      # 跑项目相关
├── config.py               # 白名单、超时、绿/黄/红名单规则
└── logs/
```

## 6. 关键代码骨架（真实 SDK 接口，落地前对一下版本）

**审批回调（`permissions.py`）——灵魂所在：**
```python
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

RED = {"rm -rf", "format", "shutdown"}          # 关键词命中即拒
async def can_use_tool(tool_name, tool_input, context):
    # 绿灯：只读 + 项目内编辑，自动放行
    if tool_name in ("Read", "Grep", "Glob", "Edit", "Write"):
        return PermissionResultAllow()
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if any(k in cmd for k in RED):
            return PermissionResultDeny(message="高危命令，已拦截")
        if is_risky(cmd):                        # 装依赖/删/push/联网
            ok = await ask_telegram(f"允许执行?\n`{cmd}`", timeout=300)  # 带按钮+超时
            return PermissionResultAllow() if ok else PermissionResultDeny(message="你拒绝了")
    return PermissionResultAllow()
```

**Agent 会话（`agent_runner.py`）：**
```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

options = ClaudeAgentOptions(
    permission_mode="acceptEdits",
    can_use_tool=can_use_tool,
    cwd=current_project_dir,
    mcp_servers={"tools": screenshot_server},   # 自定义截图工具
)
async with ClaudeSDKClient(options=options) as client:
    await client.query(user_message)
    async for msg in client.receive_response():
        await forward_to_telegram(msg)          # 文字流式转发；遇图片发 send_photo
```

**截图工具（`tools/screenshot.py`）：**
```python
from claude_agent_sdk import tool, create_sdk_mcp_server
import mss

@tool("take_desktop_screenshot", "截取当前桌面发给用户", {})
async def desktop_shot(args):
    with mss.mss() as sct:
        path = "logs/shot.png"; sct.shot(output=path)
    return {"content": [{"type": "text", "text": f"已截图: {path}"}]}

screenshot_server = create_sdk_mcp_server(name="tools", tools=[desktop_shot])
```

> ⚠️ 符号名（`PermissionResultAllow`、`create_sdk_mcp_server`、`receive_response` 等）请以你装到的 `claude-agent-sdk` 实际版本为准，用 `python -c "import claude_agent_sdk; print(dir(claude_agent_sdk))"` 核对。

## 7. 安全（自用也要做的最低限度）
- **白名单 chat_id**：只有你能指挥，否则谁知道你 bot 就能远程操控你电脑。
- **红名单硬拦截** + **黄名单人工审批**，别图省事全开 `bypassPermissions`。
- **限定工作目录**：Agent 默认只在指定项目目录活动，别让它满 C 盘乱跑。
- **密钥放 .env**，别让 Agent 读到自己的 key。

## 8. 成本
- Telegram / Playwright / mss / NSSM 全免费。
- 主要成本 = **Claude API 用量**（或你的 Claude 订阅）。自用、按需触发，通常很低。

---

## 建议：从最小闭环开始

先把**阶段 1（收指令能回话）+ 阶段 2（审批不卡住）** 打通，你就已经拥有一个"手机遥控、关键处才问你"的可用机器人了，截图和常驻是锦上添花。
