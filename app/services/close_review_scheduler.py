import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.db import SessionLocal
from app.services.auto_analysis import auto_analysis_service
from app.services.cn_market_universe import refresh_cn_market_data_daily
from app.services.repository import AppSettingRepository, DataJobRepository
from app.services.technical_snapshot_cache import rebuild_technical_snapshots


CLOSE_REVIEW_CONFIG_KEY = "close_review_scheduler_config"
CLOSE_REVIEW_JOB_TYPE = "cn_close_review"
SH_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_CLOSE_REVIEW_CONFIG = {
    "enabled": False,
    "run_hour": 16,
    "run_minute": 0,
    "provider": "tushare",
    "days_back": 7,
    "overlap_days": 3,
    "refresh_limit": 500,
    "stale_job_hours": 12,
    "retry_cooldown_minutes": 60,
    "max_attempts_per_day": 4,
    "last_attempt_at": None,
    "last_attempt_count": 0,
    "last_scheduler_attempt_date": None,
    "last_scheduler_attempt_at": None,
    "last_scheduler_attempt_count": 0,
    "last_attempt_date": None,
    "last_run_date": None,
    "last_run_at": None,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def sh_now() -> datetime:
    return datetime.now(SH_TZ).replace(microsecond=0)


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CloseReviewSchedulerService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_config(self, db=None) -> dict:
        if db is None:
            with SessionLocal() as own_db:
                return self.get_config(db=own_db)
        stored = AppSettingRepository(db).get(CLOSE_REVIEW_CONFIG_KEY)
        if not stored:
            return DEFAULT_CLOSE_REVIEW_CONFIG.copy()
        try:
            payload = json.loads(stored)
        except json.JSONDecodeError:
            payload = {}
        config = DEFAULT_CLOSE_REVIEW_CONFIG.copy()
        config.update(payload)
        config["enabled"] = bool(config.get("enabled", True))
        config["run_hour"] = min(23, max(0, _safe_int(config.get("run_hour"), 16)))
        config["run_minute"] = min(59, max(0, _safe_int(config.get("run_minute"), 0)))
        config["days_back"] = max(1, _safe_int(config.get("days_back"), 7))
        config["overlap_days"] = max(0, _safe_int(config.get("overlap_days"), 3))
        config["refresh_limit"] = max(0, _safe_int(config.get("refresh_limit"), 0))
        config["stale_job_hours"] = max(1, _safe_int(config.get("stale_job_hours"), 12))
        config["retry_cooldown_minutes"] = max(1, _safe_int(config.get("retry_cooldown_minutes"), 60))
        config["max_attempts_per_day"] = max(1, _safe_int(config.get("max_attempts_per_day"), 4))
        config["last_attempt_count"] = max(0, _safe_int(config.get("last_attempt_count"), 0))
        config["last_scheduler_attempt_count"] = max(0, _safe_int(config.get("last_scheduler_attempt_count"), 0))
        config["provider"] = str(config.get("provider") or "tushare").strip() or "tushare"
        return config

    def save_config(self, updates: dict) -> dict:
        with SessionLocal() as db:
            config = self.get_config(db=db)
            config.update({key: value for key, value in updates.items() if value is not None})
            enabled = bool(config.get("enabled"))
            now = sh_now()
            if enabled and (now.hour, now.minute) >= (_safe_int(config.get("run_hour"), 16), _safe_int(config.get("run_minute"), 0)):
                today = now.date().isoformat()
                config["last_scheduler_attempt_date"] = today
                config["last_scheduler_attempt_at"] = now.isoformat()
                config["last_scheduler_attempt_count"] = config.get("max_attempts_per_day", 4)
            AppSettingRepository(db).set(CLOSE_REVIEW_CONFIG_KEY, json.dumps(config))
            return self.get_status(db=db)

    def get_status(self, db=None) -> dict:
        config = self.get_config(db=db)
        next_run_at = None
        if config["enabled"]:
            now = sh_now()
            candidate = now.replace(hour=config["run_hour"], minute=config["run_minute"], second=0)
            if candidate <= now:
                candidate = candidate + timedelta(days=1)
                while candidate.weekday() >= 5:
                    candidate = candidate + timedelta(days=1)
            else:
                while candidate.weekday() >= 5:
                    candidate = candidate + timedelta(days=1)
                    candidate = candidate.replace(hour=config["run_hour"], minute=config["run_minute"], second=0)
            next_run_at = candidate.isoformat()
        return {**config, "next_run_at": next_run_at}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="close-review-scheduler", daemon=True)
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
        now = sh_now()
        if now.weekday() >= 5:
            return None
        if (now.hour, now.minute) < (config["run_hour"], config["run_minute"]):
            return None
        today = now.date().isoformat()
        if config.get("last_run_date") == today:
            return None
        if config.get("last_scheduler_attempt_date") == today:
            if int(config.get("last_scheduler_attempt_count") or 0) >= int(config.get("max_attempts_per_day") or 4):
                return None
            last_attempt_at = str(config.get("last_scheduler_attempt_at") or "").strip()
            if last_attempt_at:
                try:
                    retry_after = datetime.fromisoformat(last_attempt_at) + timedelta(
                        minutes=int(config.get("retry_cooldown_minutes") or 60)
                    )
                    if retry_after > now:
                        return None
                except ValueError:
                    pass
        return self.run_close_review(trigger="scheduler")

    def run_close_review(self, trigger: str = "manual") -> dict:
        config, job_id, cleaned_jobs = self._prepare_close_review_run(trigger)
        try:
            refresh_limit = None if config["refresh_limit"] == 0 else config["refresh_limit"]
            refresh_result = self._run_refresh_with_fallbacks(
                provider=config["provider"],
                days_back=config["days_back"],
                limit=refresh_limit,
                overlap_days=config["overlap_days"],
            )
            rebuild_result = rebuild_technical_snapshots(market="CN", limit=None)
            rebuilt_count = rebuild_result.get("snapshots_rebuilt")
            if rebuilt_count is None:
                rebuilt_count = rebuild_result.get("rows_written", 0)
            analysis_result = auto_analysis_service.run_watchlist_analysis(
                trigger=f"{trigger}_close_review",
                allowed_markets=["CN"],
            )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status="success",
                    message=(
                        f"Close review finished: refreshed {refresh_result['success_count']} symbol(s), "
                        f"rebuilt {rebuilt_count} snapshot(s), "
                        f"analyzed {len(analysis_result.get('tickers', []))} watchlist stock(s)"
                        + (f", cleaned {cleaned_jobs} stale job(s)" if cleaned_jobs else "")
                    ),
                )
                self._persist_last_run(db)
            return {
                "status": "success",
                "job_id": job_id,
                "refresh_result": refresh_result,
                "rebuild_result": rebuild_result,
                "analysis_result": analysis_result,
                "cleaned_stale_jobs": cleaned_jobs,
                "message": "Close review completed successfully.",
            }
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc))
            raise

    def _run_refresh_with_fallbacks(
        self,
        *,
        provider: str,
        days_back: int,
        limit: int | None,
        overlap_days: int,
    ) -> dict:
        attempted: list[str] = []
        last_result: dict | None = None
        for candidate in self._refresh_provider_candidates(provider):
            attempted.append(candidate)
            try:
                result = refresh_cn_market_data_daily(
                    days_back=days_back,
                    limit=limit,
                    provider=candidate,
                    overlap_days=overlap_days,
                )
            except Exception as exc:
                if self._should_fallback_from_exception(candidate, exc):
                    continue
                raise
            last_result = result
            if result.get("status") == "success":
                result["provider_used"] = candidate
                result["providers_attempted"] = attempted
                return result
            if result.get("status") == "partial" and (result.get("success_count") or 0) > 0:
                result["provider_used"] = candidate
                result["providers_attempted"] = attempted
                return result
        if last_result is None:
            raise RuntimeError("Close review refresh failed before any provider returned a result.")
        last_result["provider_used"] = attempted[-1] if attempted else provider
        last_result["providers_attempted"] = attempted
        return last_result

    def _refresh_provider_candidates(self, provider: str) -> list[str]:
        normalized = str(provider or "tushare").strip().lower() or "tushare"
        ordered = [normalized]
        for candidate in ("tushare", "yfinance"):
            if candidate not in ordered:
                ordered.append(candidate)
        return ordered

    def _should_fallback_from_exception(self, provider: str, exc: Exception) -> bool:
        message = str(exc).lower()
        if provider == "yfinance" and ("guce.yahoo.com" in message or "nodename nor servname" in message):
            return True
        return False

    def _persist_last_run(self, db) -> None:
        config = self.get_config(db=db)
        now = sh_now()
        config["last_run_date"] = now.date().isoformat()
        config["last_run_at"] = utc_now_iso()
        AppSettingRepository(db).set(CLOSE_REVIEW_CONFIG_KEY, json.dumps(config))

    def _persist_last_attempt(self, db, *, trigger: str) -> None:
        config = self.get_config(db=db)
        now = sh_now()
        today = now.date().isoformat()
        previous_count = int(config.get("last_attempt_count") or 0) if config.get("last_attempt_date") == today else 0
        config["last_attempt_date"] = today
        config["last_attempt_at"] = now.isoformat()
        config["last_attempt_count"] = previous_count + 1
        if trigger == "scheduler":
            previous_scheduler_count = (
                int(config.get("last_scheduler_attempt_count") or 0)
                if config.get("last_scheduler_attempt_date") == today
                else 0
            )
            config["last_scheduler_attempt_date"] = today
            config["last_scheduler_attempt_at"] = now.isoformat()
            config["last_scheduler_attempt_count"] = previous_scheduler_count + 1
        AppSettingRepository(db).set(CLOSE_REVIEW_CONFIG_KEY, json.dumps(config))

    def _prepare_close_review_run(self, trigger: str) -> tuple[dict, int, int]:
        with SessionLocal() as db:
            config = self.get_config(db=db)
            job_repo = DataJobRepository(db)
            cleaned_jobs = job_repo.complete_stale_running_jobs(
                job_types=[CLOSE_REVIEW_JOB_TYPE, "watchlist_auto_analysis", "init_cn_market_data"],
                stale_after_hours=config["stale_job_hours"],
                message_prefix="Scheduler cleanup closed a stale running job.",
            )
            if job_repo.has_running_job(CLOSE_REVIEW_JOB_TYPE):
                raise RuntimeError("Close review is already running.")
            job = job_repo.create_job(
                job_type=CLOSE_REVIEW_JOB_TYPE,
                status="running",
                params={
                    "trigger": trigger,
                    "provider": config["provider"],
                    "days_back": config["days_back"],
                    "overlap_days": config["overlap_days"],
                    "refresh_limit": None if config["refresh_limit"] == 0 else config["refresh_limit"],
                    "cleaned_stale_jobs": cleaned_jobs,
                },
            )
            self._persist_last_attempt(db, trigger=trigger)
            return config, int(job.id), cleaned_jobs


close_review_scheduler_service = CloseReviewSchedulerService()
