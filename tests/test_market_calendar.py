from __future__ import annotations

from unittest import TestCase

from app.services.market_calendar import (
    calendar_source,
    is_market_open_date,
    is_provisional,
    market_session_status,
    next_market_open_date,
    normalize_market,
    previous_market_open_date,
)


class CNCalendarTests(TestCase):
    """S-9: local CN table (official 2025/2026, provisional 2027) + XSHG cross-check."""

    def test_chunjie_2025_block_is_closed_and_afterwards_open(self) -> None:
        for day in ["2025-01-28", "2025-01-30", "2025-02-03", "2025-02-04"]:
            self.assertFalse(is_market_open_date("CN", day), day)
        self.assertTrue(is_market_open_date("CN", "2025-02-05"))
        self.assertEqual("2025-02-05", next_market_open_date("CN", "2025-01-28"))

    def test_national_day_2025_block(self) -> None:
        for day in ["2025-10-01", "2025-10-06", "2025-10-08"]:
            self.assertFalse(is_market_open_date("CN", day), day)
        self.assertTrue(is_market_open_date("CN", "2025-10-09"))

    def test_2025_local_table_matches_xshg_calendar(self) -> None:
        from datetime import date, timedelta

        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        mismatches = []
        day = date(2025, 1, 1)
        while day <= date(2025, 12, 31):
            expected = bool(calendar.is_session(day))
            actual = is_market_open_date("CN", day)
            if expected != actual:
                mismatches.append((day.isoformat(), expected, actual))
            day += timedelta(days=1)
        self.assertEqual([], mismatches)

    def test_2026_official_blocks(self) -> None:
        closed = [
            "2026-01-01", "2026-01-02",
            "2026-02-16", "2026-02-20", "2026-02-23",
            "2026-04-06",
            "2026-05-01", "2026-05-05",
            "2026-06-19",
            "2026-09-25",
            "2026-10-01", "2026-10-07",
        ]
        for day in closed:
            self.assertFalse(is_market_open_date("CN", day), day)
        opened = ["2026-01-05", "2026-02-24", "2026-04-07", "2026-05-06", "2026-06-22", "2026-09-28", "2026-10-08"]
        for day in opened:
            self.assertTrue(is_market_open_date("CN", day), day)
        status = market_session_status("CN", "2026-10-08")
        self.assertEqual("local:CN_2026_official", status["calendar_source"])

    def test_2027_table_is_provisional_and_covers_statutory_days(self) -> None:
        self.assertTrue(is_provisional("CN", "2027-01-01"))
        # statutory closures that fall on weekdays
        for day in ["2027-01-01", "2027-02-05", "2027-02-08", "2027-04-05", "2027-06-09", "2027-09-15", "2027-10-01"]:
            self.assertFalse(is_market_open_date("CN", day), day)
            self.assertTrue(is_provisional("CN", day), day)
        self.assertTrue(is_market_open_date("CN", "2027-01-04"))
        self.assertEqual("local:CN_2027_provisional", calendar_source("CN", "2027-03-01"))

    def test_weekend_is_closed_everywhere(self) -> None:
        self.assertFalse(is_market_open_date("CN", "2026-10-10"))  # Saturday
        self.assertFalse(is_market_open_date("US", "2026-10-10"))
        self.assertFalse(is_market_open_date("HK", "2026-10-10"))


class USCalendarTests(TestCase):
    def test_holidays_and_early_closes(self) -> None:
        for day in ["2025-11-27", "2025-12-25", "2026-04-03", "2026-11-26"]:
            self.assertFalse(is_market_open_date("US", day), day)
        self.assertTrue(is_market_open_date("US", "2026-11-27"))
        status = market_session_status("US", "2026-11-27")
        self.assertTrue(status["is_open"])
        self.assertEqual("early_close", status["reason"])
        self.assertEqual("13:00", status["early_close"])

    def test_next_open_crosses_holiday(self) -> None:
        self.assertEqual("2026-04-06", next_market_open_date("US", "2026-04-03"))
        self.assertEqual("2026-04-02", previous_market_open_date("US", "2026-04-03", include_self=False))

    def test_source_reports_library(self) -> None:
        self.assertEqual("exchange_calendars:XNYS", calendar_source("US", "2026-06-01"))


class HKCalendarTests(TestCase):
    def test_lunar_new_year_and_handover(self) -> None:
        for day in ["2026-02-17", "2026-02-18", "2026-02-19", "2026-07-01", "2026-10-01"]:
            self.assertFalse(is_market_open_date("HK", day), day)
        self.assertTrue(is_market_open_date("HK", "2026-02-16"))
        self.assertTrue(is_market_open_date("HK", "2026-02-20"))

    def test_christmas_eve_half_day(self) -> None:
        status = market_session_status("HK", "2026-12-24")
        self.assertTrue(status["is_open"])
        self.assertEqual("early_close", status["reason"])
        self.assertEqual("12:00", status["early_close"])
        self.assertEqual("Asia/Hong_Kong", status["timezone"])
        self.assertIsNone(status["session_break"])  # half days run straight to noon

    def test_hk_normal_day_has_lunch_break(self) -> None:
        status = market_session_status("HK", "2026-12-23")
        self.assertTrue(status["is_open"])
        self.assertEqual("regular_session", status["reason"])
        self.assertEqual("16:00", status["regular_close"])
        self.assertEqual("12:00-13:00", status["session_break"])

    def test_hk_market_normalization_and_next_open(self) -> None:
        self.assertEqual("HK", normalize_market("港股"))
        self.assertEqual("2026-02-20", next_market_open_date("HK", "2026-02-19"))
        self.assertEqual("exchange_calendars:XHKG", calendar_source("HK", "2027-03-01"))
