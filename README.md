# AI Coding Agent 机器人

让家里的 Windows 电脑常驻一个 **AI 编程监督机器人**，你只用手机上的 **Telegram** 就能：

- 给 AI 下编程任务
- 同时查看 `bot` 和 `doctor-wang` 两个项目的最近运行状态
- 在关键风险操作前收到同步提醒
- 在 AI 需要你回复/拍板时第一时间收到消息
- 让它运行项目并把截图发回手机

> 这版实现的核心执行引擎是 **本机已安装的 Claude Code CLI (`claude`)**，不再依赖 `claude-agent-sdk` Python 包。

---

## 这版现在的交互重点

这版机器人重点做了三件事：

1. **回复更短**：尽量只发简短进度和一条最终结论，减少 Telegram 噪音。
2. **双项目可见**：默认同时记录：
   - `C:/Users/wmh/Desktop/bot`
   - `C:/Users/wmh/Desktop/doctor-wang`
3. **需要你操作时立即提醒**：当 AI 需要你补充信息、做选择，或准备做高风险操作时，会主动发消息提醒你。

---

## 依赖前提

### 1）Claude Code CLI
命令行里需要能运行：

```bash
claude --version
```

### 2）Python 依赖

```bash
cd C:/Users/wmh/Desktop/bot
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium
```

---

## 配置 `.env`

复制：

```bash
cp .env.example .env
```

至少填写：

- `TELEGRAM_TOKEN`
- `ALLOWED_CHAT_ID`
- `DEFAULT_PROJECT_DIR`
- `MONITORED_PROJECTS`

示例：

```env
DEFAULT_PROJECT_DIR=C:/Users/wmh/Desktop/bot
MONITORED_PROJECTS=C:/Users/wmh/Desktop/bot,C:/Users/wmh/Desktop/doctor-wang
```

> 这一版优先使用本机 Claude CLI，所以 `.env` 里不再把 `ANTHROPIC_API_KEY` 视为必须项。

---

## 启动

```bash
py -3.11 bot.py
```

然后在手机 Telegram 里给 bot 发：

```text
/start
```

---

## 手机上怎么用

### 直接发任务
直接发一句话，就是给**当前项目**下任务，例如：

```text
查看当前进度
```

```text
修复测试失败
```

```text
把项目跑起来截图给我看
```

### `/status`
会返回一个简短面板，显示：

- 当前操作项目
- 当前任务状态
- `bot` 项目最近状态
- `doctor-wang` 项目最近状态
- 哪个项目正在运行 / 等待你回复 / 最近出错

### `/project`
支持快速切换：

```text
/project bot
```

```text
/project doctor-wang
```

也支持直接传完整路径：

```text
/project C:/Users/wmh/Desktop/doctor-wang
```

### `/stop`
停止当前正在跑的任务。

---

## 当 AI 需要你操作时

这版会尽量把“需要你现在处理”的情况单独提醒出来，而不是混在普通进度里。

常见情况：

- 需要你补充信息
- 需要你做方案选择
- 准备安装依赖
- 准备联网下载
- 准备删除文件
- 准备 `git push`

你看到提醒后：

- 如果是追问，直接回复文字即可继续
- 如果后续接入按钮审批，就点 ✅/❌ 即可

---

## 输出风格

这版机器人默认会：

- 少发中间噪音
- 避免重复发同一结论
- 优先发简短最终答复
- 在需要你操作时立即单独提醒

所以你在 Telegram 里看到的消息，应该比之前更短、更清楚。

---

## 常见问题

### 1）`claude` 命令不可用
先检查：

```bash
claude --version
```

### 2）Telegram 没反应
检查：

- `py -3.11 bot.py` 是否还在运行
- `.env` 的 `TELEGRAM_TOKEN` 是否正确
- `ALLOWED_CHAT_ID` 是否正确

### 3）只想看两个项目状态，不想并发跑两个任务
当前实现就是这样：

- **同一时刻只跑一个任务**
- 但会**同时保留两个项目的最近状态快照**

这样更稳，也更容易在手机上盯。
