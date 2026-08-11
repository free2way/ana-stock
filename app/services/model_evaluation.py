"""Persisted, market-state-aware out-of-sample model evaluation.

The existing template panels are intentionally lightweight and compute their
numbers on demand.  This module is the auditable counterpart: every execution
stores the model version, selected prediction dates, cost assumption and the
market-state label that was available *on that date*.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import ModelEvaluation, ModelEvaluationMetric, ModelRun, Prediction, Symbol, WorkspaceSnapshot
from app.services.market_lake import load_lake_price_history
from app.services.market_risk import market_risk_snapshot_type
from app.services.time_utils import app_now_iso


DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
CORPORATE_ACTION_JUMP_PCT = 80.0
STRICT_OOS_MIN_COVERAGE_DAYS = 20
STRICT_OOS_MIN_SAMPLES = 100


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _history_outcome(history: list[dict], *, trade_date: str, horizon_days: int) -> dict | None:
    """Return forward gross return and path drawdown in percent for one pick."""
    target_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") == trade_date), None)
    if target_index is None or target_index + horizon_days >= len(history):
        return None
    entry = _number(history[target_index].get("adj_close")) or _number(history[target_index].get("close"))
    exit_price = _number(history[target_index + horizon_days].get("adj_close")) or _number(history[target_index + horizon_days].get("close"))
    if not entry or exit_price is None:
        return None
    path = history[target_index : target_index + horizon_days + 1]
    path_closes = [(_number(row.get("adj_close")) or _number(row.get("close"))) for row in path]
    for previous, current in zip(path_closes, path_closes[1:], strict=False):
        if previous is None or current is None or previous <= 0:
            continue
        daily_move_pct = ((current / previous) - 1.0) * 100.0
        if abs(daily_move_pct) >= CORPORATE_ACTION_JUMP_PCT:
            # Polygon rows without split-adjusted historical prices can make a
            # reverse split look like a multi-thousand-percent prediction win.
            # Exclude the whole holding path until adjusted history is available.
            return {"excluded_reason": "suspected_corporate_action_discontinuity"}
    lows = [
        _number(row.get("low")) or _number(row.get("adj_close")) or _number(row.get("close"))
        for row in history[target_index : target_index + horizon_days + 1]
    ]
    valid_lows = [value for value in lows if value is not None]
    return {
        "gross_return_pct": ((exit_price / entry) - 1.0) * 100.0,
        "drawdown_pct": ((min(valid_lows) / entry) - 1.0) * 100.0 if valid_lows else None,
    }


def summarize_evaluation_samples(samples: list[dict], *, horizon_days: int, round_trip_cost_bps: float) -> dict:
    """Calculate net-of-cost performance; intentionally pure for regression tests."""
    cost_pct = max(0.0, float(round_trip_cost_bps)) / 100.0
    gross_returns = [float(item["gross_return_pct"]) for item in samples if item.get("gross_return_pct") is not None]
    net_returns = [value - cost_pct for value in gross_returns]
    drawdowns = [float(item["drawdown_pct"]) for item in samples if item.get("drawdown_pct") is not None]
    count = len(net_returns)
    positive = [value for value in net_returns if value > 0]
    negative = [value for value in net_returns if value < 0]
    hit_rate = (len(positive) / count * 100.0) if count else None
    if count and hit_rate is not None:
        proportion = hit_rate / 100.0
        margin = 1.96 * math.sqrt(proportion * (1.0 - proportion) / count) * 100.0
        confidence_low = max(0.0, hit_rate - margin)
        confidence_high = min(100.0, hit_rate + margin)
    else:
        confidence_low = confidence_high = None
    return {
        "horizon_days": int(horizon_days),
        "sample_count": count,
        "cost_bps": float(round_trip_cost_bps),
        "hit_rate": hit_rate,
        "avg_return": statistics.fmean(net_returns) if net_returns else None,
        "median_return": statistics.median(net_returns) if net_returns else None,
        "gross_avg_return": statistics.fmean(gross_returns) if gross_returns else None,
        "avg_drawdown": statistics.fmean(drawdowns) if drawdowns else None,
        "max_drawdown": min(drawdowns) if drawdowns else None,
        "profit_loss_ratio": (statistics.fmean(positive) / abs(statistics.fmean(negative))) if positive and negative else None,
        "turnover": (1.0 / max(1, int(horizon_days))) if count else None,
        "confidence_low": confidence_low,
        "confidence_high": confidence_high,
    }


def _snapshot_states(db: Session, *, market: str, trade_dates: set[str]) -> dict[str, dict[str, str]]:
    if not trade_dates:
        return {}
    rows = db.scalars(
        select(WorkspaceSnapshot)
        .where(
            WorkspaceSnapshot.snapshot_type == market_risk_snapshot_type(market),
            WorkspaceSnapshot.snapshot_date.in_(sorted(trade_dates)),
        )
        .order_by(WorkspaceSnapshot.snapshot_date.asc(), WorkspaceSnapshot.id.desc())
    ).all()
    states: dict[str, dict[str, str]] = {}
    for row in rows:
        date = str(row.snapshot_date or "")
        if date in states:
            continue
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        states[date] = {
            "market_regime": str((payload or {}).get("regime") or "unclassified"),
            "risk_regime": str((payload or {}).get("risk_regime") or "unclassified"),
            "buy_gate": str((payload or {}).get("buy_gate") or "UNKNOWN").upper(),
        }
    return states


def _model_input_as_of_date(run: ModelRun) -> str | None:
    config = _run_config(run)
    for key in ("input_market_date", "market_as_of_date", "as_of_date", "trade_date"):
        value = str((config or {}).get(key) or "").strip()
        if value:
            return value
    return None


def _run_config(run: ModelRun) -> dict:
    try:
        config = json.loads(run.config_json or "{}")
    except (TypeError, json.JSONDecodeError):
        config = {}
    return config if isinstance(config, dict) else {}


def _strict_oos_status(run: ModelRun, *, trade_date: str) -> tuple[bool, str, int | None]:
    """Decide OOS eligibility from the run's immutable split protocol.

    Older runs did not record a protocol and retain the conservative legacy
    rule. New runs use a walk-forward protocol where every score is generated
    before its label is observable; the label purge length is recorded for
    audit but does not delay the already-forward prediction date.
    """
    config = _run_config(run)
    purge_gap_days = config.get("purge_gap_days")
    try:
        purge_gap_days = max(0, int(purge_gap_days)) if purge_gap_days is not None else None
    except (TypeError, ValueError):
        purge_gap_days = None
    protocol = str(config.get("evaluation_protocol") or "").strip().lower()
    oos_start = str(config.get("oos_start_date") or run.test_start or "").strip()
    if protocol == "walk_forward_purged_v1":
        return bool(oos_start and trade_date >= oos_start), "walk_forward_purged_v1", purge_gap_days
    if oos_start:
        return bool(trade_date >= oos_start and (not run.train_end or trade_date > str(run.train_end))), "date_split_v1", purge_gap_days
    return bool(run.train_end and trade_date > str(run.train_end)), "legacy_train_end", purge_gap_days


def _activation_status(*, strict_sample_count: int, strict_coverage_days: int, strict_metrics: dict[int, dict]) -> str:
    """Return a governance state, never an automatic production promotion.

    Promotion needs a state-matched baseline comparison, which is intentionally
    a separate review action. This prevents a small positive sample from
    silently becoming a production BUY model.
    """
    if strict_coverage_days < STRICT_OOS_MIN_COVERAGE_DAYS or strict_sample_count < STRICT_OOS_MIN_SAMPLES:
        return "observation_insufficient_oos"
    primary = strict_metrics.get(5) or next(iter(strict_metrics.values()), {})
    if primary.get("avg_return") is None:
        return "observation_no_measurable_return"
    if float(primary.get("avg_return") or 0.0) <= 0:
        return "observation_negative_net"
    if primary.get("max_drawdown") is not None and float(primary["max_drawdown"]) <= -20.0:
        return "observation_drawdown_breach"
    return "eligible_for_champion_review"


def _selected_prediction_rows(
    db: Session,
    *,
    run: ModelRun,
    market: str,
    recent_trade_dates: int,
    top_n: int,
) -> list[tuple[Prediction, Symbol]]:
    date_rows = db.execute(
        select(Prediction.trade_date)
        .join(Symbol, Symbol.id == Prediction.symbol_id)
        .where(Prediction.model_run_id == run.id, Symbol.market == market)
        .distinct()
        .order_by(Prediction.trade_date.desc())
        .limit(max(1, int(recent_trade_dates)))
    ).all()
    trade_dates = [str(row[0]) for row in date_rows]
    if not trade_dates:
        return []
    rows = db.execute(
        select(Prediction, Symbol)
        .join(Symbol, Symbol.id == Prediction.symbol_id)
        .where(Prediction.model_run_id == run.id, Symbol.market == market, Prediction.trade_date.in_(trade_dates))
        .order_by(Prediction.trade_date.desc(), Prediction.rank_value.asc(), Prediction.score.desc(), Symbol.ticker.asc())
    ).all()
    selected: list[tuple[Prediction, Symbol]] = []
    per_date: dict[str, int] = defaultdict(int)
    for prediction, symbol in rows:
        date = str(prediction.trade_date)
        if per_date[date] >= max(1, int(top_n)):
            continue
        per_date[date] += 1
        selected.append((prediction, symbol))
    return selected


def _metric_record(
    evaluation_id: int,
    *,
    horizon_days: int,
    metric_scope: str,
    state: dict[str, str] | None,
    samples: list[dict],
    round_trip_cost_bps: float,
) -> ModelEvaluationMetric:
    summary = summarize_evaluation_samples(samples, horizon_days=horizon_days, round_trip_cost_bps=round_trip_cost_bps)
    state = state or {}
    return ModelEvaluationMetric(
        model_evaluation_id=evaluation_id,
        horizon_days=int(horizon_days),
        metric_scope=metric_scope,
        market_regime=state.get("market_regime"),
        risk_regime=state.get("risk_regime"),
        buy_gate=state.get("buy_gate"),
        sample_count=int(summary["sample_count"]),
        hit_rate=summary["hit_rate"],
        avg_return=summary["avg_return"],
        median_return=summary["median_return"],
        gross_avg_return=summary["gross_avg_return"],
        avg_drawdown=summary["avg_drawdown"],
        max_drawdown=summary["max_drawdown"],
        profit_loss_ratio=summary["profit_loss_ratio"],
        turnover=summary["turnover"],
        confidence_low=summary["confidence_low"],
        confidence_high=summary["confidence_high"],
        metrics_json=json.dumps(summary, ensure_ascii=False),
        created_at=app_now_iso(),
    )


def evaluate_model_runs(
    db: Session,
    *,
    markets: list[str] | None = None,
    model_run_id: int | None = None,
    recent_runs: int = 4,
    recent_trade_dates: int = 12,
    top_n: int = 20,
    horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
    round_trip_cost_bps: float = 20.0,
    source_job_id: int | None = None,
) -> dict:
    """Evaluate stored predictions without re-training or using current risk labels for old dates."""
    target_markets = [str(item).upper() for item in (markets or ["CN", "US"]) if str(item).upper() in {"CN", "US"}]
    target_markets = list(dict.fromkeys(target_markets)) or ["CN", "US"]
    normalized_horizons = tuple(sorted({max(1, int(value)) for value in horizons})) or DEFAULT_HORIZONS
    run_stmt = select(ModelRun).where(ModelRun.status == "success").order_by(ModelRun.id.desc())
    if model_run_id is not None:
        run_stmt = run_stmt.where(ModelRun.id == int(model_run_id))
    elif len(target_markets) == 1:
        run_stmt = run_stmt.where(ModelRun.market.in_([target_markets[0], "MIXED"]))
    runs = list(db.scalars(run_stmt.limit(1 if model_run_id is not None else max(1, int(recent_runs)))).all())
    evaluations: list[dict] = []
    history_cache: dict[tuple[str, str], list[dict]] = {}
    excluded_discontinuity_count = 0

    for run in runs:
        run_markets = target_markets if str(run.market or "").upper() in {"", "MIXED", "ALL"} else [str(run.market).upper()]
        for market in run_markets:
            if market not in target_markets:
                continue
            selected = _selected_prediction_rows(
                db, run=run, market=market, recent_trade_dates=recent_trade_dates, top_n=top_n
            )
            state_by_date = _snapshot_states(db, market=market, trade_dates={str(row[0].trade_date) for row in selected})
            samples_by_horizon: dict[int, list[dict]] = {horizon: [] for horizon in normalized_horizons}
            for prediction, symbol in selected:
                trade_date = str(prediction.trade_date)
                ticker = str(symbol.ticker or "").upper()
                key = (market, ticker)
                if key not in history_cache:
                    history_cache[key] = load_lake_price_history(market=market, ticker=ticker, limit=320)
                state = state_by_date.get(
                    trade_date,
                    {"market_regime": "unclassified", "risk_regime": "unclassified", "buy_gate": "UNKNOWN"},
                )
                for horizon in normalized_horizons:
                    outcome = _history_outcome(history_cache[key], trade_date=trade_date, horizon_days=horizon)
                    if outcome is None:
                        continue
                    if outcome.get("excluded_reason"):
                        excluded_discontinuity_count += 1
                        continue
                    is_strict_oos, oos_protocol, purge_gap_days = _strict_oos_status(run, trade_date=trade_date)
                    samples_by_horizon[horizon].append(
                        {
                            **outcome,
                            "trade_date": trade_date,
                            "ticker": ticker,
                            "market_regime": state["market_regime"],
                            "risk_regime": state["risk_regime"],
                            "buy_gate": state["buy_gate"],
                            "is_out_of_sample": is_strict_oos,
                            "oos_protocol": oos_protocol,
                            "purge_gap_days": purge_gap_days,
                        }
                    )
            any_samples = [sample for samples in samples_by_horizon.values() for sample in samples]
            strict_samples_by_horizon = {
                horizon: [sample for sample in samples if sample["is_out_of_sample"]]
                for horizon, samples in samples_by_horizon.items()
            }
            strict_samples = [sample for samples in strict_samples_by_horizon.values() for sample in samples]
            sample_dates = sorted({str(sample["trade_date"]) for sample in strict_samples})
            oos_count = len(strict_samples)
            oos_coverage_days = len({str(sample["trade_date"]) for sample in strict_samples})
            strict_metrics = {
                horizon: summarize_evaluation_samples(samples, horizon_days=horizon, round_trip_cost_bps=round_trip_cost_bps)
                for horizon, samples in strict_samples_by_horizon.items()
            }
            run_config = _run_config(run)
            purge_gap_days = next((sample.get("purge_gap_days") for sample in strict_samples if sample.get("purge_gap_days") is not None), None)
            if purge_gap_days is None:
                purge_gap_days = run_config.get("purge_gap_days")
            try:
                purge_gap_days = max(0, int(purge_gap_days)) if purge_gap_days is not None else None
            except (TypeError, ValueError):
                purge_gap_days = None
            activation_status = _activation_status(
                strict_sample_count=len({(sample["ticker"], sample["trade_date"]) for sample in strict_samples}),
                strict_coverage_days=oos_coverage_days,
                strict_metrics=strict_metrics,
            )
            summary = {
                "model_name": run.name,
                "model_type": run.model_type,
                "selected_prediction_count": len(selected),
                "measured_sample_count": len(any_samples),
                "strict_oos_measured_sample_count": len(strict_samples),
                "market_state_coverage_count": sum(1 for sample in strict_samples if sample["market_regime"] != "unclassified"),
                "out_of_sample_count": oos_count,
                "out_of_sample_coverage_days": oos_coverage_days,
                "oos_protocol": next((sample.get("oos_protocol") for sample in strict_samples), None)
                or str(run_config.get("evaluation_protocol") or "legacy_train_end"),
                "purge_gap_days": purge_gap_days,
                "benchmark_status": "not_available_in_v1",
                "activation_status": activation_status,
                "excluded_corporate_action_paths": excluded_discontinuity_count,
                "corporate_action_jump_threshold_pct": CORPORATE_ACTION_JUMP_PCT,
                "horizons": list(normalized_horizons),
            }
            evaluation = ModelEvaluation(
                model_run_id=run.id,
                source_job_id=source_job_id,
                market=market,
                evaluation_type="prediction_forward_return",
                input_as_of_date=_model_input_as_of_date(run),
                sample_start_date=sample_dates[0] if sample_dates else None,
                sample_end_date=sample_dates[-1] if sample_dates else None,
                is_out_of_sample=1 if strict_samples and oos_count == len(any_samples) else 0,
                oos_sample_count=len({(sample["ticker"], sample["trade_date"]) for sample in strict_samples}),
                oos_coverage_days=oos_coverage_days,
                purge_gap_days=purge_gap_days,
                benchmark_avg_return=None,
                universe_version=str(run_config.get("universe_version") or run.universe or "") or None,
                activation_status=activation_status,
                includes_costs=1,
                round_trip_cost_bps=float(round_trip_cost_bps),
                sample_count=len({(sample["ticker"], sample["trade_date"]) for sample in strict_samples}),
                status="success" if strict_samples else "partial",
                config_json=json.dumps(
                    {
                        "recent_trade_dates": int(recent_trade_dates),
                        "top_n": int(top_n),
                        "horizons": list(normalized_horizons),
                        "round_trip_cost_bps": float(round_trip_cost_bps),
                        "strict_oos_only": True,
                    },
                    ensure_ascii=False,
                ),
                summary_json=json.dumps(summary, ensure_ascii=False),
                created_at=app_now_iso(),
                finished_at=app_now_iso(),
            )
            db.add(evaluation)
            db.flush()
            for horizon, samples in strict_samples_by_horizon.items():
                db.add(_metric_record(evaluation.id, horizon_days=horizon, metric_scope="overall", state=None, samples=samples, round_trip_cost_bps=round_trip_cost_bps))
                state_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
                for sample in samples:
                    state_groups[(sample["market_regime"], sample["risk_regime"], sample["buy_gate"])].append(sample)
                for (regime, risk_regime, buy_gate), grouped_samples in state_groups.items():
                    db.add(
                        _metric_record(
                            evaluation.id,
                            horizon_days=horizon,
                            metric_scope=f"market_state:{regime}:{risk_regime}:{buy_gate}",
                            state={"market_regime": regime, "risk_regime": risk_regime, "buy_gate": buy_gate},
                            samples=grouped_samples,
                            round_trip_cost_bps=round_trip_cost_bps,
                        )
                    )
                # Keep exploratory records auditable without mixing them into
                # the production-facing `overall` strict-OOS metrics.
                if len(samples) != len(samples_by_horizon[horizon]):
                    db.add(
                        _metric_record(
                            evaluation.id,
                            horizon_days=horizon,
                            metric_scope="observation_all_predictions",
                            state=None,
                            samples=samples_by_horizon[horizon],
                            round_trip_cost_bps=round_trip_cost_bps,
                        )
                    )
            evaluations.append({"id": evaluation.id, "model_run_id": run.id, "market": market, "status": evaluation.status, **summary})
    db.commit()
    successful = sum(1 for row in evaluations if row["status"] == "success")
    status = "success" if evaluations and successful == len(evaluations) else "partial" if evaluations else "empty"
    return {
        "status": status,
        "markets": target_markets,
        "evaluations_created": len(evaluations),
        "successful_evaluations": successful,
        "evaluations": evaluations,
        "message": f"Persisted {len(evaluations)} structured model evaluation(s), {successful} with measurable samples.",
    }


def list_latest_model_evaluations(db: Session, *, market: str = "ALL", limit: int = 20) -> list[dict]:
    stmt = (
        select(ModelEvaluation, ModelRun)
        .join(ModelRun, ModelRun.id == ModelEvaluation.model_run_id)
        .where(ModelEvaluation.status.in_(("success", "partial")))
        .order_by(ModelEvaluation.id.desc())
    )
    market_code = str(market or "ALL").upper()
    if market_code in {"CN", "US"}:
        stmt = stmt.where(ModelEvaluation.market == market_code)
    rows = db.execute(stmt.limit(max(1, int(limit)))).all()
    result: list[dict] = []
    for evaluation, run in rows:
        metrics = db.scalars(
            select(ModelEvaluationMetric)
            .where(ModelEvaluationMetric.model_evaluation_id == evaluation.id)
            .order_by(ModelEvaluationMetric.horizon_days.asc(), ModelEvaluationMetric.metric_scope.asc())
        ).all()
        def metric_payload(item: ModelEvaluationMetric) -> dict:
            return {
                "horizon_days": item.horizon_days,
                "metric_scope": item.metric_scope,
                "market_regime": item.market_regime,
                "risk_regime": item.risk_regime,
                "buy_gate": item.buy_gate,
                "sample_count": item.sample_count,
                "hit_rate": item.hit_rate,
                "avg_return": item.avg_return,
                "median_return": item.median_return,
                "gross_avg_return": item.gross_avg_return,
                "avg_drawdown": item.avg_drawdown,
                "max_drawdown": item.max_drawdown,
                "profit_loss_ratio": item.profit_loss_ratio,
                "turnover": item.turnover,
                "confidence_low": item.confidence_low,
                "confidence_high": item.confidence_high,
            }
        result.append(
            {
                "id": evaluation.id,
                "model_run_id": run.id,
                "model_name": run.name,
                "model_type": run.model_type,
                "market": evaluation.market,
                "status": evaluation.status,
                "sample_count": evaluation.sample_count,
                "oos_sample_count": evaluation.oos_sample_count,
                "oos_coverage_days": evaluation.oos_coverage_days,
                "purge_gap_days": evaluation.purge_gap_days,
                "benchmark_avg_return": evaluation.benchmark_avg_return,
                "universe_version": evaluation.universe_version,
                "activation_status": evaluation.activation_status,
                "round_trip_cost_bps": evaluation.round_trip_cost_bps,
                "sample_start_date": evaluation.sample_start_date,
                "sample_end_date": evaluation.sample_end_date,
                "is_out_of_sample": bool(evaluation.is_out_of_sample),
                "summary": json.loads(evaluation.summary_json or "{}"),
                "metrics": [metric_payload(item) for item in metrics if item.metric_scope == "overall"],
                "market_state_metrics": [metric_payload(item) for item in metrics if item.metric_scope != "overall"],
            }
        )
    return result


def latest_model_activation_statuses(db: Session, *, model_run_ids: list[int] | set[int] | tuple[int, ...]) -> dict[int, str]:
    """Return the newest governance state for each supplied model run.

    Missing evaluations deliberately resolve to ``unverified`` at callers, so
    a technical or fundamental template cannot bypass the evidence gate.
    """
    normalized_ids = sorted({int(item) for item in model_run_ids if int(item) > 0})
    if not normalized_ids:
        return {}
    rows = db.scalars(
        select(ModelEvaluation)
        .where(ModelEvaluation.model_run_id.in_(normalized_ids))
        .where(ModelEvaluation.status.in_(("success", "partial")))
        .order_by(ModelEvaluation.model_run_id.asc(), ModelEvaluation.id.desc())
    ).all()
    result: dict[int, str] = {}
    for row in rows:
        result.setdefault(int(row.model_run_id), str(row.activation_status or "unverified"))
    return result
