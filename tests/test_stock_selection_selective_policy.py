from __future__ import annotations

from datetime import date, timedelta
import unittest

from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.selective_policy import (
    SelectiveCandidate,
    SelectiveEvaluationConfig,
    SelectivePolicyConfig,
    evaluate_selective_decisions,
    select_candidates,
)


def _candidate(
    feature_date: date,
    ticker: str,
    *,
    probability: float = 0.70,
    expected: float = 0.01,
    rank: float = 0.90,
    uncertainty: float = 0.10,
    tradable: bool = True,
    risk_tags: tuple[str, ...] = (),
) -> SelectiveCandidate:
    return SelectiveCandidate(
        sample_id=f"{feature_date.isoformat()}:{ticker}:5",
        ticker=ticker,
        feature_date=feature_date,
        horizon_days=5,
        raw_score=expected,
        cross_sectional_rank=rank,
        positive_probability=probability,
        expected_risk_adjusted_return=expected,
        uncertainty=uncertainty,
        model_version="shadow-v1",
        tradable=tradable,
        risk_tags=risk_tags,
    )


def _label(candidate: SelectiveCandidate, value: float) -> FactorScore:
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


class SelectivePolicyTests(unittest.TestCase):
    def test_policy_can_abstain_and_records_ex_ante_reasons(self) -> None:
        feature_date = date(2026, 8, 1)
        decisions = select_candidates(
            [
                _candidate(feature_date, "LOW", probability=0.59),
                _candidate(feature_date, "HALT", tradable=False),
            ]
        )
        self.assertEqual(1, len(decisions))
        self.assertTrue(decisions[0].abstained)
        self.assertEqual((), decisions[0].selected)
        self.assertEqual("not_tradable", decisions[0].abstention_reason)
        self.assertEqual(1, decisions[0].rejection_counts["below_probability_threshold"])
        self.assertEqual(1, decisions[0].rejection_counts["not_tradable"])

    def test_closed_market_gate_forces_cash_without_using_labels(self) -> None:
        feature_date = date(2026, 8, 1)
        decisions = select_candidates(
            [_candidate(feature_date, "AAA")],
            market_gate_by_date={feature_date: False},
        )
        self.assertTrue(decisions[0].abstained)
        self.assertEqual("market_gate_closed", decisions[0].abstention_reason)

    def test_thresholds_are_inclusive_and_capacity_uses_expected_net_value(self) -> None:
        feature_date = date(2026, 8, 1)
        config = SelectivePolicyConfig(max_selected_per_date=1)
        boundary = _candidate(
            feature_date,
            "BOUNDARY",
            probability=config.minimum_positive_probability,
            expected=config.minimum_expected_risk_adjusted_return,
            rank=config.minimum_cross_sectional_rank,
            uncertainty=config.maximum_uncertainty,
        )
        stronger = _candidate(feature_date, "STRONGER", expected=0.02)
        decision = select_candidates([boundary, stronger], config=config)[0]
        self.assertEqual(("STRONGER",), tuple(item.ticker for item in decision.selected))
        audit = {item.ticker: item for item in decision.candidate_decisions}
        self.assertEqual(("outside_daily_capacity",), audit["BOUNDARY"].reason_codes)
        self.assertEqual(1, audit["STRONGER"].selected_rank)

    def test_blocked_risk_tag_fails_closed_case_insensitively(self) -> None:
        feature_date = date(2026, 8, 1)
        decision = select_candidates(
            [_candidate(feature_date, "AAA", risk_tags=("SUSPENDED",))]
        )[0]
        self.assertTrue(decision.abstained)
        self.assertEqual(1, decision.rejection_counts["blocked_risk_tag"])

    def test_evaluation_counts_abstention_dates_as_cash(self) -> None:
        start = date(2026, 8, 1)
        candidates: list[SelectiveCandidate] = []
        labels: list[FactorScore] = []
        for index in range(8):
            candidate = _candidate(
                start + timedelta(days=index),
                f"S{index}",
                probability=0.70 if index < 6 else 0.40,
            )
            candidates.append(candidate)
            if index < 6:
                labels.append(_label(candidate, 0.01))
        decisions = select_candidates(candidates)
        report = evaluate_selective_decisions(
            decisions,
            labels,
            config=SelectiveEvaluationConfig(
                market="CN",
                model_key="selective-shadow",
                round_trip_cost_bps=20.0,
            ),
        )
        self.assertEqual(8, report.oos_date_count)
        self.assertEqual(6, report.active_date_count)
        self.assertEqual(2, report.abstention_date_count)
        self.assertEqual(0.75, report.coverage_rate)
        self.assertAlmostEqual(0.01, report.mean_active_risk_adjusted_return)
        self.assertAlmostEqual(0.0075, report.mean_calendar_risk_adjusted_return)
        self.assertAlmostEqual(0.01, report.extreme_five_dates_excluded_mean)
        self.assertAlmostEqual(0.01, report.extreme_five_tickers_excluded_mean)

    def test_selected_label_must_exist_and_match_identity(self) -> None:
        candidate = _candidate(date(2026, 8, 1), "AAA")
        decisions = select_candidates([candidate])
        config = SelectiveEvaluationConfig(
            market="CN",
            model_key="selective-shadow",
            round_trip_cost_bps=20.0,
        )
        with self.assertRaisesRegex(ValueError, "label is missing"):
            evaluate_selective_decisions(decisions, [], config=config)


if __name__ == "__main__":
    unittest.main()
