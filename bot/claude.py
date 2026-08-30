"""Claude 会话监控层 —— /sessions /tail /use /del /help。

2026-08-30 精简:SDK 执行层(每条消息 spawn 一个 agent 进程跑一轮)整体
退役——超时即丢结果、每次 resume 重放全量 transcript 易撞网关限频,两痛点
由 tmux 常驻会话后端(tmux_be.py,main.py 直连)根治。本模块只保留基于
Agent SDK 会话管理 API 的历史会话读写——tmux 会话的 transcript 同样落在
~/.claude 里,一样可查、可用 /use 续跑。

微信侧指令(只认 / 前缀):
  /sessions [N]      列出所有 Claude 会话(跨项目,按最近活动降序)
  /tail <ref> [N]    看某会话最近 N 条对话(ref = 上次列表的序号 / id 前缀)
  /use <ref>         常驻会话切到指定历史会话续跑(kill 后 --resume 重建)
  /del <ref>         删除某会话(磁盘 transcript 硬删,不可恢复)
  /help              指令一览

关键实现事实(2026-08-20 实测):
- list_sessions() 不带 directory = 跨全部项目,按 last_modified 降序
- get_session_messages 的 limit 是从头取(最旧在前)——取尾部要全量读再切片
"""

import os
import re
import time

import tmux_be
from claude_agent_sdk import (
    delete_session,
    get_session_info,
    get_session_messages,
    list_sessions,
)

# ---- 访问控制(main.py 用于普通消息/指令的准入;此处不再做工具分层)----
_raw_admins = os.environ.get("WECHAT_ADMIN_USERS", "")
ADMIN_USERS = {u.strip() for u in _raw_admins.split(",") if u.strip()}
if not ADMIN_USERS:
    print(
        "⚠️ WECHAT_ADMIN_USERS 未设置:当前所有人可用(含 Bash 全工具)。"
        "自用无妨;公开前请设成你自己的 user_id。"
    )

_RE_SID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# 每用户最近一次 /sessions 列出的会话 id(「序号」引用的就是它)
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
    """把常驻会话切到某个历史会话续跑:kill 后以 --resume <sid> 重建。

    tmux 里跑的是交互式 claude,没法在运行中换 resume 目标,只能重建;
    重建后该会话的历史上下文全部带上。"""
    sid = _resolve_sid(ref, user_id)
    if not sid or get_session_info(sid) is None:
        return "⚠️ 找不到会话。先发 /sessions 看列表，再用序号或 id 前缀。"
    err = tmux_be.switch_session(sid)
    if err:
        return f"⚠️ 切换失败：{err}"
    return (f"✅ 常驻会话已重建并续上 {sid[:8]} 的历史上下文，"
            "接着聊即可（发 /reset 可回到全新会话）。")


def _cmd_del(user_id: str, ref: str) -> str:
    sid = _resolve_sid(ref, user_id)
    if not sid or get_session_info(sid) is None:
        return "⚠️ 找不到会话。先发 /sessions 看列表，再用序号或 id 前缀。"
    # 正被常驻会话 resume 时不许删(删 transcript 会把在跑的会话弄坏)
    if sid in tmux_be.resuming_sid():
        return "⏳ 常驻会话正续在这个会话上,先 /reset 或 /use 切走再删。"
    try:
        delete_session(sid)               # 硬删：JSONL + 子代理 transcript
    except Exception as e:
        return f"⚠️ 删除失败：{e}"
    _last_listed[user_id] = [s for s in (_last_listed.get(user_id) or [])
                             if s != sid]
    return f"🗑️ 已删除会话 {sid[:8]}，不可恢复。"


_HELP = """📖 Bot 指令（均以 / 开头）
• /reset — 结束常驻会话，下条消息开新会话（历史保留可找回）
• /sessions [N] — 列出所有 Claude 会话（含常驻会话的历史）
• /tail <序号|id> [N] — 看某会话最近 N 条对话
• /use <序号|id前缀> — 常驻会话切到指定历史会话续跑
• /del <序号|id前缀> — 删除某会话（不可恢复）
• /screen — 常驻会话当前画面 + 状态（进度/排队/死因）
• /esc — 打断常驻会话当前任务
• /tap <键> — 透传按键（权限确认选 1/y、C-c 等）
• /file <路径> [附言] — 把服务器上的文件/图片/视频发到微信（仅管理员）

普通消息直接进常驻会话：空闲即答，在跑会排队，回复由 hook 推送。
电脑上 `tmux attach -t wxclaude` 可全交互接管同一个会话。"""


def handle_monitor_command(text: str, user_id: str) -> str | None:
    """会话监控指令的统一入口（只认 / 前缀指令）。
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
            return "用法：/use <序号|id前缀>。先 /sessions 看列表。"
        return _cmd_use(user_id, args[0])
    if cmd == "/del":
        if not args:
            return "用法：/del <序号|id前缀>（硬删不可恢复）。"
        return _cmd_del(user_id, args[0])
    return None
