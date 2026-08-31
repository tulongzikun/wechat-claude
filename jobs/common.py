#!/usr/bin/env python3
"""jobs 各定时任务的共用能力：LLM 调用 + HTTP 抓取（防谎报处理内建）。

为什么存在：2026-08 两次谎报事故（daily_update 08-27、weekly_papers 08-31）
根因相同——同一段 LLM 调用样板在姐妹文件里漏改：glm-5.3 等推理模型默认先出
thinking 块吃光 max_tokens（stop=max_tokens 且 0 个 text 块），空文本被当成
「无内容」推送出去。样板收拢到这里之后：

- thinking=disabled、失败重试、空文本检测内建——新任务 / 换模型只改这一处，
  不再依赖每个 job 各自记得抄齐三件套；
- 返回值契约统一（见下），调用方不必各自记「失败 ≠ 空」的细节。

返回值契约（防谎报核心，所有 job 必须遵守）：

- ``None`` = 失败（网络 / 限流 / 网关 / 空文本）→ 调用方走失败分支：推告警 /
  确定性兜底 / 跳过该小节——绝不能把 None 当「空结果」拼进正常文案；
- ``""`` / ``[]`` = 真空的成功结果，可以正常播报（如「上周 0 篇」）。

用法：jobs/ 内 ``from common import llm_text, http_get, log``
（run.sh 已 cd 到 jobs/，sys.path[0] 即本目录）。
"""

import os
import time
import urllib.request

# 模型：优先用网关配的 haiku 别名（.env 的 ANTHROPIC_DEFAULT_HAIKU），
# 回退到通用 ANTHROPIC_MODEL，再回退 SDK 默认名。jobs 统一从这里取。
MODEL = (
    os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or "claude-haiku-4-5"
)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def llm_text(
    prompt: str,
    *,
    max_tokens: int = 2000,
    model: str | None = None,
    retries: int = 2,
    label: str = "LLM",
) -> str | None:
    """单轮 LLM 调用 → 纯文本。失败 / 空文本返回 None（不抛异常，自带日志）。

    内建三件防谎报处理（08-27 / 08-31 两次事故的教训，走这里的调用自动获得）：
    ① thinking=disabled——推理模型默认先出 thinking 块会吃光 max_tokens
      （stop=max_tokens、0 个 text 块 → 空文本），摘要类任务不需要思考；
    ② 失败重试 retries 次（网关偶发抖动 / 限频）；
    ③ 空文本检测——「成功但 0 字」视同失败，绝不把假成功递给调用方。
    """
    from anthropic import Anthropic  # 延迟 import：顶部不加载重模块
    client = Anthropic()  # 自动用 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL
    for attempt in range(1, retries + 1):
        try:
            r = client.messages.create(
                model=model or MODEL, max_tokens=max_tokens,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in r.content
                           if getattr(b, "type", None) == "text").strip()
            if text:
                return text
            log(f"  ⚠️ {label} 返回空文本（第 {attempt} 次，stop={r.stop_reason}）")
        except Exception as e:
            log(f"  ⚠️ {label} 调用失败（第 {attempt} 次）："
                f"{type(e).__name__} {str(e)[:120]}")
    log(f"  ⚠️ {label} 共 {retries} 试均失败，返回 None（调用方须走失败分支）")
    return None


def http_get(
    url: str,
    *,
    timeout: int = 40,
    attempts: int = 3,
    delays: tuple = (10, 10),
    user_agent: str = "wechat-jobs/1.0",
) -> bytes | None:
    """GET + 重试退避。成功返回 bytes，重试耗尽返回 None。

    delays = 每次重试前的等待秒数（对应第 2..attempts 次；不够长时重复最后一个）。
    429/限流类失败通常几分钟才恢复——间隔别太短，3 秒重试等于白试
    （08-31 arXiv 429 两连挂的教训）。
    """
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            delay = delays[min(attempt - 2, len(delays) - 1)] if delays else 0
            if delay:
                log(f"  退避 {delay}s 后第 {attempt} 次尝试…")
                time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            # 日志带 url 尾段（够定位、不刷屏），重试耗尽后返回 None
            log(f"  抓取失败（第 {attempt} 次 {url.rsplit('/', 1)[-1][:30]}）: {e}")
    return None
