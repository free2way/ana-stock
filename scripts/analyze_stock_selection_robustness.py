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
from app.services.stock_selection.robustness import (
    analyze_top_n_robustness,
    load_market_breadth_history,
    load_robustness_rows,
    persist_robustness_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recalculate Top-N robustness from immutable stock-selection evidence."
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument("--model-key", default="equal_weight")
    parser.add_argument("--factor-set-key", required=True)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--round-trip-cost-bps", type=float, required=True)
    parser.add_argument("--artifact-root", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset_manifest = json.loads(
        (args.dataset_dir / "manifest.json").read_text(encoding="utf-8")
    )
    rows = load_robustness_rows(
        predictions_path=args.evidence_dir / "predictions.parquet",
        samples_path=args.dataset_dir / "samples.parquet",
        model_key=args.model_key,
    )
    horizon_days = rows[0].horizon_days
    breadth_history = load_market_breadth_history(
        samples_path=args.dataset_dir / "samples.parquet",
        horizon_days=horizon_days,
    )
    report = analyze_top_n_robustness(
        rows,
        model_key=args.model_key,
        factor_set_key=args.factor_set_key,
        dataset_version=str(dataset_manifest["dataset_version"]),
        top_n=args.top_n,
        round_trip_cost_bps=args.round_trip_cost_bps,
        market=args.market,
        gate_breadth_history=breadth_history,
    )
    root = args.artifact_root or (
        get_settings().artifacts_dir / "stock_selection_research" / "robustness"
    )
    evidence = persist_robustness_report(report, root=root)
    print(
        json.dumps(
            {
                "status": "success",
                "report": asdict(report),
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
