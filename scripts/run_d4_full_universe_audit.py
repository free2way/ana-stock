"""D-4 full-CN-universe walk-forward leakage audit via explicit tickers.

This script intentionally drives the ``explicit_tickers`` input path of
``run_production_research_challenger``: when ``tickers`` is supplied the
``require_historical_universe_contract`` readiness gate is skipped. That makes
it possible to exercise the walk-forward leakage auditor over the full CN
universe width while the PIT historical-universe contract gap is still
unresolved with external data. The result is therefore NOT an official
model-selection run and must be reported with an explicit scope caveat.

The output JSON mirrors ``scripts/run_stock_selection_research.py`` so the
existing evidence tooling can consume it, and adds a ``universe_selection``
block describing exactly where the tickers came from.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.factor_sets import research_factor_sets  # noqa: E402
from app.services.stock_selection.production_research import (  # noqa: E402
    ProductionResearchRunConfig,
    research_run_scope,
    run_production_research_challenger,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="D-4 full-CN-universe leakage audit using the explicit-tickers path."
    )
    parser.add_argument("--market", default="CN", choices=("CN", "US"))
    parser.add_argument("--horizons", default="5")
    parser.add_argument("--prediction-days", type=int, default=5)
    parser.add_argument("--models", default="ridge")
    parser.add_argument("--minimum-training-dates", type=int, default=120)
    parser.add_argument("--minimum-training-samples", type=int, default=5000)
    parser.add_argument("--ranker-estimators", type=int, default=120)
    parser.add_argument("--history-limit-per-symbol", type=int, default=320)
    parser.add_argument(
        "--universes-root",
        type=Path,
        default=ROOT_DIR / "data/artifacts/stock_selection_research/universes",
    )
    parser.add_argument(
        "--universe-dir",
        type=Path,
        default=None,
        help="Explicit pit_universe_v1_* directory. Defaults to the newest full_market_lake universe.",
    )
    parser.add_argument("--max-tickers", type=int, default=None)
    parser.add_argument("--factor-set", default="original_v1", choices=tuple(sorted(research_factor_sets())))
    parser.add_argument("--artifact-root", type=Path, default=None)
    return parser.parse_args()


def _distinct_included_tickers(universe_dir: Path) -> list[str]:
    table = pq.read_table(universe_dir / "universe.parquet", columns=["ticker", "included"])
    included = table.filter(pc.equal(table["included"], True))
    return sorted(pc.unique(included["ticker"]).to_pylist())


def _select_full_market_universe(universes_root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    candidates: list[tuple[float, str, Path]] = []
    for manifest_path in universes_root.glob("pit_universe_v1_*_*/manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        if "full_market_lake" not in str(manifest.get("source_version", "")):
            continue
        candidates.append((manifest_path.stat().st_mtime, manifest_path.parent.name, manifest_path.parent))
    if not candidates:
        raise RuntimeError(f"no full_market_lake universe found under {universes_root}")
    candidates.sort(reverse=True)
    return candidates[0][2]


def main() -> None:
    args = parse_args()
    horizons = tuple(int(value.strip()) for value in args.horizons.split(",") if value.strip())
    model_keys = tuple(value.strip() for value in args.models.split(",") if value.strip())

    universe_dir = _select_full_market_universe(args.universes_root, args.universe_dir)
    tickers = _distinct_included_tickers(universe_dir)
    if args.max_tickers is not None:
        tickers = tickers[: args.max_tickers]
    manifest = json.loads((universe_dir / "manifest.json").read_text())

    result = run_production_research_challenger(
        config=ProductionResearchRunConfig(
            market=args.market,
            horizons=horizons,
            pilot_ticker_limit=None,
            prediction_date_count=args.prediction_days,
            minimum_training_dates=args.minimum_training_dates,
            minimum_training_samples=args.minimum_training_samples,
            ranker_estimators=args.ranker_estimators,
            factor_set_key=args.factor_set,
            history_limit_per_symbol=args.history_limit_per_symbol,
            model_keys=model_keys,
        ),
        tickers=tickers,
        artifact_root=args.artifact_root,
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
        "universe_selection": {
            "source": "pit_universe_artifact_included_tickers",
            "requested_universe_dir": str(universe_dir),
            "requested_universe_version": manifest.get("universe_version"),
            "requested_universe_source_version": manifest.get("source_version"),
            "requested_ticker_count": len(tickers),
            "readiness_gate": "skipped_explicit_tickers_path",
        },
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
                "fold_audits": [
                    {
                        "prediction_date": audit.prediction_date.isoformat(),
                        "leakage_violation_count": audit.leakage_violation_count,
                        "training_sample_count": audit.training_sample_count,
                        "training_date_count": audit.training_date_count,
                        "evaluation_sample_count": audit.evaluation_sample_count,
                        "model_status": dict(audit.model_status),
                    }
                    for audit in comparison.fold_audits
                ],
                "evidence_artifact": str(result.evidence_artifacts[horizon].artifact_dir),
            }
            for horizon, comparison in result.comparisons.items()
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
