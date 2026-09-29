from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import ModelRun, Symbol, WorkspaceSnapshot
from app.services.kronos_validation import KRONOS_VALIDATION_SNAPSHOT_TYPE
from app.services.market_hot_predictions import MarketHotPredictionRepository
from app.services.market_storage_routing import (
    legacy_mirror_write_enabled,
    normalize_fact_market,
    physical_hot_prediction_models,
)
from app.services.prediction_artifacts import (
    PredictionArtifactWriter,
    select_hot_prediction_rows,
)
from app.services.repository import (
    ModelRunRepository,
    PredictionArtifactRepository,
    PredictionWriteRepository,
    SymbolRepository,
)


KRONOS_SUPPORT_SCORE = 0.68


@dataclass(frozen=True)
class KronosHistoricalPanel:
    market: str
    model_name: str
    start_date: str
    end_date: str
    snapshot_ids: tuple[int, ...]
    signal_dates: tuple[str, ...]
    kronos_predictions: tuple[dict, ...]
    baseline_predictions: tuple[dict, ...]
    decision_counts: dict[str, int]


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _payload(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_kronos_historical_panel(
    snapshots: list[dict],
    *,
    market: str = "CN",
    min_candidates_per_date: int = 5,
) -> KronosHistoricalPanel:
    """Build a point-in-time panel only from same-session production snapshots.

    Historical stale reruns are deliberately excluded: the snapshot date must
    equal the latest market date embedded in each selected row. When multiple
    snapshots exist for the same session, the last same-session snapshot wins.
    """

    market_code = str(market or "CN").upper()
    canonical: dict[str, dict] = {}
    for snapshot in snapshots:
        payload = _payload(snapshot.get("payload"))
        snapshot_date = str(snapshot.get("snapshot_date") or "")[:10]
        rows = [
            dict(row)
            for row in (payload.get("rows") or [])
            if isinstance(row, dict)
            and str(row.get("market") or "").upper() == market_code
            and str(row.get("kronos_status") or "").upper() == "READY"
            and str(row.get("latest_date") or "")[:10] == snapshot_date
            and _finite_number(row.get("kronos_score")) is not None
            and str(row.get("ticker") or "").strip()
        ]
        deduped: dict[str, dict] = {}
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker and ticker not in deduped:
                deduped[ticker] = row
        if not snapshot_date or len(deduped) < max(1, int(min_candidates_per_date)):
            continue
        candidate = {
            "id": int(snapshot.get("id") or 0),
            "snapshot_date": snapshot_date,
            "created_at": str(snapshot.get("created_at") or ""),
            "model_name": str(payload.get("model_name") or "NeoQuasar/Kronos-mini"),
            "rows": list(deduped.values()),
        }
        existing = canonical.get(snapshot_date)
        if existing is None or (candidate["created_at"], candidate["id"]) > (
            existing["created_at"],
            existing["id"],
        ):
            canonical[snapshot_date] = candidate

    if not canonical:
        raise RuntimeError("No same-session READY Kronos snapshots are available for backtesting.")

    kronos_predictions: list[dict] = []
    baseline_predictions: list[dict] = []
    snapshot_ids: list[int] = []
    decision_counts: Counter[str] = Counter()
    model_names: Counter[str] = Counter()
    for signal_date in sorted(canonical):
        snapshot = canonical[signal_date]
        rows = snapshot["rows"]
        snapshot_ids.append(snapshot["id"])
        model_names[snapshot["model_name"]] += 1
        baseline_size = len(rows)
        for source_rank, row in enumerate(rows, start=1):
            ticker = str(row.get("ticker") or "").strip().upper()
            decision = str(row.get("kronos_decision") or "unknown")
            decision_counts[decision] += 1
            baseline_predictions.append(
                {
                    "ticker": ticker,
                    "trade_date": signal_date,
                    "score": (baseline_size - source_rank + 1) / baseline_size,
                    "rank_value": float(source_rank),
                    "source_snapshot_id": snapshot["id"],
                }
            )
        ranked = sorted(
            rows,
            key=lambda row: (
                -float(row.get("kronos_score") or 0.0),
                -float(row.get("kronos_expected_return_3d_pct") or 0.0),
                str(row.get("ticker") or ""),
            ),
        )
        for rank, row in enumerate(ranked, start=1):
            kronos_predictions.append(
                {
                    "ticker": str(row.get("ticker") or "").strip().upper(),
                    "trade_date": signal_date,
                    "score": float(row.get("kronos_score") or 0.0) / 100.0,
                    "rank_value": float(rank),
                    "source_snapshot_id": snapshot["id"],
                }
            )

    signal_dates = tuple(sorted(canonical))
    return KronosHistoricalPanel(
        market=market_code,
        model_name=model_names.most_common(1)[0][0],
        start_date=signal_dates[0],
        end_date=signal_dates[-1],
        snapshot_ids=tuple(snapshot_ids),
        signal_dates=signal_dates,
        kronos_predictions=tuple(kronos_predictions),
        baseline_predictions=tuple(baseline_predictions),
        decision_counts=dict(decision_counts),
    )


def load_kronos_historical_panel(
    db: Session,
    *,
    market: str = "CN",
    min_candidates_per_date: int = 5,
) -> KronosHistoricalPanel:
    rows = db.scalars(
        select(WorkspaceSnapshot)
        .where(WorkspaceSnapshot.snapshot_type == KRONOS_VALIDATION_SNAPSHOT_TYPE)
        .order_by(WorkspaceSnapshot.created_at.asc(), WorkspaceSnapshot.id.asc())
    ).all()
    return build_kronos_historical_panel(
        [
            {
                "id": row.id,
                "snapshot_date": row.snapshot_date,
                "created_at": row.created_at,
                "payload": row.payload_json,
            }
            for row in rows
        ],
        market=market,
        min_candidates_per_date=min_candidates_per_date,
    )


def _persist_panel_run(
    db: Session,
    *,
    panel: KronosHistoricalPanel,
    name: str,
    model_type: str,
    prediction_rows: tuple[dict, ...],
    config: dict,
) -> ModelRun:
    market = normalize_fact_market(panel.market)
    settings = get_settings()
    model_repo = ModelRunRepository(db)
    run = db.scalar(
        select(ModelRun)
        .where(ModelRun.name == name, ModelRun.model_type == model_type)
        .order_by(ModelRun.id.desc())
        .limit(1)
    )
    if run is None:
        run = model_repo.create_run(
            name=name,
            model_type=model_type,
            market=market,
            universe="historical_upstream_candidate_pool",
            train_start=None,
            train_end=None,
            test_start=panel.start_date,
            test_end=panel.end_date,
            config=config,
            artifact_path=None,
            status="running",
        )
    symbols = {symbol.ticker.upper(): symbol.id for symbol in SymbolRepository(db).list_symbols()}
    prepared = [
        {
            "symbol_id": symbols[row["ticker"]],
            "trade_date": row["trade_date"],
            "score": row["score"],
            "rank_value": row["rank_value"],
        }
        for row in prediction_rows
        if row["ticker"] in symbols
    ]
    if not prepared:
        model_repo.complete_run(run.id, status="failed")
        raise RuntimeError(f"No persisted symbols matched the {model_type} historical panel.")

    symbol_markets = {
        str(value or "").strip().upper()
        for value in db.scalars(
            select(Symbol.market).where(
                Symbol.id.in_({int(row["symbol_id"]) for row in prepared})
            )
        )
    }
    if symbol_markets != {market}:
        model_repo.complete_run(run.id, status="failed")
        raise RuntimeError(
            f"Refusing {market} Kronos publication because symbols are missing or cross-market."
        )

    artifact_writer = PredictionArtifactWriter()
    publication_plan = artifact_writer.plan(
        prediction_rows=prepared,
        detail_rows=[],
        explanation_rows=[],
    )
    write_legacy = legacy_mirror_write_enabled(
        db,
        market=market,
        configured=bool(settings.market_physical_hot_dual_write_legacy),
    )
    hot_predictions = select_hot_prediction_rows(
        prepared,
        full_trade_days=int(settings.prediction_hot_full_trade_days),
        top_k=int(settings.prediction_hot_top_k),
    )
    prediction_table, _, _ = physical_hot_prediction_models(market)
    storage_contract = {
        "contract_version": "prediction-storage-v1",
        "producer": "kronos_historical_research",
        "cold_payload": "full",
        "hot_write_mode": "compact",
        "hot_full_trade_days": int(settings.prediction_hot_full_trade_days),
        "hot_top_k": int(settings.prediction_hot_top_k),
        "primary_market_table": prediction_table.__tablename__,
        "legacy_hot_dual_write": bool(write_legacy),
        "live_publication": False,
    }
    published_config = {
        **config,
        "prediction_publication_plan": publication_plan,
        "prediction_storage_contract": storage_contract,
    }
    artifact_manifest: dict | None = None
    artifact_path: str | None = None
    try:
        artifact_manifest = artifact_writer.write(
            model_run_id=run.id,
            market=market,
            prediction_rows=prepared,
            detail_rows=[],
            explanation_rows=[],
            model_metadata=published_config,
            publication_plan=publication_plan,
        )
        artifact_path = str(artifact_manifest["artifact_path"])
        PredictionArtifactRepository(db).upsert_manifest(
            artifact_manifest,
            status="verified",
        )

        if write_legacy:
            PredictionWriteRepository(db).replace_for_model_run(
                run.id,
                hot_predictions,
                commit=False,
            )
        MarketHotPredictionRepository(db).publish_for_model_run(
            model_run_id=run.id,
            market=market,
            prediction_rows=hot_predictions,
            detail_rows=[],
            explanation_rows=[],
            commit=False,
        )
        model_repo.merge_config(
            run.id,
            {
                "prediction_publication_plan": publication_plan,
                "prediction_storage_contract": storage_contract,
            },
            commit=False,
        )
        model_repo.complete_run(
            run.id,
            status="success",
            artifact_path=artifact_path,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        if artifact_manifest is not None:
            try:
                PredictionArtifactRepository(db).set_status(run.id, "failed_run")
            except Exception:
                db.rollback()
        model_repo.complete_run(
            run.id,
            status="failed",
            artifact_path=artifact_path,
        )
        raise
    return run


def persist_kronos_historical_runs(db: Session, *, market: str = "CN") -> dict:
    panel = load_kronos_historical_panel(db, market=market)
    suffix = f"{panel.start_date}_{panel.end_date}"
    common_config = {
        "source": "historical_production_kronos_snapshots",
        "evaluation_protocol": "historical_snapshot_oos_v1",
        "chronology_rule": "snapshot_date_equals_embedded_latest_market_date",
        "model_name": panel.model_name,
        "signal_dates": list(panel.signal_dates),
        "source_snapshot_ids": list(panel.snapshot_ids),
        "decision_counts": panel.decision_counts,
        "target_horizon_days": 3,
        "round_trip_cost_bps": 40.0,
    }
    baseline = _persist_panel_run(
        db,
        panel=panel,
        name=f"kronos_upstream_baseline_{suffix}",
        model_type="kronos_upstream_candidate_baseline",
        prediction_rows=panel.baseline_predictions,
        config={**common_config, "selection": "original_upstream_candidate_order"},
    )
    kronos = _persist_panel_run(
        db,
        panel=panel,
        name=f"kronos_mini_validator_{suffix}",
        model_type="kronos_mini_validator",
        prediction_rows=panel.kronos_predictions,
        config={
            **common_config,
            "selection": "kronos_score_rank_then_support_threshold",
            "support_score_threshold": KRONOS_SUPPORT_SCORE,
        },
    )
    return {
        "panel": panel,
        "baseline_model_run_id": baseline.id,
        "kronos_model_run_id": kronos.id,
    }
