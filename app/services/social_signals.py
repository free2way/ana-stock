from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.market_sync import sync_market_data
from app.services.portfolio_book import load_portfolio_positions
from app.services.repository import AppSettingRepository, DataJobRepository, PredictionRepository, SymbolRepository, WatchlistRepository
from app.services.time_utils import app_now, parse_app_datetime


SOCIAL_ACCOUNTS_KEY = "social_signal_accounts"
SOCIAL_POSTS_KEY = "social_signal_posts"
SOCIAL_ANALYSES_KEY = "social_signal_analyses"
SOCIAL_POLL_STATE_KEY = "social_signal_poll_state"
SOCIAL_US_SYNC_FAILURES_KEY = "social_us_price_sync_failures"
SOCIAL_POLL_JOB_TYPE = "social_signal_poll"
_SOCIAL_US_SYNC_BLACKLIST = {"ALRIB", "IQE", "KUR", "RPI", "SIVE", "SOI"}

_US_SYMBOL_STOPWORDS = {
    "A",
    "I",
    "P",
    "S",
    "ALL",
    "AM",
    "AND",
    "ANY",
    "ARE",
    "AS",
    "AT",
    "ATH",
    "BACK",
    "BE",
    "BEAR",
    "BEST",
    "BIG",
    "BULL",
    "BUY",
    "CAN",
    "CEO",
    "CFO",
    "CBD",
    "CLOSE",
    "CPO",
    "CPI",
    "DAY",
    "DCA",
    "DM",
    "DO",
    "DONT",
    "DOWN",
    "EPS",
    "ETF",
    "Fed".upper(),
    "FOMC",
    "FOR",
    "FROM",
    "GDP",
    "GOOD",
    "GO",
    "GPU",
    "HIGH",
    "HOT",
    "HOW",
    "IPO",
    "IR",
    "IS",
    "IT",
    "INTO",
    "LIKE",
    "LIST",
    "LOL",
    "LONG",
    "LOW",
    "MACD",
    "MA",
    "MARKET",
    "MO",
    "MORE",
    "MY",
    "NEW",
    "NEXT",
    "NEWS",
    "NO",
    "NOT",
    "NOW",
    "NYSE",
    "OLD",
    "ONLY",
    "OPEN",
    "OR",
    "PE",
    "PT",
    "QQ",
    "RSI",
    "SEC",
    "SELL",
    "SHORT",
    "SMALL",
    "SPX",
    "STOCK",
    "STOCKS",
    "TA",
    "THE",
    "THIS",
    "TODAY",
    "TO",
    "UP",
    "USD",
    "US",
    "VWAP",
    "WATCH",
    "WEEK",
    "WHEN",
    "WHY",
    "WITH",
    "YTD",
    "YOLO",
}

_US_COMPANY_ALIASES = {
    "apple": ("AAPL", "Apple"),
    "tesla": ("TSLA", "Tesla"),
    "nvidia": ("NVDA", "NVIDIA"),
    "nvda": ("NVDA", "NVIDIA"),
    "microsoft": ("MSFT", "Microsoft"),
    "msft": ("MSFT", "Microsoft"),
    "amazon": ("AMZN", "Amazon"),
    "google": ("GOOGL", "Alphabet"),
    "alphabet": ("GOOGL", "Alphabet"),
    "meta": ("META", "Meta Platforms"),
    "facebook": ("META", "Meta Platforms"),
    "netflix": ("NFLX", "Netflix"),
    "amd": ("AMD", "AMD"),
    "advanced micro devices": ("AMD", "AMD"),
    "palantir": ("PLTR", "Palantir"),
    "pltr": ("PLTR", "Palantir"),
    "super micro": ("SMCI", "Super Micro Computer"),
    "supermicro": ("SMCI", "Super Micro Computer"),
    "smci": ("SMCI", "Super Micro Computer"),
    "broadcom": ("AVGO", "Broadcom"),
    "avgo": ("AVGO", "Broadcom"),
    "qualcomm": ("QCOM", "Qualcomm"),
    "qcom": ("QCOM", "Qualcomm"),
    "intel": ("INTC", "Intel"),
    "intc": ("INTC", "Intel"),
    "micron": ("MU", "Micron"),
    "snowflake": ("SNOW", "Snowflake"),
    "snow": ("SNOW", "Snowflake"),
    "salesforce": ("CRM", "Salesforce"),
    "oracle": ("ORCL", "Oracle"),
    "uber": ("UBER", "Uber"),
    "airbnb": ("ABNB", "Airbnb"),
    "coinbase": ("COIN", "Coinbase"),
    "coin": ("COIN", "Coinbase"),
    "robinhood": ("HOOD", "Robinhood"),
    "hood": ("HOOD", "Robinhood"),
    "rocket lab": ("RKLB", "Rocket Lab"),
    "rklb": ("RKLB", "Rocket Lab"),
    "sofi": ("SOFI", "SoFi"),
    "block": ("SQ", "Block"),
    "shopify": ("SHOP", "Shopify"),
    "shop": ("SHOP", "Shopify"),
    "eli lilly": ("LLY", "Eli Lilly"),
    "lilly": ("LLY", "Eli Lilly"),
    "novo nordisk": ("NVO", "Novo Nordisk"),
    "berkshire": ("BRK.B", "Berkshire Hathaway"),
    "berkshire hathaway": ("BRK.B", "Berkshire Hathaway"),
    "spider": ("SPY", "SPDR S&P 500 ETF"),
    "s&p 500 etf": ("SPY", "SPDR S&P 500 ETF"),
    "nasdaq 100": ("QQQ", "Invesco QQQ"),
}


def load_social_accounts(db: Session) -> list[dict]:
    return _load_json_list(db, SOCIAL_ACCOUNTS_KEY)


def save_social_accounts(db: Session, accounts: list[dict]) -> None:
    AppSettingRepository(db).set(SOCIAL_ACCOUNTS_KEY, json.dumps(accounts, ensure_ascii=False))


def add_social_account(db: Session, handle: str, *, note: str = "") -> list[dict]:
    normalized = _normalize_handle(handle)
    if not normalized:
        return load_social_accounts(db)
    accounts = load_social_accounts(db)
    existing = next((item for item in accounts if item.get("handle") == normalized), None)
    if existing is None:
        accounts.append({"handle": normalized, "note": note.strip(), "created_at": _now_iso()})
    else:
        existing["note"] = note.strip() or existing.get("note") or ""
    save_social_accounts(db, accounts)
    return accounts


def remove_social_account(db: Session, handle: str) -> list[dict]:
    normalized = _normalize_handle(handle)
    accounts = [item for item in load_social_accounts(db) if item.get("handle") != normalized]
    save_social_accounts(db, accounts)
    return accounts


def get_social_poll_status(db: Session | None = None) -> dict:
    if db is None:
        with SessionLocal() as own_db:
            return get_social_poll_status(own_db)
    state = _load_json_object(db, SOCIAL_POLL_STATE_KEY)
    settings = get_settings()
    return {
        "enabled": True,
        "interval_minutes": 240,
        "provider": "x_api" if settings.x_bearer_token else "not_configured",
        "configured": bool(settings.x_bearer_token),
        "last_run_at": state.get("last_run_at"),
        "last_status": state.get("last_status"),
        "last_message": state.get("last_message"),
        "last_new_posts": int(state.get("last_new_posts") or 0),
        "last_new_mentions": int(state.get("last_new_mentions") or 0),
    }


def poll_tracked_social_accounts(db: Session, *, max_posts_per_account: int = 5) -> dict:
    accounts = load_social_accounts(db)
    state = _load_json_object(db, SOCIAL_POLL_STATE_KEY)
    settings = get_settings()
    if not accounts:
        result = {
            "status": "empty",
            "message": "No tracked X accounts.",
            "accounts": 0,
            "new_posts": 0,
            "new_mentions": 0,
            "fetched_posts": [],
            "errors": [],
        }
        _save_social_poll_state(db, state, result)
        return result
    if not settings.x_bearer_token:
        result = {
            "status": "not_configured",
            "message": "X Bearer Token is not configured. Set PQW_X_BEARER_TOKEN to enable automatic X polling.",
            "accounts": len(accounts),
            "new_posts": 0,
            "new_mentions": 0,
            "fetched_posts": [],
            "errors": [{"reason": "missing_x_bearer_token"}],
        }
        _save_social_poll_state(db, state, result)
        return result

    fetched_posts: list[dict] = []
    errors: list[dict] = []
    analyses: list[dict] = []
    for account in accounts:
        handle = _normalize_handle(account.get("handle") or "")
        if not handle:
            continue
        try:
            posts = fetch_x_account_posts(handle, max_results=max_posts_per_account)
        except Exception as exc:
            errors.append({"handle": handle, "error": str(exc)})
            continue
        for post in posts:
            try:
                analysis = add_social_post(
                    db,
                    handle=handle,
                    content=post.get("content") or "",
                    source_url=post.get("source_url") or "",
                )
            except Exception as exc:
                errors.append(
                    {
                        "handle": handle,
                        "post_id": post.get("id"),
                        "error": f"post_analysis_failed: {exc}",
                    }
                )
                continue
            if analysis:
                fetched_posts.append(post)
                analyses.append(analysis)
    sync_job = start_social_us_price_sync_job(db, analyses) if analyses else None
    new_mentions = sum(len(item.get("mentions") or []) for item in analyses)
    result = {
        "status": "success" if not errors else "partial",
        "message": f"Fetched {len(fetched_posts)} new/known post(s), parsed {new_mentions} mention(s).",
        "accounts": len(accounts),
        "new_posts": len(fetched_posts),
        "new_mentions": new_mentions,
        "fetched_posts": fetched_posts,
        "sync_job": sync_job,
        "errors": errors,
    }
    _save_social_poll_state(db, state, result)
    return result


def fetch_x_account_posts(handle: str, *, max_results: int = 5) -> list[dict]:
    settings = get_settings()
    token = settings.x_bearer_token
    if not token:
        raise RuntimeError("X Bearer Token is not configured.")
    username = _normalize_handle(handle).lstrip("@")
    if not username:
        return []
    endpoint = settings.x_api_endpoint.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(12.0, connect=8.0)
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        user_response = client.get(
            f"{endpoint}/users/by/username/{username}",
            params={"user.fields": "username,name"},
        )
        user_response.raise_for_status()
        user_payload = user_response.json()
        user_id = str((user_payload.get("data") or {}).get("id") or "")
        if not user_id:
            return []
        tweets_response = client.get(
            f"{endpoint}/users/{user_id}/tweets",
            params={
                "max_results": max(5, min(100, int(max_results))),
                "tweet.fields": "created_at,entities",
                "exclude": "replies,retweets",
            },
        )
        tweets_response.raise_for_status()
        payload = tweets_response.json()
    posts: list[dict] = []
    for item in payload.get("data") or []:
        text = _clean_post_content(item.get("text") or "")
        if not text:
            continue
        tweet_id = str(item.get("id") or "")
        posts.append(
            {
                "id": tweet_id,
                "handle": f"@{username}",
                "content": text,
                "source_url": f"https://x.com/{username}/status/{tweet_id}" if tweet_id else "",
                "created_at": item.get("created_at") or _now_iso(),
            }
        )
    return posts


def load_social_posts(db: Session) -> list[dict]:
    return _load_json_list(db, SOCIAL_POSTS_KEY)


def save_social_posts(db: Session, posts: list[dict]) -> None:
    AppSettingRepository(db).set(SOCIAL_POSTS_KEY, json.dumps(posts[-200:], ensure_ascii=False))


def add_social_post(db: Session, *, handle: str, content: str, source_url: str = "") -> dict:
    normalized = _normalize_handle(handle)
    if not normalized:
        normalized = "@manual"
    cleaned_content = _clean_post_content(content)
    if not cleaned_content:
        return {}
    post = {
        "id": f"post_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "handle": normalized,
        "content": cleaned_content,
        "source_url": source_url.strip(),
        "created_at": _now_iso(),
    }
    posts = load_social_posts(db)
    existing = next(
        (
            item
            for item in reversed(posts)
            if item.get("handle") == normalized
            and _clean_post_content(item.get("content") or "") == cleaned_content
            and str(item.get("source_url") or "").strip() == source_url.strip()
        ),
        None,
    )
    if existing is not None:
        return analyze_social_post(db, existing)
    posts.append(post)
    save_social_posts(db, posts)
    analysis = analyze_social_post(db, post)
    analyses = load_social_analyses(db)
    analyses.append(analysis)
    save_social_analyses(db, analyses)
    return analysis


def remove_social_analysis_record(db: Session, *, analysis_id: str, ticker: str | None = None) -> bool:
    normalized_id = str(analysis_id or "").strip()
    normalized_ticker = str(ticker or "").strip().upper()
    if not normalized_id:
        return False
    analyses = load_social_analyses(db)
    changed = False
    kept_analyses: list[dict] = []
    removed_post_ids: set[str] = set()
    for analysis in analyses:
        if str(analysis.get("id") or "") != normalized_id:
            kept_analyses.append(analysis)
            continue
        if normalized_ticker:
            mentions = [
                mention
                for mention in (analysis.get("mentions") or [])
                if str(mention.get("ticker") or "").upper() != normalized_ticker
            ]
            if len(mentions) != len(analysis.get("mentions") or []):
                changed = True
            if mentions:
                analysis["mentions"] = mentions
                kept_analyses.append(analysis)
            else:
                removed_post_ids.add(str(analysis.get("post_id") or ""))
        else:
            changed = True
            removed_post_ids.add(str(analysis.get("post_id") or ""))
    if not changed and len(kept_analyses) == len(analyses):
        return False
    save_social_analyses(db, kept_analyses)
    if removed_post_ids:
        posts = [post for post in load_social_posts(db) if str(post.get("id") or "") not in removed_post_ids]
        save_social_posts(db, posts)
    return True


def add_social_posts_batch(db: Session, *, handle: str, content: str, source_url: str = "") -> list[dict]:
    chunks = _split_post_batch(content)
    analyses: list[dict] = []
    for chunk in chunks:
        analysis = add_social_post(db, handle=handle, content=chunk, source_url=source_url)
        if analysis:
            analyses.append(analysis)
    return analyses


def load_social_analyses(db: Session) -> list[dict]:
    return _load_json_list(db, SOCIAL_ANALYSES_KEY)


def save_social_analyses(db: Session, analyses: list[dict]) -> None:
    AppSettingRepository(db).set(SOCIAL_ANALYSES_KEY, json.dumps(analyses[-300:], ensure_ascii=False))


def _load_social_us_sync_failures(db: Session) -> dict[str, dict]:
    raw = AppSettingRepository(db).get(SOCIAL_US_SYNC_FAILURES_KEY)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for ticker, item in payload.items():
        normalized = _normalize_us_symbol_token(str(ticker or ""))
        if not normalized or not isinstance(item, dict):
            continue
        cleaned[normalized] = {
            "count": int(item.get("count") or 0),
            "last_failed_at": item.get("last_failed_at"),
            "last_message": item.get("last_message"),
            "suppressed": bool(item.get("suppressed")),
        }
    return cleaned


def _save_social_us_sync_failures(db: Session, payload: dict[str, dict]) -> None:
    AppSettingRepository(db).set(SOCIAL_US_SYNC_FAILURES_KEY, json.dumps(payload, ensure_ascii=False))


def _is_ambiguous_social_us_ticker(ticker: str) -> bool:
    normalized = _normalize_us_symbol_token(ticker)
    if len(normalized.replace(".", "")) <= 2:
        return True
    if re.fullmatch(r"[A-Z]\d", normalized):
        return True
    return False


def _is_terminal_social_us_sync_failure(message: str | None) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    terminal_markers = (
        "no data",
        "not found",
        "no price data",
        "possibly delisted",
        "no timezone found",
        "history not found",
        "returned no rows",
        "symbol not found",
        "failed to fetch",
        "empty dataframe",
        "not available",
        "no market data",
    )
    return any(marker in normalized for marker in terminal_markers)


def _should_suppress_social_us_ticker(ticker: str, failure_state: dict[str, dict] | None = None) -> bool:
    normalized = _normalize_us_symbol_token(ticker)
    if not normalized:
        return True
    if normalized in _SOCIAL_US_SYNC_BLACKLIST:
        return True
    item = (failure_state or {}).get(normalized) or {}
    if bool(item.get("suppressed")):
        return True
    return False


def _mark_social_mentions_suppressed(
    analyses: list[dict],
    *,
    tickers: set[str],
    reason: str,
    job_id: int | None = None,
) -> bool:
    changed = False
    for analysis in analyses:
        for mention in analysis.get("mentions") or []:
            ticker = _normalize_us_symbol_token(str(mention.get("ticker") or ""))
            if str(mention.get("market") or "").upper() != "US" or ticker not in tickers:
                continue
            mention["sync_suppressed"] = True
            mention["sync_suppressed_reason"] = reason
            if job_id is not None:
                mention["price_sync_job_id"] = job_id
            changed = True
    return changed


def start_social_us_price_sync_job(db: Session, analyses: list[dict], *, start_date: str = "2025-01-01") -> dict | None:
    analysis_ids = [str(item.get("id") or "") for item in analyses if item.get("id")]
    tickers = _us_tickers_from_analyses(analyses)
    if not analysis_ids or not tickers:
        return None
    job_repo = DataJobRepository(db)
    job_repo.complete_stale_running_jobs(
        job_types=["social_us_price_sync"],
        stale_after_hours=1,
        message_prefix="Social sync bootstrap closed a stale social U.S. price sync job.",
    )
    if job_repo.has_running_job("social_us_price_sync"):
        return {
            "job_id": None,
            "tickers": tickers,
            "count": len(tickers),
            "start_date": start_date,
            "status": "already_running",
            "message": "A social US price sync job is already running.",
        }
    failure_state = _load_social_us_sync_failures(db)
    suppressed_tickers = {ticker for ticker in tickers if _should_suppress_social_us_ticker(ticker, failure_state)}
    if suppressed_tickers:
        if _mark_social_mentions_suppressed(
            analyses,
            tickers=suppressed_tickers,
            reason="suppressed_after_price_sync_failures",
        ):
            save_social_analyses(db, analyses)
    tickers = [ticker for ticker in tickers if ticker not in suppressed_tickers]
    if not tickers:
        return {
            "job_id": None,
            "tickers": [],
            "count": 0,
            "start_date": start_date,
            "status": "suppressed",
            "suppressed_tickers": sorted(suppressed_tickers),
            "message": "All detected U.S. tickers are currently suppressed from auto sync.",
        }
    job = job_repo.create_job(
        job_type="social_us_price_sync",
        status="running",
        params={
            "analysis_ids": analysis_ids,
            "tickers": tickers,
            "provider": "auto",
            "start_date": start_date,
        },
        message=f"Queued social US price sync for {len(tickers)} ticker(s).",
    )
    thread = threading.Thread(
        target=_run_social_us_price_sync_job,
        args=(job.id, analysis_ids, tickers, start_date),
        name=f"social-us-price-sync-{job.id}",
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id, "tickers": tickers, "count": len(tickers), "start_date": start_date}


def analyze_social_post(db: Session, post: dict) -> dict:
    failure_state = _load_social_us_sync_failures(db)
    raw_symbols = _extract_symbol_mentions(db, post.get("content") or "")
    symbols: list[dict] = []
    for item in raw_symbols:
        mention = dict(item)
        ticker = str(mention.get("ticker") or "").strip().upper()
        market = str(mention.get("market") or "").strip().upper()
        if market == "US" and _should_suppress_social_us_ticker(ticker, failure_state):
            mention["sync_suppressed"] = True
            mention["sync_suppressed_reason"] = "suppressed_before_analysis"
        if _should_keep_social_mention(mention):
            symbols.append(mention)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    portfolio_tickers = {str(item.get("ticker") or "").upper() for item in load_portfolio_positions()}
    prediction_repo = PredictionRepository(db)
    try:
        outputs = prediction_repo.get_latest_model_outputs_for_tickers([item["ticker"] for item in symbols])
    except Exception:
        outputs = {}
    rows: list[dict] = []
    for item in symbols:
        ticker = item["ticker"]
        latest = outputs.get(ticker) or {}
        social_view = _infer_social_view(post.get("content") or "", item)
        validation = _validate_social_mention(
            social_view=social_view,
            latest_signal=latest,
            in_watchlist=ticker in watchlist_map,
            in_portfolio=ticker in portfolio_tickers,
        )
        rows.append(
            {
                **item,
                "social_view": social_view,
                "model_score": latest.get("score"),
                "model_signal_label": latest.get("signal_label"),
                "model_signal_strength": latest.get("signal_strength"),
                "tradability_status": latest.get("tradability_status"),
                "entry_trigger": latest.get("entry_trigger"),
                "invalidation_condition": latest.get("invalidation_condition"),
                "in_watchlist": ticker in watchlist_map,
                "in_portfolio": ticker in portfolio_tickers,
                **validation,
            }
        )
    rows.sort(key=lambda item: (-int(item.get("validation_score") or 0), item.get("ticker") or ""))
    return {
        "id": f"analysis_{post.get('id') or datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "post_id": post.get("id"),
        "handle": post.get("handle"),
        "source_url": post.get("source_url"),
        "content": post.get("content"),
        "created_at": post.get("created_at") or _now_iso(),
        "analyzed_at": _now_iso(),
        "mentions": rows,
    }


def _run_social_us_price_sync_job(job_id: int, analysis_ids: list[str], tickers: list[str], start_date: str) -> None:
    try:
        results = sync_market_data(tickers=tickers, start_date=start_date, provider="auto")
        result_map = {str(item.get("ticker") or "").upper(): item for item in results}
        success_count = sum(1 for item in results if item.get("status") == "success")
        failure_count = sum(1 for item in results if item.get("status") == "failed")
        failed_tickers = [
            str(item.get("ticker") or "").upper()
            for item in results
            if str(item.get("status") or "").lower() == "failed" and str(item.get("ticker") or "").strip()
        ]
        with SessionLocal() as db:
            analyses = load_social_analyses(db)
            selected_ids = set(analysis_ids)
            failure_state = _load_social_us_sync_failures(db)
            for ticker in tickers:
                normalized = _normalize_us_symbol_token(ticker)
                if not normalized:
                    continue
                if normalized in failed_tickers:
                    item = failure_state.get(normalized) or {}
                    next_count = int(item.get("count") or 0) + 1
                    message = str((result_map.get(normalized) or {}).get("message") or "").strip() or None
                    suppressed = bool(item.get("suppressed"))
                    if _is_ambiguous_social_us_ticker(normalized):
                        suppressed = True
                    elif _is_terminal_social_us_sync_failure(message):
                        suppressed = True
                    elif next_count >= 2:
                        suppressed = True
                    failure_state[normalized] = {
                        "count": next_count,
                        "last_failed_at": _now_iso(),
                        "last_message": message,
                        "suppressed": suppressed,
                    }
                    if suppressed:
                        _SOCIAL_US_SYNC_BLACKLIST.add(normalized)
                else:
                    failure_state.pop(normalized, None)
            changed = False
            for analysis in analyses:
                if str(analysis.get("id") or "") not in selected_ids:
                    continue
                for mention in analysis.get("mentions") or []:
                    ticker = str(mention.get("ticker") or "").upper()
                    if str(mention.get("market") or "").upper() != "US" or ticker not in result_map:
                        continue
                    result = result_map[ticker]
                    mention["price_sync_job_id"] = job_id
                    mention["price_sync_status"] = result.get("status")
                    mention["price_sync_rows"] = result.get("rows") or 0
                    mention["price_sync_stored_rows"] = result.get("stored_rows") or 0
                    mention["price_sync_last_date"] = result.get("last_synced_date")
                    mention["price_sync_provider_ticker"] = result.get("provider_ticker")
                    mention["price_sync_message"] = result.get("message")
                    changed = True
                    if ticker in failed_tickers:
                        failure_item = failure_state.get(ticker) or {}
                        if bool(failure_item.get("suppressed")):
                            mention["sync_suppressed"] = True
                            mention["sync_suppressed_reason"] = "suppressed_after_price_sync_failures"
                    else:
                        mention.pop("sync_suppressed", None)
                        mention.pop("sync_suppressed_reason", None)
            if changed:
                save_social_analyses(db, analyses)
            _save_social_us_sync_failures(db, failure_state)
            failed_suffix = ""
            if failed_tickers:
                shown = ", ".join(failed_tickers[:5])
                failed_suffix = f" Failed tickers: {shown}."
            DataJobRepository(db).complete_job(
                job_id,
                status="success" if failure_count == 0 else "partial",
                message=f"Social US price sync finished: {success_count} success, {failure_count} failed.{failed_suffix}",
                result={
                    "results": results,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "failed_tickers": failed_tickers,
                },
            )
    except Exception as exc:
        with SessionLocal() as db:
            DataJobRepository(db).complete_job(
                job_id,
                status="failed",
                message=f"Social US price sync failed: {exc}",
                result={"error": str(exc), "tickers": tickers},
            )


def social_signal_summary(db: Session) -> dict:
    analyses = load_social_analyses(db)
    raw_mentions: list[dict] = []
    stats: dict[str, dict] = {}
    for analysis in analyses:
        handle = analysis.get("handle") or "-"
        item = stats.setdefault(
            handle,
            {
                "handle": handle,
                "post_count": 0,
                "mention_count": 0,
                "actionable_count": 0,
                "top_tickers": {},
                "latest_at": None,
            },
        )
        item["post_count"] += 1
        item["latest_at"] = analysis.get("analyzed_at") or item["latest_at"]
        for mention in analysis.get("mentions") or []:
            if not _should_keep_social_mention(mention):
                continue
            enriched = {
                **mention,
                "analysis_id": analysis.get("id"),
                "post_id": analysis.get("post_id"),
                "handle": analysis.get("handle"),
                "source_url": analysis.get("source_url"),
                "content": analysis.get("content"),
                "analyzed_at": analysis.get("analyzed_at"),
            }
            raw_mentions.append(enriched)
            item["mention_count"] += 1
            ticker = mention.get("ticker")
            if ticker:
                item["top_tickers"][ticker] = item["top_tickers"].get(ticker, 0) + 1
            if mention.get("system_action") in {"加入观察", "重点验证", "Add to watch", "Validate"}:
                item["actionable_count"] += 1
    mentions = _aggregate_social_mentions(raw_mentions)
    actionable = [item for item in mentions if item.get("system_action") in {"加入观察", "重点验证", "Add to watch", "Validate"}]
    account_stats = []
    for item in stats.values():
        top_tickers = sorted(item["top_tickers"].items(), key=lambda pair: (-pair[1], pair[0]))[:4]
        account_stats.append({**item, "top_tickers": top_tickers})
    account_stats.sort(key=lambda item: (-int(item.get("actionable_count") or 0), -int(item.get("mention_count") or 0), item.get("handle") or ""))
    return {
        "accounts": load_social_accounts(db),
        "account_stats": account_stats,
        "analyses": analyses,
        "hot_mentions_24h": _build_hot_mentions_24h(mentions),
        "resonance_24h": _build_resonance_24h(mentions),
        "mentions": mentions[:80],
        "actionable": actionable[:12],
        "poll_status": get_social_poll_status(db),
    }


def _aggregate_social_mentions(mentions: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for mention in mentions:
        handle = str(mention.get("handle") or "-")
        ticker = str(mention.get("ticker") or "").upper()
        if not ticker:
            continue
        key = (handle, ticker)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                **mention,
                "mention_count": 1,
                "analysis_ids": [str(mention.get("analysis_id") or "")],
                "related_post_ids": [str(mention.get("post_id") or "")],
                "latest_analyzed_at": mention.get("analyzed_at"),
                "latest_source_url": mention.get("source_url"),
                "latest_content": mention.get("content"),
            }
            continue
        existing["mention_count"] = int(existing.get("mention_count") or 0) + 1
        existing["analysis_ids"] = list(dict.fromkeys([*(existing.get("analysis_ids") or []), str(mention.get("analysis_id") or "")]))
        existing["related_post_ids"] = list(dict.fromkeys([*(existing.get("related_post_ids") or []), str(mention.get("post_id") or "")]))
        if str(mention.get("analyzed_at") or "") >= str(existing.get("latest_analyzed_at") or ""):
            existing["latest_analyzed_at"] = mention.get("analyzed_at")
            existing["latest_source_url"] = mention.get("source_url")
            existing["latest_content"] = mention.get("content")
        if int(mention.get("validation_score") or 0) > int(existing.get("validation_score") or 0):
            keep_counts = {
                "mention_count": existing.get("mention_count"),
                "analysis_ids": existing.get("analysis_ids"),
                "related_post_ids": existing.get("related_post_ids"),
                "latest_analyzed_at": existing.get("latest_analyzed_at"),
                "latest_source_url": existing.get("latest_source_url"),
                "latest_content": existing.get("latest_content"),
            }
            grouped[key] = {**mention, **keep_counts}
    rows = list(grouped.values())
    rows.sort(
        key=lambda item: (
            -int(item.get("validation_score") or 0),
            -int(item.get("mention_count") or 0),
            str(item.get("handle") or ""),
            str(item.get("ticker") or ""),
        )
    )
    return rows[:80]


def _build_hot_mentions_24h(mentions: list[dict]) -> list[dict]:
    cutoff = app_now() - timedelta(hours=24)
    hot_rows: list[dict] = []
    for item in mentions:
        latest_at = parse_app_datetime(item.get("latest_analyzed_at") or item.get("analyzed_at"))
        if latest_at is None or latest_at < cutoff:
            continue
        mention_count = int(item.get("mention_count") or 0)
        validation_score = int(item.get("validation_score") or 0)
        boost = 0
        if item.get("in_portfolio"):
            boost += 30
        elif item.get("in_watchlist"):
            boost += 15
        if item.get("system_action") in {"加入观察", "重点验证", "Add to watch", "Validate"}:
            boost += 10
        hot_rows.append(
            {
                **item,
                "hot_score": mention_count * 20 + validation_score + boost,
            }
        )
    hot_rows.sort(
        key=lambda item: (
            -int(item.get("hot_score") or 0),
            -int(item.get("mention_count") or 0),
            -int(item.get("validation_score") or 0),
            str(item.get("ticker") or ""),
        )
    )
    return hot_rows[:10]


def _build_resonance_24h(mentions: list[dict]) -> list[dict]:
    cutoff = app_now() - timedelta(hours=24)
    grouped: dict[str, dict] = {}
    for item in mentions:
        latest_at = parse_app_datetime(item.get("latest_analyzed_at") or item.get("analyzed_at"))
        if latest_at is None or latest_at < cutoff:
            continue
        ticker = str(item.get("ticker") or "").upper()
        if not ticker:
            continue
        row = grouped.setdefault(
            ticker,
            {
                **item,
                "unique_handles": set(),
                "mention_total": 0,
                "max_validation_score": int(item.get("validation_score") or 0),
                "latest_analyzed_at": item.get("latest_analyzed_at") or item.get("analyzed_at"),
            },
        )
        row["unique_handles"].add(str(item.get("handle") or "-"))
        row["mention_total"] += int(item.get("mention_count") or 0)
        row["max_validation_score"] = max(row["max_validation_score"], int(item.get("validation_score") or 0))
        if str(item.get("latest_analyzed_at") or item.get("analyzed_at") or "") >= str(row.get("latest_analyzed_at") or ""):
            row.update(
                {
                    "name": item.get("name") or row.get("name"),
                    "market": item.get("market") or row.get("market"),
                    "system_action": item.get("system_action") or row.get("system_action"),
                    "in_watchlist": item.get("in_watchlist") or row.get("in_watchlist"),
                    "in_portfolio": item.get("in_portfolio") or row.get("in_portfolio"),
                    "latest_source_url": item.get("latest_source_url") or item.get("source_url") or row.get("latest_source_url"),
                    "latest_content": item.get("latest_content") or item.get("content") or row.get("latest_content"),
                    "latest_analyzed_at": item.get("latest_analyzed_at") or item.get("analyzed_at") or row.get("latest_analyzed_at"),
                }
            )
    rows: list[dict] = []
    for row in grouped.values():
        handle_count = len(row.get("unique_handles") or [])
        if handle_count < 2:
            continue
        resonance_score = handle_count * 50 + int(row.get("mention_total") or 0) * 10 + int(row.get("max_validation_score") or 0)
        rows.append(
            {
                **row,
                "handle_count": handle_count,
                "handles_text": ", ".join(sorted(row.get("unique_handles") or [])),
                "resonance_score": resonance_score,
            }
        )
    rows.sort(
        key=lambda item: (
            -int(item.get("resonance_score") or 0),
            -int(item.get("handle_count") or 0),
            -int(item.get("mention_total") or 0),
            str(item.get("ticker") or ""),
        )
    )
    return rows[:10]


def _us_tickers_from_analyses(analyses: list[dict]) -> list[str]:
    tickers: set[str] = set()
    for analysis in analyses:
        for mention in analysis.get("mentions") or []:
            if str(mention.get("market") or "").upper() == "US":
                ticker = str(mention.get("ticker") or "").strip().upper()
                if ticker and ticker not in _SOCIAL_US_SYNC_BLACKLIST and not bool(mention.get("sync_suppressed")):
                    tickers.add(ticker)
    return sorted(tickers)


def _extract_symbol_mentions(db: Session, text: str) -> list[dict]:
    mentions: dict[str, dict] = {}
    upper_text = text.upper()
    lowered = text.lower()
    symbol_repo = SymbolRepository(db)
    for match in re.findall(r"\$[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z])?\b", upper_text):
        raw = _normalize_us_symbol_token(match.replace("$", ""))
        if _is_us_symbol_stopword(raw) or raw in _SOCIAL_US_SYNC_BLACKLIST:
            continue
        _remember_symbol_mention(
            mentions,
            symbol_repo.get_or_create_symbol(SymbolCreate(ticker=raw, name=raw, market="US", exchange=None)),
            match_type="cashtag",
            raw_match=match,
        )
    code_patterns = [
        r"\b(?:SH|SZ|BJ)?\d{6}\b",
        r"\b\d{6}\.(?:SH|SS|SZ|BJ)\b",
    ]
    for pattern in code_patterns:
        for match in re.findall(pattern, upper_text):
            raw = _normalize_us_symbol_token(match.replace("$", ""))
            candidates = _ticker_candidates(raw)
            for ticker in candidates:
                overview = symbol_repo.get_overview(ticker)
                if overview:
                    _remember_overview_mention(mentions, overview, match_type="ticker", raw_match=match)
                    break
    for match in re.findall(r"\b[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z])?\b", text):
        raw = _normalize_us_symbol_token(match.replace("$", ""))
        if _is_us_symbol_stopword(raw) or raw in _SOCIAL_US_SYNC_BLACKLIST:
            continue
        candidates = _ticker_candidates(raw)
        for ticker in candidates:
            overview = symbol_repo.get_overview(ticker)
            if overview:
                _remember_overview_mention(mentions, overview, match_type="ticker", raw_match=match)
                break
    for alias, (ticker, name) in _US_COMPANY_ALIASES.items():
        if _contains_alias(lowered, alias):
            symbol = symbol_repo.get_or_create_symbol(SymbolCreate(ticker=ticker, name=name, market="US", exchange=None))
            _remember_symbol_mention(mentions, symbol, match_type="alias", raw_match=alias)
    for symbol in symbol_repo.list_symbols():
        name = str(symbol.name or "").strip()
        if not name or len(name) < 2:
            continue
        if str(symbol.market or "").upper() == "US" and name.upper() == str(symbol.ticker or "").upper():
            continue
        if name.lower() in lowered:
            _remember_symbol_mention(mentions, symbol, match_type="name", raw_match=name, overwrite=False)
    return list(mentions.values())


def _remember_overview_mention(mentions: dict[str, dict], overview: dict, *, match_type: str, raw_match: str, overwrite: bool = True) -> None:
    ticker = str(overview.get("ticker") or "").strip().upper()
    if not ticker:
        return
    payload = {
        "ticker": ticker,
        "name": overview.get("name") or ticker,
        "market": overview.get("market"),
        "match_type": match_type,
        "raw_match": raw_match,
    }
    if overwrite:
        mentions[ticker] = payload
    else:
        mentions.setdefault(ticker, payload)


def _remember_symbol_mention(mentions: dict[str, dict], symbol, *, match_type: str, raw_match: str, overwrite: bool = True) -> None:
    ticker = str(symbol.ticker or "").strip().upper()
    if not ticker:
        return
    payload = {
        "ticker": ticker,
        "name": symbol.name or ticker,
        "market": symbol.market,
        "match_type": match_type,
        "raw_match": raw_match,
    }
    if overwrite:
        mentions[ticker] = payload
    else:
        mentions.setdefault(ticker, payload)


def _normalize_us_symbol_token(raw: str) -> str:
    return str(raw or "").strip().upper().replace("-", ".")


def _is_us_symbol_stopword(raw: str) -> bool:
    token = _normalize_us_symbol_token(raw)
    return token in _US_SYMBOL_STOPWORDS or not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?", token)


def _looks_like_standalone_us_ticker(raw: str, text: str) -> bool:
    token = _normalize_us_symbol_token(raw)
    if _is_us_symbol_stopword(token):
        return False
    if len(token.replace(".", "")) >= 3:
        return True
    window = _mention_window(text, {"raw_match": raw}, radius=90).lower()
    stock_context = [
        "stock",
        "stocks",
        "share",
        "shares",
        "ticker",
        "calls",
        "puts",
        "earnings",
        "breakout",
        "upside",
        "downside",
        "long",
        "short",
        "buy",
        "sell",
        "trim",
        "position",
        "watchlist",
        "股票",
        "美股",
        "买入",
        "卖出",
        "看多",
        "看空",
        "突破",
        "持仓",
        "自选",
    ]
    return any(keyword in window for keyword in stock_context)


def _should_keep_social_mention(mention: dict) -> bool:
    ticker = _normalize_us_symbol_token(str(mention.get("ticker") or ""))
    match_type = str(mention.get("match_type") or "").strip().lower()
    market = str(mention.get("market") or "").strip().upper()
    if not ticker:
        return False
    if bool(mention.get("sync_suppressed")):
        return False
    if ticker in _SOCIAL_US_SYNC_BLACKLIST:
        return False
    if match_type == "ticker_auto":
        return False
    if market == "US" and match_type in {"ticker_auto", "ticker"} and _is_us_symbol_stopword(ticker):
        return False
    if market == "US" and match_type == "ticker" and len(ticker.replace(".", "")) <= 1:
        return False
    return True


def _contains_alias(lowered_text: str, alias: str) -> bool:
    escaped = re.escape(alias.lower())
    if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", lowered_text):
        return True
    return False


def _ticker_candidates(raw: str) -> list[str]:
    raw = raw.strip().upper()
    if not raw:
        return []
    if re.fullmatch(r"\d{6}\.(SH|SS|SZ|BJ)", raw):
        if raw.endswith(".SH"):
            return [raw[:-3] + ".SS", raw]
        return [raw]
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", raw):
        code = raw[2:]
        suffix = ".SS" if raw.startswith("SH") else ".SZ" if raw.startswith("SZ") else ".BJ"
        return [code + suffix]
    if re.fullmatch(r"\d{6}", raw):
        prefix = raw[:3]
        if prefix in {"600", "601", "603", "605", "688", "689"}:
            return [raw + ".SS", raw + ".SH", raw]
        if prefix in {"000", "001", "002", "003", "300", "301"}:
            return [raw + ".SZ", raw]
        if prefix in {"430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "920"}:
            return [raw + ".BJ", raw]
    return [raw]


def _infer_social_view(text: str, mention: dict) -> str:
    window = _mention_window(text, mention)
    bullish = ["看多", "买入", "推荐", "突破", "强势", "long", "buy", "bull", "upside", "breakout"]
    bearish = ["看空", "卖出", "回避", "风险", "short", "sell", "bear", "downside", "avoid"]
    lowered = window.lower()
    has_bullish = any(token in lowered for token in bullish)
    has_bearish = any(token in lowered for token in bearish)
    if has_bullish and has_bearish:
        return "冲突/需复核"
    if has_bearish:
        return "看空/风险"
    if has_bullish:
        return "看多/推荐"
    return "提及/观察"


def _validate_social_mention(*, social_view: str, latest_signal: dict, in_watchlist: bool, in_portfolio: bool) -> dict:
    score = 0
    reasons: list[str] = []
    action = "观察"
    if social_view == "看多/推荐":
        score += 20
        reasons.append("社交观点偏多")
    elif social_view == "看空/风险":
        score -= 10
        reasons.append("社交观点提示风险")
    elif social_view == "冲突/需复核":
        score += 5
        reasons.append("同帖多空线索冲突")
    else:
        reasons.append("仅被提及，方向不明确")
    model_score = latest_signal.get("score")
    if model_score is not None:
        value = float(model_score)
        if value >= 0.05:
            score += 35
            reasons.append("模型分数正向")
        elif value <= -0.03:
            score -= 25
            reasons.append("模型分数偏弱")
    signal_strength = latest_signal.get("signal_strength")
    if signal_strength is not None and float(signal_strength) >= 50:
        score += 20
        reasons.append("信号强度较高")
    if latest_signal.get("entry_trigger"):
        score += 10
        reasons.append("有可验证触发条件")
    if latest_signal.get("invalidation_condition"):
        score += 10
        reasons.append("有失效条件")
    if in_portfolio:
        action = "复核持仓"
        reasons.append("已在持仓库")
    elif in_watchlist:
        action = "更新观察"
        reasons.append("已在自选股")
    elif score >= 55:
        action = "加入观察"
    elif score >= 35:
        action = "重点验证"
    elif score <= 0:
        action = "暂不采纳"
    return {
        "validation_score": max(0, min(100, score)),
        "validation_reasons": reasons[:4],
        "system_action": action,
    }


def _load_json_list(db: Session, key: str) -> list[dict]:
    raw = AppSettingRepository(db).get(key)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _load_json_object(db: Session, key: str) -> dict:
    raw = AppSettingRepository(db).get(key)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_social_poll_state(db: Session, state: dict, result: dict) -> None:
    payload = dict(state or {})
    payload.update(
        {
            "last_run_at": _now_iso(),
            "last_status": result.get("status"),
            "last_message": result.get("message"),
            "last_new_posts": int(result.get("new_posts") or 0),
            "last_new_mentions": int(result.get("new_mentions") or 0),
        }
    )
    AppSettingRepository(db).set(SOCIAL_POLL_STATE_KEY, json.dumps(payload, ensure_ascii=False))


def _split_post_batch(content: str) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    chunks = [_clean_post_content(chunk) for chunk in re.split(r"\n\s*(?:---+|====+)\s*\n", text) if chunk.strip()]
    if len(chunks) > 1:
        return [chunk for chunk in chunks if chunk]
    return [_clean_post_content(text)]


def _clean_post_content(content: str) -> str:
    lines: list[str] = []
    for line in str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(("http://", "https://")) and ("x.com/" in stripped.lower() or "twitter.com/" in stripped.lower()):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def _mention_window(text: str, mention: dict, radius: int = 120) -> str:
    candidates = [
        str(mention.get("raw_match") or ""),
        str(mention.get("ticker") or ""),
        str(mention.get("name") or ""),
    ]
    lowered = text.lower()
    positions = [lowered.find(candidate.lower()) for candidate in candidates if candidate]
    positions = [index for index in positions if index >= 0]
    if not positions:
        return text
    center = min(positions)
    start = max(0, center - radius)
    end = min(len(text), center + radius)
    return text[start:end]


def _normalize_handle(handle: str) -> str:
    value = str(handle or "").strip()
    if not value:
        return ""
    value = value.split("/")[-1] if "x.com/" in value or "twitter.com/" in value else value
    value = value.lstrip("@").strip()
    return f"@{value}" if value else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()
