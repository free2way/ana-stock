"""Provenance price loader + evaluation-side execution contract derivation.

Covers the two blockers that kept reliability/calibration artifacts
unproducible: the lake reader returned OHLCV only (so every real candidate
replayed as ``raw_price_basis_unverified``), and runs labelled with the default
executable profile persisted a null ``execution_contract``.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import polars as pl

from app.services.corporate_actions import (
    CorporateActionRecord,
    write_actions,
)
from app.services.execution_costs import FillCostModel
from app.services.execution_reconciliation import (
    execution_contract,
    replay_candidate,
)
from app.services.market_calendar import next_market_open_date
from app.services.market_lake import (
    load_lake_price_history_with_provenance,
)
from app.services.model_evaluation import resolve_execution_contract

BASE_COLUMNS: dict[str, pl.DataType] = {
    "date": pl.String,
    "symbol": pl.String,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "adj_close": pl.Float64,
}


def _series(market: str, symbol: str, signal_date: str, horizon: int, *, drift: float = 0.01) -> list[dict]:
    days = [signal_date]
    for _ in range(horizon):
        days.append(next_market_open_date(market, days[-1], include_self=False))
    rows: list[dict] = []
    price = 100.0
    for index, day in enumerate(days):
        close = round(price * (1.0 + drift * index), 4)
        rows.append(
            {
                "date": day,
                "symbol": symbol,
                "open": round(price * (1.0 + drift * index), 4),
                "high": round(close * 1.01, 4),
                "low": round(close * 0.99, 4),
                "close": close,
                "volume": 100000.0,
                "adj_close": close,
            }
        )
    return rows


def _write_partition(root: Path, store: str, market: str, rows: list[dict], *, provenance: bool) -> None:
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(str(row["date"]), []).append(row)
    for day, day_rows in by_date.items():
        payload = [dict(row) for row in day_rows]
        schema = dict(BASE_COLUMNS)
        if provenance:
            for row in payload:
                row["price_basis"] = "raw"
                row["source_reference"] = f"provider_fixture:{market.lower()}:{day}"
            schema["price_basis"] = pl.String
            schema["source_reference"] = pl.String
        store_dir = root / store / f"{market.lower()}_daily" / f"date={day}"
        store_dir.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(payload, schema=schema, orient="row").write_parquet(store_dir / "part.parquet")


class LoaderFieldTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lake_prov_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.actions_root = Path(tempfile.mkdtemp(prefix="actions_prov_"))
        self.addCleanup(shutil.rmtree, self.actions_root, ignore_errors=True)

    def test_v2_rows_expose_exact_reconciliation_vocabulary(self) -> None:
        rows = _series("CN", "000001.SZ", "2026-07-01", 5)
        _write_partition(self.root, "_lake_v2", "CN", rows, provenance=True)
        loaded = load_lake_price_history_with_provenance(
            market="CN", ticker="000001.SZ", limit=320,
            lake_root=self.root, actions_root=self.actions_root,
        )
        self.assertEqual(len(rows), len(loaded))
        for row in loaded:
            self.assertEqual("raw", row["price_basis"])
            self.assertTrue(row["execution_source_reference"].startswith("lake_v2:CN:"))
            self.assertIn(row["corporate_action_status"], {"none", "action"})
            # exact reconciliation vocab, no richer aliases leak through
            self.assertNotIn(row["corporate_action_status"], {"verified", "none_required"})
        self.assertEqual(
            "lake_v2:CN:%s:000001.SZ" % rows[0]["date"],
            loaded[0]["execution_source_reference"],
        )

    def test_v1_fallback_synthesises_documented_reference(self) -> None:
        rows = _series("US", "AAPL", "2026-07-01", 5)
        _write_partition(self.root, "", "US", rows, provenance=False)
        loaded = load_lake_price_history_with_provenance(
            market="US", ticker="AAPL", limit=320,
            lake_root=self.root, actions_root=self.actions_root,
        )
        self.assertEqual(len(rows), len(loaded))
        for row in loaded:
            self.assertEqual("raw", row["price_basis"])
            self.assertTrue(row["execution_source_reference"].startswith("lake_v1:US:"))
            self.assertEqual(
                f"us_daily/date={row['date']}/part.parquet", row["lake_source_reference"]
            )

    def test_loader_is_deterministic(self) -> None:
        rows = _series("CN", "600000.SS", "2026-07-01", 5)
        _write_partition(self.root, "_lake_v2", "CN", rows, provenance=True)
        first = load_lake_price_history_with_provenance(
            market="CN", ticker="600000.SS", limit=320,
            lake_root=self.root, actions_root=self.actions_root,
        )
        second = load_lake_price_history_with_provenance(
            market="CN", ticker="600000.SS", limit=320,
            lake_root=self.root, actions_root=self.actions_root,
        )
        self.assertEqual(first, second)

    def test_corporate_action_marks_the_event_bar_only(self) -> None:
        rows = _series("CN", "000002.SZ", "2026-07-01", 5)
        event_day = rows[3]["date"]
        write_actions(
            "CN",
            [
                CorporateActionRecord(
                    market="CN", symbol="000002.SZ", action_type="cash_dividend",
                    effective_date=date.fromisoformat(event_day), factor=None,
                    cash_amount=0.5, currency="CNY", announced_date=None,
                    source="fixture", source_reference="fixture:1",
                )
            ],
            root=self.actions_root,
        )
        _write_partition(self.root, "_lake_v2", "CN", rows, provenance=True)
        loaded = load_lake_price_history_with_provenance(
            market="CN", ticker="000002.SZ", limit=320,
            lake_root=self.root, actions_root=self.actions_root,
        )
        statuses = {row["date"]: row["corporate_action_status"] for row in loaded}
        self.assertEqual("action", statuses[event_day])
        self.assertEqual([event_day], next(
            row["corporate_action_evidence"] for row in loaded if row["date"] == event_day
        ))
        non_event = [status for day, status in statuses.items() if day != event_day]
        self.assertTrue(all(status == "none" for status in non_event))


class ExecutionContractDerivationTests(TestCase):
    def _run(self, config: dict) -> SimpleNamespace:
        return SimpleNamespace(config_json=json.dumps(config))

    def test_persisted_contract_is_returned_verbatim(self) -> None:
        frozen = execution_contract("CN", FillCostModel(8, 12))
        run = self._run({"execution_contract": frozen, "target_profile": "irrelevant"})
        self.assertEqual(frozen, resolve_execution_contract(run, "CN"))

    def test_executable_profile_derives_identical_contract_from_recorded_cost(self) -> None:
        run = self._run(
            {
                "target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2",
                "execution_cost_bps": {
                    "commission_bps_one_way": 2.5,
                    "slippage_bps_one_way": 15.0,
                },
            }
        )
        derived = resolve_execution_contract(run, "US")
        self.assertEqual(execution_contract("US", FillCostModel(2.5, 15.0)), derived)

    def test_reconciled_profile_derives_contract(self) -> None:
        run = self._run(
            {
                "target_profile": "reconciled_execution_v2_observable_ohlcv",
                "execution_cost_bps": {
                    "commission_bps_one_way": 1.0,
                    "slippage_bps_one_way": 2.0,
                },
            }
        )
        self.assertEqual(
            execution_contract("CN", FillCostModel(1.0, 2.0)),
            resolve_execution_contract(run, "CN"),
        )

    def test_legacy_profile_never_fabricates_a_contract(self) -> None:
        run = self._run(
            {
                "target_profile": "short_horizon_composite_v1",
                "execution_cost_bps": {"commission_bps_one_way": 2.5, "slippage_bps_one_way": 15.0},
            }
        )
        self.assertIsNone(resolve_execution_contract(run, "CN"))

    def test_executable_profile_without_recorded_cost_stays_unverified(self) -> None:
        run = self._run({"target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2"})
        self.assertIsNone(resolve_execution_contract(run, "CN"))

    def _write_manifest(self, root: Path, run_id: int, cost: dict | None) -> None:
        run_dir = root / "prediction_runs" / f"model_run_id={run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        payload: dict = {"model_run_id": run_id, "market": "CN"}
        if cost is not None:
            payload["execution_cost_bps"] = cost
        (run_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_config_without_cost_falls_back_to_run_manifest(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="contract_manifest_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self._write_manifest(
            root,
            390,
            {"commission_bps_one_way": 2.5, "slippage_bps_one_way": 22.5},
        )
        run = SimpleNamespace(
            id=390,
            config_json=json.dumps(
                {"target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2"}
            ),
        )
        with patch(
            "app.services.model_evaluation.get_settings",
            return_value=SimpleNamespace(artifacts_dir=root),
        ):
            derived = resolve_execution_contract(run, "CN")
        self.assertEqual(execution_contract("CN", FillCostModel(2.5, 22.5)), derived)

    def test_config_and_manifest_both_missing_cost_stays_unverified(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="contract_manifest_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        run = SimpleNamespace(
            id=391,
            config_json=json.dumps(
                {"target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2"}
            ),
        )
        with patch(
            "app.services.model_evaluation.get_settings",
            return_value=SimpleNamespace(artifacts_dir=root),
        ):
            self.assertIsNone(resolve_execution_contract(run, "CN"))

    def test_unreadable_manifest_does_not_guess_a_default_cost(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="contract_manifest_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self._write_manifest(root, 392, cost=None)
        run = SimpleNamespace(
            id=392,
            config_json=json.dumps(
                {"target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2"}
            ),
        )
        with patch(
            "app.services.model_evaluation.get_settings",
            return_value=SimpleNamespace(artifacts_dir=root),
        ):
            self.assertIsNone(resolve_execution_contract(run, "CN"))


class StripReplayProvenanceTests(TestCase):
    """The strip that used to fail: raw OHLCV -> blocker, enriched -> CLOSED."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lake_replay_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_provenance_strip_replays_closed_and_clears_blocker(self) -> None:
        market, symbol, signal_date, horizon = "CN", "000001.SZ", "2026-07-01", 5
        rows = _series(market, symbol, signal_date, horizon)
        contract = execution_contract(market, FillCostModel(2.5, 15.0))

        raw = replay_candidate(
            ticker=symbol, market=market, signal_date=signal_date,
            horizon_days=horizon, rows=[dict(row) for row in rows], contract=contract,
        )
        self.assertEqual("raw_price_basis_unverified", raw["reason"])

        _write_partition(self.root, "_lake_v2", market, rows, provenance=True)
        enriched = load_lake_price_history_with_provenance(
            market=market, ticker=symbol, limit=320,
            lake_root=self.root, actions_root=self.root / "no_actions",
        )
        replayed = replay_candidate(
            ticker=symbol, market=market, signal_date=signal_date,
            horizon_days=horizon, rows=enriched, contract=contract,
        )
        self.assertEqual("CLOSED", replayed["status"])
        self.assertIsNone(replayed["reason"])
        self.assertNotEqual("raw_price_basis_unverified", replayed["reason"])
