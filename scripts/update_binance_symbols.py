#!/usr/bin/env python3
"""刷新本地币安 USDT-M 永续合约清单快照。

为什么需要：GitHub Actions runner 的 IP 段被币安 451 屏蔽，
fapi 在云端拉不到 exchangeInfo。所以 binance_market.load_live_symbols
优先读本地 scripts/binance_symbols.json。

何时跑：
  - 本地 IP 没被屏蔽（普通家宽 / VPN）
  - 币安上线/下架合约后（每周 1-2 次足够）

执行：
  python scripts/update_binance_symbols.py
然后 git commit & push。
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

import requests

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
OUTPUT_FILE = pathlib.Path(__file__).resolve().parent / "binance_symbols.json"
NOTE = (
    "币安 USDT-M 永续合约清单快照。GitHub Actions runner 因 451 拉不到 "
    "exchangeInfo，故离线缓存。本地手动更新："
    "python scripts/update_binance_symbols.py"
)


def main() -> int:
    try:
        resp = requests.get(EXCHANGE_INFO_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 拉 fapi/exchangeInfo 失败：{exc}", file=sys.stderr)
        return 1

    symbols = sorted(
        s["symbol"]
        for s in data.get("symbols", [])
        if isinstance(s, dict) and s.get("status") == "TRADING"
    )
    if not symbols:
        print("❌ 拿到的 symbols 为空，拒绝写入", file=sys.stderr)
        return 1

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(symbols),
        "note": NOTE,
        "symbols": symbols,
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"✅ 已更新 {OUTPUT_FILE.name}：{len(symbols)} 合约")
    return 0


if __name__ == "__main__":
    sys.exit(main())
