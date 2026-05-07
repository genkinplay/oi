"""Lambda 版 OI 异动监控主流程。

跟 GitHub Actions 版（scripts/oi_monitor.py）的差异：
  - dedup 用 DynamoDB（dedup.py）
  - 没有 step summary / ::notice（Lambda 不需要）
  - 通过 lambda_function.py 入口，run_once() 返回结构化结果便于 CloudWatch 查阅
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import alerts
import binance_market as bm
import indicators as ind
from ai_analyzer import analyze as ai_analyze
from coinank import OIItem, fetch_two_axis, top_preview
from dedup import get_last_notified_at, upsert as dedup_upsert
from delisting import fetch_delisted_bases
from notifier import dispatch

CST = timezone(timedelta(hours=8))
SANITY_CAP_PCT = 500.0
DELIST_TAG = "即将下架不建议参与"


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "").strip() or default)
    except ValueError:
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, "").strip() or default)
    except ValueError:
        return default


def run_once() -> dict[str, Any]:
    threshold_5m = _f("THRESHOLD_PCT", 10.0)
    threshold_15m = _f("THRESHOLD_PCT_15M", 15.0)
    top_n = _i("TOP_N", 50)
    dedup_window_sec = _i("DEDUP_WINDOW_SEC", 1800)

    print(
        f"[oi_monitor] threshold 5m={threshold_5m}% 15m={threshold_15m}% "
        f"top_n={top_n} dedup_window={dedup_window_sec}s"
    )

    # 1) coinank 双拉
    try:
        r = fetch_two_axis(top_n=top_n, pick_per_sort=3)
    except Exception as exc:  # noqa: BLE001
        print(f"[oi_monitor] coinank 拉取失败：{exc}")
        return {"signals": 0, "alerts": 0, "error": str(exc)}

    print(
        f"[oi_monitor] fetched 5m={len(r.items_5m)} 15m={len(r.items_15m)} "
        f"candidates={len(r.candidates)}"
    )
    print(f"[oi_monitor] top3@5m: {top_preview(r.items_5m, 3, True)}")
    print(f"[oi_monitor] top3@15m: {top_preview(r.items_15m, 3, False)}")

    # 2) 下架清单
    delisted = fetch_delisted_bases()
    print(f"[oi_monitor] 下架清单：{len(delisted)} 币种")

    # 3) 阈值过滤 + dedup + 币安过滤 + AI + 通知
    text_alerts: list[str] = []
    md_alerts: list[str] = []
    signals: list[dict[str, Any]] = []

    for it in r.candidates:
        base = it.base_coin.upper().strip()
        if not base:
            continue
        chg5 = (it.oi_chg5 or 0) * 100
        chg15 = (it.oi_chg15 or 0) * 100

        # OR 阈值
        if math.fabs(chg5) < threshold_5m and math.fabs(chg15) < threshold_15m:
            continue
        # 脏数据兜底
        if math.fabs(chg5) > SANITY_CAP_PCT or math.fabs(chg15) > SANITY_CAP_PCT:
            print(f"[oi_monitor] skip {base}: insane change 5m={chg5} 15m={chg15}")
            continue

        pair = f"{base}USDT"

        # dedup（DynamoDB）
        last_ms = get_last_notified_at(base)
        if last_ms is not None:
            elapsed = time.time() * 1000 - last_ms
            if elapsed < dedup_window_sec * 1000:
                mins_ago = int(elapsed / 60000)
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

        # 币安永续过滤：区分"完全不存在" vs "结算/即将摘牌"，便于排查
        if not bm.is_perpetual_listed(pair):
            status = bm.get_listing_status(pair)
            if status is None:
                log_msg = f"币安无此 USDT 永续合约"
                reason = "币安无此 USDT 永续"
            else:
                log_msg = f"币安永续状态 {status}（非 TRADING）"
                reason = f"币安永续 {status}"
            print(f"[oi_monitor] skip {pair}: {log_msg}")
            signals.append(
                {
                    "pair": pair,
                    "chg5": chg5,
                    "chg15": chg15,
                    "passed": False,
                    "reason": reason,
                }
            )
            continue

        signals.append(
            {
                "pair": pair,
                "chg5": chg5,
                "chg15": chg15,
                "passed": True,
                "reason": None,
            }
        )

        is_delisted = base in delisted
        text, md = _build_base_alert(pair, chg5, chg15, it.price, is_delisted)

        ai_with_intervene: dict | None = None  # 用于写 alerts 历史
        if not is_delisted:
            snap = _build_market_snapshot(pair, chg5, chg15)
            ai = ai_analyze(snap)
            if ai:
                ai_text, ai_md = _format_suggestion(ai)
                text += "\n" + ai_text
                md += "\n> \n" + ai_md
                if ai.get("intervene"):
                    ai_with_intervene = ai

        text_alerts.append(text)
        md_alerts.append(md)
        dedup_upsert(base)

        # 写入 alerts 历史（24h 复盘统计用）。仅当 AI 给了明确介入方向才记录。
        if ai_with_intervene:
            direction = ai_with_intervene.get("direction") or ""
            entry_raw = ai_with_intervene.get("entry_price")
            try:
                entry_price = float(entry_raw) if entry_raw is not None else float(it.price or 0)
            except (TypeError, ValueError):
                entry_price = float(it.price or 0)
            if direction in ("long", "short") and entry_price > 0:
                alerts.record(
                    base=base,
                    pair=pair,
                    direction=direction,
                    entry_price=entry_price,
                    confidence=int(ai_with_intervene.get("confidence") or 0),
                    reasoning=(ai_with_intervene.get("reasoning") or "").strip(),
                )

    print(f"[oi_monitor] signals: {len(signals)} alerts: {len(text_alerts)}")

    if not text_alerts:
        return {"signals": len(signals), "alerts": 0}

    # 4) 通知
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"时间：{now}\n—————————————\n"
        + "\n—————————————\n".join(text_alerts)
    )
    markdown = (
        f"**时间** {now}\n\n---\n\n"
        + "\n\n---\n\n".join(md_alerts)
    )
    result = dispatch(
        title="异动通知",
        content=content,
        markdown=markdown,
        task="oi_monitor",
    )
    print(
        f"[oi_monitor] notify sent={result.sent} failed={result.failed} total={result.total}"
    )
    return {
        "signals": len(signals),
        "alerts": len(text_alerts),
        "notify_sent": result.sent,
        "notify_failed": result.failed,
    }


# --- 行情快照（喂给 AI） ---


def _build_market_snapshot(pair: str, chg5: float, chg15: float) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "symbol": pair,
        "oi_change": {
            "5m_pct": round(chg5, 4),
            "15m_pct": round(chg15, 4),
        },
        "unlock_info": "暂无解锁数据；如该标的近期有大额解锁压力，请自行核查",
    }

    t24 = bm.fetch_24h(pair)
    if t24:
        snap["ticker_24h"] = {
            "last_price": _safe_float(t24.get("lastPrice")),
            "price_change_pct": _safe_float(t24.get("priceChangePercent")),
            "high": _safe_float(t24.get("highPrice")),
            "low": _safe_float(t24.get("lowPrice")),
            "volume_base": _safe_float(t24.get("volume")),
            "volume_quote": _safe_float(t24.get("quoteVolume")),
        }
    pi = bm.fetch_premium_index(pair)
    if pi:
        nft = _safe_float(pi.get("nextFundingTime"))
        next_funding_iso = (
            datetime.fromtimestamp(nft / 1000, CST).isoformat(timespec="minutes")
            if nft
            else None
        )
        snap["funding"] = {
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
        snap["open_interest"] = _safe_float(oi.get("openInterest"))
    glsr = bm.fetch_long_short_ratio(pair, period="5m")
    if glsr:
        snap["global_long_short_ratio_5m"] = {
            "long_account_pct": _safe_float(glsr.get("longAccount")),
            "short_account_pct": _safe_float(glsr.get("shortAccount")),
            "ratio": _safe_float(glsr.get("longShortRatio")),
        }
    tlsr = bm.fetch_top_trader_long_short_ratio(pair, period="5m")
    if tlsr:
        snap["top_trader_long_short_5m"] = {
            "long_account_pct": _safe_float(tlsr.get("longAccount")),
            "short_account_pct": _safe_float(tlsr.get("shortAccount")),
            "ratio": _safe_float(tlsr.get("longShortRatio")),
        }

    snap["klines"] = {}
    snap["indicators"] = {}
    for tf, limit in (("15m", 80), ("1h", 100), ("4h", 100)):
        kl = bm.fetch_klines(pair, interval=tf, limit=limit)
        if not kl:
            continue
        closes = [float(k[4]) for k in kl if len(k) >= 5]
        highs = [float(k[2]) for k in kl if len(k) >= 5]
        lows = [float(k[3]) for k in kl if len(k) >= 5]
        snap["klines"][tf] = [
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
        snap["indicators"][tf] = {
            "ma20": ind.sma(closes, 20),
            "ma50": ind.sma(closes, 50),
            "ema12": ind.ema(closes, 12),
            "ema26": ind.ema(closes, 26),
            "macd": ind.macd(closes),
            "atr14": ind.atr(highs, lows, closes, 14),
        }
    return snap


# --- 文本 / markdown 格式化 ---


def _color(pct: float) -> str:
    if pct > 0:
        return "red"
    if pct < 0:
        return "green"
    return "grey"


def _fmt_pct(pct: float) -> str:
    return f'<font color="{_color(pct)}">**{pct:+.2f}%**</font>'


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


def _build_base_alert(
    pair: str, chg5: float, chg15: float, price: float, is_delisted: bool
) -> tuple[str, str]:
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
    return text, md


def _format_suggestion(ai: dict) -> tuple[str, str]:
    if not ai.get("intervene"):
        reasoning = (ai.get("reasoning") or "").strip()
        if reasoning:
            return "建议：观望\n理由：" + reasoning, "> **建议** 观望\n> \n> " + reasoning
        return "建议：观望", "> **建议** 观望"

    direction = ai.get("direction") or "?"
    direction_cn = {"long": "做多", "short": "做空"}.get(direction, direction)
    direction_color = (
        "red" if direction == "long" else ("green" if direction == "short" else "grey")
    )
    entry = _safe_float(ai.get("entry_price"))
    sl = _safe_float(ai.get("stop_loss"))
    conf = ai.get("confidence")
    reasoning = (ai.get("reasoning") or "").strip()

    def _p(v: float | None) -> str:
        return _fmt_price(v) if v is not None else "—"

    text_lines = [
        f"建议：{direction_cn}（轻仓）| 置信度 {conf}",
        f"介入：{_p(entry)}  止损：{_p(sl)}",
    ]
    if reasoning:
        text_lines.append(f"理由：{reasoning}")
    md_lines = [
        f'> **建议** <font color="{direction_color}">**{direction_cn}**</font>'
        f"（轻仓）｜ 置信度 **{conf}**",
        "> ",
        f"> 介入 `{_p(entry)}` 止损 `{_p(sl)}`",
    ]
    if reasoning:
        md_lines.extend(["> ", f"> {reasoning}"])
    return "\n".join(text_lines), "\n".join(md_lines)


def _safe_float(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default
