#!/usr/bin/env python3
"""每周四 10:00 推送宏观周报（run.sh macro_weekly + crontab，CRON_TZ=Asia/Shanghai）。

结构（数字确定性拼装，LLM 只写开头 2~3 句总评——数值不过模型，防幻觉）：
  一、本周重点数据发布：百度股市通经济日历，窗口=过去 7 天，公布vs预期vs前值
  二、中国基本面最新读数：akshare（东财）CPI/PPI/PMI/出口/M1M2/社融/新增信贷/
     社零/LPR + 京沪二手房价（70 城月度）
  三、市场周度表现：A股指数（新浪）+ 港美/债/汇/商品（yfinance）+ 中美 10Y（东财）
  四、行业景气·全球市值排名：8marketcap.com/companies/ 前 200 快照，与上周
     状态文件 diff → 板块平均排名变化/新进跌出（行业靠内置 ticker 映射表，
     站点本身无行业分类）
  五、下周关注：经济日历未来 7 天，重要性过滤

数据源稳定性差异大，所有抓取独立 try/except：单项失败标注跳过，不拖垮整报。

调试：python3 macro_weekly.py --dry-run   只抓+拼装+打印，不推送。
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from daily_update import push, fit_bytes, log   # 复用推送/字节预算/日志

DRY_RUN = "--dry-run" in sys.argv
DIR = Path(__file__).resolve().parent
STATE_FILE = DIR / "macro_state.json"   # 上周市值排名快照等（gitignore）

FETCH_TIMEOUT = 30
N_CAP = 200             # 8marketcap 抓取前多少名
N_PAGES = 12            # 最多翻多少页（每页行数不固定，够了就停）
EVENT_DAYS_BACK = 7     # 发布回顾窗口
EVENT_DAYS_FWD = 7      # 前瞻窗口
IMPORTANT = 2           # 经济日历重要性下限（实测 1~2 档，2=高，3 档不存在）

# ---------- 小工具 ----------

def http_get(url: str, timeout: int = FETCH_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 macro-weekly/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(st: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError as e:
        log(f"  ⚠️ 状态文件写入失败: {e}")


def pct(x, prev) -> str:
    """周涨跌幅文本；prev 缺失时只给水平值。"""
    if x is None:
        return "?"
    s = f"{x:,.2f}".rstrip("0").rstrip(".")
    if prev:
        return f"{s}（周{(x / prev - 1) * 100:+.1f}%）"
    return s


# ---------- 一 & 五、经济日历（百度股市通，经 akshare） ----------

KEY_EV = re.compile(
    r"CPI|PPI|PMI|非农|就业|失业|利率|联邦基金|FOMC|GDP|PCE|零售|贸易|"
    r"社融|融资|信贷|贷款|M2|M1|LPR|房价|工业|消费|信心|纪要|议息")
# 日历噪音：国债竞拍、地方性贸易指数、ETF 持仓变动等
EXCLUDE_EV = re.compile(r"竞拍|拍卖|非洲|持仓|批发|达拉斯|里奇蒙德|堪萨斯")

def _calendar(d: datetime.date) -> list[dict]:
    import akshare as ak
    df = ak.news_economic_baidu(date=d.strftime("%Y%m%d"))
    return df.to_dict("records")


def calendar_items(days_fwd: int) -> list[str]:
    """过去 days_fwd 天已发布（或未来天数的待发布）重要事件 → 文本行。

    重要性实测只到 2（1~3 档），阈值取 >=2；再加关键词白名单，否则全是
    ETF 持仓变动这类噪音。回顾只收已公布；前瞻带预期值。
    """
    rows = []
    today = datetime.date.today()
    for off in range(-EVENT_DAYS_BACK if days_fwd < 0 else 0,
                     days_fwd + 1 if days_fwd >= 0 else 1):
        if days_fwd < 0 and off >= 0:
            break
        d = today + datetime.timedelta(days=off)
        try:
            rows += [(d, r) for r in _calendar(d)]
        except Exception:
            continue
    out, seen = [], set()
    for d, r in rows:
        if not isinstance(r.get("重要性"), (int, float)) or r["重要性"] < IMPORTANT:
            continue
        if r.get("地区") not in ("中国", "美国"):
            continue
        ev = str(r.get("事件", "")).strip()
        if not ev or not KEY_EV.search(ev) or EXCLUDE_EV.search(ev) or (d, ev) in seen:
            continue
        pub, exp, prev = r.get("公布"), r.get("预期"), r.get("前值")
        past = days_fwd < 0
        if past and (pub is None or str(pub) == "nan"):
            continue   # 回顾只收已公布的
        def f(v):
            return "" if v is None or str(v) == "nan" else str(v)
        if past:
            seg = f(pub) + (f"（预期{f(exp)}，前值{f(prev)}）" if f(exp) or f(prev) else "")
            out.append(f"• {d:%m-%d} {ev}：{seg}")
        else:
            out.append(f"• {d:%m-%d} {ev}（预期{f(exp)}）")
        seen.add((d, ev))
    return out[:12]


# ---------- 二、中国基本面（akshare，各自独立降级） ----------

def _df_rows(fn, n=2, head=False):
    """跑一个 akshare 接口，返回最新 n 行。head=True 用于降序（最新在前的）接口。"""
    try:
        df = fn()
        return (df.head(n) if head else df.tail(n)).to_dict("records")
    except Exception as e:
        log(f"  ⚠️ 接口失败: {type(e).__name__} {str(e)[:60]}")
        return None


def china_dashboard() -> list[str]:
    import akshare as ak
    lines = []

    # 通胀（生意社源，降序：最新在 head；东财共识接口 2025-09 起停更弃用）
    r = (_df_rows(lambda: ak.macro_china_cpi(), head=True) or [{}])[0]
    if r.get("月份"):
        lines.append(f"• CPI 同比（{r['月份']}）：{r.get('全国-同比增长', '?')}%"
                     f"（环比{r.get('全国-环比增长', '?')}%）")
    r = (_df_rows(lambda: ak.macro_china_ppi(), head=True) or [{}])[0]
    if r.get("月份"):
        lines.append(f"• PPI 同比（{r['月份']}）：{r.get('当月同比增长', '?')}%")
    r = (_df_rows(lambda: ak.macro_china_pmi(), head=True) or [{}])[0]
    if r.get("月份"):
        lines.append(f"• PMI（{r['月份']}）：制造业 {r.get('制造业-指数', '?')}，"
                     f"非制造业 {r.get('非制造业-指数', '?')}")

    # 金融（money_supply/社零降序 head；社融/信贷升序 tail）
    r = (_df_rows(lambda: ak.macro_china_money_supply(), head=True) or [{}])[0]
    if r.get("月份"):
        lines.append(f"• M1/M2 同比（{r['月份']}）：{r.get('货币(M1)-同比增长', '?')}% / "
                     f"{r.get('货币和准货币(M2)-同比增长', '?')}%")
    rows = _df_rows(lambda: ak.macro_china_shrzgm())
    if rows:
        r = rows[-1]
        lines.append(f"• 社融增量（{r.get('月份')}）：{r.get('社会融资规模增量')} 亿元"
                     "（序列更新滞后，最新发布见上节日历）")
    rows = _df_rows(lambda: ak.macro_rmb_loan())
    if rows:
        r = rows[-1]
        lines.append(f"• 新增人民币贷款（{r.get('月份')}）：{r.get('新增人民币贷款-总额')} 亿元")
    r = (_df_rows(lambda: ak.macro_china_consumer_goods_retail(), head=True) or [{}])[0]
    if r.get("月份"):
        lines.append(f"• 社零同比（{r['月份']}）：{r.get('同比增长', '?')}%")
    rows = _df_rows(lambda: ak.macro_china_lpr())
    if rows:
        r = rows[-1]
        lines.append(f"• LPR（{str(r.get('TRADE_DATE'))[:10]}）：1Y {r.get('LPR1Y')} / "
                     f"5Y {r.get('LPR5Y')}")

    # 京沪二手房价（70 城月度，指数形式：100.3 = 环比+0.3%）
    try:
        df = ak.macro_china_new_house_price()
        recent = df[df["城市"].isin(["北京", "上海"])]
        latest = recent["日期"].max()
        hb = next(c for c in df.columns if "二手" in c and "环比" in c)
        tb = next(c for c in df.columns if "二手" in c and "同比" in c)
        for _, r in recent[recent["日期"] == latest].iterrows():
            def idx(v):
                try:
                    return f"{float(v) - 100:+.1f}%"
                except (TypeError, ValueError):
                    return "?"
            lines.append(f"• {r['城市']}二手房价（{str(latest)[:7]}）："
                         f"环比{idx(r.get(hb))}，同比{idx(r.get(tb))}")
    except Exception as e:
        log(f"  ⚠️ 70城房价失败: {e}")
    return lines


# ---------- 三、市场周度表现 ----------

def _wow(closes: list) -> tuple:
    """(最新值, 一周前值)。closes 升序；不足 6 个取首个。"""
    if not closes:
        return None, None
    return closes[-1], closes[-6] if len(closes) >= 6 else closes[0]


def market_weekly() -> list[str]:
    import akshare as ak
    import yfinance as yf
    lines = ["股", "债汇", "商品"]
    idx = {"groups": []}
    # A股（新浪日线，全历史，取尾）
    a = []
    for name, sym in [("沪深300", "sh000300"), ("中证500", "sh000905"), ("中证1000", "sh000852")]:
        try:
            df = ak.stock_zh_index_daily(symbol=sym)
            c, p = _wow(list(df["close"]))
            a.append(f"{name} {pct(c, p)}")
        except Exception as e:
            log(f"  ⚠️ {name}: {e}")
    # 港美（yfinance 1 个月日线）
    y = []
    for name, sym in [("恒指", "^HSI"), ("纳指", "^IXIC"), ("标普", "^GSPC")]:
        try:
            h = yf.Ticker(sym).history(period="1mo")
            c, p = _wow([float(x) for x in h["Close"]])
            y.append(f"{name} {pct(c, p)}")
        except Exception as e:
            log(f"  ⚠️ {name}: {e}")
    # 债（东财，中债/美债 10Y 已含）+ 汇
    b = []
    try:
        df = ak.bond_zh_us_rate(start_date=(datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y%m%d"))
        zh = [float(x) for x in df["中国国债收益率10年"].dropna()]
        us = [float(x) for x in df["美国国债收益率10年"].dropna()]
        for name, arr in [("10Y 中债", zh), ("10Y 美债", us)]:
            c, p = _wow(arr)
            b.append(f"{name} {c:.2f}%（周{(c - p) * 100:+.0f}bp）" if c and p else f"{name} {c}")
    except Exception as e:
        log(f"  ⚠️ 国债收益率: {e}")
    for name, sym in [("美元指数", "DX-Y.NYB"), ("离岸人民币", "CNH=X")]:
        try:
            h = yf.Ticker(sym).history(period="1mo")
            c, p = _wow([float(x) for x in h["Close"]])
            b.append(f"{name} {pct(c, p)}")
        except Exception as e:
            log(f"  ⚠️ {name}: {e}")
    # 商品类
    cm = []
    for name, sym in [("WTI 原油", "CL=F"), ("铜", "HG=F"), ("黄金", "GC=F")]:
        try:
            h = yf.Ticker(sym).history(period="1mo")
            c, p = _wow([float(x) for x in h["Close"]])
            cm.append(f"{name} {pct(c, p)}")
        except Exception as e:
            log(f"  ⚠️ {name}: {e}")
    return [f"• 股：{'；'.join(a + y)}",
            f"• 债汇：{'；'.join(b)}",
            f"• 商品：{'；'.join(cm)}"]


# ---------- 四、行业景气（8marketcap 快照 diff） ----------

# ticker → 板块（站点无行业分类，内置映射；未收录的进「未分类」不计入板块聚合）
SECTOR_MAP = {
    "NVDA": "半导体", "AVGO": "半导体", "TSM": "半导体", "AMD": "半导体", "ASML": "半导体",
    "QCOM": "半导体", "TXN": "半导体", "AMAT": "半导体", "ARM": "半导体", "MRVL": "半导体",
    "MU": "半导体", "LRCX": "半导体", "KLAC": "半导体", "SNPS": "半导体", "CDNS": "半导体",
    "ADI": "半导体", "NXPI": "半导体", "2454.TW": "半导体", "005930.KS": "半导体",
    "AAPL": "科技", "MSFT": "科技", "GOOGL": "科技", "GOOG": "科技", "META": "科技",
    "AMZN": "科技", "ORCL": "科技", "CRM": "科技", "ADBE": "科技", "NOW": "科技",
    "NFLX": "科技", "IBM": "科技", "SAP": "科技", "INTU": "科技", "PLTR": "科技",
    "UBER": "科技", "SHOP": "科技", "BABA": "科技", "9988.HK": "科技", "PDD": "科技",
    "JD": "科技", "MELI": "科技", "TCEHY": "科技", "0700.HK": "科技", "1810.HK": "科技",
    "3690.HK": "科技",
    "BRK.B": "金融", "BRK-B": "金融", "JPM": "金融", "V": "金融", "MA": "金融",
    "BAC": "金融", "WFC": "金融", "GS": "金融", "MS": "金融", "C": "金融",
    "HSBC": "金融", "BLK": "金融", "SCHW": "金融", "AXP": "金融", "SPGI": "金融",
    "ALV.DE": "金融", "1398.HK": "金融", "0939.HK": "金融",
    "LLY": "医药", "JNJ": "医药", "NVO": "医药", "MRK": "医药", "ABBV": "医药",
    "PFE": "医药", "TMO": "医药", "ABT": "医药", "UNH": "医药", "CVS": "医药",
    "AMGN": "医药", "AZN": "医药", "NVS": "医药", "ISRG": "医药",
    "XOM": "能源", "CVX": "能源", "SHEL": "能源", "TTE": "能源", "COP": "能源",
    "BP": "能源", "EOG": "能源", "SLB": "能源", "PBR": "能源", "2262.HK": "能源",
    "SU.PA": "能源",
    "WMT": "消费", "PG": "消费", "KO": "消费", "PEP": "消费", "MCD": "消费",
    "NKE": "消费", "COST": "消费", "HD": "消费", "PM": "消费", "MO": "消费",
    "DIS": "消费", "SBUX": "消费", "TGT": "消费", "LOW": "消费", "600519.SS": "消费",
    "MC.PA": "消费", "RMS.PA": "消费", "OR.PA": "消费",
    "TSLA": "汽车", "TM": "汽车", "7203.T": "汽车", "1211.HK": "汽车", "F": "汽车",
    "GM": "汽车", "RACE": "汽车", "STLA": "汽车",
    "GE": "工业", "CAT": "工业", "BA": "工业", "LMT": "工业", "RTX": "工业",
    "UNP": "工业", "HON": "工业", "DE": "工业", "300750.SZ": "工业", "AIR.PA": "工业",
    "SIE.DE": "工业",
    "VZ": "通信", "T": "通信", "TMUS": "通信",
}


def fetch_rankings() -> list[dict]:
    """8marketcap /companies/ 前 N_CAP 名。按 <tr> 分块解析（整体大正则跨行
    太脆，只吃到 1/5 行）。每页 ~100 行，翻页凑够即停。"""
    out = []
    for page in range(1, N_PAGES + 1):
        try:
            html = http_get(f"https://8marketcap.com/companies/?page={page}").decode("utf-8", "ignore")
        except Exception as e:
            log(f"  ⚠️ 8marketcap 第{page}页失败: {e}")
            break
        n_page = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            # rank 格子可能带 moves="±n"（站点自记的排名变动，窗口不明不用）；
            # 百分比 span 里数字前可能有空格
            m_rank = re.search(r'data-sort="(\d+)"[^>]*>\s*\d+\s*</td>', tr)
            m_name = re.search(r'company-name">([^<]+)<', tr)
            m_tick = re.search(r'badge-company">([^<]+)<', tr)
            m_mcap = re.search(r'data-sort="(\d+)">\$?[0-9,.]+\s*[TBM]?<', tr)
            pcts = re.findall(r'>\s*(-?[\d.]+)%</span>', tr)   # [24h, 7d]
            if not (m_rank and m_name and m_tick and m_mcap and len(pcts) >= 2):
                continue
            try:
                wk = float(pcts[1])
            except ValueError:
                continue
            out.append({"rank": int(m_rank.group(1)), "name": m_name.group(1).strip(),
                        "ticker": m_tick.group(1).strip(), "mcap": int(m_mcap.group(1)),
                        "wk": wk})
            n_page += 1
        if len(out) >= N_CAP:
            break
        if n_page == 0:
            break   # 页面结构变了，别傻翻
        time.sleep(0.4)
    return out[:N_CAP]


def industry_mood(rows: list[dict], state: dict) -> tuple[list[str], dict]:
    """与上周快照 diff → 板块景气行。返回 (文本行, 新状态)。"""
    now = {r["ticker"]: r["rank"] for r in rows}
    lines = []
    new_state = {"8m_rank": now, "ts": time.strftime("%Y-%m-%d")}
    prev = state.get("8m_rank") or {}
    if not prev:
        lines.append(f"• 前{len(rows)}名快照已建（本周起记录，下周报告排名变化）；"
                     "按 7 日市值变动：")
    # 板块聚合：7 日市值变动均值 + （有上周快照时）平均排名变化
    agg = {}
    for r in rows:
        sec = SECTOR_MAP.get(r["ticker"])
        if not sec:
            continue
        d = agg.setdefault(sec, {"wk": [], "dr": [], "n": 0})
        d["wk"].append(r["wk"])
        d["n"] += 1
        if r["ticker"] in prev:
            d["dr"].append(prev[r["ticker"]] - r["rank"])   # 正=上升
    stats = []
    for sec, d in agg.items():
        if d["n"] >= 3:
            avg_wk = sum(d["wk"]) / len(d["wk"])
            avg_dr = (sum(d["dr"]) / len(d["dr"])) if d["dr"] else None
            stats.append((sec, avg_wk, avg_dr, d["n"]))
    by_wk = sorted(stats, key=lambda x: -x[1])
    for sec, wk, dr, n in by_wk:
        dtag = f"，均排{dr:+.1f}位" if dr is not None else ""
        lines.append(f"• {sec}（{n}家）：周市值{wk:+.1f}%{dtag}")
    if prev:
        risers = sorted([r for r in rows if r["ticker"] in prev],
                        key=lambda r: -(prev[r["ticker"]] - r["rank"]))[:3]
        fallers = sorted([r for r in rows if r["ticker"] in prev],
                         key=lambda r: prev[r["ticker"]] - r["rank"])[:3]
        if risers:
            lines.append("• 升幅居前：" + "、".join(
                f"{r['name']}{prev[r['ticker']] - r['rank']:+d}位" for r in risers))
        if fallers:
            lines.append("• 降幅居前：" + "、".join(
                f"{r['name']}{prev[r['ticker']] - r['rank']:+d}位" for r in fallers))
        new_in = [r for r in rows if r["rank"] <= 100 and prev.get(r["ticker"], 999) > 100]
        if new_in:
            lines.append("• 新进前100：" + "、".join(r["name"] for r in new_in[:5]))
    return lines, new_state


# ---------- 总评（唯一经 LLM 的部分） ----------

def brief_intro(body: str) -> str:
    from anthropic import Anthropic
    prompt = ("下面是本周宏观周报的数据部分。请写 2~3 句中文总评开头（≤150字），"
              "点出最重要的 1~2 个数据变化和市场含义，措辞客观中性、无链接、"
              "不出现敏感词汇，不要逐条复述。直接输出总评文本。\n\n" + body)
    try:
        r = Anthropic().messages.create(
            model=os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
            or os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5",
            max_tokens=300, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content
                       if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        log(f"  ⚠️ 总评生成失败，跳过: {e}")
        return ""


# ---------- 主流程 ----------

def main() -> None:
    log(f"=== 宏观周报 开始{'（dry-run）' if DRY_RUN else ''} ===")
    t0 = time.time()

    sec_events = calendar_items(-EVENT_DAYS_BACK)
    sec_cn = china_dashboard()
    sec_mkt = market_weekly()
    rows = fetch_rankings()
    state = load_state()
    sec_ind, new_state = industry_mood(rows, state) if rows else (["• 抓取失败"], {})
    if rows and not DRY_RUN:
        save_state(new_state)      # dry-run 不落快照，避免污染下次 diff 基准

    parts = []
    if sec_events:
        parts.append("一、本周重点数据发布\n" + "\n".join(sec_events))
    if sec_cn:
        parts.append("二、中国基本面（最新读数）\n" + "\n".join(sec_cn))
    if sec_mkt:
        parts.append("三、市场周度表现\n" + "\n".join(sec_mkt))
    if sec_ind:
        parts.append("四、行业景气·全球市值前200\n" + "\n".join(sec_ind))
    fwd = calendar_items(EVENT_DAYS_FWD)
    if fwd:
        parts.append("五、下周关注\n" + "\n".join(fwd[:8]))

    body = "\n\n".join(parts)
    intro = brief_intro(body)
    win = f"{datetime.date.today() - datetime.timedelta(days=7):%m-%d}~{datetime.date.today():%m-%d}"
    header = (f"📊 宏观周报（{win}）\n"
              f"中国基本面 / 全球市场 / 行业景气 / 下周前瞻"
              + (f"\n\n{intro}" if intro else ""))
    full = fit_bytes(header + "\n\n" + body)
    log(f"拼装完成：{len(full.encode('utf-8'))} 字节，耗时 {time.time() - t0:.0f}s")

    if DRY_RUN:
        print("\n" + full + "\n")
        return
    n = push(full, hook_env="WECOM_WEBHOOK_MACRO")
    log(f"=== 完成，推送 {n} 通道 ===")


if __name__ == "__main__":
    main()
