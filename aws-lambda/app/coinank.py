"""coinank OI 异动数据采集（双 sortBy + 字段级合并）。

输出 candidates 列表：每个候选标的的 baseCoin、价格、5m / 15m 变动率
都已从两次拉取中字段级 union 取最完整值，避免 None 被当 0 处理。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import requests

API_URL = "https://api.coinank.com/api/instruments/agg"
API_KEY_UUID = "b2d903dd-b31e-c547-d299-b6d07b7631ab"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class OIItem:
    base_coin: str
    symbol: str
    price: float
    oi_chg5: float | None  # 原始小数（0.0636 = 6.36%）
    oi_chg15: float | None


def _generate_api_key() -> str:
    prefix = API_KEY_UUID[:8]
    shuffled = API_KEY_UUID.replace(prefix, "", 1) + prefix
    ts = f"{int(time.time() * 1000) + 2222222222222}347"
    raw = f"{shuffled}|{ts}"
    return base64.b64encode(raw.encode()).decode()


def _fetch_one(top_n: int, sort_by: str) -> list[dict]:
    url = (
        f"{API_URL}?sortBy={sort_by}&sortType=descend&type=oi&page=1&size={top_n}"
    )
    headers = {
        "coinank-apikey": _generate_api_key(),
        "Referer": "https://coinank.com/",
        "User-Agent": USER_AGENT,
        "client": "web",
        "web-version": "102",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"coinank {sort_by} returned failure")
    return data.get("data", {}).get("list", []) or []


def _to_item(d: dict) -> OIItem:
    return OIItem(
        base_coin=(d.get("baseCoin") or "").strip(),
        symbol=d.get("symbol") or "",
        price=float(d.get("price") or 0),
        oi_chg5=d.get("openInterestChM5"),
        oi_chg15=d.get("openInterestChM15"),
    )


def _merge_field(merged: dict[str, OIItem], item: OIItem) -> None:
    """字段级 union：缺失字段从对方填补，避免 None 被当 0。"""
    cur = merged.get(item.base_coin)
    if cur is None:
        merged[item.base_coin] = item
        return
    if cur.oi_chg5 is None and item.oi_chg5 is not None:
        cur.oi_chg5 = item.oi_chg5
    if cur.oi_chg15 is None and item.oi_chg15 is not None:
        cur.oi_chg15 = item.oi_chg15
    if cur.price == 0 and item.price != 0:
        cur.price = item.price
    if not cur.symbol and item.symbol:
        cur.symbol = item.symbol


@dataclass
class CoinankResult:
    items_5m: list[OIItem]
    items_15m: list[OIItem]
    candidates: list[OIItem]


def fetch_two_axis(top_n: int = 50, pick_per_sort: int = 3) -> CoinankResult:
    raw_5m = _fetch_one(top_n, "openInterestChM5")
    raw_15m = _fetch_one(top_n, "openInterestChM15")
    items_5m = [_to_item(d) for d in raw_5m]
    items_15m = [_to_item(d) for d in raw_15m]

    merged: dict[str, OIItem] = {}
    for it in items_5m + items_15m:
        if it.base_coin:
            _merge_field(merged, it)

    seen: set[str] = set()
    candidates: list[OIItem] = []
    for source in (items_5m, items_15m):
        picked = 0
        for it in source:
            if picked >= pick_per_sort:
                break
            if not it.base_coin or it.base_coin in seen:
                continue
            seen.add(it.base_coin)
            candidates.append(merged.get(it.base_coin, it))
            picked += 1

    return CoinankResult(items_5m=items_5m, items_15m=items_15m, candidates=candidates)


def top_preview(items: list[OIItem], n: int, use_5m: bool) -> str:
    parts: list[str] = []
    for it in items:
        if len(parts) >= n:
            break
        v = ((it.oi_chg5 if use_5m else it.oi_chg15) or 0) * 100
        parts.append(f"{it.base_coin} {v:+.2f}%")
    return " | ".join(parts)
