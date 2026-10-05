"""Producer-side acceptance for the reliability weighted confluence layer.

``multi_model_reliability_and_calibration`` (consumer) proved the fusion layer
*reads* ``model_oos_*`` row metadata and ``params["probability_calibration"]``.
These tests prove the missing producers actually write them from matured
out-of-sample evaluations:

1. :func:`build_rolling_oos_reliability` emits per-model rolling OOS hit
   rate / IC strictly earlier than the annotated prediction date (PIT) and
   omits under-sampled models so the fusion layer keeps its equal-weight
   fallback.
2. :func:`build_probability_calibration_artifact` turns the same matured rows
   into ``SelectiveCalibrationBin``-compatible bins that
   ``resolve_probability_calibrator`` accepts as ``probability_calibration``.
3. The end-to-end chain (row metadata -> fusion weighting -> calibrated
   probability) no longer defaults to equal weights or a null probability.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.services.model_signal_summary import resolve_probability_calibrator
from app.services.stock_selection.factor_baseline import BaselinePrediction
from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.multi_model_confluence import (
    aggregate_multi_model_rows,
)
from app.services.stock_selection.selective_calibration import (
    OOS_METRIC_IC,
    PROBABILITY_CALIBRATION_ARTIFACT_SCHEMA_VERSION,
    SelectiveCalibrationBin,
    attach_oos_reliability_metadata,
    build_probability_calibration_artifact,
    build_rolling_oos_reliability,
    calibration_artifact_from_payload,
    load_calibration_artifact,
    load_oos_reliability_metadata,
    write_calibration_artifact,
    write_oos_reliability_metadata,
)

HIGH = "lightgbm_top_picks"
MID = "technical_momentum"
LOW = "next_tesla_swing"

SCORES = (0.1, 0.3, 0.5, 0.7, 0.9)
HORIZON = 5
MATURITY_DAYS = 5
FEATURE_START = date(2026, 3, 2)
TRADING_DATES = 8

HIGH_VERSION = "model-high-v1"
LOW_VERSION = "model-low-v1"
MODEL_KEYS = {HIGH_VERSION: HIGH, LOW_VERSION: LOW}


@dataclass(frozen=True, slots=True)
class _History:
    predictions: list[BaselinePrediction]
    labels: list[FactorScore]


def _history(*, model_version: str, positive_scores: tuple[float, ...]) -> _History:
    predictions: list[BaselinePrediction] = []
    labels: list[FactorScore] = []
    for day_index in range(TRADING_DATES):
        feature_date = FEATURE_START + timedelta(days=day_index)
        for ticker_index, score in enumerate(SCORES):
            sample_id = f"{feature_date.isoformat()}:{model_version}:T{ticker_index}"
            ticker = f"T{ticker_index}"
            predictions.append(
                BaselinePrediction(
                    sample_id=sample_id,
                    ticker=ticker,
                    feature_date=feature_date,
                    horizon_days=HORIZON,
                    raw_score=score,
                    cross_sectional_rank=score,
                    model_version=model_version,
                )
            )
            value = 0.02 if score in positive_scores else -0.01
            labels.append(
                FactorScore(
                    sample_id=sample_id,
                    ticker=ticker,
                    feature_date=feature_date,
                    label_available_date=feature_date + timedelta(days=MATURITY_DAYS),
                    horizon_days=HORIZON,
                    factor_values={},
                    missing_factors=(),
                    composite_score=score,
                    cross_sectional_rank=score,
                    label_value=value,
                )
            )
    return _History(predictions=predictions, labels=labels)


def _combined() -> _History:
    """Two models over identical dates: one score-aligned, one score-inverted."""

    high = _history(model_version=HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9))
    low = _history(model_version=LOW_VERSION, positive_scores=(0.1,))
    return _History(
        predictions=[*high.predictions, *low.predictions],
        labels=[*high.labels, *low.labels],
    )


def _as_of() -> date:
    return FEATURE_START + timedelta(days=TRADING_DATES - 1 + 30)


def _row(ticker: str, *, model_score: float, action: str = "pullback") -> dict:
    return {
        "ticker": ticker,
        "market": "CN",
        "snapshot_score": model_score * 100.0,
        "trend_score": model_score * 100.0,
        "model_score": model_score,
        "action_label": action,
        "model_execution_tags": [],
        "tradability_status": "READY",
        "trade_readiness_score": 80.0,
    }


def _template_rows() -> dict[str, list[dict]]:
    return {
        HIGH: [_row("ALPHA", model_score=0.1), _row("OMEGA", model_score=0.9)],
        LOW: [_row("ALPHA", model_score=0.1), _row("OMEGA", model_score=0.9)],
    }


def _params(**overrides) -> dict:
    params = {
        "multi_model_templates": [HIGH, LOW],
        "min_multi_model_hits": 2,
        "confluence_action_filter": "ALL",
        "strategy_profile": "",
        "market": "CN",
        "universe": "full_market",
        "sort_by": "confluence_rank",
        "sort_order": "desc",
        "limit": 500,
        "lang": "zh",
    }
    params.update(overrides)
    return params


class RollingReliabilityProducerTests(unittest.TestCase):
    def test_hit_rate_metadata_is_per_model_and_point_in_time(self) -> None:
        history = _combined()

        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            lookback_dates=60,
            minimum_observations=30,
        )

        self.assertEqual({HIGH, LOW}, set(metadata))
        high = metadata[HIGH]
        low = metadata[LOW]
        # 3 of 5 score buckets are positive for the aligned model, 1 of 5 for
        # the inverted one; both windows are fully matured before as_of_date.
        self.assertAlmostEqual(0.6, high.oos_hit_rate)
        self.assertAlmostEqual(0.2, low.oos_hit_rate)
        self.assertEqual(TRADING_DATES * len(SCORES), high.sample_count)
        self.assertLess(high.window_end_date, high.as_of_date)
        self.assertLess(low.window_end_date, _as_of())
        self.assertEqual(FEATURE_START, high.window_start_date)
        self.assertEqual("net_of_cost_positive_return", high.definition)

    def test_row_metadata_aligns_with_fusion_field_names(self) -> None:
        history = _combined()
        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            minimum_observations=30,
        )

        fields = metadata[HIGH].row_metadata()

        self.assertAlmostEqual(0.6, fields["model_oos_hit_rate"])
        self.assertAlmostEqual(0.6, fields["model_reliability"])
        self.assertNotIn("model_oos_ic", fields)
        self.assertEqual(HIGH, metadata[HIGH].model_key)
        self.assertEqual(set(MODEL_KEYS.values()), set(metadata))

    def test_ic_metric_emits_ic_field_and_signed_direction(self) -> None:
        history = _combined()
        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            minimum_observations=30,
            metric=OOS_METRIC_IC,
        )

        high_fields = metadata[HIGH].row_metadata()
        low_fields = metadata[LOW].row_metadata()
        self.assertNotIn("model_oos_hit_rate", high_fields)
        self.assertGreater(high_fields["model_oos_ic"], 0.0)
        self.assertLess(low_fields["model_oos_ic"], 0.0)
        self.assertAlmostEqual(abs(low_fields["model_oos_ic"]), low_fields["model_reliability"])

    def test_labels_maturing_on_or_after_prediction_date_are_rejected(self) -> None:
        history = _history(model_version=HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9))

        # as_of_date equals the first feature date: nothing has matured yet and
        # every prediction is on/after it, so no window may be written.
        at_first_date = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=FEATURE_START,
            model_key_by_version=MODEL_KEYS,
            minimum_observations=1,
        )
        self.assertEqual({}, at_first_date)

        # A single leaked (future) label must not be usable by moving as_of_date
        # one day before its availability: it stays out of the window.
        leaked_date = FEATURE_START + timedelta(days=MATURITY_DAYS - 1)
        partial = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=leaked_date,
            model_key_by_version=MODEL_KEYS,
            minimum_observations=1,
        )
        self.assertEqual({}, partial)

    def test_window_is_capped_by_lookback_dates(self) -> None:
        history = _history(model_version=HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9))
        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            lookback_dates=2,
            minimum_observations=1,
        )

        window = metadata[HIGH]
        self.assertEqual(2 * len(SCORES), window.sample_count)
        expected_start = FEATURE_START + timedelta(days=TRADING_DATES - 2)
        self.assertEqual(expected_start, window.window_start_date)

    def test_insufficient_observations_produces_no_metadata(self) -> None:
        history = _history(model_version=HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9))

        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            minimum_observations=10_000,
        )

        self.assertEqual({}, metadata)

    def test_reliability_artifact_round_trips(self) -> None:
        history = _combined()
        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            minimum_observations=30,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reliability.json"
            write_oos_reliability_metadata(metadata, path)
            loaded = load_oos_reliability_metadata(path)

        self.assertEqual(set(metadata), set(loaded))
        self.assertEqual(metadata[HIGH].version, loaded[HIGH].version)
        self.assertAlmostEqual(metadata[HIGH].oos_hit_rate, loaded[HIGH].oos_hit_rate)


class CalibrationArtifactProducerTests(unittest.TestCase):
    def _artifact(self, **overrides):
        history = _history(model_version=HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9))
        params = {
            "as_of_date": _as_of(),
            "score_field": "model_score",
            "fit_score_field": "raw_score",
            "bin_count": 5,
            "minimum_observations": 20,
            "prior_strength": 0.0,
            "round_trip_cost_bps": 20.0,
        }
        params.update(overrides)
        return build_probability_calibration_artifact(history.predictions, history.labels, **params)

    def test_bins_are_selective_calibration_compatible(self) -> None:
        artifact = self._artifact()

        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual("bins", artifact.method)
        self.assertEqual("model_score", artifact.score_field)
        self.assertEqual("net_of_cost_positive_return", artifact.definition)
        self.assertTrue(artifact.bins)
        field_names = {field for field in SelectiveCalibrationBin.__dataclass_fields__}
        for bin_row in artifact.bins:
            self.assertEqual(field_names, set(bin_row))
        for earlier, later in zip(artifact.bins, artifact.bins[1:], strict=False):
            self.assertLessEqual(
                earlier["calibrated_positive_probability"],
                later["calibrated_positive_probability"],
            )

    def test_calibration_params_feed_resolve_probability_calibrator(self) -> None:
        artifact = self._artifact()
        assert artifact is not None

        calibrator, status = resolve_probability_calibrator(
            {"probability_calibration": artifact.calibration_params()}
        )

        self.assertEqual("calibrated", status)
        assert calibrator is not None
        self.assertEqual("bins", calibrator.method)
        self.assertEqual("model_score", calibrator.score_field)
        self.assertAlmostEqual(artifact.version, calibrator.source)
        self.assertLess(calibrator.predict(0.1), calibrator.predict(0.9))

    def test_insufficient_samples_returns_none(self) -> None:
        artifact = self._artifact(minimum_observations=10_000, bin_count=5)
        self.assertIsNone(artifact)

    def test_isotonic_variant_emits_samples(self) -> None:
        artifact = self._artifact(method="isotonic")

        assert artifact is not None
        self.assertEqual("isotonic", artifact.method)
        self.assertTrue(artifact.samples)
        calibrator, status = resolve_probability_calibrator(
            {"probability_calibration": artifact.calibration_params()}
        )
        self.assertEqual("calibrated", status)
        assert calibrator is not None

    def test_labels_maturing_in_the_future_are_excluded(self) -> None:
        history = _history(model_version=HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9))
        early = build_probability_calibration_artifact(
            history.predictions,
            history.labels,
            as_of_date=FEATURE_START,
            bin_count=2,
            minimum_observations=2,
        )
        self.assertIsNone(early)

    def test_artifact_round_trips_through_disk(self) -> None:
        artifact = self._artifact()
        assert artifact is not None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            write_calibration_artifact(artifact, path)
            loaded = load_calibration_artifact(path)

        self.assertEqual(artifact.version, loaded.version)
        self.assertEqual(artifact.bins, loaded.bins)
        self.assertEqual(artifact.calibration_params(), loaded.calibration_params())

    def test_loader_rejects_foreign_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema"):
            calibration_artifact_from_payload(
                {"schema_version": "something_else", "method": "bins", "bins": []}
            )
        self.assertEqual(
            PROBABILITY_CALIBRATION_ARTIFACT_SCHEMA_VERSION,
            "probability_calibration_artifact_v1",
        )


class EndToEndProducerToFusionTests(unittest.TestCase):
    def _produced(self):
        history = _combined()
        reliability = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            lookback_dates=60,
            minimum_observations=30,
        )
        calibration = build_probability_calibration_artifact(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key=HIGH,
            model_key_by_version=MODEL_KEYS,
            bin_count=5,
            minimum_observations=20,
            prior_strength=0.0,
            round_trip_cost_bps=20.0,
        )
        assert calibration is not None
        return reliability, calibration

    def test_row_metadata_drives_weights_and_calibrated_probability(self) -> None:
        reliability, calibration = self._produced()

        enriched = attach_oos_reliability_metadata(_template_rows(), reliability)
        out, meta = aggregate_multi_model_rows(
            enriched,
            template_keys=[HIGH, LOW],
            params=_params(probability_calibration=calibration.calibration_params()),
        )

        self.assertEqual("row_metadata:oos_hit_rate", meta["weight_source"])
        self.assertEqual("calibrated", meta["calibration_status"])
        self.assertEqual(calibration.version, meta["calibration_source"])
        by_ticker = {row["ticker"]: row for row in out}
        self.assertEqual(2, len(by_ticker))
        self.assertGreater(
            by_ticker["OMEGA"]["model_weight_lightgbm_top_picks"],
            by_ticker["OMEGA"]["model_weight_next_tesla_swing"],
        )
        omega_probability = by_ticker["OMEGA"]["expected_hit_probability"]
        alpha_probability = by_ticker["ALPHA"]["expected_hit_probability"]
        self.assertIsNotNone(omega_probability)
        self.assertIsNotNone(alpha_probability)
        self.assertGreater(omega_probability, alpha_probability)
        self.assertEqual("calibrated", by_ticker["OMEGA"]["calibration_status"])

    def test_insufficient_samples_still_fall_back_to_equal_weights(self) -> None:
        history = _combined()
        reliability = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            model_key_by_version=MODEL_KEYS,
            minimum_observations=10_000,
        )
        calibration = build_probability_calibration_artifact(
            history.predictions,
            history.labels,
            as_of_date=_as_of(),
            minimum_observations=10_000,
        )
        self.assertEqual({}, reliability)
        self.assertIsNone(calibration)

        enriched = attach_oos_reliability_metadata(_template_rows(), reliability)
        out, meta = aggregate_multi_model_rows(
            enriched,
            template_keys=[HIGH, LOW],
            params=_params(),
        )

        self.assertEqual("equal_weight_fallback", meta["weight_source"])
        self.assertTrue(meta["calibration_status"].startswith("unavailable"))
        for row in out:
            self.assertIsNone(row["expected_hit_probability"])
            self.assertEqual("equal_weight_fallback", row["weight_source"])

    def test_label_maturing_on_prediction_date_is_excluded(self) -> None:
        history = _combined()
        # Move as_of_date to the maturity boundary of the last feature date: its
        # labels mature exactly on as_of_date and must be excluded from the
        # window, which is strictly earlier than as_of_date.
        boundary = FEATURE_START + timedelta(days=TRADING_DATES - 1 + MATURITY_DAYS)
        metadata = build_rolling_oos_reliability(
            history.predictions,
            history.labels,
            as_of_date=boundary,
            model_key_by_version=MODEL_KEYS,
            lookback_dates=60,
            minimum_observations=1,
        )

        self.assertEqual({HIGH, LOW}, set(metadata))
        high = metadata[HIGH]
        self.assertLess(high.window_end_date, boundary)
        self.assertEqual(FEATURE_START + timedelta(days=TRADING_DATES - 2), high.window_end_date)
        self.assertEqual((TRADING_DATES - 1) * len(SCORES), high.sample_count)


if __name__ == "__main__":
    unittest.main()
