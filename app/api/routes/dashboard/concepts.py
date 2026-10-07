"""Concept detail, continuous-leaders and summary/data-sources routes."""

import html

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request

from fastapi.responses import HTMLResponse, RedirectResponse

from sqlalchemy.orm import Session

from app.api.presentation.i18n import t
from app.api.presentation.dashboard_legacy import render_dashboard_legacy_page
from app.api.presentation.dashboard_continuous_leaders import render_continuous_leaders_page

from app.api.presentation.dashboard_data_sources import (
    provider_strategy_view as _provider_strategy_view,  # noqa: F401 - compatibility export
    render_data_sources_page,
)

from app.api.presentation.styles_dashboard import (
    SIGNAL_KEY_STYLE,
)

from app.services.execution_tag_filters import (
    matches_execution_tag_filter as _matches_execution_tag_filter,
    excludes_execution_tag_filter as _excludes_execution_tag_filter,
)

from app.core.db import get_db_session

from app.models.schema import SymbolCreate

from app.services.auth import is_authenticated, login_redirect
from app.services.continuous_leaders import build_continuous_leader_view

from app.services.market_sync import sync_market_data

from app.services.model_signal_summary import build_model_state, build_signal_label, enrich_model_output

from app.services.repository import (
    PredictionRepository,
    SymbolRepository,
    WatchlistRepository,
)

from app.services.symbol_details import SymbolDataService

from app.services.ui_lang import resolve_request_lang

from app.services.workspace_nav import render_workspace_nav_html

from app.services.workspace_snapshots import (
    SNAPSHOT_CONTINUOUS_LEADERS,
    SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
    load_latest_workspace_snapshot,
)


from app.api.routes.dashboard._common import _clamp_lookback_runs, _concept_slug, _concept_ticker_watch_state, _concept_tr, _dashboard_model_badge, _dt, _load_home_summary, _load_summary, _lookback_pills, _percent_chip, _score_sparkline_svg, _signal_pill, _sparkline_svg
router = APIRouter(prefix="/dashboard", tags=["dashboard"])



def _mini_signal_direction(score: float | None) -> tuple[str, str] | None:
    if score is None:
        return None
    if score >= 0.18:
        return ("B", "#15803d")
    if score <= -0.05:
        return ("S", "#b91c1c")
    if score >= 0.05:
        return ("W", "#a16207")
    return None



def _price_signal_sparkline_svg(history_rows: list[dict], prediction_history: list[dict]) -> str:
    closes = [float(row["close"]) for row in history_rows if row.get("close") is not None]
    if not closes:
        return "<span class='muted'>-</span>"
    width = 150
    height = 56
    left_pad = 6
    right_pad = 6
    top_pad = 8
    bottom_pad = 8
    min_value = min(closes)
    max_value = max(closes)
    span = max(max_value - min_value, 0.000001)
    step = (width - left_pad - right_pad) / max(len(closes) - 1, 1)
    points = []
    point_meta = []
    for index, row in enumerate(history_rows):
        close_value = row.get("close")
        if close_value is None:
            continue
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((float(close_value) - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
        point_meta.append((row.get("date"), x, y, row))
    stroke = "#0f766e" if closes[-1] >= closes[0] else "#b91c1c"
    signal_map = {row["trade_date"]: row for row in prediction_history if row.get("trade_date")}
    markers: list[str] = []
    hover_targets: list[str] = []
    for date_value, x, y, row in point_meta:
        signal = signal_map.get(date_value)
        marker = _mini_signal_direction(signal.get("score") if signal else None)
        signal_text = ""
        if signal:
            label, _ = marker if marker else ("", "")
            score_value = signal.get("score")
            signal_text = f" | {label} {float(score_value):.3f}" if score_value is not None and label else ""
        hover_targets.append(
            f"<circle cx='{x:.2f}' cy='{y:.2f}' r='7' fill='transparent'>"
            f"<title>{date_value} | Close {float(row['close']):.2f}{signal_text}</title>"
            "</circle>"
        )
        if not marker:
            continue
        label, color = marker
        marker_y = max(12.0, y - 10.0)
        markers.append(f"<circle cx='{x:.2f}' cy='{marker_y:.2f}' r='6' fill='{color}' opacity='0.95'></circle>")
        markers.append(f"<text x='{x:.2f}' y='{marker_y + 3:.2f}' text-anchor='middle' font-size='7.5' font-weight='800' fill='#fff'>{label}</text>")
    return (
        f"<svg viewBox='0 0 {width} {height}' width='150' height='56' aria-label='price signal sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='10' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        f"{''.join(markers)}"
        f"{''.join(hover_targets)}"
        "</svg>"
    )



def _get_concept_from_summary(summary: dict, concept_slug: str) -> dict | None:
    return next(
        (item for item in (summary.get("market_context") or {}).get("concept_tracker", []) if item.get("slug") == concept_slug),
        None,
    )



def _first_not_none(*values):
    return next((value for value in values if value is not None), None)



def _enrich_heatmap_ticker_details(db: Session, ticker_details: list[dict], *, lang: str) -> list[dict]:
    tickers = [str(detail.get("ticker") or "").strip().upper() for detail in ticker_details if detail.get("ticker")]
    if not tickers:
        return []
    needs_lookup = [
        ticker
        for ticker, detail in zip(tickers, ticker_details)
        if detail.get("name") is None or detail.get("score") is None
    ]
    overviews = SymbolRepository(db).list_overviews_for_tickers(needs_lookup) if needs_lookup else {}
    latest_outputs = PredictionRepository(db).get_latest_model_outputs_for_tickers(needs_lookup) if needs_lookup else {}
    enriched_rows: list[dict] = []
    for raw_detail in ticker_details:
        ticker = str(raw_detail.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        latest = latest_outputs.get(ticker) or {}
        overview = overviews.get(ticker) or {}
        score = raw_detail.get("score")
        if score is None:
            score = latest.get("score")
        if score is None:
            score = raw_detail.get("trend_score")
        if score is None:
            score = 0.0
        enriched = enrich_model_output({**latest, "ticker": ticker, "score": score}, lang="en") if latest else {}
        signal_label = (
            raw_detail.get("signal_label")
            or latest.get("signal_label")
            or enriched.get("signal_label")
        )
        signal_strength = (
            raw_detail.get("signal_strength")
            if raw_detail.get("signal_strength") is not None
            else latest.get("signal_strength")
        )
        enriched_rows.append(
            {
                "ticker": ticker,
                "name": raw_detail.get("name") or overview.get("name") or latest.get("name") or ticker,
                "score": float(score or 0.0),
                "state": raw_detail.get("state") or enriched.get("state") or build_model_state(float(score or 0.0), lang="en"),
                "confidence": _first_not_none(raw_detail.get("confidence"), latest.get("confidence"), enriched.get("confidence")),
                "percentile": _first_not_none(raw_detail.get("percentile"), latest.get("percentile"), enriched.get("percentile")),
                "target_horizon_days": _first_not_none(raw_detail.get("target_horizon_days"), latest.get("target_horizon_days"), enriched.get("target_horizon_days")),
                "model_reward_risk_ratio": _first_not_none(raw_detail.get("model_reward_risk_ratio"), latest.get("model_reward_risk_ratio"), enriched.get("model_reward_risk_ratio")),
                "conviction_bucket": raw_detail.get("conviction_bucket") or latest.get("conviction_bucket") or enriched.get("conviction_bucket"),
                "position_size_hint": raw_detail.get("position_size_hint") or latest.get("position_size_hint") or enriched.get("position_size_hint"),
                "entry_style": raw_detail.get("entry_style") or latest.get("entry_style") or enriched.get("entry_style"),
                "signal_label": signal_label,
                "signal_strength": int(signal_strength or 0),
                "execution_tags": raw_detail.get("execution_tags") or latest.get("execution_tags") or enriched.get("execution_tags") or [],
            }
        )
    return sorted(enriched_rows, key=lambda detail: float(detail.get("score") or 0.0), reverse=True)



def _get_heatmap_concept_from_snapshot(db: Session, concept_slug: str, *, lang: str) -> dict | None:
    snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    payload = (snapshot or {}).get("payload") or {}
    heatmap_rows = payload.get("sector_heatmap") or []
    matched = next(
        (
            item
            for item in heatmap_rows
            if str(item.get("slug") or "") == concept_slug
            or _concept_slug(str(item.get("label") or "")) == concept_slug
            or str(item.get("label") or "") == concept_slug
        ),
        None,
    )
    if matched is None:
        return None
    ticker_details = _enrich_heatmap_ticker_details(db, matched.get("ticker_details") or [], lang=lang)
    tickers = [detail["ticker"] for detail in ticker_details]
    hits = int(matched.get("hits") or len(ticker_details) or 0)
    return {
        "concept_name": matched.get("label") or concept_slug,
        "concept_code": None,
        "slug": matched.get("slug") or concept_slug,
        "hits": hits,
        "previous_hits": 0,
        "delta_hits": 0,
        "streak": 1 if hits else 0,
        "history": [hits],
        "tickers": tickers,
        "ticker_details": ticker_details,
        "avg_score": float(matched.get("avg_score") or 0.0),
        "avg_move_5d": matched.get("avg_move_5d"),
        "avg_move_20d": matched.get("avg_move_20d"),
        "breadth_pct": matched.get("breadth_pct"),
        "buy_signal_count": int(matched.get("buy_signal_count") or 0),
        "max_signal_strength": int(matched.get("max_signal_strength") or 0),
        "execution_tags": matched.get("execution_tags") or [],
        "as_of_date": (snapshot or {}).get("created_at"),
        "source": "heatmap_snapshot",
    }



def _get_concept_for_detail(db: Session, summary: dict, concept_slug: str, *, lang: str) -> dict | None:
    concept = _get_concept_from_summary(summary, concept_slug)
    if concept is not None:
        return concept
    return _get_heatmap_concept_from_snapshot(db, concept_slug, lang=lang)



def _add_concept_tickers_to_watchlist(
    *,
    db: Session,
    concept: dict,
    auto_enable_sync: bool = False,
) -> tuple[int, int, int]:
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    added = 0
    already_in_watchlist = 0
    sync_enabled_count = 0

    for detail in concept["ticker_details"]:
        ticker = detail["ticker"]
        existing = watchlist_map.get(ticker)
        if existing:
            already_in_watchlist += 1
            if auto_enable_sync and not existing.get("sync_enabled"):
                updated = watchlist_repo.set_sync_enabled(existing["item_id"], True)
                if updated is not None:
                    sync_enabled_count += 1
                    existing["sync_enabled"] = 1
            continue

        existing_symbol = symbol_repo.get_by_ticker(ticker)
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=detail.get("name"),
                market=existing_symbol.market if existing_symbol else None,
                exchange=existing_symbol.exchange if existing_symbol else None,
            )
        )
        watchlist_item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        if auto_enable_sync:
            watchlist_repo.set_sync_enabled(watchlist_item.id, True)
            sync_enabled_count += 1
        watchlist_map[ticker] = {
            "item_id": watchlist_item.id,
            "symbol_id": symbol.id,
            "ticker": ticker,
            "name": detail.get("name"),
            "market": symbol.market,
            "sync_enabled": 1 if auto_enable_sync else 0,
        }
        added += 1

    return added, already_in_watchlist, sync_enabled_count



def _add_specific_tickers_to_watchlist(
    *,
    db: Session,
    tickers: list[str],
    auto_enable_sync: bool = False,
) -> tuple[int, int, int]:
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    added = 0
    already_in_watchlist = 0
    sync_enabled_count = 0

    for ticker in tickers:
        existing = watchlist_map.get(ticker)
        if existing:
            already_in_watchlist += 1
            if auto_enable_sync and not existing.get("sync_enabled"):
                updated = watchlist_repo.set_sync_enabled(existing["item_id"], True)
                if updated is not None:
                    sync_enabled_count += 1
                    existing["sync_enabled"] = 1
            continue

        existing_symbol = symbol_repo.get_by_ticker(ticker)
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=existing_symbol.name if existing_symbol else ticker,
                market=existing_symbol.market if existing_symbol else None,
                exchange=existing_symbol.exchange if existing_symbol else None,
            )
        )
        watchlist_item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        if auto_enable_sync:
            watchlist_repo.set_sync_enabled(watchlist_item.id, True)
            sync_enabled_count += 1
        watchlist_map[ticker] = {
            "item_id": watchlist_item.id,
            "symbol_id": symbol.id,
            "ticker": ticker,
            "name": symbol.name,
            "market": symbol.market,
            "sync_enabled": 1 if auto_enable_sync else 0,
        }
        added += 1

    return added, already_in_watchlist, sync_enabled_count



def _concept_sort_rank(sort_by: str, detail: dict) -> tuple:
    if sort_by == "ticker":
        return (detail["ticker"],)
    if sort_by == "score":
        return (-float(detail.get("score") or 0.0), detail["ticker"])
    if sort_by == "name":
        return ((detail.get("name") or detail["ticker"]).lower(), detail["ticker"])
    if sort_by == "five_day":
        return (-float(detail.get("five_day_move") or -9999.0), detail["ticker"])
    if sort_by == "watchlist":
        return (-int(detail.get("watch_state_rank") or 0), detail["ticker"])
    if sort_by == "last_sync":
        return (detail.get("last_synced_date") or "", detail["ticker"])
    return (-float(detail.get("score") or 0.0), detail["ticker"])



def _concept_sort_link(concept_slug: str, current_sort_by: str, current_sort_order: str, column: str, lang: str, comparison_sort: str) -> str:
    next_order = "asc" if current_sort_by == column and current_sort_order == "desc" else "desc"
    query = urlencode({"sort_by": column, "sort_order": next_order, "lang": lang, "comparison_sort": comparison_sort})
    return f"/dashboard/concepts/{concept_slug}?{query}"



def _comparison_sort_rank(mode: str, detail: dict) -> tuple:
    if mode == "momentum_20d":
        return (-float(detail.get("twenty_day_move") or -9999.0), -float(detail.get("score") or 0.0), detail["ticker"])
    if mode == "watchlist_ready":
        return (-int(detail.get("watch_state_rank") or 0), -float(detail.get("score") or 0.0), detail["ticker"])
    return (-float(detail.get("score") or 0.0), -float(detail.get("twenty_day_move") or -9999.0), detail["ticker"])



@router.get("/summary")
def dashboard_summary(request: Request, lookback_runs: int = 5, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return _load_summary(db, lookback_runs=_clamp_lookback_runs(lookback_runs))



@router.get("/data-sources", response_class=HTMLResponse)
def dashboard_data_sources(request: Request, lang: str = "en", db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard/data-sources")
    lang = resolve_request_lang(request)
    lookback_runs = _clamp_lookback_runs(request.query_params.get("lookback_runs", 5))
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    return render_data_sources_page(
        summary=summary,
        lang=lang,
        lookback_runs=lookback_runs,
        nav_html=render_workspace_nav_html(lang=lang, active_key="data", lookback_runs=lookback_runs),
    )


@router.get("/concepts/{concept_slug}", response_class=HTMLResponse)
def dashboard_concept_detail(
    request: Request,
    concept_slug: str,
    message: str | None = None,
    lang: str = "en",
    sort_by: str = "score",
    sort_order: str = "desc",
    comparison_sort: str = "model_score",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    lookback_runs: int = 5,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    # Concept detail needs the full market context (concept_tracker). The home
    # summary deliberately uses the lightweight loader with an empty tracker,
    # so reusing it here made every concept page 404 whenever the full summary
    # was not already cached by a preceding POST. The POST handlers on this page
    # already use _load_summary; keep the GET consistent with them.
    summary = _load_summary(db, lookback_runs=lookback_runs)
    concept = _get_concept_for_detail(db, summary, concept_slug, lang=lang)
    if concept is None:
        return HTMLResponse("<h1>Concept not found</h1>", status_code=404)

    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    symbol_data_service = SymbolDataService()
    prediction_repo = PredictionRepository(db)
    ticker_csv = ",".join(detail["ticker"] for detail in concept["ticker_details"])
    banner_html = (
        f"<div class='banner'>{html.escape(message)}</div>"
        if message
        else ""
    )
    ticker_detail_rows: list[dict] = []
    compute_price_moves = concept.get("source") != "heatmap_snapshot" or len(concept["ticker_details"]) <= 60
    for detail in concept["ticker_details"]:
        state_label, state_bg, state_fg = _concept_ticker_watch_state(watchlist_map, detail["ticker"], lang)
        existing = watchlist_map.get(detail["ticker"])
        five_day_move = None
        twenty_day_move = None
        if compute_price_moves:
            history = symbol_data_service.get_history(detail["ticker"], limit=21)
            if len(history) >= 6:
                start_close = history[-6].get("close")
                end_close = history[-1].get("close")
                if start_close not in (None, 0) and end_close is not None:
                    five_day_move = ((float(end_close) / float(start_close)) - 1) * 100
            if len(history) >= 20:
                start_close = history[-20].get("close")
                end_close = history[-1].get("close")
                if start_close not in (None, 0) and end_close is not None:
                    twenty_day_move = ((float(end_close) / float(start_close)) - 1) * 100
        ticker_detail_rows.append(
            {
                **detail,
                "display_signal_label": build_signal_label(detail.get("score"), lang=lang) or ("Hold" if lang == "en" else "持有"),
                "watch_state_label": state_label,
                "watch_state_bg": state_bg,
                "watch_state_fg": state_fg,
                "watch_state_rank": 0 if existing is None else (3 if existing.get("sync_enabled") and existing.get("sync_status") == "success" else 2 if existing.get("sync_enabled") else 1),
                "last_synced_date": existing.get("last_synced_date") if existing else None,
                "existing": existing,
                "five_day_move": five_day_move,
                "twenty_day_move": twenty_day_move,
            }
        )

    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    if signal_filter != "ALL":
        def _signal_key(detail: dict) -> str:
            label = build_signal_label(detail.get("score"), lang="en") or "Hold"
            return label.upper()
        ticker_detail_rows = [detail for detail in ticker_detail_rows if _signal_key(detail) == signal_filter]
    if min_signal_strength > 0:
        ticker_detail_rows = [
            detail for detail in ticker_detail_rows
            if int(detail.get("signal_strength") or 0) >= min_signal_strength
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        ticker_detail_rows = [
            detail for detail in ticker_detail_rows
            if _matches_execution_tag_filter(detail.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        ticker_detail_rows = [
            detail for detail in ticker_detail_rows
            if _excludes_execution_tag_filter(detail.get("execution_tags"), exclude_execution_tag_filter)
        ]

    ticker_detail_rows.sort(key=lambda item: _concept_sort_rank(sort_by, item))
    if sort_order == "desc":
        ticker_detail_rows.reverse()

    ticker_row_list: list[str] = []
    for detail in ticker_detail_rows:
        existing = detail["existing"]
        single_action_button = ""
        if existing is None:
            single_action_button = (
                f"<form action='/dashboard/concepts/{concept_slug}/ticker-action' method='post' style='display:inline-block;margin:0;'>"
                f"<input type='hidden' name='ticker' value='{detail['ticker']}' />"
                f"<input type='hidden' name='action' value='add' />"
                f"<input type='hidden' name='lang' value='{lang}' />"
                f"<button type='submit'>{_concept_tr(lang, 'add')}</button>"
                "</form>"
            )
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            single_action_button = (
                f"<a href='/insights/{detail['ticker']}?lang={lang}' class='action-link'>{_concept_tr(lang, 'open')}</a>"
            )
        else:
            single_action_button = (
                f"<form action='/dashboard/concepts/{concept_slug}/ticker-action' method='post' style='display:inline-block;margin:0;'>"
                f"<input type='hidden' name='ticker' value='{detail['ticker']}' />"
                f"<input type='hidden' name='action' value='sync' />"
                f"<input type='hidden' name='lang' value='{lang}' />"
                f"<button type='submit'>{_concept_tr(lang, 'sync')}</button>"
                "</form>"
            )
        ticker_row_list.append(
            "<tr>"
            f"<td><a href='/insights/{detail['ticker']}?lang={lang}'>{detail['ticker']}</a></td>"
            f"<td>{detail.get('name') or detail['ticker']}</td>"
            f"<td><div>{float(detail.get('score') or 0.0):.4f}</div><div style='margin-top:6px;'>{_dashboard_model_badge(detail.get('state'), confidence=detail.get('confidence'), compact=True)}</div><div style='margin-top:6px;'>{_signal_pill(detail.get('score'), lang=lang, strength=int(detail.get('signal_strength') or 0), compact=True)}</div><div style='margin-top:6px;font-size:12px;color:#6b7280;'>{('Pct ' + format(float(detail.get('percentile')), '.1f') + '%') if detail.get('percentile') is not None else ''}{(' · ' if detail.get('percentile') is not None and detail.get('target_horizon_days') is not None else '')}{('H ' + str(int(detail.get('target_horizon_days'))) + 'd') if detail.get('target_horizon_days') is not None else ''}{(' · ' if (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None) and detail.get('model_reward_risk_ratio') is not None else '')}{('R/R ' + format(float(detail.get('model_reward_risk_ratio')), '.2f')) if detail.get('model_reward_risk_ratio') is not None else ''}{(' · ' if (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None) and detail.get('conviction_bucket') else '')}{detail.get('conviction_bucket') or ''}{(' · ' if detail.get('position_size_hint') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket')) else '')}{detail.get('position_size_hint') or ''}{(' · ' if detail.get('entry_style') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket') or detail.get('position_size_hint')) else '')}{detail.get('entry_style') or ''}{(' · ' if detail.get('execution_tags') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket') or detail.get('position_size_hint') or detail.get('entry_style')) else '')}{' / '.join((detail.get('execution_tags') or [])[:2])}</div></td>"
            f"<td>{_percent_chip(detail['five_day_move'])}</td>"
            f"<td><span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:{detail['watch_state_bg']};color:{detail['watch_state_fg']};font-size:12px;font-weight:800;white-space:nowrap;'>{detail['watch_state_label']}</span></td>"
            f"<td>{detail.get('last_synced_date') or '-'}</td>"
            f"<td style='white-space:nowrap;'>{single_action_button} <a href='/insights/{detail['ticker']}?lang={lang}' class='action-link' style='margin-left:8px;'>{_concept_tr(lang, 'insight')}</a></td>"
            "</tr>"
        )
    ticker_rows = "".join(ticker_row_list) or f"<tr><td colspan='7'>{_concept_tr(lang, 'no_tickers')}</td></tr>"
    sparkline = _sparkline_svg(concept["history"])
    sorted_ticker_csv = ",".join(detail["ticker"] for detail in ticker_detail_rows)
    comparison_rows = sorted(ticker_detail_rows, key=lambda item: _comparison_sort_rank(comparison_sort, item))[:3]
    comparison_cards = []
    for detail in comparison_rows:
        history = symbol_data_service.get_history(detail["ticker"], limit=20)
        closes = [float(row["close"]) for row in history if row.get("close") is not None]
        prediction_history = prediction_repo.list_symbol_predictions(detail["ticker"], limit=120, latest_run_only=True)
        latest_close = closes[-1] if closes else None
        latest_close_html = f"<span>Last {latest_close:.2f}</span>" if latest_close is not None else "<span>Last -</span>"
        twenty_day_html = (
            f"<span>20D {'+' if detail['twenty_day_move'] > 0 else ''}{detail['twenty_day_move']:.1f}%</span>"
            if detail['twenty_day_move'] is not None
            else "<span>20D -</span>"
        )
        comparison_tag_html = "".join(
            "<span style='background:#fff7ed;color:#c2410c;padding:4px 8px;border-radius:999px;border:1px solid #fed7aa;'>"
            f"{tag}"
            "</span>"
            for tag in (detail.get("execution_tags") or [])[:2]
        )
        comparison_cards.append(
            "<article class='mini-card'>"
            f"<div class='mini-top'><a href='/insights/{detail['ticker']}?lang={lang}'>{detail['ticker']}</a><span class='mini-score'>{_concept_tr(lang, 'model_score').lower()} {detail['score']:.3f}</span></div>"
            f"<div class='mini-name'>{detail.get('name') or detail['ticker']}</div>"
            f"<div style='margin-bottom:10px;'>{_dashboard_model_badge(detail.get('state'), confidence=detail.get('confidence'), compact=True)}</div>"
            f"<div style='margin-bottom:8px;'>{_signal_pill(detail.get('score'), lang=lang, strength=int(detail.get('signal_strength') or 0), compact=True)}</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;color:#6b7280;font-size:12px;font-weight:700;'><span>{('Pct ' + format(float(detail.get('percentile')), '.1f') + '%') if detail.get('percentile') is not None else 'Pct -'}</span><span>{('H ' + str(int(detail.get('target_horizon_days'))) + 'd') if detail.get('target_horizon_days') is not None else 'H -'}</span><span>{('R/R ' + format(float(detail.get('model_reward_risk_ratio')), '.2f')) if detail.get('model_reward_risk_ratio') is not None else 'R/R -'}</span><span>{detail.get('conviction_bucket') or ('Conviction -' if lang == 'en' else '信念 -')}</span><span>{detail.get('position_size_hint') or ('Sizing -' if lang == 'en' else '仓位 -')}</span><span>{detail.get('entry_style') or ('Entry -' if lang == 'en' else '进场 -')}</span>{comparison_tag_html}</div>"
            f"{_price_signal_sparkline_svg(history, prediction_history)}"
            "<div class='mini-metrics'>"
            f"{latest_close_html}"
            f"{twenty_day_html}"
            + "</div></article>"
        )
    comparison_html = "".join(comparison_cards) or f"<div class='muted'>{_concept_tr(lang, 'not_enough_price_history')}</div>"
    comparison_tabs = "".join(
        (
            f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': mode, 'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' "
            f"class='compare-pill{' active' if comparison_sort == mode else ''}'>{label}</a>"
        )
        for mode, label in (
            ("model_score", _concept_tr(lang, "top_by_model")),
            ("momentum_20d", _concept_tr(lang, "top_by_20d")),
            ("watchlist_ready", _concept_tr(lang, "ready_first")),
        )
    )
    lang_switch = (
        f"<div style='display:flex;gap:8px;align-items:center;margin-top:12px;'>"
        f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'lang': 'en', 'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': comparison_sort, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if lang != 'zh' else ''}'>{_concept_tr('en', 'lang_en')}</a>"
        f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'lang': 'zh', 'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': comparison_sort, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{t(lang, ' active', '')}'>{_concept_tr('zh', 'lang_zh')}</a>"
        "</div>"
    )
    lookback_pills = _lookback_pills(
        f"/dashboard/concepts/{concept_slug}",
        selected=lookback_runs,
        extra_params={"lang": lang, "sort_by": sort_by, "sort_order": sort_order, "comparison_sort": comparison_sort, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter},
    )
    signal_pills = "".join(
        f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'lang': lang, 'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': comparison_sort, 'lookback_runs': lookback_runs, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    avg_move_5d_display = "-"
    if concept.get("avg_move_5d") is not None:
        avg_move_5d_display = f"{'+' if concept.get('avg_move_5d', 0) > 0 else ''}{float(concept['avg_move_5d']):.1f}%"
    avg_move_20d_display = "-"
    if concept.get("avg_move_20d") is not None:
        avg_move_20d_display = f"{'+' if concept.get('avg_move_20d', 0) > 0 else ''}{float(concept['avg_move_20d']):.1f}%"
    breadth_display = f"{float(concept['breadth_pct']):.0f}%" if concept.get("breadth_pct") is not None else "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    return render_dashboard_legacy_page(
        "dashboard/legacy/concepts_dashboard_concept_detail.html",
        fragments=[
            f'{lang}',
            f"{concept['concept_name']}",
            f'{SIGNAL_KEY_STYLE}',
            f"{t(lang, '概念详情', 'Concept Detail')}",
            f"{t(lang, '从板块热力图进入后，查看命中股票、模型信号和加入自选动作。', 'Drill from market heat into member names, model signals, and watchlist actions.')}",
            f'{nav_html}',
            f"{t(lang, '这页回答“这个概念里哪些股票被模型命中，以及哪些值得加入自选”。', 'This page answers which names inside a concept were hit by the model and which deserve watchlist attention.')}",
            f'{banner_html}',
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '返回市场概况', 'Back to Market')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '板块热力图', 'Sector Heatmap')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '概念追踪', 'Concept Tracker')}",
            f'{lang_switch}',
            f"{_concept_tr(lang, 'concept_detail')}",
            f"{concept['concept_name']}",
            f"{_concept_tr(lang, 'detail_subtitle')}",
            f"{_concept_tr(lang, 'hits')}",
            f"{concept['hits']}",
            f"{_concept_tr(lang, 'hits_help')}",
            f"{_concept_tr(lang, 'delta')}",
            f"{('+' if concept['delta_hits'] > 0 else '')}",
            f"{concept['delta_hits']}",
            f"{_concept_tr(lang, 'delta_help')}",
            f"{_concept_tr(lang, 'streak')}",
            f"{concept['streak']}",
            f"{_concept_tr(lang, 'streak_help')}",
            f"{_concept_tr(lang, 'trend')}",
            f'{sparkline}',
            f"{_concept_tr(lang, 'trend_help')}",
            f"{_concept_tr(lang, 'concept_strength')}",
            f"{_concept_tr(lang, 'concept_strength_subtitle')}",
            f"{_concept_tr(lang, 'five_day')}",
            f'{avg_move_5d_display}',
            f"{_concept_tr(lang, 'twenty_day')}",
            f'{avg_move_20d_display}',
            f"{_concept_tr(lang, 'breadth')}",
            f'{breadth_display}',
            f"{_concept_tr(lang, 'breadth_help')}",
            f"{_concept_tr(lang, 'buy_signal_count')}",
            f"{int(concept.get('buy_signal_count') or 0)}",
            f"{_concept_tr(lang, 'buy_signal_count_help')}",
            f"{_concept_tr(lang, 'max_signal_strength')}",
            f"{int(concept.get('max_signal_strength') or 0)}",
            f"{_concept_tr(lang, 'max_signal_strength_help')}",
            f'{lookback_pills}',
            f'{signal_pills}',
            f'{concept_slug}',
            f'{lang}',
            f'{sort_by}',
            f'{sort_order}',
            f'{comparison_sort}',
            f'{lookback_runs}',
            f'{signal_filter}',
            f"{('Execution Tag' if lang == 'en' else '执行提醒标签')}",
            f"{(execution_tag_filter if execution_tag_filter.upper() != 'ALL' else '')}",
            f"{('Exclude Tag' if lang == 'en' else '排除标签')}",
            f"{(exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else '')}",
            f"{('Quick Tags' if lang == 'en' else '快捷标签')}",
            f'{concept_slug}',
            f'{concept_slug}',
            f'{concept_slug}',
            f'{concept_slug}',
            f"{('exclude gap-risk' if lang == 'en' else '排除 gap-risk')}",
            f'{concept_slug}',
            f"{('Clear Tags' if lang == 'en' else '清空标签')}",
            f"{('Min Buy Count' if lang == 'en' else '最少买点数')}",
            f'{min_buy_signal_count}',
            f"{('Min Strength' if lang == 'en' else '最低强度')}",
            f'{min_signal_strength}',
            f"{_concept_tr(lang, 'apply_filters')}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'follow_this_concept')}",
            f'{concept_slug}',
            f'{ticker_csv}',
            f'{lang}',
            f"{_concept_tr(lang, 'auto_enable_sync')}",
            f"{_concept_tr(lang, 'sync_now')}",
            f"{_concept_tr(lang, 'add_concept_stocks')}",
            f"{_concept_tr(lang, 'top_n_watch')}",
            f'{concept_slug}',
            f'{sorted_ticker_csv}',
            f'{lang}',
            f"{_concept_tr(lang, 'top_n_help')}",
            f'{max(len(ticker_detail_rows), 1)}',
            f'{min(max(len(ticker_detail_rows), 1), 3)}',
            f"{_concept_tr(lang, 'auto_enable_sync')}",
            f"{_concept_tr(lang, 'sync_selected_top_n')}",
            f"{_concept_tr(lang, 'add_top_n')}",
            f"{_concept_tr(lang, 'top_movers_comparison')}",
            f'{comparison_tabs}',
            f'{comparison_html}',
            f"{_concept_tr(lang, 'ticker_breakdown')}",
            f"{_concept_sort_link(concept_slug, sort_by, sort_order, 'ticker', lang, comparison_sort)}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'ticker')}",
            f"{(' ↓' if sort_by == 'ticker' and sort_order == 'desc' else ' ↑' if sort_by == 'ticker' else '')}",
            f"{_concept_sort_link(concept_slug, sort_by, sort_order, 'name', lang, comparison_sort)}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'name')}",
            f"{(' ↓' if sort_by == 'name' and sort_order == 'desc' else ' ↑' if sort_by == 'name' else '')}",
            f"{_concept_sort_link(concept_slug, sort_by, sort_order, 'score', lang, comparison_sort)}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'model_score')}",
            f"{(' ↓' if sort_by == 'score' and sort_order == 'desc' else ' ↑' if sort_by == 'score' else '')}",
            f"{_concept_sort_link(concept_slug, sort_by, sort_order, 'five_day', lang, comparison_sort)}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'five_day')}",
            f"{(' ↓' if sort_by == 'five_day' and sort_order == 'desc' else ' ↑' if sort_by == 'five_day' else '')}",
            f"{_concept_sort_link(concept_slug, sort_by, sort_order, 'watchlist', lang, comparison_sort)}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'watchlist')}",
            f"{(' ↓' if sort_by == 'watchlist' and sort_order == 'desc' else ' ↑' if sort_by == 'watchlist' else '')}",
            f"{_concept_sort_link(concept_slug, sort_by, sort_order, 'last_sync', lang, comparison_sort)}",
            f'{lookback_runs}',
            f"{_concept_tr(lang, 'last_sync')}",
            f"{(' ↓' if sort_by == 'last_sync' and sort_order == 'desc' else ' ↑' if sort_by == 'last_sync' else '')}",
            f"{_concept_tr(lang, 'actions')}",
            f'{ticker_rows}',
        ],
    )



@router.post("/concepts/{concept_slug}/watchlist")
def dashboard_concept_add_to_watchlist(
    request: Request,
    concept_slug: str,
    tickers_csv: str = Form(""),
    lang: str = Form("en"),
    auto_enable_sync: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")
    summary = _load_summary(db)
    concept = _get_concept_from_summary(summary, concept_slug)
    if concept is None:
        return RedirectResponse(url="/dashboard?job_status=failed&job_message=Concept+not+found", status_code=303)

    auto_sync_enabled = auto_enable_sync == "1"
    sync_now = sync_after_add == "1"
    added, already_in_watchlist, sync_enabled_count = _add_concept_tickers_to_watchlist(
        db=db,
        concept=concept,
        auto_enable_sync=auto_sync_enabled,
    )

    sync_message = ""
    if sync_now and tickers_csv.strip():
        tickers = [ticker.strip() for ticker in tickers_csv.split(",") if ticker.strip()]
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
        success_count = sum(1 for item in results if item.get("status") == "success")
        sync_message = f" · Synced {success_count}/{len(tickers)}"

    if added:
        message = f"Added {added} concept stock(s) to watchlist"
    elif already_in_watchlist:
        message = "All concept stocks are already in your watchlist"
    else:
        message = "No concept stocks available to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    message += sync_message
    return RedirectResponse(
        url=f"/dashboard/concepts/{concept_slug}?{urlencode({'message': message, 'lang': lang})}",
        status_code=303,
    )



@router.post("/concepts/{concept_slug}/watchlist-top")
def dashboard_concept_add_top_to_watchlist(
    request: Request,
    concept_slug: str,
    tickers_csv: str = Form(""),
    top_n: int = Form(3),
    lang: str = Form("en"),
    auto_enable_sync: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")

    tickers = [ticker.strip() for ticker in tickers_csv.split(",") if ticker.strip()]
    if top_n > 0:
        tickers = tickers[:top_n]
    auto_sync_enabled = auto_enable_sync == "1"
    sync_now = sync_after_add == "1"

    added, already_in_watchlist, sync_enabled_count = _add_specific_tickers_to_watchlist(
        db=db,
        tickers=tickers,
        auto_enable_sync=auto_sync_enabled,
    )
    sync_message = ""
    if sync_now and tickers:
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
        success_count = sum(1 for item in results if item.get("status") == "success")
        sync_message = f" · Synced {success_count}/{len(tickers)}"

    if added:
        message = f"Added top {len(tickers)} concept stock(s) to watchlist"
    elif already_in_watchlist:
        message = "Selected concept stocks are already in your watchlist"
    else:
        message = "No concept stocks available to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    message += sync_message
    return RedirectResponse(
        url=f"/dashboard/concepts/{concept_slug}?{urlencode({'message': message, 'lang': lang})}",
        status_code=303,
    )



@router.post("/concepts/{concept_slug}/ticker-action")
def dashboard_concept_ticker_action(
    request: Request,
    concept_slug: str,
    ticker: str = Form(...),
    action: str = Form(...),
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    existing = watchlist_map.get(ticker)
    symbol = symbol_repo.get_by_ticker(ticker)

    if existing is None:
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=symbol.name if symbol else ticker,
                market=symbol.market if symbol else None,
                exchange=symbol.exchange if symbol else None,
            )
        )
        item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        existing = {
            "item_id": item.id,
            "ticker": ticker,
            "sync_enabled": 0,
        }

    if action == "sync":
        watchlist_repo.set_sync_enabled(existing["item_id"], True)
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
        result = results[0] if results else None
        if result and result.get("status") == "success":
            message = f"Synced {ticker} with {result['rows']} rows"
        elif result:
            message = f"Sync failed for {ticker}: {result.get('message', 'Unknown error')}"
        else:
            message = f"Sync did not return a result for {ticker}"
    else:
        message = f"Added {ticker} to watchlist"

    return RedirectResponse(
        url=f"/dashboard/concepts/{concept_slug}?{urlencode({'message': message, 'lang': lang})}",
        status_code=303,
    )



@router.post("/continuous-leaders/action")
def dashboard_continuous_leader_action(
    request: Request,
    ticker: str = Form(...),
    action: str = Form(...),
    lang: str = Form("en"),
    lookback_runs: int = Form(5),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    existing = watchlist_map.get(ticker)
    symbol = symbol_repo.get_by_ticker(ticker)

    if existing is None:
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=symbol.name if symbol else ticker,
                market=symbol.market if symbol else None,
                exchange=symbol.exchange if symbol else None,
            )
        )
        item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        existing = {
            "item_id": item.id,
            "ticker": ticker,
            "sync_enabled": 0,
        }

    if action == "sync":
        watchlist_repo.set_sync_enabled(existing["item_id"], True)
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
        result = results[0] if results else None
        if result and result.get("status") == "success":
            message = f"Synced {ticker} with {result['rows']} rows"
        elif result:
            message = f"Sync failed for {ticker}: {result.get('message', 'Unknown error')}"
        else:
            message = f"Sync did not return a result for {ticker}"
    else:
        message = f"Added {ticker} to watchlist"

    return RedirectResponse(
        url=f"/dashboard?{urlencode({'lang': lang, 'job_message': message, 'lookback_runs': lookback_runs})}",
        status_code=303,
    )



@router.post("/continuous-leaders/watchlist-top")
def dashboard_continuous_leaders_add_top(
    request: Request,
    tickers_csv: str = Form(""),
    top_n: int = Form(3),
    lang: str = Form("en"),
    lookback_runs: int = Form(5),
    auto_enable_sync: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    tickers = [ticker.strip() for ticker in tickers_csv.split(",") if ticker.strip()]
    if top_n > 0:
        tickers = tickers[:top_n]
    auto_sync_enabled = auto_enable_sync == "1"
    sync_now = sync_after_add == "1"

    added, already_in_watchlist, sync_enabled_count = _add_specific_tickers_to_watchlist(
        db=db,
        tickers=tickers,
        auto_enable_sync=auto_sync_enabled,
    )
    sync_message = ""
    if sync_now and tickers:
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
        success_count = sum(1 for item in results if item.get("status") == "success")
        sync_message = f" · Synced {success_count}/{len(tickers)}"

    if added:
        message = f"Added top {len(tickers)} continuous leader(s) to watchlist"
    elif already_in_watchlist:
        message = "Selected continuous leaders are already in your watchlist"
    else:
        message = "No continuous leaders available to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    message += sync_message
    return RedirectResponse(
        url=f"/dashboard?{urlencode({'lang': lang, 'job_message': message, 'lookback_runs': lookback_runs})}",
        status_code=303,
    )



@router.get("/continuous-leaders", response_class=HTMLResponse)
def dashboard_continuous_leaders_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    continuous_sort_by: str = "hits",
    continuous_sort_order: str = "desc",
    continuous_market: str = "ALL",
    continuous_state: str = "ALL",
    continuous_signal: str = "ALL",
    min_signal_strength: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/continuous-leaders")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    continuous_market = continuous_market.upper()
    continuous_state = continuous_state.upper()
    continuous_signal = continuous_signal.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    continuous_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_CONTINUOUS_LEADERS)
    continuous_rows_snapshot = ((continuous_snapshot or {}).get("payload") or {}).get("rows") if isinstance(continuous_snapshot, dict) else None
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)

    leader_view = build_continuous_leader_view(
        list(continuous_rows_snapshot or summary["market_context"].get("continuous_leaders", [])),
        watchlist_map,
        market=continuous_market, state=continuous_state, signal=continuous_signal,
        min_signal_strength=min_signal_strength,
        include_tags=execution_tag_filter, exclude_tags=exclude_execution_tag_filter,
        sort_by=continuous_sort_by, sort_order=continuous_sort_order,
    )
    rows_source = leader_view["rows"]

    params = {
        "lang": lang, "lookback_runs": lookback_runs,
        "continuous_sort_by": continuous_sort_by, "continuous_sort_order": continuous_sort_order,
        "continuous_market": continuous_market, "continuous_state": continuous_state,
        "continuous_signal": continuous_signal, "min_signal_strength": min_signal_strength,
        "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter,
    }
    rows = [
        {**item,
         "badge_html": _dashboard_model_badge(item.get("state"), confidence=item.get("confidence"), compact=True),
         "trend_html": _score_sparkline_svg(item.get("score_history", [])),
         "watch_state": _concept_ticker_watch_state(watchlist_map, item["ticker"], lang)}
        for item in rows_source
    ]
    labels = {
        key: _concept_tr(lang, key) for key in (
            "continuous_detail", "continuous_leaders", "continuous_subtitle", "back_to_dashboard",
            "market_filter", "state_filter", "apply_filters", "auto_enable_sync", "sync_selected_top_n",
            "add_top_n", "ticker", "name", "hits", "model_score", "trend", "last", "watchlist", "actions",
            "add", "sync", "open",
        )
    } | {
        key: _dt(lang, key) for key in (
            "risk_overview", "tagged_names", "risk_examples", "common_risks", "no_execution_risks",
        )
    } | {"lang_en": _concept_tr("en", "lang_en"), "lang_zh": _concept_tr("zh", "lang_zh")}
    return render_continuous_leaders_page(
        params=params, view=leader_view, rows=rows, labels=labels,
        nav_html=render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs),
        lookback_html=_lookback_pills(
            "/dashboard/continuous-leaders", selected=lookback_runs,
            extra_params={key: value for key, value in params.items() if key != "lookback_runs"},
        ),
    )
