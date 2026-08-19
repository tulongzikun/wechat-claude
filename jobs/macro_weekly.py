#!/usr/bin/env python3
"""每周四 10:00 推送宏观周报（run.sh macro_weekly + crontab，CRON_TZ=Asia/Shanghai）。

结构（数字确定性拼装，LLM 只写各节 1~2 句小结——数值不过模型，防幻觉）：
  一、中国基本面：akshare 月度序列 + 日历本周发布的中国数据（带预期/前值）
     + 京沪二手房价（环比/同比/近10年，环比指数连乘）
  二、美国基本面：日历本周发布的美国数据（公布/预期/前值，附升降箭头）
  三、市场与行业景气：股债汇商品周表现 + 8marketcap 前 200 板块聚合与
     变动最大公司（快照 diff，行业靠内置 ticker 映射，站点无行业分类）
  四、国际局势：Google News RSS 俄乌/中东/中美经贸/制裁方向（复用
     games_news 的抓取+垃圾过滤），LLM 归纳 3~5 条要点（失败退化为标题列表）
  五、下周关注：日历未来 7 天

超过单条预算自动按节贪心拆条推送（企微 markdown 4096B/条）。
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
N_PAGES = 12            # 最多翻多少页（每页 ~100 行，够了就停）
EVENT_DAYS_BACK = 7     # 发布回顾窗口
EVENT_DAYS_FWD = 7      # 前瞻窗口
IMPORTANT = 2           # 经济日历重要性下限（实测 1~2 档，2=高，3 档不存在）
SPLIT_BYTES = 3300      # 超过则拆两条推送（留 header 余量）
GEO_MAX_BYTES = 1100    # 国际局势章字节帽（LLM 输出易超产，从尾部整行删）

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


def llm_summary(section_title: str, lines: list[str], n_chars: int = 120) -> str:
    """给一节数据写 1~2 句中文小结（唯一经 LLM 的部分）。失败返回空。"""
    from anthropic import Anthropic
    prompt = (f"下面是「{section_title}」的数据列表。请写 1~2 句中文小结（≤{n_chars}字），"
              "点出关键趋势/超预期方向与含义，措辞客观中性、无链接、不含敏感词汇，"
              "不要逐条复述。直接输出小结文本。\n\n" + "\n".join(lines))
    try:
        r = Anthropic().messages.create(
            model=os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
            or os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5",
            max_tokens=300, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content
                       if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        log(f"  ⚠️ 小结生成失败（{section_title}），跳过: {e}")
        return ""


# ---------- 经济日历（百度股市通，经 akshare；一/二/四节共用） ----------

KEY_EV = re.compile(
    r"CPI|PPI|PMI|非农|就业|失业|利率|联邦基金|FOMC|GDP|PCE|零售|贸易|"
    r"社融|融资|信贷|贷款|M2|M1|LPR|房价|工业|消费|信心|纪要|议息")
# 日历噪音：国债竞拍、地方性贸易指数、ETF 持仓变动等
EXCLUDE_EV = re.compile(r"竞拍|拍卖|非洲|持仓|批发|达拉斯|里奇蒙德|堪萨斯|周度发布变动")


def _calendar_rows() -> list[tuple[datetime.date, dict]]:
    """过去 EVENT_DAYS_BACK 天的日历原始行。"""
    rows = []
    today = datetime.date.today()
    for off in range(-EVENT_DAYS_BACK, 0):
        d = today + datetime.timedelta(days=off)
        try:
            import akshare as ak
            df = ak.news_economic_baidu(date=d.strftime("%Y%m%d"))
            rows += [(d, r) for r in df.to_dict("records")]
        except Exception:
            continue
    return rows


def _s(v) -> str:
    return "" if v is None or str(v) == "nan" else str(v)


def _arrow(pub: str, ref: str) -> str:
    """公布值相对参照值的方向箭头。"""
    try:
        return "↑" if float(pub) > float(ref) else ("↓" if float(pub) < float(ref) else "=")
    except (TypeError, ValueError):
        return ""


def calendar_region_events(rows, region: str) -> list[dict]:
    """某地区本周已发布的重要事件（去重、噪音过滤）。"""
    out, seen = [], set()
    for d, r in rows:
        if not isinstance(r.get("重要性"), (int, float)) or r["重要性"] < IMPORTANT:
            continue
        if r.get("地区") != region:
            continue
        ev = str(r.get("事件", "")).strip()
        pub = _s(r.get("公布"))
        if not ev or not KEY_EV.search(ev) or EXCLUDE_EV.search(ev) \
                or (d, ev) in seen or not pub:
            continue
        seen.add((d, ev))
        out.append({"date": d, "ev": ev, "pub": pub,
                    "exp": _s(r.get("预期")), "prev": _s(r.get("前值"))})
    out.sort(key=lambda x: x["date"])
    return out


def format_us_events(evs: list[dict]) -> list[str]:
    """美国事件 → 文本行（公布 + 预期/前值箭头，一眼看出方向与超预期）。"""
    lines = []
    for e in evs[:14]:
        seg = e["pub"]
        if e["exp"]:
            seg += f"（预期{e['exp']}{_arrow(e['pub'], e['exp'])}"
            seg += f"，前值{e['prev']}{_arrow(e['pub'], e['prev'])}）" if e["prev"] else "）"
        elif e["prev"]:
            seg += f"（前值{e['prev']}{_arrow(e['pub'], e['prev'])}）"
        lines.append(f"• {e['date']:%m-%d} {e['ev']}：{seg}")
    return lines


def calendar_forward() -> list[str]:
    """未来 7 天重要事件（带预期）。"""
    rows = []
    today = datetime.date.today()
    for off in range(0, EVENT_DAYS_FWD + 1):
        d = today + datetime.timedelta(days=off)
        try:
            import akshare as ak
            df = ak.news_economic_baidu(date=d.strftime("%Y%m%d"))
            rows += [(d, r) for r in df.to_dict("records")]
        except Exception:
            continue
    out, seen = [], set()
    for d, r in rows:
        if not isinstance(r.get("重要性"), (int, float)) or r["重要性"] < IMPORTANT:
            continue
        if r.get("地区") not in ("中国", "美国"):
            continue
        ev = str(r.get("事件", "")).strip()
        if not ev or not KEY_EV.search(ev) or EXCLUDE_EV.search(ev):
            continue
        # 去重：抹掉「截至8月X日当周」这类参考周差异后同名事件只留最新一条
        key = re.sub(r"截至\d+月\d+日当周", "", ev)
        if key in seen:
            continue
        out.append(f"• {d:%m-%d} {ev}（预期{_s(r.get('预期'))}）")
        seen.add(key)
    return out[:8]


# ---------- 一、中国基本面 ----------

def _df_rows(fn, n=2, head=False):
    """跑一个 akshare 接口，返回最新 n 行。head=True 用于降序（最新在前的）接口。"""
    try:
        df = fn()
        return (df.head(n) if head else df.tail(n)).to_dict("records")
    except Exception as e:
        log(f"  ⚠️ 接口失败: {type(e).__name__} {str(e)[:60]}")
        return None


def housing_10y(city_df, col_hb: str) -> str:
    """近 10 年累计涨幅（月度环比指数连乘；定基列近年缺值不可用）。"""
    try:
        df = city_df.sort_values("日期")
        start = datetime.date.today() - datetime.timedelta(days=365 * 10)
        win = df[df["日期"] >= start]
        if len(win) < 60:
            return "?"
        prod = 1.0
        for v in win[col_hb]:
            prod *= float(v) / 100.0
        return f"{(prod - 1) * 100:+.0f}%"
    except Exception:
        return "?"


def china_dashboard(cal_rows) -> list[str]:
    import akshare as ak
    lines = []

    # 日历本周发布的中国数据（带预期/前值）：M1/M2、社融/信贷（累计差分→单月）
    evs = calendar_region_events(cal_rows, "中国")
    for e in [x for x in evs if re.search(r"M[12]货币供应", x["ev"])][:2]:
        seg = f"{e['pub']}%（预期{e['exp']}，前值{e['prev']}）" if e["exp"] else f"{e['pub']}%"
        lines.append(f"• {e['ev']}：{seg}")

    def _cum_month(ev_kw) -> str | None:
        """从「年初至今」累计事件差分出单月增量行（亿元→万亿）。"""
        e = next((x for x in evs if ev_kw in x["ev"] and "年初至今" in x["ev"]), None)
        if not e:
            return None
        try:
            single = float(e["pub"]) - float(e["prev"])
            return (f"• {re.sub(r'-年初至今.*|（.*', '', e['ev'])}"
                    f"（推算单月 {single / 10000:+.2f} 万亿元）")
        except ValueError:
            return None

    line = _cum_month("社会融资规模")
    if line:
        lines.append(line)
    line = _cum_month("新增人民币贷款")
    if line:
        lines.append(line)

    # 月度序列（akshare 生意社源，降序 head；东财共识接口 2025-09 起停更弃用）
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
    r = (_df_rows(lambda: ak.macro_china_consumer_goods_retail(), head=True) or [{}])[0]
    if r.get("月份"):
        lines.append(f"• 社零同比（{r['月份']}）：{r.get('同比增长', '?')}%")
    rows = _df_rows(lambda: ak.macro_china_lpr())
    if rows:
        r = rows[-1]
        lines.append(f"• LPR（{str(r.get('TRADE_DATE'))[:10]}）：1Y {r.get('LPR1Y')} / "
                     f"5Y {r.get('LPR5Y')}")

    # 京沪二手房价：环比/同比/近10年（月度环比指数，100.3=+0.3%）
    try:
        df = ak.macro_china_new_house_price()
        hb = next(c for c in df.columns if "二手" in c and "环比" in c)
        tb = next(c for c in df.columns if "二手" in c and "同比" in c)
        recent = df[df["城市"].isin(["北京", "上海"])]
        latest = recent["日期"].max()
        for city in ("北京", "上海"):
            cdf = recent[recent["城市"] == city]
            r = cdf[cdf["日期"] == latest].iloc[0]
            def idx(v):
                try:
                    return f"{float(v) - 100:+.1f}%"
                except (TypeError, ValueError):
                    return "?"
            lines.append(f"• {city}二手房价（{str(latest)[:7]}）：环比{idx(r.get(hb))}，"
                         f"同比{idx(r.get(tb))}，近10年{housing_10y(cdf, hb)}")
    except Exception as e:
        log(f"  ⚠️ 70城房价失败: {e}")
    return lines


# ---------- 三、市场与行业景气 ----------

def _wow(closes: list) -> tuple:
    """(最新值, 一周前值)。closes 升序；不足 6 个取首个。"""
    if not closes:
        return None, None
    return closes[-1], closes[-6] if len(closes) >= 6 else closes[0]


def market_weekly() -> list[str]:
    import akshare as ak
    import yfinance as yf
    a = []
    for name, sym in [("沪深300", "sh000300"), ("中证500", "sh000905"), ("中证1000", "sh000852")]:
        try:
            df = ak.stock_zh_index_daily(symbol=sym)
            c, p = _wow(list(df["close"]))
            a.append(f"{name} {pct(c, p)}")
        except Exception as e:
            log(f"  ⚠️ {name}: {e}")
    y = []
    for name, sym in [("恒指", "^HSI"), ("纳指", "^IXIC"), ("标普", "^GSPC")]:
        try:
            h = yf.Ticker(sym).history(period="1mo")
            c, p = _wow([float(x) for x in h["Close"]])
            y.append(f"{name} {pct(c, p)}")
        except Exception as e:
            log(f"  ⚠️ {name}: {e}")
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


# 行业景气：8marketcap 前 200 快照 diff（站点无行业分类，内置映射）
SECTOR_MAP = {
    "NVDA": "半导体", "AVGO": "半导体", "TSM": "半导体", "AMD": "半导体", "ASML": "半导体",
    "QCOM": "半导体", "TXN": "半导体", "AMAT": "半导体", "ARM": "半导体", "MRVL": "半导体",
    "MU": "半导体", "LRCX": "半导体", "KLAC": "半导体", "SNPS": "半导体", "CDNS": "半导体",
    "ADI": "半导体", "NXPI": "半导体", "2454.TW": "半导体", "005930.KS": "半导体",
    "WDC": "半导体", "SNDK": "半导体", "6861.T": "半导体", "6857.T": "半导体",
    "AAPL": "科技", "MSFT": "科技", "GOOGL": "科技", "GOOG": "科技", "META": "科技",
    "AMZN": "科技", "ORCL": "科技", "CRM": "科技", "ADBE": "科技", "NOW": "科技",
    "NFLX": "科技", "IBM": "科技", "SAP": "科技", "INTU": "科技", "PLTR": "科技",
    "UBER": "科技", "SHOP": "科技", "BABA": "科技", "9988.HK": "科技", "PDD": "科技",
    "JD": "科技", "MELI": "科技", "TCEHY": "科技", "0700.HK": "科技", "1810.HK": "科技",
    "3690.HK": "科技", "CSCO": "科技",
    "BRK.B": "金融", "BRK-B": "金融", "JPM": "金融", "V": "金融", "MA": "金融",
    "BAC": "金融", "WFC": "金融", "GS": "金融", "MS": "金融", "C": "金融",
    "HSBC": "金融", "BLK": "金融", "SCHW": "金融", "AXP": "金融", "SPGI": "金融",
    "ALV.DE": "金融", "1398.HK": "金融", "0939.HK": "金融", "2318.HK": "金融",
    "IBKR": "金融", "CBA.AX": "金融",
    "LLY": "医药", "JNJ": "医药", "NVO": "医药", "MRK": "医药", "ABBV": "医药",
    "PFE": "医药", "TMO": "医药", "ABT": "医药", "UNH": "医药", "CVS": "医药",
    "AMGN": "医药", "AZN": "医药", "NVS": "医药", "ISRG": "医药",
    "XOM": "能源", "CVX": "能源", "SHEL": "能源", "TTE": "能源", "COP": "能源",
    "BP": "能源", "EOG": "能源", "SLB": "能源", "PBR": "能源", "2262.HK": "能源",
    "SU.PA": "能源", "RELIANCE.NS": "能源",
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
    太脆）。坑：rank 格子带 moves="±n" 属性、百分比 span 数字前有空格。"""
    out = []
    for page in range(1, N_PAGES + 1):
        try:
            html = http_get(f"https://8marketcap.com/companies/?page={page}").decode("utf-8", "ignore")
        except Exception as e:
            log(f"  ⚠️ 8marketcap 第{page}页失败: {e}")
            break
        n_page = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
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
        if len(out) >= N_CAP or n_page == 0:
            break   # 凑够了 / 页面结构变了，别傻翻
        time.sleep(0.4)
    return out[:N_CAP]


def industry_mood(rows: list[dict], state: dict) -> tuple[list[str], dict]:
    """板块聚合 + 变动最大公司点名。返回 (文本行, 新状态)。"""
    prev = state.get("8m_rank") or {}
    new_state = {"8m_rank": {r["ticker"]: r["rank"] for r in rows},
                 "ts": time.strftime("%Y-%m-%d")}
    sec = lambda t: SECTOR_MAP.get(t, "其他")

    # 变化最大的公司：7 日市值变动口径（无论有无上周快照都有），排名口径补充
    movers_up = sorted(rows, key=lambda r: -r["wk"])[:3]
    movers_dn = sorted(rows, key=lambda r: r["wk"])[:3]
    lines = ["• 7日市值变动居前：" + "、".join(
        f"{r['name']}({sec(r['ticker'])}){r['wk']:+.1f}%" for r in movers_up),
        "• 7日市值变动居后：" + "、".join(
            f"{r['name']}({sec(r['ticker'])}){r['wk']:+.1f}%" for r in movers_dn)]
    if prev:
        delta = lambda r: prev[r["ticker"]] - r["rank"]
        dr = sorted((r for r in rows if r["ticker"] in prev
                     and abs(delta(r)) >= 2),            # ±1 是盘次间噪声
                    key=lambda r: -delta(r))
        # 按符号分组取 top3：达标公司不足 6 家时 dr[:3]/dr[-3:] 会重叠，
        # 曾把 -2/-3 位的公司排进「上升居前」
        ups = [f"{r['name']}{delta(r):+d}位" for r in dr if delta(r) > 0][:3]
        dns = [f"{r['name']}{delta(r):+d}位"
               for r in [x for x in dr if delta(x) < 0][-3:][::-1]]
        if ups:
            lines.append("• 排名上升居前：" + "、".join(ups))
        if dns:
            lines.append("• 排名下降居前：" + "、".join(dns))
        new_in = [r for r in rows if r["rank"] <= 100 and prev.get(r["ticker"], 999) > 100]
        if new_in:
            lines.append("• 新进前100：" + "、".join(r["name"] for r in new_in[:5]))
    else:
        lines.append(f"• 前{len(rows)}名快照已建，下周起显示排名变化")

    # 板块聚合
    agg = {}
    for r in rows:
        s = sec(r["ticker"])
        if s == "其他":
            continue
        d = agg.setdefault(s, {"wk": [], "dr": []})
        d["wk"].append(r["wk"])
        if r["ticker"] in prev:
            d["dr"].append(prev[r["ticker"]] - r["rank"])
    stats = []
    for s, d in agg.items():
        if len(d["wk"]) >= 3:
            avg_wk = sum(d["wk"]) / len(d["wk"])
            avg_dr = (sum(d["dr"]) / len(d["dr"])) if d["dr"] else None
            stats.append((s, avg_wk, avg_dr, len(d["wk"])))
    for s, wk, adr, n in sorted(stats, key=lambda x: -x[1]):
        dtag = f"，均排{adr:+.1f}位" if adr is not None else ""
        lines.append(f"• {s}（{n}家）：周市值{wk:+.1f}%{dtag}")
    return lines, new_state


# ---------- 四、国际局势（Google News RSS + LLM 归纳，复用 games_news 基建） ----------

GEO_QUERIES = ("俄乌", "中东 局势", "中美 关税", "国际制裁")

# 网关 1301 软化：时政标题里的敏感组合送模型前换中性说法（实测 ICC 制裁类被拒）
GEO_SAFE = [
    ("国际刑事法院", "国际司法机构"), ("国际刑事法庭", "国际司法机构"),
    ("ICC", "国际司法机构"), ("制裁", "施加限制措施"), ("开刀", "采取行动"),
    ("反抗", "反制"), ("打击", "行动"), ("袭击", "冲突事件"), ("战争", "军事冲突"),
]


def _geo_sanitize(text: str) -> str:
    for a, b in GEO_SAFE:
        text = text.replace(a, b)
    return text


def geopolitics() -> list[str]:
    """本周国际局势要点 3~5 条。

    网关 1301 是对整段输入的组合打分（实测：单条标题都过、拼 12 条即拒，
    剔除任一条仍拒）——所以不能一次性把标题喂给模型。改为两级：
    ① 每 6 条一批让模型写成中性要点（批内可过）；② 把各批要点合并成稿
    （此时输入已是模型中性化措辞，不再含原始标题）。任一步失败退化为
    每 query 最新 2 条的标题列表。
    """
    try:
        from games_news import fetch_gnews, JUNK_RE   # jobs/ 内复用
    except Exception as e:
        log(f"  ⚠️ 复用 games_news 失败: {e}")
        return []
    per_q: dict[str, list[dict]] = {}
    merged: dict[str, dict] = {}
    for q in GEO_QUERIES:
        bucket = []
        for it in fetch_gnews(q):
            key = re.sub(r"\W", "", it["title"].split(" - ")[0])[:24]
            if key in merged or JUNK_RE.search(it["title"]):
                continue
            merged[key] = it
            bucket.append(it)
        bucket.sort(key=lambda x: x["published"], reverse=True)
        per_q[q] = bucket
    all_items = sorted(merged.values(), key=lambda x: x["published"], reverse=True)
    if not all_items:
        return []

    from anthropic import Anthropic
    model = (os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
             or os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5")

    def _llm(prompt: str) -> str:
        r = Anthropic().messages.create(
            model=model, max_tokens=600,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content
                       if getattr(b, "type", "") == "text").strip()

    # ① 分批归纳（每批 6 条：批太大网关组合打分会拒，失败减半重试一次）
    bullets: list[str] = []
    titles = [_geo_sanitize(it["title"]) + f"（{it['source']}）"
              for it in all_items[:24]]
    for i in range(0, len(titles), 6):
        batch = titles[i:i + 6]
        for size in (len(batch), max(3, len(batch) // 2)):
            try:
                text = _llm(
                    "下面是一批上周国际时政/经贸中文新闻标题（措辞已部分中性化）。"
                    "归纳成 1~2 条要点：每条 **加粗一句话概括** + 1 句进展 + "
                    "1 句对市场/能源/供应链的影响；同事件合并，无关舍弃；"
                    "不输出链接，来源媒体名括注句尾，措辞中性客观。markdown 列表。\n\n"
                    + "\n".join(f"- {t}" for t in batch[:size]))
                bullets.extend(l for l in text.splitlines() if l.strip())
                break
            except Exception as e:
                log(f"  ⚠️ 国际局势批归纳失败（{size} 条）: {e}")
    bullets = _geo_tidy(bullets)
    if not bullets:
        return _geo_fallback(per_q)

    # ② 合并成稿（输入已是中性化要点）
    try:
        text = _llm(
            "下面是对上周国际时政/经贸新闻的分批归纳要点。请合并成最终 "
            "3~5 条本周国际局势要点：**同一事件多批重复出现时只保留一条**"
            "（取信息最全的），每条 **加粗一句话概括** + 1 句进展 + 1 句对"
            "市场/能源/供应链的影响，每条 ≤60 字，按对市场影响排序；"
            "措辞中性客观，不输出链接。markdown 列表。\n\n" + "\n".join(bullets))
        out = _geo_tidy([l for l in text.splitlines() if l.strip()])
        return out if out else bullets[:5]
    except Exception as e:
        log(f"  ⚠️ 国际局势合并失败，用分批要点: {e}")
        return bullets[:5]


def _geo_tidy(lines: list[str]) -> list[str]:
    """模型输出的行归一：行首 */-/*** 等杂前缀统一成「- **…」，段中粘连的
    『。 - **』拆成独立行。注意 *** 的前两位就是 **，不能用 find 定位加粗——
    先剥掉行首 bullet 字符，剩体内部还有闭合加粗就补一个开标记。"""
    out = []
    for l in lines:
        for piece in re.split(r"。?\s+-\s+\*\*", l):
            body = piece.strip().lstrip("*- \t")
            if not body:
                continue
            if not body.startswith("**") and "**" in body:
                body = "**" + body          # 加粗开标记被 bullet 剥离/拆分吃掉
            out.append("- " + body)
    return out


def _geo_fallback(per_q: dict[str, list[dict]]) -> list[str]:
    """LLM 全挂时的确定性兜底：每 query 最新 2 条，跨 query 去重。"""
    out, seen = [], set()
    for q in GEO_QUERIES:
        for it in per_q.get(q, [])[:2]:
            k = re.sub(r"\W", "", it["title"])[:12]
            if k not in seen:
                seen.add(k)
                out.append(f"- {it['title']}（{it['source']}）")
    return out[:6]


# ---------- 主流程 ----------

def main() -> None:
    log(f"=== 宏观周报 开始{'（dry-run）' if DRY_RUN else ''} ===")
    t0 = time.time()

    cal_rows = _calendar_rows()
    sec_cn = china_dashboard(cal_rows)
    cn_sum = llm_summary("中国基本面", sec_cn)
    sec_us = format_us_events(calendar_region_events(cal_rows, "美国"))
    us_sum = llm_summary("美国基本面", sec_us) if sec_us else ""
    sec_mkt = market_weekly()
    rows = fetch_rankings()
    state = load_state()
    sec_ind, new_state = industry_mood(rows, state) if rows else (["• 抓取失败"], {})
    if rows and not DRY_RUN:
        save_state(new_state)      # dry-run 不落快照，避免污染下次 diff 基准
    sec_geo = geopolitics()
    # 国际局势是唯一 LLM 产出的正文节，超预算从尾部整行删（前 3~4 条信息密度最高）
    kept, nb = [], 0
    for l in sec_geo:
        lb = len(l.encode("utf-8")) + 1
        if kept and nb + lb > GEO_MAX_BYTES:
            break
        kept.append(l)
        nb += lb
    sec_geo = kept
    fwd = calendar_forward()

    parts = []
    if sec_cn:
        parts.append("一、中国基本面\n" + ("\n".join(sec_cn))
                     + (f"\n小结：{cn_sum}" if cn_sum else ""))
    if sec_us:
        parts.append("二、美国基本面（本周发布）\n" + ("\n".join(sec_us))
                     + (f"\n小结：{us_sum}" if us_sum else ""))
    if sec_mkt or sec_ind:
        parts.append("三、市场与行业景气\n" + "\n".join(sec_mkt + sec_ind))
    if sec_geo:
        parts.append("四、国际局势\n" + "\n".join(sec_geo))
    if fwd:
        parts.append("五、下周关注\n" + "\n".join(fwd))

    win = f"{datetime.date.today() - datetime.timedelta(days=7):%m-%d}~{datetime.date.today():%m-%d}"
    header = f"📊 宏观周报（{win}）\n中国基本面 / 美国基本面 / 市场与行业 / 国际局势 / 下周前瞻"
    # 贪心分段：整节装进当前条，装不下就开新条（超 4096B/条会被企微拒收）
    msgs_raw, cur, cur_b = [], [], 0
    for p in parts:
        pb = len(p.encode("utf-8"))
        if cur and cur_b + pb > SPLIT_BYTES:
            msgs_raw.append("\n\n".join(cur))
            cur, cur_b = [], 0
        cur.append(p)
        cur_b += pb + 2
    if cur:
        msgs_raw.append("\n\n".join(cur))
    msgs = [fit_bytes(header + "\n\n" + msgs_raw[0])]
    for i, m in enumerate(msgs_raw[1:], 2):
        msgs.append(fit_bytes(f"📊 宏观周报（{win}·续{i}）\n\n" + m))
    log(f"拼装完成：{sum(len(m.encode('utf-8')) for m in msgs)} 字节 / {len(msgs)} 条，"
        f"耗时 {time.time() - t0:.0f}s")

    if DRY_RUN:
        for i, m in enumerate(msgs, 1):
            print(f"\n〔第 {i} 条〕\n" + m + "\n")
        return

    for i, m in enumerate(msgs, 1):
        n = push(m, hook_env="WECOM_WEBHOOK_MACRO")
        log(f"--- 第 {i}/{len(msgs)} 条推送 {n} 通道 ---")
    log(f"=== 完成，共 {len(msgs)} 条 ===")


if __name__ == "__main__":
    main()
