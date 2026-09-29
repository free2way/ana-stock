from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.us_trade_universe import USTradeUniverseConfig, build_us_trade_universe


class USTradeUniverseTests(TestCase):
    def test_excludes_symbols_missing_from_required_latest_partition(self) -> None:
        config = USTradeUniverseConfig(
            min_price=3.0,
            min_avg_dollar_volume=2_000_000.0,
            min_avg_volume=200_000.0,
            min_history_days=10,
        )
        overview = {
            "FRESH": {"name": "Fresh Corporation"},
            "STALE": {"name": "Stale Corporation"},
        }
        metrics = {
            "FRESH": {
                "latest_trade_date": "2026-09-04",
                "latest_close": 20.0,
                "avg_volume": 500_000.0,
                "avg_dollar_volume": 10_000_000.0,
                "history_days": 60,
                "duplicate_conflict_days": 0,
            },
            "STALE": {
                "latest_trade_date": "2026-05-22",
                "latest_close": 20.0,
                "avg_volume": 500_000.0,
                "avg_dollar_volume": 10_000_000.0,
                "history_days": 60,
                "duplicate_conflict_days": 0,
            },
        }
        context = MagicMock()
        context.__enter__.return_value = MagicMock()
        context.__exit__.return_value = False

        with patch(
            "app.services.us_trade_universe.SessionLocal",
            return_value=context,
        ), patch(
            "app.services.us_trade_universe.SymbolRepository"
        ) as repository, patch(
            "app.services.us_trade_universe.load_lake_latest_metrics",
            return_value=metrics,
        ):
            repository.return_value.list_overviews_for_tickers.return_value = overview
            universe, summary = build_us_trade_universe(
                tickers=["FRESH", "STALE"],
                config=config,
                expected_as_of_date="2026-09-04",
                include_summary=True,
            )

        self.assertEqual(["FRESH"], universe)
        self.assertEqual(1, summary["reasons"]["stale_lake_symbol"])
        self.assertEqual(
            "2026-09-04",
            summary["thresholds"]["required_latest_trade_date"],
        )
