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
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.statistical_inference import day_clustered_hit_rate_ci, significance_report
from app.models.tables import (
    ModelEvaluation,
    ModelEvaluationMetric,
    ModelRun,
    Prediction,
    PredictionArtifact,
    Symbol,
    WorkspaceSnapshot,
)
from app.services.market_lake import (
    load_lake_price_history,
    load_lake_price_history_with_provenance,
)
from app.services.market_risk import market_risk_snapshot_type
from app.services.prediction_artifacts import read_prediction_artifact_rows
from app.services.price_basis import preferred_close
from app.services.repositories.research import FundamentalSnapshotRepository
from app.services.time_utils import app_now_iso
from app.services.stock_selection.labels import ExecutableLabel
from app.services.stock_selection.executable_outcomes import OUTCOME_VERSION, FILL_COST_OUTCOME_VERSION
from app.services.execution_costs import FillCostModel
from app.services.execution_reconciliation import (
    VERSION as RECONCILED_VERSION, execution_contract, replay_candidate, outcome_counts,
    validate_contract, implementation_identity,
)


DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
CORPORATE_ACTION_JUMP_PCT = 80.0
STRICT_OOS_MIN_COVERAGE_DAYS = 20
STRICT_OOS_MIN_SAMPLES = 100
# Keep enough prediction dates for the longest forward-return horizon to mature
# while still leaving the challenger gate's required number of OOS dates. A
# 12-date scheduled window could never satisfy the 20-date gate.
SCHEDULED_EVALUATION_TRADE_DATES = STRICT_OOS_MIN_COVERAGE_DAYS + max(DEFAULT_HORIZONS)


def _tail_drawdown_1pct(drawdowns: list[float]) -> float:
    """Gate statistic: CVaR-style mean of the worst tail of path drawdowns.

    The legacy gate consumed the single worst sample, an extreme-value
    statistic that destabilizes as the OOS sample grows and structurally
    favored the old clamped composite labels (-0.35 floor made a breach
    nearly impossible there, while unclamped executable net labels breach
    almost by construction). The 1% tail mean converges with sample size
    and stays comparable across label protocols. v2 hardens the floor:
    from five samples on, the tail consumes at least the worst five
    draws (CVaR@95% at the 100-row scheduled metrics base of five
    scheduled days x top-20 picks), so one catastrophic sample can no
    longer decide the gate on its own; below five samples it remains
    the single worst sample, preserving legacy semantics for small
    diagnostic fixtures.
    """
    if not drawdowns:
        return 0.0
    ordered = sorted(drawdowns)
    if len(ordered) < 5:
        return ordered[0]
    tail_size = max(5, -(-len(ordered) // 100))
    return sum(ordered[:tail_size]) / tail_size


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _history_outcome(history: list[dict], *, trade_date: str, horizon_days: int) -> dict | None:
    """Return forward gross return and path drawdown in percent for one pick.

    P0 closeout: the entry basis is the next bar's open -- the price a T+1
    trader could actually pay -- gap-carried onto the stored (possibly
    adjusted) close series.  Entering at the signal-day close overstated
    returns for momentum-ranked picks that systematically gap up.
    """
    target_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") == trade_date), None)
    if target_index is None or target_index + horizon_days >= len(history):
        return None
    signal_row = history[target_index]
    next_row = history[target_index + 1]
    # R2: close-to-close legs prefer the adjusted view; the overnight gap stays
    # a raw open / raw close ratio so its meaning is unchanged.
    signal_close = preferred_close(signal_row)
    next_open = _number(next_row.get("open"))
    next_close = preferred_close(next_row)
    signal_raw_close = _number(signal_row.get("close"))
    entry = next_close or signal_close
    if signal_close and next_open and signal_raw_close:
        gap_ratio = next_open / signal_raw_close
        if 0.0 < gap_ratio < 3.0:
            entry = signal_close * gap_ratio
    exit_row = history[target_index + horizon_days]
    exit_price = preferred_close(exit_row)
    if not entry or entry <= 0 or exit_price is None:
        return None
    path = history[target_index : target_index + horizon_days + 1]
    path_closes = [preferred_close(row) for row in path]
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
        _number(row.get("low")) or preferred_close(row)
        for row in history[target_index : target_index + horizon_days + 1]
    ]
    valid_lows = [value for value in lows if value is not None]
    return {
        "gross_return_pct": ((exit_price / entry) - 1.0) * 100.0,
        "drawdown_pct": ((min(valid_lows) / entry) - 1.0) * 100.0 if valid_lows else None,
    }


def summarize_evaluation_samples(samples: list[dict], *, horizon_days: int, round_trip_cost_bps: float) -> dict:
    """Calculate net-of-cost performance; intentionally pure for regression tests."""
    cost_bps = float(round_trip_cost_bps)
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    cost_pct = cost_bps / 100.0
    valid_samples = [item for item in samples if not item.get("excluded_reason")
                     and item.get("tradable") is not False and _number(item.get("gross_return_pct")) is not None]
    gross_returns = [float(item["gross_return_pct"]) for item in valid_samples]
    net_returns = [value - cost_pct for value in gross_returns]
    trade_dates = [str(item.get("trade_date") or "") for item in valid_samples]
    drawdowns = [float(item["drawdown_pct"]) for item in valid_samples if _number(item.get("drawdown_pct")) is not None]
    return _summarize_return_vectors(gross_returns, net_returns, drawdowns,
        horizon_days=horizon_days, round_trip_cost_bps=cost_bps, selected_count=len(samples),
        trade_dates=trade_dates)


DEFAULT_COST_SENSITIVITY_LADDER_BPS = (20.0, 50.0, 80.0)


def cost_sensitivity_ladder(
    samples: list[dict],
    *,
    horizon_days: int,
    round_trip_cost_bps: float,
    ladder_bps: tuple[float, ...] = DEFAULT_COST_SENSITIVITY_LADDER_BPS,
) -> list[dict]:
    """Reprice one measured sample set at escalating round-trip cost levels.

    P0 acceptance requires the scheduled CN evaluation to prove its edge is not
    an artifact of one favorable cost choice: the same measured paths are
    evaluated at an optimistic (20bps), the scheduled nominal (50bps) and a
    punitive (80bps) round trip. Gross returns are never modified; net returns
    and hit rates may only decay as costs rise.
    """
    base_bps = float(round_trip_cost_bps)
    if not math.isfinite(base_bps) or base_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    levels: list[float] = []
    for value in ladder_bps:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("ladder_bps entries must be finite and non-negative")
        if number not in levels:
            levels.append(number)
    if base_bps not in levels:
        levels.append(base_bps)
        levels.sort()
    rows: list[dict] = []
    for level_bps in levels:
        summary = summarize_evaluation_samples(
            samples, horizon_days=horizon_days, round_trip_cost_bps=level_bps
        )
        rows.append(
            {
                "round_trip_cost_bps": level_bps,
                "sample_count": summary["sample_count"],
                "hit_rate": summary["hit_rate"],
                "net_avg_return": summary["avg_return"],
                "gross_avg_return": summary["gross_avg_return"],
            }
        )
    base_net = next(
        (row["net_avg_return"] for row in rows if row["round_trip_cost_bps"] == base_bps),
        None,
    )
    for row in rows:
        row["net_avg_return_delta_vs_base"] = (
            row["net_avg_return"] - base_net
            if base_net is not None and row["net_avg_return"] is not None
            else None
        )
    return rows


def _summarize_return_vectors(gross_returns: list[float], net_returns: list[float], drawdowns: list[float],
                              *, horizon_days: int, round_trip_cost_bps: float, selected_count: int,
                              trade_dates: list[str] | None = None) -> dict:
    """Aggregate measured vectors without interpreting or reapplying costs.

    ``confidence_low`` / ``confidence_high`` keep their historical meaning (an
    iid normal interval around the hit rate) for schema stability, but the label
    now says so explicitly.  ``hit_rate_ci95_clustered`` is the day-clustered
    moving-block bootstrap interval: picks sharing a signal date share a market
    move, so the iid interval understates uncertainty.  Promotion-style
    thresholds must consume the clustered lower bound.
    """
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
    clustered = None
    if trade_dates is not None and len(trade_dates) == count:
        clustered = day_clustered_hit_rate_ci(
            [value > 0 for value in net_returns],
            trade_dates,
            block_length=max(1, int(horizon_days)),
        )
    clustered_ci = clustered.get("ci95") if clustered else None
    significance = significance_report(net_returns, horizon_days=horizon_days) if len(net_returns) >= 2 else None
    return {
        "horizon_days": int(horizon_days),
        "sample_count": count,
        "cost_bps": float(round_trip_cost_bps),
        "hit_rate": hit_rate,
        "avg_return": statistics.fmean(net_returns) if net_returns else None,
        "avg_return_ci95_iid": (significance or {}).get("iid_ci95"),
        "avg_return_ci95_newey_west": (significance or {}).get("newey_west_ci95"),
        "avg_return_ci95_block_bootstrap": (significance or {}).get("block_bootstrap_ci95"),
        "significance_method": (significance or {}).get("ci_method"),
        "median_return": statistics.median(net_returns) if net_returns else None,
        "gross_avg_return": statistics.fmean(gross_returns) if gross_returns else None,
        "avg_drawdown": statistics.fmean(drawdowns) if drawdowns else None,
        "max_drawdown": round(_tail_drawdown_1pct(drawdowns), 4),
        "worst_sample_drawdown": min(drawdowns) if drawdowns else None,
        "activation_gate_version": "tail_drawdown_1pct_v2",
        "profit_loss_ratio": (statistics.fmean(positive) / abs(statistics.fmean(negative))) if positive and negative else None,
        "turnover": (1.0 / max(1, int(horizon_days))) if count else None,
        "confidence_low": confidence_low,
        "confidence_high": confidence_high,
        "confidence_method": "iid_normal_diagnostic_not_promotion_evidence",
        "hit_rate_ci95_iid": [confidence_low, confidence_high],
        "hit_rate_ci95_clustered": list(clustered_ci) if clustered_ci else None,
        "hit_rate_ci_lower_bound_clustered": clustered_ci[0] if clustered_ci else None,
        "hit_rate_ci_cluster_method": (clustered or {}).get("method"),
        "hit_rate_ci_cluster_days": (clustered or {}).get("days"),
        "hit_rate_ci_block_length": (clustered or {}).get("block_length"),
        "hit_rate_ci_iid_is_diagnostic_only": True,
        "drawdown_semantics": "cvar95_tail_gated_v2 (mean of worst >=5 samples; worst sample kept as record)",
        "selected_sample_count": selected_count,
        "unmeasured_sample_count": selected_count - count,
    }


def summarize_executable_labels(labels: list[ExecutableLabel], *, horizon_days: int,
                                cost_bps: float | None = None, cost_model: FillCostModel | None = None) -> dict:
    """Versioned flat-cost or already-net per-fill labels; never double charge."""
    if (cost_model is None) == (cost_bps is None):
        raise ValueError("choose exactly one cost model: flat bps or per-fill")
    version = OUTCOME_VERSION if cost_model is None else FILL_COST_OUTCOME_VERSION
    expected_bps = cost_bps if cost_model is None else cost_model.nominal_round_trip_bps
    for label in labels:
        if (label.label_version != version or label.horizon_days != horizon_days
            or label.round_trip_cost_bps != expected_bps
            or label.cost_model_version != (cost_model.version if cost_model else "flat_round_trip_bps_v1")
            or label.cost_model_hash != (cost_model.model_hash if cost_model else None)):
            raise ValueError("mixed executable label protocol, horizon or costs")
    if cost_model is None:
        result = summarize_evaluation_samples([
            {"gross_return_pct": label.gross_return * 100 if label.gross_return is not None else None,
             "drawdown_pct": label.path_drawdown * 100 if label.path_drawdown is not None else None,
             "trade_date": label.signal_date.isoformat() if label.signal_date else None,
             "tradable": label.tradable, "excluded_reason": label.exclusion_reason}
            for label in labels
        ], horizon_days=horizon_days, round_trip_cost_bps=cost_bps)
    else:
        measured = [label for label in labels if label.tradable and not label.exclusion_reason]
        if any(_number(label.net_return) is None or _number(label.gross_return) is None for label in measured):
            raise ValueError("measurable fill-cost labels require finite gross and net returns")
        result = _summarize_return_vectors(
            [label.gross_return * 100 for label in measured],
            [label.net_return * 100 for label in measured],
            [label.path_drawdown * 100 for label in measured if _number(label.path_drawdown) is not None],
            horizon_days=horizon_days, round_trip_cost_bps=expected_bps, selected_count=len(labels))
    result["outcome_protocol"] = version
    result["cost_model_version"] = cost_model.version if cost_model else "flat_round_trip_bps_v1"
    result["cost_model_hash"] = cost_model.model_hash if cost_model else None
    result["cost_bps_semantics"] = "nominal_approximation_not_deduction" if cost_model else "flat_return_deduction"
    result["return_denominator"] = "entry_notional_plus_entry_fee" if cost_model else "raw_entry_reference"
    result["excluded_reasons"] = {
        reason: sum(label.exclusion_reason == reason for label in labels)
        for reason in sorted({label.exclusion_reason for label in labels if label.exclusion_reason})
    }
    return result


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


def _run_prediction_horizon_days(run: ModelRun) -> int | None:
    """Resolve the run's declared forward horizon, if any.

    Reads the same ``prediction_horizon_days`` key that
    ``stock_selection.reliability_artifacts.resolve_run_horizon`` filters on, so
    the evaluation and the downstream reliability producer agree on the horizon
    the run scores.  Returns ``None`` when the run does not declare
    ``prediction_horizon_days`` (legacy runs), so the caller keeps its pure
    default set instead of inventing a horizon; the producer's own fallback is
    5, which the default scheduled set already contains.
    """
    try:
        horizon = int(_run_config(run).get("prediction_horizon_days"))
    except (TypeError, ValueError):
        return None
    return horizon if horizon >= 1 else None


def _horizons_with_run_horizon(
    horizons: tuple[int, ...], run: ModelRun
) -> tuple[tuple[int, ...], int | None]:
    """Union the caller's horizon set with the run's own prediction horizon.

    The reliability producer filters CLOSED candidate outcomes by the run's
    ``prediction_horizon_days``; if the evaluation never scored that horizon the
    producer sees zero matured rows and silently never writes an artifact.
    Including the run horizon keeps every existing horizon metric unchanged
    while guaranteeing the producer/evaluation parity.
    """
    run_horizon = _run_prediction_horizon_days(run)
    if run_horizon is None or run_horizon in horizons:
        return horizons, run_horizon
    return tuple(sorted({*horizons, run_horizon})), run_horizon


# Target profiles whose labels are next-session-open net-return fills.  The
# trainer only persists ``execution_contract`` for the reconciled profile, so
# runs trained with the default executable profile carry the identical
# execution semantics but a null contract.  These two profiles are the only
# ones whose labels are defined *by* that contract (entry rule + FillCostModel);
# the legacy composite profile has no execution semantics and must never be
# auto-derived here.
EXECUTABLE_CONTRACT_TARGET_PROFILES = frozenset(
    {FILL_COST_OUTCOME_VERSION, RECONCILED_VERSION}
)


def _manifest_execution_cost(run: ModelRun) -> dict | None:
    """Read the recorded cost tuple from the run's prediction artifact manifest.

    Path contract: ``<artifacts_dir>/prediction_runs/model_run_id=<id>/manifest.json``.
    This is the authoritative source the trainer wrote; it exists for every run
    published with ``prediction_artifacts_enabled``.  Any missing/unreadable/
    malformed manifest yields ``None`` so the caller stays fail-closed instead
    of guessing a default cost.
    """
    try:
        run_id = int(getattr(run, "id", None))
    except (TypeError, ValueError):
        return None
    if run_id <= 0:
        return None
    manifest_path = (
        get_settings().artifacts_dir
        / "prediction_runs"
        / f"model_run_id={run_id}"
        / "manifest.json"
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    cost_setting = payload.get("execution_cost_bps")
    return cost_setting if isinstance(cost_setting, dict) else None


def resolve_execution_contract(run: ModelRun, market: str) -> dict | None:
    """Return the frozen execution contract for ``run``/``market``, or None.

    Preference order:
    1. The contract persisted in the run config (the reconciled trainer
       profile).  Used verbatim.
    2. For runs whose ``target_profile`` is an executable next-open net-return
       label family, re-derive the *identical* contract from the cost tuple the
       run itself recorded (``execution_cost_bps``).  This is not a fabricated
       contract: ``execution_contract`` is a pure function of market + cost, and
       the trainer labelled those samples with exactly that entry rule and cost
       model.  It restores the audit link the trainer omitted for the default
       profile without changing any label.
    3. If the config predates the fix and has no ``execution_cost_bps``, fall
       back to the run's prediction artifact manifest, which carries the same
       cost tuple the trainer recorded.  A missing/unreadable manifest returns
       None rather than a guessed default.
    4. Otherwise None.  A run with no executable label family or no recorded
       cost is left unverified rather than silently blessed.
    """
    config = _run_config(run)
    persisted = config.get("execution_contract")
    if isinstance(persisted, dict):
        return persisted
    profile = str(config.get("target_profile") or "").strip().lower()
    if profile not in EXECUTABLE_CONTRACT_TARGET_PROFILES:
        return None
    cost_setting = config.get("execution_cost_bps")
    if not isinstance(cost_setting, dict):
        # Backward compatibility for runs persisted before the config fix: the
        # artifact manifest holds the authoritative cost tuple.
        cost_setting = _manifest_execution_cost(run)
    if not isinstance(cost_setting, dict):
        return None
    try:
        cost = FillCostModel(
            float(cost_setting["commission_bps_one_way"]),
            float(cost_setting["slippage_bps_one_way"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return execution_contract(market, cost)


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
    if protocol in {"walk_forward_purged_v1", "walk_forward_purged_v2"}:
        return bool(oos_start and trade_date >= oos_start), protocol, purge_gap_days
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
    cold_reads_enabled: bool | None = None,
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
        if cold_reads_enabled is None:
            cold_reads_enabled = bool(get_settings().prediction_cold_reads_enabled)
        if not cold_reads_enabled:
            return []
        artifact = db.scalar(
            select(PredictionArtifact).where(
                PredictionArtifact.model_run_id == int(run.id),
                PredictionArtifact.status == "verified",
            )
        )
        if artifact is None:
            return []
        cold_rows = read_prediction_artifact_rows(artifact.artifact_path, include_details=False)
        symbol_ids = sorted({int(row["symbol_id"]) for row in cold_rows})
        symbols = {
            int(symbol.id): symbol
            for symbol in db.scalars(select(Symbol).where(Symbol.id.in_(symbol_ids), Symbol.market == market)).all()
        }
        eligible_rows = [row for row in cold_rows if int(row["symbol_id"]) in symbols]
        cold_dates = sorted({str(row["trade_date"]) for row in eligible_rows}, reverse=True)[
            : max(1, int(recent_trade_dates))
        ]
        selected: list[tuple[Prediction, Symbol]] = []
        per_date: dict[str, int] = defaultdict(int)
        for row in sorted(
            (row for row in eligible_rows if str(row["trade_date"]) in cold_dates),
            key=lambda row: (
                str(row["trade_date"]),
                -float(row.get("rank_value") or 0.0),
                float(row.get("score") or 0.0),
                -int(row["symbol_id"]),
            ),
            reverse=True,
        ):
            trade_date = str(row["trade_date"])
            if per_date[trade_date] >= max(1, int(top_n)):
                continue
            per_date[trade_date] += 1
            prediction = SimpleNamespace(
                id=None,
                model_run_id=int(run.id),
                symbol_id=int(row["symbol_id"]),
                trade_date=trade_date,
                score=row.get("score"),
                rank_value=row.get("rank_value"),
            )
            selected.append((prediction, symbols[int(row["symbol_id"])]))
        return selected
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


STRATIFICATION_ADV_WINDOW_DAYS = 20
INDUSTRY_UNKNOWN = "unknown"
MARKET_CAP_BUCKET_LABELS = ("small", "mid", "large")
LIQUIDITY_BUCKET_LABELS = ("low", "mid", "high")


def _trailing_dollar_adv(history: list[dict], trade_date: str, *, window: int = STRATIFICATION_ADV_WINDOW_DAYS) -> float | None:
    """Trailing dollar ADV (close x volume) up to and including ``trade_date``."""
    values: list[float] = []
    for bar in reversed(history):
        bar_date = str(bar.get("date") or "")[:10]
        if not bar_date or bar_date > trade_date:
            continue
        close = _number(bar.get("close"))
        volume = _number(bar.get("volume"))
        if close and volume:
            values.append(close * volume)
        if len(values) >= window:
            break
    return statistics.fmean(values) if values else None


def _tercile_cuts(values: list[float]) -> tuple[float, float] | None:
    clean = sorted(value for value in values if value is not None)
    if len(clean) < 3:
        return None
    return clean[int(len(clean) * 0.3333)], clean[int(len(clean) * 0.6667)]


def _bucket_label(value: float | None, cuts: tuple[float, float] | None, labels: tuple[str, str, str]) -> str:
    if value is None or cuts is None:
        return "unknown"
    low_cut, high_cut = cuts
    if value <= low_cut:
        return labels[0]
    if value >= high_cut:
        return labels[2]
    return labels[1]


BENCHMARK_UNIVERSE_MAX_TICKERS = 300


def _benchmark_universe_tickers(
    db: Session,
    *,
    run: ModelRun,
    market: str,
    trade_dates: list[str],
    limit: int = BENCHMARK_UNIVERSE_MAX_TICKERS,
) -> list[str]:
    """Tickers in the run's own prediction universe for the evaluated dates.

    The equal-weight benchmark must not be built from the evaluated top-N alone
    (that would make excess identically zero), so this reads the run's full
    ranking for the same dates.  The set is capped by best (lowest) rank value
    for cost control; the cap is recorded via the returned universe size.
    """
    if not trade_dates:
        return []
    rows = db.execute(
        select(Symbol.ticker, Prediction.rank_value)
        .join(Prediction, Prediction.symbol_id == Symbol.id)
        .where(
            Prediction.model_run_id == int(run.id),
            Symbol.market == market,
            Prediction.trade_date.in_(sorted(trade_dates)),
        )
    ).all()
    best_rank: dict[str, float] = {}
    for ticker, rank_value in rows:
        ticker_code = str(ticker or "").upper()
        if not ticker_code:
            continue
        rank = _number(rank_value)
        rank = rank if rank is not None else float("inf")
        if ticker_code not in best_rank or rank < best_rank[ticker_code]:
            best_rank[ticker_code] = rank
    ordered = sorted(best_rank.items(), key=lambda item: (item[1], item[0]))
    return [ticker for ticker, _ in ordered[: max(1, int(limit))]]


def _equal_weight_benchmark_by_date(
    universe_histories: list[list[dict]],
    *,
    trade_dates: list[str],
    horizon_days: int,
) -> dict[str, float]:
    """Same-day same-universe equal-weight return, mirroring backtesting runner.

    This is the model-evaluation analogue of
    ``BacktestRunner._benchmark_returns``: for each date, average the
    close-to-close holding return of every universe member that has a
    measurable path.  Crucially the universe is the run's *prediction*
    universe, not the evaluated top-N subset, so the excess is not trivially
    zero by construction.
    """
    by_date: dict[str, float] = {}
    for trade_date in trade_dates:
        returns: list[float] = []
        for history in universe_histories:
            outcome = _history_outcome(history, trade_date=trade_date, horizon_days=horizon_days)
            value = _number((outcome or {}).get("gross_return_pct"))
            if value is not None:
                returns.append(value)
        if returns:
            by_date[trade_date] = statistics.fmean(returns)
    return by_date


def _benchmark_section(
    samples: list[dict],
    *,
    benchmark_by_date: dict[str, float],
    horizon_days: int,
    index_history: list[dict] | None,
) -> dict:
    """Excess vs a same-day equal-weight benchmark, plus an optional index overlay."""

    excess: list[float] = []
    for sample in samples:
        gross = _number(sample.get("gross_return_pct"))
        base = benchmark_by_date.get(str(sample.get("trade_date") or ""))
        if gross is None or base is None:
            continue
        excess.append(gross - base)
    index_excess: list[float] = []
    index_returns: list[float] = []
    if index_history:
        for sample in samples:
            gross = _number(sample.get("gross_return_pct"))
            index_outcome = _history_outcome(index_history, trade_date=str(sample.get("trade_date") or ""), horizon_days=horizon_days)
            index_gross = _number((index_outcome or {}).get("gross_return_pct"))
            if gross is None or index_gross is None:
                continue
            index_returns.append(index_gross)
            index_excess.append(gross - index_gross)
    status = "available_same_day_universe_equal_weight_v1"
    if not samples or not benchmark_by_date:
        status = "not_available_no_benchmark_universe"
    elif index_history and index_returns:
        status = "available_equal_weight_plus_index_overlay_v1"
    return {
        "benchmark_status": status,
        "benchmark_kind": "same_day_prediction_universe_equal_weight",
        "benchmark_dates": len(benchmark_by_date),
        "benchmark_avg_return_pct": statistics.fmean(benchmark_by_date.values()) if benchmark_by_date else None,
        "excess_avg_return_pct": statistics.fmean(excess) if excess else None,
        "excess_positive_rate_pct": (
            sum(1 for value in excess if value > 0) / len(excess) * 100.0 if excess else None
        ),
        "index_benchmark_avg_return_pct": statistics.fmean(index_returns) if index_returns else None,
        "excess_vs_index_avg_return_pct": statistics.fmean(index_excess) if index_excess else None,
        "index_overlay_available": bool(index_returns),
    }


def _stratification_groups(samples: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    """Build industry / market-cap / liquidity strata plus a shape descriptor.

    Market-cap and liquidity buckets are within-evaluation terciles, so the
    split is self-calibrating (no hard-coded currency thresholds).  A sample
    with no value lands in the explicit ``unknown`` bucket rather than being
    silently dropped.
    """
    cap_cuts = _tercile_cuts([sample.get("market_cap") for sample in samples])
    liq_cuts = _tercile_cuts([sample.get("liquidity_adv_20d") for sample in samples])
    groups: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        groups[f"industry:{sample.get('industry') or INDUSTRY_UNKNOWN}"].append(sample)
        groups[
            f"market_cap_bucket:{_bucket_label(sample.get('market_cap'), cap_cuts, MARKET_CAP_BUCKET_LABELS)}"
        ].append(sample)
        groups[
            f"liquidity_bucket:{_bucket_label(sample.get('liquidity_adv_20d'), liq_cuts, LIQUIDITY_BUCKET_LABELS)}"
        ].append(sample)
    shape = {
        "industry": sorted({scope.split(":", 1)[1] for scope in groups if scope.startswith("industry:")}),
        "market_cap_bucket": sorted(
            {scope.split(":", 1)[1] for scope in groups if scope.startswith("market_cap_bucket:")}
        ),
        "liquidity_bucket": sorted(
            {scope.split(":", 1)[1] for scope in groups if scope.startswith("liquidity_bucket:")}
        ),
        "market_cap_cut_points": list(cap_cuts) if cap_cuts else None,
        "liquidity_cut_points": list(liq_cuts) if liq_cuts else None,
    }
    return groups, shape


def _metric_record(
    evaluation_id: int,
    *,
    horizon_days: int,
    metric_scope: str,
    state: dict[str, str] | None,
    samples: list[dict],
    round_trip_cost_bps: float,
    benchmark: dict | None = None,
    extra_bucket: dict[str, str] | None = None,
) -> ModelEvaluationMetric:
    summary = summarize_evaluation_samples(samples, horizon_days=horizon_days, round_trip_cost_bps=round_trip_cost_bps)
    if benchmark:
        summary.update(benchmark)
    if extra_bucket:
        summary.update(extra_bucket)
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
    require_execution_reconciliation: bool = False,
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
        run_horizons, run_horizon_days = _horizons_with_run_horizon(normalized_horizons, run)
        run_markets = target_markets if str(run.market or "").upper() in {"", "MIXED", "ALL"} else [str(run.market).upper()]
        for market in run_markets:
            if market not in target_markets:
                continue
            selected = _selected_prediction_rows(
                db, run=run, market=market, recent_trade_dates=recent_trade_dates, top_n=top_n
            )
            if require_execution_reconciliation:
                contract = resolve_execution_contract(run, market)
                candidate_outcomes = []
                try:
                    frozen_cost = validate_contract(contract, market)
                except ValueError:
                    frozen_cost = None
                for prediction, symbol in selected:
                    key = (market, symbol.ticker)
                    if key not in history_cache:
                        history_cache[key] = (load_lake_price_history_with_provenance(
                            market=market, ticker=symbol.ticker, limit=320) if frozen_cost else [])
                    strict, _, _ = _strict_oos_status(run, trade_date=str(prediction.trade_date))
                    for horizon in run_horizons:
                        outcome = replay_candidate(
                            ticker=symbol.ticker, market=market,
                            signal_date=str(prediction.trade_date), horizon_days=horizon,
                            rows=history_cache[key], contract=contract)
                        outcome.update(model_run_id=run.id, is_out_of_sample=strict)
                        candidate_outcomes.append(outcome)
                counts = outcome_counts(candidate_outcomes)
                closed = [row for row in candidate_outcomes
                          if row['status'] == 'CLOSED' and row['is_out_of_sample']]
                verified = bool(candidate_outcomes) and not counts.get('UNVERIFIED')
                complete = verified and not counts.get('PENDING') and not counts.get('EXIT_DEFERRED')
                evaluated_status = 'success' if complete else 'partial'
                dates = {row['trade_date'] for row in closed}
                measured = {(row['ticker'], row['trade_date']) for row in closed}
                summary = {
                    "outcome_protocol": RECONCILED_VERSION,
                    "implementation": implementation_identity(),
                    "execution_verified": verified,
                    "activation_status": "observation_reconciliation_only" if verified else "observation_execution_unverified",
                    "candidate_outcomes": candidate_outcomes,
                    "selected_prediction_count": len(selected),
                    "candidate_horizon_count": len(candidate_outcomes),
                    "outcome_counts": counts,
                    "horizons": list(run_horizons),
                    "horizons_used": list(run_horizons),
                    "run_prediction_horizon_days": run_horizon_days,
                    "block_reason": None if complete else "unresolved_candidate_outcomes",
                    "legacy_fallback_used": False,
                    "cost_contract": contract,
                    "note": "Per-candidate normalized-share replay, not portfolio NAV or broker fills.",
                }
                evaluation = ModelEvaluation(
                    model_run_id=run.id, source_job_id=source_job_id, market=market,
                    evaluation_type="prediction_forward_return",
                    input_as_of_date=_model_input_as_of_date(run),
                    is_out_of_sample=int(bool(closed)), oos_sample_count=len(measured), oos_coverage_days=len(dates),
                    activation_status=summary['activation_status'],
                    includes_costs=int(frozen_cost is not None),
                    round_trip_cost_bps=frozen_cost.nominal_round_trip_bps if frozen_cost else float(round_trip_cost_bps),
                    sample_count=len(measured), status=evaluated_status,
                    config_json=json.dumps({"require_execution_reconciliation": True,
                                            "top_n": int(top_n),
                                            "recent_trade_dates": int(recent_trade_dates)}),
                    summary_json=json.dumps(summary, ensure_ascii=False),
                    created_at=app_now_iso(), finished_at=app_now_iso(),
                )
                db.add(evaluation)
                db.flush()
                for horizon in run_horizons:
                    subset = [row for row in closed if row['horizon_days'] == horizon]
                    metrics = _summarize_return_vectors(
                        [row['gross_return'] * 100 for row in subset],
                        [row['net_return'] * 100 for row in subset], [],
                        horizon_days=horizon,
                        round_trip_cost_bps=evaluation.round_trip_cost_bps,
                        selected_count=len(selected))
                    fields = {key: metrics[key] for key in (
                        'sample_count', 'hit_rate', 'avg_return', 'median_return',
                        'gross_avg_return', 'profit_loss_ratio', 'confidence_low', 'confidence_high')}
                    db.add(ModelEvaluationMetric(model_evaluation_id=evaluation.id,
                        horizon_days=horizon, metric_scope='overall', created_at=app_now_iso(), **fields))
                evaluations.append({"id": evaluation.id, "model_run_id": run.id,
                                    "market": market, "status": evaluated_status, **summary})
                continue
            state_by_date = _snapshot_states(db, market=market, trade_dates={str(row[0].trade_date) for row in selected})
            # Stratification inputs: industry is the stored symbol classification
            # (Shenwan-derived from the source feed); market cap is the latest
            # fundamental snapshot; liquidity is a trailing dollar ADV computed
            # from the already-loaded price history.
            fundamentals = FundamentalSnapshotRepository(db).list_latest_for_market(
                market, tickers=[str(symbol.ticker or "").upper() for _, symbol in selected]
            )
            market_cap_by_ticker = {
                str(row.get("ticker") or "").upper(): _number(row.get("market_cap"))
                for row in fundamentals
            }
            samples_by_horizon: dict[int, list[dict]] = {horizon: [] for horizon in run_horizons}
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
                industry = str(getattr(symbol, "industry", None) or INDUSTRY_UNKNOWN)
                market_cap = market_cap_by_ticker.get(ticker)
                liquidity_adv = _trailing_dollar_adv(history_cache[key], trade_date)
                for horizon in run_horizons:
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
                            "industry": industry,
                            "market_cap": market_cap,
                            "liquidity_adv_20d": liquidity_adv,
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
            run_config = _run_config(run)
            index_history: list[dict] | None = None
            benchmark_symbol = str(
                run_config.get("benchmark_symbol") or get_settings().backtest_benchmark_symbol or ""
            ).strip().upper()
            if benchmark_symbol:
                index_history = load_lake_price_history(market=market, ticker=benchmark_symbol, limit=320) or None
            benchmark_trade_dates = sorted({str(sample["trade_date"]) for sample in strict_samples})
            universe_tickers = _benchmark_universe_tickers(
                db, run=run, market=market, trade_dates=benchmark_trade_dates
            )
            universe_histories: list[list[dict]] = []
            for universe_ticker in universe_tickers:
                cached = history_cache.get((market, universe_ticker))
                if cached is None:
                    cached = load_lake_price_history(market=market, ticker=universe_ticker, limit=320)
                    history_cache[(market, universe_ticker)] = cached
                if cached:
                    universe_histories.append(cached)
            benchmark_by_horizon = {
                horizon: _benchmark_section(
                    samples,
                    benchmark_by_date=_equal_weight_benchmark_by_date(
                        universe_histories, trade_dates=benchmark_trade_dates, horizon_days=horizon
                    ),
                    horizon_days=horizon,
                    index_history=index_history,
                )
                for horizon, samples in strict_samples_by_horizon.items()
            }
            strict_metrics = {
                horizon: summarize_evaluation_samples(samples, horizon_days=horizon, round_trip_cost_bps=round_trip_cost_bps)
                for horizon, samples in strict_samples_by_horizon.items()
            }
            for horizon, section in benchmark_by_horizon.items():
                strict_metrics[horizon].update(section)
            primary_benchmark = (
                benchmark_by_horizon.get(5)
                or (next(iter(benchmark_by_horizon.values())) if benchmark_by_horizon else {})
            )
            primary_metrics = (
                strict_metrics.get(5)
                or (next(iter(strict_metrics.values())) if strict_metrics else {})
            )
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
            if activation_status == 'eligible_for_champion_review':
                activation_status = 'observation_execution_unverified'
            summary = {
                "outcome_protocol": "legacy_close_to_close_diagnostic_v1",
                "execution_verified": False,
                "drawdown_semantics": "cvar95_tail_gated_v2 (mean of worst >=5 samples; worst sample kept as record)",
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
                "benchmark_status": (primary_benchmark or {}).get("benchmark_status") or "not_available_no_samples",
                "benchmark_kind": (primary_benchmark or {}).get("benchmark_kind"),
                "benchmark_symbol": benchmark_symbol or None,
                "benchmark_universe_size": len(universe_histories),
                "benchmark_universe_cap": BENCHMARK_UNIVERSE_MAX_TICKERS,
                "benchmark_avg_return_pct": (primary_benchmark or {}).get("benchmark_avg_return_pct"),
                "excess_avg_return_pct": (primary_benchmark or {}).get("excess_avg_return_pct"),
                "index_benchmark_avg_return_pct": (primary_benchmark or {}).get("index_benchmark_avg_return_pct"),
                "excess_vs_index_avg_return_pct": (primary_benchmark or {}).get("excess_vs_index_avg_return_pct"),
                "benchmark_by_horizon": benchmark_by_horizon,
                "hit_rate_ci95_clustered": (primary_metrics or {}).get("hit_rate_ci95_clustered"),
                "hit_rate_ci_lower_bound_clustered": (primary_metrics or {}).get("hit_rate_ci_lower_bound_clustered"),
                "hit_rate_ci_cluster_method": (primary_metrics or {}).get("hit_rate_ci_cluster_method"),
                "hit_rate_ci_iid_is_diagnostic_only": True,
                "confidence_method": "iid_normal_diagnostic_not_promotion_evidence",
                "activation_status": activation_status,
                "excluded_corporate_action_paths": excluded_discontinuity_count,
                "corporate_action_jump_threshold_pct": CORPORATE_ACTION_JUMP_PCT,
                "horizons": list(run_horizons),
                "horizons_used": list(run_horizons),
                "run_prediction_horizon_days": run_horizon_days,
                "cost_bps": float(round_trip_cost_bps),
                "cost_basis": "round_trip",
                "cost_source": (
                    "evaluate_model_runs(round_trip_cost_bps=...) argument; the scheduled chain passes "
                    "Settings.trainer_round_trip_cost_bps (canonical default 50bps)"
                ),
                "cost_sensitivity_ladder_bps": [float(value) for value in DEFAULT_COST_SENSITIVITY_LADDER_BPS],
                "cost_sensitivity": {
                    horizon: cost_sensitivity_ladder(
                        samples, horizon_days=horizon, round_trip_cost_bps=round_trip_cost_bps
                    )
                    for horizon, samples in strict_samples_by_horizon.items()
                    if samples
                },
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
                benchmark_avg_return=(primary_benchmark or {}).get("benchmark_avg_return_pct"),
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
                        "horizons": list(run_horizons),
                        "horizons_used": list(run_horizons),
                        "run_prediction_horizon_days": run_horizon_days,
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
            stratification_shape: dict[int, dict] = {}
            for horizon, samples in strict_samples_by_horizon.items():
                db.add(_metric_record(evaluation.id, horizon_days=horizon, metric_scope="overall", state=None, samples=samples, round_trip_cost_bps=round_trip_cost_bps, benchmark=benchmark_by_horizon.get(horizon)))
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
                strat_groups, strat_shape = _stratification_groups(samples)
                stratification_shape[horizon] = strat_shape
                for scope, grouped_samples in strat_groups.items():
                    dimension, _, value = scope.partition(":")
                    db.add(
                        _metric_record(
                            evaluation.id,
                            horizon_days=horizon,
                            metric_scope=scope,
                            state=None,
                            samples=grouped_samples,
                            round_trip_cost_bps=round_trip_cost_bps,
                            extra_bucket={
                                "stratification_dimension": dimension,
                                "stratification_value": value,
                            },
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
            summary["stratification"] = {
                "dimensions": ["industry", "market_cap_bucket", "liquidity_bucket"],
                "industry_basis": "symbols.industry (source feed industry/Shenwan classification)",
                "market_cap_bucket_basis": "within-evaluation terciles of latest fundamental market_cap",
                "liquidity_bucket_basis": (
                    f"within-evaluation terciles of trailing {STRATIFICATION_ADV_WINDOW_DAYS}d close*volume ADV"
                ),
                "by_horizon": stratification_shape,
            }
            evaluation.summary_json = json.dumps(summary, ensure_ascii=False)
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
            try:
                extras = json.loads(item.metrics_json or "{}")
            except (TypeError, json.JSONDecodeError):
                extras = {}
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
                "hit_rate_ci95_clustered": extras.get("hit_rate_ci95_clustered"),
                "hit_rate_ci_lower_bound_clustered": extras.get("hit_rate_ci_lower_bound_clustered"),
                "hit_rate_ci_cluster_method": extras.get("hit_rate_ci_cluster_method"),
                "benchmark_avg_return_pct": extras.get("benchmark_avg_return_pct"),
                "excess_avg_return_pct": extras.get("excess_avg_return_pct"),
                "benchmark_status": extras.get("benchmark_status"),
                "stratification_dimension": extras.get("stratification_dimension"),
                "stratification_value": extras.get("stratification_value"),
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
                "market_state_metrics": [
                    metric_payload(item)
                    for item in metrics
                    if item.metric_scope.startswith("market_state:") or item.metric_scope == "observation_all_predictions"
                ],
                "stratified_metrics": [
                    metric_payload(item)
                    for item in metrics
                    if item.metric_scope.startswith(("industry:", "market_cap_bucket:", "liquidity_bucket:"))
                ],
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
