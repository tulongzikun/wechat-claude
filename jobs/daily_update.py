#!/usr/bin/env python3
"""每日拉取 ~/workspace 下所有项目的 mainline 更新 → Claude 总结 → 主动推送微信。

由 cron 每日 17:00 触发（run.sh daily_update + crontab）。

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
- 目录分层：本文件在 jobs/（定时任务），微信通讯层在 ../bot/
  （token.json / latest_ctx.json / ilink.py 都在那边）。

调试：python3 daily_update.py --dry-run   只 fetch+总结+打印，不推送。
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent          # jobs/（定时任务层）
BOT_DIR = DIR.parent / "bot"                   # bot/（微信通讯层，token / latest_ctx 在这里）
WORKSPACE = Path.home() / "workspace"
LATEST_CTX_FILE = BOT_DIR / "latest_ctx.json"  # {user_id: context_token}（bot 落盘）
TOKEN_FILE = BOT_DIR / "token.json"

# ilink 客户端在 bot/ 下，push 回退要用（sys.path 是给延迟 import `from ilink import ...` 的）
sys.path.insert(0, str(BOT_DIR))

FETCH_TIMEOUT = 60            # 单仓库 fetch 超时
MAX_COMMITS_PER_REPO = 30     # 每仓库喂给模型的提交上限（防超长）
MAX_REPLY_LEN = 1800          # 字符上限（体验约束，次级）
MAX_REPLY_BYTES = 3600        # 字节上限（硬约束）：企微 markdown 限 4096 字节，
                              # 中文 1 字=3 字节，1800 字≈5400 字节会超——所以
                              # 真正的限制是字节不是字符，超了整行删减绝不半句截断

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

# 报告时区由 run.sh 的 `export TZ="${CRON_TZ:-Asia/Shanghai}"` 设定，
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


def repo_filter() -> str:
    """仓库过滤子串：origin remote URL 里必须包含（如内网 git 域名）。空 = 不过滤。

    配置在 .env 的 DAILY_REPO_FILTER，按需启用——比如只想汇总内网仓库
    （git 内网域名）时设为该域名，个人/开源仓自动排除。域名本身视为敏感信息，
    只放 .env（gitignore），代码里用占位示例。
    """
    return os.environ.get("DAILY_REPO_FILTER", "").strip()


def collect_updates(since: str) -> list[dict]:
    """遍历所有仓库，返回窗口内有提交的仓库列表。

    窗口 = since 之后、按 committer date 落到 mainline 的 commit。无状态、可重复：
    每次跑都报同一个日历窗口，不受 fetch 时机或之前是否已 fetch 影响。
    """
    repos = sorted(p for p in WORKSPACE.iterdir() if (p / ".git").is_dir())
    flt = repo_filter()
    if flt:
        log(f"仓库过滤：origin 含「{flt}」")
    updates = []
    for repo in repos:
        name = repo.name
        if flt:
            _, url = git(repo, "remote", "get-url", "origin")
            if flt not in url:
                log(f"  ⊘ {name}: origin 非目标域，跳过")
                continue
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

def fit_bytes(text: str, budget: int = MAX_REPLY_BYTES) -> str:
    """按 UTF-8 字节预算裁剪：超了从末尾【整行】删，绝不半句截断。

    企微 markdown 上限 4096 字节（中文 1 字=3 字节），字符数限制管不住字节；
    半句截断会把链接/排名切成废字，所以宁可整条少一篇，末尾标注省略行数。
    """
    b = text.encode("utf-8")
    if len(b) <= budget:
        return text
    lines = text.rstrip().split("\n")
    dropped = 0
    while len(lines) > 4 and len(("\n".join(lines)).encode("utf-8")) + 64 > budget:
        lines.pop()
        dropped += 1
    return "\n".join(lines) + f"\n…（超长，整行省略 {dropped} 条）"


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


def _gather_hooks(raw: str, out: list, seen_vars: set, depth: int = 0) -> None:
    """把一段 webhook 配置值里的地址收进 out（递归展开代号）。

    代号 = 环境里已定义的 `HOOK_` 开头变量名（如 HOOK_A、HOOK_MOYU_XIEHUI）——
    引用时写变量名本身，值可以是单个地址或逗号分隔多个（代号套代号 ≤3 层，
    seen_vars 防环）。行内 `#` 注释剥离；既不是 http(s) 地址、也不是已定义
    HOOK_* 变量名的碎片直接丢弃。
    """
    if depth > 3:
        return
    for tok in re.split(r"[,\s]+", raw.split("#", 1)[0].strip()):
        if tok.startswith(("http://", "https://")):
            out.append(tok)
        elif re.fullmatch(r"HOOK_[A-Za-z0-9_]+", tok) and tok not in seen_vars:
            v = os.environ.get(tok, "")
            if v:
                _gather_hooks(v, out, seen_vars | {tok}, depth + 1)


def wecom_hooks(hook_env: str = "WECOM_WEBHOOK") -> list[str]:
    """按任务解析企微 webhook 列表（可推多个群）。

    取值：`hook_env` 指定的任务专用变量（如 WECOM_WEBHOOK_PAPERS），为空回落
    通用 WECOM_WEBHOOK。单个变量里可写多个地址（逗号/空白/换行分隔）——
    一份内容同时推到所有群，去重保序。值里可用 ` #` 写行内注释；地址可写成
    代号（= 已定义的 HOOK_* 变量名，如 HOOK_A）。配了值却一个群都解析不出时
    打警告（多半是代号拼错/未定义）。
    """
    raw = os.environ.get(hook_env, "") or os.environ.get("WECOM_WEBHOOK", "")
    hooks: list = []
    _gather_hooks(raw, hooks, {hook_env})
    seen, uniq = set(), []
    for h in hooks:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    if not uniq and raw.split("#", 1)[0].strip():
        log(f"⚠️ {hook_env} 有配置但没解析出任何群地址（代号未定义或拼写错误？）"
            f"：{raw.split('#', 1)[0].strip()}")
    return uniq


def push_wecom(text: str, hook_env: str = "WECOM_WEBHOOK") -> int:
    """企业微信群机器人 webhook 推送（markdown），支持多群。返回成功群数。

    主动推送、无 context_token 依赖——ilink 的 24h token 过期问题在这里不存在。
    webhook 列表来自 wecom_hooks()：任务专用变量 → 通用兜底，可逗号分隔多个。
    markdown content 上限 4096 字节，超长按字节安全截断（decode ignore）。
    """
    hooks = wecom_hooks(hook_env)
    if not hooks:
        return 0
    import requests  # 延迟 import
    b = text.encode("utf-8")
    content = text if len(b) <= 4000 else b[:4000].decode("utf-8", "ignore") + "\n…(已截断)"
    sent = 0
    for i, hook in enumerate(hooks, 1):
        tag = f"{i}/{len(hooks)}" if len(hooks) > 1 else ""
        try:
            r = requests.post(hook, json={"msgtype": "markdown",
                                          "markdown": {"content": content}}, timeout=15)
            r.raise_for_status()
            d = r.json()
            if d.get("errcode") != 0:
                log(f"  ⚠️ 企微推送失败{tag}: {d}")
                continue
            log(f"  📨 已推送 -> 企微群机器人{tag}")
            sent += 1
        except Exception as e:
            log(f"  ⚠️ 企微推送失败{tag}: {e}")
    return sent


_WECOM_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


def push_wecom_media(file_path: str, hook_env: str = "WECOM_WEBHOOK") -> int:
    """企微群机器人推送图片/文件（区别于 push_wecom 的纯文本/markdown）。

    - 图片（按扩展名，≤2MB）：{msgtype:image, base64+md5} 直接发，无需上传；
    - 其他文件（≤20MB）：先 POST upload_media（multipart，字段名 media）换
      media_id（仅 3 天有效、绑定该机器人 key——每个群各自的 key 都要传一遍），
      再 {msgtype:file, file:{media_id}} 发送。
    webhook key 从各群地址的 ?key= 里取。返回成功群数。
    """
    hooks = wecom_hooks(hook_env)
    if not hooks:
        return 0
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    import requests  # 延迟 import

    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    is_image = ext in _WECOM_IMAGE_EXTS
    size = os.path.getsize(file_path)
    if is_image and size > 2 * 1024 * 1024:
        log(f"  ⚠️ 企微图片上限 2MB，跳过（{file_path} {size}B）")
        return 0
    if not is_image and size > 20 * 1024 * 1024:
        log(f"  ⚠️ 企微文件上限 20MB，跳过（{file_path} {size}B）")
        return 0

    sent = 0
    for i, hook in enumerate(hooks, 1):
        tag = f"{i}/{len(hooks)}" if len(hooks) > 1 else ""
        try:
            if is_image:
                raw = open(file_path, "rb").read()
                body = {"msgtype": "image",
                        "image": {"base64": base64.b64encode(raw).decode(),
                                  "md5": hashlib.md5(raw).hexdigest()}}
            else:
                key = parse_qs(urlparse(hook).query).get("key", [""])[0]
                if not key:
                    log(f"  ⚠️ 企微 webhook{tag} 无 key，跳过文件上传")
                    continue
                up = requests.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media",
                    params={"key": key, "type": "file"},
                    files={"media": open(file_path, "rb")}, timeout=60)
                d = up.json()
                if d.get("errcode") != 0:
                    log(f"  ⚠️ 企微文件上传失败{tag}: {d}")
                    continue
                body = {"msgtype": "file", "file": {"media_id": d["media_id"]}}
            r = requests.post(hook, json=body, timeout=15)
            r.raise_for_status()
            d = r.json()
            if d.get("errcode") != 0:
                log(f"  ⚠️ 企微媒体推送失败{tag}: {d}")
                continue
            log(f"  📨 已推送 -> 企微群机器人{tag}（{'图片' if is_image else '文件'}）")
            sent += 1
        except Exception as e:
            log(f"  ⚠️ 企微媒体推送失败{tag}: {e}")
    return sent


def push_ilink(text: str) -> int:
    """ilink 微信推送。context_token 来自 latest_ctx.json（bot 落盘），约 24h 时效。"""
    tok = load_json(TOKEN_FILE, {})
    if not tok.get("token"):
        log("⚠️ token.json 无 bot token，跳过 ilink 通道")
        return 0
    from ilink import ILinkClient  # 延迟 import
    client = ILinkClient(bot_token=tok["token"], baseurl=tok.get("baseurl", ""))
    sent = 0
    for uid, ctok in recipients():
        try:
            r = client.send_message(uid, ctok, text)
            ret = r.get("ret", 0) if isinstance(r, dict) else 0
            if ret != 0:
                # send_message 只在 HTTP 层 raise（raise_for_status），业务错误在 json 里：
                # ret=-2 "prepare failed" = context_token 过期（约 24h 时效）
                log(f"  ⚠️ 推送 {uid} 失败: ret={ret} {r.get('errmsg', '')}"
                    "（多半是 context_token 过期——先给 bot 发条消息刷新 latest_ctx）")
                continue
            sent += 1
            log(f"  📨 已推送 -> 微信 {uid}")
        except Exception as e:
            log(f"  ⚠️ 推送 {uid} 失败: {e}")
    return sent


def push(text: str, hook_env: str = "WECOM_WEBHOOK") -> int:
    """双通道同步推送：企微群 webhook + ilink 个人微信，各推一份、互不影响。

    hook_env 指定本任务专用的企微 webhook 变量名（缺省通用 WECOM_WEBHOOK），
    解析顺序：专用 → 通用兜底；变量里可写多个地址（逗号/空白分隔）推多个群。
    企微是主动通道（无条件成功）；ilink 需 24h 内有互动，过期只降级不阻断。
    返回成功送达的通道数（企微按群计 + ilink 按人计；0=全没送达）。
    """
    sent = 0
    # 1) 企业微信群机器人 webhook——主动推送，不依赖用户互动 / context_token
    if push_wecom(text, hook_env=hook_env):
        sent += 1
    # 2) ilink——个人微信同步一份；token 过期只是少了这份，不影响企微结果
    if push_ilink(text):
        sent += 1
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
        push(msg, hook_env="WECOM_WEBHOOK_DAILY")
        return

    total = sum(u["count"] for u in updates)
    header = (f"📢 项目每日更新（{time.strftime('%m-%d')}）\n"
              f"自昨日17:00起，{len(updates)} 个仓库共 {total} 条新提交")
    full = header + "\n\n" + summary
    full = fit_bytes(full)   # 字节预算硬约束（企微 4096B），整行删减不半句截断

    if DRY_RUN:
        print("\n" + full + "\n")
        return

    log("总结完成，开始推送…")
    n = push(full, hook_env="WECOM_WEBHOOK_DAILY")
    log(f"=== 完成，推送 {n} 人 ===")


if __name__ == "__main__":
    main()
