import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.models.tables import DataJob, JobRunAttempt
from app.services.json_payload_artifacts import SUMMARY_MAX_STRING_CHARS
from app.services.repository import JOB_MESSAGE_MAX_CHARS, DataJobRepository


class JobPayloadTests(unittest.TestCase):
    def test_large_error_message_and_summary_are_bounded(self):
        job = DataJob(
            id=12,
            job_type="cn_fixture",
            status="running",
            started_at="2026-08-22T10:00:00+08:00",
            params_json=json.dumps({"market": "CN"}),
        )
        attempt = JobRunAttempt(
            id=2,
            job_id=12,
            attempt_no=1,
            status="running",
            started_at=job.started_at,
        )
        db = MagicMock()
        db.scalar.side_effect = [job, attempt]
        artifact_store = MagicMock()
        artifact_store.write.return_value = {
            "schema_version": "json-payload-artifact-v1",
            "relative_path": "payloads/job_results/error.json.gz",
            "content_sha256": "content",
            "file_sha256": "file",
        }
        error = "parameter payload " + ("x" * 100_000)

        with patch(
            "app.services.repository.get_settings",
            return_value=SimpleNamespace(job_inline_result_max_bytes=10),
        ), patch(
            "app.services.repository.JsonPayloadArtifactStore",
            return_value=artifact_store,
        ):
            DataJobRepository(db).complete_job(
                12,
                status="failed",
                message=error,
                result={"error": error},
            )

        stored_params = json.loads(job.params_json)
        self.assertEqual(len(job.message), JOB_MESSAGE_MAX_CHARS)
        self.assertIn("characters omitted", job.message)
        self.assertLessEqual(
            len(stored_params["result_summary"]["error"]),
            SUMMARY_MAX_STRING_CHARS + 100,
        )
        self.assertEqual(job.message, attempt.error_message)

    def test_large_result_is_externalized_and_attempt_keeps_summary(self):
        job = DataJob(
            id=10,
            job_type="cn_fixture",
            status="running",
            started_at="2026-08-22T10:00:00+08:00",
            params_json=json.dumps({"market": "CN"}),
        )
        attempt = JobRunAttempt(
            id=1,
            job_id=10,
            attempt_no=1,
            status="running",
            started_at=job.started_at,
        )
        db = MagicMock()
        db.scalar.side_effect = [job, attempt]
        artifact_store = MagicMock()
        artifact_store.write.return_value = {
            "schema_version": "json-payload-artifact-v1",
            "relative_path": "payloads/job_results/fixture.json.gz",
            "content_sha256": "content",
            "file_sha256": "file",
        }
        large_result = {
            "rows_written": 100,
            "data": [{"ticker": "000001.SZ"}] * 100,
        }

        with patch(
            "app.services.repository.get_settings",
            return_value=SimpleNamespace(job_inline_result_max_bytes=10),
        ), patch(
            "app.services.repository.JsonPayloadArtifactStore",
            return_value=artifact_store,
        ):
            completed = DataJobRepository(db).complete_job(
                10,
                status="success",
                result=large_result,
            )

        stored_params = json.loads(job.params_json)
        stored_attempt_summary = json.loads(attempt.summary_json)
        self.assertIs(job, completed)
        self.assertNotIn("result", stored_params)
        self.assertIn("result_artifact", stored_params)
        self.assertEqual(100, stored_params["result_summary"]["rows_written"])
        self.assertEqual(100, stored_attempt_summary["data_count"])
        self.assertNotIn("data", stored_attempt_summary)

    def test_detail_hydrates_artifact_while_list_uses_summary(self):
        row = DataJob(
            id=11,
            job_type="cn_fixture",
            status="success",
            started_at="2026-08-22T10:00:00+08:00",
            finished_at="2026-08-22T10:01:00+08:00",
            params_json=json.dumps(
                {
                    "result_artifact": {"relative_path": "fixture"},
                    "result_summary": {"rows_written": 100},
                }
            ),
        )
        artifact_store = MagicMock()
        artifact_store.read.return_value = {
            "rows_written": 100,
            "data": [{"ticker": "000001.SZ"}],
        }
        repository = DataJobRepository(MagicMock())

        with patch(
            "app.services.repository.JsonPayloadArtifactStore",
            return_value=artifact_store,
        ):
            summary = repository._serialize_job(row)
            hydrated = repository._serialize_job(row, hydrate_result_artifact=True)

        self.assertEqual("postgresql_summary", summary["result_source"])
        self.assertNotIn("data", summary["result"])
        self.assertEqual("compressed_artifact", hydrated["result_source"])
        self.assertEqual(1, len(hydrated["result"]["data"]))


if __name__ == "__main__":
    unittest.main()
