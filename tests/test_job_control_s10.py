"""S-10: background job control — cancel, timeout, heartbeat, idempotency."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import jobs as jobs_routes
from app.core.db import get_db_session
from app.services import job_control


class JobControlUnitTests(unittest.TestCase):
    def tearDown(self):
        job_control.clear(9001)
        job_control.clear(9002)

    def test_cancel_request_makes_checkpoint_raise(self):
        job_control.register(9001)
        self.assertFalse(job_control.is_cancel_requested(9001))
        self.assertTrue(job_control.request_cancel(9001, reason="user"))
        with self.assertRaises(job_control.JobCancelled):
            job_control.check_cancelled(9001)
        self.assertFalse(job_control.has_timed_out(9001))

    def test_deadline_marks_timeout_and_raises(self):
        job_control.register(9002, timeout_seconds=0.05)
        self.assertFalse(job_control.has_timed_out(9002))
        time.sleep(0.09)
        self.assertTrue(job_control.has_timed_out(9002))
        with self.assertRaises(job_control.JobCancelled):
            job_control.check_cancelled(9002)

    def test_heartbeat_updates_snapshot(self):
        job_control.register(9001)
        before = job_control.snapshot(9001)["heartbeat_count"]
        job_control.heartbeat(9001)
        self.assertEqual(job_control.snapshot(9001)["heartbeat_count"], before + 1)

    def test_no_state_after_clear(self):
        job_control.register(9001)
        job_control.clear(9001)
        self.assertIsNone(job_control.snapshot(9001))
        # check without registration is a no-op rather than an error
        job_control.check_cancelled(9001)

    def test_untracked_cancel_is_remembered(self):
        self.assertFalse(job_control.request_cancel(9001, reason="early"))
        self.assertTrue(job_control.is_cancel_requested(9001))
        with self.assertRaises(job_control.JobCancelled):
            job_control.check_cancelled(9001)

    def test_register_preserves_pending_cancel(self):
        job_control.request_cancel(9001, reason="early")
        job_control.register(9001)
        self.assertTrue(job_control.is_cancel_requested(9001))
        self.assertEqual(job_control.snapshot(9001)["cancel_reason"], "early")


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeJobRepo:
    completed: dict = {}
    runtime_calls: list = []
    done = threading.Event()

    def __init__(self, db):
        self.db = db

    def update_job(self, job_id, **kwargs):
        return None

    def record_job_runtime(self, job_id, **kwargs):
        _FakeJobRepo.runtime_calls.append((job_id, kwargs))
        return None

    def complete_job(self, job_id, *, status, message=None, result=None):
        _FakeJobRepo.completed[job_id] = {"status": status, "message": message, "result": result}
        _FakeJobRepo.done.set()
        return None


class BackgroundJobRunnerTests(unittest.TestCase):
    def setUp(self):
        _FakeJobRepo.completed = {}
        _FakeJobRepo.runtime_calls = []
        _FakeJobRepo.done = threading.Event()

    def _patches(self):
        return (
            patch.object(jobs_routes, "SessionLocal", _FakeSession),
            patch.object(jobs_routes, "DataJobRepository", _FakeJobRepo),
        )

    def test_cooperative_cancel_completes_with_cancelled_status(self):
        job_id = 9001
        release = threading.Event()

        def runner():
            release.wait(timeout=5)
            job_control.check_cancelled(job_id)
            return {"status": "success"}

        patch_session, patch_repo = self._patches()
        with patch_session, patch_repo:
            jobs_routes._run_background_job(
                job_id=job_id,
                label="cancel test",
                runner=runner,
                record_market_refresh_batch=False,
            )
            # register() runs synchronously before the worker thread starts.
            job_control.request_cancel(job_id, reason="user cancel")
            release.set()
            self.assertTrue(_FakeJobRepo.done.wait(timeout=5))

        self.assertEqual(_FakeJobRepo.completed[job_id]["status"], "cancelled")
        self.assertTrue(_FakeJobRepo.completed[job_id]["result"]["cancelled"])

    def test_timeout_completes_with_failed_timeout_status(self):
        job_id = 9001

        def runner():
            while True:
                job_control.check_cancelled(job_id)
                time.sleep(0.01)

        patch_session, patch_repo = self._patches()
        with patch_session, patch_repo:
            jobs_routes._run_background_job(
                job_id=job_id,
                label="timeout test",
                runner=runner,
                record_market_refresh_batch=False,
                timeout_seconds=0.15,
                heartbeat_seconds=0.05,
            )
            self.assertTrue(_FakeJobRepo.done.wait(timeout=5))

        self.assertEqual(_FakeJobRepo.completed[job_id]["status"], "failed_timeout")
        self.assertTrue(_FakeJobRepo.completed[job_id]["result"]["timed_out"])

    def test_heartbeat_is_persisted(self):
        job_id = 9001

        def runner():
            time.sleep(0.08)
            return {"status": "success", "message": "ok"}

        patch_session, patch_repo = self._patches()
        with patch_session, patch_repo:
            jobs_routes._run_background_job(
                job_id=job_id,
                label="heartbeat test",
                runner=runner,
                record_market_refresh_batch=False,
                heartbeat_seconds=0.03,
            )
            self.assertTrue(_FakeJobRepo.done.wait(timeout=5))

        self.assertEqual(_FakeJobRepo.completed[job_id]["status"], "success")
        heartbeats = [call for call in _FakeJobRepo.runtime_calls if call[1].get("heartbeat_at")]
        self.assertGreaterEqual(len(heartbeats), 1)
        self.assertIs(job_control.snapshot(job_id), None)


class _ClaimRepo:
    def __init__(self):
        self.created = []
        self.running = None

    def get_running_job(self, job_types):
        return self.running

    def create_job(self, *, job_type, status, params=None, message=None):
        self.created.append(job_type)
        self.running = {"id": len(self.created), "job_type": job_type, "status": "running"}
        return SimpleNamespace(id=self.running["id"])


class ClaimJobIdempotencyTests(unittest.TestCase):
    def test_concurrent_claims_create_only_one_job(self):
        repo = _ClaimRepo()
        outcomes = []
        start = threading.Barrier(4)
        lock = threading.Lock()

        def worker():
            start.wait()
            outcome = jobs_routes._claim_background_job(
                repo,
                job_types=("screener_precompute",),
                job_type="screener_precompute",
                params={"markets": ["CN"]},
            )
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(repo.created), 1)
        created = [job for job, existing in outcomes if job is not None]
        reused = [existing for job, existing in outcomes if job is None]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(reused), 3)


class _EndpointRepo:
    def __init__(self, job_id=777):
        self.job_id = job_id
        self.created = 0
        self.running = None
        self.runtime = []

    def get_running_job(self, job_types):
        return self.running

    def create_job(self, *, job_type, status, params=None, message=None):
        self.created += 1
        self.running = {"id": self.job_id, "job_type": job_type, "status": "running"}
        return SimpleNamespace(id=self.job_id)

    def record_job_runtime(self, job_id, **fields):
        self.runtime.append((job_id, fields))
        return None


class _FakeDb:
    def __init__(self, job=None):
        self._job = job

    def get(self, model, pk):
        return self._job


class JobEndpointTests(unittest.TestCase):
    def setUp(self):
        job_control.clear(777)
        self.repo = _EndpointRepo()
        self.db = _FakeDb(job=SimpleNamespace(id=777, status="running"))
        app = FastAPI()
        app.include_router(jobs_routes.router)
        app.dependency_overrides[get_db_session] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        job_control.clear(777)

    def test_repeated_precompute_click_reuses_running_job(self):
        with patch.object(jobs_routes, "is_authenticated", return_value=True), \
             patch.object(jobs_routes, "DataJobRepository", lambda db: self.repo), \
             patch.object(jobs_routes, "_run_background_job", lambda **kwargs: None):
            first = self.client.post("/jobs/precompute-cn-screeners-core")
            second = self.client.post("/jobs/precompute-cn-screeners-core")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["job_id"], 777)
        self.assertEqual(second.json()["job_id"], 777)
        self.assertEqual(second.json()["status"], "running")
        self.assertIn("already running", second.json()["message"])
        self.assertEqual(self.repo.created, 1)

    def test_cancel_endpoint_requests_cancellation(self):
        with patch.object(jobs_routes, "is_authenticated", return_value=True), \
             patch.object(jobs_routes, "DataJobRepository", lambda db: self.repo):
            response = self.client.post("/jobs/777/cancel", data={"reason": "stop"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")
        self.assertTrue(job_control.is_cancel_requested(777))
        self.assertTrue(self.repo.runtime and self.repo.runtime[0][1]["cancel_requested"] is True)

    def test_cancel_endpoint_is_noop_for_finished_job(self):
        self.db._job = SimpleNamespace(id=777, status="success")
        with patch.object(jobs_routes, "is_authenticated", return_value=True), \
             patch.object(jobs_routes, "DataJobRepository", lambda db: self.repo):
            response = self.client.post("/jobs/777/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertFalse(job_control.is_cancel_requested(777))


    def test_cancelled_status_has_operator_label(self):
        from app.api.presentation.dashboard_ops_history import task_status_view

        self.assertEqual(task_status_view("cancelled", lang="en")["label"], "Cancelled")
        self.assertEqual(task_status_view("cancelled", lang="zh")["label"], "已取消")


class PrecomputeCancelCheckpointTests(unittest.TestCase):
    def test_cancel_checkpoint_propagates_from_precompute_loop(self):
        from app.services import screener_snapshots

        job_control.clear(999)
        job_control.request_cancel(999, reason="stop")
        params = [{"model_template": "t1", "universe": "full_market", "market": "CN", "limit": 10}]
        with patch.object(screener_snapshots, "build_precompute_screener_params", return_value=params), \
             patch.object(screener_snapshots, "_screen_with_lake_preferred", return_value=[]):
            with self.assertRaises(job_control.JobCancelled):
                screener_snapshots.refresh_precomputed_screener_snapshots(
                    None,
                    template_keys=["t1"],
                    should_cancel=job_control.cancellation_checker(999),
                )
        job_control.clear(999)


if __name__ == "__main__":
    unittest.main()
