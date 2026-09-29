from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.repository import PointInTimeFeatureSnapshotRepository
from app.services.stock_selection.feature_availability import (
    FeatureAvailabilityConfig,
    adapt_point_in_time_feature_snapshots,
    assess_feature_availability,
    load_feature_audit_universe,
    persist_feature_availability_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit point-in-time feature availability without training a model."
    )
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--market", choices=("CN", "US"), required=True)
    parser.add_argument("--horizon-days", type=int, default=5)
    parser.add_argument("--analysis-dates", type=int, default=120)
    parser.add_argument("--exclude-tail-dates", type=int, default=120)
    parser.add_argument("--artifact-root", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    universe = load_feature_audit_universe(
        samples_path=args.dataset_dir / "samples.parquet",
        horizon_days=args.horizon_days,
        analysis_date_count=args.analysis_dates,
        exclude_tail_date_count=args.exclude_tail_dates,
    )
    with SessionLocal() as db:
        rows = PointInTimeFeatureSnapshotRepository(db).list_history_for_market(args.market)
    adapted = adapt_point_in_time_feature_snapshots(rows, market=args.market)
    timezone_name = "Asia/Shanghai" if args.market == "CN" else "America/New_York"
    report = assess_feature_availability(
        adapted.records,
        universe_by_date=universe,
        source_row_count=adapted.source_row_count,
        rejected_row_count=adapted.rejected_row_count,
        emitted_value_count=adapted.emitted_value_count,
        revision_history_preserved=adapted.revision_history_preserved,
        config=FeatureAvailabilityConfig(
            market=args.market,
            timezone_name=timezone_name,
        ),
    )
    root = args.artifact_root or (
        get_settings().artifacts_dir / "stock_selection_research" / "feature_availability"
    )
    dataset_manifest = json.loads(
        (args.dataset_dir / "manifest.json").read_text(encoding="utf-8")
    )
    evidence = persist_feature_availability_report(
        report,
        source_version=str(dataset_manifest["dataset_version"]),
        root=root,
    )
    print(
        json.dumps(
            {
                "status": "success",
                "report": asdict(report),
                "rejection_reasons": adapted.rejection_reasons,
                "evidence_version": evidence.evidence_version,
                "evidence_artifact": str(evidence.artifact_dir),
                "reused_existing": evidence.reused_existing,
            },
            default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
