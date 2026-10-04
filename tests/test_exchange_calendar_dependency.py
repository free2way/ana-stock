from __future__ import annotations

import warnings
from datetime import date
from unittest import TestCase

import exchange_calendars as xc
import pandas as pd
from exchange_calendars.errors import DateOutOfBounds

warnings.filterwarnings("ignore")


class ExchangeCalendarDependencyTests(TestCase):
    def test_dependency_is_pinned(self) -> None:
        self.assertEqual("4.11.1", getattr(xc, "__version__", None))

    def test_xnys_covers_2026_and_2027_market_holidays(self) -> None:
        calendar = xc.get_calendar("XNYS")
        for closed in ("2026-01-01", "2026-07-03", "2026-11-26", "2026-12-25", "2027-01-01"):
            self.assertFalse(calendar.is_session(pd.Timestamp(closed)), msg=closed)
        for open_day in ("2026-01-02", "2026-07-06", "2027-01-04"):
            self.assertTrue(calendar.is_session(pd.Timestamp(open_day)), msg=open_day)
        self.assertGreaterEqual(calendar.last_session.date(), date(2027, 6, 30))

    def test_xhkg_covers_2026(self) -> None:
        calendar = xc.get_calendar("XHKG")
        self.assertFalse(calendar.is_session(pd.Timestamp("2026-01-01")))
        self.assertFalse(calendar.is_session(pd.Timestamp("2026-02-17")))
        self.assertTrue(calendar.is_session(pd.Timestamp("2026-01-02")))
        self.assertGreaterEqual(calendar.last_session.date(), date(2026, 12, 31))

    def test_xshg_data_boundary_is_explicit(self) -> None:
        """XSHG holiday data ends in 2025 in 4.11.1.

        The CN calendar therefore needs a versioned local override table; this
        test pins the boundary so an upgrade cannot silently change the
        assumption.
        """

        calendar = xc.get_calendar("XSHG")
        self.assertEqual(date(2025, 12, 31), calendar.last_session.date())
        self.assertTrue(calendar.is_session(pd.Timestamp("2025-12-31")))
        with self.assertRaises(DateOutOfBounds):
            calendar.is_session(pd.Timestamp("2026-01-05"))
