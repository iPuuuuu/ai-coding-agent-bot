# AI Coding Agent 机器人

让家里的 Windows 电脑常驻一个 **Claude Code 镜像机器人**，你只用手机上的 **Telegram** 就能：

- 给电脑上的 Claude 下编程任务
- 尽量实时看到 Claude 在本机里的对话/状态输出
- 查看 `bot` 和 `doctor-wang` 两个项目的最近窗口内容
- 当 Claude 给出几个选项时，直接在手机上点按钮控制它继续
- 在需要你补充信息时，直接用手机回复继续同一个会话

> 这版实现的核心执行引擎是 **本机已安装的 Claude Code CLI (`claude`)**，不再依赖 `claude-agent-sdk` Python 包。

---

## 这版现在的目标

这版不是“摘要机器人”，而是尽量接近：

- **你在手机上看到的消息，接近电脑里 Claude Code 的窗口内容**
- **Claude 给出选项时，你可以直接在手机上选**

当前实现重点是：

1. **更完整地转发 Claude 输出**，不再只保留简短摘要。
2. **保留会话最近窗口记录**，让 `/status` 能看到最近几条真实事件。
3. **选项优先转成 Telegram 按钮**，点完后继续原来的 Claude 会话。

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
会返回：

- 当前操作项目
- 当前任务状态
- 两个项目的最近状态
- 当前项目最近几条“窗口内容”

也就是说，它不只是摘要，还会给你看最近几条 Claude 会话镜像。

### `/project`
支持快速切换：

```text
/project bot
```

```text
/project doctor-wang
```

也支持完整路径：

```text
/project C:/Users/wmh/Desktop/doctor-wang
```

### `/stop`
停止当前正在跑的任务。

---

## 当 Claude 给出选项时

如果 Claude 输出的是明确选项，比如：

- 1. 先查日志
- 2. 直接修配置
- 3. 先跑测试

机器人会尽量把它转成 **Telegram 按钮**。

你在手机上点按钮后：

- 这个选项会被当作你的回复
- 继续发送给**原来的 Claude 会话**
- 电脑上的 AI 会沿着那个选择继续跑

如果某个问题没法稳定解析成按钮，机器人会退回成：

- 把原问题转发给你
- 让你直接发文字回复

---

## 输出风格

这版默认更偏向“窗口镜像”：

- assistant 文本尽量原样转发
- result 文本也会转发
- 部分状态事件会简短显示
- 不再强行压成很短的摘要

所以你手机上看到的内容，会比上一版更长，但更接近电脑里的 Claude 窗口。

---

## 当前限制

- **同一时刻仍然只跑一个活跃任务**
- 会保留两个项目的状态和最近窗口内容
- 选项按钮优先处理 2~4 个清晰候选项
- 太复杂的开放问题仍然要你手动文字回复

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

### 3）为什么有时不是按钮，而是让我手动回复？
因为不是所有 Claude 提问都能稳定解析成结构化选项。

当前策略是：

- **能稳定识别选项** → 发按钮
- **识别不稳定** → 保留原文，让你直接文字回复
