"""DynamoDB 告警历史表：记录每次成功推送的 alert 元信息，供 24h 复盘统计使用。

表 schema：
  PK: base (S)               基础币种，如 "BTC"
  SK: notified_at_ms (N)     告警发出的 epoch ms
  pair (S)                   "BTCUSDT"
  direction (S)              "long" | "short"
  entry_price (S)            介入价（用 string 存避免 Decimal 来回转）
  confidence (N)             AI 置信度
  reasoning (S)              AI 给的简短理由（截断到 200 字）
  expire_at (N)              TTL（epoch sec），默认 48h 后自动清理

环境变量：ALERTS_TABLE（SAM 模板自动注入）。
"""

from __future__ import annotations

import os
import time

import boto3
from boto3.dynamodb.conditions import Attr

ALERTS_TABLE = os.environ.get("ALERTS_TABLE", "oi-monitor-alerts")
_DEFAULT_TTL_SEC = 48 * 3600  # 48h，确保 24h 复盘窗口能完整覆盖

_table_cache = None


def _get_table():
    global _table_cache
    if _table_cache is None:
        ddb = boto3.resource("dynamodb")
        _table_cache = ddb.Table(ALERTS_TABLE)
    return _table_cache


def record(
    base: str,
    pair: str,
    direction: str,
    entry_price: float,
    confidence: int = 0,
    reasoning: str = "",
) -> None:
    """记录一次 alert。同一 base 多次告警按时间戳区分。"""
    if not base or not direction or entry_price <= 0:
        return
    now_ms = int(time.time() * 1000)
    item = {
        "base": base,
        "notified_at_ms": now_ms,
        "pair": pair,
        "direction": direction,
        "entry_price": str(entry_price),  # str 避免 Decimal 精度问题
        "confidence": int(confidence or 0),
        "reasoning": (reasoning or "")[:200],
        "expire_at": int(time.time()) + _DEFAULT_TTL_SEC,
    }
    try:
        _get_table().put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        print(f"[alerts] put_item({base}) 失败：{exc}")


def list_recent(hours: int = 24) -> list[dict]:
    """扫描最近 N 小时的告警；TTL 限制下 24h 数据量很小（< 几百条），scan 即可。"""
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    items: list[dict] = []
    scan_kwargs: dict = {
        "FilterExpression": Attr("notified_at_ms").gte(cutoff_ms),
    }
    try:
        while True:
            resp = _get_table().scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        print(f"[alerts] scan 失败：{exc}")
        return []
    return items
