#!/usr/bin/env python3
"""每周一抓 arXiv q-fin 上周新论文 → 关键词粗筛 → Claude 按主题总结 → 推送微信。

由 cron 每周一 10:00 触发（run.sh weekly_papers + crontab，CRON_TZ=Asia/Shanghai）。

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
import xml.etree.ElementTree as ET

from common import http_get, llm_text, log          # 共用 LLM/抓取样板（防谎报内建）
from daily_update import push, fit_bytes            # 复用推送 + 字节预算裁剪

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_RECENT = "https://arxiv.org/list/q-fin/recent"   # 备胎：主站列表页（限流与 export 独立）
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
FETCH_TIMEOUT = 40
MAX_RESULTS = 200        # 单次拉取（q-fin 每周 ~50，200 够覆盖数周）
MAX_HITS = 40            # 喂给模型的论文上限（防 prompt 超长）
SUMMARY_BYTES = 3000     # 总结正文字节预算（企微 4096B 减去标题/链接余量）；
                         # 超了让模型压缩一次，再超由 fit_bytes 整行删减兜底

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

def fetch_arxiv(max_results: int = MAX_RESULTS) -> list[dict] | None:
    """拉最近 q-fin 论文（按 submittedDate 降序），解析成列表。

    返回 None = 抓取失败（网络/限流），[] = 抓到了但 0 篇——两者语义不同，
    main 里 None 直接走失败通知，绝不把失败伪装成"上周 0 篇"推送出去
    （2026-08-31 10:00 实际事故：arXiv 429 两次→📭"共 0 篇，均未命中"）。
    """
    url = (f"{ARXIV_API}?search_query=cat:q-fin*&max_results={max_results}"
           f"&sortBy=submittedDate&sortOrder=descending")
    log(f"抓取 arXiv q-fin（最多 {max_results} 篇）…")
    # 429 退避：arXiv 限流通常几分钟内恢复，3 秒重试等于白试（08-31 两连挂的教训）
    data = http_get(url, timeout=FETCH_TIMEOUT, attempts=4,
                    delays=(30, 60, 120), user_agent="weekly-papers/1.0")
    if data is None:
        log("❌ arXiv 抓取失败（重试耗尽）")
        return None

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


# ---------- API 备胎：主站列表页 ----------

def fetch_arxiv_listing(start, end) -> list[dict] | None:
    """API 被 429 时的备胎：arxiv.org 主站 recent 列表。

    主站与 export（API host）限流独立——2026-08-31 export 被公司 NAT 出口 IP
    429 两小时+未解封时主站列表页仍秒开（Scholar 无 API 且 403，月度页无日期，
    都不可用；recent 有按公告日分段的 h3 + dt/dd 题录 + skip/show 分页）。

    两步：① recent 翻页拿 窗口内的 (id, 公告日, 标题)——无摘要；② 只对标题
    关键词命中的逐篇 /abs 补摘要（1.5s 间隔，命中通常 10~25 篇，控制请求数）。
    口径差异（日志会注明）：比 API 少一层摘要关键词召回，标题不含主题词的
    论文会漏；published 取公告日 00:00 UTC（日粒度，周边界与 API 差≤1天）。
    失败返回 None；返回的列表含窗口内全部论文（未补摘要的 summary 为空，
    filter_papers 的 total 口径仍准确）。
    """
    day_re = re.compile(r"<h3[^>]*>(\w{3}, \d+ \w+ \d{4})[^<]*</h3>")
    ent_re = re.compile(
        # 注意 arXiv 列表页的 href 等号前有随机空格（href ="/abs/…"），别"顺手修齐"
        r'<dt>.*?href\s*=\s*"/abs/([\d.v]+)".*?list-title mathjax\'>.*?</span>\s*(.*?)\s*</div>', re.S)
    start_utc = start.astimezone(datetime.timezone.utc)
    end_utc = end.astimezone(datetime.timezone.utc)
    papers, skip, too_old = [], 0, False
    for _page in range(8):                      # 最多 8 页防死循环
        html = http_get(f"{ARXIV_RECENT}?skip={skip}&show=100",
                        timeout=FETCH_TIMEOUT, user_agent="weekly-papers/1.0")
        if html is None:
            log(f"  ⚠️ 列表页翻页失败（skip={skip}），用已拿到的 {len(papers)} 篇继续")
            break
        text = html.decode("utf-8", "ignore")
        blocks = day_re.split(text)             # [前言, 日1, 体1, 日2, 体2, ...]
        for i in range(1, len(blocks) - 1, 2):
            day = datetime.datetime.strptime(
                blocks[i], "%a, %d %b %Y").replace(tzinfo=datetime.timezone.utc)
            if day < start_utc:
                too_old = True                  # 更早的公告日不必再翻
                continue
            if day >= end_utc:
                continue
            for pid, title in ent_re.findall(blocks[i + 1]):
                papers.append({
                    "title": " ".join(title.split()), "summary": "",
                    "link": f"https://arxiv.org/abs/{pid}",
                    "published": day, "categories": [],
                })
        if too_old or not text.strip():
            break
        skip += len(ent_re.findall(text))       # 页内实际条目数（日段可能截断）
    if not papers:
        log("❌ 列表页备胎也失败")
        return None
    # 标题命中才补摘要（请求数收敛）；未命中的保留空摘要（total 口径不受影响）
    need = [p for p in papers
            if any(k.search(p["title"]) for k in TOPIC_KW.values())]
    log(f"  备胎口径：窗口内 {len(papers)} 篇，标题命中 {len(need)} 篇（逐篇补摘要）")
    for p in need:
        b = http_get(p["link"], timeout=FETCH_TIMEOUT, attempts=1,
                     user_agent="weekly-papers/1.0")
        if b:
            m = re.search(
                r'<blockquote class="abstract mathjax">\s*<span[^>]*>Abstract:</span>'
                r"(.*?)</blockquote>", b.decode("utf-8", "ignore"), re.S)
            if m:
                p["summary"] = " ".join(m.group(1).replace("</p>", " ").split())
        time.sleep(1.5)
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
        "每篇归入最相关的一个主题，不标排名编号，未入选的不再提及。\n"
        "4. 每篇格式：**论文英文原题**（保留原文标题、不翻译）——一句中文点评"
        "（≤30 字：做了什么、结论/价值）后跟 arXiv 链接。\n"
        "5. 用 markdown 列表，全文（含链接）严格控制在 900 字内——这是硬要求，"
        "超长会被系统整条删掉。\n\n"
        f"论文列表：\n{raw}"
    )
    # thinking=disabled + 重试 + 空文本检测内建在 common.llm_text（08-31 在本文件
    # 踩过：切 glm-5.3 首跑 thinking 吃光 max_tokens，12 篇命中被推成"均未命中"）
    text = llm_text(prompt, label="论文摘要 LLM")
    if text is None:
        return None
    # LLM 对字数不自控、逐次波动：超字节预算就明确要求压缩，重生成一次
    if len(text.encode("utf-8")) > SUMMARY_BYTES:
        log(f"  总结 {len(text.encode('utf-8'))} 字节超预算，压缩重生成…")
        t2 = llm_text(
            "下面这份论文速递太长了，必须压缩到 900 字以内（含链接）。"
            "保持 Top10、主题分节和每篇的英文原题，点评压到 ≤25 字，删掉修饰词。\n\n" + text,
            max_tokens=1500, retries=1, label="压缩重生成")
        if t2:
            text = t2
    return text or None


# ---------- 主流程 ----------

def main() -> None:
    log(f"=== 每周论文速递 开始{'（dry-run）' if DRY_RUN else ''} ===")
    tz = os.environ.get("TZ", "(未设TZ)")
    start, end = last_week_window()
    log(f"窗口：{start:%Y-%m-%d %a} ~ {end:%Y-%m-%d %a}（{tz} 上个自然周）")

    papers = fetch_arxiv()
    if papers is None:
        log("  ↩️ API 不可用（429/网络），切换备胎：arxiv.org 主站列表页")
        papers = fetch_arxiv_listing(start, end)
    if papers is None:
        # 抓取失败 ≠ 上周没有论文（08-31 事故根因）——推失败告警而非伪装成
        # "共 0 篇"；收到告警可手动重跑 run.sh weekly_papers（窗口是固定日历周，
        # 当天任何时间重跑结果一致）。
        msg = (f"⚠️ 每周论文速递（{start:%m-%d}~{end - datetime.timedelta(days=1):%m-%d}）："
               f"arXiv 抓取失败（429/网络），本期未生成。可手动重跑补发。")
        log("❌ 抓取失败，推送失败告警（不伪装成 0 篇）。")
        if DRY_RUN:
            print(msg)
            return
        push(msg, hook_env="WECOM_WEBHOOK_PAPERS")
        return
    hits, total = filter_papers(papers, start, end)
    log(f"窗口内 {total} 篇，关键词命中 {len(hits)} 篇")

    win = f"{start:%m-%d}~{end - datetime.timedelta(days=1):%m-%d}"
    if not hits:
        msg = (f"📭 每周论文速递（{win}）：上周 arXiv q-fin 共 {total} 篇，"
               f"均未命中期货/股票/趋势/多因子/择时/量化。")
        log("无命中。")
        if DRY_RUN:
            print(msg)
            return
        push(msg, hook_env="WECOM_WEBHOOK_PAPERS")
        return

    summary = summarize(hits, start, end)
    if summary is None:
        # LLM 失败 ≠ 零命中（08-31 事故变体：12 篇命中被推成"均未命中"）——
        # 退化成原始命中列表照常推送，绝不谎报
        log("  ⚠️ AI 摘要两试均空，退化为命中论文原始列表推送")
        plain = "\n".join(f"- **{p['title']}**\n  {p['link']}" for p in hits[:MAX_HITS])
        summary = (f"⚠️ AI 摘要生成失败（网关异常），命中 {len(hits)} 篇原始列表：\n{plain}")

    top_n = min(10, len(hits))
    header = (f"📚 每周论文速递 Top{top_n}（{win}）\n"
              f"arXiv q-fin 命中 {len(hits)}/{total} 篇，精选 {top_n} 篇"
              f"（{', '.join(TOPIC_ORDER)}）")
    full = header + "\n\n" + summary
    full = fit_bytes(full)   # 字节预算硬约束（企微 4096B），整行删减不半句截断

    if DRY_RUN:
        print("\n" + full + "\n")
        return

    log("总结完成，开始推送…")
    n = push(full, hook_env="WECOM_WEBHOOK_PAPERS")
    log(f"=== 完成，推送 {n} 人 ===")


if __name__ == "__main__":
    main()
