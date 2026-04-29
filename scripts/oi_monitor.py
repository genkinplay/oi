#!/usr/bin/env python3
"""
OI Monitor - GitHub Actions version
Direct port of internal/runner/oi/runner.go, using the same coinank.com API.

Environment variables:
  WEBHOOK_URL             - 旧版通用 webhook（POST {title, content, task}）；多个用逗号分隔
  FEISHU_WEBHOOK_URLS     - 飞书自定义机器人 webhook 列表（逗号分隔）
  DINGTALK_WEBHOOK_URLS   - 钉钉自定义机器人 webhook 列表（逗号分隔）
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

# 让本脚本在任意 cwd 下都能 import 同目录下的 notifier 模块
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

CST = timezone(timedelta(hours=8))

import requests
from notifier import dispatch  # noqa: E402

THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "10"))
TOP_N = int(os.environ.get("TOP_N", "50"))
DEDUP_WINDOW_SEC = int(os.environ.get("DEDUP_WINDOW_SEC", "3600"))
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
        print(f"[oi_monitor] 拉取下架清单失败，跳过标签注入: {exc}")
        return set()

    contracts = data.get("all_contracts") or []
    bases: set[str] = set()
    for sym in contracts:
        if not isinstance(sym, str):
            continue
        base = _extract_base(sym)
        if base:
            bases.add(base)
    print(f"[oi_monitor] 下架清单: {len(contracts)} 合约 / {len(bases)} 币种")
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
    return f"<font color=\"{_color(pct)}\">**{pct:+.2f}%**</font>"


def notify_alerts(text_alerts: list[str], md_alerts: list[str]) -> None:
    """同时构造纯文本（给老 webhook）和 markdown（给飞书/钉钉）。"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    title = "异动通知"

    body = "\n—————————————\n".join(text_alerts)
    content = f"时间: {now}\n—————————————\n{body}"

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
        pair = item.get("symbol") or f"{symbol}USDT"
        is_delisted = symbol.upper() in delisted_bases

        text = (
            f"{pair}\n"
            f"5m 变动: {chg5:+.2f}%\n"
            f"15m 变动: {chg15:+.2f}%\n"
            f"当前价格: {_fmt_price(price)}"
        )
        # 钉钉 markdown 富文本：### ticker + 引用块包数据 + 涨跌染色
        md = (
            f"### {pair}\n"
            f"> **5m** {_fmt_pct(chg5)}　｜　**15m** {_fmt_pct(chg15)}\n"
            f"> \n"
            f"> 价格 `{_fmt_price(price)}`"
        )
        if is_delisted:
            text += f"\n{DELIST_TAG}"
            md += (
                f"\n> \n"
                f"> <font color=\"red\">**{DELIST_TAG}**</font>"
            )

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
