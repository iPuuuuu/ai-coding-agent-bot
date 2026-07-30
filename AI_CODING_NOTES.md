# AI Coding 通用问题记录

> 这份文档记录用 AI（Claude / Codex 等）做编程时反复遇到的通用坑和最佳实践，
> **供以后的 AI agent 和我自己参考**。遇到新坑就往里加。
>
> 维护约定：按主题分节；每条写清「现象 → 原因 → 正确做法」。

---

## 🔑 密钥 / Token 安全（最高优先级）

**现象**：为了让 agent 帮忙推 GitHub / 调用某服务，直接把 token 明文贴进对话或命令行。

**原因**：
- 对话记录、shell 历史、日志都会留存明文 token
- 很多人图省事给 token **勾选全部权限**，一旦泄露等于整个账号被接管

**正确做法**：
1. **能自己推就自己推**：让 agent 把 `git push` 命令给你，你在本地终端执行，token 不经过 agent。
2. **必须给 agent 用时**：
   - 只勾**最小权限**（推代码只需 `repo`，不要 all scopes）
   - 用**短期/一次性** token，**用完立刻去 GitHub 设置页 revoke**
3. **永远不要把密钥写进会提交的文件**。放 `.env`，并在**第一次 commit 前**就把 `.env` 写进 `.gitignore`。
4. 推送后检查 git remote URL 里**没有内嵌 token**（`git remote get-url origin`），有就用
   `git remote set-url origin <干净地址>` 清掉。

> 本项目实例：初次配置时 token 曾被明文贴出且勾了全部权限 → 事后应立即 revoke 重建。

---

## 🪟 Windows + Git Bash 环境坑

- **`gh` CLI 常常没装**：想建 GitHub 仓库时 `gh` 不存在。替代方案：用 `curl` 调 GitHub REST API
  （`POST https://api.github.com/user/repos`，带 `Authorization: token <TOKEN>`）。
- **curl 传 JSON 时引号被 shell 吃掉**：Windows 的 bash 对 `-d '{...}'` 里的引号处理不稳，
  经常报 `Problems parsing JSON`。→ 把 JSON 写进文件再用 `curl -d @payload.json`。
- **路径用正斜杠**：`C:/Users/xxx` 而不是 `C:\Users\xxx`，且 `cd` 目标加引号。
- **`/dev/null` 不是 `NUL`**：在 git bash 里用 Unix 写法（`2>/dev/null`）。
- **LF/CRLF 警告**：`git add` 时的 `LF will be replaced by CRLF` 只是提醒，不影响提交。

---

## 🐍 Python 环境坑（本机特有）

- **默认 `python` 是老的 Anaconda 3.6**，很多新库（如 `claude-agent-sdk` 需要 3.10+）装不了。
- 本机有 **Python 3.11**，统一用 **`py -3.11`** 调用（`py -0p` 可列出所有解释器）。
- 装依赖也要指定解释器：`py -3.11 -m pip install ...`，别用裸 `pip`（可能指向 3.6）。

---

## 🤖 用 SDK / 库时的通用纪律

- **不要凭记忆写库的接口**：SDK（尤其 `claude-agent-sdk`、agent 相关）迭代快，类名/参数经常变。
  落地前用 `pip show <包>` 看版本，用 `python -c "import x; print(dir(x))"` 或看 docstring 核对真实符号，
  再写调用代码。
- **import 报错先核对符号名**，别急着改逻辑——通常只是类名/函数名换了。
- 写完 Python 先 `py -3.11 -m py_compile <files>` 做语法自检，成本极低。

---

## 🛡️ 让 agent「自主但不失控」的模式

- 用权限回调（如 Claude Agent SDK 的 `can_use_tool`）做**三档分级**：
  - 🟢 只读 / 项目内编辑 → 自动放行
  - 🟡 装依赖 / 删文件 / push / 联网 → 发消息问人，**带超时兜底**（超时按默认 deny，避免永久卡死）
  - 🔴 `rm -rf /`、格式化等 → 直接拒
- 关键：**超时一定要有默认动作**，否则「等人审批」会把自动化永久卡住。

---

## 📌 待补充

- （遇到新坑往这里加）
