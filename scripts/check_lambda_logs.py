"""检查 oi-monitor Lambda 最近一段时间的运行日志，找出该推没推、发送失败、运行异常。

用法：
    python scripts/check_lambda_logs.py            # 默认最近 60 分钟
    python scripts/check_lambda_logs.py 120        # 最近 120 分钟

依赖：本机已配置 AWS CLI 且会话有效（aws sts get-caller-identity 能通）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
LOG_GROUP = "/aws/lambda/oi-monitor"
REGION = "ap-southeast-1"


def fetch_events(minutes: int) -> list[dict]:
    start_ms = int((datetime.now(timezone.utc).timestamp() - minutes * 60) * 1000)
    cmd = [
        "aws", "logs", "filter-log-events",
        "--log-group-name", LOG_GROUP,
        "--region", REGION,
        "--start-time", str(start_ms),
        "--output", "json",
    ]
    raw = subprocess.check_output(cmd, text=True)
    return sorted(json.loads(raw).get("events", []), key=lambda e: e["timestamp"])


def group_by_invocation(events: list[dict]) -> list[dict]:
    """按 START / END RequestId 把日志切成每次调用一组。"""
    calls: list[dict] = []
    cur: dict | None = None
    for e in events:
        msg = e["message"].rstrip()
        ts = e["timestamp"]
        if msg.startswith("START RequestId:"):
            cur = {"start_ms": ts, "lines": []}
            calls.append(cur)
        elif msg.startswith("END RequestId:"):
            cur = None
        elif msg.startswith("REPORT "):
            continue
        elif cur is not None:
            cur["lines"].append(msg)
    return calls


def _first(lines: list[str], needle: str) -> str | None:
    return next((m for m in lines if needle in m), None)


def _strip_prefix(line: str | None) -> str:
    if not line:
        return "-"
    return line.split("] ", 1)[1] if "] " in line else line


def _int_after(line: str | None, pattern: str) -> int:
    if not line:
        return 0
    m = re.search(pattern, line)
    return int(m.group(1)) if m else 0


def report(calls: list[dict], minutes: int) -> int:
    print(f"\n最近 {minutes} 分钟：{len(calls)} 次 Lambda 调用（每 2 分钟一次预期 ~{minutes // 2}）\n")

    totals = {"alerts": 0, "sent": 0, "failed": 0, "errors": 0, "send_fail": 0}
    for c in calls:
        lines = c["lines"]
        sig = _first(lines, "[oi_monitor] signals:")
        notify = _first(lines, "[oi_monitor] notify")
        summary = _first(lines, "[summary]")
        send_fails = [m for m in lines if "发送失败" in m or "webhook 失败" in m]
        errors = [m for m in lines if "[ERROR]" in m or "Traceback" in m or "Task timed out" in m]
        skips = [m for m in lines if "[oi_monitor] skip" in m]

        flag = ""
        if errors:
            flag += " ‼ERR"
        if send_fails:
            flag += " ‼SEND-FAIL"

        t = datetime.fromtimestamp(c["start_ms"] / 1000, CST).strftime("%m-%d %H:%M:%S")
        head = _strip_prefix(sig or summary)
        tail = _strip_prefix(notify)
        print(f"  {t}  {head:38s}  {tail:50s}{flag}")
        for s in skips:
            print(f"             ↳ {s.split('] ', 1)[1]}")
        for e in errors[:2]:
            print(f"             ↳ {e[:160]}")
        for f in send_fails:
            print(f"             ↳ {f}")

        totals["alerts"]    += _int_after(sig,    r"alerts: (\d+)")
        totals["sent"]      += _int_after(notify, r"sent=(\d+)")
        totals["failed"]    += _int_after(notify, r"failed=(\d+)")
        totals["errors"]    += len(errors)
        totals["send_fail"] += len(send_fails)

    print(
        f"\n汇总：alerts={totals['alerts']}  notify_sent={totals['sent']}  "
        f"notify_failed={totals['failed']}  errors={totals['errors']}  "
        f"send_fail={totals['send_fail']}"
    )
    if totals["errors"] == 0 and totals["send_fail"] == 0 and totals["failed"] == 0:
        print("✅ 无异常\n")
        return 0
    print("⚠️  存在需要人工确认的异常项\n")
    return 1


def main() -> int:
    minutes = 60
    if len(sys.argv) > 1 and sys.argv[1].strip():
        try:
            minutes = int(sys.argv[1])
        except ValueError:
            print(f"参数必须是整数（分钟数），收到：{sys.argv[1]!r}", file=sys.stderr)
            return 2
    return report(group_by_invocation(fetch_events(minutes)), minutes)


if __name__ == "__main__":
    sys.exit(main())
