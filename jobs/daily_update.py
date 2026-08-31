#!/usr/bin/env python3
"""每日监控 GHE 组织下所有仓库的 mainline 更新 → Claude 总结 → 主动推送微信。

由 cron 每日 17:00 触发（run.sh daily_update + crontab）。

设计要点：
- **数据源是 GitHub Enterprise API，不是本地 workspace**——监控范围 = GHE_ORGS
  里各组织名下的全部仓库（含本地没 clone 的），每天先刷新 org 仓库清单落盘到
  jobs/repository.txt（副产物：既是人可读的监控名单，也是 API 不可用时的回退源），
  再逐仓查 default branch 在窗口内的提交。零本地 clone、零 git 依赖。
- **窗口语义**：每次报告【昨日 17:00 之后落到 mainline 的所有 commit】（按 committer
  date），确定的日历窗口、无状态——commits API 的 since 过滤与原 git log --since 同义
  （已实测两边逐条一致）。
- **鉴权**：GHE_API（如 https://<内网域名>/api/v3）+ GHE_AUTH（user:password，Basic），
  都放 .env（域名/凭证敏感不入库）；GHE_AUTH 未设时自动从 ~/.git-credentials 里
  找该 host 的凭证（bot 与 cron 同一 OS 用户，等价可用）。
- 用 anthropic SDK 总结（自动走 ANTHROPIC_BASE_URL 网关 + ANTHROPIC_AUTH_TOKEN，
  模型用 HAIKU，便宜够用）。
- 推送走 ilink.ILinkClient + token.json；收件人 = WECHAT_ADMIN_USERS，
  context_token 从 latest_ctx.json 取（bot 每次收消息时落盘）。
- 目录分层：本文件在 jobs/（定时任务），微信通讯层在 ../bot/
  （token.json / latest_ctx.json / ilink.py 都在那边）。

调试：python3 daily_update.py --dry-run   只拉取+总结+打印，不推送。
"""

import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from common import MODEL, llm_text, log   # 共用 LLM 样板 / 模型名 / 日志（jobs/ 内）

DIR = Path(__file__).resolve().parent          # jobs/（定时任务层）
BOT_DIR = DIR.parent / "bot"                   # bot/（微信通讯层，token / latest_ctx 在这里）
LATEST_CTX_FILE = BOT_DIR / "latest_ctx.json"  # {user_id: context_token}（bot 落盘）
TOKEN_FILE = BOT_DIR / "token.json"
REPO_LIST_FILE = DIR / "repository.txt"        # 监控名单副产物（gitignore，不入库）

# ilink 客户端在 bot/ 下，push 回退要用（sys.path 是给延迟 import `from ilink import ...` 的）
sys.path.insert(0, str(BOT_DIR))

API_TIMEOUT = 20              # 单次 GHE API 超时（秒）
MAX_COMMITS_PER_REPO = 30     # 每仓库喂给模型的提交上限（防超长）
MAX_REPLY_LEN = 1800          # 字符上限（体验约束，次级）
MAX_REPLY_BYTES = 3600        # 字节上限（硬约束）：企微 markdown 限 4096 字节，
                              # 中文 1 字=3 字节，1800 字≈5400 字节会超——所以
                              # 真正的限制是字节不是字符，超了整行删减绝不半句截断

DRY_RUN = "--dry-run" in sys.argv


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
    成完整 24h。返回 naive 时间串，to_utc_iso() 再转成 API 要的 UTC——所以不写死
    offset、不依赖具体地区，换时区零改动。
    """
    now = datetime.datetime.now()  # naive，按 TZ env（= CRON_TZ）
    y = (now - datetime.timedelta(days=1)).replace(
        hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    return y.strftime("%Y-%m-%d %H:%M:%S")


def to_utc_iso(local_naive: str) -> str:
    """naive 本地时间串（按进程 TZ 解释）→ UTC ISO8601（commits API 的 since 参数）。

    commits API 的 since/分页里的时间都是 UTC；本地窗口串按 TZ env 解释成 epoch
    再 gmtime 输出，与 git log --since（本地 TZ 解释）完全同窗。
    """
    st = time.strptime(local_naive, "%Y-%m-%d %H:%M:%S")
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.mktime(st)))


# ---------- GHE API ----------

# API 基址与组织列表放 .env（内网域名敏感不入库）；未配置时明确报错而不是静默空跑。
GHE_API = os.environ.get("GHE_API", "").rstrip("/")
GHE_ORGS = [o.strip() for o in os.environ.get("GHE_ORGS", "").split(",") if o.strip()]


def _ghe_auth() -> str | None:
    """GHE API 的 Authorization 头（Basic）。

    优先 .env 的 GHE_AUTH="user:password"；未设则从 ~/.git-credentials 里找
    GHE_API 对应 host 的凭证（本任务与 git 同一 OS 用户，凭证同源；密码含特殊
    字符时 credential store 已按 percent-encoding 存，解开即原值）。
    """
    explicit = os.environ.get("GHE_AUTH", "").strip()
    if explicit:
        return "Basic " + base64.b64encode(explicit.encode()).decode()
    cred_file = Path.home() / ".git-credentials"
    try:
        host = urllib.parse.urlparse(GHE_API).hostname
        for line in cred_file.read_text().splitlines():
            u = urllib.parse.urlparse(line.strip())
            if u.hostname == host and u.username and u.password:
                pw = urllib.parse.unquote(u.password)
                return "Basic " + base64.b64encode(f"{u.username}:{pw}".encode()).decode()
    except OSError:
        pass
    return None


def ghe_get(path: str, **params) -> list | dict | None:
    """GET {GHE_API}{path}?params，返回解析后的 json；失败（网络/40x）返回 None。"""
    import requests  # 延迟 import
    try:
        r = requests.get(f"{GHE_API}{path}", params=params, timeout=API_TIMEOUT,
                         headers={"Authorization": _ghe_auth() or "",
                                  "User-Agent": "daily-update"},)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  ⚠️ GHE API {path} 失败：{type(e).__name__} {str(e)[:100]}")
        return None


def list_org_repos(org: str) -> list[dict] | None:
    """某组织名下全部仓库（分页拉全）。首页就失败返回 None（区别于真的 0 仓）。"""
    repos, page = [], 1
    while True:
        batch = ghe_get(f"/orgs/{org}/repos", per_page=100, page=page, type="all")
        if batch is None:
            return None if page == 1 else repos  # 翻页挂：用已拉到的部分
        repos += batch
        if len(batch) < 100:
            return repos
        page += 1


def _read_repo_list_file() -> list[dict]:
    """把落盘名单读回监控条目（pushed_at 未知 → None，collect 时不跳过）。"""
    if not REPO_LIST_FILE.exists():
        return []
    out = []
    for l in REPO_LIST_FILE.read_text(encoding="utf-8").splitlines():
        p = l.split()
        if p:
            out.append({"full_name": p[0],
                        "default_branch": p[1] if len(p) > 1 else "master",
                        "pushed_at": None})
    return out


def refresh_repo_list() -> list[dict]:
    """刷新监控名单：GHE_ORGS 各 org 的仓库全量 → 落盘 repository.txt，返回列表。

    每行 "owner/repo default分支"（人可读、也是 API 挂掉时的回退源）。某个 org
    拉取失败时从旧名单补上该 org 的条目（别让一次抖动把半个名单冲掉）；全部失败
    且没有旧名单可回退时直接抛错——宁可不推也不能装作"无提交"（同 08-27 谎报原则）。
    """
    if not GHE_API or not GHE_ORGS:
        raise RuntimeError("GHE_API / GHE_ORGS 未配置（应在 .env 里）")
    fresh, failed_orgs = [], []
    for org in GHE_ORGS:
        got = list_org_repos(org)
        if got is None:
            failed_orgs.append(org)
        else:
            fresh += got
    stale = _read_repo_list_file()
    if failed_orgs:
        # 失败 org 的旧条目顶上（没有旧的就认缺失，log 里说清楚）
        keep = [s for s in stale if s["full_name"].split("/")[0] in failed_orgs]
        if keep:
            log(f"  ⚠️ org {','.join(failed_orgs)} 列表拉取失败，"
                f"沿用旧名单 {len(keep)} 个条目")
        else:
            log(f"  ⚠️ org {','.join(failed_orgs)} 列表拉取失败且无旧条目可补")
        fresh += keep
    if not fresh:
        if stale:
            log(f"  ⚠️ 全部 org 列表拉取失败，回退 {REPO_LIST_FILE.name}"
                f"（{len(stale)} 个，可能不含最新建仓）")
            return stale
        raise RuntimeError("org 仓库列表拉取失败，且无 repository.txt 可回退")
    fresh.sort(key=lambda r: r["full_name"])
    lines = [f"{r['full_name']} {r.get('default_branch') or 'master'}" for r in fresh]
    REPO_LIST_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"监控名单：{len(fresh)} 个仓库（{', '.join(GHE_ORGS)}）→ {REPO_LIST_FILE.name}")
    return fresh


def collect_updates(since: str) -> list[dict]:
    """遍历监控名单里的所有仓库，返回窗口内有提交的仓库列表。

    窗口 = since（本地 naive 串）之后、按 committer date 落到 default 分支的 commit。
    无状态、可重复：每次跑都报同一个日历窗口。commits API 的 since 与 git log
    --since 同义（committer date、按 UTC 比较），已实测与本地 git log 逐条一致。

    防谎报（同 08-27 原则）：任一仓库查询失败会记数；若最终一个更新都没收集到
    但存在失败，直接抛错让 cron 日志留痕——绝不把"查不到"包装成"无提交"。
    """
    since_utc = to_utc_iso(since)
    updates, failed = [], 0
    for r in refresh_repo_list():
        name, branch = r["full_name"], r.get("default_branch") or "master"
        # pushed_at 预筛：全仓最后一次 push 都早于窗口起点 ⇒ 任何分支都不可能有
        # 窗口内提交，省一次 commits 调用（回退名单无此信息时不跳）
        pushed = r.get("pushed_at")
        if pushed and pushed < since_utc:
            log(f"  • {name}: 窗口内无提交")
            continue
        d = ghe_get(f"/repos/{name}/commits", sha=branch, since=since_utc,
                    per_page=100)
        if d is None:
            # 回退名单的分支信息可能过期（404），换另一个主线名再试一次
            alt = "main" if branch == "master" else "master"
            d = ghe_get(f"/repos/{name}/commits", sha=alt, since=since_utc,
                        per_page=100)
            if d is not None:
                branch = alt
        if d is None:
            failed += 1
            continue
        commits = [f"{c['sha'][:8]} {c['commit']['message'].splitlines()[0]}".rstrip()
                   for c in d if isinstance(c, dict)]
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
    if not updates and failed:
        raise RuntimeError(f"{failed} 个仓库提交查询失败且无任何收集结果，疑似 API/鉴权故障")
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

    # thinking=disabled + 重试 + 空文本检测内建在 common.llm_text（08-27 空文本
    # 被当"无提交"谎报的教训）；失败返回 None，由 main 走确定性兜底。
    return llm_text(prompt, label="摘要 LLM")


def _plain_digest(updates: list[dict]) -> str:
    """LLM 摘要不可用时的确定性兜底：各仓提交标题平铺（超长由 fit_bytes 整行截）。"""
    lines = []
    for u in updates:
        cs = "；".join(c for c in u["commits"][:6])
        more = f"…（共 {u['count']} 条）" if u["count"] > 6 else ""
        lines.append(f"• {u['name']}({u['branch']})：{cs}{more}")
    return "\n".join(lines)


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
    if not updates:
        msg = (f"📭 {time.strftime('%m-%d')} 自昨日17:00起，"
               f"~/workspace 各项目 mainline 无新提交。")
        log("无更新。")
        if DRY_RUN:
            print(msg)
            return
        push(msg, hook_env="WECOM_WEBHOOK_DAILY")
        return

    summary = summarize(updates, since)
    if summary is None:
        # LLM 空输出 ≠ 无提交（08-27 17:00 曾因此谎报过"无新提交"）：退化为确定性列表
        log("  ⚠️ AI 摘要两试均空，退化为原始提交列表推送")
        summary = ("⚠️ AI 摘要生成失败（网关异常），原始提交列表：\n"
                   + _plain_digest(updates))

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
