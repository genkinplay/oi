"""币安 USDT 永续合约行情数据采集。

只暴露简单的函数式接口，每个调用都是 stateless 的。
不抛网络异常 —— 失败统一返回 None / 空结构，调用方自行决定降级。
"""

from __future__ import annotations

from typing import Optional

import requests

FAPI_BASE = "https://fapi.binance.com"
DEFAULT_TIMEOUT = 10

# 模块级缓存：exchangeInfo 数据量大（>600 个合约），单次运行内复用
_LIVE_SYMBOLS: set[str] | None = None


def _get(
    path: str, params: dict | None = None, timeout: int = DEFAULT_TIMEOUT
) -> dict | list | None:
    try:
        resp = requests.get(f"{FAPI_BASE}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[binance] {path} 请求失败：{str(exc)[:120]}")
        return None


def load_live_symbols() -> set[str]:
    """拉一次 exchangeInfo，缓存所有 TRADING 状态的合约名。"""
    global _LIVE_SYMBOLS
    if _LIVE_SYMBOLS is not None:
        return _LIVE_SYMBOLS
    data = _get("/fapi/v1/exchangeInfo")
    symbols: set[str] = set()
    if isinstance(data, dict):
        for s in data.get("symbols", []) or []:
            if s.get("status") == "TRADING":
                symbols.add(s["symbol"])
    _LIVE_SYMBOLS = symbols
    return symbols


def is_perpetual_listed(symbol: str) -> bool:
    """该 symbol 是否在币安 USDT-M 永续合约里活跃。"""
    return symbol.upper() in load_live_symbols()


def fetch_24h(symbol: str) -> Optional[dict]:
    """24 小时行情：lastPrice、priceChangePercent、volume、quoteVolume 等。"""
    data = _get("/fapi/v1/ticker/24hr", {"symbol": symbol})
    return data if isinstance(data, dict) else None


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 100) -> list[list]:
    """K 线，返回 raw list；每根：[openTime,o,h,l,c,v,closeTime,quoteV,trades,...]"""
    data = _get(
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    return data if isinstance(data, list) else []


def fetch_premium_index(symbol: str) -> Optional[dict]:
    """标记价 + 资金费率（lastFundingRate / nextFundingTime / markPrice）。"""
    data = _get("/fapi/v1/premiumIndex", {"symbol": symbol})
    return data if isinstance(data, dict) else None


def fetch_open_interest(symbol: str) -> Optional[dict]:
    """当前未平仓合约（持仓量）。"""
    data = _get("/fapi/v1/openInterest", {"symbol": symbol})
    return data if isinstance(data, dict) else None


def fetch_long_short_ratio(symbol: str, period: str = "5m") -> Optional[dict]:
    """全市场账户多空比，取最新一条。"""
    data = _get(
        "/futures/data/globalLongShortAccountRatio",
        {"symbol": symbol, "period": period, "limit": 1},
    )
    if isinstance(data, list) and data:
        return data[-1]
    return None


def fetch_top_trader_long_short_ratio(
    symbol: str, period: str = "5m"
) -> Optional[dict]:
    """大户持仓多空比，取最新一条。"""
    data = _get(
        "/futures/data/topLongShortPositionRatio",
        {"symbol": symbol, "period": period, "limit": 1},
    )
    if isinstance(data, list) and data:
        return data[-1]
    return None
