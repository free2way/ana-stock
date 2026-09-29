from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.production_research import (
    ProductionResearchRunConfig,
    research_run_scope,
    run_production_research_challenger,
)
from app.services.stock_selection.factor_sets import research_factor_sets
from app.services.execution_costs import FillCostModel
from app.services.stock_selection.execution_evidence import ResearchExecutionEvidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the point-in-time stock-selection challenger without changing the production champion."
    )
    parser.add_argument("--market", required=True, choices=("CN", "US"))
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument("--prediction-days", type=int, default=20)
    parser.add_argument("--pilot-ticker-limit", type=int)
    parser.add_argument("--minimum-training-dates", type=int, default=120)
    parser.add_argument("--minimum-training-samples", type=int, default=5000)
    parser.add_argument("--ranker-estimators", type=int, default=120)
    parser.add_argument("--top-tail-n", type=int, default=5)
    parser.add_argument("--two-stage-validation-days", type=int, default=20)
    parser.add_argument("--two-stage-minimum-validation-days", type=int, default=10)
    parser.add_argument("--history-limit-per-symbol", type=int, default=320)
    parser.add_argument("--models", default="equal_weight,ridge,lambdarank")
    parser.add_argument("--round-trip-cost-bps", type=float)
    parser.add_argument("--label-protocol", choices=("legacy", "cash_net_v2"), default="legacy")
    parser.add_argument("--commission-bps-one-way", type=float)
    parser.add_argument("--slippage-bps-one-way", type=float)
    parser.add_argument("--execution-evidence", type=Path)
    parser.add_argument("--target-mode", choices=("risk_adjusted_return", "net_return"),
                        default="risk_adjusted_return")
    parser.add_argument(
        "--factor-set",
        default="original_v1",
        choices=tuple(sorted(research_factor_sets())),
    )
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    if args.label_protocol == "cash_net_v2":
        if args.execution_evidence is None or args.commission_bps_one_way is None or args.slippage_bps_one_way is None:
            parser.error("cash_net_v2 requires execution evidence and explicit one-way cost parameters")
        if args.target_mode != "net_return" or args.round_trip_cost_bps is not None:
            parser.error("cash_net_v2 requires target-mode=net_return and forbids round-trip-cost-bps")
    elif any(value is not None for value in (args.execution_evidence, args.commission_bps_one_way, args.slippage_bps_one_way)):
        parser.error("per-fill cost/evidence flags require cash_net_v2")
    return args


def main() -> None:
    args = parse_args()
    horizons = tuple(int(value.strip()) for value in args.horizons.split(",") if value.strip())
    model_keys = tuple(value.strip() for value in args.models.split(",") if value.strip())
    fill_cost_model = (FillCostModel(args.commission_bps_one_way, args.slippage_bps_one_way)
                       if args.label_protocol == "cash_net_v2" else None)
    execution_evidence = ResearchExecutionEvidence.from_path(args.execution_evidence) if args.execution_evidence else None
    result = run_production_research_challenger(
        config=ProductionResearchRunConfig(
            market=args.market,
            horizons=horizons,
            pilot_ticker_limit=args.pilot_ticker_limit,
            prediction_date_count=args.prediction_days,
            minimum_training_dates=args.minimum_training_dates,
            minimum_training_samples=args.minimum_training_samples,
            ranker_estimators=args.ranker_estimators,
            top_tail_n=args.top_tail_n,
            two_stage_validation_dates=args.two_stage_validation_days,
            two_stage_minimum_validation_dates=args.two_stage_minimum_validation_days,
            factor_set_key=args.factor_set,
            history_limit_per_symbol=args.history_limit_per_symbol,
            model_keys=model_keys,
            round_trip_cost_bps=0 if fill_cost_model else (args.round_trip_cost_bps if args.round_trip_cost_bps is not None else 20.0),
            drawdown_penalty=0 if fill_cost_model else 0.25,
            fill_cost_model=fill_cost_model,
            target_mode=args.target_mode,
        ),
        artifact_root=args.artifact_root,
        execution_evidence=execution_evidence,
    )
    payload = {
        "status": "success",
        "scope": research_run_scope(result.inputs.selection_mode),
        "market": result.inputs.market,
        "selection_mode": result.inputs.selection_mode,
        "ticker_count": len(result.inputs.selected_tickers),
        "trading_date_count": len(result.dataset.trading_dates),
        "eligible_sample_count": result.dataset.sample_result.eligible_count,
        "dataset_version": result.dataset.sample_result.dataset_version,
        "universe_version": result.dataset.universe_result.universe_version,
        "factor_set_key": result.factor_set.key if result.factor_set else None,
        "factor_set_version": result.factor_set.version() if result.factor_set else None,
        "round_trip_cost_bps": result.config.round_trip_cost_bps,
        "target_mode": result.config.target_mode,
        "label_contract": dict(result.dataset.sample_result.label_contract),
        "universe_artifact": str(result.universe_artifact.artifact_dir),
        "sample_artifact": str(result.sample_artifact.artifact_dir),
        "horizons": {
            str(horizon): {
                "evaluated_dates": len(comparison.common_evaluated_dates),
                "evaluation_sample_count": comparison.evaluation_sample_count,
                "reports": {
                    model_key: {
                        "rank_ic_mean": report.rank_ic_mean,
                        "rank_ic_ir": report.rank_ic_ir,
                        "top_minus_bottom_mean": report.top_minus_bottom_mean,
                        "top_n_mean_label": {
                            str(top_n): metrics.mean_label
                            for top_n, metrics in report.top_n_metrics.items()
                        },
                        "positive_oracle_precision_at_n": {
                            str(top_n): metrics.positive_oracle_precision_at_n
                            for top_n, metrics in report.top_n_metrics.items()
                        },
                    }
                    for model_key, report in comparison.reports.items()
                },
                "evidence_artifact": str(result.evidence_artifacts[horizon].artifact_dir),
            }
            for horizon, comparison in result.comparisons.items()
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
