# AI Coding Agent 机器人

让家里的 Windows 电脑常驻一个 **AI 编程监督机器人**，你只用手机上的 **Telegram** 就能：

- 给 AI 下编程任务
- 看它的阶段性进度反馈
- 在关键风险操作前看到它先来问你
- 按你的回复继续任务
- 让它运行项目并把截图发回手机

> 这版实现的核心执行引擎是 **本机已安装的 Claude Code CLI (`claude`)**，不再依赖 `claude-agent-sdk` Python 包。

---

## 这版为什么改成 Claude CLI

原来的方案依赖 `claude-agent-sdk`，但这台机器上安装它及其依赖链时会被 pip/网络环境反复卡住。
为了先把“电脑常驻 + 手机 Telegram 监督 AI 写代码”这件事跑起来，这版改成：

- **Telegram 负责远程控制和监督**
- **本机 Claude Code CLI 负责真正执行 Claude 任务**
- **Python 只负责桥接、状态管理、截图和消息回传**

这样你不需要先把 `claude-agent-sdk` 装通，也能继续推进项目。

---

## 现在怎么交互

机器人运行在你的电脑上，你通过 Telegram 和它对话。

### 日常使用方式

1. **先在电脑上启动 bot**
2. **手机 Telegram 给 bot 发消息**
3. 它会：
   - 调用本机 Claude Code CLI 处理任务
   - 把进度和结果发回 Telegram
   - 如果需要你补充信息，会明确问你一句
   - 如果任务和“运行/截图”有关，会尽量辅助启动并截图

### 你和它的交互规则

- **直接发普通文字** = 给 Claude 的任务
- **发 `/project 路径`** = 切换当前工作项目
- **发 `/status`** = 看当前它在做什么
- **发 `/stop`** = 停止当前任务
- **它问你问题时** = 你直接回复下一条消息即可继续

---

## 依赖前提

你这台电脑上需要两类东西：

### 1）Claude Code CLI
也就是命令行里能直接运行：

```bash
claude --version
```

如果这条命令能输出版本号，就说明 Claude CLI 已可用。

### 2）Python 依赖（只负责 Telegram 桥接和截图）

```bash
cd C:/Users/wmh/Desktop/bot
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium
```

---

## 你只有手机 Telegram 时怎么配置

### 1）创建 Telegram Bot
在手机 Telegram 里找到 **@BotFather**：

- 发送 `/newbot`
- 按提示创建一个 bot
- 拿到 token

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

在返回 JSON 里找：

```json
"chat": {"id": 123456789, ...}
```

这个数字就是你的 `ALLOWED_CHAT_ID`。

### 4）复制 `.env.example` 为 `.env`

```bash
cp .env.example .env
```

至少填这几个：

- `TELEGRAM_TOKEN`
- `ALLOWED_CHAT_ID`
- `DEFAULT_PROJECT_DIR`

> 这一版先优先使用本机 Claude CLI，所以 `.env` 里不再把 `ANTHROPIC_API_KEY` 视为必须项。

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

### `/start`
会告诉你：
- 当前项目目录
- 当前状态
- 怎么发第一个任务
- 怎么切项目
- 怎么停止任务

### `/project 路径`
例如：

```text
/project C:/Users/wmh/Desktop/myapp
```

### 直接发任务
例如：

```text
帮我分析一下这个项目怎么启动
```

```text
修复测试失败并告诉我改了什么
```

```text
把项目跑起来截图给我看
```

### `/status`
会返回：
- 当前项目目录
- 当前状态（idle / running / waiting_user_reply）
- 当前任务编号
- 已运行多久
- 最近事件
- 最近错误

### `/stop`
停止当前任务。

### 当它问你问题时
你直接回复一句话就行，例如：

```text
先修测试
```

---

## 风险操作说明

由于这版不再使用 `claude-agent-sdk` 的 Python 工具回调，风险控制方式改成：

- 在系统提示里明确要求 Claude：
  - 遇到装依赖、联网下载、删除文件、`git push` 等高风险操作时，先明确问你
- 你在 Telegram 回答后，它再继续

这意味着：
- 这版依然保留“先问你再继续”的监督模型
- 但它不再依赖 SDK 的 `can_use_tool` 回调机制

---

## 截图与运行项目

如果你发：

```text
把项目跑起来截图给我看
```

机器人会尽量：

1. 调用 Claude 分析如何运行项目
2. Python 侧尝试给出常见启动命令提示
3. 如果任务里明确涉及截图，会尝试桌面截图或网页截图

目前对以下类型更友好：

- Node.js 项目（`package.json`）
- Python 项目（`main.py` / `app.py` / `manage.py`）

---

## 常见问题

### 1）`claude` 命令不可用
先检查：

```bash
claude --version
```

如果这里都不通，这版 bot 也无法调用 Claude。

### 2）Telegram 没反应
检查：
- 电脑上的 `py -3.11 bot.py` 是否还在运行
- `.env` 的 `TELEGRAM_TOKEN` 是否正确
- `ALLOWED_CHAT_ID` 是否填成了你自己的 chat_id

### 3）网页截图失败
先确保装过 Playwright 浏览器：

```bash
py -3.11 -m playwright install chromium
```

### 4）桌面截图失败
桌面截图依赖当前 Windows 会话可见；网页截图通常更稳。

---

## 建议的使用顺序

第一次上手建议按这个顺序：

1. `/start`
2. `/project C:/你的项目路径`
3. `帮我分析一下这个项目结构`
4. `告诉我这个项目怎么启动`
5. `把项目跑起来截图给我看`
6. `修复测试失败并汇报改动`
