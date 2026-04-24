from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import Session

from app.services.market_news import MarketNewsService
from app.services.news_nlp import analyze_news_articles
from app.services.portfolio_book import load_portfolio_positions
from app.services.repository import WatchlistRepository, WorkspaceSnapshotRepository
from app.services.time_utils import app_now_iso, app_today_iso


SNAPSHOT_WATCHLIST_NLP = "watchlist_nlp_workspace"
SNAPSHOT_PORTFOLIO_NLP = "portfolio_nlp_workspace"
SNAPSHOT_DASHBOARD_NLP = "dashboard_nlp_workspace"

NEWS_ENRICHMENT_JOB_TYPE = "news_enrichment"


def _load_watchlist_items(db: Session) -> list[dict]:
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    return watchlist_repo.list_items(watchlist.id)


def _analyze_tickers(db: Session, tickers: list[str], names: dict[str, str], markets: dict[str, str | None]) -> list[dict]:
    service = MarketNewsService()
    rows: list[dict] = []
    for ticker in tickers:
        name = names.get(ticker) or ticker
        market = markets.get(ticker)
        try:
            articles = service.fetch_symbol_headlines(ticker=ticker, name=name, market=market, limit=6)
        except Exception:
            articles = []
        query_terms = {ticker.upper()}
        raw_name = str(name or "").strip()
        if raw_name:
            query_terms.add(raw_name.upper())
            query_terms.update(part.upper() for part in raw_name.split() if part.strip())
        matched_articles = [
            article
            for article in articles
            if any(
                term and term in f"{article.get('title', '')} {article.get('summary', '')}".upper()
                for term in query_terms
            )
        ]
        articles = matched_articles
        nlp = analyze_news_articles(articles)
        source_counts = Counter(str(article.get("source") or "").strip() for article in articles if article.get("source"))
        rows.append(
            {
                "ticker": ticker,
                "name": name,
                "market": market,
                "sentiment_label": nlp["sentiment_label"],
                "sentiment_score": nlp["sentiment_score"],
                "topic_label": nlp["topic_label"],
                "risk_tags": nlp["risk_tags"],
                "entities": nlp["entities"],
                "summary_text": nlp["summary_text"],
                "headline_count": nlp["headline_count"],
                "positive_count": nlp["positive_count"],
                "negative_count": nlp["negative_count"],
                "headlines": nlp["headlines"],
                "source_counts": dict(source_counts),
                "updated_at": app_now_iso(),
            }
        )
    rows.sort(key=lambda item: (item["sentiment_score"], -item["headline_count"], item["ticker"]), reverse=True)
    return rows


def summarize_news_rows(rows: list[dict]) -> dict:
    total = len(rows)
    matched_rows = [row for row in rows if int(row.get("headline_count") or 0) > 0]
    source_counter: Counter[str] = Counter()
    market_counter: Counter[str] = Counter()
    headline_total = 0
    positive_total = 0
    negative_total = 0
    for row in rows:
        market = str(row.get("market") or "").strip().upper()
        if market:
            market_counter[market] += 1
        headline_total += int(row.get("headline_count") or 0)
        positive_total += int(row.get("positive_count") or 0)
        negative_total += int(row.get("negative_count") or 0)
        for source, count in (row.get("source_counts") or {}).items():
            source_counter[str(source)] += int(count or 0)
    matched_total = len(matched_rows)
    return {
        "ticker_count": total,
        "matched_ticker_count": matched_total,
        "coverage_pct": round((matched_total / total) * 100.0, 1) if total else 0.0,
        "headline_total": headline_total,
        "positive_total": positive_total,
        "negative_total": negative_total,
        "top_sources": [{"source": source, "count": count} for source, count in source_counter.most_common(5)],
        "markets": dict(market_counter),
        "updated_at": app_now_iso(),
    }


def build_watchlist_nlp_snapshot(db: Session, *, lang: str | None = None) -> dict:
    del lang
    items = _load_watchlist_items(db)
    tickers = [str(item.get("ticker") or "").strip().upper() for item in items if item.get("ticker")]
    names = {str(item.get("ticker") or "").strip().upper(): item.get("name") or item.get("ticker") for item in items}
    markets = {str(item.get("ticker") or "").strip().upper(): item.get("market") for item in items}
    rows = _analyze_tickers(db, tickers, names, markets)
    return {"rows": rows, "meta": summarize_news_rows(rows), "updated_at": app_now_iso()}


def build_portfolio_nlp_snapshot(db: Session, *, lang: str | None = None) -> dict:
    del lang
    positions = load_portfolio_positions()
    tickers = [str(item.get("ticker") or "").strip().upper() for item in positions if item.get("ticker")]
    names = {str(item.get("ticker") or "").strip().upper(): item.get("name") or item.get("ticker") for item in positions}
    markets = {str(item.get("ticker") or "").strip().upper(): item.get("market") for item in positions}
    rows = _analyze_tickers(db, tickers, names, markets)
    return {"rows": rows, "meta": summarize_news_rows(rows), "updated_at": app_now_iso()}


def build_dashboard_nlp_snapshot(db: Session, *, lang: str | None = None) -> dict:
    watchlist_payload = build_watchlist_nlp_snapshot(db, lang=lang)
    rows = list(watchlist_payload.get("rows") or [])
    opportunities = sorted(
        [
            item
            for item in rows
            if float(item.get("sentiment_score") or 0.0) > 0
        ],
        key=lambda item: (
            -float(item.get("sentiment_score") or 0.0),
            -int(item.get("headline_count") or 0),
            item.get("ticker") or "",
        ),
    )[:5]
    risks = sorted(
        [
            item
            for item in rows
            if float(item.get("sentiment_score") or 0.0) < 0
            or (item.get("risk_tags") and float(item.get("sentiment_score") or 0.0) <= 0)
        ],
        key=lambda item: (
            0 if float(item.get("sentiment_score") or 0.0) < 0 else 1,
            float(item.get("sentiment_score") or 0.0),
            -len(item.get("risk_tags") or []),
            -int(item.get("headline_count") or 0),
            item.get("ticker") or "",
        ),
    )[:5]
    return {
        "opportunities": opportunities,
        "risks": risks,
        "meta": watchlist_payload.get("meta") or summarize_news_rows(rows),
        "updated_at": app_now_iso(),
    }


def refresh_nlp_snapshots(db: Session, *, source_job_id: int | None = None) -> dict:
    repo = WorkspaceSnapshotRepository(db)
    snapshot_date = app_today_iso()
    created: dict[str, dict] = {}
    builders = {
        SNAPSHOT_WATCHLIST_NLP: build_watchlist_nlp_snapshot,
        SNAPSHOT_PORTFOLIO_NLP: build_portfolio_nlp_snapshot,
        SNAPSHOT_DASHBOARD_NLP: build_dashboard_nlp_snapshot,
    }
    for snapshot_type, builder in builders.items():
        payload = builder(db)
        row = repo.create_snapshot(
            snapshot_type=snapshot_type,
            snapshot_date=snapshot_date,
            payload=payload,
            source_job_id=source_job_id,
        )
        created[snapshot_type] = {
            "id": row.id,
            "snapshot_date": row.snapshot_date,
            "created_at": row.created_at,
        }
    return created
