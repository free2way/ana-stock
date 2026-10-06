from __future__ import annotations

import json
from unittest import TestCase

from app.services.providers.fundamental import GlobalStockDataSECFundamentalProvider
from app.services.stock_selection.feature_availability import adapt_fundamental_snapshots
from app.services.trainer import SignalTrainer, group_point_in_time_fundamental_history


class FundamentalAvailabilityTests(TestCase):
    def test_grouping_pivots_features_and_uses_last_available_time(self) -> None:
        records = [
            {
                "ticker": "600000.SS",
                "feature_name": "roe_avg_3y",
                "feature_value": 12.5,
                "available_time": "2026-04-28T16:00:00+08:00",
                "ingested_time": "2026-04-29T09:00:00+08:00",
                "source": "community_cn",
                "payload_json": json.dumps({"report_date": "2026-03-31"}),
            },
            {
                "ticker": "600000.SS",
                "feature_name": "net_profit_yoy",
                "feature_value": 25.0,
                "available_time": "2026-04-29T10:00:00+08:00",
                "ingested_time": "2026-04-29T10:05:00+08:00",
                "source": "community_cn",
                "payload_json": json.dumps({"report_date": "2026-03-31"}),
            },
            {
                # No availability timestamp: must never enter training history.
                "ticker": "600000.SS",
                "feature_name": "revenue_yoy",
                "feature_value": 9.9,
                "available_time": "",
                "payload_json": json.dumps({"report_date": "2026-06-30"}),
            },
            {
                "ticker": "000001.SZ",
                "feature_name": "roe_avg_3y",
                "feature_value": 8.0,
                "available_time": "2026-04-27T16:00:00+08:00",
                "payload_json": json.dumps({"report_date": "2026-03-31"}),
            },
        ]
        history = group_point_in_time_fundamental_history(records)
        self.assertEqual({"600000.SS", "000001.SZ"}, set(history))

        items = history["600000.SS"]
        self.assertEqual(1, len(items))
        self.assertEqual("2026-03-31", items[0]["report_date"])
        # The group is only fully known once its last feature was published.
        self.assertEqual("2026-04-29T10:00:00+08:00", items[0]["available_time"])
        self.assertAlmostEqual(12.5, items[0]["roe_avg_3y"])
        self.assertAlmostEqual(25.0, items[0]["net_profit_yoy"])
        self.assertNotIn("revenue_yoy", items[0])

    def test_cursor_waits_for_publication_not_period_end(self) -> None:
        trainer = SignalTrainer()
        history = [
            {
                "report_date": "2026-03-31",
                "available_time": "2026-04-28T16:00:00+08:00",
                "roe_avg_3y": 12.5,
            },
            {
                "report_date": "2026-06-30",
                "available_time": "2026-08-30T16:00:00+08:00",
                "roe_avg_3y": 13.1,
            },
        ]

        # Fiscal period has ended, but the report is not published: unusable.
        cursor, active = trainer._advance_fundamental_cursor(history=history, cursor=0, trade_date="2026-04-01")
        self.assertEqual(0, cursor)
        self.assertIsNone(active)

        # Publication day: usable from that date onward.
        cursor, active = trainer._advance_fundamental_cursor(history=history, cursor=0, trade_date="2026-04-28")
        self.assertEqual(1, cursor)
        self.assertAlmostEqual(12.5, active["roe_avg_3y"])

        # Between publications the previous report stays active.
        cursor, active = trainer._advance_fundamental_cursor(history=history, cursor=1, trade_date="2026-07-15")
        self.assertEqual(1, cursor)
        self.assertAlmostEqual(12.5, active["roe_avg_3y"])

        cursor, active = trainer._advance_fundamental_cursor(history=history, cursor=1, trade_date="2026-08-31")
        self.assertEqual(2, cursor)
        self.assertAlmostEqual(13.1, active["roe_avg_3y"])

    def test_rows_without_availability_are_skipped_not_backfilled(self) -> None:
        trainer = SignalTrainer()
        history = [
            {"report_date": "2026-03-31", "roe_avg_3y": 1.0},
            {
                "report_date": "2026-06-30",
                "available_time": "2026-08-30T16:00:00+08:00",
                "roe_avg_3y": 2.0,
            },
        ]
        cursor, active = trainer._advance_fundamental_cursor(history=history, cursor=0, trade_date="2026-09-01")
        self.assertEqual(2, cursor)
        self.assertAlmostEqual(2.0, active["roe_avg_3y"])

    def test_sec_filing_date_bounds_training_sample_availability(self) -> None:
        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": {"units": {"USD": [
                        {"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 120},
                        {"form": "10-K", "fp": "FY", "end": "2024-12-31", "filed": "2025-02-01", "val": 100},
                    ]}},
                    "NetIncomeLoss": {"units": {"USD": [
                        {"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 24},
                        {"form": "10-K", "fp": "FY", "end": "2024-12-31", "filed": "2025-02-01", "val": 20},
                    ]}},
                    "Assets": {"units": {"USD": [{"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 200}]}},
                    "Liabilities": {"units": {"USD": [{"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 80}]}},
                }
            }
        }
        snapshot = GlobalStockDataSECFundamentalProvider.__new__(GlobalStockDataSECFundamentalProvider)._facts_to_snapshot(
            ticker="ACME", cik=7, company_name="Acme Inc.", facts=facts
        )
        adapted = adapt_fundamental_snapshots(
            (
                {
                    **snapshot,
                    "source": "global_stock_data_sec_edgar",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                },
            ),
            market="US",
        )
        history = group_point_in_time_fundamental_history(
            [
                {
                    "ticker": record.ticker,
                    "feature_name": record.feature_name,
                    "feature_value": record.value,
                    "available_time": record.available_time.isoformat(),
                    "ingested_time": record.ingested_time.isoformat(),
                    "source": record.source,
                    "payload_json": json.dumps({"report_date": snapshot["report_date"]}),
                }
                for record in adapted.records
            ]
        )["ACME"]
        trainer = SignalTrainer()

        # Decision before the filing date: the report is not usable yet.
        cursor, active = trainer._advance_fundamental_cursor(
            history=history, cursor=0, trade_date="2026-01-31"
        )
        self.assertEqual(0, cursor)
        self.assertIsNone(active)

        # On the filing date the report becomes usable.
        cursor, active = trainer._advance_fundamental_cursor(
            history=history, cursor=0, trade_date="2026-02-01"
        )
        self.assertEqual(1, cursor)
        self.assertAlmostEqual(20.0, active["revenue_yoy"])

