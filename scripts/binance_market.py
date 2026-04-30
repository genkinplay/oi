"""币安 USDT 永续合约行情数据采集。

只暴露简单的函数式接口，每个调用都是 stateless 的。
不抛网络异常 —— 失败统一返回 None / 空结构，调用方自行决定降级。
"""

from __future__ import annotations

from typing import Optional

import requests

FAPI_BASE = "https://fapi.binance.com"
DEFAULT_TIMEOUT = 10

import json
import pathlib

# 模块级缓存：exchangeInfo 数据量大（>600 个合约），单次运行内复用
_LIVE_SYMBOLS: set[str] | None = None
# 拉 exchangeInfo 失败（如 451 地域屏蔽）后置 True；后续 is_perpetual_listed 改为放行
_EXCHANGE_INFO_UNAVAILABLE: bool = False

# 本地清单快照路径：GitHub Actions runner 被币安 451 拒绝时使用
_SYMBOLS_FILE = pathlib.Path(__file__).resolve().parent / "binance_symbols.json"


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


def _load_symbols_from_local() -> set[str] | None:
    if not _SYMBOLS_FILE.exists():
        return None
    try:
        payload = json.loads(_SYMBOLS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[binance] 本地清单解析失败：{exc}")
        return None
    syms = payload.get("symbols") or []
    if not syms:
        return None
    print(
        f"[binance] 使用本地清单（{len(syms)} 合约 / "
        f"updated_at={payload.get('updated_at', 'unknown')}）"
    )
    return {s for s in syms if isinstance(s, str)}


def load_live_symbols() -> set[str]:
    """加载币安 USDT-M 永续合约清单。
    优先实时拉 fapi/exchangeInfo（永远拿最新）；
    GitHub Actions 上 451 屏蔽时，回落到本地 binance_symbols.json。"""
    global _LIVE_SYMBOLS, _EXCHANGE_INFO_UNAVAILABLE
    if _LIVE_SYMBOLS is not None:
        return _LIVE_SYMBOLS

    # 1) 实时拉
    data = _get("/fapi/v1/exchangeInfo")
    if isinstance(data, dict) and data.get("symbols"):
        symbols = {
            s["symbol"]
            for s in data["symbols"]
            if s.get("status") == "TRADING"
        }
        if symbols:
            print(f"[binance] 使用 fapi 实时清单（{len(symbols)} 合约）")
            _LIVE_SYMBOLS = symbols
            return symbols

    # 2) 本地兜底
    local = _load_symbols_from_local()
    if local:
        _LIVE_SYMBOLS = local
        return local

    # 3) 都拿不到 → 放行模式，避免大面积误过滤
    _EXCHANGE_INFO_UNAVAILABLE = True
    print(
        "[binance] fapi 与本地清单均不可用，is_perpetual_listed 进入放行模式"
    )
    return set()


def is_perpetual_listed(symbol: str) -> bool:
    """该 symbol 是否在币安 USDT-M 永续合约里活跃。
    清单不可用时保守放行（返回 True），避免大量误过滤。"""
    syms = load_live_symbols()
    if _EXCHANGE_INFO_UNAVAILABLE:
        return True
    return symbol.upper() in syms


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
