from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import polars as pl
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.market_calendar import is_market_open_date, next_market_open_date
from app.services.market_lake import load_lake_rows
from app.services.repository import PointInTimeFeatureSnapshotRepository, WorkspaceSnapshotRepository
from app.services.stock_selection.factor_pipeline import CrossSectionalFactorPipeline, FactorObservation
from app.services.stock_selection.factor_sets import get_research_factor_set
from app.services.stock_selection.feature_availability import (
    PointInTimeFeatureRecord,
    adapt_point_in_time_feature_snapshots,
)
from app.services.stock_selection.point_in_time_features import (
    PointInTimeFeatureJoinConfig,
    build_point_in_time_feature_join,
)
from app.services.time_utils import app_now


CN_FORWARD_SHADOW_SNAPSHOT_TYPE = "stock_selection_shadow_daily:CN:quality_value_shadow_v1"


@dataclass(frozen=True, slots=True)
class CNForwardShadowConfig:
    factor_set_key: str = "quality_value_shadow_v1"
    horizon_days: int = 5
    top_observation_count: int = 20
    minimum_cross_section_coverage: float = 0.60
    minimum_confirmation_dates: int = 60
    timezone_name: str = "Asia/Shanghai"
    schema_version: str = "stock_selection_forward_shadow_v1"

    def __post_init__(self) -> None:
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.top_observation_count <= 0:
            raise ValueError("top_observation_count must be positive")
        if not 0.0 < self.minimum_cross_section_coverage <= 1.0:
            raise ValueError("minimum_cross_section_coverage must be in (0, 1]")
        if self.minimum_confirmation_dates <= 0:
            raise ValueError("minimum_confirmation_dates must be positive")
        ZoneInfo(self.timezone_name)
        get_research_factor_set(self.factor_set_key)

    def version(self) -> str:
        factor_set = get_research_factor_set(self.factor_set_key)
        payload = {"config": asdict(self), "factor_set_version": factor_set.version()}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.schema_version}:CN:{digest}"


@dataclass(frozen=True, slots=True)
class CNForwardShadowBuildResult:
    payload: Mapping[str, object]
    score_rows: tuple[Mapping[str, object], ...]


def _canonical_hash(value: object, *, length: int = 20) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _source_version(records: Iterable[PointInTimeFeatureRecord]) -> str:
    identities = sorted(
        (
            item.record_id,
            item.ticker,
            item.feature_name,
            item.revision_id,
            item.event_time.isoformat(),
            item.available_time.isoformat(),
            item.ingested_time.isoformat(),
            float(item.value),
        )
        for item in records
    )
    return f"point_in_time_feature_store_v1:CN:{_canonical_hash(identities)}"


def _effective_trade_date(cutoff: datetime) -> str:
    local_cutoff = cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
    include_self = (
        not is_market_open_date("CN", local_cutoff.date())
        or local_cutoff.time() < time(hour=9, minute=25)
    )
    return next_market_open_date("CN", local_cutoff.date(), include_self=include_self)


def build_cn_forward_shadow_snapshot(
    records: Iterable[PointInTimeFeatureRecord],
    *,
    universe: Iterable[str],
    feature_date: date,
    decision_cutoff: datetime,
    source_version: str,
    revision_history_preserved: bool,
    config: CNForwardShadowConfig | None = None,
) -> CNForwardShadowBuildResult:
    """Build an uncalibrated, abstaining forward snapshot from knowledge available now."""

    resolved = config or CNForwardShadowConfig()
    if decision_cutoff.tzinfo is None or decision_cutoff.utcoffset() is None:
        raise ValueError("decision_cutoff must be timezone-aware")
    timezone = ZoneInfo(resolved.timezone_name)
    local_cutoff = decision_cutoff.astimezone(timezone)
    if feature_date > local_cutoff.date():
        raise ValueError("feature_date must not be after decision_cutoff")
    tickers = tuple(sorted({str(item or "").strip().upper() for item in universe if str(item or "").strip()}))
    point_in_time = build_point_in_time_feature_join(
        tuple(records),
        universe_by_date={feature_date: tickers},
        source_version=source_version,
        config=PointInTimeFeatureJoinConfig(
            market="CN",
            minimum_cross_section_coverage=resolved.minimum_cross_section_coverage,
            timezone_name=resolved.timezone_name,
        ),
        cutoff_by_date={feature_date: local_cutoff},
    )
    coverage = point_in_time.date_coverage[0]
    factor_set = get_research_factor_set(resolved.factor_set_key)
    observations: list[FactorObservation] = []
    raw_features_by_ticker: dict[str, dict[str, float]] = {}
    if coverage.enabled:
        for ticker in tickers:
            features = dict(point_in_time.features_by_key.get((ticker, feature_date), {}))
            complete = all(
                name in features and math.isfinite(float(features[name]))
                for name in factor_set.feature_names
            )
            if not complete:
                continue
            raw_features_by_ticker[ticker] = {
                name: float(features[name]) for name in factor_set.feature_names
            }
            observations.append(
                FactorObservation(
                    observation_id=f"cn-forward-shadow:{feature_date.isoformat()}:{ticker}",
                    ticker=ticker,
                    feature_date=feature_date,
                    horizon_days=resolved.horizon_days,
                    features=features,
                )
            )
    scores = (
        CrossSectionalFactorPipeline(factor_set.specs).transform(observations)
        if observations
        else ()
    )
    score_rows = tuple(
        {
            "sample_id": item.sample_id,
            "ticker": item.ticker,
            "feature_date": item.feature_date.isoformat(),
            "effective_trade_date": _effective_trade_date(local_cutoff),
            "horizon_days": item.horizon_days,
            "composite_score": item.composite_score,
            "cross_sectional_rank": item.cross_sectional_rank,
            "raw_features_json": json.dumps(
                raw_features_by_ticker[item.ticker], sort_keys=True, separators=(",", ":")
            ),
            "normalized_factors_json": json.dumps(
                dict(item.factor_values), sort_keys=True, separators=(",", ":")
            ),
        }
        for item in sorted(
            scores,
            key=lambda value: (-value.cross_sectional_rank, -value.composite_score, value.ticker),
        )
    )
    top_rows = score_rows[: resolved.top_observation_count]
    status = "success" if coverage.enabled and score_rows else "collecting"
    payload: dict[str, object] = {
        "schema_version": resolved.schema_version,
        "scope": "cn_forward_shadow_only",
        "status": status,
        "market": "CN",
        "feature_date": feature_date.isoformat(),
        "decision_cutoff": local_cutoff.isoformat(),
        "effective_trade_date": _effective_trade_date(local_cutoff),
        "horizon_days": resolved.horizon_days,
        "config_version": resolved.version(),
        "factor_set_key": factor_set.key,
        "factor_set_version": factor_set.version(),
        "point_in_time_feature_set_version": point_in_time.feature_set_version,
        "source_version": source_version,
        "revision_history_preserved": bool(revision_history_preserved),
        "universe_symbol_count": len(tickers),
        "eligible_score_count": len(score_rows),
        "excluded_incomplete_score_count": max(0, len(tickers) - len(score_rows)),
        "selected_record_count": point_in_time.selected_record_count,
        "as_of_gate": "PASS" if coverage.enabled else "COLLECTING",
        "minimum_feature_coverage": coverage.minimum_feature_coverage,
        "feature_coverage": dict(coverage.feature_coverage),
        "shadow_decision": "ABSTAIN",
        "abstention_reason": (
            "uncalibrated_confirmation_window"
            if coverage.enabled and score_rows
            else "point_in_time_data_gate_closed"
        ),
        "promotion_status": "BLOCKED",
        "promotion_blockers": [
            "minimum_60_untouched_dates_not_met",
            *([] if revision_history_preserved else ["revision_history_not_preserved"]),
            *([] if coverage.enabled else ["point_in_time_data_gate_closed"]),
        ],
        "top_observations": [dict(item) for item in top_rows],
    }
    return CNForwardShadowBuildResult(payload=payload, score_rows=score_rows)


def _persist_shadow_artifact(
    *,
    payload: Mapping[str, object],
    score_rows: tuple[Mapping[str, object], ...],
    artifacts_root: Path,
) -> tuple[str, Path, Path]:
    evidence_payload = {"payload": payload, "score_rows": score_rows}
    evidence_version = f"stock_selection_forward_shadow_v1:CN:{_canonical_hash(evidence_payload)}"
    artifact_dir = artifacts_root / "stock_selection_research" / "forward_shadow" / evidence_version.replace(":", "_")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    scores_path = artifact_dir / "scores.parquet"
    manifest_path = artifact_dir / "manifest.json"
    if not scores_path.exists():
        frame = pl.DataFrame(score_rows) if score_rows else pl.DataFrame(
            schema={
                "sample_id": pl.String,
                "ticker": pl.String,
                "feature_date": pl.String,
                "effective_trade_date": pl.String,
                "horizon_days": pl.Int64,
                "composite_score": pl.Float64,
                "cross_sectional_rank": pl.Float64,
                "raw_features_json": pl.String,
                "normalized_factors_json": pl.String,
            }
        )
        temporary_scores = scores_path.with_name(f".{scores_path.name}.tmp")
        frame.write_parquet(temporary_scores, compression="zstd")
        temporary_scores.replace(scores_path)
    manifest = {
        "evidence_version": evidence_version,
        "score_row_count": len(score_rows),
        "scores_path": str(scores_path),
        "payload": payload,
    }
    if not manifest_path.exists():
        temporary_manifest = manifest_path.with_name(f".{manifest_path.name}.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.replace(manifest_path)
    return evidence_version, scores_path, manifest_path


def create_cn_forward_shadow_snapshot(
    db: Session,
    *,
    feature_date: str | date,
    source_job_id: int | None = None,
    decision_cutoff: datetime | None = None,
    config: CNForwardShadowConfig | None = None,
) -> dict:
    """Persist the first immutable shadow decision for an effective trade date."""

    resolved = config or CNForwardShadowConfig()
    parsed_feature_date = (
        feature_date if isinstance(feature_date, date) else date.fromisoformat(str(feature_date)[:10])
    )
    cutoff = decision_cutoff or app_now()
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("decision_cutoff must be timezone-aware")
    effective_trade_date = _effective_trade_date(cutoff)
    snapshot_repo = WorkspaceSnapshotRepository(db)
    existing = next(
        (
            item
            for item in snapshot_repo.list_snapshots(CN_FORWARD_SHADOW_SNAPSHOT_TYPE, limit=400)
            if str(item.get("snapshot_date") or "")[:10] == effective_trade_date
        ),
        None,
    )
    if existing is not None:
        return {
            "status": "success",
            "reused_existing": True,
            "snapshot_id": existing["id"],
            "snapshot_date": existing["snapshot_date"],
            "payload": existing.get("payload") or {},
        }

    point_rows = PointInTimeFeatureSnapshotRepository(db).list_history_for_market("CN")
    adapted = adapt_point_in_time_feature_snapshots(point_rows, market="CN")
    universe_rows = load_lake_rows(
        markets=["CN"],
        start_date=parsed_feature_date.isoformat(),
        end_date=parsed_feature_date.isoformat(),
    )
    universe = sorted(
        {
            str(item.get("symbol") or "").strip().upper()
            for item in universe_rows
            if str(item.get("date") or "")[:10] == parsed_feature_date.isoformat()
            and str(item.get("symbol") or "").strip()
        }
    )
    previous = snapshot_repo.list_snapshots(CN_FORWARD_SHADOW_SNAPSHOT_TYPE, limit=400)
    confirmation_date_count = len(
        {str(item.get("snapshot_date") or "")[:10] for item in previous if item.get("snapshot_date")}
    ) + 1
    build = build_cn_forward_shadow_snapshot(
        adapted.records,
        universe=universe,
        feature_date=parsed_feature_date,
        decision_cutoff=cutoff,
        source_version=_source_version(adapted.records),
        revision_history_preserved=adapted.revision_history_preserved,
        config=resolved,
    )
    payload = {
        **dict(build.payload),
        "confirmation_date_count": confirmation_date_count,
        "minimum_confirmation_dates": resolved.minimum_confirmation_dates,
        "remaining_confirmation_dates": max(
            0, resolved.minimum_confirmation_dates - confirmation_date_count
        ),
        "adapter_rejected_row_count": adapted.rejected_row_count,
    }
    evidence_version, scores_path, manifest_path = _persist_shadow_artifact(
        payload=payload,
        score_rows=build.score_rows,
        artifacts_root=get_settings().artifacts_dir,
    )
    payload.update(
        {
            "evidence_version": evidence_version,
            "scores_path": str(scores_path),
            "manifest_path": str(manifest_path),
        }
    )
    snapshot = snapshot_repo.create_snapshot(
        snapshot_type=CN_FORWARD_SHADOW_SNAPSHOT_TYPE,
        snapshot_date=effective_trade_date,
        payload=payload,
        source_job_id=source_job_id,
    )
    return {
        "status": str(payload.get("status") or "collecting"),
        "reused_existing": False,
        "snapshot_id": snapshot.id,
        "snapshot_date": snapshot.snapshot_date,
        "payload": payload,
    }


def load_latest_cn_forward_shadow_snapshot(db: Session) -> dict | None:
    return WorkspaceSnapshotRepository(db).get_latest_snapshot(CN_FORWARD_SHADOW_SNAPSHOT_TYPE)
