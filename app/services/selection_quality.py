from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from statistics import mean
from typing import Any

from app.services.cost_basis import (
    CANONICAL_COST_SOURCE,
    canonical_round_trip_cost_bps,
)
from app.services.factor_experiments import (
    FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE,
    attach_forward_outcomes,
)
from app.services.recommendation_regression import (
    AI_DAILY_REPORT_HISTORY_SNAPSHOT_TYPE,
    _iter_report_candidate_rows,
)
from app.services.market_lake import load_lake_rows
from app.services.price_basis import preferred_close
from app.services.repository import WorkspaceSnapshotRepository
from app.services.market_freshness import is_snapshot_as_of_current
from app.services.runtime_cache import clear_namespace, get_or_set
from app.services.statistical_inference import day_clustered_hit_rate_ci
from app.services.time_utils import app_now_iso, app_today_iso


SELECTION_QUALITY_SNAPSHOT_TYPE = "selection_quality_snapshot"


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_avg(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(mean(clean), 2) if clean else None


def _pct_rate(flags: list[bool | None]) -> float | None:
    clean = [bool(value) for value in flags if value is not None]
    return round(sum(1 for value in clean if value) / len(clean) * 100.0, 1) if clean else None


def _iid_rate_ci95(flags: list[bool | None]) -> list[float] | None:
    """Legacy iid normal interval for a rate, kept as the diagnostic companion."""

    clean = [bool(value) for value in flags if value is not None]
    if not clean:
        return None
    rate = sum(1 for value in clean if value) / len(clean)
    margin = 1.96 * (rate * (1.0 - rate) / len(clean)) ** 0.5 * 100.0
    return [max(0.0, rate * 100.0 - margin), min(100.0, rate * 100.0 + margin)]


def _source_key(record: dict[str, Any]) -> str:
    return f"{record.get('source_type') or 'unknown'}:{record.get('source_name') or 'unknown'}"


def _load_histories(rows: list[dict[str, Any]], *, limit_per_symbol: int = 320) -> dict[tuple[str, str], list[dict[str, Any]]]:
    tickers_by_market: dict[str, set[str]] = {"CN": set(), "US": set()}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        market = str(row.get("market") or "").strip().upper() or "CN"
        if ticker and market in tickers_by_market:
            tickers_by_market[market].add(ticker)
    histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market, tickers in tickers_by_market.items():
        if not tickers:
            continue
        for item in load_lake_rows(markets=[market], tickers=tickers, limit_per_symbol=limit_per_symbol):
            ticker = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
            if ticker:
                histories.setdefault((market, ticker), []).append(item)
    for history in histories.values():
        history.sort(key=lambda item: str(item.get("date") or item.get("trade_date") or ""))
    return histories


def _next_session_metrics_from_history(*, history: list[dict[str, Any]] | None, report_date: str) -> dict[str, Any] | None:
    if not history:
        return None
    baseline = None
    next_row = None
    for row in history:
        row_date = str(row.get("date") or row.get("trade_date") or "")[:10]
        if not row_date:
            continue
        if row_date <= report_date:
            baseline = row
            continue
        next_row = row
        break
    if baseline is None or next_row is None:
        return None
    # R2: close-to-close return prefers the adjusted view.  The raw close is
    # retained for the overnight gap and intraday open-to-close ratios, whose
    # other leg (raw open) is not adjusted -- mixing bases there would change
    # their meaning, not just their scale.
    raw_base_close = _safe_float(baseline.get("close"))
    base_close = preferred_close(baseline) or raw_base_close
    next_open = _safe_float(next_row.get("open"))
    next_high = _safe_float(next_row.get("high"))
    next_low = _safe_float(next_row.get("low"))
    raw_next_close = _safe_float(next_row.get("close"))
    next_close = preferred_close(next_row) or raw_next_close
    if not base_close or not next_open or not next_high or not next_low or not next_close:
        return None

    def pct(start: float, end: float) -> float:
        return round((end / start - 1.0) * 100.0, 2)

    gap_open = pct(raw_base_close or base_close, next_open)
    open_to_high = pct(next_open, next_high)
    open_to_low = pct(next_open, next_low)
    close_1d = pct(base_close, next_close)
    return {
        "next_date": str(next_row.get("date") or next_row.get("trade_date") or "")[:10],
        "gap_open_pct": gap_open,
        "open_to_high_pct": open_to_high,
        "open_to_low_pct": open_to_low,
        "open_to_close_pct": pct(next_open, raw_next_close or next_close),
        "close_1d_pct": close_1d,
        "close_hit": close_1d > 0,
        "execution_hit": open_to_high >= 2.0 and open_to_low > -4.0,
        "gap_blocked": gap_open >= 7.0,
    }


def _build_ai_records(repo: WorkspaceSnapshotRepository, *, history_limit: int) -> list[dict[str, Any]]:
    snapshots = repo.list_snapshots(AI_DAILY_REPORT_HISTORY_SNAPSHOT_TYPE, limit=history_limit)
    candidate_rows: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    history_seed_rows: list[dict[str, Any]] = []
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        report_date = str(snapshot.get("snapshot_date") or payload.get("report_date") or "")[:10]
        if not report_date:
            continue
        for row in _iter_report_candidate_rows(payload, report_date=report_date):
            ticker = str(row.get("ticker") or "").strip().upper()
            market = str(row.get("market") or "").strip().upper() or "CN"
            if not ticker:
                continue
            candidate_rows.append((snapshot, row, report_date))
            history_seed_rows.append({"ticker": ticker, "market": market})
    histories = _load_histories(history_seed_rows, limit_per_symbol=320)

    records_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for snapshot, row, report_date in candidate_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        market = str(row.get("market") or "").strip().upper() or "CN"
        metrics = _next_session_metrics_from_history(history=histories.get((market, ticker)), report_date=report_date)
        template = str(row.get("full_market_template") or row.get("report_source_label") or "ai_daily_report")
        cost_bps = canonical_round_trip_cost_bps(market)
        cost_pct = cost_bps / 100.0
        record = {
            "source_type": "ai_daily_report",
            "source_name": f"{row.get('report_pool') or 'unknown'} · {template}",
            "source_group": row.get("report_pool") or "unknown",
            "source_snapshot_id": snapshot.get("id"),
            "signal_date": report_date,
            "ticker": ticker,
            "name": row.get("name"),
            "market": market,
            "rank": row.get("report_rank"),
            "score": _safe_float(row.get("trade_readiness_score") or row.get("quality_gate_score") or row.get("score")),
            "status": "pending",
            "next_date": None,
            "return_1d_pct": None,
            "net_return_1d_pct": None,
            "cost_bps": cost_bps,
            "cost_basis": "round_trip",
            "cost_source": CANONICAL_COST_SOURCE,
            "return_3d_pct": None,
            "return_5d_pct": None,
            "next_open_gap_pct": None,
            "open_to_high_pct": None,
            "open_to_low_pct": None,
            "open_to_close_pct": None,
            "max_drawdown_5d_pct": None,
            "hit_1d": None,
            "execution_hit": None,
            "gap_blocked": None,
            "risk_flags": list(row.get("risk_flags") or [])[:5],
        }
        if metrics:
            close_1d = _safe_float(metrics.get("close_1d_pct"))
            open_to_high = _safe_float(metrics.get("open_to_high_pct"))
            open_to_low = _safe_float(metrics.get("open_to_low_pct"))
            # Cost-adjusted hit semantics: `hit_1d` is the net close-to-close
            # sign and `execution_hit` needs the intraday high to clear the
            # profit target *after* the canonical round trip.  `return_1d_pct`
            # stays gross so existing consumers are unchanged.
            net_return_1d = round(close_1d - cost_pct, 2) if close_1d is not None else None
            execution_hit = (
                open_to_high is not None
                and open_to_low is not None
                and (open_to_high - cost_pct) >= 2.0
                and open_to_low > -4.0
            )
            record.update(
                {
                    "status": "available",
                    "next_date": metrics.get("next_date"),
                    "return_1d_pct": close_1d,
                    "net_return_1d_pct": net_return_1d,
                    "next_open_gap_pct": _safe_float(metrics.get("gap_open_pct")),
                    "open_to_high_pct": open_to_high,
                    "open_to_low_pct": open_to_low,
                    "open_to_close_pct": _safe_float(metrics.get("open_to_close_pct")),
                    "hit_1d": (net_return_1d > 0) if net_return_1d is not None else None,
                    "execution_hit": execution_hit,
                    "gap_blocked": bool(metrics.get("gap_blocked")),
                }
            )
        records_by_key[(report_date, str(row.get("report_pool") or ""), ticker)] = record
    return list(records_by_key.values())


def _build_factor_records(repo: WorkspaceSnapshotRepository, *, history_limit: int) -> list[dict[str, Any]]:
    snapshots = repo.list_snapshots(FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE, limit=history_limit)
    records_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
        rows = [dict(row) for row in deepcopy(payload.get("rows") or []) if isinstance(row, dict)]
        if not rows:
            continue
        attach_forward_outcomes(rows)
        strategy_id = str(strategy.get("id") or "unknown")
        strategy_name = str(strategy.get("name") or strategy_id)
        for index, row in enumerate(rows[:80], start=1):
            ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
            market = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SZ", ".SS", ".SH")) else "US")
            if not ticker:
                continue
            outcome = row.get("forward_outcome") if isinstance(row.get("forward_outcome"), dict) else {}
            signal_date = str(outcome.get("trade_date") or row.get("factor_signal_trade_date") or row.get("trade_date") or "")[:10]
            if not signal_date:
                continue
            return_1d = _safe_float(outcome.get("return_1d_pct"))
            open_to_high = _safe_float(outcome.get("next_open_to_high_pct"))
            open_to_low = _safe_float(outcome.get("next_open_to_low_pct"))
            cost_bps = _safe_float(outcome.get("cost_bps"))
            if cost_bps is None:
                cost_bps = canonical_round_trip_cost_bps(market)
            cost_pct = cost_bps / 100.0
            net_return_1d = _safe_float(outcome.get("net_return_1d_pct"))
            if net_return_1d is None and return_1d is not None:
                net_return_1d = round(return_1d - cost_pct, 2)
            execution_hit = (
                (open_to_high - cost_pct) >= 2.0 and open_to_low > -4.0
            ) if open_to_high is not None and open_to_low is not None else None
            record = {
                "source_type": "factor_experiment",
                "source_name": strategy_name,
                "source_group": strategy_id,
                "source_snapshot_id": snapshot.get("id"),
                "signal_date": signal_date,
                "ticker": ticker,
                "name": row.get("name"),
                "market": market,
                "rank": index,
                "score": _safe_float(row.get("factor_score")),
                "status": "available" if outcome.get("status") == "ok" and return_1d is not None else str(outcome.get("status") or "pending"),
                "next_date": outcome.get("next_trade_date"),
                "return_1d_pct": return_1d,
                "net_return_1d_pct": net_return_1d,
                "cost_bps": cost_bps,
                "cost_basis": "round_trip",
                "cost_source": CANONICAL_COST_SOURCE,
                "return_3d_pct": _safe_float(outcome.get("return_3d_pct")),
                "return_5d_pct": _safe_float(outcome.get("return_5d_pct")),
                "next_open_gap_pct": _safe_float(outcome.get("next_open_gap_pct")),
                "open_to_high_pct": open_to_high,
                "open_to_low_pct": open_to_low,
                "open_to_close_pct": None,
                "max_drawdown_5d_pct": _safe_float(outcome.get("max_drawdown_5d_pct")),
                "hit_1d": (net_return_1d > 0) if net_return_1d is not None else None,
                "execution_hit": execution_hit,
                "gap_blocked": bool(outcome.get("gap_unbuyable")) if outcome.get("gap_unbuyable") is not None else None,
                "risk_flags": list(row.get("risk_flags") or row.get("execution_tags") or [])[:5],
            }
            records_by_key[(strategy_id, signal_date, ticker)] = record
    return list(records_by_key.values())


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    available = [record for record in records if record.get("return_1d_pct") is not None]
    signal_dates = [str(record.get("signal_date") or "") for record in available]
    # The ledger's forward label is one session ahead, so the clustering block is
    # one day.  Both hit-rate intervals are day-clustered; the iid point-in-time
    # rate is retained by `_pct_rate` as the diagnostic companion.
    hit_ci = day_clustered_hit_rate_ci(
        [record.get("hit_1d") for record in available], signal_dates, block_length=1
    )
    execution_ci = day_clustered_hit_rate_ci(
        [record.get("execution_hit") for record in available], signal_dates, block_length=1
    )
    return {
        "count": len(records),
        "available_1d": len(available),
        "cost_bps": canonical_round_trip_cost_bps(),
        "cost_basis": "round_trip",
        "cost_source": CANONICAL_COST_SOURCE,
        "hit_rate_1d_pct": _pct_rate([record.get("hit_1d") for record in available]),
        "hit_rate_1d_ci95_clustered": list(hit_ci["ci95"]) if hit_ci.get("ci95") else None,
        "hit_rate_1d_ci95_iid": _iid_rate_ci95([record.get("hit_1d") for record in available]),
        "hit_rate_1d_ci_lower_bound_clustered": hit_ci["ci95"][0] if hit_ci.get("ci95") else None,
        "hit_rate_ci_cluster_method": hit_ci.get("method"),
        "hit_rate_ci_cluster_days": hit_ci.get("days"),
        "hit_rate_ci_block_length": hit_ci.get("block_length"),
        "hit_rate_1d_ci95_iid_is_diagnostic_only": True,
        "execution_hit_rate_pct": _pct_rate([record.get("execution_hit") for record in available]),
        "execution_hit_rate_ci95_clustered": list(execution_ci["ci95"]) if execution_ci.get("ci95") else None,
        "execution_hit_rate_ci_lower_bound_clustered": execution_ci["ci95"][0] if execution_ci.get("ci95") else None,
        "avg_return_1d_pct": _pct_avg([_safe_float(record.get("return_1d_pct")) for record in available]),
        "avg_net_return_1d_pct": _pct_avg([_safe_float(record.get("net_return_1d_pct")) for record in available]),
        "avg_return_3d_pct": _pct_avg([_safe_float(record.get("return_3d_pct")) for record in records]),
        "avg_return_5d_pct": _pct_avg([_safe_float(record.get("return_5d_pct")) for record in records]),
        "avg_open_to_high_pct": _pct_avg([_safe_float(record.get("open_to_high_pct")) for record in available]),
        "avg_open_to_low_pct": _pct_avg([_safe_float(record.get("open_to_low_pct")) for record in available]),
        "avg_max_drawdown_5d_pct": _pct_avg([_safe_float(record.get("max_drawdown_5d_pct")) for record in records]),
        "gap_blocked_rate_pct": _pct_rate([record.get("gap_blocked") for record in available]),
    }


SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT = 50.0


def _guidance_from_summary(summary_by_source: list[dict[str, Any]]) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in summary_by_source:
        metrics = item.get("metrics") or {}
        available = int(metrics.get("available_1d") or 0)
        lower = metrics.get("hit_rate_1d_ci_lower_bound_clustered")
        if available < 3:
            rejected.append({"source_name": item.get("source_name"), "reason": "insufficient_samples", "available_1d": available})
            continue
        if lower is None:
            rejected.append({"source_name": item.get("source_name"), "reason": "clustered_ci_unavailable_insufficient_days", "available_1d": available})
            continue
        if float(lower) <= SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT:
            # A source whose day-clustered hit-rate lower bound does not clear
            # chance cannot be preferred, regardless of its point estimate.
            rejected.append(
                {
                    "source_name": item.get("source_name"),
                    "reason": "clustered_hit_rate_ci_lower_bound_not_above_chance",
                    "observed_lower_bound_pct": lower,
                    "threshold_pct": SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT,
                }
            )
            continue
        eligible.append(item)
    eligible.sort(
        key=lambda item: (
            float((item.get("metrics") or {}).get("execution_hit_rate_pct") or 0.0),
            float((item.get("metrics") or {}).get("hit_rate_1d_pct") or 0.0),
            float((item.get("metrics") or {}).get("hit_rate_1d_ci_lower_bound_clustered") or 0.0),
            float((item.get("metrics") or {}).get("avg_net_return_1d_pct") or -99.0),
        ),
        reverse=True,
    )
    leader = eligible[0] if eligible else None
    if not leader:
        return {
            "stance": "collect_more",
            "headline_zh": "还需要继续累计样本，暂时不要只依赖单一模型或策略。",
            "headline_en": "More samples are needed before trusting one model or strategy.",
            "preferred_sources": [],
            "rules_zh": ["优先使用多模型共振、低风险标签、接近买点的候选；样本不足时宁缺毋滥。"],
            "hit_rate_ci_lower_threshold_pct": SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT,
            "rejected_sources": rejected[:8],
        }
    metrics = leader.get("metrics") or {}
    source_name = str(leader.get("source_name") or "-")
    return {
        "stance": "prefer_leader",
        "headline_zh": f"近期相对领先的是 {source_name}，但仍需看次日开盘承接，不能追高。",
        "headline_en": f"Recent leader: {source_name}. Still require next-session confirmation and avoid chasing.",
        "preferred_sources": [item.get("source_name") for item in eligible[:3]],
        "rules_zh": [
            f"优先复用执行命中率较高的来源：{source_name}，当前执行命中率 {metrics.get('execution_hit_rate_pct') or '-'}%。",
            "若高开过大或开盘后快速跌破开盘价 3%-4%，即使模型命中也放弃。",
            "日报候选和因子实验若同时命中，优先级高于单一来源候选。",
        ],
        "hit_rate_ci_lower_threshold_pct": SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT,
        "leader_hit_rate_ci_lower_bound_clustered": metrics.get("hit_rate_1d_ci_lower_bound_clustered"),
        "rejected_sources": rejected[:8],
    }


def build_selection_quality(*, db, ai_history_limit: int = 60, factor_run_limit: int = 40) -> dict[str, Any]:
    repo = WorkspaceSnapshotRepository(db)
    ai_records = _build_ai_records(repo, history_limit=ai_history_limit)
    factor_records = _build_factor_records(repo, history_limit=factor_run_limit)
    records = ai_records + factor_records
    records.sort(key=lambda item: (str(item.get("signal_date") or ""), str(item.get("source_type") or ""), int(item.get("rank") or 9999)), reverse=True)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[_source_key(record)].append(record)
    summary_by_source = [
        {
            "source_key": key,
            "source_type": rows[0].get("source_type"),
            "source_name": rows[0].get("source_name"),
            "source_group": rows[0].get("source_group"),
            "metrics": _aggregate(rows),
        }
        for key, rows in grouped.items()
    ]
    summary_by_source.sort(
        key=lambda item: (
            int((item.get("metrics") or {}).get("available_1d") or 0),
            float((item.get("metrics") or {}).get("execution_hit_rate_pct") or 0.0),
            float((item.get("metrics") or {}).get("avg_return_1d_pct") or -99.0),
        ),
        reverse=True,
    )
    payload = {
        "snapshot_type": SELECTION_QUALITY_SNAPSHOT_TYPE,
        "generated_at": app_now_iso(),
        "snapshot_date": app_today_iso(),
        "source_counts": {
            "ai_daily_report": len(ai_records),
            "factor_experiment": len(factor_records),
        },
        "sample_count": len(records),
        "summary": {
            "all": _aggregate(records),
            "by_source": summary_by_source,
            "cost_bps": canonical_round_trip_cost_bps(),
            "cost_basis": "round_trip",
            "cost_source": CANONICAL_COST_SOURCE,
        },
        "guidance": _guidance_from_summary(summary_by_source),
        "recent_records": records[:80],
    }
    return payload


def save_selection_quality_snapshot(*, db, source_job_id: int | None = None) -> dict[str, Any]:
    payload = build_selection_quality(db=db)
    payload["schema_version"] = 1
    snapshot = WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=SELECTION_QUALITY_SNAPSHOT_TYPE,
        snapshot_date=app_today_iso(),
        payload=payload,
        source_job_id=source_job_id,
    )
    clear_namespace("selection_quality")
    return {
        "id": snapshot.id,
        "snapshot_type": snapshot.snapshot_type,
        "snapshot_date": snapshot.snapshot_date,
        "created_at": snapshot.created_at,
        "sample_count": int(payload.get("sample_count") or 0),
    }


def load_latest_selection_quality_snapshot(*, db) -> dict[str, Any] | None:
    snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(SELECTION_QUALITY_SNAPSHOT_TYPE)
    if not snapshot:
        return None
    payload = snapshot.get("payload") or {}
    if not payload.get("schema_version"):
        return snapshot
    # This ledger is consumed by both CN and US recommendations.  Do not let
    # an old successful job silently influence today's ranking policy.
    if not all(
        is_snapshot_as_of_current(snapshot.get("snapshot_date"), market)
        for market in ("CN", "US")
    ):
        return None
    return snapshot


def load_or_build_selection_quality(*, db) -> dict[str, Any]:
    def _load() -> dict[str, Any]:
        snapshot = load_latest_selection_quality_snapshot(db=db)
        if snapshot and isinstance(snapshot.get("payload"), dict):
            payload = dict(snapshot.get("payload") or {})
            if int(payload.get("sample_count") or 0) > 0:
                payload["snapshot_meta"] = {
                    "source": "snapshot",
                    "snapshot_id": snapshot.get("id"),
                    "snapshot_date": snapshot.get("snapshot_date"),
                    "created_at": snapshot.get("created_at"),
                }
                return payload
        payload = build_selection_quality(db=db)
        payload["snapshot_meta"] = {"source": "live"}
        return payload

    return get_or_set("selection_quality", "latest", ttl_seconds=600.0, loader=_load)
