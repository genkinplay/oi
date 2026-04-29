"""调用 DeepSeek 分析交易机会。

返回结构化 JSON：
{
  "intervene": bool,
  "direction": "long" | "short" | null,
  "entry_price": float | null,
  "position": "light" | "heavy" | null,
  "stop_loss": float | null,
  "take_profit": float | null,
  "confidence": int,        # 0-100
  "reasoning": str
}
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import requests

DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
# 默认走 v4-pro（推理质量更高）；要追求响应速度可改成 deepseek-v4-flash
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEFAULT_TIMEOUT = 60

SYSTEM_PROMPT = """你是一名严谨的加密货币短线合约交易分析助手。
基于用户提供的行情快照（包含 OI 异动、多周期 K 线、技术指标、资金费率、多空比等），
判断当前是否有清晰可执行的短线机会。

判断纪律：
1. 信号不明确时，intervene=false；不要为了给建议而强行下结论。
2. direction 只能是 long 或 short；当 intervene=false 时为 null。
3. entry_price 优先用回踩 / 突破后回测的价位；不要追高也不要抄底无支撑位置。
4. position 取 light（轻仓）或 heavy（重仓）；信号强、上下文一致才用 heavy。
5. stop_loss / take_profit 必须基于 ATR 或关键支撑阻力，给出具体价格；
   止损不应超过 ATR*2，盈亏比 ≥ 1.5:1。
6. confidence 0-100，反映你对这次判断的把握；50 以下默认不介入。
7. reasoning 用中文，2-4 句，重点说决定性依据，不要堆砌数据。

输出严格 JSON，不要 markdown，不要解释包装。"""


def _build_user_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "请基于以下行情快照给出交易判断（严格 JSON 格式）：\n\n"
        f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n```"
    )


def _coerce_json(text: str) -> Optional[dict]:
    """模型有时会包一层 ```json ... ``` 或裸文本前后多空白。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 退化：找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def analyze(snapshot: dict[str, Any]) -> Optional[dict]:
    """调 DeepSeek 拿分析结果。失败返回 None。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("[ai] DEEPSEEK_API_KEY 未配置，跳过 AI 分析")
        return None

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(snapshot)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            DEEPSEEK_API, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[ai] DeepSeek 调用失败：{str(exc)[:160]}")
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print(f"[ai] DeepSeek 响应格式异常：{exc}; raw={str(data)[:200]}")
        return None

    parsed = _coerce_json(content)
    if not parsed:
        print(f"[ai] 解析 JSON 失败：{content[:200]}")
        return None
    return parsed
