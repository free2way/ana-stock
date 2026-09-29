from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from app.models.tables import (
    CNFundamentalSnapshot,
    CNLivePrediction,
    CNModelChartSignal,
    CNPointInTimeFeature,
    CNPrediction,
    CNPredictionDetail,
    CNPredictionExplanation,
    CNPredictionTradePlan,
    CNTechnicalSnapshot,
    HKFundamentalSnapshot,
    HKLivePrediction,
    HKModelChartSignal,
    HKPointInTimeFeature,
    HKPrediction,
    HKPredictionDetail,
    HKPredictionExplanation,
    HKPredictionTradePlan,
    HKTechnicalSnapshot,
    LivePrediction,
    ModelRun,
    Prediction,
    PredictionArtifact,
    PredictionDetail,
    PredictionExplanation,
    StorageCapacitySample,
    Symbol,
    USFundamentalSnapshot,
    USLivePrediction,
    USModelChartSignal,
    USPointInTimeFeature,
    USPrediction,
    USPredictionDetail,
    USPredictionExplanation,
    USPredictionTradePlan,
    USTechnicalSnapshot,
)
from app.services.time_utils import app_now_iso


CRITICAL_TABLES = {
    "symbols": Symbol,
    "model_runs": ModelRun,
    "prediction_artifacts": PredictionArtifact,
    "predictions": Prediction,
    "prediction_details": PredictionDetail,
    "prediction_explanations": PredictionExplanation,
    "live_predictions": LivePrediction,
    "cn_live_predictions": CNLivePrediction,
    "us_live_predictions": USLivePrediction,
    "hk_live_predictions": HKLivePrediction,
    "cn_predictions": CNPrediction,
    "cn_prediction_details": CNPredictionDetail,
    "cn_prediction_explanations": CNPredictionExplanation,
    "cn_model_chart_signals": CNModelChartSignal,
    "cn_prediction_trade_plans": CNPredictionTradePlan,
    "us_predictions": USPrediction,
    "us_prediction_details": USPredictionDetail,
    "us_prediction_explanations": USPredictionExplanation,
    "us_model_chart_signals": USModelChartSignal,
    "us_prediction_trade_plans": USPredictionTradePlan,
    "hk_predictions": HKPrediction,
    "hk_prediction_details": HKPredictionDetail,
    "hk_prediction_explanations": HKPredictionExplanation,
    "hk_model_chart_signals": HKModelChartSignal,
    "hk_prediction_trade_plans": HKPredictionTradePlan,
    "cn_fundamental_snapshots": CNFundamentalSnapshot,
    "cn_point_in_time_features": CNPointInTimeFeature,
    "cn_technical_snapshots": CNTechnicalSnapshot,
    "us_fundamental_snapshots": USFundamentalSnapshot,
    "us_point_in_time_features": USPointInTimeFeature,
    "us_technical_snapshots": USTechnicalSnapshot,
    "hk_fundamental_snapshots": HKFundamentalSnapshot,
    "hk_point_in_time_features": HKPointInTimeFeature,
    "hk_technical_snapshots": HKTechnicalSnapshot,
    "storage_capacity_samples": StorageCapacitySample,
}
RESTORE_DATABASE_PATTERN = re.compile(r"^quant_restore_acceptance_[a-z0-9_]+$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def postgres_cli_connection(database_url: str, *, database: str | None = None) -> tuple[list[str], dict[str, str]]:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("PostgreSQL database URL is required.")
    target_database = str(database or url.database or "").strip()
    if not target_database:
        raise ValueError("PostgreSQL database name is required.")
    args = ["--dbname", target_database]
    if url.host:
        args.extend(["--host", str(url.host)])
    if url.port:
        args.extend(["--port", str(url.port)])
    if url.username:
        args.extend(["--username", str(url.username)])
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return args, env


def database_url_for(database_url: str, database: str) -> str:
    url: URL = make_url(database_url)
    return url.set(database=str(database)).render_as_string(hide_password=False)


def validate_restore_database_name(database_name: str) -> str:
    normalized = str(database_name or "").strip().lower()
    if not RESTORE_DATABASE_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Restore database name must match quant_restore_acceptance_[a-z0-9_]+."
        )
    return normalized


def compare_critical_counts(expected: dict[str, int], actual: dict[str, int]) -> dict:
    tables = sorted(set(expected) | set(actual))
    differences = {
        table: {"expected": int(expected.get(table, -1)), "actual": int(actual.get(table, -1))}
        for table in tables
        if int(expected.get(table, -1)) != int(actual.get(table, -1))
    }
    return {
        "status": "pass" if not differences else "fail",
        "exact_match": not differences,
        "differences": differences,
        "expected": {table: int(expected[table]) for table in sorted(expected)},
        "actual": {table: int(actual[table]) for table in sorted(actual)},
    }


def critical_table_counts(db: Session) -> dict[str, int]:
    return {
        name: int(db.scalar(select(func.count(model.id))) or 0)
        for name, model in CRITICAL_TABLES.items()
    }


def _archive_catalog(pg_restore: str, target: Path) -> tuple[int, int]:
    listing = subprocess.run(
        [pg_restore, "--list", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        raise RuntimeError(f"pg_restore --list failed: {listing.stderr[-4000:]}")
    toc_lines = [line for line in listing.stdout.splitlines() if line.strip() and not line.lstrip().startswith(";")]
    table_data_entries = [line for line in toc_lines if " TABLE DATA " in line]
    return len(toc_lines), len(table_data_entries)


def inspect_postgres_logical_backup(db: Session, *, destination: Path) -> dict:
    target = destination.resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("PostgreSQL pg_restore is required.")
    database_name = str(db.scalar(text("select current_database()")) or "")
    database_version = str(db.scalar(text("select version()")) or "")
    database_bytes = int(db.scalar(text("select pg_database_size(current_database())")) or 0)
    counts = critical_table_counts(db)
    db.rollback()
    toc_entry_count, table_data_entry_count = _archive_catalog(pg_restore, target)
    return {
        "backup_version": "postgres-logical-backup-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "mode": "inspect_existing",
        "format": "postgresql_custom",
        "database_name": database_name,
        "database_version": database_version,
        "database_bytes": database_bytes,
        "critical_table_counts": counts,
        "count_capture_note": "Counts were captured immediately after the consistent backup completed.",
        "backup_path": str(target),
        "backup_bytes": target.stat().st_size,
        "backup_sha256": file_sha256(target),
        "toc_entry_count": toc_entry_count,
        "table_data_entry_count": table_data_entry_count,
        "database_mutated": False,
    }


def create_postgres_logical_backup(
    db: Session,
    *,
    database_url: str,
    destination: Path,
    compression_level: int = 6,
) -> dict:
    target = destination.resolve()
    if target.exists():
        raise FileExistsError(f"Backup already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise RuntimeError("PostgreSQL pg_dump and pg_restore are required.")

    database_name = str(db.scalar(text("select current_database()")) or "")
    database_version = str(db.scalar(text("select version()")) or "")
    database_bytes = int(db.scalar(text("select pg_database_size(current_database())")) or 0)
    counts = critical_table_counts(db)
    # Do not retain an idle SQLAlchemy transaction while pg_dump performs a
    # potentially long independent consistent-snapshot read.
    db.rollback()
    connection_args, env = postgres_cli_connection(database_url)
    command = [
        pg_dump,
        "--format=custom",
        f"--compress=zstd:{max(1, min(19, int(compression_level)))}",
        "--no-owner",
        "--no-privileges",
        "--lock-wait-timeout=60s",
        "--serializable-deferrable",
        "--file",
        str(temporary),
        *connection_args,
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {completed.stderr[-4000:]}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    elapsed_seconds = time.perf_counter() - started

    toc_entry_count, table_data_entry_count = _archive_catalog(pg_restore, target)
    return {
        "backup_version": "postgres-logical-backup-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "format": "postgresql_custom",
        "compression": f"zstd:{max(1, min(19, int(compression_level)))}",
        "database_name": database_name,
        "database_version": database_version,
        "database_bytes": database_bytes,
        "critical_table_counts": counts,
        "backup_path": str(target),
        "backup_bytes": target.stat().st_size,
        "backup_sha256": file_sha256(target),
        "toc_entry_count": toc_entry_count,
        "table_data_entry_count": table_data_entry_count,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "database_mutated": False,
    }


def restore_postgres_logical_backup(
    db: Session,
    *,
    database_url: str,
    backup_path: Path,
    backup_sha256: str,
    expected_counts: dict[str, int],
    restore_database: str,
    jobs: int = 4,
) -> dict:
    """Restore a verified archive to an isolated database and remove it afterward."""

    target_database = validate_restore_database_name(restore_database)
    archive = backup_path.resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    actual_backup_sha256 = file_sha256(archive)
    if actual_backup_sha256 != str(backup_sha256):
        raise RuntimeError("Backup SHA-256 does not match the frozen receipt.")
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("PostgreSQL pg_restore is required.")

    source_database = str(db.scalar(text("select current_database()")) or "")
    if target_database == source_database:
        raise ValueError("Restore database must never equal the source database.")
    db.rollback()
    source_engine = db.get_bind()
    created = False
    dropped = False
    started = time.perf_counter()
    restore_database_bytes = 0
    restored_counts: dict[str, int] = {}
    restored_table_count = 0
    restored_constraint_count = 0
    try:
        with source_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            exists = connection.scalar(
                text("select 1 from pg_database where datname = :name"),
                {"name": target_database},
            )
            if exists:
                raise RuntimeError(f"Refusing to overwrite existing database: {target_database}")
            connection.execute(text(f'create database "{target_database}"'))
            created = True

        connection_args, env = postgres_cli_connection(database_url, database=target_database)
        completed = subprocess.run(
            [
                pg_restore,
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--jobs",
                str(max(1, int(jobs))),
                *connection_args,
                str(archive),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"pg_restore failed: {completed.stderr[-4000:]}")

        restored_engine = create_engine(database_url_for(database_url, target_database), future=True)
        try:
            with Session(restored_engine) as restored_db:
                restored_counts = critical_table_counts(restored_db)
                restore_database_bytes = int(
                    restored_db.scalar(text("select pg_database_size(current_database())")) or 0
                )
            restored_inspector = inspect(restored_engine)
            restored_table_count = len(restored_inspector.get_table_names())
            restored_constraint_count = sum(
                len(restored_inspector.get_unique_constraints(table))
                + len(restored_inspector.get_foreign_keys(table))
                for table in restored_inspector.get_table_names()
            )
        finally:
            restored_engine.dispose()

        parity = compare_critical_counts(expected_counts, restored_counts)
        if parity["status"] != "pass":
            raise RuntimeError(f"Restored critical table counts differ: {parity['differences']}")
    finally:
        if created:
            with source_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(
                    text(
                        "select pg_terminate_backend(pid) from pg_stat_activity "
                        "where datname = :name and pid <> pg_backend_pid()"
                    ),
                    {"name": target_database},
                )
                connection.execute(text(f'drop database "{target_database}"'))
                dropped = True

    elapsed_seconds = time.perf_counter() - started
    parity = compare_critical_counts(expected_counts, restored_counts)
    return {
        "restore_version": "postgres-logical-restore-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if parity["status"] == "pass" and dropped else "fail",
        "source_database": source_database,
        "restore_database": target_database,
        "backup_path": str(archive),
        "backup_sha256": actual_backup_sha256,
        "critical_table_parity": parity,
        "restored_database_bytes": restore_database_bytes,
        "restored_table_count": restored_table_count,
        "restored_constraint_count": restored_constraint_count,
        "restore_jobs": max(1, int(jobs)),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "restore_database_created": created,
        "restore_database_dropped": dropped,
        "source_database_mutated": False,
    }
