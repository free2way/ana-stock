from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import (
    Prediction,
    PredictionDetail,
    PredictionExplanation,
    Symbol,
)
from app.services.prediction_archive_migration import (
    DETAIL_COLUMNS,
    EXPLANATION_COLUMNS,
    _canonical_rows_digest,
)
from app.services.prediction_artifacts import _manifest_digest, _sha256
from app.services.time_utils import app_now_iso


PREDICTION_COLUMNS = (
    "model_run_id",
    "symbol_id",
    "trade_date",
    "score",
    "rank_value",
)
MARKET_DETAIL_COLUMNS = (
    "model_run_id",
    "symbol_id",
    "trade_date",
    *DETAIL_COLUMNS,
)
MARKET_EXPLANATION_COLUMNS = (
    "model_run_id",
    *EXPLANATION_COLUMNS,
)


def _write_rows(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if rows:
        frame = frame.select(list(columns))
    else:
        frame = pl.DataFrame({name: [] for name in columns})
    frame.write_parquet(path, compression="zstd", statistics=True)


def _load_market_rows(db: Session, *, market: str) -> tuple[list[dict], list[dict], list[dict]]:
    predictions = [
        {
            **dict(row),
            "model_run_id": int(row.model_run_id),
            "symbol_id": int(row.symbol_id),
            "trade_date": str(row.trade_date),
        }
        for row in db.execute(
            select(
                Prediction.model_run_id,
                Prediction.symbol_id,
                Prediction.trade_date,
                Prediction.score,
                Prediction.rank_value,
            )
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Symbol.market == market)
            .order_by(
                Prediction.model_run_id.asc(),
                Prediction.trade_date.asc(),
                Prediction.symbol_id.asc(),
            )
        ).mappings()
    ]
    detail_columns = [getattr(PredictionDetail, name) for name in DETAIL_COLUMNS]
    details = [
        {
            **dict(row),
            "model_run_id": int(row.model_run_id),
            "symbol_id": int(row.symbol_id),
            "trade_date": str(row.trade_date),
        }
        for row in db.execute(
            select(
                Prediction.model_run_id,
                Prediction.symbol_id,
                Prediction.trade_date,
                *detail_columns,
            )
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Symbol.market == market)
            .order_by(
                Prediction.model_run_id.asc(),
                Prediction.trade_date.asc(),
                Prediction.symbol_id.asc(),
            )
        ).mappings()
    ]
    explanations = [
        {
            **dict(row),
            "model_run_id": int(row.model_run_id),
            "symbol_id": int(row.symbol_id),
            "trade_date": str(row.trade_date),
        }
        for row in db.execute(
            select(
                Prediction.model_run_id,
                Prediction.symbol_id,
                Prediction.trade_date,
                PredictionExplanation.feature_name,
                PredictionExplanation.feature_value,
                PredictionExplanation.contribution,
                PredictionExplanation.direction,
                PredictionExplanation.display_order,
            )
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(
                PredictionExplanation,
                PredictionExplanation.prediction_id == Prediction.id,
            )
            .where(Symbol.market == market)
            .order_by(
                Prediction.model_run_id.asc(),
                Prediction.trade_date.asc(),
                Prediction.symbol_id.asc(),
                PredictionExplanation.display_order.asc(),
                PredictionExplanation.feature_name.asc(),
            )
        ).mappings()
    ]
    return predictions, details, explanations


def archive_legacy_market_facts(
    db: Session,
    *,
    market: str,
    apply: bool = False,
    artifact_root: Path | None = None,
) -> dict:
    normalized_market = str(market or "").strip().upper()
    if normalized_market not in {"CN", "HK", "US"}:
        raise ValueError(f"Unsupported legacy fact market: {market!r}")
    predictions, details, explanations = _load_market_rows(
        db, market=normalized_market
    )
    run_ids = sorted({int(row["model_run_id"]) for row in predictions})
    dates = sorted({str(row["trade_date"]) for row in predictions})
    counts = {
        "predictions": len(predictions),
        "prediction_details": len(details),
        "prediction_explanations": len(explanations),
    }
    preview = {
        "archive_version": "legacy-market-fact-archive-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "mode": "apply" if apply else "dry_run",
        "market": normalized_market,
        "counts": counts,
        "model_run_ids": run_ids,
        "min_trade_date": min(dates, default=None),
        "max_trade_date": max(dates, default=None),
        "source_rows_deleted": 0,
    }
    if not apply:
        return preview

    settings = get_settings()
    root = (
        artifact_root
        or settings.artifacts_dir / "legacy_market_facts" / f"market={normalized_market}"
    ).resolve()
    final_dir = root / "snapshot=pre-shared-table-retention-20260901"
    temporary_dir = root / f".snapshot.{uuid.uuid4().hex}.tmp"
    root.mkdir(parents=True, exist_ok=True)
    temporary_dir.mkdir(parents=False, exist_ok=False)
    try:
        specs = (
            ("predictions.parquet", predictions, PREDICTION_COLUMNS),
            ("prediction_details.parquet", details, MARKET_DETAIL_COLUMNS),
            (
                "prediction_explanations.parquet",
                explanations,
                MARKET_EXPLANATION_COLUMNS,
            ),
        )
        source_digests: dict[str, str] = {}
        for name, rows, columns in specs:
            _write_rows(temporary_dir / name, rows, columns)
            source_digests[name] = _canonical_rows_digest(rows, columns)
        files = {
            name: {
                "bytes": (temporary_dir / name).stat().st_size,
                "sha256": _sha256(temporary_dir / name),
                "row_digest": source_digests[name],
            }
            for name, _, _ in specs
        }
        manifest = {**preview, "files": files}
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        manifest_path = temporary_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        if final_dir.exists():
            existing = json.loads((final_dir / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("files") != manifest.get("files"):
                raise RuntimeError("Immutable legacy market archive already exists with different content.")
            return {**existing, "artifact_path": str(final_dir / "manifest.json"), "idempotent": True}
        os.replace(temporary_dir, final_dir)
        published = final_dir / "manifest.json"
        for name, rows, columns in specs:
            cold_rows = pl.read_parquet(final_dir / name).select(list(columns)).to_dicts()
            if len(cold_rows) != len(rows) or _canonical_rows_digest(
                cold_rows, columns
            ) != source_digests[name]:
                raise RuntimeError(f"Legacy market archive parity failed: {name}")
        return {
            **manifest,
            "artifact_path": str(published),
            "verification_status": "success",
            "idempotent": False,
        }
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
