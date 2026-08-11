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
    f"工作目录是 {AGENT_CWD}。用简洁、自然的中文回答，适合微信阅读。"
    "\n\n【工具使用】按需合理调用：需要本机实时状态（查进程/读文件/跑命令/看日志）时，"
    "该查就查、该多步就多步，以准确为先；只是不必为求'全面'反复查询同一信息。"
    "而普通对话、知识问答、翻译、写作、建议这类——直接回答，不调用工具。"
    "\n\n回复只写最终结论，省略工具过程；不要回复\"无法访问/无法查询\"。"
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


def ask_claude(text: str, user_id: str = "default") -> str:
    """微信消息 -> agent 跑一轮 -> 回复文本。维护该 user_id 的 agent 会话。

    main.py 是同步长轮询，这里用 asyncio.run 为每条消息起一个临时 event loop
    驱动 agent；会话连续性靠 resume=<session_id> 维持（session 存在 claude 的
    磁盘 transcript 里，跨 loop/进程可恢复）。
    """
    sid = _sessions.get(user_id)
    try:
        answer, new_sid = asyncio.run(
            asyncio.wait_for(_agent_turn(text, sid, user_id), timeout=TURN_TIMEOUT)
        )
    except asyncio.TimeoutError:
        return f"⏳ 处理超时（>{TURN_TIMEOUT}s），换个简单点的问题再试。"
    except Exception as e:
        return f"⚠️ agent 出错：{e}"
    if new_sid:
        _sessions[user_id] = new_sid
    return answer


def reset_user(user_id: str) -> None:
    """清空某用户的 agent 会话（用户发"重置"时调用，下条消息开新会话）。"""
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
