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
import threading
import time
import uuid

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
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


def _run_turn_sync(text: str, user_id: str) -> str:
    """跑一轮 agent（含超时与 session 更新），返回给用户的文本。

    调用方必须已持有该 user 的锁——保证同一 session 不会被并发 resume。
    每次新建临时 event loop 驱动 agent（可安全用于主线程外的后台线程）。
    """
    sid = _sessions.get(user_id)
    try:
        answer, new_sid = asyncio.run(
            asyncio.wait_for(_agent_turn(text, sid, user_id), timeout=TURN_TIMEOUT)
        )
    except asyncio.TimeoutError:
        return (f"⏳ 处理超时（>{TURN_TIMEOUT}s）。"
                f"这类长任务建议用 /bg <内容> 跑后台，不卡循环、不撞超时。")
    except Exception as e:
        return f"⚠️ agent 出错：{e}"
    if new_sid:
        _sessions[user_id] = new_sid
    return answer


def try_run_inline(text: str, user_id: str = "default") -> str | None:
    """同步跑一轮（阻塞主线程），立刻返回回复。

    适合短任务（普通对话 / 单步查询）。若该用户已有任务在跑，返回 None，
    由调用方回复"忙"。返回 None 而非等待，是为了主收消息循环绝不卡死。
    """
    lk = _user_lock(user_id)
    if not lk.acquire(blocking=False):
        return None
    try:
        return _run_turn_sync(text, user_id)
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
        }

    def _worker():
        result = ""
        try:
            result = _run_turn_sync(text, user_id)
        except Exception as e:  # _run_turn_sync 已兜底，这里是双保险
            result = f"⚠️ 后台作业异常：{e}"
        finally:
            lk.release()
            with _jobs_guard:
                if job_id in _jobs:
                    _jobs[job_id]["status"] = "done"
                    _jobs[job_id]["finished"] = time.time()
            try:
                on_done(result)
            except Exception as e:
                print(f"[bg {job_id}] on_done 回调失败：{e}")

    threading.Thread(target=_worker, daemon=True, name=f"bg-{job_id}").start()
    return job_id


def ask_claude(text: str, user_id: str = "default") -> str:
    """阻塞式跑一轮（自测/兼容用）。main.py 请用 try_run_inline / submit_background。"""
    lk = _user_lock(user_id)
    lk.acquire()  # 阻塞等待该用户的锁
    try:
        return _run_turn_sync(text, user_id)
    finally:
        lk.release()


def reset_user(user_id: str) -> None:
    """清空某用户的 agent 会话（用户发"重置"时调用，下条消息开新会话）。

    注意：若该用户正好有后台作业在跑，在跑的 turn 会把自己的 session 写回，
    可能把重置覆盖掉——所以重置最好在没有 /jobs 进行时发。"""
    _sessions.pop(user_id, None)


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
