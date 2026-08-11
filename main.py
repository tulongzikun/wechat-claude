"""iLink + Claude 微信 Bot 主程序。

启动后：
    1. 从 token.json 读取已登录的 bot token（没有就先扫码登录）
    2. 进入长轮询循环，收到文本消息 -> 丢给 Claude 生成回复 -> 原路发回
    3. 回复必须带上消息自带的 context_token，否则微信不知道回给谁
"""

import json
import os
import time

from claude import ask_claude, reset_user
from ilink import ILinkClient
from login import load_token, login

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
                print(f"👤 [{user_id}] {text}")

                # 简单指令：发"重置"清空该用户的对话历史
                if text.strip() in ("重置", "reset", "/reset"):
                    reset_user(user_id)
                    reply = "已清空对话历史，可以重新开始啦 ✨"
                else:
                    reply = ask_claude(text, user_id)

                # 3. 回复 —— context_token 原样带回（微信靠它路由到对应对话）
                client.send_message(user_id, context_token, reply)
                print(f"🤖 -> [{user_id}] {reply[:60]}{'...' if len(reply) > 60 else ''}")

        except KeyboardInterrupt:
            print("\n👋 收到退出信号，Bye~")
            break
        except Exception as e:
            # 任何异常都不要让主循环挂掉，记日志后退避重试
            print(f"⚠️ 发生错误：{e}，5 秒后重试 ...")
            time.sleep(5)


if __name__ == "__main__":
    main()
