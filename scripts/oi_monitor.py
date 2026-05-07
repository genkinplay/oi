#!/usr/bin/env python3
"""
OI Monitor - GitHub Actions version
Direct port of internal/runner/oi/runner.go, using the same coinank.com API.

Environment variables:
  WEBHOOK_URL             - 旧版通用 webhook（POST {title, content, task}）；多个用逗号分隔
  FEISHU_WEBHOOK_URLS     - 飞书自定义机器人 webhook 列表（逗号分隔）
  DINGTALK_WEBHOOK_URLS   - 钉钉自定义机器人 webhook 列表（逗号分隔）
  DEEPSEEK_API_KEY        - DeepSeek API key（不配则跳过 AI 分析）
  DEEPSEEK_MODEL          - DeepSeek 模型名（默认 deepseek-v4-pro，可选 deepseek-v4-flash）
  THRESHOLD_PCT           - 5m OI 变动阈值（百分比，默认 10）
  THRESHOLD_PCT_15M       - 15m OI 变动阈值（百分比，默认 15）
  TOP_N                   - Number of symbols to monitor (default: 50)
  DEDUP_WINDOW_SEC        - Seconds before re-alerting same symbol (default: 3600)
  DEDUP_FILE              - Path to dedup state file (default: /tmp/oi_dedup.json)
"""

import base64
import json
import math
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# 让本脚本在任意 cwd 下都能 import 同目录下的 notifier 模块
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

CST = timezone(timedelta(hours=8))

import binance_market as bm  # noqa: E402
import indicators as ind  # noqa: E402
import requests
from ai_analyzer import analyze as ai_analyze  # noqa: E402
from notifier import dispatch  # noqa: E402

THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "10"))
THRESHOLD_PCT_15M = float(os.environ.get("THRESHOLD_PCT_15M", "15"))
TOP_N = int(os.environ.get("TOP_N", "50"))
DEDUP_WINDOW_SEC = int(os.environ.get("DEDUP_WINDOW_SEC", "1800"))
DEDUP_FILE = os.environ.get("DEDUP_FILE", "/tmp/oi_dedup.json")

DELISTING_LIST_URL = os.environ.get(
    "DELISTING_LIST_URL",
    "https://raw.githubusercontent.com/genkinplay/oi/refs/heads/main/delisted_symbols.json",
)
DELIST_TAG = "即将下架不建议参与"

# 从下架合约符号里剥离出 base coin 时尝试的 quote 后缀（长的优先）
_QUOTE_SUFFIXES: tuple[str, ...] = (
    "USD_PERP",
    "FDUSD",
    "BUSD",
    "TUSD",
    "USDC",
    "USDT",
    "BTC",
    "ETH",
    "BNB",
    "USD",
)


def generate_api_key() -> str:
    """Port of runner.go generateAPIKey() — reverse-engineered from coinank.com frontend JS."""
    uuid = "b2d903dd-b31e-c547-d299-b6d07b7631ab"
    prefix = uuid[:8]
    shuffled = uuid.replace(prefix, "", 1) + prefix
    ts = f"{int(time.time() * 1000) + 2222222222222}347"
    raw = f"{shuffled}|{ts}"
    return base64.b64encode(raw.encode()).decode()


def fetch_oi(top_n: int, sort_by: str = "openInterestChM5") -> list[dict]:
    url = (
        f"https://api.coinank.com/api/instruments/agg"
        f"?sortBy={sort_by}&sortType=descend&type=oi&page=1&size={top_n}"
    )
    headers = {
        "coinank-apikey": generate_api_key(),
        "Referer": "https://coinank.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "client": "web",
        "web-version": "102",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    body = resp.text[:200]
    print(f"[oi_monitor] api sort={sort_by} status={resp.status_code}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"API returned failure: {body}")
    return data["data"]["list"]


def load_dedup() -> dict[str, float]:
    try:
        with open(DEDUP_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_dedup(state: dict[str, float]) -> None:
    with open(DEDUP_FILE, "w") as f:
        json.dump(state, f)


def _extract_base(symbol: str) -> str | None:
    """把 'AIUSDT' / 'BTCUSD_PERP' / 'ETHBUSD' 等剥离成 base 部分。"""
    s = symbol.upper().strip()
    for q in _QUOTE_SUFFIXES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return None


def fetch_delisted_bases(timeout: int = 10) -> set[str]:
    """从远端 delisted_symbols.json 拉取待下架合约清单，返回其 base coin 集合。
    任何失败都视为空集（不阻塞通知主流程）。"""
    try:
        resp = requests.get(DELISTING_LIST_URL, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 容错，远端拉不到就跳过标签
        print(f"[oi_monitor] 拉取下架清单失败，跳过标签注入：{exc}")
        return set()

    contracts = data.get("all_contracts") or []
    bases: set[str] = set()
    for sym in contracts:
        if not isinstance(sym, str):
            continue
        base = _extract_base(sym)
        if base:
            bases.add(base)
    print(f"[oi_monitor] 下架清单：{len(contracts)} 合约 / {len(bases)} 币种")
    return bases


def _fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    if p >= 0.0001:
        return f"{p:.7f}"
    return f"{p:.10f}"


def _color(pct: float) -> str:
    """涨用红、跌用绿（A 股惯例），平为灰。"""
    if pct > 0:
        return "red"
    if pct < 0:
        return "green"
    return "grey"


def _fmt_pct(pct: float) -> str:
    return f'<font color="{_color(pct)}">**{pct:+.2f}%**</font>'


def _kline_closes(klines: list[list]) -> list[float]:
    return [float(k[4]) for k in klines if len(k) >= 5]


def _kline_highs(klines: list[list]) -> list[float]:
    return [float(k[2]) for k in klines if len(k) >= 5]


def _kline_lows(klines: list[list]) -> list[float]:
    return [float(k[3]) for k in klines if len(k) >= 5]


def _safe_float(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_market_snapshot(
    pair: str, oi_chg5: float, oi_chg15: float
) -> dict[str, Any] | None:
    """采集币安多维行情数据，组装成给 AI 的快照。
    pair 必须是币安存量合约（调用前由 is_perpetual_listed 校验过）。"""
    snapshot: dict[str, Any] = {
        "symbol": pair,
        "oi_change": {
            "5m_pct": round(oi_chg5, 4),
            "15m_pct": round(oi_chg15, 4),
        },
        "unlock_info": "暂无解锁数据；如该标的近期有大额解锁压力，请自行核查",
    }

    t24 = bm.fetch_24h(pair)
    if t24:
        snapshot["ticker_24h"] = {
            "last_price": _safe_float(t24.get("lastPrice")),
            "price_change_pct": _safe_float(t24.get("priceChangePercent")),
            "high": _safe_float(t24.get("highPrice")),
            "low": _safe_float(t24.get("lowPrice")),
            "volume_base": _safe_float(t24.get("volume")),
            "volume_quote": _safe_float(t24.get("quoteVolume")),
        }

    pi = bm.fetch_premium_index(pair)
    if pi:
        next_funding_iso = None
        nft = _safe_float(pi.get("nextFundingTime"))
        if nft:
            next_funding_iso = datetime.fromtimestamp(nft / 1000, CST).isoformat(
                timespec="minutes"
            )
        snapshot["funding"] = {
            "mark_price": _safe_float(pi.get("markPrice")),
            "last_funding_rate_pct": (
                round(_safe_float(pi.get("lastFundingRate"), 0) * 100, 4)
                if pi.get("lastFundingRate") is not None
                else None
            ),
            "next_funding_at_cst": next_funding_iso,
        }

    oi = bm.fetch_open_interest(pair)
    if oi:
        snapshot["open_interest"] = _safe_float(oi.get("openInterest"))

    glsr = bm.fetch_long_short_ratio(pair, period="5m")
    if glsr:
        snapshot["global_long_short_ratio_5m"] = {
            "long_account_pct": _safe_float(glsr.get("longAccount")),
            "short_account_pct": _safe_float(glsr.get("shortAccount")),
            "ratio": _safe_float(glsr.get("longShortRatio")),
        }

    tlsr = bm.fetch_top_trader_long_short_ratio(pair, period="5m")
    if tlsr:
        snapshot["top_trader_long_short_5m"] = {
            "long_account_pct": _safe_float(tlsr.get("longAccount")),
            "short_account_pct": _safe_float(tlsr.get("shortAccount")),
            "ratio": _safe_float(tlsr.get("longShortRatio")),
        }

    # 多周期 K 线 + 指标
    snapshot["klines"] = {}
    snapshot["indicators"] = {}
    for tf, limit in (("15m", 80), ("1h", 100), ("4h", 100)):
        kl = bm.fetch_klines(pair, interval=tf, limit=limit)
        if not kl:
            continue
        closes = _kline_closes(kl)
        highs = _kline_highs(kl)
        lows = _kline_lows(kl)
        # 只送最后 20 根原始 OHLCV 给 AI（避免 prompt 过长）
        snapshot["klines"][tf] = [
            {
                "open_time": int(k[0]),
                "open": _safe_float(k[1]),
                "high": _safe_float(k[2]),
                "low": _safe_float(k[3]),
                "close": _safe_float(k[4]),
                "volume": _safe_float(k[5]),
            }
            for k in kl[-20:]
        ]
        snapshot["indicators"][tf] = {
            "ma20": ind.sma(closes, 20),
            "ma50": ind.sma(closes, 50),
            "ema12": ind.ema(closes, 12),
            "ema26": ind.ema(closes, 26),
            "macd": ind.macd(closes),
            "atr14": ind.atr(highs, lows, closes, 14),
        }

    return snapshot


def _fmt_ai_block(ai: dict[str, Any]) -> tuple[str, str]:
    """把 AI 输出格式化成 (text, markdown) 两份。"""
    intervene = bool(ai.get("intervene"))
    if not intervene:
        reasoning = (ai.get("reasoning") or "").strip()
        text = f"建议：观望\n理由：{reasoning}" if reasoning else "建议：观望"
        md = (
            f"> **建议** 观望\n> \n> {reasoning}"
            if reasoning
            else "> **建议** 观望"
        )
        return text, md

    direction = ai.get("direction") or "?"
    direction_cn = {"long": "做多", "short": "做空"}.get(direction, direction)
    direction_color = (
        "red" if direction == "long" else ("green" if direction == "short" else "grey")
    )

    entry = ai.get("entry_price")
    sl = ai.get("stop_loss")
    conf = ai.get("confidence")
    reasoning = (ai.get("reasoning") or "").strip()

    def _fmt_price_safe(v: Any) -> str:
        f = _safe_float(v)
        return _fmt_price(f) if f is not None else "—"

    text_lines = [
        f"建议：{direction_cn}（轻仓）| 置信度 {conf}",
        f"介入：{_fmt_price_safe(entry)}  止损：{_fmt_price_safe(sl)}",
    ]
    if reasoning:
        text_lines.append(f"理由：{reasoning}")

    md_lines = [
        f'> **建议** <font color="{direction_color}">**{direction_cn}**</font>'
        f"（轻仓）｜ 置信度 **{conf}**",
        f"> ",
        f"> 介入 `{_fmt_price_safe(entry)}` 止损 `{_fmt_price_safe(sl)}`",
    ]
    if reasoning:
        md_lines.append("> ")
        md_lines.append(f"> {reasoning}")

    return "\n".join(text_lines), "\n".join(md_lines)


def notify_alerts(text_alerts: list[str], md_alerts: list[str]) -> None:
    """同时构造纯文本（给老 webhook）和 markdown（给飞书/钉钉）。"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    title = "异动通知"

    body = "\n—————————————\n".join(text_alerts)
    content = f"时间：{now}\n—————————————\n{body}"

    md_body = "\n\n---\n\n".join(md_alerts)
    markdown = f"**时间** {now}\n\n---\n\n{md_body}"

    result = dispatch(
        title=title,
        content=content,
        markdown=markdown,
        task="oi_monitor",
    )
    print(
        f"[oi_monitor] notify sent={result.sent} failed={result.failed} "
        f"total={result.total}"
    )


def main() -> None:
    print(
        f"[oi_monitor] threshold 5m={THRESHOLD_PCT}% 15m={THRESHOLD_PCT_15M}% "
        f"top_n={TOP_N} dedup_window={DEDUP_WINDOW_SEC}s"
    )

    # 分别按 5m 和 15m 排序拉两次 top3，合并候选 → OR 阈值
    items_5m = fetch_oi(TOP_N, sort_by="openInterestChM5")
    items_15m = fetch_oi(TOP_N, sort_by="openInterestChM15")

    # coinank 在不同 sortBy 下返回的同一标的字段值会缺失或 None，
    # 先把两份**全表**做字段级 union（缺什么补什么），再从中按候选顺序取。
    merged: dict[str, dict] = {}
    for it in items_5m + items_15m:
        base = it.get("baseCoin")
        if not base:
            continue
        if base not in merged:
            merged[base] = dict(it)
        else:
            for k, v in it.items():
                if merged[base].get(k) is None and v is not None:
                    merged[base][k] = v

    candidate_order: list[str] = []
    seen: set[str] = set()
    for it in items_5m[:3] + items_15m[:3]:
        base = it.get("baseCoin")
        if base and base not in seen:
            seen.add(base)
            candidate_order.append(base)
    items = [merged[b] for b in candidate_order if b in merged]

    top5m = " | ".join(
        f"{x.get('baseCoin','')} {(x.get('openInterestChM5') or 0)*100:.2f}%"
        for x in items_5m[:3]
    )
    top15m = " | ".join(
        f"{x.get('baseCoin','')} {(x.get('openInterestChM15') or 0)*100:.2f}%"
        for x in items_15m[:3]
    )
    print(
        f"[oi_monitor] fetched 5m={len(items_5m)} 15m={len(items_15m)} "
        f"candidates={len(items)}"
    )
    print(f"[oi_monitor] top3@5m:  {top5m}")
    print(f"[oi_monitor] top3@15m: {top15m}")

    delisted_bases = fetch_delisted_bases()

    dedup = load_dedup()
    text_alerts: list[str] = []
    md_alerts: list[str] = []
    # signals = 5m / 15m 至少一项过阈值且不在 dedup 窗口内的标的（含被币安过滤的）
    signals: list[dict[str, Any]] = []

    for item in items:
        symbol = item.get("baseCoin")
        if not symbol:
            continue
        chg5 = (item.get("openInterestChM5") or 0) * 100
        chg15 = (item.get("openInterestChM15") or 0) * 100
        # OR 触发：任一周期超阈值
        if math.fabs(chg5) < THRESHOLD_PCT and math.fabs(chg15) < THRESHOLD_PCT_15M:
            continue

        pair = f"{symbol.upper()}USDT"

        # dedup 跳过：仍然记入 signals 让 summary 看得见
        last = dedup.get(symbol)
        if last and (time.time() - last) < DEDUP_WINDOW_SEC:
            mins_ago = int((time.time() - last) / 60)
            print(f"[oi_monitor] skip {pair}: dedup（{mins_ago}m 前已告警）")
            signals.append(
                {
                    "pair": pair,
                    "chg5": chg5,
                    "chg15": chg15,
                    "passed": False,
                    "reason": f"dedup 内（{mins_ago}m 前已告警）",
                }
            )
            continue

        price = item.get("price") or 0

        # 币安永续过滤：明确不存在则跳过；清单不可用（如 451）会保守放行
        if not bm.is_perpetual_listed(pair):
            print(f"[oi_monitor] skip {pair}: 币安无此 USDT 永续合约")
            signals.append(
                {
                    "pair": pair,
                    "chg5": chg5,
                    "chg15": chg15,
                    "passed": False,
                    "reason": "币安无此 USDT 永续",
                }
            )
            continue

        reason = "清单不可用，保守放行" if bm._EXCHANGE_INFO_UNAVAILABLE else None
        signals.append(
            {
                "pair": pair,
                "chg5": chg5,
                "chg15": chg15,
                "passed": True,
                "reason": reason,
            }
        )

        is_delisted = symbol.upper() in delisted_bases

        text = (
            f"{pair}\n"
            f"5m 变动：{chg5:+.2f}%\n"
            f"15m 变动：{chg15:+.2f}%\n"
            f"当前价格：{_fmt_price(price)}"
        )
        md = (
            f"### {pair}\n"
            f"> **5m** {_fmt_pct(chg5)} ｜ **15m** {_fmt_pct(chg15)}\n"
            f"> \n"
            f"> 价格 `{_fmt_price(price)}`"
        )
        if is_delisted:
            text += f"\n{DELIST_TAG}"
            md += f'\n> \n> <font color="red">**{DELIST_TAG}**</font>'
            # 即将下架的标的不参与交易判断，直接推送
        else:
            # 行情快照 + 建议
            snapshot = build_market_snapshot(pair, chg5, chg15)
            if snapshot:
                ai = ai_analyze(snapshot)
                if ai:
                    ai_text, ai_md = _fmt_ai_block(ai)
                    text += "\n" + ai_text
                    md += "\n> \n" + ai_md

        text_alerts.append(text)
        md_alerts.append(md)
        dedup[symbol] = time.time()

    save_dedup(dedup)
    print(f"[oi_monitor] signals: {len(signals)} alerts: {len(text_alerts)}")

    write_run_marker(signals, text_alerts, top5m, top15m)

    if not text_alerts:
        return

    for a in text_alerts:
        print(f"[oi_monitor] ALERT {a}")

    notify_alerts(text_alerts, md_alerts)


def write_run_marker(
    signals: list[dict[str, Any]],
    text_alerts: list[str],
    top5m: str,
    top15m: str,
) -> None:
    """在 GitHub Actions 列表行打可见标记。
    - signal 数 = 5m / 15m 任一过阈值且不在 dedup 窗口内的标的（含被币安过滤的）
    - alert  数 = signal 中通过币安过滤、最终会推送的告警
    用 ::notice 让列表行右侧出现蓝色 ℹ️ 标记；step summary 给详情页明细。
    """
    n_signals = len(signals)
    n_alerts = len(text_alerts)

    if n_signals > 0:
        title = f"信号 {n_signals} → 告警 {n_alerts}"
        details = ", ".join(
            (
                f"{s['pair']} 5m {s['chg5']:+.2f}% / 15m {s['chg15']:+.2f}%"
                + (
                    ""
                    if s["passed"] and not s.get("reason")
                    else (
                        f" [{'过滤' if not s['passed'] else '注意'}：{s['reason']}]"
                    )
                )
            )
            for s in signals
        )
        print(f"::notice title={title}::{details}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            if n_signals > 0:
                f.write(f"## 信号 {n_signals} → 告警 {n_alerts}\n\n")
                f.write("| 标的 | 5m 变动 | 15m 变动 | 状态 |\n")
                f.write("| --- | --- | --- | --- |\n")
                for s in signals:
                    if s["passed"]:
                        status = "通过" if not s.get("reason") else f"通过（{s['reason']}）"
                    else:
                        status = f"过滤：{s['reason']}"
                    f.write(
                        f"| `{s['pair']}` | {s['chg5']:+.2f}% | "
                        f"{s['chg15']:+.2f}% | {status} |\n"
                    )
                f.write("\n")
            else:
                f.write("## 本轮无满足阈值的信号\n\n")
            f.write(f"Top3 by 5m:{top5m}\n\n")
            f.write(f"Top3 by 15m:{top15m}\n")
    except OSError as exc:
        print(f"[oi_monitor] 写入 step summary 失败：{exc}")


if __name__ == "__main__":
    main()
