from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import re
from xml.etree import ElementTree as ET

import httpx

from app.core.config import get_settings
from app.services.runtime_cache import get_or_set


RSS_SOURCES = [
    {"name": "Reuters Markets", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
]

CN_NEWS_SOURCES = [
    "cls",
    "eastmoney",
    "10jqka",
    "wallstreetcn",
    "yicai",
]


def _normalize_market(value: str | None) -> str | None:
    raw = str(value or "").strip().upper()
    return raw or None


def _infer_market_from_ticker(ticker: str | None) -> str | None:
    raw = str(ticker or "").strip().upper()
    if not raw:
        return None
    if raw.endswith((".SS", ".SZ", ".BJ")):
        return "CN"
    if raw.endswith(".HK"):
        return "HK"
    return "US"


def _cn_code_from_ticker(ticker: str | None) -> str:
    raw = str(ticker or "").strip().upper()
    match = re.match(r"^(\d{6})", raw)
    return match.group(1) if match else raw


def _query_terms(ticker: str, name: str | None = None) -> set[str]:
    terms = {str(ticker or "").strip().upper()}
    cn_code = _cn_code_from_ticker(ticker)
    if cn_code:
        terms.add(cn_code)
    raw_name = str(name or "").strip()
    if raw_name:
        terms.add(raw_name.upper())
        terms.update(part.upper() for part in raw_name.split() if part.strip())
    return {term for term in terms if term}


def _contains_terms(text: str, terms: set[str]) -> bool:
    normalized = str(text or "").upper()
    return any(term in normalized for term in terms)


class MarketNewsService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def fetch_headlines(self, *, limit: int = 8, timeout: float = 6.0, market: str | None = None) -> list[dict]:
        normalized_market = _normalize_market(market)
        if normalized_market == "CN":
            return self._fetch_cn_general_headlines(limit=limit)
        return self._fetch_rss_headlines(limit=limit, timeout=timeout)

    def fetch_symbol_headlines(
        self,
        *,
        ticker: str,
        name: str | None = None,
        market: str | None = None,
        limit: int = 5,
        timeout: float = 6.0,
    ) -> list[dict]:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_market = _normalize_market(market) or _infer_market_from_ticker(normalized_ticker)
        if not normalized_ticker:
            return []

        def _load() -> list[dict]:
            if normalized_market == "CN":
                rows = self._fetch_cn_symbol_headlines(
                    ticker=normalized_ticker,
                    name=name,
                    limit=limit,
                )
                return rows[:limit]
            if normalized_market == "US":
                rows = self._fetch_us_symbol_headlines(
                    ticker=normalized_ticker,
                    limit=limit,
                    timeout=timeout,
                )
                if rows:
                    return rows[:limit]
            return self._fetch_rss_symbol_headlines(
                ticker=normalized_ticker,
                name=name,
                limit=limit,
                timeout=timeout,
            )

        key = f"{normalized_market or 'ALL'}::{normalized_ticker}::{(name or '').upper()}::{limit}"
        return get_or_set("symbol_headlines", key, ttl_seconds=300.0, loader=_load)

    def _fetch_rss_headlines(self, *, limit: int = 8, timeout: float = 6.0) -> list[dict]:
        def _load() -> list[dict]:
            items: list[dict] = []
            for source in RSS_SOURCES:
                try:
                    response = httpx.get(source["url"], timeout=timeout, follow_redirects=True)
                    response.raise_for_status()
                    items.extend(self._parse_rss(response.text, source_name=source["name"]))
                except Exception:
                    continue
            items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
            return items[:limit]

        return get_or_set("market_headlines", f"rss::{limit}", ttl_seconds=300.0, loader=_load)

    def _fetch_rss_symbol_headlines(
        self,
        *,
        ticker: str,
        name: str | None,
        limit: int,
        timeout: float,
    ) -> list[dict]:
        terms = _query_terms(ticker, name)
        all_items = self._fetch_rss_headlines(limit=24, timeout=timeout)
        matched = [
            item
            for item in all_items
            if _contains_terms(f"{item.get('title', '')} {item.get('summary', '')}", terms)
        ]
        return (matched or all_items)[:limit]

    def _fetch_cn_general_headlines(self, *, limit: int = 8) -> list[dict]:
        def _load() -> list[dict]:
            rows: list[dict] = []
            for src in CN_NEWS_SOURCES:
                rows.extend(self._fetch_tushare_news_rows(src=src, limit=limit))
            rows.sort(key=lambda item: item.get("published_at") or "", reverse=True)
            return rows[:limit]

        return get_or_set("market_headlines", f"cn::{limit}", ttl_seconds=300.0, loader=_load)

    def _fetch_cn_symbol_headlines(self, *, ticker: str, name: str | None, limit: int) -> list[dict]:
        akshare_rows = self._fetch_akshare_cn_symbol_headlines(ticker=ticker, limit=limit)
        if akshare_rows:
            return akshare_rows[:limit]
        terms = _query_terms(ticker, name)
        general_rows = self._fetch_cn_general_headlines(limit=48)
        matched = [
            item
            for item in general_rows
            if _contains_terms(f"{item.get('title', '')} {item.get('summary', '')}", terms)
        ]
        return matched[:limit]

    def _fetch_akshare_cn_symbol_headlines(self, *, ticker: str, limit: int) -> list[dict]:
        symbol_code = _cn_code_from_ticker(ticker)
        if not symbol_code:
            return []

        def _load() -> list[dict]:
            try:
                import akshare as ak  # type: ignore
            except ImportError:
                return []
            try:
                frame = ak.stock_news_em(symbol=symbol_code)
            except Exception:
                return []
            if frame is None or frame.empty:
                return []
            rows: list[dict] = []
            for _, row in frame.head(max(limit * 2, limit)).iterrows():
                item = row.to_dict()
                title = str(item.get("新闻标题") or item.get("title") or "").strip()
                if not title:
                    continue
                rows.append(
                    {
                        "source": str(item.get("文章来源") or "东方财富").strip() or "东方财富",
                        "title": title,
                        "link": str(item.get("新闻链接") or "").strip(),
                        "summary": str(item.get("新闻内容") or "").strip(),
                        "published_at": self._normalize_date(item.get("发布时间")),
                        "market": "CN",
                    }
                )
            rows.sort(key=lambda item: item.get("published_at") or "", reverse=True)
            return rows[:limit]

        return get_or_set("cn_symbol_headlines", f"akshare::{symbol_code}::{limit}", ttl_seconds=900.0, loader=_load)

    def _fetch_tushare_news_rows(self, *, src: str, limit: int) -> list[dict]:
        if not self.settings.tushare_token:
            return []
        try:
            import tushare as ts  # type: ignore
        except ImportError:
            return []

        pro = ts.pro_api(self.settings.tushare_token)
        if pro is None:
            return []

        now = datetime.now()
        start = now - timedelta(days=2)
        try:
            frame = pro.news(
                src=src,
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=now.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            return []
        if frame is None or frame.empty:
            return []

        rows: list[dict] = []
        for _, row in frame.head(max(limit * 2, limit)).iterrows():
            item = row.to_dict()
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if not title:
                continue
            rows.append(
                {
                    "source": f"TuShare:{src}",
                    "title": title,
                    "link": item.get("url") or "",
                    "summary": content,
                    "published_at": self._normalize_date(item.get("datetime")),
                    "market": "CN",
                }
            )
        return rows[:limit]

    def _fetch_us_symbol_headlines(
        self,
        *,
        ticker: str,
        limit: int,
        timeout: float,
    ) -> list[dict]:
        if not self.settings.polygon_api_key:
            return []

        def _load() -> list[dict]:
            url = f"{self.settings.polygon_endpoint.rstrip('/')}/v2/reference/news"
            response = httpx.get(
                url,
                params={
                    "ticker": ticker,
                    "limit": max(1, min(int(limit), 50)),
                    "sort": "published_utc",
                    "order": "desc",
                    "apiKey": self.settings.polygon_api_key,
                },
                timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            rows: list[dict] = []
            for item in payload.get("results") or []:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                publisher = item.get("publisher") or {}
                rows.append(
                    {
                        "source": publisher.get("name") or "Polygon News",
                        "title": title,
                        "link": item.get("article_url") or "",
                        "summary": item.get("description") or "",
                        "published_at": self._normalize_date(item.get("published_utc")),
                        "market": "US",
                    }
                )
            return rows[:limit]

        return get_or_set("us_symbol_headlines", f"{ticker}::{limit}", ttl_seconds=300.0, loader=_load)

    def _parse_rss(self, xml_text: str, *, source_name: str) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        items: list[dict] = []
        for node in root.findall(".//item"):
            title = self._node_text(node.find("title"))
            link = self._node_text(node.find("link"))
            description = self._node_text(node.find("description"))
            published_raw = self._node_text(node.find("pubDate"))
            if not title:
                continue
            items.append(
                {
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "summary": description,
                    "published_at": self._normalize_date(published_raw),
                }
            )
        return items

    def _node_text(self, node) -> str:
        if node is None or node.text is None:
            return ""
        return str(node.text).strip()

    def _normalize_date(self, value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return parsedate_to_datetime(raw).isoformat()
        except Exception:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
            except Exception:
                return raw
