"""DynamoDB dedup：替代 Python 版的文件存储。

表 schema：
  - PK: base (String)              基础币种，例如 "AI" / "BTC"
  - last_notified_at (Number)      上次告警的 epoch ms
  - expire_at (Number)             TTL 字段（epoch sec），DynamoDB 自动清理过期项

环境变量：DEDUP_TABLE（SAM 模板自动注入）。
"""

from __future__ import annotations

import os
import time
from typing import Optional

import boto3

_DEFAULT_TTL_SEC = 86400  # 24h；远大于 DEDUP_WINDOW_SEC，确保查询时不会刚过期就清掉

_table_cache = None


def _get_table():
    global _table_cache
    if _table_cache is None:
        ddb = boto3.resource("dynamodb")
        table_name = os.environ.get("DEDUP_TABLE", "oi-monitor-dedup")
        _table_cache = ddb.Table(table_name)
    return _table_cache


def get_last_notified_at(base: str) -> Optional[float]:
    """返回上次告警的 epoch ms；从未告警则 None。"""
    try:
        resp = _get_table().get_item(Key={"base": base})
    except Exception as exc:  # noqa: BLE001
        print(f"[dedup] get_item({base}) 失败：{exc}")
        return None
    item = resp.get("Item")
    if not item:
        return None
    raw = item.get("last_notified_at")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def upsert(base: str, ttl_sec: int = _DEFAULT_TTL_SEC) -> None:
    """记录最近告警时间，TTL 过期自动清理。"""
    now_ms = int(time.time() * 1000)
    expire_at = int(time.time()) + ttl_sec
    try:
        _get_table().put_item(
            Item={
                "base": base,
                "last_notified_at": now_ms,
                "expire_at": expire_at,
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[dedup] put_item({base}) 失败：{exc}")
