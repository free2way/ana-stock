from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from app.services.stock_selection.sample_builder import SampleBuildResult
from app.services.stock_selection.universe import UniverseBuildResult


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    artifact_dir: Path
    data_path: Path
    manifest_path: Path
    data_sha256: str
    reused_existing: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _finalize_immutable_artifact(
    *,
    temporary_dir: Path,
    target_dir: Path,
    data_filename: str,
    manifest: dict[str, Any],
) -> ArtifactWriteResult:
    data_path = temporary_dir / data_filename
    data_sha256 = _sha256(data_path)
    completed_manifest = {**manifest, "data_file": data_filename, "data_sha256": data_sha256}
    _write_json(temporary_dir / "manifest.json", completed_manifest)

    if target_dir.exists():
        existing_manifest_path = target_dir / "manifest.json"
        if not existing_manifest_path.exists():
            raise RuntimeError(f"immutable artifact directory is missing manifest: {target_dir}")
        try:
            existing_manifest = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"immutable artifact manifest is unreadable: {target_dir}") from exc
        if existing_manifest.get("data_sha256") != data_sha256:
            raise RuntimeError(
                f"refusing to overwrite immutable artifact version with different content: {target_dir.name}"
            )
        return ArtifactWriteResult(
            artifact_dir=target_dir,
            data_path=target_dir / data_filename,
            manifest_path=existing_manifest_path,
            data_sha256=data_sha256,
            reused_existing=True,
        )

    temporary_dir.replace(target_dir)
    return ArtifactWriteResult(
        artifact_dir=target_dir,
        data_path=target_dir / data_filename,
        manifest_path=target_dir / "manifest.json",
        data_sha256=data_sha256,
        reused_existing=False,
    )


def persist_universe_artifact(result: UniverseBuildResult, *, root: Path) -> ArtifactWriteResult:
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / result.universe_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".universe-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        records = [
            {
                "snapshot_id": item.snapshot_id,
                "market": item.market,
                "trade_date": item.trade_date.isoformat(),
                "ticker": item.ticker,
                "included": item.included,
                "exclusion_reason_codes_json": json.dumps(item.exclusion_reason_codes, ensure_ascii=False),
                "source_as_of": item.source_as_of.isoformat(),
                "universe_version": item.universe_version,
                "security_type": item.security_type,
                "price": item.price,
                "adv20": item.adv20,
                "volume": item.volume,
                "listing_age_sessions": item.listing_age_sessions,
                "suspended": item.suspended,
                "limit_status": item.limit_status,
                "corporate_action_status": item.corporate_action_status,
                "metadata_json": json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True),
                "schema_version": item.schema_version,
            }
            for item in result.snapshots
        ]
        pl.DataFrame(records).write_parquet(temporary_dir / "universe.parquet", compression="zstd")
        return _finalize_immutable_artifact(
            temporary_dir=temporary_dir,
            target_dir=target_dir,
            data_filename="universe.parquet",
            manifest={"artifact_type": "point_in_time_universe", **result.manifest()},
        )


def persist_sample_artifact(result: SampleBuildResult, *, root: Path) -> ArtifactWriteResult:
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / result.dataset_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".samples-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        records = [
            {
                "sample_id": item.sample_id,
                "market": item.market,
                "ticker": item.ticker,
                "feature_date": item.feature_date.isoformat(),
                "label_start_date": item.label_start_date.isoformat(),
                "label_end_date": item.label_end_date.isoformat(),
                "label_available_date": item.label_available_date.isoformat(),
                "horizon_days": item.horizon_days,
                "label_value": item.label_value,
                "target_mode": item.target_mode,
                "label_components_json": json.dumps(dict(item.label_components), sort_keys=True),
                "features_json": json.dumps(dict(item.features), sort_keys=True),
                "tradable": item.tradable,
                "exclusion_reason": item.exclusion_reason,
                "dataset_version": item.dataset_version,
                "schema_version": item.schema_version,
            }
            for item in result.samples
        ]
        pl.DataFrame(records).write_parquet(temporary_dir / "samples.parquet", compression="zstd")
        return _finalize_immutable_artifact(
            temporary_dir=temporary_dir,
            target_dir=target_dir,
            data_filename="samples.parquet",
            manifest={"artifact_type": "stock_selection_training_samples", **result.manifest()},
        )
