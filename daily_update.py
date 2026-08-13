#!/usr/bin/env python3
"""每日拉取 ~/workspace 下所有项目的 mainline 更新 → Claude 总结 → 主动推送微信。

由 cron 每日 17:00 触发（见 daily_update.sh + crontab）。

设计要点：
- 只 `git fetch`，**不 merge / 不 checkout**，绝不碰工作区。
- **窗口语义**：每次报告【昨日 17:00 之后落到 mainline 的所有 commit】（按 committer
  date），不依赖 fetch 时机 / 手动重跑——即便某 commit 之前已被 fetch、也已出现在
  origin/master，只要其提交时间在窗口内就照常总结。这是确定的日历窗口，无状态。
- 每个仓库按 mainline 分支（origin/master → main，**不看 origin/HEAD**）。
- 用 anthropic SDK 总结（自动走 ANTHROPIC_BASE_URL 网关 + ANTHROPIC_AUTH_TOKEN，
  模型用 HAIKU，便宜够用）。
- 推送走 ilink.ILinkClient + token.json；收件人 = WECHAT_ADMIN_USERS，
  context_token 从 latest_ctx.json 取（bot 每次收消息时落盘）。

调试：python3 daily_update.py --dry-run   只 fetch+总结+打印，不推送。
"""

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
WORKSPACE = Path.home() / "workspace"
LATEST_CTX_FILE = DIR / "latest_ctx.json"   # {user_id: context_token}（bot 落盘）
TOKEN_FILE = DIR / "token.json"

FETCH_TIMEOUT = 60            # 单仓库 fetch 超时
MAX_COMMITS_PER_REPO = 30     # 每仓库喂给模型的提交上限（防超长）
MAX_REPLY_LEN = 1800          # 微信长文体验差，超出截断

# 模型：优先用网关配的 haiku 别名，回退到默认模型名
MODEL = (
    os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or "claude-haiku-4-5"
)

DRY_RUN = "--dry-run" in sys.argv


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------- 磁盘小工具 ----------

def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


# ---------- 时间窗口 ----------

# 报告时区由 daily_update.sh 的 `export TZ="${CRON_TZ:-Asia/Shanghai}"` 设定，
# 复用 cron 注入的 CRON_TZ（cronie 已实测会传进 job 环境），与 cron 触发时区一致——
# 单一来源、零硬编码：换时区只改 crontab 的 CRON_TZ，脚本无需改动。
# 下午 5 点是「每日报告」的业务锚点。
REPORT_HOUR = 17


def since_yesterday_5pm() -> str:
    """报告时区的昨日 17:00（窗口起点）。

    时区由进程 TZ 决定（= cron 的 CRON_TZ，默认 Asia/Shanghai），与 cron 触发点对齐
    成完整 24h。返回 naive 时间串，`git log --since` 按进程 TZ 解释——所以不写死
    offset、不依赖具体地区，换时区零改动。
    """
    now = datetime.datetime.now()  # naive，按 TZ env（= CRON_TZ）
    y = (now - datetime.timedelta(days=1)).replace(
        hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    return y.strftime("%Y-%m-%d %H:%M:%S")


# ---------- git ----------

def git(repo: Path, *args: str) -> tuple[int, str]:
    """跑 git，返回 (rc, stdout)。超时/异常不抛，由调用方判断。"""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=FETCH_TIMEOUT,
        )
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return 1, f"<git error: {e}>"


def default_branch(repo: Path) -> str | None:
    """该仓库的 mainline 分支：origin/master → origin/main（取存在的第一个），都没有返回 None。

    故意不看 origin/HEAD——它是「当前 checkout 的远程默认分支」，某些仓会指向 docs/
    feature 分支（如 notes 指向 docs/ic-daily-global），那样会漏掉真正主线上的提交。
    统一以 origin/master（兜底 main）为每个仓的主线。
    """
    for b in ("master", "main"):
        rc, _ = git(repo, "rev-parse", "--verify", f"origin/{b}")
        if rc == 0:
            return b
    return None


def collect_updates(since: str) -> list[dict]:
    """遍历所有仓库，返回窗口内有提交的仓库列表。

    窗口 = since 之后、按 committer date 落到 mainline 的 commit。无状态、可重复：
    每次跑都报同一个日历窗口，不受 fetch 时机或之前是否已 fetch 影响。
    """
    repos = sorted(p for p in WORKSPACE.iterdir() if (p / ".git").is_dir())
    updates = []
    for repo in repos:
        name = repo.name
        rc, _ = git(repo, "fetch", "--quiet", "origin")
        if rc != 0:
            log(f"  ⚠️ {name}: fetch 失败，沿用本地 origin 引用")

        branch = default_branch(repo)
        if not branch:
            log(f"  ⊘ {name}: 无 origin/master|main，跳过")
            continue

        _, body = git(repo, "log", f"--since={since}", "--format=%h %s",
                      f"origin/{branch}")
        commits = [l for l in body.splitlines() if l.strip()]
        if not commits:
            log(f"  • {name}: 窗口内无提交")
            continue

        capped = commits[:MAX_COMMITS_PER_REPO]
        updates.append({
            "name": name, "branch": branch,
            "count": len(commits), "commits": capped,
            "truncated": len(commits) > len(capped),
        })
        tail = "（截断）" if len(commits) > len(capped) else ""
        log(f"  ▲ {name}({branch}): 窗口内 {len(commits)} 条{tail}")
    return updates


# ---------- 总结 ----------

def summarize(updates: list[dict], since: str) -> str | None:
    """把窗口内提交喂给 Claude，生成适合微信阅读的中文摘要。无内容返回 None。"""
    if not updates:
        return None

    total = sum(u["count"] for u in updates)
    sections = []
    for u in updates:
        tag = f"（窗口内 {u['count']} 条" + ("，已截断" if u["truncated"] else "") + "）"
        sections.append(f"### {u['name']}（{u['branch']}）{tag}\n"
                        + "\n".join(u["commits"]))
    raw = "\n\n".join(sections)

    prompt = (
        f"下面是我多个代码仓库【自 {since}（昨日17:00）起】落到 mainline 的 git 提交记录"
        f"（共 {total} 条，{len(updates)} 个仓库）。"
        "请生成一份给微信看的【每日更新摘要】，要求：\n"
        "1. 开头一句总体概述（这段时间主要在推进什么）。\n"
        "2. 按仓库分节，每节先一句话概括这批改动的目的，再列 2-4 条要点（合并同义的细碎提交）。\n"
        "3. 突出有意义的业务/逻辑变化，忽略纯格式、merge、typo、CI 微调。\n"
        "4. 用 emoji 标注类型：✨新功能 🐛修复 ♻️重构 📊数据/回测 🧪测试 📝文档 🔧配置/构建。\n"
        "5. 控制在 1200 字内，适合手机阅读；用 markdown 列表，不要大段文字。\n\n"
        f"提交记录：\n{raw}"
    )

    from anthropic import Anthropic  # 延迟 import：顶部不加载重模块
    client = Anthropic()  # 自动用 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL
    r = client.messages.create(
        model=MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
    return text or None


# ---------- 推送 ----------

def recipients() -> list[tuple[str, str]]:
    """[(user_id, context_token)]。admin 用户（未设则全部已知用户）× 各自最新 token。"""
    ctx_map = load_json(LATEST_CTX_FILE, {})
    raw = os.environ.get("WECHAT_ADMIN_USERS", "")
    admins = {u.strip() for u in raw.split(",") if u.strip()}
    users = admins if admins else set(ctx_map)
    out = []
    for uid in users:
        tok = ctx_map.get(uid)
        if tok:
            out.append((uid, tok))
        else:
            log(f"  ⚠️ {uid}: latest_ctx.json 无 context_token，跳过（先给 bot 发条消息即可）")
    return out


def push(text: str) -> int:
    tok = load_json(TOKEN_FILE, {})
    if not tok.get("token"):
        log("❌ token.json 无 bot token，无法推送")
        return 0
    from ilink import ILinkClient  # 延迟 import
    client = ILinkClient(bot_token=tok["token"], baseurl=tok.get("baseurl", ""))
    sent = 0
    for uid, ctok in recipients():
        try:
            client.send_message(uid, ctok, text)
            sent += 1
            log(f"  📨 已推送 -> {uid}")
        except Exception as e:
            log(f"  ⚠️ 推送 {uid} 失败: {e}")
    return sent


def main() -> None:
    log(f"=== 每日项目更新摘要 开始{'（dry-run）' if DRY_RUN else ''} ===")
    since = since_yesterday_5pm()
    tz_name = os.environ.get("TZ", "(未设TZ)")
    log(f"报告窗口起点：{since}（{tz_name} 昨日17:00）")
    updates = collect_updates(since)
    summary = summarize(updates, since)

    if summary is None:
        msg = (f"📭 {time.strftime('%m-%d')} 自昨日17:00起，"
               f"~/workspace 各项目 mainline 无新提交。")
        log("无更新。")
        if DRY_RUN:
            print(msg)
            return
        push(msg)
        return

    total = sum(u["count"] for u in updates)
    header = (f"📢 项目每日更新（{time.strftime('%m-%d')}）\n"
              f"自昨日17:00起，{len(updates)} 个仓库共 {total} 条新提交")
    full = header + "\n\n" + summary
    if len(full) > MAX_REPLY_LEN:
        full = full[:MAX_REPLY_LEN] + "\n…(已截断)"

    if DRY_RUN:
        print("\n" + full + "\n")
        return

    log("总结完成，开始推送…")
    n = push(full)
    log(f"=== 完成，推送 {n} 人 ===")


if __name__ == "__main__":
    main()
