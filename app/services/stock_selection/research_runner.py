from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

from app.services.stock_selection.evaluation import (
    CrossSectionalEvaluationConfig,
    CrossSectionalEvaluationReport,
    evaluate_cross_sectional_predictions,
)
from app.services.stock_selection.factor_baseline import (
    BaselinePrediction,
    equal_weight_predictions,
    fit_ridge_factor_baseline,
)
from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorScore,
)
from app.services.stock_selection.ranker import (
    RankerConfig,
    RankerPrediction,
    fit_cross_sectional_ranker,
)
from app.services.stock_selection.p1_sampling import (
    P1SamplingConfig,
    build_p1_training_weights,
)
from app.services.stock_selection.regime_policy import evaluate_regime_policy
from app.services.stock_selection.top_tail import (
    TopTailConfig,
    TopTailPrediction,
    fit_top_tail_classifier,
)
from app.services.stock_selection.two_stage import (
    TwoStageConfig,
    TwoStagePrediction,
    fit_two_stage_selector,
)
from app.services.stock_selection.schemas import LabeledSample
from app.services.stock_selection.walk_forward import (
    PointInTimeTrainingPool,
    audit_training_samples,
)


Prediction = BaselinePrediction | RankerPrediction | TopTailPrediction | TwoStagePrediction


@dataclass(frozen=True, slots=True)
class WalkForwardComparisonConfig:
    horizon_days: int
    purge_sessions: int
    embargo_sessions: int = 0
    minimum_training_dates: int = 20
    minimum_training_samples: int = 100
    ridge_alpha: float = 10.0
    top_tail_n: int = 5
    top_tail_estimators: int = 80
    two_stage_validation_dates: int = 20
    two_stage_minimum_validation_dates: int = 10
    top_ns: tuple[int, ...] = (5, 10, 20)
    quantile_count: int = 5
    require_all_prediction_dates: bool = False
    model_keys: tuple[str, ...] = ("equal_weight", "ridge", "lambdarank")
    regime_policy_mode: str = "not_evaluated"
    training_sampling: P1SamplingConfig | None = None

    def __post_init__(self) -> None:
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.purge_sessions < self.horizon_days:
            raise ValueError("purge_sessions must be at least horizon_days")
        if self.embargo_sessions < 0:
            raise ValueError("embargo_sessions must not be negative")
        if self.minimum_training_dates < 1 or self.minimum_training_samples < 2:
            raise ValueError("minimum training requirements are invalid")
        if self.ridge_alpha < 0:
            raise ValueError("ridge_alpha must not be negative")
        if self.top_tail_n <= 0 or self.top_tail_estimators <= 0:
            raise ValueError("top-tail parameters must be positive")
        if (
            self.two_stage_minimum_validation_dates < 2
            or self.two_stage_validation_dates < self.two_stage_minimum_validation_dates
        ):
            raise ValueError("two-stage validation date requirements are invalid")
        allowed_models = {"equal_weight", "ridge", "lambdarank", "top_tail", "two_stage"}
        if not self.model_keys or any(item not in allowed_models for item in self.model_keys):
            raise ValueError("model_keys contains an unsupported model")
        if len(set(self.model_keys)) != len(self.model_keys):
            raise ValueError("model_keys must not contain duplicates")
        if self.regime_policy_mode not in {"not_evaluated", "historical_required"}:
            raise ValueError("unsupported regime_policy_mode")
        if (
            self.training_sampling is not None
            and self.training_sampling.mode == "decay_plus_regime_v1"
            and self.regime_policy_mode != "historical_required"
        ):
            raise ValueError("regime-matched sampling requires historical regime policy mode")
        if (
            self.training_sampling is not None
            and {"top_tail", "two_stage"} & set(self.model_keys)
        ):
            raise ValueError("P1 weighted sampling currently supports Ridge/LambdaRank challengers only")


@dataclass(frozen=True, slots=True)
class WalkForwardFoldAudit:
    prediction_date: date
    evaluation_sample_count: int
    training_sample_count: int
    training_date_count: int
    leakage_violation_count: int
    model_status: Mapping[str, str]
    model_metadata: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    training_start_date: date | None = None
    training_end_date: date | None = None
    maximum_label_available_date: date | None = None
    purge_sessions: int = 0
    embargo_sessions: int = 0
    preprocessing_scope: str = "fit_free_cross_section_by_feature_date"
    regime_policy: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WalkForwardComparisonResult:
    horizon_days: int
    common_evaluated_dates: tuple[date, ...]
    evaluation_sample_count: int
    reports: Mapping[str, CrossSectionalEvaluationReport]
    predictions: Mapping[str, tuple[Prediction, ...]]
    fold_audits: tuple[WalkForwardFoldAudit, ...]
    purge_sessions: int
    embargo_sessions: int
    preprocessing_scope: str
    regime_policy_mode: str
    regime_policy_by_date: Mapping[date, Mapping[str, object]]
    regime_candidate_sample_ids: Mapping[str, Mapping[date, tuple[str, ...]]]
    regime_coverage: Mapping[str, object]
    training_sampling_mode: str
    training_sampling_config_version: str


def _regime_inputs(
    *,
    market: str,
    prediction_dates: Sequence[date],
    config: WalkForwardComparisonConfig,
    snapshots: Mapping[date, Mapping[str, object]] | None,
    cutoffs: Mapping[date, str] | None,
) -> tuple[dict[date, Mapping[str, object]], dict[str, object]]:
    snapshot_map = dict(snapshots or {})
    cutoff_map = dict(cutoffs or {})
    requested = set(prediction_dates)
    if any(not isinstance(key, date) for key in (*snapshot_map, *cutoff_map)):
        raise ValueError("regime evidence keys must be date objects")
    if (set(snapshot_map) | set(cutoff_map)) - requested:
        raise ValueError("regime evidence contains dates outside the requested OOS panel")
    if config.regime_policy_mode == "not_evaluated":
        if snapshot_map or cutoff_map:
            raise ValueError("regime evidence requires historical_required mode")
        return {}, {
            "schema_version": "regime_research_coverage_v1",
            "status": "NOT_EVALUATED",
            "requested_date_count": len(prediction_dates),
            "snapshot_date_count": 0,
            "cutoff_date_count": 0,
            "evidence_complete": False,
            "allow_date_count": 0,
            "review_date_count": 0,
            "blocked_date_count": 0,
            "research_only": True,
        }
    policies = {
        prediction_date: evaluate_regime_policy(
            snapshot_map.get(prediction_date),
            market=market,
            expected_market_date=prediction_date.isoformat(),
            decision_cutoff_at=cutoff_map.get(prediction_date, ""),
            max_new_candidates=5,
        )
        for prediction_date in prediction_dates
    }
    gate_counts = {
        gate: sum(policy["buy_gate"] == gate for policy in policies.values())
        for gate in ("ALLOW", "REVIEW", "BLOCK")
    }
    evidence_complete = requested <= set(snapshot_map) and requested <= set(cutoff_map)
    return policies, {
        "schema_version": "regime_research_coverage_v1",
        "status": "COMPLETE" if evidence_complete else "BLOCKED_MISSING_EVIDENCE",
        "requested_date_count": len(prediction_dates),
        "snapshot_date_count": len(snapshot_map),
        "cutoff_date_count": len(cutoff_map),
        "evidence_complete": evidence_complete,
        "allow_date_count": gate_counts["ALLOW"],
        "review_date_count": gate_counts["REVIEW"],
        "blocked_date_count": gate_counts["BLOCK"],
        "research_only": True,
    }


def _regime_candidate_layer(
    predictions_by_model: Mapping[str, tuple[Prediction, ...]],
    policies: Mapping[date, Mapping[str, object]],
) -> dict[str, dict[date, tuple[str, ...]]]:
    output: dict[str, dict[date, tuple[str, ...]]] = {}
    for model_key, predictions in predictions_by_model.items():
        grouped: dict[date, list[Prediction]] = defaultdict(list)
        for prediction in predictions:
            grouped[prediction.feature_date].append(prediction)
        output[model_key] = {}
        for feature_date, group in sorted(grouped.items()):
            policy = policies.get(feature_date)
            if not policy:
                continue
            cap = int(policy["max_new_candidates"])
            ranked = sorted(
                group,
                key=lambda item: (-float(item.cross_sectional_rank), -float(item.raw_score), item.ticker, item.sample_id),
            )
            output[model_key][feature_date] = tuple(item.sample_id for item in ranked[:cap])
    return output


def _usable_ranker_group_count(scores: Iterable[FactorScore], min_group_size: int) -> int:
    grouped: dict[date, list[FactorScore]] = defaultdict(list)
    for item in scores:
        grouped[item.feature_date].append(item)
    usable = 0
    for group in grouped.values():
        labels = [item.label_value for item in group if item.label_value is not None]
        if len(group) >= min_group_size and len(labels) == len(group) and max(labels) != min(labels):
            usable += 1
    return usable


def run_walk_forward_model_comparison(
    samples: Iterable[LabeledSample],
    *,
    trading_dates: Sequence[date],
    prediction_dates: Sequence[date],
    factor_pipeline: CrossSectionalFactorPipeline,
    ranker_config: RankerConfig,
    config: WalkForwardComparisonConfig,
    regime_snapshots_by_date: Mapping[date, Mapping[str, object]] | None = None,
    regime_decision_cutoffs_by_date: Mapping[date, str] | None = None,
    historical_training_regime_by_date: Mapping[date, str] | None = None,
) -> WalkForwardComparisonResult:
    if ranker_config.horizon_days != config.horizon_days:
        raise ValueError("ranker and comparison horizons must match")
    calendar = list(trading_dates)
    if calendar != sorted(calendar) or len(set(calendar)) != len(calendar):
        raise ValueError("trading_dates must be unique and ascending")
    requested_dates = list(prediction_dates)
    if requested_dates != sorted(requested_dates) or len(set(requested_dates)) != len(requested_dates):
        raise ValueError("prediction_dates must be unique and ascending")
    missing_dates = [item for item in requested_dates if item not in set(calendar)]
    if missing_dates:
        raise ValueError("prediction_dates must exist in trading_dates")

    horizon_samples = [
        item
        for item in samples
        if item.horizon_days == config.horizon_days and item.tradable
    ]
    if not horizon_samples:
        raise ValueError("comparison received no tradable samples for configured horizon")
    if any(len({getattr(item, field) for item in horizon_samples}) != 1
           for field in ("market", "dataset_version", "target_mode")):
        raise ValueError("comparison cannot mix markets, dataset versions or target modes")
    market = horizon_samples[0].market
    regime_policies, regime_coverage = _regime_inputs(
        market=market,
        prediction_dates=requested_dates,
        config=config,
        snapshots=regime_snapshots_by_date,
        cutoffs=regime_decision_cutoffs_by_date,
    )
    sampling_config = config.training_sampling
    if sampling_config is None and historical_training_regime_by_date:
        raise ValueError("historical training regimes require an explicit P1 sampling config")
    if sampling_config is not None and sampling_config.mode == "decay_plus_regime_v1":
        if not regime_coverage["evidence_complete"]:
            raise ValueError("regime-matched sampling requires complete prediction-date regime evidence")
        if not historical_training_regime_by_date:
            raise ValueError("regime-matched sampling requires historical training regimes")
    sample_ids = [item.sample_id for item in horizon_samples]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("comparison sample_id values must be unique")

    samples_by_date: dict[date, list[LabeledSample]] = defaultdict(list)
    for sample in horizon_samples:
        samples_by_date[sample.feature_date].append(sample)
    pool = PointInTimeTrainingPool(
        horizon_samples,
        trading_dates=calendar,
        feature_date=lambda item: item.feature_date,
        label_end_date=lambda item: item.label_end_date,
        label_available_date=lambda item: item.label_available_date,
        sample_id=lambda item: item.sample_id,
        purge_sessions=config.purge_sessions,
        embargo_sessions=config.embargo_sessions,
    )

    # The factor pipeline is fit-free and normalizes strictly within each
    # feature-date cross section. Precomputing once is therefore identical to
    # repeated fold transforms and does not expose a future date to training.
    precomputed_scores = factor_pipeline.transform(horizon_samples)
    score_by_sample_id = {item.sample_id: item for item in precomputed_scores}
    if len(score_by_sample_id) != len(horizon_samples):
        raise ValueError("precomputed factor scores do not align with horizon samples")

    predictions_by_model: dict[str, list[Prediction]] = {
        model_key: [] for model_key in config.model_keys
    }
    label_scores_by_id: dict[str, FactorScore] = {}
    fold_audits: list[WalkForwardFoldAudit] = []
    for prediction_date in requested_dates:
        training_samples = list(pool.advance(prediction_date))
        evaluation_samples = sorted(
            samples_by_date.get(prediction_date, []),
            key=lambda item: (item.ticker, item.sample_id),
        )
        training_date_count = len({item.feature_date for item in training_samples})
        training_start_date = min(
            (item.feature_date for item in training_samples), default=None
        )
        training_end_date = max(
            (item.feature_date for item in training_samples), default=None
        )
        maximum_label_available_date = max(
            (item.label_available_date for item in training_samples), default=None
        )
        leakage_audit = audit_training_samples(
            training_samples,
            prediction_date=prediction_date,
            trading_dates=calendar,
            purge_sessions=config.purge_sessions,
            embargo_sessions=config.embargo_sessions,
        )
        if not leakage_audit.passed:
            raise ValueError(f"walk-forward leakage audit failed at {prediction_date.isoformat()}")
        statuses = {model_key: "not_run" for model_key in config.model_keys}
        model_metadata: dict[str, Mapping[str, object]] = {}
        if not evaluation_samples:
            statuses = {key: "skipped:no_evaluation_samples" for key in statuses}
            fold_audits.append(
                WalkForwardFoldAudit(
                    prediction_date=prediction_date,
                    evaluation_sample_count=0,
                    training_sample_count=len(training_samples),
                    training_date_count=training_date_count,
                    leakage_violation_count=0,
                    model_status=statuses,
                    training_start_date=training_start_date,
                    training_end_date=training_end_date,
                    maximum_label_available_date=maximum_label_available_date,
                    purge_sessions=config.purge_sessions,
                    embargo_sessions=config.embargo_sessions,
                    regime_policy=regime_policies.get(prediction_date),
                )
            )
            continue

        inference_scores = tuple(score_by_sample_id[item.sample_id] for item in evaluation_samples)
        labeled_evaluation_scores = inference_scores
        label_scores_by_id.update({item.sample_id: item for item in labeled_evaluation_scores})
        if "equal_weight" in predictions_by_model:
            predictions_by_model["equal_weight"].extend(equal_weight_predictions(inference_scores))
            statuses["equal_weight"] = "success"

        trainable_models = {"ridge", "lambdarank", "top_tail", "two_stage"} & set(predictions_by_model)
        if trainable_models and (
            len(training_samples) < config.minimum_training_samples
            or training_date_count < config.minimum_training_dates
        ):
            reason = "skipped:insufficient_training_history"
            for model_key in trainable_models:
                statuses[model_key] = reason
        elif trainable_models:
            training_scores = tuple(
                score_by_sample_id[item.sample_id] for item in training_samples
            )
            sampling_result = (
                build_p1_training_weights(
                    training_scores,
                    trading_dates=calendar,
                    prediction_date=prediction_date,
                    config=sampling_config,
                    historical_regime_by_date=historical_training_regime_by_date,
                    prediction_regime=(
                        regime_policies[prediction_date]["risk_regime"]
                        if sampling_config.mode == "decay_plus_regime_v1"
                        else None
                    ),
                )
                if sampling_config is not None
                else None
            )
            sampling_version = (
                sampling_config.version()
                if sampling_config is not None
                else "legacy_uniform_row_weight_v1"
            )
            if "ridge" in trainable_models:
                ridge_model = fit_ridge_factor_baseline(
                    training_scores,
                    factor_names=ranker_config.feature_names,
                    horizon_days=config.horizon_days,
                    prediction_date=prediction_date,
                    alpha=config.ridge_alpha,
                    sample_weight_by_id=(
                        sampling_result.weights_by_sample_id if sampling_result else None
                    ),
                    sampling_config_version=sampling_version,
                )
                predictions_by_model["ridge"].extend(ridge_model.predict(inference_scores))
                statuses["ridge"] = "success"
                model_metadata["ridge"] = {
                    "training_sampling": (
                        dict(sampling_result.audit) if sampling_result else {
                            "status": "LEGACY_CONTROL",
                            "applied_mode": "legacy_uniform_row_weight_v1",
                            "config_version": sampling_version,
                        }
                    ),
                    "effective_sample_count": ridge_model.training_effective_sample_count,
                }

            if "lambdarank" in trainable_models:
                if _usable_ranker_group_count(training_scores, ranker_config.min_group_size) == 0:
                    statuses["lambdarank"] = "skipped:no_usable_training_groups"
                else:
                    ranker_model = fit_cross_sectional_ranker(
                        training_scores,
                        config=ranker_config,
                        prediction_date=prediction_date,
                        sample_weight_by_id=(
                            sampling_result.weights_by_sample_id if sampling_result else None
                        ),
                        sampling_config_version=sampling_version,
                    )
                    predictions_by_model["lambdarank"].extend(ranker_model.predict(inference_scores))
                    statuses["lambdarank"] = "success"
                    model_metadata["lambdarank"] = {
                        "training_sampling": (
                            dict(sampling_result.audit) if sampling_result else {
                                "status": "LEGACY_CONTROL",
                                "applied_mode": "legacy_uniform_row_weight_v1",
                                "config_version": sampling_version,
                            }
                        ),
                        "effective_sample_count": (
                            ranker_model.audit.training_effective_sample_count
                        ),
                    }

            if "top_tail" in trainable_models:
                top_tail_model = fit_top_tail_classifier(
                    training_scores,
                    config=TopTailConfig(
                        feature_names=ranker_config.feature_names,
                        horizon_days=config.horizon_days,
                        target_top_n=config.top_tail_n,
                        min_group_size=max(config.top_tail_n, ranker_config.min_group_size),
                        n_estimators=config.top_tail_estimators,
                    ),
                    prediction_date=prediction_date,
                )
                predictions_by_model["top_tail"].extend(
                    top_tail_model.predict(inference_scores)
                )
                statuses["top_tail"] = "success"
                model_metadata["top_tail"] = {
                    "target_top_n": config.top_tail_n,
                    "positive_label_count": top_tail_model.audit.positive_label_count,
                    "negative_label_count": top_tail_model.audit.negative_label_count,
                }

            if "two_stage" in trainable_models:
                risk_factor_name = next(
                    (
                        name
                        for name in ("intraday_range_5d", "volatility_20d")
                        if name in ranker_config.feature_names
                    ),
                    None,
                )
                if risk_factor_name is None:
                    raise ValueError(
                        "two_stage requires intraday_range_5d or volatility_20d"
                    )
                two_stage_model = fit_two_stage_selector(
                    training_scores,
                    config=TwoStageConfig(
                        horizon_days=config.horizon_days,
                        risk_factor_name=risk_factor_name,
                        target_top_n=config.top_tail_n,
                        validation_dates=config.two_stage_validation_dates,
                        minimum_validation_dates=config.two_stage_minimum_validation_dates,
                    ),
                    prediction_date=prediction_date,
                )
                predictions_by_model["two_stage"].extend(
                    two_stage_model.predict(inference_scores)
                )
                statuses["two_stage"] = "success"
                selected_metric = next(
                    item
                    for item in two_stage_model.audit.fraction_metrics
                    if item.candidate_fraction
                    == two_stage_model.selected_candidate_fraction
                )
                model_metadata["two_stage"] = {
                    "risk_factor_name": risk_factor_name,
                    "selected_candidate_fraction": two_stage_model.selected_candidate_fraction,
                    "validation_start_date": two_stage_model.audit.validation_dates[0].isoformat(),
                    "validation_end_date": two_stage_model.audit.validation_dates[-1].isoformat(),
                    "validation_objective": selected_metric.selection_objective,
                }

        fold_audits.append(
            WalkForwardFoldAudit(
                prediction_date=prediction_date,
                evaluation_sample_count=len(evaluation_samples),
                training_sample_count=len(training_samples),
                training_date_count=training_date_count,
                leakage_violation_count=leakage_audit.violation_count,
                model_status=statuses,
                model_metadata=model_metadata,
                training_start_date=training_start_date,
                training_end_date=training_end_date,
                maximum_label_available_date=maximum_label_available_date,
                purge_sessions=config.purge_sessions,
                embargo_sessions=config.embargo_sessions,
                regime_policy=regime_policies.get(prediction_date),
            )
        )

    dates_by_model = {
        model_key: {item.feature_date for item in predictions}
        for model_key, predictions in predictions_by_model.items()
    }
    common_dates = set(requested_dates)
    for model_dates in dates_by_model.values():
        common_dates &= model_dates
    if not common_dates:
        status_summary = "; ".join(
            f"{item.prediction_date.isoformat()}[train_samples={item.training_sample_count},"
            f"train_dates={item.training_date_count},eval_samples={item.evaluation_sample_count}]="
            + ",".join(f"{key}:{value}" for key, value in sorted(item.model_status.items()))
            for item in fold_audits[-10:]
        )
        raise ValueError(
            "comparison has no common successful OOS dates across all models; "
            f"fold_status={status_summary}"
        )
    common_dates_tuple = tuple(sorted(common_dates))
    if config.require_all_prediction_dates and common_dates_tuple != tuple(requested_dates):
        missing = sorted(set(requested_dates) - set(common_dates_tuple))
        raise ValueError(
            "comparison did not complete every requested OOS date; "
            f"completed={len(common_dates_tuple)}, requested={len(requested_dates)}, "
            f"missing_first={missing[0].isoformat() if missing else 'none'}"
        )
    common_labels = tuple(
        item
        for item in label_scores_by_id.values()
        if item.feature_date in common_dates
    )
    common_predictions = {
        model_key: tuple(
            item for item in predictions if item.feature_date in common_dates
        )
        for model_key, predictions in predictions_by_model.items()
    }
    reports = {
        model_key: evaluate_cross_sectional_predictions(
            predictions,
            common_labels,
            config=CrossSectionalEvaluationConfig(
                model_key=model_key,
                top_ns=config.top_ns,
                quantile_count=config.quantile_count,
            ),
        )
        for model_key, predictions in common_predictions.items()
    }
    sample_counts = {item.sample_count for item in reports.values()}
    evaluated_date_sets = {item.evaluated_dates for item in reports.values()}
    if len(sample_counts) != 1 or len(evaluated_date_sets) != 1:
        raise ValueError("model reports are not aligned on the same OOS sample panel")
    regime_candidates = _regime_candidate_layer(common_predictions, regime_policies)
    return WalkForwardComparisonResult(
        horizon_days=config.horizon_days,
        common_evaluated_dates=common_dates_tuple,
        evaluation_sample_count=next(iter(sample_counts)),
        reports=reports,
        predictions=common_predictions,
        fold_audits=tuple(fold_audits),
        purge_sessions=config.purge_sessions,
        embargo_sessions=config.embargo_sessions,
        preprocessing_scope="fit_free_cross_section_by_feature_date",
        regime_policy_mode=config.regime_policy_mode,
        regime_policy_by_date=regime_policies,
        regime_candidate_sample_ids=regime_candidates,
        regime_coverage=regime_coverage,
        training_sampling_mode=(
            sampling_config.mode if sampling_config else "legacy_uniform_row_weight_v1"
        ),
        training_sampling_config_version=(
            sampling_config.version() if sampling_config else "legacy_uniform_row_weight_v1"
        ),
    )
