"""远端下架清单：从 raw.githubusercontent.com 上的 delisted_symbols.json 提取 base 集合。
任何失败返回空集，不阻塞主流程。
"""

from __future__ import annotations

import os

import requests

QUOTE_SUFFIXES = (
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


def _strip_quote_suffix(symbol: str) -> str | None:
    s = symbol.upper().strip()
    for q in QUOTE_SUFFIXES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return None


def fetch_delisted_bases(timeout: int = 10) -> set[str]:
    url = os.environ.get("DELISTING_LIST_URL", "").strip()
    if not url:
        return set()
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[delisting] 拉取失败：{exc}")
        return set()

    bases: set[str] = set()
    for sym in data.get("all_contracts") or []:
        if not isinstance(sym, str):
            continue
        b = _strip_quote_suffix(sym)
        if b:
            bases.add(b)
    return bases
