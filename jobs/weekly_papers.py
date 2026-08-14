#!/usr/bin/env python3
"""每周一抓 arXiv q-fin 上周新论文 → 关键词粗筛 → Claude 按主题总结 → 推送微信。

由 cron 每周一 10:00 触发（见 weekly_papers.sh + crontab，CRON_TZ=Asia/Shanghai）。

设计要点：
- 数据源 arXiv q-fin（cat:q-fin*），走 https（本机 80 端口封、443 通）。
- 窗口 = 报告时区（TZ env = CRON_TZ，默认 Asia/Shanghai）的上个自然周
  （周一 00:00 ~ 周日 23:59）。周一 10:00 推送时正好覆盖刚结束的完整上周。
- 六主题关键词粗筛（期货/股票/趋势/多因子/择时/量化），再让 Claude 按影响力
  （创新性/实盘相关性/跨学科可迁移性——新论文无引用数据的代理）精选 Top10，
  按主题分组中文总结，每篇一句点评 + arXiv 链接。
- 推送直接复用 daily_update.push（ilink + token.json + latest_ctx.json）。

调试：python3 weekly_papers.py --dry-run   只抓+筛+总结+打印，不推送。
"""

import datetime
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

from daily_update import push, MODEL   # 复用推送 + 模型名（haiku 走网关）

ARXIV_API = "https://export.arxiv.org/api/query"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
FETCH_TIMEOUT = 40
MAX_RESULTS = 200        # 单次拉取（q-fin 每周 ~50，200 够覆盖数周）
MAX_HITS = 40            # 喂给模型的论文上限（防 prompt 超长）
MAX_REPLY_LEN = 1800     # 微信长文截断

# 六主题关键词（匹配 标题 + 摘要）
TOPIC_KW = {
    "期货": re.compile(r"futures?|commodit", re.I),
    "股票": re.compile(r"stock|equit", re.I),
    "趋势": re.compile(r"trend|momentum", re.I),
    "多因子": re.compile(r"multi[- ]?factor|factor\s*(?:model|invest|exposure|zoo)", re.I),
    "择时": re.compile(r"\btiming\b|regime", re.I),
    "量化": re.compile(r"quant|\balpha\b|risk\s+premium|arbitrage|\bsignal", re.I),
}
TOPIC_ORDER = ["期货", "股票", "趋势", "多因子", "择时", "量化"]

DRY_RUN = "--dry-run" in sys.argv


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------- 时间窗口 ----------

def last_week_window() -> tuple[datetime.datetime, datetime.datetime]:
    """报告时区（TZ env = CRON_TZ）的上个自然周 [周一00:00, 下周一00:00)，aware。

    now().astimezone() 按 TZ env 给本地 aware；weekday() 周一=0。
    返回的 start/end 是 aware，可与论文 published（UTC aware）直接比较。
    """
    now = datetime.datetime.now().astimezone()
    this_monday = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return this_monday - datetime.timedelta(days=7), this_monday


# ---------- arXiv 抓取 ----------

def fetch_arxiv(max_results: int = MAX_RESULTS) -> list[dict]:
    """拉最近 q-fin 论文（按 submittedDate 降序），解析成列表。失败返回 []。"""
    url = (f"{ARXIV_API}?search_query=cat:q-fin*&max_results={max_results}"
           f"&sortBy=submittedDate&sortOrder=descending")
    log(f"抓取 arXiv q-fin（最多 {max_results} 篇）…")
    data = b""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weekly-papers/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                data = r.read()
            break
        except Exception as e:
            log(f"  抓取失败（第 {attempt + 1} 次）: {e}")
            if attempt == 0:
                time.sleep(3)
    if not data:
        log("❌ arXiv 抓取失败，放弃")
        return []

    papers = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        log(f"❌ XML 解析失败: {e}")
        return []
    for e in root.findall("a:entry", NS):
        def txt(path):
            n = e.find(path, NS)
            return n.text if n is not None else ""
        pub = txt("a:published")
        try:
            pub_dt = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            continue
        papers.append({
            "title": " ".join(txt("a:title").split()),
            "summary": " ".join(txt("a:summary").split()),
            "link": txt("a:id"),
            "published": pub_dt,  # aware UTC
            "categories": [c.get("term") for c in e.findall("a:category", NS)],
        })
    if papers:
        log(f"  拿到 {len(papers)} 篇"
            f"（{papers[0]['published']:%Y-%m-%d} ~ {papers[-1]['published']:%Y-%m-%d}）")
    else:
        log("  拿到 0 篇")
    return papers


# ---------- 筛选 ----------

def filter_papers(papers, start, end):
    """窗口内 + 关键词粗筛。返回 (命中列表, 窗口内总数)。

    论文 published 是 UTC aware，start/end 是本地 aware —— aware 间可直接比较。
    """
    in_window = [p for p in papers if start <= p["published"] < end]
    hits = []
    for p in in_window:
        text = p["title"] + " " + p["summary"]
        topics = [k for k in TOPIC_ORDER if TOPIC_KW[k].search(text)]
        if topics:
            p["topics"] = topics
            hits.append(p)
    return hits, len(in_window)


# ---------- 总结 ----------

def summarize(hits, start, end) -> str | None:
    """命中的论文喂 Claude，按主题分组中文摘要。无命中返回 None。"""
    if not hits:
        return None
    capped = hits[:MAX_HITS]
    items = []
    for i, p in enumerate(capped, 1):
        items.append(
            f"[{i}] {p['title']}\n"
            f"  主题:{'/'.join(p['topics'])} 分类:{','.join(p['categories'][:3])}\n"
            f"  摘要:{p['summary'][:400]}\n"
            f"  链接:{p['link']}"
        )
    raw = "\n\n".join(items)
    win = f"{start:%m-%d}~{end - datetime.timedelta(days=1):%m-%d}"
    top_n = min(10, len(capped))
    prompt = (
        f"下面是上周（{win}）arXiv q-fin 中命中【期货/股票/趋势交易/多因子/择时/量化模型】"
        f"主题的 {len(capped)} 篇论文（标题/主题标签/分类/摘要/链接）。\n"
        "请生成一份给微信看的【每周论文速递 Top10】，要求：\n"
        f"1. 只挑影响力/价值最大的 {top_n} 篇——新论文没有引用数据，按以下代理判断："
        "创新性（是否提出新方法/新结论而非增量改进）、与实盘量化交易的相关度和实用性、"
        "交叉列出 cs.LG/stat.ML 等且方法可迁移的优先。\n"
        "2. 开头一句总体概述：这周量化金融主要在推进哪些方向。\n"
        "3. 按主题分节：期货 / 股票 / 趋势交易 / 多因子 / 择时 / 量化模型；"
        "每篇归入最相关的一个主题，标注 [排名]，未入选的不再提及。\n"
        "4. 每篇格式：「一句中文点评（做了什么、结论/价值、为什么重要）」后跟 arXiv 链接。\n"
        "5. 用 markdown 列表，控制在 1200 字内，适合手机阅读、绝不能被截断。\n\n"
        f"论文列表：\n{raw}"
    )
    from anthropic import Anthropic  # 延迟 import
    client = Anthropic()  # 自动用 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL
    r = client.messages.create(
        model=MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
    return text or None


# ---------- 主流程 ----------

def main() -> None:
    log(f"=== 每周论文速递 开始{'（dry-run）' if DRY_RUN else ''} ===")
    tz = os.environ.get("TZ", "(未设TZ)")
    start, end = last_week_window()
    log(f"窗口：{start:%Y-%m-%d %a} ~ {end:%Y-%m-%d %a}（{tz} 上个自然周）")

    papers = fetch_arxiv()
    hits, total = filter_papers(papers, start, end)
    log(f"窗口内 {total} 篇，关键词命中 {len(hits)} 篇")
    summary = summarize(hits, start, end)

    win = f"{start:%m-%d}~{end - datetime.timedelta(days=1):%m-%d}"
    if summary is None:
        msg = (f"📭 每周论文速递（{win}）：上周 arXiv q-fin 共 {total} 篇，"
               f"均未命中期货/股票/趋势/多因子/择时/量化。")
        log("无命中。")
        if DRY_RUN:
            print(msg)
            return
        push(msg)
        return

    top_n = min(10, len(hits))
    header = (f"📚 每周论文速递 Top{top_n}（{win}）\n"
              f"arXiv q-fin 命中 {len(hits)}/{total} 篇，精选 {top_n} 篇"
              f"（{', '.join(TOPIC_ORDER)}）")
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
