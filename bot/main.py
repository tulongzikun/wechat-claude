"""iLink + Claude 微信 Bot 主程序。

启动后：
    1. 从 token.json 读取已登录的 bot token（没有就先扫码登录）
    2. 进入长轮询循环，收到文本消息
    3. 回复必须带上消息自带的 context_token，否则微信不知道回给谁

2026-08-30 精简：对话统一走 tmux 常驻会话（tmux_be）——普通消息注入即
返回，claude 跑完一轮由 Stop hook（tmux_hook.py）推回微信，没有超时窗口
（原 SDK 执行层 /bg /new 及内联 agent 退役，claude.py 只剩会话监控指令）。
"""

import json
import os
import time

import tmux_be
from claude import ADMIN_USERS, handle_monitor_command
from ilink import ILinkClient
from login import load_token, login


def _ts() -> str:
    """日志时间戳 HH:MM:SS。"""
    return time.strftime("%H:%M:%S")


# 游标持久化：重启后接着上次的位置拉消息，避免服务端重复投递已处理的旧消息
_DIR = os.path.dirname(os.path.abspath(__file__))
CURSOR_FILE = os.path.join(_DIR, "cursor.json")


def load_cursor() -> str:
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return json.load(f).get("get_updates_buf", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


def save_cursor(buf: str) -> None:
    try:
        with open(CURSOR_FILE, "w", encoding="utf-8") as f:
            json.dump({"get_updates_buf": buf}, f)
    except OSError:
        pass


def extract_text(msg: dict) -> str:
    """从一条 iLink 消息里安全取出文本内容。"""
    try:
        return msg["item_list"][0]["text_item"]["text"]
    except (KeyError, IndexError, TypeError):
        return ""


# 每个用户最近一次入站消息的 context_token，落盘供 hook（bot 进程外的
# 独立进程）推送时读取——常驻会话的回复全靠它路由回微信。
_LATEST_CTX_FILE = os.path.join(_DIR, "latest_ctx.json")
_latest_ctx: dict[str, str] = {}


def _save_latest_ctx() -> None:
    try:
        with open(_LATEST_CTX_FILE, "w", encoding="utf-8") as f:
            json.dump(_latest_ctx, f, ensure_ascii=False)
    except OSError:
        pass


def _is_admin(user_id: str) -> bool:
    """准入：白名单没设 = 自用全放开；设了就只认名单内的人。"""
    return not ADMIN_USERS or user_id in ADMIN_USERS


def main() -> None:
    # 1. 拿 token（本地有就用本地的，没有就走扫码登录）
    token_data = load_token()
    if token_data is None:
        bot_token, baseurl = login()
    else:
        bot_token = token_data["token"]
        baseurl = token_data.get("baseurl", "")

    client = ILinkClient(bot_token=bot_token, baseurl=baseurl)
    print("🤖 Bot 已上线，开始监听消息 ...")

    # 2. 长轮询主循环
    get_updates_buf = load_cursor()
    while True:
        try:
            result = client.get_updates(get_updates_buf)
            # 下一轮的游标，必须更新并持久化（重启不丢、不重复投递旧消息）
            get_updates_buf = result.get("get_updates_buf", get_updates_buf)
            save_cursor(get_updates_buf)

            for msg in result.get("msgs", []):
                text = extract_text(msg)
                if not text:
                    continue

                context_token = msg.get("context_token", "")
                user_id = msg.get("from_user_id", "")
                _latest_ctx[user_id] = context_token  # hook 推回要用最新的
                _save_latest_ctx()                    # 落盘，供 hook 进程读
                stripped = text.strip()
                print(f"{_ts()} 👤 [{user_id}] {text}")

                # ---- 指令分流 ----

                # /reset：结束常驻会话（下条消息自动开新的；历史留盘可找回）
                if stripped == "/reset":
                    tmux_be.kill_session()
                    client.send_message(user_id, context_token,
                                        "👋 常驻会话已结束，下条消息自动新开"
                                        "（不带旧上下文；历史 /sessions 可查、"
                                        "/use 可找回）。")

                # 会话监控：/sessions /tail /use /del /help
                #（claude.py 实现，非监控指令返回 None 落到下面的分发）
                elif (mon := handle_monitor_command(stripped, user_id)) is not None:
                    client.send_message(user_id, context_token, mon)

                # /screen：常驻会话状态 + 当前画面（看进度 / 排队 / 死因）
                elif stripped == "/screen":
                    head = tmux_be.status()
                    if not tmux_be.has_session():
                        client.send_message(user_id, context_token,
                                            f"{head}\n（未运行，发条消息会自动创建）")
                    else:
                        client.send_message(user_id, context_token,
                                            (head + "\n---\n"
                                             + tmux_be.capture(40))[:2000])

                # /esc：打断常驻会话当前回合（插队说新话）
                elif stripped == "/esc":
                    tmux_be.send_escape()
                    client.send_message(user_id, context_token, "⏹️ 已发送打断（Esc）。")

                # /tap <键>：向常驻会话透传按键（权限确认选 1/2、C-c 等）
                elif stripped == "/tap" or stripped.startswith("/tap "):
                    keys = stripped[4:].strip()
                    if not keys:
                        client.send_message(user_id, context_token,
                                            "用法：/tap <键>（tmux 键名，如 1 / y / Enter / "
                                            "C-c / Up，可多个空格分隔）")
                    else:
                        tmux_be.send_keys(keys)
                        client.send_message(user_id, context_token, f"⌨️ 已发送：{keys}")

                # /file <路径> [说明]：把服务器上的文件/图片/视频发到微信（按扩展名路由）。
                # 这是任意文件读取通道，仅管理员可用。
                elif stripped == "/file" or stripped.startswith("/file "):
                    parts = stripped.split(None, 2)
                    path = os.path.expanduser(parts[1]) if len(parts) > 1 else ""
                    caption = parts[2] if len(parts) > 2 else ""
                    if not _is_admin(user_id):
                        client.send_message(user_id, context_token, "⚠️ /file 仅管理员可用。")
                    elif not path:
                        client.send_message(user_id, context_token,
                                            "用法：/file <服务器上的文件路径> [附言]，例如 /file ~/workspace/wechat/README.md 项目说明")
                    elif not os.path.isfile(path):
                        client.send_message(user_id, context_token, f"⚠️ 文件不存在：{path}")
                    elif os.path.getsize(path) > 30 * 1024 * 1024:
                        client.send_message(user_id, context_token, "⚠️ 文件超过 30MB，不发。")
                    else:
                        try:
                            client.send_media(user_id, context_token, path,
                                              caption or f"📄 {os.path.basename(path)}")
                            client.send_message(user_id, context_token, f"✅ 已发送：{path}")
                            print(f"{_ts()} 📎 -> [{user_id}] 文件 {path}")
                        except Exception as e:
                            client.send_message(user_id, context_token, f"⚠️ 发送失败：{e}")
                            print(f"{_ts()} ⚠️ [{user_id}] 文件发送失败 {path}：{e}")

                # 其他未知 / 指令：别当消息灌进会话
                elif stripped.startswith("/"):
                    client.send_message(user_id, context_token,
                                        "未知指令，发 /help 看指令一览。")

                # 普通消息：直接进 tmux 常驻会话（唯一对话路径）。
                # 注入即返回不阻塞；空闲即答，在跑会排队（不丢），
                # 回复由 Stop hook 推回。仅管理员（会话有 Bash 全工具）。
                else:
                    if not _is_admin(user_id):
                        client.send_message(user_id, context_token,
                                            "⚠️ 本 bot 仅管理员可用。")
                        continue
                    tmux_be.bind_user(user_id)
                    r = tmux_be.ensure_session()
                    if r in ("ok", "restarted"):
                        tmux_be.send_text(text)
                        print(f"{_ts()} 📨 -> [{user_id}] "
                              f"tmux<{tmux_be.SESSION}>：{text[:60]}")
                    else:
                        client.send_message(user_id, context_token,
                                            f"⚠️ 常驻会话不可用：{r}（/screen 看详情）")

        except KeyboardInterrupt:
            print("\n👋 收到退出信号，Bye~")
            break
        except Exception as e:
            # 任何异常都不要让主循环挂掉，记日志后退避重试
            print(f"⚠️ 发生错误：{e}，5 秒后重试 ...")
            time.sleep(5)


if __name__ == "__main__":
    main()
