"""24h OI 信号复盘统计。

每天 CST 20:00（UTC 12:00）由 EventBridge 调度，统计：
  - 24h 内推送过的所有 alert（同一 base 取最早一次）
  - 假设按介入价介入，到现在为止能拿到的最大盈利空间
    - long → 取从介入时间到现在的 max(high)
    - short → 取从介入时间到现在的 min(low)
  - 仅展示盈利的标的，按盈利百分比降序

通知文案兼顾钉钉 markdown 表格和飞书 / 通用 webhook 的纯文本。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import binance_market as bm
from alerts import list_recent
from notifier import dispatch

CST = timezone(timedelta(hours=8))

# 仅统计盈利幅度严格大于该阈值的标的，避免噪音
MIN_PROFIT_PCT = 2.0


def run_summary() -> dict[str, Any]:
    items = list_recent(hours=24)
    print(f"[summary] 24h 内告警：{len(items)} 条")

    # 同一 base 取最早一次告警
    earliest: dict[str, dict] = {}
    for it in items:
        base = it.get("base")
        ts = int(it.get("notified_at_ms", 0))
        if not base:
            continue
        cur = earliest.get(base)
        if cur is None or ts < int(cur["notified_at_ms"]):
            earliest[base] = it

    profits: list[dict] = []
    for base, it in earliest.items():
        try:
            entry = float(it.get("entry_price", 0))
            if entry <= 0:
                continue
            pair = it.get("pair") or f"{base}USDT"
            direction = it.get("direction", "")
            ts_ms = int(it.get("notified_at_ms", 0))

            extreme = _fetch_extreme(pair, ts_ms, direction)
            if extreme is None:
                continue
            extreme_price, extreme_ts_ms = extreme

            if direction == "long":
                profit_pct = (extreme_price - entry) / entry * 100
            elif direction == "short":
                profit_pct = (entry - extreme_price) / entry * 100
            else:
                continue

            if profit_pct <= MIN_PROFIT_PCT:
                continue

            profits.append(
                {
                    "base": base,
                    "pair": pair,
                    "direction": direction,
                    "entry": entry,
                    "extreme": extreme_price,
                    "extreme_ts_ms": extreme_ts_ms,
                    "profit_pct": profit_pct,
                    "ts_ms": ts_ms,
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[summary] {base} 处理失败：{exc}")

    profits.sort(key=lambda x: x["profit_pct"], reverse=True)

    text, md = _format(profits, len(earliest))
    if not earliest:
        # 24h 完全没有有效 alert 时不发通知，避免噪音
        print("[summary] 24h 无有效信号，跳过通知")
        return {"signals": 0, "winners": 0}

    result = dispatch(
        title="📊 24h 信号复盘",
        content=text,
        markdown=md,
        task="oi_monitor",
    )
    print(f"[summary] notify sent={result.sent} failed={result.failed}")
    return {"signals": len(earliest), "winners": len(profits)}


# --- 行情取数 ---


def _fetch_extreme(pair: str, since_ms: int, direction: str) -> tuple[float, int] | None:
    """从 since_ms 起拉 5m K 线，返回 (极值价，极值出现的 K 线开盘 ms)。
    long → 最高价；short → 最低价。"""
    if direction not in ("long", "short"):
        return None
    klines = bm.fetch_klines(pair, interval="5m", limit=288)  # 24h × 12 = 288
    if not klines:
        return None
    # k 结构：[openTime, o, h, l, c, v, ...]
    in_window = [k for k in klines if int(k[0]) >= since_ms]
    if not in_window:
        return None
    if direction == "long":
        best = max(in_window, key=lambda k: float(k[2]))
        return float(best[2]), int(best[0])
    best = min(in_window, key=lambda k: float(k[3]))
    return float(best[3]), int(best[0])


# --- 格式化 ---


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


def _format(profits: list[dict], total_signals: int) -> tuple[str, str]:
    if not profits:
        text = f"24h 复盘：{total_signals} 个信号 / 0 盈利 >{MIN_PROFIT_PCT:g}%"
        md = (
            f"### 24h 信号复盘\n\n"
            f"24h 共 **{total_signals}** 个信号，目前**没有**标的盈利幅度超过 "
            f"**{MIN_PROFIT_PCT:g}%**."
        )
        return text, md

    # 纯文本（旧版通用 webhook 用）—— 紧凑一行一个标的
    text_lines = [
        f"24h 复盘 {len(profits)}/{total_signals} 盈利 >{MIN_PROFIT_PCT:g}%"
    ]
    for p in profits:
        sig_t = datetime.fromtimestamp(p["ts_ms"] / 1000, CST).strftime("%H:%M")
        peak_t = datetime.fromtimestamp(p["extreme_ts_ms"] / 1000, CST).strftime("%H:%M")
        dir_cn = "多" if p["direction"] == "long" else "空"
        text_lines.append(
            f"• {p['pair']} {dir_cn} ${_fmt_price(p['entry'])} (信号 {sig_t}) "
            f"→ +{p['profit_pct']:.2f}% (高点 {peak_t})"
        )
    text = "\n".join(text_lines)

    # markdown（钉钉 / 飞书）—— 卡片式：每个标的两行，移动端阅读友好
    md_lines = [
        f"### 24h 信号复盘",
        f"共 **{total_signals}** 个信号，**{len(profits)}** 个盈利幅度超过 "
        f"**{MIN_PROFIT_PCT:g}%**",
        "",
        "---",
        "",
    ]
    for i, p in enumerate(profits, 1):
        sig_t = datetime.fromtimestamp(p["ts_ms"] / 1000, CST).strftime("%m-%d %H:%M")
        peak_t = datetime.fromtimestamp(p["extreme_ts_ms"] / 1000, CST).strftime("%m-%d %H:%M")
        is_long = p["direction"] == "long"
        dir_cn = "做多" if is_long else "做空"
        dir_color = "red" if is_long else "green"
        peak_label = "高点" if is_long else "低点"
        md_lines.extend(
            [
                f"**{i}. {p['pair']}** ｜ "
                f'<font color="{dir_color}">**{dir_cn}**</font> ｜ '
                f'<font color="red">**+{p["profit_pct"]:.2f}%**</font>',
                f"> 介入 `{_fmt_price(p['entry'])}` (信号 {sig_t}) → "
                f"{peak_label} `{_fmt_price(p['extreme'])}` ({peak_t})",
                "",
            ]
        )
    md_lines.extend(
        [
            "---",
            "",
            "> 仅统计从信号到当前的最大盈利幅度，仅供回顾参考。",
        ]
    )
    md = "\n".join(md_lines)
    return text, md
