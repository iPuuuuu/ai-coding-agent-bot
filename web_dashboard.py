"""
web_dashboard.py —— 只读 Web 看板：在浏览器/手机上查看家里电脑的会话状态。

零第三方依赖（Python 标准库 http.server），随 bot 进程启动：
- GET /                单页 HTML（深色主题、手机自适应、5 秒自动刷新）
- GET /api/overview    总览 JSON（会话、队列、等待、并行度）
- GET /api/session?id= 单个会话详情 JSON

鉴权：请求需带 ?token= 或 X-Dashboard-Token 头；未配置 token 时只监听 127.0.0.1。
会话扫描结果由 bot 的轮询循环写入 scan_cache，避免看板重复触发远端 SSH。
"""
from __future__ import annotations

import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

# bot 轮询循环写入的远端会话扫描缓存（source/session 记录列表）。
_scan_cache: list[dict] = []
_scan_cache_at = 0.0
_scan_lock = threading.Lock()


def set_scan_cache(records: list[dict]) -> None:
    global _scan_cache, _scan_cache_at
    with _scan_lock:
        _scan_cache = records
        _scan_cache_at = time.time()


def get_scan_cache() -> tuple[list[dict], float]:
    with _scan_lock:
        return list(_scan_cache), _scan_cache_at


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    token = config.DASHBOARD_TOKEN
    if not token:
        # 未配置 token：只允许本机访问（绑定 127.0.0.1 时有效）。
        return handler.client_address[0] in {"127.0.0.1", "::1"}
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    provided = (query.get("token") or [""])[0] or handler.headers.get("X-Dashboard-Token", "")
    return hmac.compare_digest(provided, token)


def _overview_payload() -> dict:
    import bot

    scan, scan_at = get_scan_cache()
    state = bot._state
    running_keys = {k for k, t in bot._running_tasks.items() if not t.done()}
    records: list[dict] = []
    seen: set[str] = set()
    # 远端扫描（含本 bot 托管会话）+ 本机托管会话并集
    for item in scan:
        sid = str(item.get("session_id", ""))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        status = str(item.get("status", "unknown"))
        if sid in state.sessions:
            managed = state.sessions[sid]
            if sid in bot._state.pending_by_session:
                status = "waiting_user_reply"
            elif sid in running_keys:
                status = "running"
            last_event = managed.last_event or str(item.get("last_event", ""))
        else:
            last_event = str(item.get("last_event", ""))
        records.append({
            "source": str(item.get("source", "codex")),
            "session_id": sid,
            "short_id": sid[:8],
            "project": bot._project_label(str(item.get("cwd", ""))),
            "cwd": str(item.get("cwd", "")),
            "status": status,
            "last_event": last_event[:160],
            "activity_age_seconds": int(item.get("activity_age_seconds", 0) or 0),
            "managed": sid in state.sessions,
            "focus": sid == state.current_session_id,
            "waiting": sid in bot._state.pending_by_session,
            "last_prompt": (state.sessions[sid].last_prompt[:200] if sid in state.sessions else ""),
        })
    for sid, managed in state.sessions.items():
        if sid in seen:
            continue
        seen.add(sid)
        if sid in bot._state.pending_by_session:
            status = "waiting_user_reply"
        elif sid in running_keys:
            status = "running"
        else:
            status = managed.mode or "idle"
        records.append({
            "source": "codex",
            "session_id": sid,
            "short_id": sid[:8],
            "project": managed.project_label,
            "cwd": managed.project_path,
            "status": status,
            "last_event": (managed.last_event or "")[:160],
            "activity_age_seconds": int(max(0.0, time.time() - (managed.updated_at or 0))),
            "managed": True,
            "focus": sid == state.current_session_id,
            "waiting": sid in bot._state.pending_by_session,
            "last_prompt": managed.last_prompt[:200],
        })
    records.sort(key=lambda r: (r["status"] in {"running", "waiting_user_reply", "active"}, r["focus"]),
                 reverse=True)

    pending_list = [
        {
            "session_id": key,
            "short_id": key[:8],
            "question": (pending.prompt_text or "")[:200],
            "options": pending.options,
        }
        for key, pending in bot._state.pending_by_session.items()
    ]
    queue_list = [
        {
            "task_number": task.task_number,
            "session": task.session_key[:8] if task.session_key and not task.session_key.startswith("new:") else "新会话",
            "project": bot._project_label(task.project_path),
            "prompt": task.prompt[:120],
        }
        for task in state.task_queue
    ]
    return {
        "bot_mode": state.mode,
        "current_project": state.current_project_label or bot._project_label(state.current_cwd),
        "current_session": (state.current_session_id or "")[:8],
        "parallel": {
            "running": len(running_keys),
            "limit": config.MAX_PARALLEL_TASKS,
            "queued": len(state.task_queue),
        },
        "remote": config.CODEX_REMOTE,
        "sessions": records,
        "pending": pending_list,
        "queue": queue_list,
        "scan_stale": (time.time() - scan_at) > 30,
        "updated_at": time.time(),
    }


def _session_detail(session_id: str) -> dict | None:
    import bot

    state = bot._state
    managed = state.sessions.get(session_id)
    if managed is None:
        return None
    project = state.projects.get(bot._norm_path(managed.project_path))
    transcript = []
    if project is not None:
        transcript = [
            {"kind": entry.kind, "text": entry.text[:500], "session": entry.session_id[:8], "at": entry.timestamp}
            for entry in project.transcript[-40:]
        ]
    return {
        "session_id": session_id,
        "short_id": session_id[:8],
        "project": managed.project_label,
        "cwd": managed.project_path,
        "title": managed.title,
        "mode": managed.mode,
        "last_event": managed.last_event,
        "last_prompt": managed.last_prompt,
        "created_at": managed.created_at,
        "updated_at": managed.updated_at,
        "transcript": transcript,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "ai-coding-agent-dashboard/1.0"

    def log_message(self, fmt, *args):
        # 静默访问日志（避免刷 bot 日志）；错误仍可查。
        pass

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(_PAGE)
            return
        if path == "/api/overview":
            if not _authorized(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(_overview_payload())
            return
        if path == "/api/session":
            if not _authorized(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            sid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            detail = _session_detail(sid)
            if detail is None:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json(detail)
            return
        self._send_json({"error": "not found"}, 404)


def start_dashboard() -> threading.Thread | None:
    """在后台线程启动看板服务；配置关闭或端口占用时返回 None 并打印告警。"""
    if not config.DASHBOARD_ENABLED:
        return None
    host = config.DASHBOARD_HOST
    if not config.DASHBOARD_TOKEN:
        host = "127.0.0.1"
        print("[dashboard] 未配置 DASHBOARD_TOKEN，仅监听 127.0.0.1", flush=True)
    try:
        httpd = ThreadingHTTPServer((host, config.DASHBOARD_PORT), _Handler)
    except OSError as exc:
        print(f"[dashboard] 端口 {config.DASHBOARD_PORT} 启动失败：{exc}", flush=True)
        return None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="web-dashboard")
    thread.start()
    print(
        f"[dashboard] 看板已启动：http://{host}:{config.DASHBOARD_PORT}/ "
        f"{'（token 鉴权）' if config.DASHBOARD_TOKEN else '（仅本机）'}",
        flush=True,
    )
    return thread


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Coding Agent · 会话看板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: radial-gradient(1200px 600px at 20% -10%, #1b2a4a 0%, #0d1420 55%, #0a0f18 100%);
    color: #dbe4f0; min-height: 100vh; padding: 16px; padding-bottom: 60px;
  }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  h1 { font-size: 18px; font-weight: 700; letter-spacing: .5px; }
  .live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #8fb7ff; background: #16233d; border-radius: 999px; padding: 4px 10px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #34d399; box-shadow: 0 0 8px #34d39988; }
  .dot.off { background: #f87171; box-shadow: 0 0 8px #f8717188; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .stat { background: #131c2ecc; border: 1px solid #23304d; border-radius: 14px; padding: 12px 14px; }
  .stat b { display: block; font-size: 22px; line-height: 1.2; }
  .stat span { font-size: 12px; color: #8ea3c4; }
  .stat.running b { color: #34d399; } .stat.waiting b { color: #fbbf24; } .stat.queued b { color: #60a5fa; }
  .section { margin: 18px 0 8px; font-size: 13px; color: #8ea3c4; display: flex; align-items: center; gap: 8px; }
  .section::after { content: ""; flex: 1; height: 1px; background: #23304d; }
  .card {
    background: #131c2ecc; border: 1px solid #23304d; border-radius: 14px;
    padding: 12px 14px; margin-bottom: 10px; cursor: pointer; transition: border-color .15s, transform .1s;
  }
  .card:hover { border-color: #3b5b9f; transform: translateY(-1px); }
  .card.focus { border-color: #60a5fa; box-shadow: 0 0 0 1px #60a5fa55; }
  .row { display: flex; align-items: center; gap: 10px; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
  .b-running { background: #0d3b2e; color: #34d399; } .b-waiting_user_reply { background: #4a3508; color: #fbbf24; }
  .b-active { background: #0d3b2e; color: #34d399; } .b-waiting { background: #4a3508; color: #fbbf24; }
  .b-completed { background: #1c2637; color: #93a6c4; } .b-stopped { background: #2a1a1a; color: #f87171; }
  .b-stale { background: #2a1a1a; color: #f87171; } .b-unknown { background: #1c2637; color: #93a6c4; }
  .b-idle { background: #1c2637; color: #93a6c4; }
  .name { font-weight: 600; font-size: 14px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sub { font-size: 12px; color: #8ea3c4; margin-top: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag { font-size: 10px; color: #6b81a6; border: 1px solid #23304d; border-radius: 6px; padding: 1px 5px; }
  .empty { color: #6b81a6; font-size: 13px; text-align: center; padding: 24px 0; }
  .overlay { position: fixed; inset: 0; background: #05080fe6; display: none; z-index: 10; overflow-y: auto; padding: 20px; }
  .overlay.open { display: block; }
  .panel { background: #101828; border: 1px solid #2a3b5e; border-radius: 16px; max-width: 720px; margin: 24px auto; padding: 18px; }
  .panel h2 { font-size: 16px; margin-bottom: 4px; }
  .panel .meta { font-size: 12px; color: #8ea3c4; margin-bottom: 12px; word-break: break-all; }
  .close { float: right; background: #1c2a45; border: none; color: #dbe4f0; border-radius: 8px; padding: 4px 12px; cursor: pointer; font-size: 13px; }
  .msg { border-left: 3px solid #33405c; padding: 6px 10px; margin: 6px 0; font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; background: #0e1526; border-radius: 0 8px 8px 0; }
  .msg.user { border-left-color: #60a5fa; } .msg.error { border-left-color: #f87171; } .msg.assistant { border-left-color: #34d399; }
  .msg .k { font-size: 10px; color: #6b81a6; }
  footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 10px; text-align: center; font-size: 11px; color: #4d5f7d; background: #0a0f18f2; }
</style>
</head>
<body>
<header>
  <h1>🤖 AI Coding Agent · 会话看板</h1>
  <div class="live" id="live"><span class="dot" id="dot"></span><span id="livetext">连接中…</span></div>
</header>

<div class="stats">
  <div class="stat running"><b id="st-running">–</b><span>运行中</span></div>
  <div class="stat waiting"><b id="st-waiting">–</b><span>等待你决策</span></div>
  <div class="stat queued"><b id="st-queued">–</b><span>队列</span></div>
  <div class="stat"><b id="st-total">–</b><span>会话总数</span></div>
</div>

<div class="section" id="pending-title">需要你决策</div>
<div id="pending"></div>

<div class="section">会话列表</div>
<div id="sessions"></div>

<footer>数据来自家里电脑的 Codex 会话 · 5 秒自动刷新</footer>

<div class="overlay" id="overlay"><div class="panel" id="panel"></div></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || localStorage.getItem('dash_token') || '';
if (new URLSearchParams(location.search).get('token')) localStorage.setItem('dash_token', TOKEN);
const API = (path) => { const sep = path.includes('?') ? '&' : '?'; return fetch(path + sep + 'token=' + encodeURIComponent(TOKEN)); };

const ST = {
  running: '运行中', waiting_user_reply: '等待你决策', waiting: '等待输入',
  active: '活动中', completed: '已完成', stopped: '已停止', stale: '疑似中断',
  idle: '空闲', unknown: '未知'
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function refresh() {
  let data;
  try {
    const r = await API('/api/overview');
    if (r.status === 401) { document.getElementById('livetext').textContent = '需要 token（URL 加 ?token=xxx）'; return; }
    data = await r.json();
  } catch (e) { document.getElementById('livetext').textContent = '连接失败'; return; }
  document.getElementById('dot').className = 'dot' + (data.scan_stale ? ' off' : '');
  document.getElementById('livetext').textContent = '已同步 · ' + new Date(data.updated_at * 1000).toLocaleTimeString();
  document.getElementById('st-running').textContent = data.parallel.running;
  document.getElementById('st-waiting').textContent = data.pending.length;
  document.getElementById('st-queued').textContent = data.parallel.queued;
  document.getElementById('st-total').textContent = data.sessions.length;

  const pend = document.getElementById('pending');
  if (data.pending.length === 0) {
    document.getElementById('pending-title').style.display = 'none';
    pend.innerHTML = '';
  } else {
    document.getElementById('pending-title').style.display = 'flex';
    pend.innerHTML = data.pending.map(p => `
      <div class="card focus"><div class="row">
        <span class="badge b-waiting_user_reply">待决策</span>
        <span class="name">${esc(p.short_id)}</span></div>
        <div class="sub">${esc(p.question)}</div>
        ${p.options && p.options.length ? '<div class="sub">选项：' + p.options.map(esc).join(' / ') + '</div>' : ''}
      </div>`).join('');
  }

  const box = document.getElementById('sessions');
  if (data.sessions.length === 0) { box.innerHTML = '<div class="empty">暂无会话</div>'; return; }
  box.innerHTML = data.sessions.map(s => `
    <div class="card ${s.focus ? 'focus' : ''}" onclick="openDetail('${s.session_id}')">
      <div class="row">
        <span class="badge b-${esc(s.status)}">${esc(ST[s.status] || s.status)}</span>
        <span class="name">${esc(s.project)} / ${esc(s.short_id)}</span>
        ${s.focus ? '<span class="tag">焦点</span>' : ''}
        ${s.managed ? '' : '<span class="tag">远端</span>'}
      </div>
      <div class="sub">${esc(s.last_event || s.last_prompt || '—')}</div>
    </div>`).join('');
}

async function openDetail(id) {
  const overlay = document.getElementById('overlay');
  const panel = document.getElementById('panel');
  panel.innerHTML = '<div class="meta">加载中…</div>';
  overlay.classList.add('open');
  const r = await API('/api/session?id=' + encodeURIComponent(id));
  if (!r.ok) { panel.innerHTML = '<div class="meta">加载失败</div>'; return; }
  const d = await r.json();
  panel.innerHTML = `
    <button class="close" onclick="document.getElementById('overlay').classList.remove('open')">✕ 关闭</button>
    <h2>${esc(d.project)} / ${esc(d.short_id)}</h2>
    <div class="meta">${esc(d.session_id)}<br>标题：${esc(d.title)}<br>状态：${esc(ST[d.mode] || d.mode)}<br>最后事件：${esc(d.last_event)}<br>最后任务：${esc(d.last_prompt)}</div>
    <div class="section">最近窗口</div>
    ${(d.transcript && d.transcript.length) ? d.transcript.map(m =>
      `<div class="msg ${esc(m.kind)}"><span class="k">[${esc(m.kind)}] ${esc(m.session)}</span>${esc(m.text)}</div>`).join('')
      : '<div class="empty">暂无记录</div>'}`;
}
document.getElementById('overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') e.target.classList.remove('open'); });

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
