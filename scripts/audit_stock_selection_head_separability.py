from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings
from app.services.stock_selection.head_separability import (
    analyze_head_separability,
    default_separability_score_definitions,
    load_separability_rows,
    persist_head_separability_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit whether point-in-time features separate the positive daily head."
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
    definitions = default_separability_score_definitions()
    factor_names = {
        component.factor_name
        for definition in definitions
        for component in definition.components
    }
    rows, dates = load_separability_rows(
        samples_path=args.dataset_dir / "samples.parquet",
        horizon_days=args.horizon_days,
        factor_names=factor_names,
        analysis_date_count=args.analysis_dates,
        exclude_tail_date_count=args.exclude_tail_dates,
    )
    manifest = json.loads((args.dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    report = analyze_head_separability(
        rows,
        market=args.market,
        dataset_version=str(manifest["dataset_version"]),
        horizon_days=args.horizon_days,
        analysis_dates=dates,
        excluded_tail_date_count=args.exclude_tail_dates,
        score_definitions=definitions,
    )
    root = args.artifact_root or (
        get_settings().artifacts_dir / "stock_selection_research" / "head_separability"
    )
    evidence = persist_head_separability_report(report, root=root)
    print(
        json.dumps(
            {
                "status": "success",
                "report": {
                    "market": report.market,
                    "dataset_version": report.dataset_version,
                    "horizon_days": report.horizon_days,
                    "analysis_start": report.analysis_dates[0],
                    "analysis_end": report.analysis_dates[-1],
                    "analysis_date_count": len(report.analysis_dates),
                    "excluded_tail_date_count": report.excluded_tail_date_count,
                    "verdict": report.verdict,
                    "separable_score_keys": report.separable_score_keys,
                    "summaries": [asdict(item) for item in report.summaries],
                },
                "evidence_version": evidence.evidence_version,
                "evidence_artifact": str(evidence.artifact_dir),
                "reused_existing": evidence.reused_existing,
            },
            default=lambda value: value.isoformat() if isinstance(value, date) else str(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
