"""Acceptance tests for the shared cost basis, clustered hit-rate CI and
lower-bound gate consumption added on 2026-10-05.

These are pure unit tests (no database): they pin the *direction* of the cost
deduction, the clustered-vs-iid interval relationship and that the promotion /
guidance consumers reject on the clustered lower bound.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.cost_basis import canonical_round_trip_cost_bps
from app.services.factor_experiments import compute_forward_outcome, summarize_factor_outcomes
from app.services.model_evaluation import summarize_evaluation_samples
from app.services.selection_quality import (
    SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT,
    _aggregate,
    _guidance_from_summary,
)
from app.services.statistical_inference import day_clustered_hit_rate_ci
from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.selective_policy import (
    SelectiveCandidate,
    SelectiveEvaluationConfig,
    evaluate_selective_decisions,
    select_candidates,
)


class CanonicalCostDefaultsTests(TestCase):
    def test_default_canonical_round_trip_cost_is_50bps(self) -> None:
        self.assertEqual(50.0, canonical_round_trip_cost_bps("CN"))
        self.assertEqual(50.0, canonical_round_trip_cost_bps("US"))


class CostDeductionDirectionTests(TestCase):
    def test_factor_close_to_close_hit_is_net_and_gross_is_preserved(self) -> None:
        history = [
            {"date": "2026-08-03", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 1000},
            # +0.3% gross: a winner at zero cost, a loser after the 50bps round trip.
            {"date": "2026-08-04", "open": 10.0, "high": 10.2, "low": 9.95, "close": 10.03, "volume": 1200},
        ]
        row = {"ticker": "000001.SZ", "market": "CN", "factor_signal_trade_date": "2026-08-03"}
        outcome = compute_forward_outcome(row, history=history)
        self.assertEqual(0.3, outcome["return_1d_pct"])
        self.assertEqual(-0.2, outcome["net_return_1d_pct"])
        self.assertEqual(50.0, outcome["cost_bps"])
        self.assertIn("cost_source", outcome)

        metrics = summarize_factor_outcomes([{"forward_outcome": outcome}])
        self.assertEqual(0.3, metrics["avg_return_1d_pct"])
        self.assertEqual(-0.2, metrics["avg_net_return_1d_pct"])
        # Same sample that was a "hit" gross is a miss net of the round trip.
        self.assertEqual(0.0, metrics["hit_rate_1d_pct"])

    def test_selection_quality_aggregate_uses_net_hit_and_exposes_cost(self) -> None:
        records = [
            {
                "signal_date": f"2026-08-0{index + 1}",
                "return_1d_pct": 0.2,
                "net_return_1d_pct": -0.3,
                "hit_1d": False,
                "execution_hit": False,
                "market": "CN",
            }
            for index in range(8)
        ]
        metrics = _aggregate(records)
        self.assertEqual(0.0, metrics["hit_rate_1d_pct"])
        self.assertEqual(-0.3, metrics["avg_net_return_1d_pct"])
        self.assertEqual(50.0, metrics["cost_bps"])
        self.assertEqual("round_trip", metrics["cost_basis"])


class DayClusterCiTests(TestCase):
    @staticmethod
    def _clustered_flags() -> tuple[list[bool], list[str]]:
        flags: list[bool] = []
        dates: list[str] = []
        for index, day_sign in enumerate([True, False, True, False, True, False]):
            flags.extend([day_sign] * 8)
            dates.extend([f"2026-08-0{index + 1}"] * 8)
        return flags, dates

    def test_clustered_interval_is_wider_than_iid_under_day_clustering(self) -> None:
        flags, dates = self._clustered_flags()
        clustered = day_clustered_hit_rate_ci(flags, dates, block_length=1, iterations=600, seed=7)
        self.assertIsNotNone(clustered["ci95"])
        self.assertEqual(6, clustered["days"])
        self.assertIn("day_cluster", clustered["method"])
        low, high = clustered["ci95"]
        clustered_width = high - low
        # 48 picks but only 6 independent dates: the iid width overcounts n.
        iid_width = 2 * 1.96 * ((0.5 * 0.5 / len(flags)) ** 0.5) * 100.0
        self.assertGreater(clustered_width, iid_width)

    def test_clustered_interval_is_deterministic(self) -> None:
        flags, dates = self._clustered_flags()
        first = day_clustered_hit_rate_ci(flags, dates, block_length=1, iterations=400, seed=11)
        second = day_clustered_hit_rate_ci(flags, dates, block_length=1, iterations=400, seed=11)
        self.assertEqual(first, second)

    def test_model_evaluation_reports_iid_and_clustered_side_by_side(self) -> None:
        samples = []
        for index, day_sign in enumerate([1.0, -1.0, 1.0, -1.0, 1.0, -1.0]):
            for pick in range(8):
                samples.append(
                    {
                        "gross_return_pct": day_sign,
                        "drawdown_pct": -0.5,
                        "trade_date": f"2026-08-0{index + 1}",
                        "ticker": f"S{index}{pick}",
                    }
                )
        result = summarize_evaluation_samples(samples, horizon_days=1, round_trip_cost_bps=50.0)
        iid = result["hit_rate_ci95_iid"]
        clustered = result["hit_rate_ci95_clustered"]
        self.assertIsNotNone(iid)
        self.assertIsNotNone(clustered)
        self.assertTrue(result["hit_rate_ci_iid_is_diagnostic_only"])
        self.assertEqual("iid_normal_diagnostic_not_promotion_evidence", result["confidence_method"])
        self.assertGreater(clustered[1] - clustered[0], iid[1] - iid[0])
        self.assertEqual(clustered[0], result["hit_rate_ci_lower_bound_clustered"])


class GuidanceLowerBoundTests(TestCase):
    def _metrics(self, *, hit: bool, days: int = 8) -> dict:
        records = [
            {
                "signal_date": f"2026-08-{index + 1:02d}",
                "return_1d_pct": 1.0 if hit else 0.2,
                "net_return_1d_pct": 0.5 if hit else -0.3,
                "hit_1d": hit,
                "execution_hit": hit,
                "market": "CN",
            }
            for index in range(days)
        ]
        return _aggregate(records)

    def test_source_below_clustered_lower_bound_cannot_be_preferred(self) -> None:
        weak = {"source_name": "WEAK", "metrics": self._metrics(hit=False)}
        guidance = _guidance_from_summary([weak])
        self.assertEqual("collect_more", guidance["stance"])
        self.assertEqual([], guidance["preferred_sources"])
        self.assertEqual(
            "clustered_hit_rate_ci_lower_bound_not_above_chance",
            guidance["rejected_sources"][0]["reason"],
        )

    def test_source_clearing_clustered_lower_bound_is_preferred(self) -> None:
        strong = {"source_name": "STRONG", "metrics": self._metrics(hit=True)}
        weak = {"source_name": "WEAK", "metrics": self._metrics(hit=False)}
        guidance = _guidance_from_summary([weak, strong])
        self.assertEqual("prefer_leader", guidance["stance"])
        self.assertEqual(["STRONG"], guidance["preferred_sources"])
        self.assertGreaterEqual(
            guidance["leader_hit_rate_ci_lower_bound_clustered"],
            SELECTION_HIT_RATE_CI_LOWER_THRESHOLD_PCT,
        )


def _candidate(feature_date: date, ticker: str) -> SelectiveCandidate:
    return SelectiveCandidate(
        sample_id=f"{feature_date.isoformat()}:{ticker}:5",
        ticker=ticker,
        feature_date=feature_date,
        horizon_days=5,
        raw_score=0.01,
        cross_sectional_rank=0.9,
        positive_probability=0.7,
        expected_risk_adjusted_return=0.01,
        uncertainty=0.1,
        model_version="shadow-v1",
        tradable=True,
    )


def _label(candidate, value: float) -> FactorScore:
    return FactorScore(
        sample_id=candidate.sample_id,
        ticker=candidate.ticker,
        feature_date=candidate.feature_date,
        label_available_date=candidate.feature_date + timedelta(days=5),
        horizon_days=candidate.horizon_days,
        factor_values={},
        missing_factors=(),
        composite_score=candidate.raw_score,
        cross_sectional_rank=candidate.cross_sectional_rank,
        label_value=value,
        label_components={"risk_adjusted_return": value},
    )


class SelectivePolicyClusteredCiTests(TestCase):
    def test_active_mean_ci_is_day_clustered_and_keeps_iid_diagnostic(self) -> None:
        start = date(2026, 8, 3)
        # Clustered positive-then-negative runs make the two intervals differ.
        values = [0.02, 0.02, 0.02, 0.005, -0.003, -0.006]
        candidates = []
        labels = []
        for index, value in enumerate(values):
            candidate = _candidate(start + timedelta(days=index), f"S{index}")
            candidates.append(candidate)
            labels.append(_label(candidate, value))
        report = evaluate_selective_decisions(
            select_candidates(candidates),
            labels,
            config=SelectiveEvaluationConfig(
                market="CN", model_key="selective-shadow", round_trip_cost_bps=20.0
            ),
        )
        self.assertIsNotNone(report.active_mean_ci95_iid)
        self.assertIn("day_order_moving_block_bootstrap", report.active_mean_ci_cluster_method)
        if report.active_mean_ci95_iid != report.active_mean_ci95:
            self.assertNotEqual(report.active_mean_ci95_iid, report.active_mean_ci95)
