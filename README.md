# AI Coding Agent 机器人

常驻家里 Win10 电脑、通过 **Telegram** 指挥的 AI 编程助手：后台自主写代码、跑测试，
遇到关键操作才主动发消息问你（**不卡住**），还能运行代码、截图发回给你看。

> 详细设计见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)。
> AI coding 通用踩坑记录见 [AI_CODING_NOTES.md](AI_CODING_NOTES.md)。

## 它能做什么

- 手机发一句"写个 xxx 并测试"，它自己写完、跑测试、把结果发回来
- 装依赖 / 删文件 / `git push` 等**黄灯操作**会弹 ✅/❌ 按钮问你，你点一下才继续（超时不回有安全兜底，**不会永久卡死**）
- `rm -rf /` 等**红灯操作**直接拒绝
- 能截**桌面**或**网页**发给你看效果

## 目录结构

```
bot/
├── bot.py            # 入口：Telegram 桥接
├── agent_runner.py   # 封装 Claude Agent SDK 一次对话
├── permissions.py    # ⭐ can_use_tool 三档分级（绿/黄/红）
├── channel.py        # Telegram 发消息/发图/审批按钮
├── config.py         # 配置 + 风险关键词规则
├── tools/
│   └── screenshot.py # 桌面/网页截图 MCP 工具
├── .env.example      # 配置模板（复制成 .env 填真实值）
└── requirements.txt
```

## 安装（本机用 Python 3.11）

本机默认 `python` 是老的 Anaconda 3.6，SDK 需要 3.10+，所以统一用 `py -3.11`：

```bash
cd C:/Users/wmh/Desktop/bot
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium      # 网页截图需要
```

## 配置

1. 找 Telegram 的 **@BotFather** 创建一个 bot，拿到 token
2. 给你的 bot 随便发条消息，然后浏览器打开
   `https://api.telegram.org/bot<你的TOKEN>/getUpdates`，在返回里找 `chat.id`
3. 复制 `.env.example` 为 `.env`，填入 `TELEGRAM_TOKEN`、`ALLOWED_CHAT_ID`、`ANTHROPIC_API_KEY`

```bash
cp .env.example .env   # 然后编辑 .env
```

## 运行

```bash
py -3.11 bot.py
```

然后去 Telegram 给你的 bot 发 `/start`。

## 开机自启（可选，阶段 4）

用 [NSSM](https://nssm.cc/) 把它注册成 Windows 服务，关机重启自动拉起：

```bash
nssm install AiCodingBot "C:\Users\wmh\AppData\Local\Programs\Python\Python311\python.exe" "C:\Users\wmh\Desktop\bot\bot.py"
nssm set AiCodingBot AppDirectory "C:\Users\wmh\Desktop\bot"
nssm start AiCodingBot
```

## ⚠️ 重要提示

- **SDK 接口可能随版本变化**：本项目按 `claude-agent-sdk` 的已知接口编写。若启动时 import 报错，
  运行 `py -3.11 -c "import claude_agent_sdk; print(dir(claude_agent_sdk))"` 核对真实符号名，
  按报错微调 `permissions.py` / `agent_runner.py` 顶部的 import（核心逻辑不用改）。
- **安全**：只有 `.env` 里白名单的 chat_id 能指挥；红/黄名单在 `config.py` 里可自行增删。
- **`.env` 已被 `.gitignore` 忽略**，不会误传到 GitHub。
