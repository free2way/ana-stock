from __future__ import annotations

from datetime import date, datetime, timezone
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo

from app.services.global_fundamentals import GlobalFundamentalRow
from app.services.providers.fundamental import GlobalStockDataSECFundamentalProvider
from app.services.stock_selection.feature_availability import (
    FeatureAvailabilityConfig,
    PointInTimeFeatureRecord,
    adapt_fundamental_snapshots,
    adapt_point_in_time_feature_snapshots,
    assess_feature_availability,
    persist_feature_availability_report,
    select_feature_records_as_of,
)
from app.services.repository import PointInTimeFeatureSnapshotRepository


def _sec_company_facts(*, filed: str | None) -> dict:
    """Minimal two-year EDGAR companyfacts payload for the SEC provider."""

    def annual(end: str, value: float, item_filed: str | None) -> dict:
        item: dict = {"form": "10-K", "fp": "FY", "end": end, "val": value}
        if item_filed is not None:
            item["filed"] = item_filed
        return item

    return {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    annual("2025-12-31", 120, filed),
                    annual("2024-12-31", 100, filed or "2025-02-01"),
                ]}},
                "NetIncomeLoss": {"units": {"USD": [
                    annual("2025-12-31", 24, filed),
                    annual("2024-12-31", 20, filed or "2025-02-01"),
                ]}},
                "Assets": {"units": {"USD": [annual("2025-12-31", 200, filed)]}},
                "Liabilities": {"units": {"USD": [annual("2025-12-31", 80, filed)]}},
            }
        }
    }


class StockSelectionFeatureAvailabilityTests(TestCase):
    def test_append_only_adapter_reads_long_form_feature_rows(self) -> None:
        result = adapt_point_in_time_feature_snapshots(
            (
                {
                    "id": 7,
                    "ticker": "000001.SZ",
                    "market": "CN",
                    "feature_name": "pe_ttm",
                    "feature_value": 5.1,
                    "event_time": "2026-08-14T00:00:00+08:00",
                    "available_time": "2026-08-14T16:30:00+08:00",
                    "ingested_time": "2026-08-14T16:30:00+08:00",
                    "source": "community_eastmoney",
                    "revision_id": "revision-1",
                    "payload_json": json.dumps({"revision_history_preserved": False}),
                },
            ),
            market="CN",
            feature_names=("pe_ttm",),
        )

        self.assertEqual(1, result.source_row_count)
        self.assertEqual(1, result.emitted_value_count)
        self.assertEqual("pe_ttm", result.records[0].feature_name)
        self.assertEqual(5.1, result.records[0].value)
        self.assertFalse(result.revision_history_preserved)

    def test_adapter_honors_source_and_feature_specific_availability(self) -> None:
        result = adapt_fundamental_snapshots(
            [
                {
                    "ticker": "600519.SS",
                    "source": "community_eastmoney",
                    "report_date": "2026-06-30",
                    "available_time": "2026-08-11T23:59:59+08:00",
                    "ingested_time": "2026-08-11T23:59:59+08:00",
                    "created_at": "2026-08-15T10:00:00+08:00",
                    "updated_at": "2026-08-15T10:00:00+08:00",
                    "revision_id": "source-revision-1",
                    "roe_avg_3y": 27.0,
                    "pe_ttm": 20.5,
                    "feature_times": {
                        "pe_ttm": {
                            "event_time": "2026-08-14T00:00:00+08:00",
                            "available_time": "2026-08-14T16:30:00+08:00",
                            "ingested_time": "2026-08-14T16:30:00+08:00",
                        }
                    },
                }
            ],
            market="CN",
            feature_names=("roe_avg_3y", "pe_ttm"),
        )

        by_name = {record.feature_name: record for record in result.records}
        self.assertEqual("source-revision-1", by_name["roe_avg_3y"].revision_id)
        self.assertEqual(30, by_name["roe_avg_3y"].event_time.day)
        self.assertEqual(11, by_name["roe_avg_3y"].available_time.day)
        self.assertEqual(14, by_name["pe_ttm"].event_time.day)
        self.assertEqual(16, by_name["pe_ttm"].available_time.hour)

    @staticmethod
    def _record(
        ticker: str,
        *,
        value: float,
        available_day: int,
        revision_id: str = "r1",
    ) -> PointInTimeFeatureRecord:
        return PointInTimeFeatureRecord(
            record_id=f"{ticker}:{revision_id}",
            market="CN",
            ticker=ticker,
            feature_name="quality",
            value=value,
            event_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            available_time=datetime(2026, 1, available_day, tzinfo=timezone.utc),
            ingested_time=datetime(2026, 1, available_day, 12, tzinfo=timezone.utc),
            source="fixture",
            revision_id=revision_id,
        )

    def test_as_of_selection_waits_for_knowledge_time_and_revision(self) -> None:
        original = self._record("A", value=1.0, available_day=2)
        revision = self._record("A", value=2.0, available_day=4, revision_id="r2")

        before_ingestion = select_feature_records_as_of(
            (original, revision),
            cutoff=datetime(2026, 1, 2, 6, tzinfo=timezone.utc),
        )
        after_original = select_feature_records_as_of(
            (original, revision),
            cutoff=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        after_revision = select_feature_records_as_of(
            (original, revision),
            cutoff=datetime(2026, 1, 5, tzinfo=timezone.utc),
        )

        self.assertEqual({}, before_ingestion)
        self.assertEqual(1.0, after_original[("A", "quality")].value)
        self.assertEqual(2.0, after_revision[("A", "quality")].value)

    def test_readiness_requires_cross_section_dates_and_revision_history(self) -> None:
        universe = {
            date(2026, 1, 3): ("A", "B"),
            date(2026, 1, 4): ("A", "B"),
        }
        records = (
            self._record("A", value=1.0, available_day=2),
            self._record("B", value=2.0, available_day=2),
        )
        config = FeatureAvailabilityConfig(
            market="CN",
            required_features=("quality",),
            minimum_cross_section_coverage=1.0,
            minimum_date_coverage=1.0,
        )
        passing = assess_feature_availability(
            records,
            universe_by_date=universe,
            source_row_count=2,
            rejected_row_count=0,
            emitted_value_count=2,
            revision_history_preserved=True,
            config=config,
        )
        blocked = assess_feature_availability(
            records[:1],
            universe_by_date=universe,
            source_row_count=2,
            rejected_row_count=0,
            emitted_value_count=1,
            revision_history_preserved=False,
            config=config,
        )

        self.assertEqual("PASS", passing.verdict)
        self.assertEqual(1.0, passing.feature_coverage[0].pair_coverage)
        self.assertEqual("FAIL", blocked.verdict)
        self.assertIn("insufficient_pair_coverage:quality", blocked.blockers)
        self.assertIn("revision_history_not_preserved", blocked.blockers)

    def test_fundamental_adapter_uses_latest_update_as_availability(self) -> None:
        result = adapt_fundamental_snapshots(
            (
                {
                    "ticker": "600000.SS",
                    "source": "fixture",
                    "report_date": "2026-01-01",
                    "created_at": "2026-01-02T00:00:00+00:00",
                    "updated_at": "2026-01-03T00:00:00+00:00",
                    "roe_avg_3y": 12.5,
                },
                {
                    "ticker": "600001.SS",
                    "source": "fixture",
                    "report_date": "2026-01-01",
                    "roe_avg_3y": 8.0,
                },
            ),
            market="CN",
            feature_names=("roe_avg_3y",),
        )

        self.assertEqual(2, result.source_row_count)
        self.assertEqual(1, result.rejected_row_count)
        self.assertEqual(1, result.emitted_value_count)
        self.assertEqual(
            datetime(2026, 1, 3, tzinfo=timezone.utc),
            result.records[0].available_time,
        )
        self.assertFalse(result.revision_history_preserved)

    def test_evidence_is_content_addressed_and_idempotent(self) -> None:
        report = assess_feature_availability(
            (self._record("A", value=1.0, available_day=2),),
            universe_by_date={date(2026, 1, 3): ("A",)},
            source_row_count=1,
            rejected_row_count=0,
            emitted_value_count=1,
            revision_history_preserved=True,
            config=FeatureAvailabilityConfig(
                market="CN",
                required_features=("quality",),
                minimum_cross_section_coverage=1.0,
                minimum_date_coverage=1.0,
            ),
        )
        with TemporaryDirectory() as name:
            first = persist_feature_availability_report(
                report,
                source_version="fixture-v1",
                root=Path(name),
            )
            second = persist_feature_availability_report(
                report,
                source_version="fixture-v1",
                root=Path(name),
            )
        self.assertFalse(first.reused_existing)
        self.assertTrue(second.reused_existing)
        self.assertEqual(first.evidence_version, second.evidence_version)

    def test_append_only_repository_validates_and_adds_without_mutation(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.added = []

            def scalar(self, _statement):
                return None

            def add(self, value) -> None:
                self.added.append(value)

            def flush(self) -> None:
                return None

        session = FakeSession()
        repository = PointInTimeFeatureSnapshotRepository(session)  # type: ignore[arg-type]
        snapshot, inserted = repository.append_snapshot(
            symbol_id=1,
            feature_name="quality",
            feature_value=1.25,
            event_time="2026-01-01T00:00:00+08:00",
            available_time="2026-01-02T16:00:00+08:00",
            ingested_time="2026-01-02T16:01:00+08:00",
            source="fixture",
            source_record_id="source-1",
            revision_id="revision-1",
            commit=False,
        )

        self.assertTrue(inserted)
        self.assertEqual([snapshot], session.added)
        self.assertEqual("quality", snapshot.feature_name)
        self.assertEqual("2025-12-31T16:00:00+00:00", snapshot.event_time)
        self.assertEqual("2026-01-02T08:00:00+00:00", snapshot.available_time)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            repository.append_snapshot(
                symbol_id=1,
                feature_name="quality",
                feature_value=1.25,
                event_time="2026-01-01T00:00:00",
                available_time="2026-01-02T16:00:00+08:00",
                ingested_time="2026-01-02T16:01:00+08:00",
                source="fixture",
                source_record_id="source-2",
                revision_id="revision-1",
                commit=False,
            )

    def test_sec_edgar_filing_date_sets_available_time(self) -> None:
        provider = GlobalStockDataSECFundamentalProvider.__new__(GlobalStockDataSECFundamentalProvider)
        snapshot = provider._facts_to_snapshot(
            ticker="ACME",
            cik=7,
            company_name="Acme Inc.",
            facts=_sec_company_facts(filed="2026-02-01"),
        )
        result = adapt_fundamental_snapshots(
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

        by_name = {record.feature_name: record for record in result.records}
        self.assertEqual({"net_profit_yoy", "revenue_yoy", "debt_to_assets"}, set(by_name))
        expected_available = datetime(
            2026, 2, 1, 23, 59, 59, 999999, tzinfo=ZoneInfo("America/New_York")
        )
        for record in by_name.values():
            # Availability is the filing date (end of the U.S. Eastern day),
            # not the local sync time.
            self.assertEqual(expected_available, record.available_time)
            self.assertNotEqual(datetime(2026, 6, 1, tzinfo=timezone.utc), record.available_time)
            # Ingestion is never backdated onto the historical filing date.
            self.assertEqual(datetime(2026, 6, 1, tzinfo=timezone.utc), record.ingested_time)

    def test_sec_edgar_without_filing_date_falls_back_to_sync_time(self) -> None:
        provider = GlobalStockDataSECFundamentalProvider.__new__(GlobalStockDataSECFundamentalProvider)
        snapshot = provider._facts_to_snapshot(
            ticker="ACME",
            cik=7,
            company_name="Acme Inc.",
            facts=_sec_company_facts(filed=None),
        )
        result = adapt_fundamental_snapshots(
            (
                {
                    **snapshot,
                    "source": "global_stock_data_sec_edgar",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:00+00:00",
                },
            ),
            market="US",
        )

        record = {item.feature_name: item for item in result.records}["revenue_yoy"]
        # Without a filing date the record keeps the legacy conservative
        # fallback: availability is the latest local ingestion timestamp.
        self.assertEqual(datetime(2026, 6, 2, tzinfo=timezone.utc), record.available_time)
        self.assertEqual(datetime(2026, 6, 2, tzinfo=timezone.utc), record.ingested_time)

    def test_filing_date_point_in_time_boundary(self) -> None:
        filed_eod = datetime(
            2026, 2, 1, 23, 59, 59, 999999, tzinfo=ZoneInfo("America/New_York")
        )
        record = PointInTimeFeatureRecord(
            record_id="fundamental:US:ACME:global_stock_data_sec_edgar:2025-12-31:revenue_yoy:rev",
            market="US",
            ticker="ACME",
            feature_name="revenue_yoy",
            value=20.0,
            event_time=datetime(2025, 12, 31, tzinfo=ZoneInfo("America/New_York")),
            available_time=filed_eod,
            ingested_time=filed_eod,
            source="global_stock_data_sec_edgar",
            revision_id="rev",
        )

        # A decision earlier on the filing day cannot see the filing yet.
        before_filing = select_feature_records_as_of(
            (record,),
            cutoff=datetime(2026, 2, 1, 12, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        after_filing = select_feature_records_as_of(
            (record,),
            cutoff=datetime(2026, 2, 2, 0, 0, tzinfo=ZoneInfo("America/New_York")),
        )

        self.assertEqual({}, before_filing)
        self.assertEqual(20.0, after_filing[("ACME", "revenue_yoy")].value)

    def test_sec_feature_times_survive_global_row_transmission(self) -> None:
        provider = GlobalStockDataSECFundamentalProvider.__new__(GlobalStockDataSECFundamentalProvider)
        snapshot = provider._facts_to_snapshot(
            ticker="ACME",
            cik=7,
            company_name="Acme Inc.",
            facts=_sec_company_facts(filed="2026-02-01"),
        )
        # Mirror ``sync_global_fundamentals``: the snapshot is narrowed onto the
        # wide row before it reaches the point-in-time adapter.  This is the
        # exact hop that previously dropped ``feature_times``.
        row = GlobalFundamentalRow(
            ticker="ACME",
            market="US",
            report_date=snapshot["report_date"],
            source="global_stock_data_sec_edgar",
            available_time=snapshot.get("available_time"),
            feature_times=snapshot.get("feature_times"),
            name=snapshot.get("name"),
            net_profit_yoy=snapshot.get("net_profit_yoy"),
            revenue_yoy=snapshot.get("revenue_yoy"),
            debt_to_assets=snapshot.get("debt_to_assets"),
        )
        result = adapt_fundamental_snapshots(
            (
                {
                    **asdict(row),
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                },
            ),
            market="US",
        )

        by_name = {record.feature_name: record for record in result.records}
        expected_available = datetime(
            2026, 2, 1, 23, 59, 59, 999999, tzinfo=ZoneInfo("America/New_York")
        )
        self.assertEqual(expected_available, by_name["revenue_yoy"].available_time)
        self.assertEqual(expected_available, by_name["net_profit_yoy"].available_time)
        self.assertEqual(expected_available, by_name["debt_to_assets"].available_time)


