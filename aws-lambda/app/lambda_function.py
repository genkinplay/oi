"""AWS Lambda 入口。
按 event.action 分发：
  - "summary" → 24h 信号复盘（每天 CST 20:00 由 EventBridge 触发）
  - 其它（含空 event）→ OI 异动主流程（每 2 分钟 EventBridge 触发）

手动触发：
  aws lambda invoke --function-name oi-monitor --payload '{"action":"summary"}' /tmp/o.json
"""

from __future__ import annotations

import json
import logging
import traceback


logging.getLogger().setLevel(logging.INFO)


def lambda_handler(event, context):  # noqa: ARG001
    action = ""
    if isinstance(event, dict):
        action = str(event.get("action") or "").lower()

    try:
        if action == "summary":
            import summary

            result = summary.run_summary()
        else:
            import oi_monitor

            result = oi_monitor.run_once()
        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}
    except Exception as exc:  # noqa: BLE001
        logging.error("lambda failed: %s\n%s", exc, traceback.format_exc())
        return {"statusCode": 500, "body": str(exc)}
