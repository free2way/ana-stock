"""Corporate-action coverage producer: rule, honesty guards and gate mapping.

The unified promotion gate's ``corporate_action_coverage`` check needs a run's
traded window audited. Before ``app/services/corporate_action_coverage.py`` the
trainer path had no producer, so every training run reported
``NOT_ENOUGH_EVIDENCE``. These tests pin the shared rule, the "absent store is
not a pass" guard, and the run-config mapping the gate reads.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app.services.corporate_action_coverage import (
    assess_corporate_action_coverage,
    corporate_action_window_end,
    coverage_evidence_fields,
    modeled_action,
)
from app.services.corporate_actions import CorporateActionRecord


def _record(symbol: str, action_type: str, effective: str, **kwargs) -> CorporateActionRecord:
    return CorporateActionRecord(
        market="CN",
        symbol=symbol,
        action_type=action_type,
        effective_date=date.fromisoformat(effective),
        **kwargs,
    )


class ModeledActionRuleTests(TestCase):
    def test_split_like_needs_a_factor(self) -> None:
        self.assertTrue(modeled_action("split", factor=2.0))
        self.assertTrue(modeled_action("stock_dividend", factor=0.5))
        self.assertFalse(modeled_action("split", factor=None))
        self.assertFalse(modeled_action("stock_dividend", factor=0.0))

    def test_cash_dividend_needs_a_cash_amount(self) -> None:
        self.assertTrue(modeled_action("cash_dividend", cash_amount=0.0))
        self.assertFalse(modeled_action("cash_dividend", cash_amount=None))

    def test_event_day_types_are_unmodeled(self) -> None:
        for action_type in ("merger", "spinoff", "delisting", "rights", "adjustment_factor"):
            self.assertFalse(modeled_action(action_type), action_type)

    def test_window_end_appends_the_holding_tail(self) -> None:
        # Mirrors the runner: max(14, holding_days * 3 + 7).
        self.assertEqual("2026-07-23", corporate_action_window_end("2026-07-09", holding_days=0))
        self.assertEqual("2026-08-03", corporate_action_window_end("2026-07-09", holding_days=6))


class CoverageAuditTests(TestCase):
    def setUp(self) -> None:
        # A *present* store path: `assess_corporate_action_coverage` only reads
        # the parquet through the patched `load_actions`, so an empty file is
        # enough to exercise the audited branch.
        self._tmp = TemporaryDirectory()
        self._store = Path(self._tmp.name) / "cn_actions.parquet"
        self._store.write_bytes(b"")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unmodeled_event_inside_window_is_reported(self) -> None:
        records = [
            _record("600000.SH", "cash_dividend", "2026-07-10", cash_amount=0.5),
            _record("600000.SH", "split", "2026-07-11", factor=2.0),
            _record("600001.SH", "merger", "2026-07-12"),
            _record("600002.SH", "spinoff", "2026-07-13"),
            # Outside the audited window (end + 14-day tail).
            _record("600003.SH", "rights", "2026-09-01"),
        ]
        with patch(
            "app.services.corporate_action_coverage.load_actions",
            return_value=records,
        ), patch(
            "app.services.corporate_action_coverage.actions_path",
            return_value=self._store,
        ):
            result = assess_corporate_action_coverage(
                market="CN",
                symbols={"600000.SH", "600001.SH", "600002.SH", "600003.SH"},
                start_date="2026-07-09",
                end_date="2026-07-20",
            )
        self.assertTrue(result["audited"])
        self.assertEqual(4, result["loaded"])
        self.assertEqual(2, result["modeled"])
        self.assertEqual(2, result["unmodeled"])
        self.assertEqual(
            [
                {"symbol": "600001.SH", "action_type": "merger", "effective_date": "2026-07-12"},
                {"symbol": "600002.SH", "action_type": "spinoff", "effective_date": "2026-07-13"},
            ],
            result["unmodeled_corporate_actions"],
        )
        self.assertFalse(result["unmodeled_opt_in"])
        # Gate-ready mapping: the two keys the gate reads, opt-in stays False.
        self.assertEqual(
            {
                "unmodeled_corporate_actions": result["unmodeled_corporate_actions"],
                "unmodeled_opt_in": False,
            },
            coverage_evidence_fields(result),
        )

    def test_symbol_filter_scopes_the_audit(self) -> None:
        records = [
            _record("600000.SH", "merger", "2026-07-12"),
            _record("600009.SH", "merger", "2026-07-12"),
        ]
        with patch(
            "app.services.corporate_action_coverage.load_actions", return_value=records
        ), patch(
            "app.services.corporate_action_coverage.actions_path",
            return_value=self._store,
        ):
            result = assess_corporate_action_coverage(
                market="CN",
                symbols={"600000.SH"},
                start_date="2026-07-09",
                end_date="2026-07-20",
            )
        self.assertEqual(1, result["loaded"])
        self.assertEqual(["600000.SH"], [row["symbol"] for row in result["unmodeled_corporate_actions"]])

    def test_absent_store_is_not_reported_as_a_pass(self) -> None:
        with patch(
            "app.services.corporate_action_coverage.actions_path",
            return_value=Path(self._tmp.name) / "does-not-exist.parquet",
        ):
            result = assess_corporate_action_coverage(
                market="HK", symbols=None, start_date="2026-07-09", end_date="2026-07-20"
            )
        self.assertFalse(result["audited"])
        self.assertIn("no corporate-action store", str(result["missing_reason"]))
        # The mapping must carry the reason, never the two evidence keys.
        fields = coverage_evidence_fields(result)
        self.assertNotIn("unmodeled_corporate_actions", fields)
        self.assertIn("corporate_action_coverage_missing_reason", fields)

    def test_unreadable_store_is_not_reported_as_a_pass(self) -> None:
        with patch(
            "app.services.corporate_action_coverage.actions_path",
            return_value=self._store,
        ), patch(
            "app.services.corporate_action_coverage.load_actions",
            side_effect=RuntimeError("corrupt parquet"),
        ):
            result = assess_corporate_action_coverage(
                market="CN", symbols=None, start_date="2026-07-09", end_date="2026-07-20"
            )
        self.assertFalse(result["audited"])
        self.assertIn("could not be read", str(result["missing_reason"]))


if __name__ == "__main__":
    import unittest

    unittest.main()
