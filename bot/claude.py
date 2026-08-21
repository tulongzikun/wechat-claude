"""Claude Agent SDK 后端 —— 每个微信用户对应一个常驻的 Claude agent 会话。

用官方 claude-agent-sdk：agent 自带工具（读写文件、Bash、grep、web 搜索等）、
持久会话（跨消息续上下文）、独立工作区。每条微信消息驱动 agent 跑一轮，
最终文本回复给微信。

相比上一版（无状态 messages.create + 手写 run_shell 工具），这一版是一个
真正的常驻 agent：能连续多步干活、记得上文、可读写工作区文件。

鉴权复用本机 claude 的环境变量（ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL）——
Agent SDK 会 spawn claude 子进程并继承这些变量，所以不需要单独的 API key。
"""

import asyncio
import os
import re
import subprocess
import threading
import time
import uuid

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    delete_session,
    get_session_info,
    get_session_messages,
    list_sessions,
    query,
)

# ---- 可调配置（环境变量覆盖）----

# agent 的文件操作根目录（读/写/Bash 默认以它为工作区）。
# 默认整个 home 目录；想收紧就把环境变量 WECHAT_AGENT_CWD 设成更小目录。
# 注意：Bash 工具本身不严格受 cwd 限制（可 cd/绝对路径），真正的安全边界是
# 进程的 OS 用户权限——agent 以当前用户身份跑，能访问该用户可访问的一切。
AGENT_CWD = os.environ.get("WECHAT_AGENT_CWD", "/home/zhouzikun")
# agent 单轮最多步数（防止失控长跑烧钱）
MAX_TURNS = int(os.environ.get("WECHAT_AGENT_MAX_TURNS", "12"))
# 单轮整体超时（秒），超时给用户一句提示，不让一条慢消息卡死收消息循环
# 180 是给多步探索任务（查会话/读多文件/分析）留余量——实测这类任务常跑到 120s+；
# 普通对话 ~9s、单步工具 ~15-30s，远不会触顶。
TURN_TIMEOUT = int(os.environ.get("WECHAT_AGENT_TIMEOUT", "180"))
# 回复最长字符（微信长文体验差，超出截断）
MAX_REPLY_LEN = 2000

SYSTEM_PROMPT = (
    "你是一个运行在微信里的 AI 助手，托管在一台 Linux 服务器上，"
    f"默认工作目录是 {AGENT_CWD}。用简洁、自然的中文回答，适合微信阅读。\n"
    "你自己就跑在这个 bot 里，源码在 ~/workspace/wechat/"
    "（main.py 收发并分发消息、claude.py 就是你这个 agent）。\n"
    "\n"
    "【涉及本机事实的问题——一律先查再答，绝不凭记忆猜】\n"
    "凡是问「有没有 / 装没装 / 是否设置 / 在不在跑 / 几点跑 / 在哪 / 支持什么命令」"
    "这类关于本机实际状态的问题，都要先用工具查证，查到什么答什么。常用查法：\n"
    "- 定时任务、计划任务 → `crontab -l`\n"
    "- 某进程/服务在不在跑 → `ps aux | grep <名字>` 或看对应的 pid 文件\n"
    "- 文件 / 代码在哪、有哪些 → `ls`、`find`、`grep`\n"
    "- 装没装某命令或工具 → `which <名字>` / `command -v <名字>`\n"
    "- 这个 bot 支持哪些指令、怎么用 → 读 ~/workspace/wechat/main.py 的分发逻辑\n"
    "- 配置内容 → 直接读对应文件\n"
    "以上是「怎么查」的方法，结果一律以查到的为准。本机随时可能增删任务、改动命令，"
    "所以不要靠记忆，每次现查。\n"
    "\n"
    "普通对话、知识问答、翻译、写作、建议——直接回答，不必调用工具。\n"
    "确实查不到时，如实说\"没查到 / 不确定\"，不要编造答案。\n"
    "\n"
    "回复只写最终结论，省略工具过程。"
)

# 授权用户（全工具）：能读写文件、跑命令、联网
FULL_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
# 非授权用户（只读）：只能看，不能改文件、不能跑命令
READONLY_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]

# ---- 访问控制：谁能用全工具 ----
# 微信 user_id 白名单（逗号分隔），从环境变量 WECHAT_ADMIN_USERS 读取。
# 留空 = 所有人都算授权（仅自用场景）。一旦公开 bot，务必设成你自己的
# user_id —— 这样别人发消息只能用只读工具，无法在你机器上跑命令/改文件。
_raw_admins = os.environ.get("WECHAT_ADMIN_USERS", "")
ADMIN_USERS = {u.strip() for u in _raw_admins.split(",") if u.strip()}
if not ADMIN_USERS:
    print(
        "⚠️ WECHAT_ADMIN_USERS 未设置：当前所有用户都用全工具（含 Bash/Write）。"
        "自用无妨；公开前请设成你自己的 user_id，让其他人只读。"
    )


def _tools_for(user_id: str) -> list[str]:
    """授权用户给全工具；其他人只读（无 Bash/Edit/Write）。"""
    if ADMIN_USERS and user_id not in ADMIN_USERS:
        return READONLY_TOOLS
    return FULL_TOOLS

# 每个微信用户 -> agent session_id。首次为 None（开新会话），跑完一轮从
# ResultMessage 拿到 session_id 后存起来，下一轮用 resume= 续上下文。
_sessions: dict[str, str | None] = {}

# ── 后台作业（机制 3）─────────────────────────────────────────────────────
# 目的：长任务（多步探索 / 读别的 agent 产物再修订 / 回测）扔到工作线程里跑，
# 主收消息循环不阻塞、不撞 180s 超时；完成后回调把结果推回微信。
#
# 关键约束：同一用户同时只能有一个 agent turn 在跑 —— 否则两个 turn 都 resume
# 同一个 session_id 会冲突。所以用 per-user 锁串行化（inline 与后台共用）。

_user_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _user_lock(user_id: str) -> threading.Lock:
    """取（必要时建）某用户的锁。"""
    with _locks_guard:
        lk = _user_locks.get(user_id)
        if lk is None:
            lk = threading.Lock()
            _user_locks[user_id] = lk
        return lk


# job_id -> {id, user_id, status, started, snippet, finished?}
_jobs: dict[str, dict] = {}
_jobs_guard = threading.Lock()
_JOB_TTL = 3600  # 已完成作业保留 1 小时，超时清理，免得内存无限增长


def list_jobs(user_id: str | None = None) -> list[dict]:
    """列出作业（可选按 user 过滤）。顺带清理过期已完成作业。"""
    now = time.time()
    with _jobs_guard:
        # 清理：完成超过 _JOB_TTL 的删掉
        stale = [
            jid for jid, j in _jobs.items()
            if j.get("status") == "done" and now - j.get("finished", now) > _JOB_TTL
        ]
        for jid in stale:
            del _jobs[jid]
        items = [dict(j) for j in _jobs.values()]
    if user_id:
        items = [j for j in items if j.get("user_id") == user_id]
    return items


async def _agent_turn(
    prompt: str, session_id: str | None, user_id: str
) -> tuple[str, str | None]:
    """跑一轮 agent，返回 (给用户的回复文本, 新的 session_id)。"""
    opts_kwargs = dict(
        allowed_tools=_tools_for(user_id),
        permission_mode="acceptEdits",
        cwd=AGENT_CWD,
        system_prompt=SYSTEM_PROMPT,
        max_turns=MAX_TURNS,
    )
    if session_id:
        # 续上这个用户的 agent 会话（跨进程从磁盘 transcript 恢复）
        opts_kwargs["resume"] = session_id
    options = ClaudeAgentOptions(**opts_kwargs)

    texts: list[str] = []
    new_sid = session_id
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                t = getattr(block, "text", None)
                if t:
                    texts.append(t)
        elif isinstance(msg, ResultMessage):
            # 每轮都更新 session_id（resume 链路里它应保持稳定）
            new_sid = getattr(msg, "session_id", None) or new_sid

    answer = "".join(texts).strip()
    if len(answer) > MAX_REPLY_LEN:
        answer = answer[:MAX_REPLY_LEN] + "\n...(已截断)"
    return answer or "（agent 这一轮没有返回文本）", new_sid


def _run_turn_sync(text: str, user_id: str, isolated: bool = False) -> tuple[str, str | None]:
    """跑一轮 agent（含超时），返回 (回复文本, 本轮落到的 session_id)。

    调用方一般应已持有该 user 的锁——保证同一 session 不会被并发 resume。
    每次新建临时 event loop 驱动 agent（可安全用于主线程外的后台线程）。
    isolated=True：不读也不写该用户的会话指针——起全新会话跑一次性任务，
    与该用户正在续的其他会话互不冲突（transcript 不同，可并行）。
    """
    sid = None if isolated else _sessions.get(user_id)
    try:
        answer, new_sid = asyncio.run(
            asyncio.wait_for(_agent_turn(text, sid, user_id), timeout=TURN_TIMEOUT)
        )
    except asyncio.TimeoutError:
        return (f"⏳ 处理超时（>{TURN_TIMEOUT}s）。"
                f"这类长任务建议用 /bg 或 /new 跑后台，不卡循环、不撞超时。", sid)
    except Exception as e:
        return f"⚠️ agent 出错：{e}", sid
    if new_sid and not isolated:
        _sessions[user_id] = new_sid
    return answer, new_sid


def try_run_inline(text: str, user_id: str = "default") -> str | None:
    """同步跑一轮（阻塞主线程），立刻返回回复。

    适合短任务（普通对话 / 单步查询）。若该用户已有任务在跑，返回 None，
    由调用方回复"忙"。返回 None 而非等待，是为了主收消息循环绝不卡死。
    """
    lk = _user_lock(user_id)
    if not lk.acquire(blocking=False):
        return None
    try:
        return _run_turn_sync(text, user_id)[0]
    finally:
        lk.release()


def submit_background(text: str, user_id: str, on_done) -> str | None:
    """派一个后台作业跑 agent，完成时在工作线程里回调 on_done(result_text)。

    主循环立即返回，不阻塞、不受 TURN_TIMEOUT 限制。返回 job_id；
    若该用户已有任务在跑（锁被占），返回 None（调用方提示忙）。
    on_done 在工作线程内执行，应只做"发回微信"这类线程安全的事。
    """
    lk = _user_lock(user_id)
    if not lk.acquire(blocking=False):
        return None

    job_id = uuid.uuid4().hex[:8]
    with _jobs_guard:
        _jobs[job_id] = {
            "id": job_id,
            "user_id": user_id,
            "status": "running",
            "started": time.time(),
            "snippet": text[:40],
            "session": _sessions.get(user_id),   # 本作业正在续的会话
            "kind": "bg",
        }

    def _worker():
        result, sid = "", None
        try:
            result, sid = _run_turn_sync(text, user_id)
        except Exception as e:  # _run_turn_sync 已兜底，这里是双保险
            result = f"⚠️ 后台作业异常：{e}"
        finally:
            lk.release()
            with _jobs_guard:
                if job_id in _jobs:
                    _jobs[job_id]["status"] = "done"
                    _jobs[job_id]["finished"] = time.time()
                    # 作业落到的（可能是新的）会话 id——/use <job_id> 继续它
                    _jobs[job_id]["session"] = sid or _sessions.get(user_id)
            try:
                on_done(result)
            except Exception as e:
                print(f"[bg {job_id}] on_done 回调失败：{e}")

    threading.Thread(target=_worker, daemon=True, name=f"bg-{job_id}").start()
    return job_id


# 同一用户同时跑的 /new 全新会话作业数上限（不限会膨胀失控）
_MAX_FRESH_PER_USER = 2


def submit_fresh(text: str, user_id: str, on_done) -> str | None:
    """起一个【全新会话】的后台子进程跑任务（不 resume 任何历史）。

    与 submit_background 的三点区别：
    - 不占该用户的会话锁——全新 transcript 与在续会话无冲突，可与当前对话
      并行跑（per-user 锁的存在意义就是防并发 resume 同一 session）；
    - 不动 _sessions[user_id]——当前对话照旧续原会话，任务跑完也不切换；
    - 落到的新会话记进 job["session"]，/use <job_id> 可继续它。
    完成回调 on_done(result_text)（工作线程内执行）。返回 job_id；
    并行数达上限返回 None。
    """
    running = [j for j in list_jobs(user_id)
               if j.get("status") == "running" and j.get("kind") == "fresh"]
    if len(running) >= _MAX_FRESH_PER_USER:
        return None
    job_id = uuid.uuid4().hex[:8]
    with _jobs_guard:
        _jobs[job_id] = {
            "id": job_id,
            "user_id": user_id,
            "status": "running",
            "started": time.time(),
            "snippet": text[:40],
            "session": None,          # 全新会话，跑完才落定
            "kind": "fresh",
        }

    def _worker():
        result, sid = "", None
        try:
            result, sid = _run_turn_sync(text, user_id, isolated=True)
        except Exception as e:
            result = f"⚠️ 新会话作业异常：{e}"
        finally:
            with _jobs_guard:
                if job_id in _jobs:
                    _jobs[job_id].update(status="done", finished=time.time(),
                                         session=sid)
            try:
                on_done(result)
            except Exception as e:
                print(f"[fresh {job_id}] on_done 回调失败：{e}")

    threading.Thread(target=_worker, daemon=True, name=f"fresh-{job_id}").start()
    return job_id


def ask_claude(text: str, user_id: str = "default") -> str:
    """阻塞式跑一轮（自测/兼容用）。main.py 请用 try_run_inline / submit_background。"""
    lk = _user_lock(user_id)
    lk.acquire()  # 阻塞等待该用户的锁
    try:
        return _run_turn_sync(text, user_id)[0]
    finally:
        lk.release()


def reset_user(user_id: str) -> None:
    """清空某用户的 agent 会话（/reset 或 /exit 时调用，下条消息开新会话）。

    注意：若该用户正好有后台作业在跑，在跑的 turn 会把自己的 session 写回，
    可能把重置覆盖掉——所以重置最好在没有 /jobs 进行时发。"""
    _sessions.pop(user_id, None)


# ── 会话/子进程监控（SDK 会话管理 API + /proc 扫描）──────────────────────
# 微信侧指令（只认 / 前缀，无文字别名；main.py 分发到 handle_monitor_command）：
#   /sessions [N]      列出所有 Claude 会话（跨项目，按最近活动降序）
#   /tail <ref> [N]    看某会话最近 N 条对话（ref = 上次列表的序号 / id 前缀）
#   /use <ref>         把当前用户的对话切到指定会话继续（也可接后台 job_id）
#   /exit              退出当前会话（下条消息开新会话；原会话保留可 /use 找回）
#   /del <ref>         删除某会话（磁盘 transcript 硬删，不可恢复）
#   /procs             bot 派生的 claude 子进程（含各自续的会话）+ 运行中作业
#   /help              指令一览
#
# 关键实现事实（2026-08-20 实测）：
# - list_sessions() 不带 directory = 跨全部项目，按 last_modified 降序
# - get_session_messages 的 limit 是从头取（最旧在前）——取尾部要全量读再切片
# - SDK 派生的 claude CLI 是本进程的后代，cmdline 含 "--resume=<sid>"
#   （新会话无此 flag），据此把 OS 进程和会话关联起来

_RE_SID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RE_RESUME = re.compile(r"--resume=([0-9a-f-]{36})")

# 每用户最近一次 /sessions 或 /procs 列出的会话 id（「序号」引用的就是它）
_last_listed: dict[str, list[str]] = {}


def _age(ts_ms: float) -> str:
    """epoch 毫秒 → 『3分钟前』。"""
    s = max(0, time.time() - ts_ms / 1000)
    for unit, div in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if s >= div:
            return f"{int(s // div)}{unit}前"
    return f"{int(s)}秒前"


def _msg_text(sm) -> str:
    """SessionMessage.message（原始 API dict）→ 可读文本；非文本块跳过。"""
    content = (sm.message or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _resolve_sid(ref: str, user_id: str) -> str | None:
    """把 /tail、/use 的参数解析成 session_id：序号 / 完整 id / id 前缀。"""
    ref = ref.strip().lower()
    if ref.isdigit():                          # 序号 → 上次列出的会话
        lst = _last_listed.get(user_id) or []
        i = int(ref)
        return lst[i - 1] if 1 <= i <= len(lst) else None
    if _RE_SID.match(ref):
        return ref
    cands = [s.session_id for s in list_sessions(limit=50)
             if s.session_id.startswith(ref)]  # id 前缀（>=8 位基本唯一）
    return cands[0] if len(cands) == 1 else None


def _cmd_sessions(user_id: str, limit: int) -> str:
    sessions = list_sessions(limit=limit)
    _last_listed[user_id] = [s.session_id for s in sessions]
    lines = []
    for i, s in enumerate(sessions, 1):
        title = (s.custom_title or s.summary or s.first_prompt or "（无摘要）")
        lines.append(f"{i}. {s.session_id[:8]} · {_age(s.last_modified)} · "
                     f"{title.replace(chr(10), ' ')[:24]}")
    body = "\n".join(lines) if lines else "（没有找到会话）"
    return (f"📋 Claude 会话（最近 {len(sessions)} 个，跨项目）：\n{body}\n"
            "— /tail <序号> 看内容 · /use <序号> 切过去继续")


def _cmd_tail(user_id: str, ref: str, n: int) -> str:
    sid = _resolve_sid(ref, user_id)
    if not sid:
        return "⚠️ 找不到会话。先发 /sessions 看列表，再用序号或 id 前缀（>=8位）。"
    info = get_session_info(sid)
    if info is None:
        return f"⚠️ 会话 {sid[:8]} 不存在（可能已被清理）。"
    msgs = get_session_messages(sid)           # limit 从头取，取尾只能全量读
    shown = []
    for m in reversed(msgs):                   # 从最新往回取有文本的 n 条
        t = _msg_text(m)
        if not t.strip() or t.startswith("This session is being continued"):
            continue                           # 跳过空消息和压缩续接样板
        shown.append(("👤" if m.type == "user" else "🤖")
                     + " " + t.replace("\n", " ")[:80])
        if len(shown) >= n:
            break
    title = (info.custom_title or info.summary or sid[:8]).replace("\n", " ")
    head = f"📂 {title[:40]}（共 {len(msgs)} 条，最近 {len(shown)} 条，倒序）"
    return head + "\n" + "\n".join(shown)


def _cmd_use(user_id: str, ref: str) -> str:
    # 先看是不是后台作业 id → 接它（跑完）的会话
    with _jobs_guard:
        job = _jobs.get(ref)
    if job is not None:
        if job.get("status") == "running":
            return f"⏳ job={ref} 还在跑，等完成后再续（/jobs 看状态）。"
        sid = job.get("session")
        if not sid:
            return f"⚠️ job={ref} 没有记录会话 id。"
    else:
        sid = _resolve_sid(ref, user_id)
    if not sid or get_session_info(sid) is None:
        return "⚠️ 找不到会话。先发 /sessions 看列表，再用序号或 id 前缀。"
    if _user_lock(user_id).locked():
        return "⚠️ 你有任务正在跑，完成后再切换，否则正在跑的 turn 会把会话写回覆盖。"
    _sessions[user_id] = sid
    return (f"✅ 已切到会话 {sid[:8]}，下一条消息就续在这个会话上"
            "（发 /exit 断开回新会话）。")


def _descendants(root: int) -> set[int]:
    """root 的全部后代 pid（扫 /proc/*/stat 的 ppid 链，一层层往外扩）。"""
    pp = {}
    for e in os.scandir("/proc"):
        if not e.name.isdigit():
            continue
        try:
            with open(f"/proc/{e.name}/stat") as f:
                pp[int(e.name)] = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    desc, frontier = set(), {root}
    while frontier:
        nxt = {p for p, q in pp.items() if q in frontier and p not in desc}
        desc |= nxt
        frontier = nxt
    return desc


def _claude_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").replace("\0", " ").strip()
    except OSError:
        return ""


def _cmd_procs(user_id: str) -> str:
    lines, sids = [], []
    pids = sorted(p for p in _descendants(os.getpid())
                  if "claude_agent_sdk" in _claude_cmdline(p))
    if pids:
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=,etime=,args=", "-p", ",".join(map(str, pids))],
                capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                ps_pid, etime, args = ln.strip().split(None, 2)
                m = _RE_RESUME.search(args)
                tag = f"续会话 {m.group(1)[:8]}" if m else "新会话"
                sids.append(m.group(1)) if m else None
                lines.append(f"• PID {ps_pid} · 已跑 {etime} · {tag}")
        except Exception as e:
            lines.append(f"• ps 查询失败：{e}")
    else:
        lines.append("• 当前没有在跑的 claude 子进程")

    running = [j for j in list_jobs() if j.get("status") == "running"]
    jlines = []
    for j in running:
        sid = j.get("session")
        if sid:
            sids.append(sid)
        if j.get("kind") == "fresh":
            tag = "🆕全新会话" + (f"→{sid[:8]}" if sid else "")
        else:
            tag = "续" + (f"会话 {sid[:8]}" if sid else "新会话")
        jlines.append(f"• job={j['id']} · {_age(j['started'] * 1000)}开始 · "
                      f"{tag} · {j.get('snippet', '')}")
    if jlines:
        lines.append("后台作业：")
        lines.extend(jlines)

    if sids:
        _last_listed[user_id] = sids           # 序号可接着 /use、/tail（空则保留上次）
    head = f"🔧 claude 子进程（bot PID {os.getpid()} 的后代）："
    return head + "\n" + "\n".join(lines) + \
        ("\n— /use <序号> 续接对应会话" if sids else "")


def _cmd_exit(user_id: str) -> str:
    """退出当前会话：只断开指针（下条消息开新会话），transcript 保留。"""
    sid = _sessions.get(user_id)
    reset_user(user_id)
    return (f"👋 已退出会话 {sid[:8] if sid else '（无）'}，下条消息开新会话。\n"
            "原会话仍在磁盘上，/sessions + /use 可随时找回继续；"
            "要彻底删除用 /del <序号|id>。")


def _cmd_del(user_id: str, ref: str) -> str:
    sid = _resolve_sid(ref, user_id)
    if not sid or get_session_info(sid) is None:
        return "⚠️ 找不到会话。先发 /sessions 看列表，再用序号或 id 前缀。"
    # 有后台作业正 resume 它时不许删（删 transcript 会把在跑的 turn 弄坏）
    for j in list_jobs():
        if j.get("status") == "running" and j.get("session") == sid:
            return f"⏳ job={j['id']} 正在这个会话上跑，等它完成后再删。"
    try:
        delete_session(sid)               # 硬删：JSONL + 子代理 transcript
    except Exception as e:
        return f"⚠️ 删除失败：{e}"
    if _sessions.get(user_id) == sid:     # 删的是当前会话 → 一并退出
        reset_user(user_id)
        detached = "（是当前会话，已一并退出）"
    else:
        detached = ""
    _last_listed[user_id] = [s for s in (_last_listed.get(user_id) or [])
                             if s != sid]
    return f"🗑️ 已删除会话 {sid[:8]}{detached}，不可恢复。"


_HELP = """📖 Bot 指令（均以 / 开头）
• /sessions [N] — 列出所有 Claude 会话
• /tail <序号|id> [N] — 看某会话最近 N 条对话
• /use <序号|id|job_id> — 切到某会话继续
• /exit — 退出当前会话（保留，可 /use 找回）
• /del <序号|id> — 删除某会话（不可恢复）
• /procs — claude 子进程与后台作业
• /new <任务> — 另起全新会话后台跑（不带当前对话历史，完成通知，可同时跑 2 个）
• /bg <任务> — 后台跑长任务（续当前会话，完成推回）
• /jobs — 后台作业状态
• /reset — 同 /exit（清空当前对话）"""


def handle_monitor_command(text: str, user_id: str) -> str | None:
    """会话/子进程监控指令的统一入口（只认 / 前缀指令）。
    不是这类指令返回 None（main.py 继续分发）。裸指令缺参数时给用法提示。"""
    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "/help":
        return _HELP
    if cmd == "/sessions":
        limit = 10
        if args and args[0].isdigit():
            limit = min(int(args[0]), 30)
        return _cmd_sessions(user_id, limit)[:2000]
    if cmd == "/tail":
        if not args:
            return "用法：/tail <序号|id前缀> [条数]。先 /sessions 看列表。"
        n = 6
        if len(args) > 1 and args[1].isdigit():
            n = min(int(args[1]), 12)
        return _cmd_tail(user_id, args[0], n)[:2000]
    if cmd == "/use":
        if not args:
            return "用法：/use <序号|id前缀|job_id>。先 /sessions 或 /procs 看列表。"
        return _cmd_use(user_id, args[0])
    if cmd == "/exit":
        return _cmd_exit(user_id)
    if cmd == "/del":
        if not args:
            return "用法：/del <序号|id前缀>（硬删不可恢复；只断开用 /exit）。"
        return _cmd_del(user_id, args[0])
    if cmd == "/procs":
        return _cmd_procs(user_id)[:2000]
    return None


if __name__ == "__main__":
    # 自测：设置好 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 后 python claude.py
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit("请先设置环境变量 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN")
    # 连发两条，验证第二条能续上第一条的上下文（session resume）
    uid = "selftest"
    print("Q1: 用 Bash 查 whoami，一句话告诉我。")
    print("A1:", ask_claude("用 Bash 查 whoami，一句话告诉我。", uid))
    print("\nQ2: 刚才查到的用户名是什么？（测上下文延续，不应再执行命令）")
    print("A2:", ask_claude("刚才查到的用户名是什么？", uid))
