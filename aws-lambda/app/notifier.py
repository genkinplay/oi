"""统一 webhook 通知层。

走两套消息格式：
- 飞书 / 钉钉：markdown 富文本（飞书 interactive 卡片 + 钉钉 msgtype=markdown）
- 通用 webhook（旧版）：纯文本 {title, content, task}，行为不变

环境变量配置（多个 URL 用英文逗号分隔）：
  FEISHU_WEBHOOK_URLS    飞书自定义机器人 webhook 列表
  DINGTALK_WEBHOOK_URLS  钉钉自定义机器人 webhook 列表
  WEBHOOK_URL            旧版通用 webhook（向后兼容）

调用方建议同时传：
  content   —— 纯文本，给通用 webhook 使用
  markdown  —— markdown 内容（不含标题行，标题由 send_* 自行套上）
若 markdown 留空，飞书 / 钉钉会回退到把 content 当 markdown 用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

import requests

DEFAULT_TIMEOUT = 10

# 飞书 markdown 不渲染 <font color>，发送前剥离掉，保留内层文本
_FONT_TAG_RE = re.compile(
    r"""<font\s+color=['"]?[^'">]+['"]?\s*>(.*?)</font>""",
    re.IGNORECASE | re.DOTALL,
)


def _strip_color_tags(md: str) -> str:
    return _FONT_TAG_RE.sub(r"\1", md)


def _split_urls(value: str | None) -> list[str]:
    if not value:
        return []
    return [u.strip() for u in value.split(",") if u.strip()]


def _mask(url: str) -> str:
    if len(url) <= 30:
        return url
    return f"{url[:30]}...{url[-6:]}"


def send_feishu(
    url: str,
    title: str,
    markdown: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """飞书自定义机器人 - interactive 卡片，body 为 markdown。
    title 走卡片 header（plain_text），不重复进 body。
    飞书 markdown 不渲染 <font color>，发送前剥离避免标签裸露。"""
    body = _strip_color_tags(markdown)
    card: dict = {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": body}],
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": "red",
        }
    payload = {"msg_type": "interactive", "card": card}
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    code = data.get("code") if isinstance(data, dict) else None
    if code not in (0, None):
        raise RuntimeError(f"飞书返回失败：{data}")


def send_dingtalk(
    url: str,
    title: str,
    markdown: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """钉钉自定义机器人 - msgtype=markdown。
    title 用作通知摘要 + 正文一级标题。"""
    text = f"## {title}\n\n{markdown}" if title else markdown
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title or "通知", "text": text},
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    err = data.get("errcode") if isinstance(data, dict) else None
    if err not in (0, None):
        raise RuntimeError(f"钉钉返回失败：{data}")


def send_generic(
    url: str,
    title: str,
    content: str,
    task: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """旧版通用 webhook：POST {title, content, task}，纯文本。"""
    payload: dict[str, str] = {"title": title, "content": content}
    if task:
        payload["task"] = task
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()


@dataclass(frozen=True)
class NotifyResult:
    sent: int
    failed: int

    @property
    def total(self) -> int:
        return self.sent + self.failed


def dispatch(
    title: str,
    content: str,
    markdown: str | None = None,
    task: str | None = None,
    feishu_urls: Iterable[str] | None = None,
    dingtalk_urls: Iterable[str] | None = None,
    generic_urls: Iterable[str] | None = None,
) -> NotifyResult:
    """聚合发送。
    - 飞书 / 钉钉 使用 markdown（缺省时回落到 content）
    - 通用 webhook 始终用 content
    URL 来源优先级：参数 > 环境变量。
    """
    feishu = (
        list(feishu_urls)
        if feishu_urls is not None
        else _split_urls(os.environ.get("FEISHU_WEBHOOK_URLS"))
    )
    dingtalk = (
        list(dingtalk_urls)
        if dingtalk_urls is not None
        else _split_urls(os.environ.get("DINGTALK_WEBHOOK_URLS"))
    )
    generic = (
        list(generic_urls)
        if generic_urls is not None
        else _split_urls(os.environ.get("WEBHOOK_URL"))
    )

    md = markdown if markdown is not None else content

    sent = 0
    failed = 0

    for url in feishu:
        try:
            send_feishu(url, title, md)
            sent += 1
            print(f"[notifier] 飞书已送达 {_mask(url)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[notifier] 飞书发送失败 {_mask(url)}: {exc}")

    for url in dingtalk:
        try:
            send_dingtalk(url, title, md)
            sent += 1
            print(f"[notifier] 钉钉已送达 {_mask(url)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[notifier] 钉钉发送失败 {_mask(url)}: {exc}")

    for url in generic:
        try:
            send_generic(url, title, content, task)
            sent += 1
            print(f"[notifier] 通用 webhook 已送达 {_mask(url)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[notifier] 通用 webhook 失败 {_mask(url)}: {exc}")

    return NotifyResult(sent=sent, failed=failed)
