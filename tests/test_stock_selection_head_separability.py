from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

import polars as pl

from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorDirection,
    FactorObservation,
    FactorSpec,
)
from app.services.stock_selection.head_separability import (
    HeadSeparabilityConfig,
    ScoreComponent,
    SeparabilityRow,
    SeparabilityScoreDefinition,
    _definition_scores,
    analyze_head_separability,
    load_separability_rows,
    persist_head_separability_report,
)


class HeadSeparabilityTests(unittest.TestCase):
    def _rows(self) -> tuple[list[SeparabilityRow], list[date]]:
        dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(40)]
        rows: list[SeparabilityRow] = []
        for day_index, feature_date in enumerate(dates):
            for ticker_index in range(100):
                label = (ticker_index - 50) / 100.0
                unstable = label if day_index < 20 else -label
                rows.append(
                    SeparabilityRow(
                        sample_id=f"{feature_date.isoformat()}:S{ticker_index:03d}",
                        ticker=f"S{ticker_index:03d}",
                        feature_date=feature_date,
                        label_value=label,
                        features={"good": label, "unstable": unstable},
                    )
                )
        return rows, dates

    def test_detects_stable_head_separability_and_rejects_unstable_signal(self) -> None:
        rows, dates = self._rows()
        definitions = (
            SeparabilityScoreDefinition(
                "good",
                (ScoreComponent("good", FactorDirection.HIGHER_BETTER),),
            ),
            SeparabilityScoreDefinition(
                "unstable",
                (ScoreComponent("unstable", FactorDirection.HIGHER_BETTER),),
            ),
        )
        report = analyze_head_separability(
            rows,
            market="US",
            dataset_version="fixture-v1",
            horizon_days=5,
            analysis_dates=dates,
            excluded_tail_date_count=10,
            score_definitions=definitions,
            config=HeadSeparabilityConfig(minimum_cross_section_size=100),
        )
        summaries = {item.score_key: item for item in report.summaries}
        self.assertTrue(summaries["good"].passed)
        self.assertAlmostEqual(1.0, summaries["good"].head_auc_mean)
        self.assertAlmostEqual(1.0, summaries["good"].precision_at_n_mean)
        self.assertFalse(summaries["unstable"].passed)
        self.assertEqual(("good",), report.separable_score_keys)
        self.assertEqual("PASS", report.verdict)

    def test_loader_excludes_outer_oos_tail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "samples.parquet"
            records = []
            start = date(2026, 1, 1)
            for day_index in range(8):
                feature_date = start + timedelta(days=day_index)
                for ticker_index in range(10):
                    records.append(
                        {
                            "sample_id": f"{day_index}:{ticker_index}",
                            "ticker": f"S{ticker_index}",
                            "feature_date": feature_date.isoformat(),
                            "horizon_days": 5,
                            "tradable": True,
                            "label_value": float(ticker_index),
                            "features_json": json.dumps({"good": float(ticker_index)}),
                        }
                    )
            pl.DataFrame(records).write_parquet(path)
            rows, dates = load_separability_rows(
                samples_path=path,
                horizon_days=5,
                factor_names=("good",),
                analysis_date_count=3,
                exclude_tail_date_count=2,
            )
        self.assertEqual((start + timedelta(days=3), start + timedelta(days=4), start + timedelta(days=5)), dates)
        self.assertEqual(set(dates), {item.feature_date for item in rows})

    def test_composite_scores_match_the_production_factor_transform(self) -> None:
        feature_date = date(2026, 1, 2)
        rows = [
            SeparabilityRow(
                sample_id=f"sample-{index}",
                ticker=f"S{index}",
                feature_date=feature_date,
                label_value=float(index),
                features={"first": first, "second": second},
            )
            for index, (first, second) in enumerate(
                ((1.0, 90.0), (2.0, 40.0), (3.0, 30.0), (4.0, 20.0), (100.0, 10.0))
            )
        ]
        specs = (
            FactorSpec("first", FactorDirection.HIGHER_BETTER),
            FactorSpec("second", FactorDirection.LOWER_BETTER),
        )
        definition = SeparabilityScoreDefinition(
            "composite",
            tuple(
                ScoreComponent(
                    item.name,
                    item.direction,
                    item.weight,
                    item.winsor_lower,
                    item.winsor_upper,
                )
                for item in specs
            ),
        )
        _, audited_scores = _definition_scores(rows, definition, zscore_clip=3.0)
        production_scores = CrossSectionalFactorPipeline(specs).transform(
            tuple(
                FactorObservation(
                    observation_id=item.sample_id,
                    ticker=item.ticker,
                    feature_date=item.feature_date,
                    horizon_days=5,
                    features=item.features,
                )
                for item in rows
            )
        )

        self.assertEqual(
            {item.sample_id: item.composite_score for item in production_scores},
            {item.sample_id: score for item, score in zip(rows, audited_scores, strict=True)},
        )

    def test_evidence_is_immutable_and_idempotent(self) -> None:
        rows, dates = self._rows()
        report = analyze_head_separability(
            rows,
            market="US",
            dataset_version="fixture-v1",
            horizon_days=5,
            analysis_dates=dates,
            excluded_tail_date_count=10,
            score_definitions=(
                SeparabilityScoreDefinition(
                    "good",
                    (ScoreComponent("good", FactorDirection.HIGHER_BETTER),),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first = persist_head_separability_report(report, root=root)
            second = persist_head_separability_report(report, root=root)
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.evidence_version, second.evidence_version)


if __name__ == "__main__":
    unittest.main()
