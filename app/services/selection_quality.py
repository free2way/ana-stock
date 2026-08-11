from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from statistics import mean
from typing import Any

from app.services.factor_experiments import (
    FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE,
    attach_forward_outcomes,
)
from app.services.recommendation_regression import (
    AI_DAILY_REPORT_HISTORY_SNAPSHOT_TYPE,
    _iter_report_candidate_rows,
)
from app.services.market_lake import load_lake_rows
from app.services.repository import WorkspaceSnapshotRepository
from app.services.market_freshness import is_snapshot_as_of_current
from app.services.runtime_cache import clear_namespace, get_or_set
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
    base_close = _safe_float(baseline.get("close"))
    next_open = _safe_float(next_row.get("open"))
    next_high = _safe_float(next_row.get("high"))
    next_low = _safe_float(next_row.get("low"))
    next_close = _safe_float(next_row.get("close"))
    if not base_close or not next_open or not next_high or not next_low or not next_close:
        return None

    def pct(start: float, end: float) -> float:
        return round((end / start - 1.0) * 100.0, 2)

    open_to_high = pct(next_open, next_high)
    open_to_low = pct(next_open, next_low)
    close_1d = pct(base_close, next_close)
    return {
        "next_date": str(next_row.get("date") or next_row.get("trade_date") or "")[:10],
        "gap_open_pct": pct(base_close, next_open),
        "open_to_high_pct": open_to_high,
        "open_to_low_pct": open_to_low,
        "open_to_close_pct": pct(next_open, next_close),
        "close_1d_pct": close_1d,
        "close_hit": close_1d > 0,
        "execution_hit": open_to_high >= 2.0 and open_to_low > -4.0,
        "gap_blocked": pct(base_close, next_open) >= 7.0,
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
            record.update(
                {
                    "status": "available",
                    "next_date": metrics.get("next_date"),
                    "return_1d_pct": close_1d,
                    "next_open_gap_pct": _safe_float(metrics.get("gap_open_pct")),
                    "open_to_high_pct": _safe_float(metrics.get("open_to_high_pct")),
                    "open_to_low_pct": _safe_float(metrics.get("open_to_low_pct")),
                    "open_to_close_pct": _safe_float(metrics.get("open_to_close_pct")),
                    "hit_1d": bool(metrics.get("close_hit")) if close_1d is not None else None,
                    "execution_hit": bool(metrics.get("execution_hit")),
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
                "return_3d_pct": _safe_float(outcome.get("return_3d_pct")),
                "return_5d_pct": _safe_float(outcome.get("return_5d_pct")),
                "next_open_gap_pct": _safe_float(outcome.get("next_open_gap_pct")),
                "open_to_high_pct": open_to_high,
                "open_to_low_pct": open_to_low,
                "open_to_close_pct": None,
                "max_drawdown_5d_pct": _safe_float(outcome.get("max_drawdown_5d_pct")),
                "hit_1d": return_1d > 0 if return_1d is not None else None,
                "execution_hit": (open_to_high >= 2.0 and open_to_low > -4.0) if open_to_high is not None and open_to_low is not None else None,
                "gap_blocked": bool(outcome.get("gap_unbuyable")) if outcome.get("gap_unbuyable") is not None else None,
                "risk_flags": list(row.get("risk_flags") or row.get("execution_tags") or [])[:5],
            }
            records_by_key[(strategy_id, signal_date, ticker)] = record
    return list(records_by_key.values())


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    available = [record for record in records if record.get("return_1d_pct") is not None]
    return {
        "count": len(records),
        "available_1d": len(available),
        "hit_rate_1d_pct": _pct_rate([record.get("hit_1d") for record in available]),
        "execution_hit_rate_pct": _pct_rate([record.get("execution_hit") for record in available]),
        "avg_return_1d_pct": _pct_avg([_safe_float(record.get("return_1d_pct")) for record in available]),
        "avg_return_3d_pct": _pct_avg([_safe_float(record.get("return_3d_pct")) for record in records]),
        "avg_return_5d_pct": _pct_avg([_safe_float(record.get("return_5d_pct")) for record in records]),
        "avg_open_to_high_pct": _pct_avg([_safe_float(record.get("open_to_high_pct")) for record in available]),
        "avg_open_to_low_pct": _pct_avg([_safe_float(record.get("open_to_low_pct")) for record in available]),
        "avg_max_drawdown_5d_pct": _pct_avg([_safe_float(record.get("max_drawdown_5d_pct")) for record in records]),
        "gap_blocked_rate_pct": _pct_rate([record.get("gap_blocked") for record in available]),
    }


def _guidance_from_summary(summary_by_source: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = [
        item
        for item in summary_by_source
        if int((item.get("metrics") or {}).get("available_1d") or 0) >= 3
    ]
    ranked.sort(
        key=lambda item: (
            float((item.get("metrics") or {}).get("execution_hit_rate_pct") or 0.0),
            float((item.get("metrics") or {}).get("hit_rate_1d_pct") or 0.0),
            float((item.get("metrics") or {}).get("avg_return_1d_pct") or -99.0),
        ),
        reverse=True,
    )
    leader = ranked[0] if ranked else None
    if not leader:
        return {
            "stance": "collect_more",
            "headline_zh": "还需要继续累计样本，暂时不要只依赖单一模型或策略。",
            "headline_en": "More samples are needed before trusting one model or strategy.",
            "preferred_sources": [],
            "rules_zh": ["优先使用多模型共振、低风险标签、接近买点的候选；样本不足时宁缺毋滥。"],
        }
    metrics = leader.get("metrics") or {}
    source_name = str(leader.get("source_name") or "-")
    return {
        "stance": "prefer_leader",
        "headline_zh": f"近期相对领先的是 {source_name}，但仍需看次日开盘承接，不能追高。",
        "headline_en": f"Recent leader: {source_name}. Still require next-session confirmation and avoid chasing.",
        "preferred_sources": [item.get("source_name") for item in ranked[:3]],
        "rules_zh": [
            f"优先复用执行命中率较高的来源：{source_name}，当前执行命中率 {metrics.get('execution_hit_rate_pct') or '-'}%。",
            "若高开过大或开盘后快速跌破开盘价 3%-4%，即使模型命中也放弃。",
            "日报候选和因子实验若同时命中，优先级高于单一来源候选。",
        ],
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
