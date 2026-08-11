from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.market_refresh_audit import record_market_refresh_result


class MarketRefreshAuditTests(TestCase):
    def test_records_batch_and_attaches_quality_summary(self) -> None:
        result = {
            "market": "CN",
            "status": "partial",
            "provider_used": "tushare_lake",
            "required_as_of_date": "2026-07-22",
            "actual_as_of_date": "2026-07-22",
            "total_symbols": 2,
            "success_count": 1,
            "stale_count": 1,
            "rows_written": 1,
        }
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        repo = MagicMock()
        repo.record_result.return_value = {
            "id": 42,
            "market": "CN",
            "requested_as_of_date": "2026-07-22",
            "actual_as_of_date": "2026-07-22",
            "success_count": 1,
            "no_trade_count": 0,
            "inactive_count": 0,
            "partial_count": 1,
            "missing_count": 0,
            "failed_count": 0,
        }
        with patch("app.services.market_refresh_audit.SessionLocal", return_value=session), patch(
            "app.services.market_refresh_audit.MarketRefreshBatchRepository",
            return_value=repo,
        ):
            batch = record_market_refresh_result(source_job_id=9, result=result)

        self.assertEqual(42, batch["id"])
        self.assertEqual(42, result["refresh_batch_id"])
        self.assertEqual(1, result["quality_summary"]["partial_count"])
        repo.record_result.assert_called_once_with(
            source_job_id=9,
            market="CN",
            provider="tushare_lake",
            requested_as_of_date="2026-07-22",
            result=result,
        )

    def test_existing_batch_and_non_market_result_do_not_duplicate_audit(self) -> None:
        existing = {"market": "US", "refresh_batch_id": 7, "trade_date": "2026-07-21"}
        self.assertEqual({"id": 7}, record_market_refresh_result(source_job_id=9, result=existing))
        self.assertIsNone(record_market_refresh_result(source_job_id=9, result={"market": "HK", "trade_date": "2026-07-22"}))
