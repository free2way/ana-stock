from __future__ import annotations

import json
import threading
from datetime import timedelta

from app.core.db import SessionLocal
from app.services.repository import AppSettingRepository, DataJobRepository
from app.services.time_utils import app_now
from app.services.us_symbol_metadata import refresh_us_symbol_metadata


US_SYMBOL_METADATA_SCHEDULER_CONFIG_KEY = "us_symbol_metadata_scheduler_config"
US_SYMBOL_METADATA_JOB_TYPE = "us_symbol_metadata_refresh"

DEFAULT_US_SYMBOL_METADATA_SCHEDULER_CONFIG = {
    "enabled": True,
    "run_hour": 23,
    "run_minute": 0,
    "limit": 300,
    "last_run_date": None,
    "last_run_at": None,
}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class USSymbolMetadataSchedulerService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_config(self, db=None) -> dict:
        if db is None:
            with SessionLocal() as own_db:
                return self.get_config(db=own_db)
        stored = AppSettingRepository(db).get(US_SYMBOL_METADATA_SCHEDULER_CONFIG_KEY)
        payload = {}
        if stored:
            try:
                payload = json.loads(stored)
            except json.JSONDecodeError:
                payload = {}
        config = DEFAULT_US_SYMBOL_METADATA_SCHEDULER_CONFIG.copy()
        config.update(payload)
        config["enabled"] = bool(config.get("enabled"))
        config["run_hour"] = min(23, max(0, _safe_int(config.get("run_hour"), 23)))
        config["run_minute"] = min(59, max(0, _safe_int(config.get("run_minute"), 0)))
        config["limit"] = max(50, _safe_int(config.get("limit"), 300))
        return config

    def get_status(self, db=None) -> dict:
        config = self.get_config(db=db)
        next_run_at = None
        if config["enabled"]:
            now = app_now()
            candidate = now.replace(hour=config["run_hour"], minute=config["run_minute"], second=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            next_run_at = candidate.isoformat()
        return {**config, "next_run_at": next_run_at}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="us-symbol-metadata-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.wait(30):
            try:
                self.run_due_job()
            except Exception:
                continue

    def run_due_job(self) -> dict | None:
        config = self.get_config()
        if not config["enabled"]:
            return None
        now = app_now()
        if (now.hour, now.minute) < (config["run_hour"], config["run_minute"]):
            return None
        today = now.date().isoformat()
        if config.get("last_run_date") == today:
            return None
        return self.run_now(trigger="scheduler")

    def run_now(self, trigger: str = "manual") -> dict:
        config = self.get_config()
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[US_SYMBOL_METADATA_JOB_TYPE],
                stale_after_hours=4,
                message_prefix="U.S. symbol metadata scheduler closed a stale metadata refresh job.",
            )
            if job_repo.has_running_job(US_SYMBOL_METADATA_JOB_TYPE):
                return {"job_id": None, "status": "skipped", "message": "A U.S. symbol metadata refresh job is already running."}
            job = job_repo.create_job(
                job_type=US_SYMBOL_METADATA_JOB_TYPE,
                status="running",
                params={
                    "source": trigger,
                    "market": "US",
                    "limit": int(config.get("limit") or 300),
                    "only_missing": True,
                },
                message="Refreshing missing U.S. symbol name, exchange, sector and industry metadata.",
            )
        try:
            result = refresh_us_symbol_metadata(limit=int(config.get("limit") or 300), only_missing=True)
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job.id,
                    status=str(result.get("status") or "success"),
                    message=result.get("message") or "U.S. symbol metadata refresh finished.",
                    result=result,
                )
                if trigger == "scheduler":
                    self._persist_last_run(db)
            return {"job_id": job.id, **result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job.id,
                    status="failed",
                    message=f"U.S. symbol metadata refresh failed: {exc}",
                    result={"error": str(exc)},
                )
            return {"job_id": job.id, "status": "failed", "message": str(exc)}

    def _persist_last_run(self, db) -> None:
        repo = AppSettingRepository(db)
        config = self.get_config(db=db)
        now = app_now()
        config["last_run_date"] = now.date().isoformat()
        config["last_run_at"] = now.isoformat()
        repo.set(US_SYMBOL_METADATA_SCHEDULER_CONFIG_KEY, json.dumps(config, ensure_ascii=False))


us_symbol_metadata_scheduler_service = USSymbolMetadataSchedulerService()
