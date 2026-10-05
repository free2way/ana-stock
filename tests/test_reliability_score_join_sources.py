"""Score-join storage-layer + evaluated-run-selection acceptance.

The real database stores US scores only in the physical ``us_predictions`` table
(legacy ``predictions`` has zero rows) and cold ``prediction_artifacts``; and
ablation/experiment runs are written as ``success`` without a
``ModelEvaluation``.  These tests pin the fixes:

1. the score join falls back to the physical table and the cold Parquet artifact,
   aligned with the legacy structure;
2. when a key exists in several layers the physical table wins and the winning
   source is audited;
3. the newest *evaluated* success run is selected, with skipped runs and reasons
   audited, so an unevaluated run cannot shadow it;
4. an explicit ``run_ids`` list overrides the scan.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import tables  # noqa: F401  (register metadata)
from app.models.base import Base
from app.models.tables import (
    ModelEvaluation,
    ModelRun,
    Prediction,
    PredictionArtifact,
    Symbol,
    USPrediction,
)
from app.services.prediction_artifacts import PredictionArtifactWriter
from app.services.stock_selection.reliability_artifacts import (
    MaturedReliabilityRow,
    collect_matured_rows,
    model_key_resolution_path,
    persist_reliability_artifacts,
)

HIGH = "lightgbm_top_picks"
HORIZON = 5
CREATED = "2026-04-20T00:00:00+00:00"
PHYSICAL_CREATED = datetime(2026, 4, 20, 0, 0, 0)


def _symbol(db: Session, *, ticker: str, market: str = "US") -> Symbol:
    symbol = Symbol(
        ticker=ticker,
        name=ticker,
        market=market,
        is_active=1,
        created_at=CREATED,
        updated_at=CREATED,
    )
    db.add(symbol)
    db.flush()
    return symbol


def _run(db: Session, *, run_id: int, name: str, market: str = "US") -> ModelRun:
    run = ModelRun(
        id=run_id,
        name=name,
        model_type="lightgbm_multifactor",
        market=market,
        universe="full_market_us_lake",
        status="success",
        created_at=CREATED,
        config_json=json.dumps(
            {"model_type": "lightgbm", "signal_type": "momentum", "prediction_horizon_days": HORIZON}
        ),
    )
    db.add(run)
    db.flush()
    return run


def _evaluation(
    db: Session,
    *,
    run_id: int,
    market: str,
    outcomes: list[dict],
    status: str = "success",
) -> ModelEvaluation:
    evaluation = ModelEvaluation(
        model_run_id=run_id,
        market=market,
        evaluation_type="prediction_forward_return",
        is_out_of_sample=1,
        sample_count=len(outcomes),
        status=status,
        round_trip_cost_bps=50.0,
        summary_json=json.dumps({"candidate_outcomes": outcomes}),
        created_at=CREATED,
        finished_at=CREATED,
    )
    db.add(evaluation)
    db.flush()
    return evaluation


def _outcomes(
    *, tickers: tuple[str, ...], trade_dates: tuple[date, ...], score_by_ticker: dict[str, float]
) -> list[dict]:
    rows: list[dict] = []
    for trade_date in trade_dates:
        for ticker in tickers:
            score = score_by_ticker[ticker]
            rows.append(
                {
                    "ticker": ticker,
                    "trade_date": trade_date.isoformat(),
                    "horizon_days": HORIZON,
                    "status": "CLOSED",
                    "is_out_of_sample": True,
                    "net_return": 0.02 if score > 0.5 else -0.01,
                    "label_available_date": (trade_date + timedelta(days=HORIZON)).isoformat(),
                }
            )
    return rows


class ScoreJoinLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.dates = (date(2026, 3, 2), date(2026, 3, 3))

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_physical_table_fallback_when_legacy_is_empty(self) -> None:
        with Session(self.engine) as db:
            symbols = {ticker: _symbol(db, ticker=ticker) for ticker in ("T0", "T1")}
            run = _run(db, run_id=100, name="us_close_2026-03-04")
            _evaluation(
                db,
                run_id=run.id,
                market="US",
                outcomes=_outcomes(
                    tickers=("T0", "T1"), trade_dates=self.dates, score_by_ticker={"T0": 0.1, "T1": 0.9}
                ),
            )
            for trade_date in self.dates:
                for ticker, score in (("T0", 0.1), ("T1", 0.9)):
                    db.add(
                        USPrediction(
                            model_run_id=run.id,
                            symbol_id=symbols[ticker].id,
                            market="US",
                            trade_date=trade_date,
                            score=score,
                            rank_value=score,
                            created_at=PHYSICAL_CREATED,
                        )
                    )
            db.commit()

            rows, key_map = collect_matured_rows(db, markets=["US"], recent_runs=1)

        self.assertEqual(len(self.dates) * 2, len(rows))
        self.assertEqual({HIGH}, set(key_map["model_key_by_version"].values()))
        version = next(iter(key_map["score_sources"]))
        audit = key_map["score_sources"][version]
        self.assertEqual("us_predictions", audit["physical_table"])
        self.assertEqual(len(self.dates) * 2, audit["physical_rows"])
        self.assertEqual(0, audit["legacy_rows"])
        self.assertEqual(0, audit["cold_artifact_rows"])
        self.assertEqual({"physical_table": len(self.dates) * 2}, audit["origin_counts"])
        self.assertEqual("physical_table>legacy_table>cold_artifact", audit["priority"])

    def test_cold_artifact_fallback_when_hot_layers_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Session(self.engine) as db:
                symbols = {ticker: _symbol(db, ticker=ticker) for ticker in ("T0", "T1")}
                run = _run(db, run_id=101, name="us_close_2026-03-04")
                _evaluation(
                    db,
                    run_id=run.id,
                    market="US",
                    outcomes=_outcomes(
                        tickers=("T0", "T1"),
                        trade_dates=self.dates,
                        score_by_ticker={"T0": 0.2, "T1": 0.8},
                    ),
                )
                manifest = PredictionArtifactWriter(root=Path(directory)).write(
                    model_run_id=run.id,
                    market="US",
                    prediction_rows=[
                        {
                            "symbol_id": symbols[ticker].id,
                            "trade_date": trade_date.isoformat(),
                            "score": score,
                            "rank_value": score,
                        }
                        for trade_date in self.dates
                        for ticker, score in (("T0", 0.2), ("T1", 0.8))
                    ],
                )
                db.add(
                    PredictionArtifact(
                        model_run_id=run.id,
                        status="verified",
                        schema_version=str(manifest["schema_version"]),
                        market="US",
                        artifact_path=str(manifest["artifact_path"]),
                        manifest_sha256=str(manifest["manifest_sha256"]),
                        prediction_count=int(manifest["row_count"]),
                        created_at=CREATED,
                    )
                )
                db.commit()

                rows, key_map = collect_matured_rows(db, markets=["US"], recent_runs=1)

        self.assertEqual(len(self.dates) * 2, len(rows))
        version = next(iter(key_map["score_sources"]))
        audit = key_map["score_sources"][version]
        self.assertEqual(0, audit["physical_rows"])
        self.assertEqual(0, audit["legacy_rows"])
        self.assertEqual(len(self.dates) * 2, audit["cold_artifact_rows"])
        self.assertEqual({"cold_artifact": len(self.dates) * 2}, audit["origin_counts"])
        self.assertAlmostEqual(0.8, max(row.raw_score for row in rows))

    def test_physical_wins_over_legacy_and_cold_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Session(self.engine) as db:
                symbols = {ticker: _symbol(db, ticker=ticker) for ticker in ("T0", "T1", "T2")}
                run = _run(db, run_id=102, name="us_close_2026-03-04")
                trade_date = self.dates[0]
                _evaluation(
                    db,
                    run_id=run.id,
                    market="US",
                    outcomes=_outcomes(
                        tickers=("T0", "T1", "T2"),
                        trade_dates=(trade_date,),
                        score_by_ticker={"T0": 0.5, "T1": 0.5, "T2": 0.5},
                    ),
                )
                # T0 exists in all three layers with conflicting scores; the
                # physical table value (0.9) must win.
                db.add(
                    Prediction(
                        model_run_id=run.id,
                        symbol_id=symbols["T0"].id,
                        trade_date=trade_date.isoformat(),
                        score=0.3,
                        rank_value=0.3,
                        created_at=CREATED,
                    )
                )
                db.add(
                    Prediction(
                        model_run_id=run.id,
                        symbol_id=symbols["T1"].id,
                        trade_date=trade_date.isoformat(),
                        score=0.7,
                        rank_value=0.7,
                        created_at=CREATED,
                    )
                )
                for ticker, score in (("T0", 0.9), ("T2", 0.1)):
                    db.add(
                        USPrediction(
                            model_run_id=run.id,
                            symbol_id=symbols[ticker].id,
                            market="US",
                            trade_date=trade_date,
                            score=score,
                            rank_value=score,
                            created_at=PHYSICAL_CREATED,
                        )
                    )
                manifest = PredictionArtifactWriter(root=Path(directory)).write(
                    model_run_id=run.id,
                    market="US",
                    prediction_rows=[
                        {
                            "symbol_id": symbols[ticker].id,
                            "trade_date": trade_date.isoformat(),
                            "score": 0.4,
                            "rank_value": 0.4,
                        }
                        for ticker in ("T0",)
                    ],
                )
                db.add(
                    PredictionArtifact(
                        model_run_id=run.id,
                        status="verified",
                        schema_version=str(manifest["schema_version"]),
                        market="US",
                        artifact_path=str(manifest["artifact_path"]),
                        manifest_sha256=str(manifest["manifest_sha256"]),
                        prediction_count=int(manifest["row_count"]),
                        created_at=CREATED,
                    )
                )
                db.commit()

                rows, key_map = collect_matured_rows(db, markets=["US"], recent_runs=1)

        by_ticker = {row.ticker: row for row in rows}
        self.assertEqual({"T0", "T1", "T2"}, set(by_ticker))
        # T0: physical 0.9 beats legacy 0.3 and cold 0.4.
        self.assertAlmostEqual(0.9, by_ticker["T0"].raw_score)
        # T1: legacy only.
        self.assertAlmostEqual(0.7, by_ticker["T1"].raw_score)
        # T2: physical only.
        self.assertAlmostEqual(0.1, by_ticker["T2"].raw_score)
        version = next(iter(key_map["score_sources"]))
        audit = key_map["score_sources"][version]
        self.assertEqual(2, audit["physical_rows"])
        self.assertEqual(2, audit["legacy_rows"])
        self.assertEqual(1, audit["cold_artifact_rows"])
        self.assertEqual(3, audit["merged_score_keys"])
        self.assertEqual(
            {"physical_table": 2, "legacy_table": 1},
            audit["origin_counts"],
        )


class AuditPersistenceTests(unittest.TestCase):
    def test_resolution_sidecar_records_score_sources_and_run_selection(self) -> None:
        rows = [
            MaturedReliabilityRow(
                model_version="model_run:1:us_close",
                ticker="T0",
                feature_date=date(2026, 1, 5),
                label_available_date=date(2026, 1, 12),
                horizon_days=HORIZON,
                raw_score=0.1,
                cross_sectional_rank=0.1,
                net_return=0.01,
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            persist_reliability_artifacts(
                rows,
                as_of_date=date(2026, 2, 1),
                score_sources={"model_run:1:us_close": {"origin_counts": {"physical_table": 1}}},
                run_selection={
                    "US": {
                        "selection_basis": "newest_evaluated_success_run",
                        "selected": [{"run_id": 1, "evaluation_id": 2}],
                        "skipped": [{"run_id": 3, "reason": "no_evaluation"}],
                    }
                },
                root=directory,
            )
            payload = json.loads(model_key_resolution_path(directory).read_text(encoding="utf-8"))

        self.assertEqual(
            {"origin_counts": {"physical_table": 1}},
            payload["score_sources"]["model_run:1:us_close"],
        )
        selection = payload["run_selection"]["US"]
        self.assertEqual("newest_evaluated_success_run", selection["selection_basis"])
        self.assertEqual(2, selection["selected"][0]["evaluation_id"])
        self.assertEqual("no_evaluation", selection["skipped"][0]["reason"])


class RunSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.dates = (date(2026, 3, 2), date(2026, 3, 3))
        self.tickers = ("T0", "T1")

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed(
        self, db: Session, run_id: int, *, with_evaluation: bool, symbols: dict[str, Symbol]
    ) -> None:
        run = _run(db, run_id=run_id, name=f"us_close_run_{run_id}")
        for trade_date in self.dates:
            for ticker, score in (("T0", 0.1), ("T1", 0.9)):
                db.add(
                    USPrediction(
                        model_run_id=run.id,
                        symbol_id=symbols[ticker].id,
                        market="US",
                        trade_date=trade_date,
                        score=score,
                        rank_value=score,
                        created_at=PHYSICAL_CREATED,
                    )
                )
        if with_evaluation:
            _evaluation(
                db,
                run_id=run.id,
                market="US",
                outcomes=_outcomes(
                    tickers=self.tickers, trade_dates=self.dates, score_by_ticker={"T0": 0.1, "T1": 0.9}
                ),
            )
        db.flush()

    def test_unevaluated_success_run_is_skipped_and_audited(self) -> None:
        with Session(self.engine) as db:
            symbols = {ticker: _symbol(db, ticker=ticker) for ticker in self.tickers}
            self._seed(db, 11, with_evaluation=True, symbols=symbols)
            self._seed(db, 12, with_evaluation=False, symbols=symbols)  # ablation-style shadow run
            db.commit()

            rows, key_map = collect_matured_rows(db, markets=["US"], recent_runs=1)

        self.assertEqual(len(self.dates) * 2, len(rows))
        selection = key_map["run_selection"]["US"]
        self.assertEqual("newest_evaluated_success_run", selection["selection_basis"])
        self.assertEqual([11], [item["run_id"] for item in selection["selected"]])
        self.assertIsNotNone(selection["selected"][0]["evaluation_id"])
        self.assertEqual(
            [{"run_id": 12, "reason": "no_evaluation", "run_status": "success", "run_name": "us_close_run_12"}],
            selection["skipped"],
        )
        self.assertEqual(2, selection["scanned_run_count"])

    def test_selection_is_independent_of_unevaluated_run_depth(self) -> None:
        with Session(self.engine) as db:
            symbols = {ticker: _symbol(db, ticker=ticker) for ticker in self.tickers}
            self._seed(db, 30, with_evaluation=True, symbols=symbols)
            for run_id in range(31, 41):  # 10 ablation runs stack above it
                self._seed(db, run_id, with_evaluation=False, symbols=symbols)
            db.commit()

            rows, key_map = collect_matured_rows(
                db, markets=["US"], recent_runs=1, run_scan_limit=3
            )

        self.assertEqual(len(self.dates) * 2, len(rows))
        selection = key_map["run_selection"]["US"]
        self.assertEqual([30], [item["run_id"] for item in selection["selected"]])
        # The audit list is bounded by run_scan_limit but the evaluated run is
        # still found no matter how deep it sits below unevaluated runs.
        self.assertEqual(3, len(selection["skipped"]))
        self.assertEqual(40, selection["skipped"][0]["run_id"])

    def test_explicit_run_ids_override_newest_selection(self) -> None:
        with Session(self.engine) as db:
            symbols = {ticker: _symbol(db, ticker=ticker) for ticker in self.tickers}
            self._seed(db, 21, with_evaluation=True, symbols=symbols)
            self._seed(db, 22, with_evaluation=True, symbols=symbols)
            db.commit()

            default_rows, default_map = collect_matured_rows(db, markets=["US"], recent_runs=1)
            self.assertEqual([22], [item["run_id"] for item in default_map["run_selection"]["US"]["selected"]])

            pinned_rows, pinned_map = collect_matured_rows(db, markets=["US"], run_ids=[21])

        self.assertEqual(len(self.dates) * 2, len(pinned_rows))
        self.assertEqual(len(default_rows), len(pinned_rows))
        selection = pinned_map["run_selection"]["US"]
        self.assertEqual("explicit_run_ids", selection["selection_basis"])
        self.assertEqual([21], [item["run_id"] for item in selection["selected"]])
        self.assertEqual("model_run:21:us_close_run_21", next(iter(pinned_map["score_sources"])))
        self.assertEqual("model_run:22:us_close_run_22", next(iter(default_map["score_sources"])))


if __name__ == "__main__":
    unittest.main()
