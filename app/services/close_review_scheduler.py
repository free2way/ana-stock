import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.db import SessionLocal
from app.services.auto_analysis import auto_analysis_service
from app.services.cn_market_universe import refresh_cn_market_data_daily, refresh_cn_market_data_lake_only
from app.services.nlp_snapshots import NEWS_ENRICHMENT_JOB_TYPE, refresh_nlp_snapshots
from app.services.repository import AppSettingRepository, DataJobRepository, WatchlistRepository
from app.services.screener_snapshots import refresh_precomputed_screener_snapshots
from app.services.technical_snapshot_cache import rebuild_technical_snapshots
from app.services.workspace_snapshots import (
    SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
    build_market_heatmap_snapshot,
    build_market_mode_snapshot,
    build_market_workspace_snapshot,
    refresh_workspace_snapshots,
)
from app.services.time_utils import app_today_iso


CLOSE_REVIEW_CONFIG_KEY = "close_review_scheduler_config"
CLOSE_REVIEW_JOB_TYPE = "cn_close_review"
SCREENER_PRECOMPUTE_JOB_TYPE = "screener_precompute"
MARKET_SNAPSHOT_JOB_TYPE = "market_snapshot_refresh"
SH_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_CLOSE_REVIEW_CONFIG = {
    "enabled": False,
    "run_hour": 18,
    "run_minute": 0,
    "provider": "auto",
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
        config["provider"] = str(config.get("provider") or "auto").strip() or "auto"
        return config

    def save_config(self, updates: dict) -> dict:
        with SessionLocal() as db:
            config = self.get_config(db=db)
            config.update({key: value for key, value in updates.items() if value is not None})
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
            refresh_result = self._run_lake_refresh_with_fallback(
                provider=config["provider"],
                days_back=config["days_back"],
                limit=refresh_limit,
                overlap_days=config["overlap_days"],
                rebuild_snapshots=False,
            )
            rebuild_result = rebuild_technical_snapshots(
                market="CN",
                tickers=self._load_cn_watchlist_tickers(),
            )
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
                        f"Close review finished: CN lake refresh {refresh_result['success_count']} row(s), "
                        f"watchlist snapshot rebuild {rebuilt_count} symbol(s), "
                        f"watchlist deep analysis {len(analysis_result.get('tickers', []))} stock(s)"
                        + (f", cleaned {cleaned_jobs} stale job(s)" if cleaned_jobs else "")
                    ),
                )
                self._persist_last_run(db)
            threading.Thread(
                target=self._refresh_workspace_snapshots_safe,
                args=(job_id,),
                name=f"close-review-workspace-snapshot-{job_id}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._run_screener_precompute_job_safe,
                args=(job_id,),
                name=f"close-review-screener-precompute-{job_id}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._run_news_enrichment_job_safe,
                args=(job_id,),
                name=f"close-review-news-enrichment-{job_id}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._run_market_snapshot_job_safe,
                args=(job_id,),
                name=f"close-review-market-snapshot-{job_id}",
                daemon=True,
            ).start()
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

    def _run_lake_refresh_with_fallback(
        self,
        *,
        provider: str,
        days_back: int,
        limit: int | None,
        overlap_days: int,
        rebuild_snapshots: bool,
    ) -> dict:
        end_date = sh_now().date()
        start_date = end_date - timedelta(days=max(0, days_back - 1))
        if str(provider or "auto").strip().lower() in {"auto", "tushare"}:
            result = refresh_cn_market_data_lake_only(
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                limit=limit,
            )
            result["provider_used"] = "tushare_lake"
            result["providers_attempted"] = ["tushare_lake"]
            return result
        return self._run_refresh_with_fallbacks(
            provider=provider,
            days_back=days_back,
            limit=limit,
            overlap_days=overlap_days,
            rebuild_snapshots=rebuild_snapshots,
        )

    def _run_refresh_with_fallbacks(
        self,
        *,
        provider: str,
        days_back: int,
        limit: int | None,
        overlap_days: int,
        rebuild_snapshots: bool,
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
                    rebuild_snapshots=rebuild_snapshots,
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
        if normalized == "auto":
            normalized = "tushare"
        ordered = [normalized]
        for candidate in ("tushare", "yfinance"):
            if candidate not in ordered:
                ordered.append(candidate)
        return ordered

    def _load_cn_watchlist_tickers(self) -> list[str]:
        with SessionLocal() as db:
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            return [
                item["ticker"]
                for item in watchlist_repo.list_items(watchlist.id)
                if item.get("sync_enabled") and str(item.get("market") or "").upper() == "CN"
            ]

    def _should_fallback_from_exception(self, provider: str, exc: Exception) -> bool:
        message = str(exc).lower()
        if provider == "yfinance" and ("guce.yahoo.com" in message or "nodename nor servname" in message):
            return True
        return False

    def _run_screener_precompute_job_safe(self, source_job_id: int) -> None:
        precompute_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                if job_repo.has_running_job(SCREENER_PRECOMPUTE_JOB_TYPE):
                    return
                job = DataJobRepository(db).create_job(
                    job_type=SCREENER_PRECOMPUTE_JOB_TYPE,
                    status="running",
                    params={"source_job_id": source_job_id},
                    message="Precomputing screener model results after close review.",
                )
                precompute_job_id = job.id
            with SessionLocal() as db:
                result = refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    lake_only=True,
                )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    precompute_job_id,
                    status="success" if result.get("count", 0) > 0 else "failed",
                    message=(
                        f"Precomputed {result.get('count', 0)} screener model snapshot(s) "
                        f"after close review job {source_job_id}"
                        + (
                            f"; {result.get('failed_count', 0)} template(s) failed."
                            if result.get("failed_count", 0)
                            else "."
                        )
                    ),
                    result=result,
                )
        except Exception:
            try:
                if precompute_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            precompute_job_id,
                            status="failed",
                            message="Screener precompute failed.",
                        )
            except Exception:
                pass
            return

    def _run_news_enrichment_job_safe(self, source_job_id: int) -> None:
        enrichment_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                if job_repo.has_running_job(NEWS_ENRICHMENT_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=NEWS_ENRICHMENT_JOB_TYPE,
                    status="running",
                    params={"source_job_id": source_job_id},
                    message="Refreshing watchlist and portfolio news snapshots after close review.",
                )
                enrichment_job_id = job.id
            with SessionLocal() as db:
                result = refresh_nlp_snapshots(db, source_job_id=source_job_id)
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    enrichment_job_id,
                    status="success" if result else "failed",
                    message=(
                        f"Refreshed {len(result)} NLP snapshot(s) after close review job {source_job_id}."
                        if result
                        else "No NLP snapshots were refreshed."
                    ),
                    result=result,
                )
        except Exception:
            try:
                if enrichment_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            enrichment_job_id,
                            status="failed",
                            message="News enrichment failed.",
                        )
            except Exception:
                pass
            return

    def _run_market_snapshot_job_safe(self, source_job_id: int) -> None:
        market_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                if job_repo.has_running_job(MARKET_SNAPSHOT_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=MARKET_SNAPSHOT_JOB_TYPE,
                    status="running",
                    params={"source_job_id": source_job_id},
                    message="Refreshing market snapshot boards after close review.",
                )
                market_job_id = job.id
            preview_payload = build_market_workspace_snapshot(None)
            premarket_payload = build_market_mode_snapshot("premarket")
            monitor_payload = build_market_mode_snapshot("monitor")
            postmarket_payload = build_market_mode_snapshot("postmarket")
            heatmap_payload = build_market_heatmap_snapshot(None)
            with SessionLocal() as db:
                from app.services.repository import WorkspaceSnapshotRepository

                repo = WorkspaceSnapshotRepository(db)
                rows = {
                    SNAPSHOT_MARKET_WORKSPACE: repo.create_snapshot(
                        snapshot_type=SNAPSHOT_MARKET_WORKSPACE,
                        snapshot_date=app_today_iso(),
                        payload=preview_payload,
                        source_job_id=source_job_id,
                    ),
                    SNAPSHOT_MARKET_WORKSPACE_PREMARKET: repo.create_snapshot(
                        snapshot_type=SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
                        snapshot_date=app_today_iso(),
                        payload=premarket_payload,
                        source_job_id=source_job_id,
                    ),
                    SNAPSHOT_MARKET_WORKSPACE_MONITOR: repo.create_snapshot(
                        snapshot_type=SNAPSHOT_MARKET_WORKSPACE_MONITOR,
                        snapshot_date=app_today_iso(),
                        payload=monitor_payload,
                        source_job_id=source_job_id,
                    ),
                    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET: repo.create_snapshot(
                        snapshot_type=SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
                        snapshot_date=app_today_iso(),
                        payload=postmarket_payload,
                        source_job_id=source_job_id,
                    ),
                    SNAPSHOT_MARKET_HEATMAP_WORKSPACE: repo.create_snapshot(
                        snapshot_type=SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
                        snapshot_date=app_today_iso(),
                        payload=heatmap_payload,
                        source_job_id=source_job_id,
                    ),
                }
                DataJobRepository(db).complete_job(
                    market_job_id,
                    status="success",
                    message=(
                        f"Refreshed market snapshot workspace after close review job {source_job_id}."
                    ),
                    result={
                        "snapshot_ids": {key: row.id for key, row in rows.items()},
                        "modes": ["premarket", "monitor", "postmarket"],
                    },
                )
        except Exception:
            try:
                if market_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            market_job_id,
                            status="failed",
                            message="Market snapshot refresh failed.",
                        )
            except Exception:
                pass
            return

    def _refresh_workspace_snapshots_safe(self, job_id: int) -> None:
        try:
            with SessionLocal() as db:
                refresh_workspace_snapshots(db, source_job_id=job_id)
        except Exception:
            return

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
