from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from app.services.trainer import SignalTrainer


def _rows_with_split() -> list[dict]:
    """Six sessions where the raw close halves on day 2 (2:1 split) with no real move."""

    closes = [10.0, 10.0, 5.0, 5.0, 5.0, 5.0]
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "symbol": "SPLIT",
                "date": f"2026-06-{index + 1:02d}",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000.0,
            }
        )
    return rows


def _attach_adjusted(rows: list[dict]) -> None:
    for row in rows:
        # Adjusted series is flat at 10: the split is neutralised.
        for field in ("open", "high", "low", "close"):
            row[f"adjusted_{field}"] = 10.0


class TrainerLabelBasisTests(TestCase):
    """A1: labels consume the versioned adjusted view; features keep raw prices."""

    def test_label_price_prefers_adjusted_and_falls_back(self) -> None:
        trainer = SignalTrainer()
        row = {"close": 5.0, "adjusted_close": 10.0}
        self.assertEqual(10.0, trainer._label_price(row, "close"))
        self.assertEqual(5.0, trainer._label_price({"close": 5.0}, "close"))
        self.assertEqual(5.0, trainer._label_price({"close": 5.0, "adjusted_close": 0.0}, "close"))

    def test_attach_adjusted_basis_sets_basis_and_fields(self) -> None:
        trainer = SignalTrainer()
        rows = _rows_with_split()
        fake_view = {"SPLIT": {row["date"]: {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0} for row in rows}}
        with patch("app.services.adjusted_view_store.load_adjusted_bars", return_value=fake_view):
            attached = trainer._attach_adjusted_basis(rows, market="CN")
        self.assertEqual(len(rows), attached)
        self.assertEqual("adjusted_view", trainer._label_basis)
        self.assertEqual(10.0, rows[2]["adjusted_close"])

    def test_attach_without_view_keeps_raw_basis(self) -> None:
        trainer = SignalTrainer()
        with patch("app.services.adjusted_view_store.load_adjusted_bars", return_value={}):
            attached = trainer._attach_adjusted_basis(_rows_with_split(), market="CN")
        self.assertEqual(0, attached)
        self.assertEqual("raw", trainer._label_basis)

    def test_split_neutralised_in_labels_only_when_adjusted_present(self) -> None:
        trainer = SignalTrainer()
        rows = _rows_with_split()
        index = 1
        anchor_raw = rows[index]["close"]

        raw_target, raw_profile = trainer._build_short_horizon_target_profile(
            symbol_rows=rows, index=index, anchor_close=anchor_raw, limit_band_pct=10.0
        )
        # Raw basis sees the split as a ~-50% one-day move.
        self.assertLess(raw_profile["next_1d_close_return"], -0.4)

        _attach_adjusted(rows)
        adjusted_target, adjusted_profile = trainer._build_short_horizon_target_profile(
            symbol_rows=rows, index=index, anchor_close=trainer._label_price(rows[index], "close"), limit_band_pct=10.0
        )
        # Adjusted basis sees the true (flat) move.
        self.assertAlmostEqual(0.0, adjusted_profile["next_1d_close_return"], places=9)
        self.assertNotEqual(raw_target, adjusted_target)
