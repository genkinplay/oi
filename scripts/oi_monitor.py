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
  THRESHOLD_PCT           - Alert threshold in percent (default: 10)
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


def fetch_oi(top_n: int) -> list[dict]:
    url = (
        f"https://api.coinank.com/api/instruments/agg"
        f"?sortBy=openInterestChM5&sortType=descend&type=oi&page=1&size={top_n}"
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
    print(f"[oi_monitor] api status={resp.status_code} body={body}")
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
        f"> 介入 `{_fmt_price_safe(entry)}`　止损 `{_fmt_price_safe(sl)}`",
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
        f"[oi_monitor] threshold={THRESHOLD_PCT}% top_n={TOP_N} dedup_window={DEDUP_WINDOW_SEC}s"
    )

    items = fetch_oi(TOP_N)
    top3 = []
    for i, item in enumerate(items[:3]):
        symbol = item.get("baseCoin", "")
        chg = item.get("openInterestChM5", 0) * 100
        top3.append(f"{symbol} {chg:.2f}%")
    print(f"[oi_monitor] fetched={len(items)} top3: {' | '.join(top3)}")

    delisted_bases = fetch_delisted_bases()

    dedup = load_dedup()
    text_alerts: list[str] = []
    md_alerts: list[str] = []

    for item in items[:3]:
        symbol = item.get("baseCoin")
        change_raw = item.get("openInterestChM5")
        if not symbol or change_raw is None:
            continue
        change_pct = change_raw * 100
        if math.fabs(change_pct) < THRESHOLD_PCT:
            continue
        last = dedup.get(symbol)
        if last and (time.time() - last) < DEDUP_WINDOW_SEC:
            continue

        chg5 = change_pct
        chg15 = (item.get("openInterestChM15") or 0) * 100
        price = item.get("price") or 0
        # coinank 返回的 item.symbol 经常是 <BASE>PERP 形式（如 1000NEIROCTOPERP），
        # 跟币安 fapi 的 <BASE>USDT 命名不一致，直接用 baseCoin 拼 USDT 永续。
        pair = f"{symbol.upper()}USDT"

        # 币安永续不存在 → 跳过整条告警（不再通知）
        if not bm.is_perpetual_listed(pair):
            print(f"[oi_monitor] skip {pair}: 币安无此 USDT 永续合约")
            continue

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
    print(f"[oi_monitor] alerts: {len(text_alerts)}")

    if not text_alerts:
        return

    for a in text_alerts:
        print(f"[oi_monitor] ALERT {a}")

    notify_alerts(text_alerts, md_alerts)


if __name__ == "__main__":
    main()
