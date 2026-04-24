from __future__ import annotations

import threading
from datetime import timedelta

from app.core.db import SessionLocal
from app.services.repository import DataJobRepository
from app.services.social_signals import SOCIAL_POLL_JOB_TYPE, poll_tracked_social_accounts
from app.services.time_utils import app_now


class SocialSignalSchedulerService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_started_at = None
        self.interval_minutes = 30

    def get_status(self) -> dict:
        next_run_at = None
        if self._last_started_at is None:
            next_run_at = app_now().isoformat()
        else:
            next_run_at = (self._last_started_at + timedelta(minutes=self.interval_minutes)).isoformat()
        return {
            "enabled": True,
            "interval_minutes": self.interval_minutes,
            "last_started_at": None if self._last_started_at is None else self._last_started_at.isoformat(),
            "next_run_at": next_run_at,
            "running": bool(self._thread and self._thread.is_alive()),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="social-signal-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def run_now(self) -> dict:
        with SessionLocal() as db:
            repo = DataJobRepository(db)
            repo.complete_stale_running_jobs(
                job_types=[SOCIAL_POLL_JOB_TYPE],
                stale_after_hours=2,
                message_prefix="Social scheduler closed a stale social poll job.",
            )
            if repo.has_running_job(SOCIAL_POLL_JOB_TYPE):
                return {
                    "job_id": None,
                    "status": "skipped",
                    "message": "A social signal poll job is already running.",
                }
            job = repo.create_job(
                job_type=SOCIAL_POLL_JOB_TYPE,
                status="running",
                params={"source": "scheduler", "interval_minutes": self.interval_minutes},
                message="Polling tracked X accounts for social ticker mentions.",
            )
        self._last_started_at = app_now()
        try:
            with SessionLocal() as db:
                result = poll_tracked_social_accounts(db)
                DataJobRepository(db).complete_job(
                    job.id,
                    status=_job_status_from_result(result),
                    message=result.get("message"),
                    result=result,
                )
                return {"job_id": job.id, **result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job.id,
                    status="failed",
                    message=f"Social signal poll failed: {exc}",
                    result={"error": str(exc)},
                )
            return {"job_id": job.id, "status": "failed", "message": str(exc)}

    def run_now_async(self) -> dict:
        with SessionLocal() as db:
            repo = DataJobRepository(db)
            repo.complete_stale_running_jobs(
                job_types=[SOCIAL_POLL_JOB_TYPE],
                stale_after_hours=2,
                message_prefix="Social scheduler closed a stale social poll job.",
            )
            if repo.has_running_job(SOCIAL_POLL_JOB_TYPE):
                return {"job_id": None, "status": "skipped", "message": "A social signal poll job is already running."}
            job = repo.create_job(
                job_type=SOCIAL_POLL_JOB_TYPE,
                status="running",
                params={"source": "manual"},
                message="Manual social signal poll queued.",
            )
        thread = threading.Thread(target=self._run_existing_job, args=(job.id,), name=f"social-poll-{job.id}", daemon=True)
        thread.start()
        return {"job_id": job.id, "status": "queued"}

    def _run_existing_job(self, job_id: int) -> None:
        self._last_started_at = app_now()
        try:
            with SessionLocal() as db:
                result = poll_tracked_social_accounts(db)
                DataJobRepository(db).complete_job(
                    job_id,
                    status=_job_status_from_result(result),
                    message=result.get("message"),
                    result=result,
                )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status="failed",
                    message=f"Social signal poll failed: {exc}",
                    result={"error": str(exc)},
                )

    def _loop(self) -> None:
        while not self._stop_event.wait(30):
            if self._last_started_at is not None:
                elapsed = app_now() - self._last_started_at
                if elapsed < timedelta(minutes=self.interval_minutes):
                    continue
            self.run_now()


def _job_status_from_result(result: dict) -> str:
    status = str(result.get("status") or "").lower()
    if status in {"success", "partial", "failed", "empty", "not_configured"}:
        return status
    return "success"


social_signal_scheduler_service = SocialSignalSchedulerService()
