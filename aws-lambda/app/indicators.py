"""轻量技术指标，全用纯 Python，避免引入 numpy / pandas。

输入是按时间正序的浮点数组（最新值在末尾）。
所有函数都返回最新一根 K 线对应的指标值；None 表示数据不足。
"""

from __future__ import annotations

from typing import Optional


def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema_series(values: list[float], n: int) -> list[float]:
    """完整 EMA 序列，长度与 values 相同；前 n-1 个用累计 SMA 兜底。"""
    if not values or n <= 0:
        return []
    k = 2 / (n + 1)
    out: list[float] = []
    cumsum = 0.0
    for i, v in enumerate(values):
        cumsum += v
        if i < n - 1:
            out.append(cumsum / (i + 1))
        elif i == n - 1:
            out.append(cumsum / n)
        else:
            out.append(out[-1] + k * (v - out[-1]))
    return out


def ema(values: list[float], n: int) -> Optional[float]:
    s = ema_series(values, n)
    return s[-1] if s else None


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[dict[str, float]]:
    """返回 dif / dea / hist；数据不足返回 None。"""
    if len(values) < slow + signal:
        return None
    fast_ema = ema_series(values, fast)
    slow_ema = ema_series(values, slow)
    dif = [f - s for f, s in zip(fast_ema, slow_ema)]
    dea_series = ema_series(dif, signal)
    return {
        "dif": dif[-1],
        "dea": dea_series[-1],
        "hist": (dif[-1] - dea_series[-1]) * 2,
    }


def true_range(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    """逐根计算 True Range；首根用 high-low 兜底。"""
    if not highs:
        return []
    out = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        out.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return out


def atr(
    highs: list[float], lows: list[float], closes: list[float], n: int = 14
) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    trs = true_range(highs, lows, closes)
    return ema(trs, n)
