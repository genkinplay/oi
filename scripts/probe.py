#!/usr/bin/env python3
"""探测项目用到的所有外部地址的可达性。

两种用法：
  1) 本地：make probe（或 uv run python scripts/probe.py）
     输出表格 + JSON，看本地出站对这些地址的可达性
  2) AWS Lambda：把这个文件 zip 上传，handler 入口 lambda_function.lambda_handler
     用来测 AWS Lambda 出站 IP 段对币安等服务是否被屏蔽

verdict 字段的含义：
  - OK                          status 200，完全可达
  - HTTP-XXX-but-reachable      TCP+TLS 通了，业务层 4xx/5xx（如缺 auth），代码逻辑能用
  - BLOCKED-451                 服务方主动拒绝，IP 段被屏蔽
  - UNREACHABLE                 网络层不通（timeout / DNS / connection refused）
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

TARGETS: list[tuple[str, str]] = [
    # === 数据源（必须通）===
    ("binance fapi exchangeInfo",
     "https://fapi.binance.com/fapi/v1/exchangeInfo"),
    ("binance fapi 24h ticker",
     "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT"),
    ("binance fapi klines",
     "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2"),
    ("binance fapi premiumIndex",
     "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    ("binance fapi openInterest",
     "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
    ("binance futures-data long-short",
     "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1"),
    ("coinank OI api",
     "https://api.coinank.com/api/instruments/agg?sortBy=openInterestChM5&sortType=descend&type=oi&page=1&size=5"),

    # === AI ===
    ("deepseek api root",
     "https://api.deepseek.com/"),

    # === 下架清单 ===
    ("github raw delisted_symbols",
     "https://raw.githubusercontent.com/genkinplay/oi/refs/heads/main/delisted_symbols.json"),

    # === 通知 ===
    ("feishu open-apis root",
     "https://open.feishu.cn/"),
    ("dingtalk robot host",
     "https://oapi.dingtalk.com/"),

    # === 已部署的 CF worker（如果存在）===
    ("cf worker simple proxy",
     "https://oi-binance-proxy.hemengzhi88.workers.dev/"),
    ("cf worker oi-monitor",
     "https://oi-monitor.hemengzhi88.workers.dev/"),
]


def probe(name: str, url: str, timeout: int = 8) -> dict:
    start = time.time()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms = int((time.time() - start) * 1000)
            return {
                "name": name,
                "status": r.status,
                "ms": ms,
                "verdict": "OK" if r.status == 200 else f"HTTP-{r.status}-but-reachable",
            }
    except urllib.error.HTTPError as e:
        ms = int((time.time() - start) * 1000)
        verdict = "BLOCKED-451" if e.code == 451 else f"HTTP-{e.code}-but-reachable"
        return {"name": name, "status": e.code, "ms": ms, "verdict": verdict}
    except Exception as e:  # noqa: BLE001
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ms": ms, "verdict": "UNREACHABLE", "error": repr(e)[:160]}


def run_all() -> list[dict]:
    return [probe(name, url) for name, url in TARGETS]


# AWS Lambda 入口
def lambda_handler(event, context):  # noqa: ARG001
    return run_all()


def _print_table(results: list[dict]) -> None:
    name_w = max(len(r["name"]) for r in results)
    print(f"{'TARGET':<{name_w}}  STATUS  TIME    VERDICT")
    print("-" * (name_w + 32))
    for r in results:
        status = str(r.get("status", "-"))
        ms = f"{r.get('ms', 0)}ms"
        verdict = r.get("verdict", "?")
        marker = ""
        if verdict == "OK":
            marker = "✅"
        elif verdict.startswith("HTTP-") and verdict.endswith("-but-reachable"):
            marker = "🟡"  # 业务层错误，但网络通
        elif verdict == "BLOCKED-451":
            marker = "❌"
        elif verdict == "UNREACHABLE":
            marker = "💀"
        line = f"{r['name']:<{name_w}}  {status:<6}  {ms:<6}  {marker} {verdict}"
        if "error" in r:
            line += f"\n{' ' * (name_w + 2)}  ↳ {r['error']}"
        print(line)


def main() -> int:
    results = run_all()
    _print_table(results)
    print()
    print("=== 完整 JSON ===")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    # 关键服务挂了时退出码非 0，便于 CI 判断
    critical = {"binance fapi exchangeInfo", "coinank OI api"}
    failed = [r for r in results if r["name"] in critical and r["verdict"] in ("BLOCKED-451", "UNREACHABLE")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
