"""Dashboard insight summaries extracted from the route layer.

Business interpretation (recommendation validation, watchlist post-add
performance, model run performance, weekly review) plus the small pure
calculators they share. Routes orchestrate; this module computes.
"""

from app.api.presentation.i18n import t
from app.models.tables import DataJob, Prediction, PredictionDetail, Symbol
from app.services.ai_daily_report import (
    _report_text_with_security_names,
    _report_ticker_labels,
    _security_name_is_code,
    build_trade_explain_text,
    build_close_review_action_feed,
    format_risk_flags,
    format_trade_gate_reason,
    format_trade_status,
    list_ai_daily_report_history,
    load_ai_daily_report,
    load_ai_daily_report_history_item,
    render_ai_daily_report_message,
)
from app.services.market_lake import (
    get_latest_lake_trade_date,
    load_lake_price_history,
    load_lake_rows,
)
from app.services.model_selection_guidance import (
    ACTION_BUCKET_LABELS,
    load_model_selection_guidance_snapshot,
    summarize_model_selection_guidance,
)
from app.services.model_signal_summary import build_model_state, build_signal_label, enrich_model_output, model_confidence
from app.services.portfolio_book import (
    load_portfolio_positions,
    load_portfolio_trades,
    trade_reason_bucket,
    trade_reason_label,
)
from app.services.repository import (
    ConceptSnapshotRepository,
    DataJobRepository,
    DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
    FundamentalSnapshotRepository,
    ModelRunRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PointInTimeFeatureSnapshotRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WatchlistRepository,
    WorkspaceSnapshotRepository,
)
from app.services.runtime_cache import get_cached, get_or_set, set_cached
from app.services.template_evaluation import (
    aggregate_window_stats as _aggregate_window_stats,
    build_lightgbm_evaluation,
    build_lightgbm_prediction_evaluation,
    build_next_tesla_evaluation,
    build_technical_momentum_evaluation,
    lightgbm_bias,
    lightgbm_maturity,
    next_tesla_market_bias,
    next_tesla_maturity,
    resolve_template_group_label,
    technical_momentum_bias,
    technical_momentum_maturity,
)
from app.services.time_utils import app_today_iso, format_app_datetime, parse_app_datetime
from datetime import date, datetime, time, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
import json


def _forward_return_from_history(history: list[dict], *, trade_date: str, sessions: int) -> float | None:
    if not history or sessions <= 0:
        return None
    start_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") >= str(trade_date)), None)
    if start_index is None:
        return None
    end_index = start_index + sessions
    if end_index >= len(history):
        return None
    start_close = history[start_index].get("close")
    end_close = history[end_index].get("close")
    if start_close in (None, 0) or end_close is None:
        return None
    try:
        return round(((float(end_close) / float(start_close)) - 1.0) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _return_since_history_start(history: list[dict], *, trade_date: str) -> float | None:
    if not history:
        return None
    start_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") >= str(trade_date)), None)
    if start_index is None or start_index >= len(history):
        return None
    start_close = history[start_index].get("close")
    latest_close = history[-1].get("close")
    if start_close in (None, 0) or latest_close is None:
        return None
    try:
        return round(((float(latest_close) / float(start_close)) - 1.0) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _window_return_pct(history: list[dict], sessions: int) -> float | None:
    if len(history) < sessions + 1:
        return None
    start_close = history[-(sessions + 1)].get("close")
    end_close = history[-1].get("close")
    if start_close in (None, 0) or end_close is None:
        return None
    return round(((float(end_close) / float(start_close)) - 1.0) * 100.0, 2)


def _fmt_optional_float(value: object, *, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _display_time(value: str | None, *, with_tz: bool = False) -> str:
    return format_app_datetime(value, with_tz=with_tz)


def _report_market_rows(report: dict) -> list[dict]:
    payload = report or {}
    explicit_all = payload.get("market_candidates_all")
    if isinstance(explicit_all, list) and explicit_all:
        return list(explicit_all)
    actionable = list(payload.get("market_recommendations") or payload.get("rows") or [])
    watch = list(payload.get("market_watch_recommendations") or [])
    combined: list[dict] = []
    seen: set[str] = set()
    for item in actionable + watch:
        ticker = str(item.get("ticker") or "").strip().upper()
        key = ticker or str(id(item))
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined


def _report_outcome_rows(report: dict, *, report_date: str | None) -> list[dict]:
    rows = _report_market_rows(report)
    outcome_rows: list[dict] = []
    for item in rows[:5]:
        ticker = str(item.get("ticker") or "").strip().upper()
        market = str(item.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US")
        history = load_lake_price_history(market=market, ticker=ticker, limit=260)
        baseline = None
        if report_date:
            prior_or_same = [row for row in history if str(row.get("date") or "") <= str(report_date)]
            if prior_or_same:
                baseline = prior_or_same[-1]
        if baseline is None and history:
            baseline = history[0]
        latest = history[-1] if history else None
        try:
            baseline_close = float((baseline or {}).get("close"))
        except (TypeError, ValueError):
            baseline_close = None
        try:
            latest_close = float((latest or {}).get("close"))
        except (TypeError, ValueError):
            latest_close = None
        baseline_date = str((baseline or {}).get("date") or "-")
        latest_date = str((latest or {}).get("date") or "-")
        return_pct = None
        status = "pending"
        if baseline_close and latest_close and latest_date > baseline_date:
            return_pct = (latest_close / baseline_close - 1.0) * 100.0
            if return_pct >= 3:
                status = "hit"
            elif return_pct <= -3:
                status = "miss"
            else:
                status = "watch"
        outcome_rows.append(
            {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": market,
                "baseline_date": baseline_date,
                "baseline_close": baseline_close,
                "latest_date": latest_date,
                "latest_close": latest_close,
                "return_pct": return_pct,
                "status": status,
            }
        )
    return outcome_rows


def _report_outcome_summary(outcome_rows: list[dict], *, lang: str) -> str:
    measured = [row for row in outcome_rows if row.get("return_pct") is not None]
    if not measured:
        return "暂无后续交易日价格，先保留待观察。" if lang == "zh" else "No later trading-day prices yet; keep this report pending."
    avg_return = sum(float(row.get("return_pct") or 0.0) for row in measured) / len(measured)
    hit_count = sum(1 for row in measured if float(row.get("return_pct") or 0.0) > 0)
    if lang == "zh":
        return f"已可验证 {len(measured)} 只，平均收益 {avg_return:.2f}%，上涨命中 {hit_count}/{len(measured)}。"
    return f"{len(measured)} names are measurable, average return {avg_return:.2f}%, positive hits {hit_count}/{len(measured)}."


def _outcome_status_label(status: str | None, *, lang: str) -> str:
    normalized = str(status or "").lower()
    if lang == "zh":
        return {
            "pending": "待观察",
            "hit": "命中",
            "miss": "失效",
            "watch": "观察",
        }.get(normalized, "-")
    return {
        "pending": "Pending",
        "hit": "Hit",
        "miss": "Miss",
        "watch": "Watch",
    }.get(normalized, "-")


def _audit_conclusion_for_trade(item: dict, *, lang: str) -> tuple[str, str]:
    action_hint = str(item.get("action_hint_at_exit") or "").strip().lower()
    reason = str(item.get("reason") or "").strip()
    pnl_pct = float(item.get("realized_pnl_pct") or 0.0)
    post_5d = item.get("post_sell_return_5d")
    post_10d = item.get("post_sell_return_10d")

    if not action_hint and not reason:
        return (
            "缺少审计快照" if lang == "zh" else "Missing audit snapshot",
            "这笔历史卖出没有保存当时建议，暂时只能看结果，无法判断建议与执行是否一致。"
            if lang == "zh"
            else "This historical exit did not save the advice snapshot, so we can only see the outcome for now.",
        )

    if reason == "止损/风险收缩":
        if any(token in action_hint for token in ("减", "exit", "trim", "risk", "退出")):
            return (
                "建议与执行一致" if lang == "zh" else "Advice matched execution",
                "当时系统偏向风险收缩，最终也按止损/减仓思路执行。"
                if lang == "zh"
                else "The system leaned defensive and the final action followed that risk-reduction posture.",
            )
        return (
            "执行偏保守" if lang == "zh" else "Execution was more defensive",
            "系统当时没有明确要求退出，但最终按止损/风险收缩执行。"
            if lang == "zh"
            else "The system did not explicitly call for an exit, yet the final action was more defensive.",
        )

    if reason in {"止盈/保护利润", "调仓"}:
        if any(token in action_hint for token in ("持有", "watch", "观察", "monitor")):
            return (
                "执行偏积极" if lang == "zh" else "Execution was more proactive",
                "系统更偏继续观察，但最终选择了兑现利润或调仓。"
                if lang == "zh"
                else "The system leaned toward monitoring, while the final action locked gains or rebalanced earlier.",
            )
        return (
            "建议与执行接近" if lang == "zh" else "Advice roughly matched execution",
            "系统给出的建议与最终的止盈/调仓动作大体一致。"
            if lang == "zh"
            else "The system advice broadly aligned with the eventual trim or rebalance.",
        )

    if reason == "复核后卖出":
        return (
            "人工复核主导" if lang == "zh" else "Human review led the exit",
            "这笔卖出更像复核后的主观执行，适合结合当日新闻和盘面再看。"
            if lang == "zh"
            else "This exit appears review-led and should be judged alongside the day’s news and tape.",
        )

    if post_5d is not None:
        if float(post_5d) <= -3.0:
            return (
                "卖出时机较好" if lang == "zh" else "Exit timing looked good",
                "卖出后 5 日价格继续走弱，说明这次退出至少避免了后续回撤。"
                if lang == "zh"
                else "Price kept weakening over the next 5 sessions, so the exit at least avoided further drawdown.",
            )
        if float(post_5d) >= 3.0:
            return (
                "可能偏早卖出" if lang == "zh" else "Exit may have been early",
                "卖出后 5 日价格继续上行，后续可以复盘是否过早兑现或过早止损。"
                if lang == "zh"
                else "Price continued higher over the next 5 sessions, so it is worth reviewing whether the exit was early.",
            )
    if post_10d is not None and float(post_10d) <= -5.0:
        return (
            "中期退出有效" if lang == "zh" else "Medium-term exit was effective",
            "卖出后 10 日仍明显走弱，说明这次退出在中期也具有保护效果。"
            if lang == "zh"
            else "The name remained weak over the next 10 sessions, suggesting the exit helped on a medium-term basis.",
        )

    if pnl_pct >= 0:
        return (
            "结果偏正面" if lang == "zh" else "Outcome was positive",
            "当前至少以正收益结束，但还需要更多样本判断系统建议是否长期有效。"
            if lang == "zh"
            else "The trade closed with a positive result, though more samples are needed to judge long-run advice quality.",
        )
    return (
        "结果偏负面" if lang == "zh" else "Outcome was negative",
        "这笔以负收益结束，后续适合回看是否该更早执行风险控制。"
        if lang == "zh"
        else "The trade ended negatively, so it is worth reviewing whether risk control should have happened earlier.",
    )


def _build_recommendation_validation_summary(
    db: Session,
    *,
    market: str,
    lang: str,
    selection_guidance: dict | None,
    selection_guidance_summary: dict | None = None,
    report_limit: int = 30,
    allow_compute: bool = True,
) -> dict:
    windows = (1, 3, 5, 10)
    market_code = str(market or "CN").strip().upper()
    target_market = market_code if market_code in {"CN", "US"} else None
    guidance = selection_guidance or {}
    recommendations = list(guidance.get("recommendations") or [])
    combos = list(guidance.get("combos") or [])
    top_model = recommendations[0] if recommendations else {}
    top_combo = combos[0] if combos else {}
    guidance_summary = selection_guidance_summary or summarize_model_selection_guidance(selection_guidance, lang=lang)
    cache_key = json.dumps(
        {
            "market": market_code,
            "lang": lang,
            "report_limit": max(5, int(report_limit)),
            "guidance_snapshot": (guidance.get("snapshot_meta") or {}).get("snapshot_id"),
            "guidance_date": (guidance.get("snapshot_meta") or {}).get("snapshot_date"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    cached = get_cached("dashboard_recommendation_validation", cache_key)
    if cached is not None:
        return cached
    if not allow_compute:
        return {"rows": [], "windows": windows, "report_count": 0, "measured_rows": 0}

    def _stats_for(item: dict, window: int) -> dict:
        return dict(item.get(f"stats_{window}d") or {})

    def _sample_count(item: dict) -> int:
        return max(int(_stats_for(item, window).get("count") or 0) for window in windows)

    rows: list[dict] = []
    if top_model:
        model_label = str(top_model.get("template_label") or top_model.get("template") or "-")
        action_bucket = str(top_model.get("action_bucket") or "").strip()
        if action_bucket and action_bucket not in {"ALL", "unclassified"}:
            action_label = ACTION_BUCKET_LABELS.get(action_bucket, {}).get(lang, action_bucket)
            model_label = f"{model_label} · {action_label}"
        rows.append(
            {
                "key": "priority_model",
                "label": "今日优先模型" if lang == "zh" else "Priority Model",
                "title": model_label,
                "count": _sample_count(top_model),
                "windows": {window: _stats_for(top_model, window) for window in windows},
                "note": (
                    f"强票提前覆盖 {int(top_model.get('winner_capture_count') or 0)} 只"
                    if lang == "zh"
                    else f"Captured {int(top_model.get('winner_capture_count') or 0)} strong movers"
                ),
                "href": guidance_summary.get("top_model_href"),
            }
        )
    if top_combo:
        combo_label = (top_combo.get("label") or {}).get(lang) or (top_combo.get("label") or {}).get("zh") or "-"
        rows.append(
            {
                "key": "priority_combo",
                "label": "今日优先组合" if lang == "zh" else "Priority Combo",
                "title": combo_label,
                "count": _sample_count(top_combo),
                "windows": {window: _stats_for(top_combo, window) for window in windows},
                "note": (
                    f"强票覆盖率 {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                    if lang == "zh"
                    else f"Winner capture {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                ),
                "href": top_combo.get("screener_href"),
            }
        )

    report_values: dict[int, list[float]] = {window: [] for window in windows}
    measured_rows = 0
    report_count = 0
    report_history = list_ai_daily_report_history(limit=max(5, int(report_limit)), db=db)
    # Forward-return measurement below is lake I/O, not database work.
    db.commit()
    for item in report_history:
        payload = item.get("payload") or {}
        report_date = str(item.get("snapshot_date") or payload.get("report_date") or "")[:10]
        if not report_date:
            continue
        rows_payload = _report_market_rows(payload)
        report_has_measurement = False
        for row in rows_payload[:5]:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            row_market = str(row.get("market") or "").strip().upper()
            if not row_market:
                row_market = "CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US"
            if target_market and row_market != target_market:
                continue
            history = load_lake_price_history(market=row_market, ticker=ticker, limit=260)
            measured = False
            for window in windows:
                value = _forward_return_from_history(history, trade_date=report_date, sessions=window)
                if value is None:
                    continue
                report_values[window].append(float(value))
                measured = True
            if measured:
                measured_rows += 1
                report_has_measurement = True
        if report_has_measurement:
            report_count += 1
    rows.append(
        {
            "key": "ai_report_top5",
            "label": "AI 日报 Top 5" if lang == "zh" else "AI Report Top 5",
            "title": "历史日报推荐留档" if lang == "zh" else "Archived Daily Recommendations",
            "count": measured_rows,
            "windows": {window: _aggregate_window_stats(report_values[window]) for window in windows},
            "note": (
                f"已纳入 {report_count} 期日报归档"
                if lang == "zh"
                else f"Based on {report_count} archived reports"
            ),
            "href": f"/dashboard/ai-daily-report/history?lang={lang}",
        }
    )
    result = {
        "rows": rows,
        "windows": windows,
        "report_count": report_count,
        "measured_rows": measured_rows,
    }
    set_cached("dashboard_recommendation_validation", cache_key, result, ttl_seconds=300.0)
    return result


def _build_watchlist_post_add_summary(
    db: Session,
    *,
    market: str = "ALL",
    allow_compute: bool = True,
) -> dict:
    normalized_market = str(market or "ALL").upper()
    cache_key = json.dumps({"market": normalized_market}, sort_keys=True, ensure_ascii=False)

    if not allow_compute:
        return get_cached("dashboard_watchlist_post_add_performance", cache_key) or {
            "rows": [],
            "count": 0,
            "windows": {3: _aggregate_window_stats([]), 5: _aggregate_window_stats([]), 10: _aggregate_window_stats([])},
            "current": {"count": 0, "avg_return": None, "hit_rate": None},
        }

    def _loader() -> dict:
        watchlist_repo = WatchlistRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        items = watchlist_repo.list_items(watchlist.id)
        if normalized_market != "ALL":
            items = [item for item in items if str(item.get("market") or "").upper() == normalized_market]
        # The remaining work is market-lake I/O. Close the read transaction
        # before a cold cache can trip PostgreSQL's idle transaction timeout.
        db.commit()
        rows: list[dict] = []
        window_values: dict[int, list[float]] = {3: [], 5: [], 10: []}
        current_values: list[float] = []
        for item in items:
            ticker = str(item.get("ticker") or "").upper()
            item_market = str(item.get("market") or "CN").upper() or "CN"
            added_at = str(item.get("created_at") or "")
            added_date = added_at[:10] if len(added_at) >= 10 else added_at
            if not ticker or not added_date:
                continue
            history = load_lake_price_history(market=item_market, ticker=ticker, limit=260)
            row_payload = {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": item_market,
                "added_date": added_date,
                "last_synced_date": item.get("last_synced_date"),
                "sync_status": item.get("sync_status"),
                "return_3d": _forward_return_from_history(history, trade_date=added_date, sessions=3),
                "return_5d": _forward_return_from_history(history, trade_date=added_date, sessions=5),
                "return_10d": _forward_return_from_history(history, trade_date=added_date, sessions=10),
                "current_return": _return_since_history_start(history, trade_date=added_date),
            }
            for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                value = row_payload.get(key)
                if value is not None:
                    window_values[window].append(float(value))
            if row_payload.get("current_return") is not None:
                current_values.append(float(row_payload["current_return"]))
            rows.append(row_payload)
        rows.sort(
            key=lambda item: (
                str(item.get("added_date") or ""),
                float(item.get("current_return") or -9999.0),
            ),
            reverse=True,
        )
        current_summary = _aggregate_window_stats(current_values)
        return {
            "rows": rows[:80],
            "count": len(rows),
            "windows": {window: _aggregate_window_stats(values) for window, values in window_values.items()},
            "current": {
                "count": current_summary.get("count"),
                "avg_return": current_summary.get("avg_return"),
                "hit_rate": current_summary.get("hit_rate"),
            },
        }

    return get_or_set("dashboard_watchlist_post_add_performance", cache_key, ttl_seconds=180.0, loader=_loader)


def _build_model_run_performance_summary(
    db: Session,
    *,
    run_id: int,
    top_n: int = 10,
    max_trade_dates: int = 20,
    market: str = "ALL",
    allow_compute: bool = True,
) -> dict | None:
    run = ModelRunRepository(db).get_run_by_id(run_id)
    if run is None:
        return None
    run_payload = {
        "id": run.id,
        "name": run.name,
        "market": run.market,
        "universe": run.universe,
        "status": run.status,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }
    normalized_market = str(market or "ALL").upper()
    cache_key = json.dumps(
        {
            "run_id": run_id,
            "top_n": top_n,
            "max_trade_dates": max_trade_dates,
            "market": normalized_market,
            "finished_at": run_payload["finished_at"],
            "status": run_payload["status"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    # A cache hit must not leave the request holding an open transaction.
    db.commit()
    if not allow_compute:
        return get_cached("dashboard_model_run_performance", cache_key)

    def _loader() -> dict:
        def _resolved_regime_label(detail: PredictionDetail | None, score: float | None) -> str | None:
            existing = str((detail.regime_label if detail is not None else "") or "").strip()
            if existing:
                return existing
            enriched = enrich_model_output({"score": score}, lang="en") or {}
            derived = str(enriched.get("regime_label") or "").strip()
            return derived or None

        date_stmt = (
            select(Prediction.trade_date)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Prediction.model_run_id == run_id)
            .distinct()
            .order_by(Prediction.trade_date.desc())
            .limit(max_trade_dates)
        )
        if normalized_market != "ALL":
            date_stmt = date_stmt.where(Symbol.market == normalized_market)
        selected_dates = [str(value) for value in db.scalars(date_stmt).all()]
        if not selected_dates:
            db.commit()
            return {
                "run": run_payload,
                "windows": {3: _aggregate_window_stats([]), 5: _aggregate_window_stats([]), 10: _aggregate_window_stats([])},
                "trade_dates": 0,
                "pick_count": 0,
                "latest_trade_date": None,
                "rows": [],
            }

        stmt = (
            select(Prediction, Symbol, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Prediction.model_run_id == run_id)
            .where(Prediction.trade_date.in_(selected_dates))
            .order_by(Prediction.trade_date.desc(), Prediction.score.desc(), Symbol.ticker.asc())
        )
        if normalized_market != "ALL":
            stmt = stmt.where(Symbol.market == normalized_market)
        rows = db.execute(stmt).all()
        if not rows:
            db.commit()
            return {
                "run": run_payload,
                "windows": {3: _aggregate_window_stats([]), 5: _aggregate_window_stats([]), 10: _aggregate_window_stats([])},
                "trade_dates": 0,
                "pick_count": 0,
                "latest_trade_date": None,
                "rows": [],
            }

        grouped: dict[str, list[tuple[Prediction, Symbol, PredictionDetail | None]]] = {}
        for prediction, symbol, detail in rows:
            grouped.setdefault(str(prediction.trade_date), []).append((prediction, symbol, detail))
        selected_items: list[dict] = []
        tickers_by_market: dict[str, set[str]] = {}
        for trade_date in selected_dates:
            for prediction, symbol, detail in grouped.get(trade_date, [])[:top_n]:
                ticker = str(symbol.ticker or "").upper()
                market_code = str(symbol.market or run_payload.get("market") or "").upper() or "CN"
                selected_items.append(
                    {
                        "trade_date": trade_date,
                        "ticker": ticker,
                        "name": symbol.name or ticker,
                        "market": symbol.market,
                        "market_code": market_code,
                        "sector": symbol.sector,
                        "industry": symbol.industry,
                        "exchange": symbol.exchange,
                        "regime_label": _resolved_regime_label(detail, prediction.score),
                        "score": prediction.score,
                        "signal_label": detail.signal_label if detail is not None else None,
                        "signal_strength": detail.signal_strength if detail is not None else None,
                    }
                )
                tickers_by_market.setdefault(market_code, set()).add(ticker)
        # All ORM values needed below have been copied to plain dictionaries.
        # Close the DB transaction before reading Parquet/DuckDB history.
        db.commit()
        history_cache: dict[tuple[str, str], list[dict]] = {}
        for market_code, tickers in tickers_by_market.items():
            for row in load_lake_rows(markets=[market_code], tickers=tickers, limit_per_symbol=260):
                ticker = str(row.get("symbol") or "").strip().upper()
                if ticker:
                    history_cache.setdefault((market_code, ticker), []).append(row)
        for key in list(history_cache.keys()):
            history_cache[key].sort(key=lambda item: str(item.get("date") or ""))
        window_values: dict[int, list[float]] = {3: [], 5: [], 10: []}
        pick_rows: list[dict] = []
        for item in selected_items:
            trade_date = str(item["trade_date"])
            ticker = str(item["ticker"])
            market_code = str(item["market_code"])
            history = history_cache.get((market_code, ticker), [])
            row_payload = {
                "trade_date": trade_date,
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": item.get("market"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "sector_group": resolve_template_group_label(
                    meta={
                        "sector": item.get("sector"),
                        "industry": item.get("industry"),
                        "exchange": item.get("exchange"),
                        "name": item.get("name"),
                    },
                    ticker=ticker,
                    market_code=market_code,
                    name=item.get("name"),
                ),
                "regime_label": item.get("regime_label"),
                "score": item.get("score"),
                "signal_label": item.get("signal_label"),
                "signal_strength": item.get("signal_strength"),
                "return_3d": _forward_return_from_history(history, trade_date=trade_date, sessions=3),
                "return_5d": _forward_return_from_history(history, trade_date=trade_date, sessions=5),
                "return_10d": _forward_return_from_history(history, trade_date=trade_date, sessions=10),
            }
            for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                value = row_payload.get(key)
                if value is not None:
                    window_values[window].append(float(value))
            pick_rows.append(row_payload)
        return {
            "run": run_payload,
            "windows": {window: _aggregate_window_stats(values) for window, values in window_values.items()},
            "trade_dates": len(selected_dates),
            "pick_count": len(pick_rows),
            "latest_trade_date": selected_dates[0] if selected_dates else None,
            "rows": pick_rows[: min(80, len(pick_rows))],
        }

    return get_or_set("dashboard_model_run_performance", cache_key, ttl_seconds=300.0, loader=_loader)


def _build_weekly_review_summary(db: Session, *, lang: str) -> dict:
    today = datetime.now(timezone.utc).astimezone().date()
    week_start = today - timedelta(days=6)
    week_start_iso = week_start.isoformat()
    cache_key = json.dumps({"week_start": week_start_iso, "lang": lang}, sort_keys=True, ensure_ascii=False)

    def _loader() -> dict:
        price_history_cache: dict[tuple[str, str], list[dict]] = {}

        def _cached_price_history(*, market_code: str, ticker: str) -> list[dict]:
            cache_key = (market_code, ticker)
            if cache_key not in price_history_cache:
                price_history_cache[cache_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
            return price_history_cache.get(cache_key) or []

        report_history = [
            item
            for item in list_ai_daily_report_history(limit=20, db=db)
            if str(item.get("snapshot_date") or "") >= week_start_iso
        ]
        top_ticker_counts: dict[str, dict] = {}
        mood_counts: dict[str, int] = {}
        report_window_values: dict[int, list[float]] = {1: [], 3: [], 5: [], 10: []}
        measured_report_rows = 0
        for item in report_history:
            payload = item.get("payload") or {}
            mood = str(payload.get("mood") or "").strip() or (t(lang, "未标记", "Unlabeled"))
            mood_counts[mood] = mood_counts.get(mood, 0) + 1
            report_date = str(item.get("snapshot_date") or payload.get("report_date") or "")[:10]
            for row in _report_market_rows(payload)[:5]:
                ticker = str(row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                bucket = top_ticker_counts.setdefault(
                    ticker,
                    {
                        "ticker": ticker,
                        "name": row.get("name") or ticker,
                        "count": 0,
                        "latest_verdict": row.get("verdict") or "-",
                    },
                )
                bucket["count"] += 1
                market_code = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US")
                history = _cached_price_history(market_code=market_code, ticker=ticker)
                row_measured = False
                for window in (1, 3, 5, 10):
                    value = _forward_return_from_history(history, trade_date=report_date, sessions=window)
                    if value is None:
                        continue
                    report_window_values[window].append(float(value))
                    row_measured = True
                if row_measured:
                    measured_report_rows += 1
        repeated_top_tickers = sorted(
            top_ticker_counts.values(),
            key=lambda item: (-int(item.get("count") or 0), str(item.get("ticker") or "")),
        )[:8]
        report_window_summary = {window: _aggregate_window_stats(values) for window, values in report_window_values.items()}

        selection_guidance = load_model_selection_guidance_snapshot(db, market="CN", allow_fallback=True)
        selection_guidance_summary = summarize_model_selection_guidance(selection_guidance, lang=lang)
        top_model = dict(selection_guidance_summary.get("top_model") or {})
        top_combo = dict(selection_guidance_summary.get("top_combo") or {})
        recommendation_validation_rows: list[dict] = []
        if top_model:
            recommendation_validation_rows.append(
                {
                    "label": "今日优先模型" if lang == "zh" else "Priority Model",
                    "title": selection_guidance_summary.get("top_model_title") or "-",
                    "href": selection_guidance_summary.get("top_model_href") or f"/dashboard/model-performance?lang={lang}",
                    "windows": {
                        1: top_model.get("stats_1d") or {},
                        3: top_model.get("stats_3d") or {},
                        5: top_model.get("stats_5d") or {},
                        10: top_model.get("stats_10d") or {},
                    },
                    "note": (
                        f"强票提前覆盖 {int(top_model.get('winner_capture_count') or 0)} 只"
                        if lang == "zh"
                        else f"Captured {int(top_model.get('winner_capture_count') or 0)} strong movers"
                    ),
                }
            )
        if top_combo:
            combo_label = (top_combo.get("label") or {}).get(lang) or (top_combo.get("label") or {}).get("zh") or "-"
            recommendation_validation_rows.append(
                {
                    "label": "今日优先组合" if lang == "zh" else "Priority Combo",
                    "title": combo_label,
                    "href": top_combo.get("screener_href") or f"/screeners?lang={lang}",
                    "windows": {
                        1: top_combo.get("stats_1d") or {},
                        3: top_combo.get("stats_3d") or {},
                        5: top_combo.get("stats_5d") or {},
                        10: top_combo.get("stats_10d") or {},
                    },
                    "note": (
                        f"强票覆盖率 {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                        if lang == "zh"
                        else f"Winner capture {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                    ),
                }
            )
        recommendation_validation_rows.append(
            {
                "label": "本周 AI 日报 Top 5" if lang == "zh" else "Weekly AI Report Top 5",
                "title": "日报归档自动验证" if lang == "zh" else "Archived Daily Recommendations",
                "href": f"/dashboard/ai-daily-report/history?lang={lang}",
                "windows": report_window_summary,
                "note": (
                    f"{len(report_history)} 期日报，{measured_report_rows} 个可测样本"
                    if lang == "zh"
                    else f"{len(report_history)} reports, {measured_report_rows} measurable rows"
                ),
            }
        )

        recent_jobs = DataJobRepository(db).list_recent_jobs(limit=180)
        weekly_jobs = [item for item in recent_jobs if str(item.get("started_at") or "")[:10] >= week_start_iso]
        job_status_counts: dict[str, int] = {}
        partial_or_failed_jobs: list[dict] = []
        for item in weekly_jobs:
            status = str(item.get("status") or "").lower() or "unknown"
            job_status_counts[status] = job_status_counts.get(status, 0) + 1
            if status in {"failed", "partial", "empty"}:
                partial_or_failed_jobs.append(item)

        recent_runs = [
            item
            for item in ModelRunRepository(db).list_recent_runs(limit=24)
            if str(item.get("created_at") or "")[:10] >= week_start_iso and str(item.get("status") or "").lower() == "success"
        ]
        run_rows: list[dict] = []
        model_window_values: dict[int, list[float]] = {3: [], 5: [], 10: []}
        for item in recent_runs[:8]:
            summary = _build_model_run_performance_summary(
                db,
                run_id=int(item["id"]),
                top_n=10,
                max_trade_dates=20,
                market=str(item.get("market") or "ALL"),
            )
            windows = (summary or {}).get("windows") or {}
            run_payload = {
                "id": int(item["id"]),
                "name": item.get("name") or "-",
                "market": item.get("market") or "-",
                "latest_trade_date": (summary or {}).get("latest_trade_date") or "-",
                "window_3": windows.get(3) or {},
                "window_5": windows.get(5) or {},
                "window_10": windows.get(10) or {},
            }
            for window in (3, 5, 10):
                avg_return = (windows.get(window) or {}).get("avg_return")
                if avg_return is not None:
                    model_window_values[window].append(float(avg_return))
            run_rows.append(run_payload)
        model_window_summary = {window: _aggregate_window_stats(values) for window, values in model_window_values.items()}

        weekly_trades = [
            item for item in load_portfolio_trades()
            if str(item.get("trade_date") or "") >= week_start_iso
        ]
        enriched_weekly_trades: list[dict] = []
        for item in weekly_trades:
            item_market = str(item.get("market") or "").strip().upper() or (
                "CN" if str(item.get("ticker") or "").upper().endswith((".SS", ".SZ", ".SH", ".BJ")) else "US"
            )
            ticker = str(item.get("ticker") or "").strip().upper()
            history = _cached_price_history(market_code=item_market, ticker=ticker)
            enriched_weekly_trades.append(
                {
                    **item,
                    "post_sell_return_3d": _forward_return_from_history(history, trade_date=str(item.get("trade_date") or ""), sessions=3),
                    "post_sell_return_5d": _forward_return_from_history(history, trade_date=str(item.get("trade_date") or ""), sessions=5),
                    "post_sell_return_10d": _forward_return_from_history(history, trade_date=str(item.get("trade_date") or ""), sessions=10),
                }
            )
        weekly_trades = enriched_weekly_trades
        realized_pnl = round(sum(float(item.get("realized_pnl") or 0.0) for item in weekly_trades), 2)
        winners = sum(1 for item in weekly_trades if float(item.get("realized_pnl") or 0.0) > 0)
        advice_effectiveness: dict[str, dict] = {}
        for item in weekly_trades:
            advice_key = trade_reason_bucket(item.get("reason"))
            bucket = advice_effectiveness.setdefault(
                advice_key,
                {"count": 0, "winner_count": 0, "realized_pnl": 0.0, "avg_return": 0.0},
            )
            bucket["count"] += 1
            pnl = float(item.get("realized_pnl") or 0.0)
            pnl_pct = float(item.get("realized_pnl_pct") or 0.0)
            bucket["realized_pnl"] += pnl
            bucket["avg_return"] += pnl_pct
            if pnl > 0:
                bucket["winner_count"] += 1
        advice_labels = {
            "profit_protection": "止盈/保护利润" if lang == "zh" else "Profit Protection",
            "risk_reduction": "止损/风险收缩" if lang == "zh" else "Risk Reduction",
            "rebalance": "调仓" if lang == "zh" else "Rebalance",
            "review": "复核后卖出" if lang == "zh" else "Review-led Exit",
            "event_risk": "事件风险" if lang == "zh" else "Event Risk",
            "other": "其他" if lang == "zh" else "Other",
        }
        advice_rows = []
        for key, payload in advice_effectiveness.items():
            count = int(payload.get("count") or 0)
            avg_return = (float(payload.get("avg_return") or 0.0) / count) if count else 0.0
            advice_rows.append(
                {
                    "bucket_key": key,
                    "bucket_label": advice_labels.get(key, key),
                    "count": count,
                    "winner_count": int(payload.get("winner_count") or 0),
                    "win_rate": round((int(payload.get("winner_count") or 0) / count) * 100.0, 1) if count else None,
                    "realized_pnl": round(float(payload.get("realized_pnl") or 0.0), 2),
                    "avg_return": round(avg_return, 2),
                }
            )
        advice_rows.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("bucket_label") or "")))
        structured_reason_count = sum(
            1
            for item in weekly_trades
            if str(item.get("reason") or "").strip() and str(item.get("reason") or "").strip() != "其他"
        )
        unresolved_trade_rows = [
            item for item in weekly_trades
            if str(item.get("reason") or "").strip() == "其他"
        ]
        audited_trade_rows = [
            item for item in weekly_trades
            if str(item.get("action_hint_at_exit") or "").strip() or str(item.get("action_reason_at_exit") or "").strip()
        ]

        return {
            "week_start": week_start_iso,
            "week_end": today.isoformat(),
            "report_count": len(report_history),
            "mood_counts": mood_counts,
            "repeated_top_tickers": repeated_top_tickers,
            "report_window_summary": report_window_summary,
            "measured_report_rows": measured_report_rows,
            "recommendation_validation_rows": recommendation_validation_rows,
            "job_status_counts": job_status_counts,
            "partial_or_failed_jobs": partial_or_failed_jobs[:10],
            "run_rows": run_rows,
            "model_window_summary": model_window_summary,
            "trade_rows": weekly_trades[:20],
            "trade_summary": {
                "count": len(weekly_trades),
                "realized_pnl": realized_pnl,
                "winner_count": winners,
            },
            "audit_summary": {
                "count": len(audited_trade_rows),
                "coverage_pct": round((len(audited_trade_rows) / len(weekly_trades)) * 100.0, 1) if weekly_trades else None,
            },
            "structured_reason_summary": {
                "count": structured_reason_count,
                "coverage_pct": round((structured_reason_count / len(weekly_trades)) * 100.0, 1) if weekly_trades else None,
            },
            "unresolved_trade_rows": unresolved_trade_rows[:12],
            "audited_trade_rows": audited_trade_rows[:12],
            "advice_effectiveness_rows": advice_rows,
        }

    return get_or_set("dashboard_weekly_review_summary", cache_key, ttl_seconds=300.0, loader=_loader)
