"""调用 DeepSeek 分析交易机会。

返回结构化 JSON：
{
  "intervene": bool,
  "direction": "long" | "short" | null,
  "entry_price": float | null,
  "stop_loss": float | null,
  "confidence": int,        # 0-100
  "reasoning": str
}

仓位策略：所有介入一律按轻仓处理（脚本侧不再询问 / 显示 position）。
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

SYSTEM_PROMPT = """你是一名敏锐的加密货币短线合约交易助手。
你的任务是从行情快照里**主动捕捉短线机会**——OI 异动是关键 alpha 信号，
只要方向感清晰、有至少一个技术面同向就足够介入，不必等多重指标完美共振。

输出字段（严格 JSON，不要 markdown 包装）：
- intervene: 是否介入；介入一律按轻仓执行
- direction: 介入方向 long / short
- entry_price: 介入价
- stop_loss: 止损价
- confidence: 0-100 的置信度
- reasoning: 中文 2-4 句决定性理由

判断纪律：
1. **核心**：OI 异动本身就是 alpha。配合下列任一同向信号即可介入——
   K 线趋势 / MA 排列 / MACD 方向 / 多空比偏向 / 资金费率倾向。**不要求三重共振**。
2. direction 综合判断：
   - OI 暴增 + 价格上涨 → long（多头加仓推涨）
   - OI 暴增 + 价格下跌 → short（空头加仓砸盘）
   - OI 暴减 + 价格反向 → 平仓行情，谨慎判断
3. entry_price 可选：当前价小幅追入 / 最近回踩位 / 突破回测位。**不必死等深度回调**。
4. stop_loss 必须基于 ATR 或最近 swing low/high，距离 entry_price 不超过 ATR*2.5
   ——这是**风控底线，绝不放松**。
5. confidence ≥ 40 就给介入；只有当信号**严重相反**（如 OI 涨 + MACD 死叉 + 多空比极度反向 +
   资金费率极端）或**关键数据缺失**（K 线为空）才判 intervene=false。
6. reasoning 直说"为什么介入"或"为什么观望"，简短利落，不堆砌数据。

记住：宁可错过几次小机会，也不要因过度严谨错过明显的方向性行情。
不必怀疑 OI 异动的有效性——这是已经过滤后的强信号。"""


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
