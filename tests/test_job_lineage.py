import os
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import text
from starlette.requests import Request

from app.api.routes.dashboard import dashboard_ops_job_detail
from app.core.config import get_settings
from app.core.db import SessionLocal, configure_database, init_db
from app.models.base import Base
from app.services.market_refresh_audit import record_market_refresh_result
from app.services.repository import DataJobRepository


TEST_DATABASE_URL = "postgresql+psycopg://quant:quant!123@127.0.0.1:5432/quant_test"


class JobLineageRepositoryTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["PQW_DATABASE_URL"] = os.getenv("PQW_TEST_DATABASE_URL", TEST_DATABASE_URL)
        get_settings.cache_clear()
        configure_database()
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
            db.commit()

    def test_job_run_records_definition_attempt_and_source_dependency(self) -> None:
        with SessionLocal() as db:
            repo = DataJobRepository(db)
            refresh = repo.create_job(
                job_type="refresh_cn_market_data_lake_only",
                status="running",
                params={"market": "CN", "provider": "tushare_lake"},
            )
            repo.complete_job(
                refresh.id,
                status="success",
                message="refresh complete",
                result={"market": "CN", "status": "success", "required_as_of_date": "2026-07-22"},
            )
            batch = record_market_refresh_result(
                source_job_id=refresh.id,
                result={
                    "market": "CN",
                    "status": "success",
                    "provider_used": "tushare_lake",
                    "required_as_of_date": "2026-07-22",
                    "actual_as_of_date": "2026-07-22",
                    "total_symbols": 2,
                    "success_count": 2,
                    "rows_written": 2,
                },
            )
            training = repo.create_job(
                job_type="train_cn_signals",
                status="running",
                params={"market": "CN", "source_job_id": refresh.id},
            )
            repo.complete_job(training.id, status="success", message="training complete", result={"predictions_written": 1})

            detail = repo.get_job_detail(training.id)
            refresh_detail = repo.get_job_detail(refresh.id)

        self.assertEqual(refresh.id, batch["source_job_id"])
        self.assertEqual(batch["id"], refresh_detail["market_refresh_batches"][0]["id"])
        self.assertEqual(["CN"], detail["definition"]["markets"])
        self.assertEqual("satisfied", detail["dependencies"][0]["status"])
        self.assertEqual(refresh.id, detail["dependencies"][0]["upstream_job_id"])
        self.assertEqual("success", detail["attempts"][0]["status"])
        self.assertEqual(1, detail["attempts"][0]["summary"]["predictions_written"])

    def test_job_detail_page_renders_structured_lineage(self) -> None:
        with SessionLocal() as db:
            repo = DataJobRepository(db)
            job = repo.create_job(job_type="refresh_us_grouped_daily", status="running", params={"market": "US"})
            repo.complete_job(job.id, status="success", message="refresh complete", result={"rows_written": 2})
            scope = {
                "type": "http",
                "method": "GET",
                "path": f"/dashboard/ops/job/{job.id}",
                "headers": [],
                "query_string": b"lang=zh",
            }
            with patch("app.api.routes.dashboard.is_authenticated", return_value=True):
                response = dashboard_ops_job_detail(job.id, Request(scope), lang="zh", db=db)

        self.assertIn("输入与执行追溯", response)
        self.assertIn("refresh_us_grouped_daily", response)
