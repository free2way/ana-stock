from __future__ import annotations

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


def _analyze_tickers(db: Session, tickers: list[str], names: dict[str, str]) -> list[dict]:
    service = MarketNewsService()
    rows: list[dict] = []
    for ticker in tickers:
        name = names.get(ticker) or ticker
        try:
            articles = service.fetch_symbol_headlines(ticker=ticker, name=name, limit=6)
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
        rows.append(
            {
                "ticker": ticker,
                "name": name,
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
                "updated_at": app_now_iso(),
            }
        )
    rows.sort(key=lambda item: (item["sentiment_score"], -item["headline_count"], item["ticker"]), reverse=True)
    return rows


def build_watchlist_nlp_snapshot(db: Session, *, lang: str | None = None) -> dict:
    del lang
    items = _load_watchlist_items(db)
    tickers = [str(item.get("ticker") or "").strip().upper() for item in items if item.get("ticker")]
    names = {str(item.get("ticker") or "").strip().upper(): item.get("name") or item.get("ticker") for item in items}
    rows = _analyze_tickers(db, tickers, names)
    return {"rows": rows, "updated_at": app_now_iso()}


def build_portfolio_nlp_snapshot(db: Session, *, lang: str | None = None) -> dict:
    del lang
    positions = load_portfolio_positions()
    tickers = [str(item.get("ticker") or "").strip().upper() for item in positions if item.get("ticker")]
    names = {str(item.get("ticker") or "").strip().upper(): item.get("name") or item.get("ticker") for item in positions}
    rows = _analyze_tickers(db, tickers, names)
    return {"rows": rows, "updated_at": app_now_iso()}


def build_dashboard_nlp_snapshot(db: Session, *, lang: str | None = None) -> dict:
    watchlist_payload = build_watchlist_nlp_snapshot(db, lang=lang)
    rows = list(watchlist_payload.get("rows") or [])
    opportunities = [item for item in rows if float(item.get("sentiment_score") or 0.0) > 0][:5]
    risks = [item for item in rows if item.get("risk_tags") or float(item.get("sentiment_score") or 0.0) < 0][:5]
    return {
        "opportunities": opportunities,
        "risks": risks,
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
