#!/usr/bin/env python3
"""每周二抓米哈游游戏上周资讯 → Claude 分类总结 → 推送微信/企微。

由 cron 每周二 10:00 触发（见 games_news.sh + crontab，CRON_TZ=Asia/Shanghai）。

游戏：原神 / 崩坏：星穹铁道 / 绝区零 / 因缘精灵
内容：游戏更新、内鬼信息（未实装爆料）、联动信息。

数据源：Google News RSS（实测可达，支持 when:7d + 中文；Reddit 被挡、
米游社无公开 API）。每游戏两条查询（通用 + 内鬼/联动定向），按标题去重，
再按上个自然周过滤。分类只做关键词预标注，最终归组交给 Claude。

调试：python3 games_news.py --dry-run   只抓+总结+打印，不推送。
"""

import datetime
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from daily_update import push, MODEL, fit_bytes, log   # 复用推送/模型/字节预算

GNEWS = "https://news.google.com/rss/search"
FETCH_TIMEOUT = 30
MAX_PER_GAME = 20        # 每游戏喂给模型的条目上限
SUMMARY_BYTES = 3000     # 总结正文字节预算（企微 4096B 减标题/链接余量）

# 游戏：显示名 -> (检索词, 标题必须命中的别名表——防 Google 语义联想带进无关新闻)
GAMES = [
    ("原神", ("原神", ["原神", "Genshin"])),
    ("崩坏：星穹铁道", ("星穹铁道", ["星穹铁道", "星铁", "崩铁", "Star Rail"])),
    ("绝区零", ("绝区零", ["绝区零", "ZZZ", "Zenless"])),
    ("因缘精灵", ("因缘精灵", ["因缘精灵"])),
]

# 来源黑名单：屡产赌博导流假标题的农场站（试过白名单，误杀 17173/notebookcheck
# 等正经游戏媒体，放弃——黑名单 + 垃圾词 + 游戏别名三重过滤足够）
FARM_RE = re.compile(r"womenofchina\.com|cj\.sina\.cn", re.I)

# 每游戏附加的定向查询（未实装情报 与 联动），提高漏报召回
EXTRA_QUERIES = {
    "内鬼": "内鬼 OR 爆料 OR 未实装",
    "联动": "联动 OR 联名 OR 合作",
}

# 措辞脱敏：这些词会让智谱网关内容过滤报 1301（2026-08-17 实测），送给
# 模型前替换成中性说法——查询 Google 用原词（不经过网关），只有模型输入输出要脱敏。
SAFE_WORDS = [
    ("内鬼", "未实装情报"),
    ("爆料", "传闻"),
    ("泄露", "流出"),
    ("偷跑", "提前流出"),
]

def sanitize(text: str) -> str:
    for a, b in SAFE_WORDS:
        text = text.replace(a, b)
    return text

# 分类预标注（只用于给模型提示，最终归组由模型判断）；匹配的是脱敏后的标题。
# 「联动」正则不含裸「合作」——会用大量无关娱乐新闻误命中。
TOPIC_KW = {
    "更新": re.compile(
        r"更新|版本|维护|上线|实装|前瞻|直播|卡池|复刻|补丁|"
        r"PV|预告|实机|公开|发布|新角色|卫星|限定|活动|音乐会|动画"),
    "情报": re.compile(r"未实装|情报|传闻|流出|疑似"),
    "联动": re.compile(r"联动|联名|跨界|主题店|咖啡|官方合作"),
}

# 垃圾条目特征：Google News 中文游戏查询混入大量博彩/导流站和手机广告，
# 命中即整条丢弃（实测 2026-08-17：原神 159 条里近半是这类垃圾）
JUNK_RE = re.compile(
    r"网址|下载|官网版|注册|开户|登录|首存|彩金|投注|娱乐城|亚[博慱]体育|"
    r"vwin|体育APP|买球|赌|手机推荐|手机怎么选|帧率|散热|游戏手机|"
    r"性价比|评测.*手机|手机.*评测|彩票|皇冠|威尼斯|汇盛|太阳城|金沙|"
    r"会员开|打赏|返利|优惠|福利", re.I)

DRY_RUN = "--dry-run" in sys.argv

# ---------- hsr.nanoka.cc 星铁未实装新角色（静态 JSON 数据库） ----------
# 站点是 SvelteKit SPA，真实数据在 static.nanoka.cc：版本号从首页 HTML 里
# 正则提取（随官方版本更新），失败回落内置版本。release=0/未定 = 未实装。
NANOKA_HOME = "https://hsr.nanoka.cc/"
NANOKA_VER_FALLBACK = "4.4.54"


def http_get(url: str, timeout: int = FETCH_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 games-digest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_nanoka_upcoming() -> list[str]:
    """星铁未实装新角色列表（名字/星级/命途/属性/背景梗概）。失败返回 []。"""
    ver = NANOKA_VER_FALLBACK
    try:
        m = re.search(rb"static\.nanoka\.cc/hsr/([\d.]+)/character\.json", http_get(NANOKA_HOME))
        if m:
            ver = m.group(1).decode()
    except Exception as e:
        log(f"  ⚠️ nanoka 首页抓取失败: {e}")
    try:
        data = json.loads(http_get(f"https://static.nanoka.cc/hsr/{ver}/character.json"))
    except Exception as e:
        log(f"  ⚠️ nanoka character.json（{ver}）抓取失败: {e}")
        return []
    now = time.time()
    lines = []
    for cid, c in sorted(data.items()):
        rel = c.get("release", 0)
        if rel and rel <= now:   # 已实装跳过；未定档（rel=0）保留
            continue
        star = re.search(r"(\d)$", c.get("rank", ""))
        lines.append(
            f"- {c.get('zh', '?')}（{c.get('en', '?')}）"
            f"{star.group(1) if star else '?'}星 命途:{c.get('baseType', '?')} "
            f"属性:{c.get('damageType', '?')} 上线未定档。"
            f"背景：{(c.get('desc') or '').replace(chr(10), ' ')[:200]}")
    log(f"  nanoka：未实装新角色 {len(lines)} 名")
    return lines


# ---------- 时间窗口（与 weekly_papers 同语义：上个自然周 周一~周日）----------

def last_week_window() -> tuple[datetime.datetime, datetime.datetime]:
    now = datetime.datetime.now().astimezone()
    this_monday = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return this_monday - datetime.timedelta(days=7), this_monday


# ---------- Google News RSS ----------

def fetch_gnews(query: str) -> list[dict]:
    """拉 Google News RSS 一条查询，解析条目。失败返回 []。"""
    url = (f"{GNEWS}?q={urllib.parse.quote(query)}%20when:7d"
           f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    data = b""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 games-digest/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                data = r.read()
            break
        except Exception as e:
            log(f"  抓取失败（{query}，第 {attempt + 1} 次）: {e}")
            if attempt == 0:
                time.sleep(3)
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        log(f"  XML 解析失败（{query}）: {e}")
        return []
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        src_node = it.find("source")
        src = (src_node.text or "").strip() if src_node is not None else ""
        src_url = (src_node.get("url", "") if src_node is not None else "")
        try:  # RFC822 -> aware datetime
            pub_dt = email.utils.parsedate_to_datetime(pub)
        except Exception:
            continue
        if title and link:
            items.append({"title": sanitize(title), "link": link,
                          "published": pub_dt, "source": sanitize(src),
                          "src_url": src_url})
    return items


def collect(start, end) -> dict[str, list[dict]]:
    """每游戏拉 通用+内鬼+联动 三条查询，四重过滤后返回。"""
    out: dict[str, list[dict]] = {}
    for disp, (q, aliases) in GAMES:
        merged: dict[str, dict] = {}
        queries = [f'"{q}"'] + [f'"{q}" ({kw})' for kw in EXTRA_QUERIES.values()]
        for query in queries:
            for it in fetch_gnews(query):
                if not (start <= it["published"] < end):
                    continue
                key = re.sub(r"\s+", "", it["title"].split(" - ")[0]).lower()
                if key not in merged:
                    merged[key] = it
        items = sorted(merged.values(), key=lambda x: x["published"], reverse=True)
        # 三重过滤：①垃圾特征词+农场站 ②标题必须含游戏别名（Google 语义联想
        # 会把无关新闻带进来）③必须命中 更新/情报/联动 主题词
        kept = []
        for it in items:
            if JUNK_RE.search(it["title"]):
                continue
            if FARM_RE.search(it["src_url"]) or FARM_RE.search(it["source"]):
                continue
            if not any(a.lower() in it["title"].lower() for a in aliases):
                continue
            tags = [k for k, rx in TOPIC_KW.items() if rx.search(it["title"])]
            if tags:
                it["topics"] = tags
                kept.append(it)
        out[disp] = kept[:MAX_PER_GAME]
        log(f"  {disp}: 窗口内 {len(merged)} 条，过滤后 {len(kept)}，取前 {len(out[disp])}")
    return out


# ---------- 总结 ----------

def summarize(news: dict[str, list[dict]], start, end,
              hsr_upcoming: list[str] | None = None) -> str | None:
    if not any(news.values()) and not hsr_upcoming:
        return None
    raw_parts = []
    for game, items in news.items():
        if not items:
            continue
        lines = [f"## {game}"]
        for it in items:
            lines.append(f"- [{'+'.join(it['topics'])}] {it['title']}（{it['source']}）")
        raw_parts.append("\n".join(lines))
    if hsr_upcoming:
        raw_parts.append(
            "## 星穹铁道·未实装新角色（数据库 hsr.nanoka.cc，数据较可靠但未实装）\n"
            + "\n".join(hsr_upcoming))
    raw = "\n\n".join(raw_parts)
    win = f"{start:%m-%d}~{end - datetime.timedelta(days=1):%m-%d}"
    prompt = (
        f"下面是上周（{win}）Google News 收录的米哈游游戏中文资讯"
        f"（原神/崩坏：星穹铁道/绝区零/因缘精灵），每条带预标注主题、来源媒体名。\n"
        "请生成给微信看的【米哈游每周游戏资讯】，要求：\n"
        "1. 按游戏分节；每节内按 更新 / 未实装情报（传闻，注明以官方为准）/ 联动 归类，"
        "无关条目（股价、招聘、纯广告）舍弃。\n"
        "2. 每条：**一句话标题式概括**（加粗）+ 1~2 句详细说明（版本号/日期/角色名/"
        "机制数值/联动对象/上线时间等关键信息尽量写全）。**不要输出任何链接或 URL**，"
        "来源媒体名括注在句尾即可。同一事件多篇报道合并成一条，信息取并集。\n"
        "3. 开头一句总览；某游戏无论述就整节省略，不写「暂无消息」。\n"
        "4. 「未实装新角色」块：每名角色写 2 句中文介绍——翻译背景故事梗概、"
        "点明星级/命途/属性/预期定位，标注未实装。命途字段是内部英文名，对照："
        "Knight=存护、Warrior=毁灭、Rogue=巡猎、Mage=智识、Shaman=丰饶、"
        "Priest=虚无、Memory=记忆、Elation=欢愉，其余按官方命途译名。\n"
        "5. 措辞规范：未实装内容只写「传闻/未实装」，不要使用敏感措辞。\n"
        "6. 用 markdown 列表，全文严格 ≤1100 字——硬要求，超长会被系统整条删掉。\n\n"
        f"资讯列表：\n{raw}"
    )
    from anthropic import Anthropic  # 延迟 import
    client = Anthropic()
    try:
        r = client.messages.create(
            model=MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        # 网关内容过滤/模型不可用：退化为纯条目列表（不经模型，内容不受影响）
        log(f"  ⚠️ 模型总结失败（{e}），退化为纯条目列表")
        return plain_digest(news)
    text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
    if text and len(text.encode("utf-8")) > SUMMARY_BYTES:  # LLM 字数不自控，压缩一次
        log(f"  总结 {len(text.encode('utf-8'))} 字节超预算，压缩重生成…")
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=1500,
                messages=[{"role": "user", "content":
                           "下面这份游戏资讯太长了，必须压缩到 1100 字以内（不含链接，别加链接）。"
                           "保持游戏分节与 更新/未实装情报/联动 分类，删掉次要条目、保留每条最关键信息。\n\n" + text}],
            )
            t2 = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
            if t2:
                text = t2
        except Exception as e:
            log(f"  ⚠️ 压缩重生成失败，沿用初稿: {e}")
    return text or None


def plain_digest(news: dict[str, list[dict]], hsr_upcoming: list[str] | None = None) -> str:
    """模型不可用/被网关过滤时的兜底：不经模型，直接拼脱敏后的条目（无链接）。"""
    parts = []
    for game, items in news.items():
        if not items:
            continue
        lines = [f"### {game}"]
        for it in items[:10]:
            lines.append(f"- [{'+'.join(it['topics'])}] {it['title']}（{it['source']}）")
        parts.append("\n".join(lines))
    if hsr_upcoming:
        parts.append("### 星穹铁道·未实装新角色\n" + "\n".join(hsr_upcoming))
    return "\n\n".join(parts)


# ---------- 主流程 ----------

def main() -> None:
    log(f"=== 米哈游每周游戏资讯 开始{'（dry-run）' if DRY_RUN else ''} ===")
    tz = os.environ.get("TZ", "(未设TZ)")
    start, end = last_week_window()
    log(f"窗口：{start:%Y-%m-%d %a} ~ {end:%Y-%m-%d %a}（{tz} 上个自然周）")

    news = collect(start, end)
    total = sum(len(v) for v in news.values())
    hsr_up = fetch_nanoka_upcoming()
    summary = summarize(news, start, end, hsr_upcoming=hsr_up)

    win = f"{start:%m-%d}~{end - datetime.timedelta(days=1):%m-%d}"
    if summary is None:
        msg = f"📭 米哈游每周游戏资讯（{win}）：上周 Google News 未见相关报道。"
        log("无资讯。")
        if DRY_RUN:
            print(msg)
            return
        push(msg, hook_env="WECOM_WEBHOOK_GAMES")
        return

    header = (f"🎮 米哈游每周游戏资讯（{win}）\n"
              f"原神 / 星穹铁道 / 绝区零 / 因缘精灵 · 共 {total} 条来源，精选如下")
    full = fit_bytes(header + "\n\n" + summary)   # 字节预算硬约束，整行删减

    if DRY_RUN:
        print("\n" + full + "\n")
        return

    log("总结完成，开始推送…")
    n = push(full, hook_env="WECOM_WEBHOOK_GAMES")
    log(f"=== 完成，推送 {n} 通道 ===")


if __name__ == "__main__":
    main()
