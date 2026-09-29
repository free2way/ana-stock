from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorDirection,
    FactorSpec,
)
from app.services.stock_selection.ranker import RankerConfig
from app.services.stock_selection.research_runner import (
    WalkForwardComparisonConfig,
    run_walk_forward_model_comparison,
)
from app.services.stock_selection.research_artifacts import persist_walk_forward_evidence
from app.services.stock_selection.schemas import LabeledSample


class WalkForwardModelComparisonTests(TestCase):
    def setUp(self) -> None:
        self.trading_dates = [date(2026, 1, 2) + timedelta(days=index) for index in range(18)]
        self.prediction_dates = self.trading_dates[10:14]
        self.pipeline = CrossSectionalFactorPipeline(
            [
                FactorSpec("momentum", FactorDirection.HIGHER_BETTER),
                FactorSpec("risk", FactorDirection.LOWER_BETTER),
            ]
        )
        self.ranker_config = RankerConfig(
            feature_names=("momentum", "risk"),
            horizon_days=3,
            min_group_size=5,
            n_estimators=25,
            learning_rate=0.1,
            num_leaves=7,
            min_child_samples=2,
            subsample=1.0,
            colsample_bytree=1.0,
        )
        self.comparison_config = WalkForwardComparisonConfig(
            horizon_days=3,
            purge_sessions=3,
            minimum_training_dates=4,
            minimum_training_samples=24,
            ridge_alpha=1.0,
            top_ns=(2, 4),
            quantile_count=4,
        )
        self.samples = self._build_samples()

    def _build_samples(self) -> list[LabeledSample]:
        samples: list[LabeledSample] = []
        for date_index in range(15):
            feature_date = self.trading_dates[date_index]
            for ticker_index in range(8):
                ticker = f"S{ticker_index:02d}"
                momentum = float(ticker_index) + date_index * 0.02
                samples.append(
                    LabeledSample(
                        sample_id=f"{feature_date.isoformat()}:{ticker}:3",
                        market="US",
                        ticker=ticker,
                        feature_date=feature_date,
                        label_start_date=self.trading_dates[date_index + 1],
                        label_end_date=self.trading_dates[date_index + 3],
                        label_available_date=self.trading_dates[date_index + 3],
                        horizon_days=3,
                        label_value=momentum - ticker_index * 0.01,
                        features={
                            "momentum": momentum,
                            "risk": float(8 - ticker_index),
                        },
                        dataset_version="walk-forward-fixture-v1",
                    )
                )
        return samples

    def _run(self, samples: list[LabeledSample]):
        return run_walk_forward_model_comparison(
            samples,
            trading_dates=self.trading_dates,
            prediction_dates=self.prediction_dates,
            factor_pipeline=self.pipeline,
            ranker_config=self.ranker_config,
            config=self.comparison_config,
        )

    def _regime_evidence(self, *, crash_date: date | None = None):
        snapshots = {}
        cutoffs = {}
        for prediction_date in self.prediction_dates:
            snapshots[prediction_date] = {
                "market": "US",
                "snapshot_date": prediction_date.isoformat(),
                "generated_at": f"{prediction_date.isoformat()}T17:00:00-05:00",
                "risk_regime": "crash" if prediction_date == crash_date else "risk_on",
                "buy_gate": "BLOCK" if prediction_date == crash_date else "ALLOW",
                "max_position_scale": 0.0 if prediction_date == crash_date else 1.0,
            }
            cutoffs[prediction_date] = f"{prediction_date.isoformat()}T18:00:00-05:00"
        return snapshots, cutoffs

    def test_models_share_identical_strict_oos_panel(self) -> None:
        result = self._run(self.samples)

        self.assertEqual(tuple(self.prediction_dates), result.common_evaluated_dates)
        self.assertEqual(32, result.evaluation_sample_count)
        self.assertEqual({"equal_weight", "ridge", "lambdarank"}, set(result.reports))
        self.assertEqual(
            {tuple(self.prediction_dates)},
            {report.evaluated_dates for report in result.reports.values()},
        )
        self.assertEqual({32}, {report.sample_count for report in result.reports.values()})
        self.assertTrue(all(item.leakage_violation_count == 0 for item in result.fold_audits))
        self.assertEqual(3, result.purge_sessions)
        self.assertEqual(0, result.embargo_sessions)
        self.assertTrue(
            all(item.purge_sessions == 3 for item in result.fold_audits)
        )
        self.assertTrue(
            all(
                item.maximum_label_available_date is None
                or item.maximum_label_available_date < item.prediction_date
                for item in result.fold_audits
            )
        )
        self.assertTrue(
            all(
                item.training_start_date is None
                or item.training_start_date <= item.training_end_date
                for item in result.fold_audits
            )
        )
        self.assertTrue(
            all(item.model_status["lambdarank"] == "success" for item in result.fold_audits)
        )
        self.assertEqual("NOT_EVALUATED", result.regime_coverage["status"])
        self.assertEqual({}, result.regime_candidate_sample_ids["lambdarank"])

    def test_historical_regime_layer_preserves_full_panel_and_blocks_crash_candidates(self) -> None:
        crash_date = self.prediction_dates[1]
        snapshots, cutoffs = self._regime_evidence(crash_date=crash_date)
        result = run_walk_forward_model_comparison(
            self.samples,
            trading_dates=self.trading_dates,
            prediction_dates=self.prediction_dates,
            factor_pipeline=self.pipeline,
            ranker_config=self.ranker_config,
            config=replace(self.comparison_config, regime_policy_mode="historical_required"),
            regime_snapshots_by_date=snapshots,
            regime_decision_cutoffs_by_date=cutoffs,
        )
        self.assertEqual(32, result.evaluation_sample_count)
        self.assertEqual("COMPLETE", result.regime_coverage["status"])
        self.assertEqual(1, result.regime_coverage["blocked_date_count"])
        for model_key, by_date in result.regime_candidate_sample_ids.items():
            self.assertEqual(0, len(by_date[crash_date]), model_key)
            self.assertTrue(all(len(by_date[day]) == 5 for day in self.prediction_dates if day != crash_date))
        self.assertEqual("BLOCK", result.regime_policy_by_date[crash_date]["buy_gate"])
        self.assertTrue(all(fold.regime_policy is not None for fold in result.fold_audits))

    def test_required_regime_layer_fails_closed_on_missing_evidence(self) -> None:
        result = run_walk_forward_model_comparison(
            self.samples,
            trading_dates=self.trading_dates,
            prediction_dates=self.prediction_dates,
            factor_pipeline=self.pipeline,
            ranker_config=self.ranker_config,
            config=replace(self.comparison_config, regime_policy_mode="historical_required"),
        )
        self.assertEqual("BLOCKED_MISSING_EVIDENCE", result.regime_coverage["status"])
        self.assertEqual(len(self.prediction_dates), result.regime_coverage["blocked_date_count"])
        self.assertTrue(all(not ids for by_date in result.regime_candidate_sample_ids.values() for ids in by_date.values()))
        snapshots, cutoffs = self._regime_evidence()
        with self.assertRaisesRegex(ValueError, "historical_required"):
            run_walk_forward_model_comparison(
                self.samples,
                trading_dates=self.trading_dates,
                prediction_dates=self.prediction_dates,
                factor_pipeline=self.pipeline,
                ranker_config=self.ranker_config,
                config=self.comparison_config,
                regime_snapshots_by_date=snapshots,
                regime_decision_cutoffs_by_date=cutoffs,
            )

    def test_prediction_date_labels_cannot_change_same_date_predictions(self) -> None:
        baseline = self._run(self.samples)
        attacked_samples = [
            replace(item, label_value=-1_000_000.0 - index)
            if item.feature_date == self.prediction_dates[0]
            else item
            for index, item in enumerate(self.samples)
        ]
        attacked = self._run(attacked_samples)

        for model_key in baseline.predictions:
            baseline_rows = {
                item.sample_id: item.raw_score
                for item in baseline.predictions[model_key]
                if item.feature_date == self.prediction_dates[0]
            }
            attacked_rows = {
                item.sample_id: item.raw_score
                for item in attacked.predictions[model_key]
                if item.feature_date == self.prediction_dates[0]
            }
            self.assertEqual(baseline_rows, attacked_rows)

    def test_horizon_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "horizons must match"):
            run_walk_forward_model_comparison(
                self.samples,
                trading_dates=self.trading_dates,
                prediction_dates=self.prediction_dates,
                factor_pipeline=self.pipeline,
                ranker_config=replace(self.ranker_config, horizon_days=5),
                config=self.comparison_config,
            )

    def test_strict_panel_rejects_partial_requested_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "every requested OOS date"):
            run_walk_forward_model_comparison(
                self.samples,
                trading_dates=self.trading_dates,
                prediction_dates=self.prediction_dates,
                factor_pipeline=self.pipeline,
                ranker_config=self.ranker_config,
                config=replace(
                    self.comparison_config,
                    minimum_training_dates=8,
                    require_all_prediction_dates=True,
                ),
            )

    def test_lightweight_screen_can_skip_lambdarank(self) -> None:
        result = run_walk_forward_model_comparison(
            self.samples,
            trading_dates=self.trading_dates,
            prediction_dates=self.prediction_dates,
            factor_pipeline=self.pipeline,
            ranker_config=self.ranker_config,
            config=replace(
                self.comparison_config,
                model_keys=("equal_weight", "ridge"),
                require_all_prediction_dates=True,
            ),
        )

        self.assertEqual({"equal_weight", "ridge"}, set(result.reports))
        self.assertTrue(all("lambdarank" not in item.model_status for item in result.fold_audits))

    def test_top_tail_classifier_uses_the_same_strict_oos_panel(self) -> None:
        result = run_walk_forward_model_comparison(
            self.samples,
            trading_dates=self.trading_dates,
            prediction_dates=self.prediction_dates,
            factor_pipeline=self.pipeline,
            ranker_config=self.ranker_config,
            config=replace(
                self.comparison_config,
                model_keys=("equal_weight", "top_tail"),
                top_tail_n=2,
                top_tail_estimators=20,
                require_all_prediction_dates=True,
            ),
        )

        self.assertEqual({"equal_weight", "top_tail"}, set(result.reports))
        self.assertEqual(tuple(self.prediction_dates), result.common_evaluated_dates)
        self.assertTrue(
            all(item.model_status["top_tail"] == "success" for item in result.fold_audits)
        )

    def test_two_stage_selector_records_internal_validation_choice(self) -> None:
        samples = [
            replace(
                item,
                features={
                    **item.features,
                    "intraday_range_5d": float(int(item.ticker[1:]) + 1),
                },
            )
            for item in self.samples
        ]
        pipeline = CrossSectionalFactorPipeline(
            [
                FactorSpec("momentum", FactorDirection.HIGHER_BETTER),
                FactorSpec("intraday_range_5d", FactorDirection.LOWER_BETTER),
            ]
        )
        ranker_config = replace(
            self.ranker_config,
            feature_names=("momentum", "intraday_range_5d"),
        )
        snapshots, cutoffs = self._regime_evidence(crash_date=self.prediction_dates[-1])
        result = run_walk_forward_model_comparison(
            samples,
            trading_dates=self.trading_dates,
            prediction_dates=self.prediction_dates,
            factor_pipeline=pipeline,
            ranker_config=ranker_config,
            config=replace(
                self.comparison_config,
                model_keys=("equal_weight", "two_stage"),
                top_tail_n=2,
                two_stage_validation_dates=10,
                two_stage_minimum_validation_dates=4,
                require_all_prediction_dates=True,
                regime_policy_mode="historical_required",
            ),
            regime_snapshots_by_date=snapshots,
            regime_decision_cutoffs_by_date=cutoffs,
        )

        self.assertEqual({"equal_weight", "two_stage"}, set(result.reports))
        self.assertTrue(
            all(item.model_status["two_stage"] == "success" for item in result.fold_audits)
        )
        self.assertEqual([], list(result.regime_candidate_sample_ids["two_stage"][self.prediction_dates[-1]]))
        self.assertEqual(5, len(result.regime_candidate_sample_ids["two_stage"][self.prediction_dates[0]]))
        self.assertTrue(
            all(
                item.model_metadata["two_stage"]["selected_candidate_fraction"]
                in {0.1, 0.2, 0.4, 1.0}
                for item in result.fold_audits
            )
        )

    def test_comparison_evidence_is_immutable_and_reusable(self) -> None:
        result = self._run(self.samples)
        with TemporaryDirectory() as temporary_name:
            first = persist_walk_forward_evidence(
                result,
                root=Path(temporary_name),
                market="US",
                source_version="lake-fixture-v1",
                universe_version="universe-fixture-v1",
                dataset_version="dataset-fixture-v1",
            )
            second = persist_walk_forward_evidence(
                result,
                root=Path(temporary_name),
                market="US",
                source_version="lake-fixture-v1",
                universe_version="universe-fixture-v1",
                dataset_version="dataset-fixture-v1",
            )

            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.evidence_version, second.evidence_version)
            self.assertTrue((first.artifact_dir / "predictions.parquet").exists())
            self.assertTrue((first.artifact_dir / "regime_policy.json").exists())
            self.assertTrue((first.artifact_dir / "regime_candidates.json").exists())
            self.assertTrue((first.artifact_dir / "reports.json").exists())
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(3, manifest["training_policy"]["purge_sessions"])
            self.assertEqual(
                "strictly_before_prediction_date",
                manifest["training_policy"]["label_availability_rule"],
            )
