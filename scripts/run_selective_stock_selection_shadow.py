from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sys

import polars as pl


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.factor_baseline import BaselinePrediction
from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.selective_calibration import (
    SelectiveCalibrationConfig,
    run_selective_walk_forward,
)
from app.services.stock_selection.selective_policy import (
    SelectiveEvaluationConfig,
    SelectivePolicyConfig,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay the abstention-capable stock selector from immutable OOS artifacts."
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--market", default="CN")
    parser.add_argument("--round-trip-cost-bps", type=float, required=True)
    parser.add_argument("--calibration-bins", type=int, default=10)
    parser.add_argument("--calibration-min-observations", type=int, default=5000)
    parser.add_argument("--calibration-prior-strength", type=float, default=30.0)
    parser.add_argument("--calibration-lookback-dates", type=int, default=60)
    parser.add_argument("--max-selected", type=int, default=5)
    parser.add_argument("--minimum-probability", type=float, default=0.60)
    parser.add_argument("--minimum-expected-return", type=float, default=0.003)
    parser.add_argument("--minimum-rank", type=float, default=0.80)
    parser.add_argument("--maximum-uncertainty", type=float, default=0.20)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _load_inputs(
    *,
    evidence_dir: Path,
    dataset_dir: Path,
    model_key: str,
) -> tuple[list[BaselinePrediction], list[FactorScore], dict[str, object]]:
    evidence_manifest = json.loads(
        (evidence_dir / "manifest.json").read_text(encoding="utf-8")
    )
    dataset_manifest = json.loads(
        (dataset_dir / "manifest.json").read_text(encoding="utf-8")
    )
    if evidence_manifest["dataset_version"] != dataset_manifest["dataset_version"]:
        raise ValueError("evidence and dataset manifests have different dataset_version values")

    prediction_frame = (
        pl.scan_parquet(evidence_dir / "predictions.parquet")
        .filter(pl.col("model_key") == model_key)
        .collect()
    )
    if prediction_frame.height == 0:
        raise ValueError(f"model_key {model_key!r} is absent from prediction evidence")
    label_frame = (
        pl.scan_parquet(dataset_dir / "samples.parquet")
        .select(
            "sample_id",
            "ticker",
            "feature_date",
            "label_available_date",
            "horizon_days",
            "label_value",
            "label_components_json",
        )
        .join(prediction_frame.lazy().select("sample_id"), on="sample_id", how="inner")
        .collect()
    )
    if label_frame.height != prediction_frame.height:
        raise ValueError("prediction evidence and sample labels do not align one-to-one")

    predictions = [
        BaselinePrediction(
            sample_id=str(item["sample_id"]),
            ticker=str(item["ticker"]),
            feature_date=date.fromisoformat(str(item["feature_date"])),
            horizon_days=int(item["horizon_days"]),
            raw_score=float(item["raw_score"]),
            cross_sectional_rank=float(item["cross_sectional_rank"]),
            model_version=str(item["model_version"]),
        )
        for item in prediction_frame.iter_rows(named=True)
    ]
    labels = [
        FactorScore(
            sample_id=str(item["sample_id"]),
            ticker=str(item["ticker"]),
            feature_date=date.fromisoformat(str(item["feature_date"])),
            label_available_date=date.fromisoformat(str(item["label_available_date"])),
            horizon_days=int(item["horizon_days"]),
            factor_values={},
            missing_factors=(),
            composite_score=0.0,
            cross_sectional_rank=0.0,
            label_value=float(item["label_value"]),
            label_components={
                key: float(value)
                for key, value in json.loads(str(item["label_components_json"])).items()
            },
        )
        for item in label_frame.iter_rows(named=True)
    ]
    return predictions, labels, {
        "evidence_version": evidence_manifest["evidence_version"],
        "dataset_version": dataset_manifest["dataset_version"],
    }


def main() -> None:
    args = parse_args()
    predictions, labels, source = _load_inputs(
        evidence_dir=args.evidence_dir,
        dataset_dir=args.dataset_dir,
        model_key=args.model_key,
    )
    calibration_config = SelectiveCalibrationConfig(
        bin_count=args.calibration_bins,
        minimum_observations=args.calibration_min_observations,
        prior_strength=args.calibration_prior_strength,
    )
    policy_config = SelectivePolicyConfig(
        max_selected_per_date=args.max_selected,
        minimum_positive_probability=args.minimum_probability,
        minimum_expected_risk_adjusted_return=args.minimum_expected_return,
        minimum_cross_sectional_rank=args.minimum_rank,
        maximum_uncertainty=args.maximum_uncertainty,
    )
    result = run_selective_walk_forward(
        predictions,
        labels,
        evaluation_config=SelectiveEvaluationConfig(
            market=args.market,
            model_key=args.model_key,
            round_trip_cost_bps=args.round_trip_cost_bps,
        ),
        calibration_config=calibration_config,
        policy_config=policy_config,
        calibration_lookback_dates=args.calibration_lookback_dates,
    )
    successful = [item for item in result.audits if item.status == "success"]
    skipped = [item for item in result.audits if item.status != "success"]
    evaluation = asdict(result.evaluation) if result.evaluation is not None else None
    if evaluation is not None:
        evaluation.pop("daily_metrics", None)
    payload = {
        "status": "success",
        "scope": "shadow_research_only",
        "source": source,
        "model_key": args.model_key,
        "prediction_row_count": len(predictions),
        "prediction_date_count": len(result.audits),
        "successful_decision_date_count": len(successful),
        "active_decision_date_count": sum(item.selected_count > 0 for item in successful),
        "abstention_decision_date_count": sum(item.selected_count == 0 for item in successful),
        "maximum_calibrated_probability": (
            max(
                item.maximum_calibrated_probability
                for item in successful
                if item.maximum_calibrated_probability is not None
            )
            if successful
            else None
        ),
        "maximum_expected_risk_adjusted_return": (
            max(
                item.maximum_expected_risk_adjusted_return
                for item in successful
                if item.maximum_expected_risk_adjusted_return is not None
            )
            if successful
            else None
        ),
        "skipped_calibration_date_count": len(skipped),
        "calibration_config": asdict(calibration_config),
        "calibration_lookback_dates": args.calibration_lookback_dates,
        "policy_config": asdict(policy_config),
        "evaluation": evaluation,
        "last_five_audits": [asdict(item) for item in result.audits[-5:]],
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
