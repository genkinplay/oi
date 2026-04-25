"""采集币安下架公告中的合约标的。

覆盖两类标的：
1. 合约下架公告 → 直接提取合约符号
2. 现货下架公告 → 基础币种匹配当前存量合约，拿到关联合约
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Iterable

import requests

LIST_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
USDT_M_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
COIN_M_EXCHANGE_INFO = "https://dapi.binance.com/dapi/v1/exchangeInfo"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "clienttype": "web",
    "lang": "en",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/en/support/announcement",
}

DELISTING_CATALOG_ID = 161
REQUEST_INTERVAL_SEC = 1.2  # 详情接口对 IP 限流较严
MAX_PAGES = 1
PAGE_SIZE = 5
MAX_ARTICLES = 5  # 只处理最新 N 条
MAX_DETAIL_RETRIES = 5
BACKOFF_BASE_SEC = 4.0

# 计价币种，越长放越前，避免 "FDUSD" 被 "USD" 先吃掉
QUOTE_ASSETS: tuple[str, ...] = (
    "FDUSD", "BUSD", "TUSD", "USDC", "USDT",
    "BTC", "ETH", "BNB", "TRY", "EUR", "GBP", "BRL", "JPY", "AUD", "USD",
)

SPOT_KEYWORDS: tuple[str, ...] = ("现货", "交易对", "Spot", "SPOT", "spot trading")
FUTURES_KEYWORDS: tuple[str, ...] = (
    "永续", "U 本位", "U本位", "币本位", "币 本位", "交割合约", "合约",
    "Futures", "Perpetual", "USDⓈ-M", "USDS-M", "USD-M", "COIN-M", "Coin-M",
    "Delivery Contract",
)
# 保证金（杠杆）交易相关公告，不属于现货也不属于合约，跳过
MARGIN_KEYWORDS: tuple[str, ...] = (
    "Margin", "margin", "杠杆", "保证金",
)
# "Binance Will Delist X, Y, Z on YYYY-MM-DD" 等代币级下架标题（标题里没显式说 spot/futures）
TOKEN_DELIST_TITLE_RE = re.compile(
    r"(?i)\bbinance\s+(?:will\s+)?delist\s+[A-Z0-9,\s]+(?:on|as of|\.|$)"
)

# 基础币种白名单字符集：字母+数字，长度 2~15
_BASE_TOKEN = r"[A-Z0-9]{2,15}"
_QUOTE_ALT = "|".join(QUOTE_ASSETS)
# 使用 lookaround 替代 \b，规避 Python str regex 把中文当词字符的问题
_LEFT_BOUND = r"(?<![A-Z0-9])"
_RIGHT_BOUND = r"(?![A-Z0-9])"

PAIR_SPLIT_RE = re.compile(
    _LEFT_BOUND + r"(" + _BASE_TOKEN + r")[/\-_](" + _QUOTE_ALT + r")" + _RIGHT_BOUND
)
PAIR_JOIN_RE = re.compile(
    _LEFT_BOUND + r"(" + _BASE_TOKEN + r")(" + _QUOTE_ALT + r")" + _RIGHT_BOUND
)


@dataclass(frozen=True)
class Article:
    code: str
    title: str
    release_date: int | None


@dataclass
class ParsedArticle:
    title: str
    category: str  # "futures" | "spot" | "other"
    direct_contracts: set[str] = field(default_factory=set)
    base_tokens: set[str] = field(default_factory=set)
    linked_contracts: set[str] = field(default_factory=set)


_session = requests.Session()
_session.headers.update(HEADERS)


def _http_get(
    url: str,
    params: dict | None = None,
    timeout: int = 15,
    extra_headers: dict | None = None,
) -> dict:
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    resp = _session.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _http_get_with_retry(
    url: str,
    params: dict | None = None,
    timeout: int = 15,
    extra_headers: dict | None = None,
    max_retries: int = MAX_DETAIL_RETRIES,
) -> dict | None:
    for attempt in range(1, max_retries + 1):
        try:
            return _http_get(url, params=params, timeout=timeout, extra_headers=extra_headers)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429 or (status and 500 <= status < 600):
                wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                print(f"    ↻ HTTP {status}，{wait:.1f}s 后重试 ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise
        except requests.RequestException as exc:
            wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            print(f"    ↻ 网络异常 {type(exc).__name__}，{wait:.1f}s 后重试 ({attempt}/{max_retries})")
            time.sleep(wait)
    return None


def fetch_delisting_articles(
    max_pages: int = MAX_PAGES,
    page_size: int = PAGE_SIZE,
) -> list[Article]:
    articles: list[Article] = []
    for page in range(1, max_pages + 1):
        params = {
            "type": 1,
            "catalogId": DELISTING_CATALOG_ID,
            "pageNo": page,
            "pageSize": page_size,
        }
        try:
            data = _http_get(LIST_URL, params=params, timeout=10)
        except Exception as exc:
            print(f"⚠️  列表第 {page} 页请求失败: {str(exc)[:120]}")
            break

        # 返回结构: data.catalogs[0].articles
        catalogs = data.get("data", {}).get("catalogs", []) or []
        page_articles: list[dict] = []
        for cat in catalogs:
            page_articles.extend(cat.get("articles", []) or [])
        # 兼容旧结构
        if not page_articles:
            page_articles = data.get("data", {}).get("articles", []) or []
        if not page_articles:
            break

        for art in page_articles:
            code = art.get("code")
            if not code:
                continue
            articles.append(
                Article(
                    code=code,
                    title=art.get("title", "") or "",
                    release_date=art.get("releaseDate"),
                )
            )
        time.sleep(REQUEST_INTERVAL_SEC)
    return articles


def _walk_body_text(node: Any, buf: list[str]) -> None:
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text:
            buf.append(text)
        for child in node.get("child", []) or []:
            _walk_body_text(child, buf)
    elif isinstance(node, list):
        for child in node:
            _walk_body_text(child, buf)


@dataclass
class ArticleDetail:
    title: str
    body_text: str
    pairs: list[str]


def fetch_article_detail(code: str) -> ArticleDetail | None:
    extra = {
        "Referer": f"https://www.binance.com/en/support/announcement/detail/{code}",
    }
    data = _http_get_with_retry(
        DETAIL_URL, params={"articleCode": code}, timeout=15, extra_headers=extra
    )
    if not data:
        print(f"⚠️  公告 {code} 详情失败（重试耗尽）")
        return None

    payload = data.get("data") or {}
    title = payload.get("title", "") or ""
    pairs = [p for p in (payload.get("pairs") or []) if isinstance(p, str)]

    body_raw = payload.get("body", "") or ""
    text_parts: list[str] = []
    if body_raw:
        try:
            body_tree = json.loads(body_raw)
            _walk_body_text(body_tree, text_parts)
        except (json.JSONDecodeError, TypeError):
            # 退化：按 HTML 清洗
            stripped = re.sub(r"<[^>]+>", " ", unescape(body_raw))
            text_parts.append(stripped)

    body_text = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
    return ArticleDetail(title=title, body_text=body_text, pairs=pairs)


def classify(title: str, content: str) -> str:
    """分类
    - futures:      合约下架（直接落为合约标的）
    - token_delist: 币种级下架（该代币会从现货+合约整体摘牌，反查关联合约）
    - spot_pair:    单交易对下架（仅去掉 X/Y 这个特定报价对，不影响币种本身 → 不反查合约）
    - other:        保证金、Earn、无关公告
    """
    head = f"{title} {content[:600]}"

    # 保证金／杠杆：无合约／现货关键词时跳过
    if any(k in title for k in MARGIN_KEYWORDS) and not any(
        k in title for k in FUTURES_KEYWORDS
    ) and "Spot Trading" not in title:
        return "other"

    if any(k in title for k in FUTURES_KEYWORDS):
        return "futures"

    # "Binance Will Delist FOO, BAR on ..." —— 币种整体下架
    if TOKEN_DELIST_TITLE_RE.search(title):
        return "token_delist"

    if any(k in title for k in SPOT_KEYWORDS):
        return "spot_pair"

    # 标题没透露，退回到正文前部
    if any(k in head for k in FUTURES_KEYWORDS):
        return "futures"
    if any(k in head for k in SPOT_KEYWORDS):
        return "spot_pair"
    return "other"


def _extract_pairs(text: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for m in PAIR_SPLIT_RE.finditer(text):
        base, quote = m.group(1), m.group(2)
        if base not in QUOTE_ASSETS:
            pairs.add((base, quote))
    for m in PAIR_JOIN_RE.finditer(text):
        base, quote = m.group(1), m.group(2)
        if base in QUOTE_ASSETS:
            continue
        # 避免贪吃：BTCUSDT 先试长 quote，如果匹配到短 quote 但去掉短 quote 后尾巴还能被长 quote 消化，就忽略
        if any(base.endswith(q) and (len(q) > len(quote)) for q in QUOTE_ASSETS):
            continue
        pairs.add((base, quote))
    return pairs


def extract_contract_symbols(text: str) -> set[str]:
    """从合约公告中抽合约符号，统一成无分隔的拼写 (BTCUSDT / BTCUSD_PERP 省略后缀)。"""
    return {base + quote for base, quote in _extract_pairs(text)}


def extract_base_tokens(text: str) -> set[str]:
    return {base for base, _ in _extract_pairs(text)}


# 匹配 "Delist FOO, BAR and BAZ" 里的纯代币列表。只用于标题。
_DELIST_TOKEN_LIST_RE = re.compile(
    r"(?i)\bdelist(?:ing)?\s+((?:[A-Z0-9]{2,15}(?:\s*,\s*|\s+and\s+|\s+&\s+))+[A-Z0-9]{2,15})"
)
_SEP_RE = re.compile(r"\s*(?:,|and|&)\s*", re.IGNORECASE)

# 不是币种的常见噪音词（以 ASCII 大写/数字形式出现，容易误匹配）
_TOKEN_STOPWORDS: frozenset[str] = frozenset({
    "BINANCE", "FUTURES", "PERPETUAL", "SPOT", "MARGIN", "TRADING",
    "THE", "AND", "WILL", "DELIST", "USD", "USDⓈ", "USDS",
    "CEASE", "REMOVE", "PAIR", "PAIRS", "BOT", "BOTS",
})


def extract_tokens_from_delist_list(text: str) -> set[str]:
    tokens: set[str] = set()
    for m in _DELIST_TOKEN_LIST_RE.finditer(text):
        raw = m.group(1)
        for part in _SEP_RE.split(raw):
            part = part.strip().upper()
            if not part or not part.isalnum():
                continue
            if not (2 <= len(part) <= 15):
                continue
            if part in QUOTE_ASSETS or part in _TOKEN_STOPWORDS:
                continue
            tokens.add(part)
    return tokens


def fetch_live_futures_symbols() -> set[str]:
    live: set[str] = set()
    for url, label in (
        (USDT_M_EXCHANGE_INFO, "U 本位"),
        (COIN_M_EXCHANGE_INFO, "币本位"),
    ):
        try:
            data = _http_get(url, timeout=15)
        except Exception as exc:
            print(f"⚠️  获取 {label} 合约清单失败: {str(exc)[:120]}")
            continue
        for s in data.get("symbols", []) or []:
            status = s.get("status") or s.get("contractStatus")
            if status and status != "TRADING":
                continue
            symbol = s.get("symbol")
            if symbol:
                live.add(symbol)
    return live


def link_spot_to_contracts(
    base_tokens: Iterable[str],
    known_futures: set[str],
) -> set[str]:
    """基础币种 → 合约映射。
    known_futures 是"已知曾经或当前存在"的合约全集。
    同时无条件生成 {token}USDT 候选（最常见的 U 本位永续），
    即便 known 集合里没有也保留——币种级下架必然涉及其 USDT 永续（若曾存在）。
    """
    linked: set[str] = set()
    for token in base_tokens:
        # 兜底：币种级下架最核心的合约就是 USDT 永续
        linked.add(f"{token}USDT")
        for quote in ("USDC", "BUSD"):
            candidate = f"{token}{quote}"
            if candidate in known_futures:
                linked.add(candidate)
        for sym in known_futures:
            if sym.startswith(f"{token}USD_") or sym == f"{token}USD":
                linked.add(sym)
    return linked


@dataclass
class RawArticle:
    article: Article
    detail: ArticleDetail
    category: str
    direct_contracts: set[str] = field(default_factory=set)


def fetch_and_classify(article: Article) -> RawArticle | None:
    detail = fetch_article_detail(article.code)
    time.sleep(REQUEST_INTERVAL_SEC)
    if not detail:
        return None
    category = classify(article.title, detail.body_text)
    return RawArticle(article=article, detail=detail, category=category)


def parse_futures(raw: RawArticle) -> ParsedArticle:
    full_text = f"{raw.article.title}\n{raw.detail.body_text}\n{' '.join(raw.detail.pairs)}"
    api_pairs = {
        p.upper().replace("/", "").replace("-", "")
        for p in raw.detail.pairs
        if p
    }
    return ParsedArticle(
        title=raw.article.title,
        category="futures",
        direct_contracts=api_pairs | extract_contract_symbols(full_text),
    )


def parse_token_delist(
    raw: RawArticle,
    known_futures: set[str],
) -> ParsedArticle:
    # 只从标题提取，避免 body 里 "Binance" / "Futures" 等噪音词被误当 token
    base_tokens = extract_tokens_from_delist_list(raw.article.title)
    return ParsedArticle(
        title=raw.article.title,
        category="token_delist",
        base_tokens=base_tokens,
        linked_contracts=link_spot_to_contracts(base_tokens, known_futures),
    )


def collect() -> tuple[set[str], set[str], list[ParsedArticle]]:
    articles = fetch_delisting_articles()[:MAX_ARTICLES]
    print(f"📥 抓取最新 {len(articles)} 条下架公告")

    live_futures = fetch_live_futures_symbols()
    print(f"📡 币安当前存量合约: {len(live_futures)}")

    # 第一轮：抓全部详情，分类并收集 futures 公告里的直接合约（作为"已知合约"累积）
    raws: list[RawArticle] = []
    direct: set[str] = set()
    for idx, article in enumerate(articles, 1):
        raw = fetch_and_classify(article)
        if not raw:
            continue
        raws.append(raw)
        tag = {
            "futures": "🔻",
            "token_delist": "🔗",
            "spot_pair": "·",
            "other": "·",
        }.get(raw.category, "?")
        hit_hint = ""
        if raw.category == "futures":
            parsed = parse_futures(raw)
            raw.direct_contracts = parsed.direct_contracts
            direct.update(parsed.direct_contracts)
            hit_hint = f"(+{len(parsed.direct_contracts)})"
        print(
            f"  {idx:>3}/{len(articles)} {tag} [{raw.category:>12}] "
            f"{article.title[:60]}  {hit_hint}"
        )

    known_futures = live_futures | direct
    print(f"🧾 合约全集 (live ∪ 公告提取): {len(known_futures)}")

    # 第二轮：用合约全集回填 token_delist 的关联合约
    linked: set[str] = set()
    parsed_list: list[ParsedArticle] = []
    for raw in raws:
        if raw.category == "futures":
            parsed_list.append(
                ParsedArticle(
                    title=raw.article.title,
                    category="futures",
                    direct_contracts=raw.direct_contracts,
                )
            )
        elif raw.category == "token_delist":
            parsed = parse_token_delist(raw, known_futures)
            linked.update(parsed.linked_contracts)
            parsed_list.append(parsed)
        else:
            parsed_list.append(ParsedArticle(title=raw.article.title, category=raw.category))

    return direct, linked, parsed_list


OUTPUT_PATH = "delisted_symbols.json"


def write_output(
    direct: set[str],
    linked: set[str],
    path: str = OUTPUT_PATH,
) -> None:
    """只写合并去重后的合约清单。
    排序固定，刻意不带时间戳，避免 GitHub Action 产生空提交。"""
    payload = {"all_contracts": sorted(direct | linked)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> None:
    direct, linked, _ = collect()
    write_output(direct, linked)
    print("\n===== 汇总 =====")
    print(f"🔻 直接合约下架: {len(direct)}")
    print(f"🔗 现货关联合约: {len(linked)}")
    print(f"📦 合计（去重后）: {len(direct | linked)}")
    print(f"📝 已写入 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
