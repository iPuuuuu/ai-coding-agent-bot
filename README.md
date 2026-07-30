# AI Coding Agent 机器人

让家里的 Windows 电脑常驻一个 **AI 编程监督机器人**，你只用手机上的 **Telegram** 就能：

- 给 AI 下编程任务
- 看它的阶段性进度反馈
- 在关键风险操作时点 ✅/❌ 审批
- 按你的回复继续任务
- 让它运行项目并把截图发回手机

> 适合你的使用方式：**电脑上跑这个机器人，你人在外面只拿手机 Telegram 远程监督 AI 写代码。**

---

## 这个项目现在能怎么交互

机器人运行在你的电脑上，你通过 Telegram 和它对话。

### 日常使用方式

1. **先在电脑上启动 bot**
2. **手机 Telegram 给 bot 发消息**
3. 它会：
   - 理解你的任务
   - 调用 Claude Agent 在当前项目目录里读写代码 / 跑测试
   - 持续给你发进度
   - 遇到装依赖、删文件、联网下载、`git push` 之类风险操作时，弹按钮问你
   - 需要你补充信息时，直接问你一句，你回它就继续

### 你和它的交互规则

- **直接发普通文字** = 给 AI 的任务
- **发 `/project 路径`** = 切换当前工作项目
- **发 `/status`** = 看当前它在做什么
- **发 `/stop`** = 停止当前任务
- **它问你问题时** = 你直接回复下一条消息即可继续
- **它弹审批按钮时** = 你点 ✅/❌ 决定是否继续该风险操作

---

## 它适合做什么

比如你在 Telegram 里发：

- `帮我看看这个项目怎么启动`
- `修复测试失败并告诉我改了什么`
- `把项目跑起来截图给我看`
- `分析这个仓库结构，给我一个开发建议`
- `给这个 Python 项目加一个接口并测试`

---

## 目录结构

```text
bot/
├── bot.py               # 入口：Telegram 桥接 + 任务状态管理
├── agent_runner.py      # Claude Agent SDK 对话封装
├── permissions.py       # can_use_tool 三档分级（绿/黄/红）
├── channel.py           # Telegram 发消息/发图/审批按钮
├── config.py            # 配置 + 风险关键词规则
├── tools/
│   ├── screenshot.py    # 桌面/网页截图 MCP 工具
│   └── run_project.py   # 常见项目启动提示辅助
├── .env.example         # 配置模板（复制成 .env 填真实值）
├── requirements.txt
├── README.md
└── DEVELOPMENT_PLAN.md
```

---

## 安装（本机用 Python 3.11）

本机默认 `python` 可能是老的 Anaconda 3.6，所以统一用 **`py -3.11`**：

```bash
cd C:/Users/wmh/Desktop/bot
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium
```

如果你看到 `No module named claude_agent_sdk`，说明依赖还没装好；先重新执行上面的安装命令。

---

## 第一次配置：你只有手机 Telegram 时怎么做

### 1）创建 Telegram Bot

在手机 Telegram 里找到 **@BotFather**：

- 发送 `/newbot`
- 按提示创建一个 bot
- 拿到一个 token，长得像：

```text
123456:ABC-xxxxxxxxxxxxxxxxxxxx
```

### 2）先给你的 bot 发一条消息

在手机里找到你刚创建的 bot，发任意一句话，比如：

```text
hi
```

### 3）在电脑上查你的 chat_id

在电脑浏览器打开：

```text
https://api.telegram.org/bot<你的TOKEN>/getUpdates
```

把 `<你的TOKEN>` 替换成 BotFather 给你的 token。

返回 JSON 里找：

```json
"chat": {"id": 123456789, ...}
```

这个数字就是你的 `ALLOWED_CHAT_ID`。

### 4）复制 `.env.example` 为 `.env`

```bash
cp .env.example .env
```

然后编辑 `.env`，至少填这几个：

- `TELEGRAM_TOKEN`
- `ALLOWED_CHAT_ID`
- `DEFAULT_PROJECT_DIR`
- `ANTHROPIC_API_KEY`（如果你走 API key 模式）

---

## `.env` 配置说明

参考 `.env.example`。

最关键的是：

- `TELEGRAM_TOKEN`：你的 Telegram bot token
- `ALLOWED_CHAT_ID`：只允许这个 chat_id 控制机器人
- `DEFAULT_PROJECT_DIR`：默认工作目录
- `ANTHROPIC_API_KEY`：Claude API key
- `APPROVAL_TIMEOUT`：审批超时时间（秒）
- `APPROVAL_TIMEOUT_ACTION`：超时默认 `deny` 或 `allow`

---

## 启动

```bash
py -3.11 bot.py
```

看到类似提示：

```text
🤖 机器人已启动，去 Telegram 给它发消息吧。Ctrl+C 退出。
```

然后你在手机 Telegram 里给它发：

```text
/start
```

---

## 手机上怎么用

### 1. `/start`

会告诉你：

- 当前项目目录
- 当前状态
- 怎么发第一个任务
- 怎么切项目
- 怎么停止任务

### 2. `/project 路径`

例如：

```text
/project C:/Users/wmh/Desktop/myapp
```

切换后，后续任务都默认在这个目录执行。

### 3. 直接发任务

例如：

```text
帮我分析一下这个项目怎么启动
```

或：

```text
修复测试失败并告诉我改了什么
```

或：

```text
把项目跑起来截图给我看
```

### 4. `/status`

会返回：

- 当前项目目录
- 当前状态（idle / running / waiting_user_reply）
- 当前任务编号
- 已运行多久
- 最近事件
- 是否在等你回复
- 最近错误

### 5. `/stop`

停止当前任务。适合：

- 它跑太久
- 方向不对
- 你要换任务

### 6. 当它问你问题时

例如它发：

```text
我需要你确认：你是想先修测试，还是先把项目跑起来？
```

你**直接回复一句话**就行，例如：

```text
先修测试
```

它会继续当前任务，而不是新开一个任务。

### 7. 当它弹审批按钮时

比如装依赖、删文件、联网下载、`git push` 等风险操作，它会给你发：

- ✅ 通过
- ❌ 拒绝

你点一下，它就继续或跳过。

---

## 审批规则

### 自动放行（绿灯）

通常包括：

- 读文件
- 搜索代码
- 项目内写代码
- 普通测试命令

### 需要审批（黄灯）

通常包括：

- `pip install`
- `npm install`
- `git push`
- `curl` / `wget`
- 删除 / 移动文件
- 可能影响系统或联网的命令

### 直接拒绝（红灯）

例如：

- `rm -rf /`
- 格式化
- 系统级高危命令

---

## 运行项目和截图

如果你发：

```text
把项目跑起来截图给我看
```

机器人会尽量：

1. 识别项目类型
2. 尝试启动项目
3. 如果是网页，优先用浏览器截图
4. 如果是桌面程序，尝试桌面截图
5. 把截图直接发回 Telegram

目前对以下类型更友好：

- Node.js 项目（`package.json`）
- Python 项目（`main.py` / `app.py` / `manage.py`）

如果自动识别不了，它会反问你该怎么启动。

---

## 常见问题

### 1）Telegram 没反应

检查：

- 电脑上的 `py -3.11 bot.py` 是否还在运行
- `.env` 的 `TELEGRAM_TOKEN` 是否正确
- `ALLOWED_CHAT_ID` 是否填成了你自己的 chat_id
- 你是不是在用另一个 Telegram 账号发消息

### 2）提示缺少配置

说明 `.env` 没填完整。

至少要有：

- `TELEGRAM_TOKEN`
- `ALLOWED_CHAT_ID`

### 3）提示 `No module named claude_agent_sdk`

说明依赖没装好：

```bash
py -3.11 -m pip install -r requirements.txt
```

### 4）网页截图失败

先确保装过 Playwright 浏览器：

```bash
py -3.11 -m playwright install chromium
```

### 5）桌面截图失败

桌面截图依赖当前 Windows 会话可见；如果电脑处于特殊后台会话，网页截图通常更稳。

---

## 建议的使用顺序

第一次上手建议按这个顺序：

1. `/start`
2. `/project C:/你的项目路径`
3. `帮我分析一下这个项目结构`
4. `告诉我这个项目怎么启动`
5. `把项目跑起来截图给我看`
6. `修复测试失败并汇报改动`

---

## 开机自启（可选）

如果你想让这台电脑重启后也自动上线，可以用 [NSSM](https://nssm.cc/) 把它注册成 Windows 服务：

```bash
nssm install AiCodingBot "C:\Users\wmh\AppData\Local\Programs\Python\Python311\python.exe" "C:\Users\wmh\Desktop\bot\bot.py"
nssm set AiCodingBot AppDirectory "C:\Users\wmh\Desktop\bot"
nssm start AiCodingBot
```

> 注意：如果你非常依赖“桌面截图”，服务模式下的可见桌面能力可能受限；网页截图通常更稳。

---

## 重要提示

- 这是**远程监督 AI 写代码**的工具，不是完全无人监管的自动化系统。
- 高风险操作默认会问你，避免它在你看不到的情况下乱动。
- `.env` 已被 `.gitignore` 忽略，不会误传到 git。
- 如果后续你想进一步强化，可以继续加：任务队列、日志查询、自动重启、更多审批按钮、更多项目类型识别。
