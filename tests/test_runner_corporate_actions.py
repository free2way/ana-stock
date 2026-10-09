from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import polars as pl

from app.services.adjusted_view_store import adjusted_view_path
from app.services.backtesting.runner import EventDrivenBacktestRunner
from app.services.corporate_actions import CorporateActionRecord

ROOT = Path(__file__).resolve().parents[1]


def _record(symbol: str, action_type: str, effective: str, **kwargs) -> CorporateActionRecord:
    return CorporateActionRecord(
        market="US", symbol=symbol, action_type=action_type,
        effective_date=date.fromisoformat(effective), **kwargs,
    )


@contextmanager
def _session():
    yield None


def _fakes(captured: dict):
    """Fake repository classes that record the run manifest for assertions."""

    class _StrategyRepo:
        def __init__(self, db):
            pass

        def create_run(self, **kwargs):
            captured["config"] = dict(kwargs.get("config") or {})
            return SimpleNamespace(id=5)

        def merge_config(self, run_id, updates, *, commit=True):
            captured["config"].update(dict(updates))
            return SimpleNamespace(id=run_id)

        def replace_daily_metrics(self, *args, **kwargs):
            return {}

        def replace_execution_audit(self, *args, **kwargs):
            return {}

        def complete_run(self, run_id, *, status, summary):
            captured["status"] = status
            captured["summary"] = summary

    class _ModelRepo:
        def __init__(self, db):
            pass

        def get_run_by_id(self, run_id):
            return SimpleNamespace(id=1, market="US", name="m1")

        def get_latest_run(self):
            return SimpleNamespace(id=1, market="US", name="m1")

    class _SymbolRepo:
        def __init__(self, db):
            pass

        def list_symbols(self):
            return [SimpleNamespace(id=1, ticker="AAA", name="Alpha")]

    class _PredictionRepo:
        def __init__(self, db):
            pass

        def list_for_model_run(self, run_id):
            return [
                SimpleNamespace(
                    id=11, symbol_id=1, trade_date=date(2026, 1, 5), score=1.0, rank_value=1.0
                )
            ]

    return _StrategyRepo, _ModelRepo, _SymbolRepo, _PredictionRepo


_LAKE_ROWS = [
    {"symbol": "AAA", "date": "2026-01-05", "open": 10.0, "high": 10.0, "low": 10.0,
     "close": 10.0, "volume": 100_000.0},
    {"symbol": "AAA", "date": "2026-01-06", "open": 10.0, "high": 10.0, "low": 10.0,
     "close": 10.0, "volume": 100_000.0},
]


@contextmanager
def _isolated_price_basis(*, view_state: str = "present"):
    """Use real adjusted-view probing and hashing, never the operator's lake."""
    if view_state not in {"present", "absent", "unreadable"}:
        raise ValueError(f"Unsupported fixture view state: {view_state}")
    with TemporaryDirectory(prefix="ana-backtest-basis-") as directory:
        data_dir = Path(directory)
        path = adjusted_view_path("US", root=data_dir / "lake")
        if view_state != "absent":
            path.parent.mkdir(parents=True, exist_ok=True)
            if view_state == "present":
                pl.DataFrame(_LAKE_ROWS).write_parquet(path)
            else:
                path.write_bytes(b"fixture: not a parquet file")
        settings = SimpleNamespace(
            data_dir=data_dir,
            trainer_allow_raw_fallback=False,
            optin_reason=None,
            optin_operator=None,
        )
        with (
            patch("app.services.adjusted_view_store.get_settings", return_value=settings),
            patch("app.services.adjustment_snapshot.get_settings", return_value=settings),
            patch("app.services.backtesting.runner.get_settings", return_value=settings),
        ):
            yield path


def _run_backtest(
    *,
    records,
    allow_unmodeled_corporate_actions: bool = False,
    optin_reason: str | None = None,
    optin_operator: str | None = None,
    expect_error: bool = False,
    adjusted_view_state: str = "present",
) -> dict:
    """Drive the real runner.run with fake repositories and a two-session lake.

    Persistence, raw lake rows and corporate actions are faked. The adjusted view
    is an isolated file; its probe/hash, the event engine and guards run for real.
    """

    captured: dict = {}
    _StrategyRepo, _ModelRepo, _SymbolRepo, _PredictionRepo = _fakes(captured)
    with (
        _isolated_price_basis(view_state=adjusted_view_state),
        patch("app.services.backtesting.runner.SessionLocal", _session),
        patch("app.services.backtesting.runner.StrategyRunRepository", _StrategyRepo),
        patch("app.services.backtesting.runner.ModelRunRepository", _ModelRepo),
        patch("app.services.backtesting.runner.SymbolRepository", _SymbolRepo),
        patch("app.services.backtesting.runner.PredictionWriteRepository", _PredictionRepo),
        patch("app.services.backtesting.runner.load_lake_rows", return_value=_LAKE_ROWS),
        patch("app.services.corporate_actions.load_actions", return_value=records),
    ):
        try:
            EventDrivenBacktestRunner().run(
                top_n=1,
                model_run_id=1,
                holding_days=1,
                commission_bps=0.0,
                slippage_bps=0.0,
                max_position_weight=1.0,
                min_signal_score=0.0,
                min_adv=0.0,
                max_gap_pct=1.0,
                allow_unmodeled_corporate_actions=allow_unmodeled_corporate_actions,
                optin_reason=optin_reason,
                optin_operator=optin_operator,
            )
        except RuntimeError as exc:
            if not expect_error:
                raise
            captured["error"] = exc
    return captured


class RunnerCorporateActionTests(TestCase):
    """A1: the event-engine runner consumes the stored corporate actions."""

    def test_mapping_supports_splits_bonus_and_cash(self) -> None:
        records = [
            _record("AAA", "split", "2026-06-10", factor=2.0),
            _record("AAA", "stock_dividend", "2026-07-01", factor=1.1),
            _record("AAA", "cash_dividend", "2026-08-01", cash_amount=0.5),
            _record("AAA", "merger", "2026-09-01", cash_amount=12.0),
            _record("BBB", "split", "2026-06-10", factor=3.0),  # not in the ticker set
            _record("AAA", "split", "2027-06-10", factor=2.0),  # outside the window
        ]
        with patch("app.services.corporate_actions.load_actions", return_value=records):
            actions, stats = EventDrivenBacktestRunner()._load_market_corporate_actions(
                market="US",
                tickers={"AAA"},
                start_date="2026-06-01",
                end_date="2026-09-30",
                holding_days=5,
            )
        self.assertEqual(["2026-06-10", "2026-07-01", "2026-08-01"], [item.effective_date for item in actions])
        self.assertEqual(["split", "split", "cash_dividend"], [item.action_type for item in actions])
        self.assertAlmostEqual(2.0, actions[0].factor)
        self.assertAlmostEqual(1.1, actions[1].factor)
        self.assertAlmostEqual(0.5, actions[2].cash_amount)
        # The unsupported merger is kept verbatim, not just counted.
        self.assertEqual(
            {
                "loaded": 4,
                "supported": 3,
                "unsupported": 1,
                "unsupported_details": [
                    {"symbol": "AAA", "action_type": "merger", "effective_date": "2026-09-01"}
                ],
            },
            stats,
        )

    def test_format_lists_head_events_and_remaining_count(self) -> None:
        details = [
            {"symbol": f"S{i:02d}", "action_type": "merger", "effective_date": f"2026-06-{i + 1:02d}"}
            for i in range(7)
        ]
        rendered = EventDrivenBacktestRunner._format_unmodeled_corporate_actions(details, limit=5)
        self.assertIn("7 event(s)", rendered)
        self.assertIn("S00 merger 2026-06-01", rendered)
        self.assertIn("S04 merger 2026-06-05", rendered)
        self.assertNotIn("S05", rendered)
        self.assertIn("+2 more", rendered)

    def test_runner_passes_actions_into_the_engine(self) -> None:
        source = (ROOT / "app" / "services" / "backtesting" / "runner.py").read_text(encoding="utf-8")
        self.assertIn("corporate_actions=corporate_actions)", source)
        self.assertIn("_load_market_corporate_actions(", source)
        self.assertIn('"corporate_actions": action_stats', source)
        self.assertIn('"unmodeled_corporate_actions": unmodeled', source)
        self.assertIn('"unmodeled_opt_in"', source)


class RunnerUnmodeledCorporateActionGuardTests(TestCase):
    """Fail closed when traded names carry event types the engine cannot model."""

    def test_in_window_unmodeled_event_blocks_by_default(self) -> None:
        captured = _run_backtest(
            records=[_record("AAA", "merger", "2026-01-06", cash_amount=12.0)],
            expect_error=True,
        )
        self.assertIsInstance(captured["error"], RuntimeError)
        message = str(captured["error"])
        self.assertIn("1 event(s)", message)
        self.assertIn("AAA merger 2026-01-06", message)
        self.assertIn("allow_unmodeled_corporate_actions=True", message)
        # The aborted run is persisted as failed with the event list recorded.
        self.assertEqual("failed", captured["status"])
        self.assertEqual(
            [{"symbol": "AAA", "action_type": "merger", "effective_date": "2026-01-06"}],
            captured["summary"]["unmodeled_corporate_actions"],
        )
        self.assertFalse(captured["summary"]["unmodeled_opt_in"])
        # Config carries the same detail: it is merged before the guard aborts.
        self.assertEqual(
            [{"symbol": "AAA", "action_type": "merger", "effective_date": "2026-01-06"}],
            captured["config"]["unmodeled_corporate_actions"],
        )
        self.assertFalse(captured["config"]["unmodeled_opt_in"])
        self.assertTrue(captured["config"]["allow_unmodeled_corporate_actions"] is False)

    def test_opt_in_runs_and_manifest_records_events_and_flag(self) -> None:
        captured = _run_backtest(
            records=[_record("AAA", "merger", "2026-01-06", cash_amount=12.0)],
            allow_unmodeled_corporate_actions=True,
            optin_reason="fixture: accepting modeled-out merger risk",
            optin_operator="fixture_operator",
        )
        self.assertEqual("success", captured["status"])
        self.assertEqual(
            [{"symbol": "AAA", "action_type": "merger", "effective_date": "2026-01-06"}],
            captured["summary"]["unmodeled_corporate_actions"],
        )
        self.assertTrue(captured["summary"]["unmodeled_opt_in"])
        self.assertTrue(captured["config"]["allow_unmodeled_corporate_actions"])
        # Config carries the same detail on the opt-in (success) path.
        self.assertEqual(
            [{"symbol": "AAA", "action_type": "merger", "effective_date": "2026-01-06"}],
            captured["config"]["unmodeled_corporate_actions"],
        )
        self.assertTrue(captured["config"]["unmodeled_opt_in"])
        # The opt-in is upgraded to a structured, accountable record in both the
        # config and the summary so the waiver can be traced to a person/reason.
        for holder in (captured["config"], captured["summary"]):
            audit = holder["unmodeled_corporate_actions_optin_audit"]
            self.assertTrue(audit["enabled"])
            self.assertEqual("fixture_operator", audit["operator"])
            self.assertEqual("run_parameter", audit["operator_source"])
            self.assertEqual("fixture: accepting modeled-out merger risk", audit["reason"])
            self.assertEqual("allow_unmodeled_corporate_actions", audit["source"])
            self.assertEqual("backtest", audit["scope"]["entry_point"])
            self.assertIsNotNone(audit["decided_at"])

    def test_opt_in_without_reason_is_refused_before_any_run_is_created(self) -> None:
        with patch.dict("os.environ", {"PQW_OPTIN_REASON": ""}, clear=False):
            captured = _run_backtest(
                records=[_record("AAA", "merger", "2026-01-06", cash_amount=12.0)],
                allow_unmodeled_corporate_actions=True,
                optin_reason="",
                expect_error=True,
            )
        self.assertIsInstance(captured["error"], RuntimeError)
        self.assertIn("without a reason", str(captured["error"]))
        # Fail closed before any manifest row exists.
        self.assertNotIn("config", captured)
        self.assertNotIn("status", captured)

    def test_out_of_window_or_untraded_events_do_not_block(self) -> None:
        records = [
            _record("AAA", "merger", "2026-03-01", cash_amount=12.0),  # outside backtest window
            _record("BBB", "spinoff", "2026-01-06", factor=1.0),  # not in the traded set
        ]
        captured = _run_backtest(records=records)
        self.assertEqual("success", captured["status"])
        self.assertEqual([], captured["summary"]["unmodeled_corporate_actions"])
        self.assertFalse(captured["summary"]["unmodeled_opt_in"])
        self.assertEqual([], captured["config"]["unmodeled_corporate_actions"])
        self.assertFalse(captured["config"]["unmodeled_opt_in"])

    def test_supported_splits_and_cash_dividends_still_run(self) -> None:
        records = [
            _record("AAA", "split", "2026-01-06", factor=2.0),
            _record("AAA", "cash_dividend", "2026-01-06", cash_amount=0.5),
        ]
        captured = _run_backtest(records=records)
        self.assertEqual("success", captured["status"])
        self.assertEqual(2, captured["summary"]["corporate_actions"]["supported"])
        self.assertEqual(0, captured["summary"]["corporate_actions"]["unsupported"])


class RunnerCorporateActionConfigPersistenceTests(TestCase):
    """Read the persisted config back from the DB layer.

    The fake-repository tests above assert the manifest dict the runner hands
    to the repository; these drive the real repositories against in-memory
    SQLite and read ``strategy_runs.config_json`` back through
    ``BacktestRepository.get_backtest`` so a missing/incorrect persistence path
    cannot pass.
    """

    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.models.base import Base
        from app.models.tables import ModelRun, Prediction, Symbol

        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, future=True
        )
        now = "2026-01-01T00:00:00+00:00"
        with self.SessionLocal() as db:
            db.add(
                Symbol(
                    id=1, ticker="AAA", name="Alpha", market="US", exchange="TEST",
                    is_active=1, created_at=now, updated_at=now,
                )
            )
            db.add(
                ModelRun(
                    id=1, name="m1", model_type="ridge", market="US",
                    status="success", created_at=now,
                )
            )
            db.add(
                Prediction(
                    id=11, model_run_id=1, symbol_id=1, trade_date="2026-01-05",
                    score=1.0, rank_value=1.0, created_at=now,
                )
            )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _run(self, *, records, allow: bool, expect_error: bool, reason: str | None = None) -> dict:
        with (
            _isolated_price_basis(),
            patch("app.services.backtesting.runner.SessionLocal", self.SessionLocal),
            patch("app.services.backtesting.runner.load_lake_rows", return_value=_LAKE_ROWS),
            patch("app.services.corporate_actions.load_actions", return_value=records),
        ):
            try:
                EventDrivenBacktestRunner().run(
                    top_n=1,
                    model_run_id=1,
                    holding_days=1,
                    commission_bps=0.0,
                    slippage_bps=0.0,
                    max_position_weight=1.0,
                    min_signal_score=0.0,
                    min_adv=0.0,
                    max_gap_pct=1.0,
                    allow_unmodeled_corporate_actions=allow,
                    optin_reason=reason,
                    optin_operator="fixture_operator" if allow else None,
                )
            except RuntimeError:
                if not expect_error:
                    raise
        from app.services.repositories.backtests import BacktestRepository

        with self.SessionLocal() as db:
            repo = BacktestRepository(db)
            latest = repo.get_latest_backtest()
            return repo.get_backtest(latest.id)

    def test_default_reject_persists_details_in_config_and_summary(self) -> None:
        payload = self._run(
            records=[_record("AAA", "merger", "2026-01-06", cash_amount=12.0)],
            allow=False,
            expect_error=True,
        )
        expected = [
            {"symbol": "AAA", "action_type": "merger", "effective_date": "2026-01-06"}
        ]
        self.assertEqual("failed", payload["status"])
        self.assertEqual(expected, payload["config"]["unmodeled_corporate_actions"])
        self.assertFalse(payload["config"]["unmodeled_opt_in"])
        self.assertEqual(expected, payload["summary"]["unmodeled_corporate_actions"])
        self.assertFalse(payload["summary"]["unmodeled_opt_in"])

    def test_opt_in_persists_details_in_config_and_summary(self) -> None:
        payload = self._run(
            records=[_record("AAA", "merger", "2026-01-06", cash_amount=12.0)],
            allow=True,
            expect_error=False,
            reason="fixture: accept merger risk for persistence test",
        )
        expected = [
            {"symbol": "AAA", "action_type": "merger", "effective_date": "2026-01-06"}
        ]
        self.assertEqual("success", payload["status"])
        self.assertEqual(expected, payload["config"]["unmodeled_corporate_actions"])
        self.assertTrue(payload["config"]["unmodeled_opt_in"])
        self.assertEqual(expected, payload["summary"]["unmodeled_corporate_actions"])
        self.assertTrue(payload["summary"]["unmodeled_opt_in"])
        # Structured audit survives the real DB round-trip.
        config_audit = payload["config"]["unmodeled_corporate_actions_optin_audit"]
        summary_audit = payload["summary"]["unmodeled_corporate_actions_optin_audit"]
        self.assertTrue(config_audit["enabled"])
        self.assertEqual("fixture_operator", config_audit["operator"])
        self.assertEqual("fixture: accept merger risk for persistence test", config_audit["reason"])
        self.assertIsNotNone(config_audit["decided_at"])
        self.assertEqual(config_audit, summary_audit)
