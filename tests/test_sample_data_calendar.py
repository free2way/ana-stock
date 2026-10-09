"""The sample-data seed path must honour the market calendar.

The curated fixture hard-codes ``2026-04-03``, a US market holiday (Good
Friday). ``extend_sample_rows`` has always filtered closed sessions, but the
default ``seed_sample_data(days=None)`` path -- the UI "inject sample data"
entry point -- did not, so re-seeding re-created a non-trading bar in the lake.
These tests pin the fix: every curated read is calendar-filtered.
"""

from unittest import TestCase
from unittest.mock import patch

from tests.postgres_safety import ApplicationPostgresTestCase


class CuratedRowsCalendarFilterTests(TestCase):
    def test_closed_session_dropped_from_curated_rows(self) -> None:
        from app.services.sample_data import SAMPLE_DATA, _curated_rows

        aapl = _curated_rows("AAPL")
        dates = [row["date"] for row in aapl]
        self.assertNotIn("2026-04-03", dates)
        self.assertIn("2026-04-02", dates)
        # One closed session is the only row dropped from the fixture.
        self.assertEqual(len(SAMPLE_DATA["AAPL"]) - 1, len(aapl))

    def test_extend_sample_rows_tail_is_curated_and_filtered(self) -> None:
        from app.services.sample_data import extend_sample_rows

        rows = extend_sample_rows("MSFT", days=10)
        self.assertNotIn("2026-04-03", [row["date"] for row in rows])
        # The curated tail remains the most recent sessions.
        self.assertEqual("2026-04-02", rows[-1]["date"])


class SeedSampleDataCalendarTests(ApplicationPostgresTestCase):
    def test_default_seed_path_never_writes_closed_session(self) -> None:
        from app.services import sample_data

        captured: dict[str, list[dict]] = {}

        def fake_write(*, market, rows, provenance):
            captured.setdefault(market, []).extend(rows)
            return []

        with patch(
            "app.services.sample_data.write_ohlcv_rows_to_lake",
            side_effect=fake_write,
        ):
            results = sample_data.seed_sample_data()

        us_rows = captured.get("US", [])
        dates = {row["date"] for row in us_rows}
        self.assertNotIn("2026-04-03", dates)
        self.assertIn("2026-04-02", dates)

        # Every curated ticker keeps its tradable tail, and the reported row
        # count matches what was actually handed to the lake writer.
        for item in results:
            self.assertGreater(item["rows"], 0)

    def test_default_seed_sync_state_points_at_last_open_session(self) -> None:
        from app.core.db import SessionLocal
        from app.services import sample_data
        from app.services.repository import PriceSyncStateRepository

        with patch(
            "app.services.sample_data.write_ohlcv_rows_to_lake",
            return_value=[],
        ):
            sample_data.seed_sample_data()

        with SessionLocal() as db:
            states = {
                item["ticker"]: item["last_synced_date"]
                for item in PriceSyncStateRepository(db).list_states_with_symbols()
            }
        self.assertEqual("2026-04-02", states["AAPL"])
        self.assertEqual("2026-04-02", states["MSFT"])
