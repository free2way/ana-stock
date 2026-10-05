"""Offline tests for the frozen ``sentiment_v1`` forward-batch protocol.

Everything here runs on fixtures: no database, no network, no market lake.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorObservation,
    MissingFactorPolicy,
)
from app.services.stock_selection.factor_sets import (
    factor_pipeline_for_factor_set,
    get_research_factor_set,
    missing_factor_policy_for_factor_set,
    sentiment_factor_set,
)
from app.services.stock_selection.sentiment_features import SENTIMENT_FACTOR_SET_KEY
from app.services.stock_selection.sentiment_forward_batch import (
    OPERATOR_ENV_VAR,
    SentimentForwardBatchConflict,
    assess_sentiment_forward_maturity,
    build_sentiment_forward_batch_spec,
    build_sentiment_forward_panel,
    build_sentiment_forward_scores,
    default_start_date,
    freeze_sentiment_forward_batch,
    load_sentiment_forward_batch,
    resolve_operator,
    run_sentiment_forward_experiment,
)

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
TRADING_DAYS = (
    "2026-08-25",
    "2026-08-26",
    "2026-08-27",
    "2026-08-28",
    "2026-08-31",
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
    "2026-09-04",
    "2026-09-07",
    "2026-09-08",
)


def _batch(**overrides):
    kwargs = {
        "universe_version": "pit_universe_v1:CN:fixture",
        "start_date": "2026-08-24",
        "operator": "tester",
        "created_at": "2026-08-24T18:00:00+08:00",
        "top_n": 2,
        "horizon_days": 5,
    }
    kwargs.update(overrides)
    return build_sentiment_forward_batch_spec(**kwargs)


def _features(feature_dates: tuple[date, ...]) -> dict:
    features: dict = {}
    for feature_date in feature_dates:
        for index, ticker in enumerate(TICKERS):
            features[(feature_date, ticker)] = {"limit_up_flag": float(index)}
    return features


def _universe(feature_dates: tuple[date, ...]) -> dict:
    return {feature_date: TICKERS for feature_date in feature_dates}


def _prices() -> list[dict]:
    rows: list[dict] = []
    for index, ticker in enumerate(TICKERS):
        for offset, trade_date in enumerate(TRADING_DAYS):
            base = 10.0 + index + offset * 0.1
            rows.append(
                {
                    "symbol": ticker,
                    "date": trade_date,
                    "open": base,
                    "close": base + 0.05,
                }
            )
    return rows


class SentimentForwardBatchSpecTests(TestCase):
    def test_freeze_is_idempotent_and_conflicts_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"
            spec = _batch()

            first = freeze_sentiment_forward_batch(path, spec)
            second = freeze_sentiment_forward_batch(path, spec)

            self.assertEqual("created", first["status"])
            self.assertEqual("reused_existing", second["status"])
            self.assertEqual(first["dataset_hash"], second["dataset_hash"])
            self.assertEqual(spec.dataset_hash, load_sentiment_forward_batch(path).dataset_hash)

            drifted = _batch(cost_bps=40.0)
            with self.assertRaises(SentimentForwardBatchConflict):
                freeze_sentiment_forward_batch(path, drifted)

    def test_dataset_hash_is_stable_and_covers_cutoff_and_window(self) -> None:
        base = _batch()
        same_contract = _batch(operator="other", created_at="2030-01-01T00:00:00+08:00")
        self.assertEqual(base.dataset_hash, same_contract.dataset_hash)

        for overrides in (
            {"cost_bps": 40.0},
            {"horizon_days": 10},
            {"top_n": 5},
            {"coverage_end": "2026-12-31"},
            {"auction_path_included": True},
            {"universe_version": "pit_universe_v1:CN:other"},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(base.dataset_hash, _batch(**overrides).dataset_hash)

    def test_missing_policy_matches_the_shared_family_factory(self) -> None:
        """The batch scores under the same policy production/experiments use."""

        spec = _batch()
        expected = missing_factor_policy_for_factor_set(
            get_research_factor_set(SENTIMENT_FACTOR_SET_KEY)
        ).value
        self.assertEqual(MissingFactorPolicy.EXCLUDE.value, expected)
        self.assertEqual(expected, spec.missing_policy())
        self.assertEqual(expected, spec.to_payload()["missing_policy"])
        self.assertEqual(
            expected,
            factor_pipeline_for_factor_set(sentiment_factor_set()).missing_policy.value,
        )

    def test_dataset_hash_tracks_the_missing_factor_policy(self) -> None:
        """A policy switch must fork the hash, not reinterpret the frozen batch."""

        base = _batch()
        with patch(
            "app.services.stock_selection.sentiment_forward_batch."
            "missing_factor_policy_for_factor_set",
            return_value=MissingFactorPolicy.NEUTRAL_ZERO,
        ):
            drifted = _batch()
            self.assertEqual(MissingFactorPolicy.NEUTRAL_ZERO.value, drifted.missing_policy())
        self.assertNotEqual(base.dataset_hash, drifted.dataset_hash)

    def test_operator_reads_env_and_defaults_to_unknown(self) -> None:
        self.assertEqual("unknown", resolve_operator({}))
        self.assertEqual("unknown", resolve_operator({OPERATOR_ENV_VAR: "  "}))
        self.assertEqual("alice", resolve_operator({OPERATOR_ENV_VAR: " alice "}))

    def test_start_date_skips_cn_holidays(self) -> None:
        self.assertEqual("2026-09-30", default_start_date(date(2026, 9, 30)))
        self.assertEqual("2026-10-08", default_start_date(date(2026, 10, 1)))


class SentimentForwardMaturityTests(TestCase):
    def test_immature_batch_is_blocked_without_conclusion(self) -> None:
        result = assess_sentiment_forward_maturity(matured_date_count=5, min_matured_dates=60)

        self.assertEqual("BLOCKED", result["promotion_status"])
        self.assertFalse(result["conclusion_allowed"])
        self.assertEqual(55, result["remaining_matured_dates"])
        self.assertIn("minimum_60_matured_dates_not_met", result["promotion_blockers"])

    def test_mature_batch_is_reviewable_and_incomplete_batches_block(self) -> None:
        mature = assess_sentiment_forward_maturity(matured_date_count=60, min_matured_dates=60)
        self.assertEqual("REVIEW_REQUIRED", mature["promotion_status"])
        self.assertTrue(mature["conclusion_allowed"])

        incomplete = assess_sentiment_forward_maturity(
            matured_date_count=60, min_matured_dates=60, incomplete_date_count=1
        )
        self.assertEqual("BLOCKED", incomplete["promotion_status"])
        self.assertIn("incomplete_frozen_batches", incomplete["promotion_blockers"])


class SentimentForwardCoverageTests(TestCase):
    def test_out_of_coverage_samples_never_enter_the_score_panel(self) -> None:
        batch = _batch(coverage_end="2026-09-01")
        covered = (date(2026, 8, 24), date(2026, 9, 1))
        outside = (date(2026, 8, 20), date(2026, 9, 8))
        features = _features(covered + outside)
        universe = _universe(covered + outside)
        # A covered date with a ticker outside the frozen universe must also drop.
        features[(date(2026, 8, 24), "ZZZ")] = {"limit_up_flag": 9.0}

        scored = build_sentiment_forward_scores(
            batch=batch, features_by_key=features, universe_by_date=universe
        )

        scored_dates = {row["feature_date"] for row in scored["score_rows"]}
        self.assertEqual({"2026-08-24", "2026-09-01"}, scored_dates)
        self.assertEqual(["2026-08-20", "2026-09-08"], scored["coverage_excluded_dates"])
        self.assertNotIn("ZZZ", {row["ticker"] for row in scored["score_rows"]})

    def test_scores_use_the_shared_exclude_missing_policy(self) -> None:
        """A missing sentiment cell is omitted, never scored as a neutral zero."""

        batch = _batch()
        feature_date = date(2026, 8, 24)
        features = {
            (feature_date, "AAA"): {"limit_up_flag": 3.0},
            (feature_date, "BBB"): {"limit_up_flag": 0.0, "hot_rank": 3.0},
            (feature_date, "CCC"): {"limit_up_flag": 1.0, "hot_rank": 1.0},
        }
        scored = build_sentiment_forward_scores(
            batch=batch,
            features_by_key=features,
            universe_by_date={feature_date: ("AAA", "BBB", "CCC")},
        )
        forward = {row["ticker"]: row for row in scored["score_rows"]}
        self.assertEqual({"AAA", "BBB", "CCC"}, set(forward))
        self.assertEqual(
            {MissingFactorPolicy.EXCLUDE.value},
            {row["missing_policy"] for row in forward.values()},
        )

        observations = [
            FactorObservation(
                observation_id=f"sentiment-forward:{feature_date.isoformat()}:{ticker}",
                ticker=ticker,
                feature_date=feature_date,
                horizon_days=batch.horizon_days,
                features=values,
            )
            for (_, ticker), values in features.items()
        ]
        excluded = {
            score.ticker: score.composite_score
            for score in factor_pipeline_for_factor_set(sentiment_factor_set()).transform(
                observations
            )
        }
        neutral_zero = {
            score.ticker: score.composite_score
            for score in CrossSectionalFactorPipeline(
                sentiment_factor_set().specs,
                missing_policy=MissingFactorPolicy.NEUTRAL_ZERO,
            ).transform(observations)
        }
        for ticker, row in forward.items():
            with self.subTest(ticker=ticker):
                self.assertAlmostEqual(excluded[ticker], row["composite_score"])
        # AAA only has one of the two factors present; the legacy neutral-zero
        # contract would dilute its composite, so the two are distinguishable.
        self.assertNotAlmostEqual(neutral_zero["AAA"], forward["AAA"]["composite_score"])

    def test_panel_only_includes_matured_covered_dates(self) -> None:
        batch = _batch()
        covered = (date(2026, 8, 24), date(2026, 9, 1))
        features = _features(covered)
        universe = _universe(covered)

        panel = build_sentiment_forward_panel(
            batch=batch,
            features_by_key=features,
            universe_by_date=universe,
            price_rows=_prices(),
            as_of_date="2026-09-30",
        )

        self.assertEqual(["2026-08-24", "2026-09-01"], panel["matured_dates"])
        self.assertEqual(2, panel["evaluated_date_count"])
        self.assertEqual(4, len(panel["treated_rows"]))
        self.assertEqual(8, len(panel["control_rows"]))
        self.assertFalse(panel["maturity"]["conclusion_allowed"])

    def test_experiment_framework_runs_on_the_frozen_panel(self) -> None:
        batch = _batch()
        covered = (date(2026, 8, 24), date(2026, 9, 1))
        panel = build_sentiment_forward_panel(
            batch=batch,
            features_by_key=_features(covered),
            universe_by_date=_universe(covered),
            price_rows=_prices(),
            as_of_date="2026-09-30",
        )

        report = run_sentiment_forward_experiment(
            batch=batch,
            treated_rows=panel["treated_rows"],
            control_rows=panel["control_rows"],
            oos_end="2026-09-01",
        )

        self.assertIn(report["decision"], {"PASS", "REJECT"})
        self.assertIn("ci95", report["overall"])
        self.assertEqual(batch.dataset_hash, report["dataset_hash"])


if __name__ == "__main__":
    import unittest

    unittest.main()
