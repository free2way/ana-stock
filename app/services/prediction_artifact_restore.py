from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import uuid
from collections import defaultdict
from pathlib import Path

import polars as pl

from app.services.prediction_archive_migration import (
    DETAIL_COLUMNS,
    EXPLANATION_COLUMNS,
    PREDICTION_COLUMNS,
    compare_prediction_artifact_rows,
)
from app.services.prediction_artifacts import (
    DETAIL_FILE,
    EXPLANATION_FILE,
    PREDICTION_FILE,
    verify_prediction_artifact,
)


SQLITE_NAN = "__PREDICTION_ARTIFACT_NAN__"
SQLITE_POS_INF = "__PREDICTION_ARTIFACT_POS_INF__"
SQLITE_NEG_INF = "__PREDICTION_ARTIFACT_NEG_INF__"
SQLITE_NEG_ZERO = "__PREDICTION_ARTIFACT_NEG_ZERO__"


def _sqlite_value(value):
    if isinstance(value, float) and value == 0.0 and math.copysign(1.0, value) < 0:
        return SQLITE_NEG_ZERO
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return SQLITE_NAN
        return SQLITE_POS_INF if value > 0 else SQLITE_NEG_INF
    return value


def _restored_value(value):
    if value == SQLITE_NEG_ZERO:
        return -0.0
    if value == SQLITE_NAN:
        return float("nan")
    if value == SQLITE_POS_INF:
        return float("inf")
    if value == SQLITE_NEG_INF:
        return float("-inf")
    return value


def _insert_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    rows: list[dict],
) -> None:
    if not rows:
        return
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
        [tuple(_sqlite_value(row.get(name)) for name in columns) for row in rows],
    )


def _read_rows(connection: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[dict]:
    names = ", ".join(columns)
    cursor = connection.execute(f"SELECT {names} FROM {table}")
    return [
        {name: _restored_value(value) for name, value in zip(columns, row, strict=True)}
        for row in cursor.fetchall()
    ]


def _top_k_digest(rows: list[dict], *, top_k: int) -> tuple[int, str]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("trade_date") or "")].append(row)
    selected: list[str] = []
    for trade_date, bucket in grouped.items():
        ranked = sorted(
            bucket,
            key=lambda row: (
                float(row.get("rank_value")) if row.get("rank_value") is not None else float("inf"),
                -float(row.get("score") or 0.0),
                int(row["symbol_id"]),
            ),
        )[: max(1, int(top_k))]
        selected.extend(
            json.dumps(
                {
                    "trade_date": trade_date,
                    "symbol_id": int(row["symbol_id"]),
                    "score": row.get("score"),
                    "rank_value": row.get("rank_value"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in ranked
        )
    selected.sort()
    return len(selected), hashlib.sha256("\n".join(selected).encode("utf-8")).hexdigest()


def restore_prediction_artifact_to_sqlite(
    manifest_path: Path | str,
    *,
    destination: Path | str,
    top_k: int = 100,
) -> dict:
    """Restore one cold artifact into an isolated, normalized verification DB."""

    manifest_file = Path(manifest_path).resolve()
    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise FileExistsError(f"Restore destination already exists: {destination_path}")
    integrity = verify_prediction_artifact(manifest_file)
    if integrity["status"] != "success":
        raise RuntimeError(f"Artifact integrity verification failed: {integrity['errors']}")
    artifact_dir = manifest_file.parent
    prediction_rows = pl.read_parquet(artifact_dir / PREDICTION_FILE).to_dicts()
    detail_rows = pl.read_parquet(artifact_dir / DETAIL_FILE).to_dicts()
    explanation_rows = pl.read_parquet(artifact_dir / EXPLANATION_FILE).to_dicts()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    connection = sqlite3.connect(temporary_path)
    try:
        connection.executescript(
            """
            CREATE TABLE predictions (
              model_run_id INTEGER NOT NULL,
              symbol_id INTEGER NOT NULL,
              trade_date TEXT NOT NULL,
              score REAL,
              rank_value REAL,
              PRIMARY KEY (model_run_id, symbol_id, trade_date)
            );
            CREATE TABLE prediction_details (
              symbol_id INTEGER NOT NULL,
              trade_date TEXT NOT NULL,
              confidence REAL, bullish_prob REAL, bearish_prob REAL,
              expected_return_5d REAL, expected_return_20d REAL,
              expected_drawdown_20d REAL, model_reward_risk_ratio REAL,
              risk_score REAL, target_horizon_days INTEGER, universe_size INTEGER,
              percentile REAL, regime_label TEXT, conviction_bucket TEXT,
              position_size_hint TEXT, entry_style TEXT, signal_label TEXT,
              signal_strength REAL, summary_text TEXT,
              PRIMARY KEY (symbol_id, trade_date)
            );
            CREATE TABLE prediction_explanations (
              symbol_id INTEGER NOT NULL,
              trade_date TEXT NOT NULL,
              feature_name TEXT NOT NULL,
              feature_value REAL,
              contribution REAL,
              direction TEXT,
              display_order INTEGER
            );
            CREATE INDEX ix_restore_predictions_date_rank
              ON predictions (trade_date, rank_value, score);
            """
        )
        _insert_rows(
            connection,
            table="predictions",
            columns=("model_run_id", *PREDICTION_COLUMNS),
            rows=prediction_rows,
        )
        _insert_rows(
            connection,
            table="prediction_details",
            columns=("symbol_id", "trade_date", *DETAIL_COLUMNS),
            rows=detail_rows,
        )
        _insert_rows(
            connection,
            table="prediction_explanations",
            columns=EXPLANATION_COLUMNS,
            rows=explanation_rows,
        )
        connection.commit()
        restored_predictions = _read_rows(connection, "predictions", PREDICTION_COLUMNS)
        restored_details = _read_rows(
            connection,
            "prediction_details",
            ("symbol_id", "trade_date", *DETAIL_COLUMNS),
        )
        restored_explanations = _read_rows(connection, "prediction_explanations", EXPLANATION_COLUMNS)
    finally:
        connection.close()
        if sys.exc_info()[0] is not None:
            temporary_path.unlink(missing_ok=True)
    try:
        parity = compare_prediction_artifact_rows(
            manifest_file,
            prediction_rows=restored_predictions,
            detail_rows=restored_details,
            explanation_rows=restored_explanations,
        )
        source_top_k = _top_k_digest(prediction_rows, top_k=top_k)
        restored_top_k = _top_k_digest(restored_predictions, top_k=top_k)
        top_k_match = source_top_k == restored_top_k
        if parity["status"] != "success" or not top_k_match:
            raise RuntimeError(
                f"Restored artifact parity failed: tables={parity['failed_tables']}, "
                f"details={parity['tables']}, top_k_match={top_k_match}"
            )
        os.replace(temporary_path, destination_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return {
        "restore_version": "prediction-artifact-restore-v2",
        "restore_target": "isolated_sqlite",
        "status": "success",
        "model_run_id": int(integrity["manifest"]["model_run_id"]),
        "market": str(integrity["manifest"].get("market") or "").upper(),
        "artifact_schema_version": str(
            integrity["manifest"].get("schema_version") or ""
        ),
        "manifest_path": str(manifest_file),
        "manifest_sha256": hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        "destination": str(destination_path),
        "database_bytes": destination_path.stat().st_size,
        "integrity_status": integrity["status"],
        "parity": parity,
        "top_k": {
            "k": max(1, int(top_k)),
            "rows": restored_top_k[0],
            "source_sha256": source_top_k[1],
            "restored_sha256": restored_top_k[1],
            "exact_match": top_k_match,
        },
        "production_rows_changed": 0,
    }
