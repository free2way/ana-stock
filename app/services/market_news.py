from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx
from app.services.runtime_cache import get_or_set


RSS_SOURCES = [
    {"name": "Reuters Markets", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
]


class MarketNewsService:
    def fetch_headlines(self, *, limit: int = 8, timeout: float = 6.0) -> list[dict]:
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

        return get_or_set("market_headlines", f"limit={limit}", ttl_seconds=300.0, loader=_load)

    def fetch_symbol_headlines(
        self,
        *,
        ticker: str,
        name: str | None = None,
        limit: int = 5,
        timeout: float = 6.0,
    ) -> list[dict]:
        query_terms = {ticker.upper()}
        if name:
            query_terms.update(part.upper() for part in str(name).split() if part.strip())
            query_terms.add(str(name).upper())

        def _load() -> list[dict]:
            all_items = self.fetch_headlines(limit=24, timeout=timeout)
            matched = [
                item
                for item in all_items
                if any(term and term in f"{item.get('title', '')} {item.get('summary', '')}".upper() for term in query_terms)
            ]
            return (matched or all_items)[:limit]

        key = f"{ticker.upper()}::{(name or '').upper()}::{limit}"
        return get_or_set("symbol_headlines", key, ttl_seconds=300.0, loader=_load)

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

    def _normalize_date(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return parsedate_to_datetime(raw).isoformat()
        except Exception:
            try:
                return datetime.fromisoformat(raw).isoformat()
            except Exception:
                return raw
