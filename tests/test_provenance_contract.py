from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from app.services.market_lake import _with_provenance

ROOT = Path(__file__).resolve().parents[1]

WRITER_FILES = (
    "app/services/market_sync.py",
    "app/services/us_market_universe.py",
    "app/services/cn_market_universe.py",
    "app/services/hithink_market_data.py",
    "app/services/stock_selection/history_backfill.py",
    "app/services/sample_data.py",
    "scripts/backfill_cn_lake_gaps.py",
)


class ProvenanceContractTests(TestCase):
    """A3: placeholder provenance fails closed; live writers pass real sources."""

    def _row(self, **overrides) -> dict:
        row = {
            "date": "2026-10-01",
            "symbol": "000001.SZ",
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100.0,
        }
        row.update(overrides)
        return row

    def test_missing_provider_or_reference_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider is required"):
            _with_provenance(self._row(), market_code="CN", batch_id=None)
        with self.assertRaisesRegex(ValueError, "provider is required"):
            _with_provenance(self._row(provider="unknown"), market_code="CN", batch_id=None)
        with self.assertRaisesRegex(ValueError, "source_reference is required"):
            _with_provenance(self._row(provider="tushare"), market_code="CN", batch_id=None)
        with self.assertRaisesRegex(ValueError, "source_reference is required"):
            _with_provenance(self._row(provider="tushare", source_reference="unspecified"), market_code="CN", batch_id=None)

    def test_real_provenance_passes_and_legacy_mode_is_explicit(self) -> None:
        enriched = _with_provenance(
            self._row(provider="tushare", source_reference="tushare:pro.daily"),
            market_code="CN",
            batch_id="b1",
        )
        self.assertEqual("tushare", enriched["provider"])
        self.assertEqual("tushare:pro.daily", enriched["source_reference"])

        legacy = _with_provenance(self._row(), market_code="CN", batch_id=None, allow_legacy_defaults=True)
        self.assertEqual("unknown", legacy["provider"])
        self.assertEqual("unspecified", legacy["source_reference"])

    def test_live_writers_pass_real_provenance(self) -> None:
        for relative in WRITER_FILES:
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn('"provider"', text, relative)
            self.assertIn("source_reference", text, relative)

    def test_legacy_backfill_opts_in_explicitly(self) -> None:
        text = (ROOT / "scripts" / "backfill_lake_v2_provenance.py").read_text(encoding="utf-8")
        self.assertIn("allow_legacy_defaults=True", text)
