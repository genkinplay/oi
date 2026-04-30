"""AWS Lambda 入口。EventBridge schedule 每 2 分钟触发一次 lambda_handler。"""

from __future__ import annotations

import json
import logging
import traceback

import oi_monitor

logging.getLogger().setLevel(logging.INFO)


def lambda_handler(event, context):  # noqa: ARG001
    try:
        result = oi_monitor.run_once()
        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}
    except Exception as exc:  # noqa: BLE001
        logging.error("oi monitor failed: %s\n%s", exc, traceback.format_exc())
        return {"statusCode": 500, "body": str(exc)}
