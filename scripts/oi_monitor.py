#!/usr/bin/env python3
"""
OI Monitor - GitHub Actions version
Direct port of internal/runner/oi/runner.go, using the same coinank.com API.

Environment variables:
  WEBHOOK_URL         - Webhook URL to POST alerts to
  THRESHOLD_PCT       - Alert threshold in percent (default: 10)
  TOP_N               - Number of symbols to monitor (default: 50)
  DEDUP_WINDOW_SEC    - Seconds before re-alerting same symbol (default: 3600)
  DEDUP_FILE          - Path to dedup state file (default: /tmp/oi_dedup.json)
"""

import base64
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

import requests

THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "10"))
TOP_N = int(os.environ.get("TOP_N", "50"))
DEDUP_WINDOW_SEC = int(os.environ.get("DEDUP_WINDOW_SEC", "3600"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
DEDUP_FILE = os.environ.get("DEDUP_FILE", "/tmp/oi_dedup.json")


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


def send_webhook(url: str, alerts: list[str]) -> None:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    body = "\n—————————————\n".join(alerts)
    content = f"🚨 OI 异动提醒\n时间: {now}\n—————————————\n{body}"
    payload = {"title": "OI 异动告警", "content": content, "task": "oi_monitor"}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()


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

    dedup = load_dedup()
    alerts: list[str] = []

    alert_count = 0
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
        alert_count += 1
        chg5 = change_pct
        chg15 = (item.get("openInterestChM15") or 0) * 100
        price = item.get("price") or 0
        exchange = item.get("exchangeName") or ""
        pair = item.get("symbol") or f"{symbol}USDT"
        alert_lines = (
            f"{pair}\n"
            f"5m 变动: {chg5:+.2f}%\n"
            f"15m 变动: {chg15:+.2f}%\n"
            f"当前价格: {_fmt_price(price)}"
        )
        alerts.append(alert_lines)
        dedup[symbol] = time.time()

    save_dedup(dedup)
    print(f"[oi_monitor] alerts: {len(alerts)}")

    if not alerts:
        return

    for a in alerts:
        print(f"[oi_monitor] ALERT {a}")

    if WEBHOOK_URL:
        send_webhook(WEBHOOK_URL, alerts)
        print("[oi_monitor] webhook sent")
    else:
        print("[oi_monitor] WEBHOOK_URL not set, skipping notification")


if __name__ == "__main__":
    main()
