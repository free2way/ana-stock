from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.production_research import (
    ProductionFactorDiagnosticConfig,
    run_production_factor_diagnostics,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose point-in-time stock-selection factor IC, direction, coverage, and redundancy."
    )
    parser.add_argument("--market", required=True, choices=("CN", "US"))
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--diagnostic-days", type=int, default=60)
    parser.add_argument("--pilot-ticker-limit", type=int, default=80)
    parser.add_argument("--full-market", action="store_true")
    parser.add_argument("--history-limit-per-symbol", type=int, default=200)
    parser.add_argument("--artifact-root", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_production_factor_diagnostics(
        config=ProductionFactorDiagnosticConfig(
            market=args.market,
            horizon_days=args.horizon,
            pilot_ticker_limit=None if args.full_market else args.pilot_ticker_limit,
            diagnostic_date_count=args.diagnostic_days,
            history_limit_per_symbol=args.history_limit_per_symbol,
        ),
        artifact_root=args.artifact_root,
    )
    payload = {
        "status": "success",
        "scope": result.run_scope,
        "market": result.inputs.market,
        "selection_mode": result.inputs.selection_mode,
        "ticker_count": len(result.inputs.selected_tickers),
        "dataset_version": result.dataset.sample_result.dataset_version,
        "horizon_days": result.config.horizon_days,
        "targets": {
            target: {
                "evaluated_date_count": len(report.evaluated_dates),
                "sample_count": report.sample_count,
                "recommendations": {
                    factor_name: summary.recommendation
                    for factor_name, summary in report.factor_summaries.items()
                },
                "rank_ic_mean": {
                    factor_name: summary.rank_ic_mean
                    for factor_name, summary in report.factor_summaries.items()
                },
                "high_correlation_pairs": [
                    {
                        "left_factor": item.left_factor,
                        "right_factor": item.right_factor,
                        "mean_rank_correlation": item.mean_rank_correlation,
                    }
                    for item in report.high_correlation_pairs
                ],
                "evidence_artifact": str(result.evidence_artifacts[target].artifact_dir),
            }
            for target, report in result.reports.items()
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
