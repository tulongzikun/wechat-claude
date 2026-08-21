"""iLink + Claude 微信 Bot 主程序。

启动后：
    1. 从 token.json 读取已登录的 bot token（没有就先扫码登录）
    2. 进入长轮询循环，收到文本消息 -> 丢给 Claude 生成回复 -> 原路发回
    3. 回复必须带上消息自带的 context_token，否则微信不知道回给谁
"""

import json
import os
import time

from claude import (
    handle_monitor_command,
    list_jobs,
    reset_user,
    submit_background,
    submit_fresh,
    try_run_inline,
)
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
# 预估超过这个值 → 直接转后台跑（不阻塞主循环、不撞 agent 180s 超时）
BG_THRESHOLD = 40


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


# 每个用户最近一次入站消息的 context_token。后台作业完成时用它把结果推回微信
# （后台作业跨越多条消息，不能只锁死派发那一刻的 token——用最新的路由更稳）。
_latest_ctx: dict[str, str] = {}

# 把 latest_ctx 落盘，供【外部定时任务】（如每日 17:00 的项目更新推送）读取——
# 那些任务在 bot 进程外运行，要主动推送就得有可路由的 context_token。
_LATEST_CTX_FILE = os.path.join(_DIR, "latest_ctx.json")


def _save_latest_ctx() -> None:
    try:
        with open(_LATEST_CTX_FILE, "w", encoding="utf-8") as f:
            json.dump(_latest_ctx, f, ensure_ascii=False)
    except OSError:
        pass


def push_back(client: ILinkClient, user_id: str, text: str) -> None:
    """后台作业完成后，用该用户最新的 context_token 把结果推回微信。"""
    ctx = _latest_ctx.get(user_id)
    if not ctx:
        print(f"{_ts()} ⚠️ [{user_id}] 无可用 context_token，后台结果无法回推")
        return
    try:
        client.send_message(user_id, ctx, text)
        print(f"{_ts()} 📨 -> [{user_id}] (后台回推) "
              f"{text[:60]}{'...' if len(text) > 60 else ''}")
    except Exception as e:
        print(f"{_ts()} ⚠️ [{user_id}] 后台回推失败：{e}")


_BG_PREFIXES = ("/bg ",)   # 指令只认 / 前缀（裸 /bg 在分流里单独给用法提示）


def is_bg_command(stripped: str) -> bool:
    return any(stripped.startswith(p) for p in _BG_PREFIXES)


def parse_bg_task(stripped: str) -> str:
    for p in _BG_PREFIXES:
        if stripped.startswith(p):
            return stripped[len(p):].strip()
    return stripped


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
                _latest_ctx[user_id] = context_token  # 后台作业回推要用最新的
                _save_latest_ctx()                    # 落盘，供外部定时推送任务用
                stripped = text.strip()
                print(f"{_ts()} 👤 [{user_id}] {text}")

                # ---- 指令分流 ----

                # /reset：清空该用户的 agent 会话（同 /exit）
                if stripped == "/reset":
                    reset_user(user_id)
                    client.send_message(user_id, context_token,
                                        "已清空对话历史，可以重新开始啦 ✨")

                # /jobs：看自己的后台作业状态
                elif stripped == "/jobs":
                    running = [j for j in list_jobs(user_id)
                               if j.get("status") == "running"]
                    if running:
                        body = "\n".join(
                            f"• job={j['id']}（{j.get('snippet', '')}）" for j in running)
                        client.send_message(user_id, context_token,
                                            "🔄 进行中的后台作业：\n" + body)
                    else:
                        client.send_message(user_id, context_token,
                                            "没有正在跑的后台作业。")

                # 会话/子进程监控：/sessions /tail /use /exit /del /procs /help
                #（claude.py 实现，非监控指令返回 None 落到后面的正常分发）
                elif (mon := handle_monitor_command(stripped, user_id)) is not None:
                    client.send_message(user_id, context_token, mon)

                # 裸 /bg：给用法（带任务的 "/bg xxx" 走下面的分支）
                elif stripped == "/bg":
                    client.send_message(user_id, context_token,
                                        "用法：/bg <要办的事>，例如 /bg 读 novel/outline.md 并续写第三章")

                # /bg <任务>：显式后台跑
                elif is_bg_command(stripped):
                    task = parse_bg_task(stripped)
                    if not task:
                        client.send_message(user_id, context_token,
                                            "用法：/bg <要办的事>，例如 /bg 读 novel/outline.md 并续写第三章")
                        continue
                    job_id = submit_background(
                        task, user_id,
                        on_done=lambda r, uid=user_id: push_back(
                            client, uid, f"✅ 后台任务完成：\n{r}"),
                    )
                    if job_id is None:
                        client.send_message(user_id, context_token,
                                            "⏳ 上一条还在跑，等它完成（/jobs 看状态）或发 /exit 后再试。")
                    else:
                        client.send_message(user_id, context_token,
                                            f"🚀 已派后台 job={job_id}，跑完发回这里。\n任务：{task[:80]}")
                        print(f"{_ts()} 🚀 -> [{user_id}] 后台 job={job_id}：{task[:60]}")

                # /new <任务>：另起【全新会话】的后台子进程跑（不 resume 历史、
                # 不占当前会话、可与当前对话并行），完成推回；/use <job_id> 可续它
                elif stripped == "/new" or stripped.startswith("/new "):
                    task = stripped[4:].strip()
                    if not task:
                        client.send_message(user_id, context_token,
                                            "用法：/new <要办的事>——另起全新会话后台跑，"
                                            "不带当前对话历史，完成发回这里。")
                        continue
                    job_id = submit_fresh(
                        task, user_id,
                        on_done=lambda r, uid=user_id: push_back(
                            client, uid, f"✅ 新会话任务完成：\n{r}"),
                    )
                    if job_id is None:
                        client.send_message(user_id, context_token,
                                            "⏳ 你同时跑的新会话任务已到上限（2 个），等一个完成再发（/jobs 看状态）。")
                    else:
                        client.send_message(user_id, context_token,
                                            f"🆕 已新起子进程 job={job_id}（全新会话、不带历史、"
                                            f"不影响当前对话），跑完发回这里。\n任务：{task[:80]}")
                        print(f"{_ts()} 🆕 -> [{user_id}] 全新会话 job={job_id}：{task[:60]}")

                else:
                    est = estimate_seconds(text)
                    if est >= BG_THRESHOLD:
                        # 预估较久 → 自动转后台，主循环不阻塞、不撞 agent 超时
                        job_id = submit_background(
                            text, user_id,
                            on_done=lambda r, uid=user_id: push_back(
                                client, uid, f"✅ 完成：\n{r}"),
                        )
                        if job_id is None:
                            client.send_message(user_id, context_token,
                                                "⏳ 上一条还在跑，等它完成或发 /exit。")
                        else:
                            client.send_message(user_id, context_token,
                                                f"⏳ 这个要点时间，已转后台 job={job_id}，跑完发回。")
                            print(f"{_ts()} ⏳ -> [{user_id}] 自动转后台 job={job_id}（est={est}s）")
                    else:
                        # 短任务：同步跑（仅短暂阻塞主循环）
                        if est >= HINT_THRESHOLD:
                            client.send_message(user_id, context_token,
                                                f"⏳ 预计约 {est}s，稍等…")
                        reply = try_run_inline(text, user_id)
                        if reply is None:
                            client.send_message(user_id, context_token,
                                                "⏳ 上一条还在处理，等完成或发 /exit。")
                        else:
                            client.send_message(user_id, context_token, reply)
                            print(f"{_ts()} 🤖 -> [{user_id}] "
                                  f"{reply[:60]}{'...' if len(reply) > 60 else ''}")

        except KeyboardInterrupt:
            print("\n👋 收到退出信号，Bye~")
            break
        except Exception as e:
            # 任何异常都不要让主循环挂掉，记日志后退避重试
            print(f"⚠️ 发生错误：{e}，5 秒后重试 ...")
            time.sleep(5)


if __name__ == "__main__":
    main()
