"""tmux 常驻会话后端 —— 一个交互式 claude 跑在 tmux 里,微信只是控制面。

与 claude.py(SDK 后端,每条消息 spawn 新进程)的根本区别:
- 会话常驻:没有"单轮超时"概念,任务想跑多久跑多久;
- prompt cache 常热:不再每条消息 resume 重放全量 transcript,调用更省、
  也更不易撞网关限频;
- 天然串行:同一时间一个回合,消息排队不丢(急事 /esc 打断插队);
- 控制面解耦:手机微信异步使唤(/t),坐到电脑前 `tmux attach -t wxclaude`
  全交互接管——两边看到的是同一个 claude。

消息流:
    微信 /t <文本>  → send_text() 注入 tmux(瞬间返回,永不阻塞收消息循环)
    claude 跑完一轮 → Stop hook(tmux_hook.py)读 transcript 尾部 → 推回微信
    等权限确认      → Notification hook 推提醒 → 微信 /tap <键> 透传回应

tmux 会话独立于 bot 进程生命周期:bot 重启不丢会话;claude 崩了 pane 留尸
(remain-on-exit,可 /screen 查死因),ensure_session() 检测后重建。
"""

import json
import os
import shlex
import shutil
import subprocess
import time

_DIR = os.path.dirname(os.path.abspath(__file__))

# tmux 会话名 / agent 工作区(与 SDK 后端 AGENT_CWD 对齐,可环境变量覆盖)
SESSION = os.environ.get("WECHAT_TMUX_SESSION", "wxclaude")
CWD = os.environ.get("WECHAT_AGENT_CWD", "/home/zhouzikun")
# 挂到这个 claude 上的额外 settings(--settings):hooks + 权限白名单
SETTINGS_FILE = os.path.join(_DIR, "tmux_settings.json")
# 会话 ↔ 微信用户绑定(hook 推送目标),由 main.py 的 /t 写入
STATE_FILE = os.path.join(_DIR, "tmux_state.json")

_TMUX = shutil.which("tmux")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([_TMUX, *args], capture_output=True, text=True, timeout=15)


def has_session() -> bool:
    return _run(["has-session", "-t", SESSION]).returncode == 0


def _pane_dead() -> bool:
    """会话的首 pane 是否已死(claude 退出/崩溃后 remain-on-exit 留尸)。"""
    r = _run(["list-panes", "-t", SESSION, "-F", "#{pane_dead}"])
    if r.returncode != 0:
        return True
    return r.stdout.strip().splitlines()[:1] == ["1"]


def ensure_session() -> str:
    """确保 tmux 会话活着并跑着 claude。返回 "ok" / "restarted" / 失败原因。"""
    if has_session() and not _pane_dead():
        return "ok"
    if has_session():                       # pane 死了:留尸可查,这里整个重建
        _run(["kill-session", "-t", SESSION])
    claude = shutil.which("claude")
    if not claude:
        return "找不到 claude 可执行文件(PATH)"
    # ⚠️ tmux server 可能是别的环境起的,new-session 不继承本进程环境,
    # 所以把鉴权/代理/PATH 显式写进 pane 的启动命令里,claude 才连得上网关。
    passthrough = {
        k: v for k, v in os.environ.items()
        if k.startswith(("ANTHROPIC_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                         "http_proxy", "https_proxy", "no_proxy"))
    }
    passthrough["PATH"] = os.environ.get("PATH", "")
    env_cmd = " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in passthrough.items())
    cmd = (f"{env_cmd} exec {shlex.quote(claude)} "
           f"--settings {shlex.quote(SETTINGS_FILE)} --permission-mode acceptEdits")
    r = _run(["new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50",
              "-c", CWD, cmd])
    if r.returncode != 0:
        return f"tmux new-session 失败:{r.stderr.strip()}"
    _run(["set-option", "-t", SESSION, "remain-on-exit", "on"])
    return "restarted"


def kill_session() -> None:
    if has_session():
        _run(["kill-session", "-t", SESSION])


def send_text(text: str) -> None:
    """把一段文本投进会话输入框并回车提交。
    多行用 bracketed paste(claude 视作一条多行消息,不会逐行触发提交)。"""
    if "\n" in text or "\r" in text:
        subprocess.run([_TMUX, "load-buffer", "-"], input=text, text=True, timeout=15)
        _run(["paste-buffer", "-p", "-t", SESSION])
    else:
        _run(["send-keys", "-t", SESSION, "-l", "--", text])
    _run(["send-keys", "-t", SESSION, "Enter"])


def send_keys(keys: str) -> None:
    """透传按键(tmux 键名:1 / y / Enter / C-c / Up …,可多个,空格分隔)。"""
    _run(["send-keys", "-t", SESSION, *keys.split()])


def send_escape() -> None:
    """打断当前回合(claude 的 Esc)。"""
    _run(["send-keys", "-t", SESSION, "Escape"])


def capture(lines: int = 40) -> str:
    """抓会话当前画面(尾部 lines 行),供 /screen 看进度/死因。"""
    r = _run(["capture-pane", "-t", SESSION, "-p", "-S", f"-{lines}"])
    out = "\n".join(ln.rstrip() for ln in r.stdout.splitlines())
    while "\n\n\n" in out:                  # 压掉 3+ 连续空行
        out = out.replace("\n\n\n", "\n\n")
    return out.strip()


def bind_user(user_id: str) -> None:
    """把会话绑定到某微信用户(hook 推送的目标)。"""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"user": user_id, "bound_at": time.time()}, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def bound_user() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("user")
    except (OSError, json.JSONDecodeError):
        return None


def status() -> str:
    alive = has_session() and not _pane_dead()
    return (f"tmux 会话 {SESSION}:{'🟢 claude 在跑' if alive else '🔴 未运行'}"
            f" · 绑定微信用户 {bound_user() or '(无)'} · 工作区 {CWD}")


if __name__ == "__main__":
    # 自测:拉起会话,等 claude TUI 渲染,抓屏看是否就绪
    print("ensure_session:", ensure_session())
    time.sleep(6)
    print(status())
    print("--- capture ---")
    print(capture(30))
