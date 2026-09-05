#!/usr/bin/env python3
"""tmux 常驻会话的 Claude Code hook:Stop / Notification → 推回微信。

由 tmux_settings.json 挂到 wxclaude 会话的 claude 上(经 --settings 注入),
claude 每轮结束 / 需要关注时 spawn 本脚本,事件 JSON 从 stdin 进。

推送链路复用 jobs 外部定时任务的成熟模式:
    login.load_token() + ILinkClient + latest_ctx.json(最近 context_token)
目标用户来自 tmux_state.json(main.py 的 /t 指令写入的绑定关系)。

永远 exit 0:hook 失败不该影响 claude 本身的工作流。
"""

import json
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

MAX_LEN = 1800   # 微信长文体验差,超过截断
LOG_FILE = os.path.join(_DIR, "tmux_hook.log")


def _log(msg: str) -> None:
    """追加一行运行日志(hook 在 claude 子进程里跑,stderr 看不到,落盘排障)。"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _push(text: str) -> None:
    if not text:
        return
    _log(f"push 尝试:{text[:50]!r}")
    try:
        with open(os.path.join(_DIR, "tmux_state.json"), encoding="utf-8") as f:
            user = json.load(f).get("user")
    except (OSError, json.JSONDecodeError):
        user = None
    # 最近 context_token(main.py 持续落盘,专供外部进程回推)
    try:
        with open(os.path.join(_DIR, "latest_ctx.json"), encoding="utf-8") as f:
            ctx = (json.load(f) or {}).get(user or "", "")
    except (OSError, json.JSONDecodeError):
        ctx = ""
    if not user or not ctx:
        _log(f"跳过:无可推送目标(user={user!r})")
        return
    from ilink import ILinkClient
    from login import load_token
    td = load_token()
    if td is None:
        _log("跳过:token.json 缺失")
        return
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "\n…(已截断,/screen 看全屏)"
    client = ILinkClient(bot_token=td["token"], baseurl=td.get("baseurl", ""))
    client.send_message(user, ctx, text)
    _log(f"push 成功 -> {user}: {text[:60]!r}")


def _pane_tail(max_lines: int = 14, max_bytes: int = 900) -> str:
    """tmux 画面尾部（去空行）：权限确认时这就是确认框本体——待批命令和
    1/2/3 选项都在里面。拿不到（tmux 不在/刚重建）返回空，通知退回纯文案。"""
    try:
        import subprocess
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", "wxclaude", "-p"],
            capture_output=True, text=True, timeout=5)
        lines = [l.rstrip() for l in r.stdout.splitlines() if l.strip()]
        return "\n".join(lines[-max_lines:]).encode("utf-8")[:max_bytes].decode(
            "utf-8", "ignore")
    except Exception:
        return ""


def _wait_for_new_text(transcript_path: str, timeout: float = 5.0) -> str:
    """等本轮最终答案落盘。

    实测(Claude Code v2.1.227):Stop hook 触发时本轮的最终 assistant 文本
    **总是**还没写进 transcript——hook 读到的永远是上一轮的。所以不能用
    "文本是否非空"判断,要以行号为锚:启动时记下最后一条 assistant 文本行,
    轮询直到出现更靠后的新行;超时仍无 → 本轮没有新输出(被打断/空回合),
    不推送。"""
    idx0, _ = _last_assistant(transcript_path)
    deadline = time.time() + timeout
    while True:
        idx, t = _last_assistant(transcript_path)
        if t and idx > idx0:
            return t
        if time.time() >= deadline:
            return ""
        time.sleep(0.3)


def _last_assistant(transcript_path: str) -> tuple[int, str]:
    """transcript JSONL 从后往前找最后一条主链 assistant 文本行。

    返回 (行号, 文本);没有则 (-1, "")。行号是 _wait_for_new_text 的
    "本轮是否有新输出"锚点,别改成只返回文本。"""
    if not transcript_path:
        return -1, ""
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return -1, ""
    for i in range(len(lines) - 1, -1, -1):
        try:
            obj = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        else:
            parts = []
        t = "".join(parts).strip()
        if t:
            return i, t
    return -1, ""


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if event == "stop":
        t = _wait_for_new_text(data.get("transcript_path", ""))
        if t:
            _push(f"🤖(常驻会话):{t}")
        else:
            _log("stop:未等到新 assistant 文本(打断/空回合/超时),不推送")
    elif event == "notify":
        m = data.get("message") or "需要你关注"
        # 在批什么必须可见：只推"Claude needs your permission"时用户不知道
        # /tap 1 批的是什么——2026-09-04 由此卡了一整天（第二轮确认无人应、
        # 回合悬死、Stop hook 不触发，用户以为 /use 没恢复上下文）。
        panel = _pane_tail()
        body = f"🔔(常驻会话):{m}\n{panel}\n" if panel else f"🔔(常驻会话):{m}\n"
        _push(body + "(/tap <键> 回应,如 /tap 1;/esc 打断)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
