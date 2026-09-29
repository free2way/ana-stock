from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.stock_selection.robustness import (
    RobustnessRow,
    analyze_top_n_robustness,
    persist_robustness_report,
)


class StockSelectionRobustnessTests(TestCase):
    def _rows(self) -> list[RobustnessRow]:
        rows: list[RobustnessRow] = []
        start = date(2026, 1, 2)
        for day in range(10):
            feature_date = start + timedelta(days=day)
            market_return = 0.01 if day % 2 == 0 else -0.01
            for ticker_index in range(8):
                gross = 0.02 - ticker_index * 0.002 + day * 0.0001
                net = gross - 0.002
                risk = net - ticker_index * 0.001
                rows.append(
                    RobustnessRow(
                        sample_id=f"{day}:{ticker_index}",
                        ticker=f"S{ticker_index}",
                        feature_date=feature_date,
                        horizon_days=5,
                        raw_score=float(8 - ticker_index),
                        label_value=risk,
                        label_components={
                            "gross_return": gross,
                            "net_return": net,
                            "market_excess_return": net - market_return,
                        },
                        features={
                            "price_vs_ma20": 0.01 if day % 2 == 0 else -0.01,
                        },
                    )
                )
        return rows

    def test_analyzes_exclusions_slices_and_staggered_equity(self) -> None:
        report = analyze_top_n_robustness(
            self._rows(),
            model_key="equal_weight",
            factor_set_key="fixture",
            dataset_version="dataset-v1",
            top_n=3,
            round_trip_cost_bps=20.0,
            extreme_ticker_count=2,
            extreme_date_count=2,
            gate_minimum_cross_section_size=5,
        )

        self.assertEqual(10, report.base_metrics.evaluated_date_count)
        self.assertEqual(2, len(report.excluded_tickers))
        self.assertEqual(8, report.date_exclusion_metrics.evaluated_date_count)
        self.assertEqual({"market_down", "market_up"}, {item.key for item in report.market_state_metrics})
        self.assertEqual(5, report.portfolio_metrics.sleeve_count)
        self.assertEqual(10, len(report.equity_curve))
        self.assertGreater(report.portfolio_metrics.total_return, 0.0)
        self.assertEqual(5, report.point_in_time_gate.gate_on_date_count)
        self.assertEqual(5, report.point_in_time_gate.gate_off_date_count)
        self.assertGreater(report.point_in_time_gate.portfolio_metrics.total_return, 0.0)
        self.assertGreater(report.point_in_time_gate.average_scaled_exposure, 0.0)
        self.assertGreater(report.point_in_time_gate.scaled_portfolio_metrics.total_return, 0.0)

    def test_cost_mismatch_fails_closed_and_evidence_is_immutable(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            analyze_top_n_robustness(
                self._rows(),
                model_key="equal_weight",
                factor_set_key="fixture",
                dataset_version="dataset-v1",
                top_n=3,
                round_trip_cost_bps=40.0,
                gate_minimum_cross_section_size=5,
            )

        report = analyze_top_n_robustness(
            self._rows(),
            model_key="equal_weight",
            factor_set_key="fixture",
            dataset_version="dataset-v1",
            top_n=3,
            round_trip_cost_bps=20.0,
            gate_minimum_cross_section_size=5,
        )
        with TemporaryDirectory() as temporary_name:
            first = persist_robustness_report(report, root=Path(temporary_name))
            second = persist_robustness_report(report, root=Path(temporary_name))
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.evidence_version, second.evidence_version)

    def test_gate_decision_does_not_use_future_labels(self) -> None:
        rows = self._rows()
        breadth_history = {
            date(2025, 10, 1) + timedelta(days=index): 0.5
            for index in range(70)
        }
        baseline = analyze_top_n_robustness(
            rows,
            model_key="equal_weight",
            factor_set_key="fixture",
            dataset_version="dataset-v1",
            top_n=3,
            round_trip_cost_bps=20.0,
            gate_minimum_cross_section_size=5,
            gate_breadth_history=breadth_history,
        )
        attacked = analyze_top_n_robustness(
            [replace(item, label_value=-item.label_value) for item in rows],
            model_key="equal_weight",
            factor_set_key="fixture",
            dataset_version="dataset-v1",
            top_n=3,
            round_trip_cost_bps=20.0,
            gate_minimum_cross_section_size=5,
            gate_breadth_history=breadth_history,
        )

        self.assertEqual(
            baseline.point_in_time_gate.gate_on_date_count,
            attacked.point_in_time_gate.gate_on_date_count,
        )
        self.assertEqual(
            baseline.point_in_time_gate.mean_breadth,
            attacked.point_in_time_gate.mean_breadth,
        )
        self.assertEqual("trailing_median", baseline.point_in_time_gate.threshold_mode)
