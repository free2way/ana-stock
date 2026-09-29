from datetime import datetime, timezone
from unittest import TestCase

from app.services.stock_selection.data_contracts import (
    SourceDataContract, audit_source_data_contracts, candidate_eligibility,
    feature_known_at_cutoff, source_data_contracts, validate_market_ticker,
)


class DataContractsP0Tests(TestCase):
    def test_report_period_alone_is_not_point_in_time_availability(self):
        cutoff = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)
        legacy = SourceDataContract("CN", "legacy_wide", "report_date", None, "ingested_time", False, False, "not_price")
        row = {"market": "CN", "report_date": "2026-06-30", "ingested_time": "2026-09-10T12:00:00+00:00"}
        self.assertFalse(feature_known_at_cutoff(row, contract=legacy, cutoff=cutoff))
        pit = SourceDataContract("CN", "append_only", "event_time", "available_time", "ingested_time", True, True, "not_price")
        row["available_time"] = "2026-09-11T09:00:00+00:00"
        self.assertFalse(feature_known_at_cutoff(row, contract=pit, cutoff=cutoff))
        row["available_time"] = "2026-09-10T09:00:00+00:00"
        row["ingested_time"] = "2026-09-11T09:00:00+00:00"
        self.assertFalse(feature_known_at_cutoff(row, contract=pit, cutoff=cutoff))
        row["ingested_time"] = "2026-09-11T07:00:00+00:00"
        self.assertTrue(feature_known_at_cutoff(row, contract=pit, cutoff=cutoff))

    def test_graded_market_does_not_override_suspended_top_candidate(self):
        top = candidate_eligibility(market="CN", ticker="600000.SS", market_health="graded", symbol_status="suspended")
        other = candidate_eligibility(market="CN", ticker="000001.SZ", market_health="graded", symbol_status="ready")
        self.assertFalse(top["eligible"])
        self.assertEqual("symbol_not_tradable", top["reason"])
        self.assertTrue(other["eligible"])
        self.assertEqual("symbol_quote_unknown", candidate_eligibility(
            market="CN", ticker="600000.SS", market_health="graded", symbol_status="missing_quote",
        )["reason"])

    def test_market_ticker_cannot_cross_cn_us_hk(self):
        for market, ticker in (("CN", "AAPL"), ("CN", "0700.HK"),
                               ("US", "600000.SS"), ("HK", "AAPL")):
            with self.assertRaises(ValueError):
                validate_market_ticker(market, ticker)
        self.assertEqual("0700.HK", validate_market_ticker("HK", "0700.hk"))
        self.assertEqual("600000.SS", validate_market_ticker("CN", "600000.SH"))

    def test_registered_sources_expose_market_specific_pit_blockers(self):
        cn = {item.source: item for item in source_data_contracts(market="CN")}
        self.assertIn("community_eastmoney", cn)
        self.assertEqual("Asia/Shanghai", cn["community_eastmoney"].timezone_name)
        self.assertIn(
            "revision_history_not_preserved",
            cn["community_eastmoney"].blockers,
        )
        self.assertFalse(cn["community_eastmoney"].permits_historical_pit)
        with self.assertRaises(ValueError):
            source_data_contracts(market="EU")

    def test_source_contract_audit_blocks_unknown_and_backdated_rows(self):
        report = audit_source_data_contracts(
            (
                {
                    "market": "CN",
                    "source": "community_eastmoney",
                    "row_count": 20,
                    "suspected_backdated_ingestion_count": 12,
                },
                {"market": "US", "source": "unregistered_fixture", "row_count": 2},
            )
        )
        self.assertEqual("BLOCKED", report["verdict"])
        by_key = {
            (item["market"], item["source"]): item for item in report["entries"]
        }
        self.assertIn(
            "suspected_backdated_ingestion",
            by_key[("CN", "community_eastmoney")]["blockers"],
        )
        self.assertEqual(
            ["source_contract_missing"],
            by_key[("US", "unregistered_fixture")]["blockers"],
        )
