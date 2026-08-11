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


def _ts() -> str:
    """日志时间戳 HH:MM:SS，用于测收消息→发回复的端到端延迟。"""
    return time.strftime("%H:%M:%S")


# ---- 回复耗时预估（只用于决定要不要先发一条"请稍等"提示）----
# 纯关键词规则、不调模型、零延迟，估不准也无妨——目的只是让用户在等较久任务时
# 知道 bot 还活着。真正耗时仍由 agent 决定，这里只给个体感数字。
_TOOL_WORDS = (
    "查看", "查询", "搜索", "搜", "读取", "跑", "执行", "运行", "进程", "文件",
    "日志", "目录", "统计", "找", "分析", "安装", "部署", "重启", "配置", "脚本",
    "代码", "最新", "历史", "会话", "列表", "哪些", "所有", "比较", "检查", "监控",
)
# 预估超过这个值才先发提示（普通对话 ~9s，等得起就不打扰）
HINT_THRESHOLD = 20


def estimate_seconds(text: str) -> int:
    """粗估这条消息的回复耗时（秒）。命中越多工具/多步关键词 → 越久。"""
    hits = sum(1 for w in _TOOL_WORDS if w in text)
    if hits == 0:
        return 8            # 普通对话/知识问答
    if hits <= 2:
        return 30           # 单一工具任务（查个进程/读个文件）
    return 60               # 多步探索任务（查会话最新回复/分析/对比）


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
                print(f"{_ts()} 👤 [{user_id}] {text}")

                # 简单指令：发"重置"清空该用户的对话历史
                if text.strip() in ("重置", "reset", "/reset"):
                    reset_user(user_id)
                    reply = "已清空对话历史，可以重新开始啦 ✨"
                else:
                    # 预估耗时：较久的任务先回一条"预计 Xs"，免得用户干等以为没响应
                    est = estimate_seconds(text)
                    if est >= HINT_THRESHOLD:
                        client.send_message(
                            user_id, context_token,
                            f"⏳ 这个问题要点时间，预计约 {est}s，请稍等…")
                        print(f"{_ts()} ⏳ -> [{user_id}] 预估 {est}s，已先发提示")
                    reply = ask_claude(text, user_id)

                # 3. 回复 —— context_token 原样带回（微信靠它路由到对应对话）
                client.send_message(user_id, context_token, reply)
                print(f"{_ts()} 🤖 -> [{user_id}] {reply[:60]}{'...' if len(reply) > 60 else ''}")

        except KeyboardInterrupt:
            print("\n👋 收到退出信号，Bye~")
            break
        except Exception as e:
            # 任何异常都不要让主循环挂掉，记日志后退避重试
            print(f"⚠️ 发生错误：{e}，5 秒后重试 ...")
            time.sleep(5)


if __name__ == "__main__":
    main()
