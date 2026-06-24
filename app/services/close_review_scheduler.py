import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.tables import ModelRun
from app.services.ai_daily_report import build_ai_daily_report, render_ai_daily_report_push_messages, save_ai_daily_report
from app.services.cn_fundamentals import sync_cn_fundamentals
from app.services.cn_concepts import sync_cn_concepts
from app.services.auto_analysis import auto_analysis_service
from app.services.cn_market_universe import refresh_cn_market_data_daily, refresh_cn_market_data_lake_only
from app.services.focus_pool import load_today_focus_pool
from app.services.kronos_validation import KRONOS_VALIDATION_JOB_TYPE, save_kronos_validation_snapshot
from app.services.market_lake import count_lake_symbols_for_trade_date, get_latest_lake_trade_date, list_lake_symbols
from app.services.market_calendar import is_market_open_date, next_market_open_date
from app.services.model_selection_guidance import save_model_selection_guidance_snapshots
from app.services.nlp_snapshots import NEWS_ENRICHMENT_JOB_TYPE, refresh_nlp_snapshots
from app.services.push_notifications import PushNotificationService
from app.services.recommendation_regression import save_ai_report_recommendation_regression_snapshot
from app.services.repository import AppSettingRepository, DataJobRepository, PredictionRepository, WatchlistRepository, WorkspaceSnapshotRepository
from app.services.selection_quality import save_selection_quality_snapshot
from app.services.screener_snapshots import (
    CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES,
    FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    WATCHLIST_PRECOMPUTE_TEMPLATES,
    refresh_precomputed_multi_screener_snapshots,
    refresh_precomputed_screener_snapshots,
)
from app.services.technical_snapshot_cache import rebuild_technical_snapshots
from app.services.template_evaluation import build_lightgbm_prediction_evaluation
from app.services.trainer import SignalTrainer
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
CN_SIGNAL_TRAIN_JOB_TYPE = "train_cn_signals"
CN_FUNDAMENTAL_SYNC_JOB_TYPE = "sync_cn_fundamentals"
CN_CONCEPT_SYNC_JOB_TYPE = "sync_cn_concepts"
SCREENER_PRECOMPUTE_JOB_TYPE = "screener_precompute"
SCREENER_PRECOMPUTE_CORE_JOB_TYPE = "screener_precompute_core"
SCREENER_PRECOMPUTE_COMBO_JOB_TYPE = "screener_precompute_combos"
SCREENER_PRECOMPUTE_REST_JOB_TYPE = "screener_precompute_rest"
MODEL_SELECTION_GUIDANCE_JOB_TYPE = "model_selection_guidance_snapshot"
MODEL_CALIBRATION_JOB_TYPE = "model_calibration_snapshot"
MARKET_SNAPSHOT_JOB_TYPE = "market_snapshot_refresh"
RECOMMENDATION_REGRESSION_JOB_TYPE = "ai_report_recommendation_regression"
SELECTION_QUALITY_JOB_TYPE = "selection_quality_snapshot"
AI_DAILY_REPORT_JOB_TYPE = "generate_ai_daily_report"
SH_TZ = ZoneInfo("Asia/Shanghai")
CN_MIN_FULL_MARKET_REFRESH_SYMBOLS = 4000


CN_CLOSE_REVIEW_COMPLETION_REQUIRED_JOBS = [
    CLOSE_REVIEW_JOB_TYPE,
    CN_SIGNAL_TRAIN_JOB_TYPE,
    SCREENER_PRECOMPUTE_JOB_TYPE,
    SCREENER_PRECOMPUTE_CORE_JOB_TYPE,
    SCREENER_PRECOMPUTE_COMBO_JOB_TYPE,
    SCREENER_PRECOMPUTE_REST_JOB_TYPE,
    MODEL_SELECTION_GUIDANCE_JOB_TYPE,
    MODEL_CALIBRATION_JOB_TYPE,
    KRONOS_VALIDATION_JOB_TYPE,
    MARKET_SNAPSHOT_JOB_TYPE,
    AI_DAILY_REPORT_JOB_TYPE,
    SELECTION_QUALITY_JOB_TYPE,
]

DEFAULT_CLOSE_REVIEW_CONFIG = {
    "enabled": False,
    "run_hour": 18,
    "run_minute": 0,
    "provider": "auto",
    "days_back": 7,
    "overlap_days": 3,
    "refresh_limit": 0,
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
            next_date = next_market_open_date("CN", now.date(), include_self=True)
            candidate = now.replace(
                year=int(next_date[:4]),
                month=int(next_date[5:7]),
                day=int(next_date[8:10]),
                hour=config["run_hour"],
                minute=config["run_minute"],
                second=0,
            )
            if candidate <= now:
                next_date = next_market_open_date("CN", now.date(), include_self=False)
                candidate = now.replace(
                    year=int(next_date[:4]),
                    month=int(next_date[5:7]),
                    day=int(next_date[8:10]),
                    hour=config["run_hour"],
                    minute=config["run_minute"],
                    second=0,
                )
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
        if not is_market_open_date("CN", now.date()):
            return None
        if (now.hour, now.minute) < (config["run_hour"], config["run_minute"]):
            return None
        today = now.date().isoformat()
        if config.get("last_run_date") == today:
            return None
        with SessionLocal() as db:
            completion = self._daily_close_review_pipeline_completion(db, target_date=today)
            if completion.get("completed"):
                self._persist_last_run(
                    db,
                    reason="scheduler_skip_completed_pipeline",
                    completion=completion,
                )
                return {
                    "status": "skipped",
                    "reason": "completed_pipeline",
                    "message": "Today's close-review pipeline is already complete; scheduler skipped the night inspection rerun.",
                    "completion": completion,
                }
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

    def run_close_review(self, trigger: str = "manual", *, force: bool = False) -> dict:
        if self._should_skip_completed_pipeline(trigger, force=force):
            with SessionLocal() as db:
                completion = self._daily_close_review_pipeline_completion(db, target_date=sh_now().date().isoformat())
                self._persist_last_run(
                    db,
                    reason=f"{trigger}_skip_completed_pipeline",
                    completion=completion,
                )
            return {
                "status": "skipped",
                "reason": "completed_pipeline",
                "message": (
                    "Today's close-review pipeline is already complete; skipped duplicate close-review/night inspection."
                ),
                "completion": completion,
            }
        config, job_id, cleaned_jobs = self._prepare_close_review_run(trigger)
        try:
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    job_id,
                    message="Refreshing CN lake prices after close.",
                    progress={"step": "lake_refresh", "trigger": trigger},
                )
            refresh_limit = None if config["refresh_limit"] == 0 else config["refresh_limit"]
            refresh_result = self._run_lake_refresh_with_fallback(
                provider=config["provider"],
                days_back=config["days_back"],
                limit=refresh_limit,
                overlap_days=config["overlap_days"],
                rebuild_snapshots=False,
            )
            refresh_status = str(refresh_result.get("status") or "success").lower()
            refreshed_symbols = int(refresh_result.get("success_count") or refresh_result.get("rows_written") or 0)
            required_as_of_date = str(refresh_result.get("required_as_of_date") or sh_now().date().isoformat())
            lake_symbol_count = count_lake_symbols_for_trade_date(market="CN", trade_date=required_as_of_date)
            freshness_confirmed = refresh_status == "success" or (
                refresh_status == "partial"
                and refreshed_symbols >= CN_MIN_FULL_MARKET_REFRESH_SYMBOLS
                and lake_symbol_count >= CN_MIN_FULL_MARKET_REFRESH_SYMBOLS
            )
            if not freshness_confirmed:
                stale_count = int(refresh_result.get("stale_count") or 0)
                message = (
                    "Close review stopped before analysis because CN price freshness was not confirmed: "
                    f"refresh status {refresh_status}, stale {stale_count}, "
                    f"refreshed {refreshed_symbols}, lake symbols {lake_symbol_count}."
                )
                with SessionLocal() as db:
                    DataJobRepository(db).complete_job(job_id, status="partial", message=message)
                return {
                    "status": "partial",
                    "job_id": job_id,
                    "refresh_result": refresh_result,
                    "rebuild_result": {"status": "skipped", "message": "Skipped until price freshness is confirmed."},
                    "analysis_result": {"status": "skipped", "tickers": []},
                    "cleaned_stale_jobs": cleaned_jobs,
                    "message": message,
                }
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    job_id,
                    message="Rebuilding watchlist technical snapshots.",
                    progress={
                        "step": "snapshot_rebuild",
                        "refresh_success_count": int(refresh_result.get("success_count") or 0),
                    },
                )
            rebuild_result = rebuild_technical_snapshots(
                market="CN",
                tickers=self._load_cn_watchlist_tickers(),
            )
            rebuilt_count = rebuild_result.get("snapshots_rebuilt")
            if rebuilt_count is None:
                rebuilt_count = rebuild_result.get("rows_written", 0)
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    job_id,
                    message="Running watchlist AI analysis.",
                    progress={
                        "step": "watchlist_analysis",
                        "refresh_success_count": int(refresh_result.get("success_count") or 0),
                        "rebuilt_count": int(rebuilt_count or 0),
                    },
                )
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
            notifier = PushNotificationService()
            if notifier.available_channels():
                notifier.send_event(
                    event_type="system_update",
                    title="A股收盘刷新完成",
                    body=(
                        f"行情刷新：{refresh_result['success_count']} 条\n"
                        f"技术快照：{rebuilt_count} 只\n"
                        f"自选深度分析：{len(analysis_result.get('tickers', []))} 只\n"
                        "后续会继续运行模型训练、核心预计算和 AI 日报。"
                    ),
                )
            threading.Thread(
                target=self._run_post_close_followups_safe,
                args=(job_id,),
                name=f"close-review-followups-{job_id}",
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

    def _should_skip_completed_pipeline(self, trigger: str, *, force: bool = False) -> bool:
        if force:
            return False
        today = sh_now().date().isoformat()
        with SessionLocal() as db:
            completion = self._daily_close_review_pipeline_completion(db, target_date=today)
        return bool(completion.get("completed"))

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
            # The close-review pipeline is a daily production pipeline. It must
            # not create a canonical "today" lake partition from only a limited
            # subset of symbols; that was the root cause of stale watchlist data.
            effective_limit = None
            result = refresh_cn_market_data_lake_only(
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                limit=effective_limit,
            )
            result["provider_used"] = "tushare_lake"
            result["providers_attempted"] = ["tushare_lake"]
            if limit is not None:
                result["requested_limit"] = limit
                result["effective_limit"] = effective_limit
                result["limit_ignored_reason"] = "close_review_requires_full_market_cn_lake_refresh"
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
                job_repo.complete_stale_running_jobs(
                    job_types=[
                        SCREENER_PRECOMPUTE_JOB_TYPE,
                        SCREENER_PRECOMPUTE_CORE_JOB_TYPE,
                        SCREENER_PRECOMPUTE_COMBO_JOB_TYPE,
                        SCREENER_PRECOMPUTE_REST_JOB_TYPE,
                    ],
                    stale_after_hours=1,
                    message_prefix="Close-review cleanup closed a stale screener precompute job.",
                )
                if job_repo.has_running_job(SCREENER_PRECOMPUTE_JOB_TYPE):
                    return
                job = DataJobRepository(db).create_job(
                    job_type=SCREENER_PRECOMPUTE_JOB_TYPE,
                    status="running",
                    params={"source_job_id": source_job_id},
                    message="Precomputing screener model results after close review.",
                )
                precompute_job_id = job.id
            core_result = self._run_screener_precompute_core_job_safe(
                source_job_id=source_job_id,
                parent_job_id=precompute_job_id,
            )
            result = {
                "status": "success" if int(core_result.get("count", 0) or 0) > 0 else "failed",
                "count": int(core_result.get("count", 0) or 0),
                "failed_count": int(core_result.get("failed_count", 0) or 0),
                "snapshots_created": list(core_result.get("snapshots_created") or []),
                "failed_templates": list(core_result.get("failed_templates") or []),
                "batches": [core_result],
                "tail_jobs_scheduled": True,
            }
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    precompute_job_id,
                    status="success" if result.get("count", 0) > 0 else "failed",
                    message=(
                        f"Core precompute finished with {result.get('count', 0)} screener snapshot(s) "
                        f"after close review job {source_job_id}; combo/rest jobs continue in background."
                        + (
                            f"; {result.get('failed_count', 0)} template(s) failed."
                            if result.get("failed_count", 0)
                            else "."
                        )
                    ),
                    result=result,
                )
            notifier = PushNotificationService()
            if notifier.available_channels():
                notifier.send_event(
                    event_type="precompute",
                    title="A股核心模型预计算完成",
                    body=(
                        f"核心快照：{result.get('count', 0)} 个\n"
                        f"失败模板：{result.get('failed_count', 0)} 个\n"
                        "组合/补全预计算会在后台继续，页面优先读取已完成核心快照。"
                    ),
                )
            if result.get("count", 0) > 0:
                threading.Thread(
                    target=self._run_screener_precompute_tail_jobs_safe,
                    args=(source_job_id, precompute_job_id),
                    name=f"screener-precompute-tail-{source_job_id}",
                    daemon=True,
                ).start()
        except Exception as exc:
            try:
                if precompute_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            precompute_job_id,
                            status="failed",
                            message=f"Screener precompute failed: {exc}",
                )
            except Exception:
                pass
            return

    def _run_screener_precompute_tail_jobs_safe(self, source_job_id: int, parent_job_id: int | None = None) -> None:
        try:
            for runner in (
                self._run_screener_precompute_combo_job_safe,
                self._run_screener_precompute_rest_job_safe,
            ):
                runner(source_job_id=source_job_id, parent_job_id=parent_job_id)
            self._run_model_selection_guidance_job_safe(source_job_id)
            self._run_model_calibration_job_safe(source_job_id)
            self._run_kronos_validation_job_safe(source_job_id)
            self._run_ai_daily_report_job_safe(source_job_id=source_job_id, parent_job_id=parent_job_id)
            self._run_selection_quality_job_safe(source_job_id=source_job_id, parent_job_id=parent_job_id)
        except Exception:
            return

    def _run_screener_precompute_core_job_safe(self, *, source_job_id: int, parent_job_id: int | None = None) -> dict:
        def _runner(emitted_job_id: int) -> dict:
            with SessionLocal() as db:
                return refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=emitted_job_id,
                    markets=["CN"],
                    include_watchlist=False,
                    lake_only=False,
                    template_keys=CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
                    universes=["full_market"],
                    include_all_market=False,
                )

        return self._run_named_screener_precompute_job(
            job_type=SCREENER_PRECOMPUTE_CORE_JOB_TYPE,
            source_job_id=source_job_id,
            parent_job_id=parent_job_id,
            message="Precomputing core CN screener templates after close review.",
            runner=_runner,
        )

    def _run_screener_precompute_combo_job_safe(self, *, source_job_id: int, parent_job_id: int | None = None) -> dict:
        def _runner(emitted_job_id: int) -> dict:
            with SessionLocal() as db:
                return refresh_precomputed_multi_screener_snapshots(
                    db,
                    source_job_id=emitted_job_id,
                    markets=["CN"],
                )

        return self._run_named_screener_precompute_job(
            job_type=SCREENER_PRECOMPUTE_COMBO_JOB_TYPE,
            source_job_id=source_job_id,
            parent_job_id=parent_job_id,
            message="Precomputing multi-model CN confluence snapshots after close review.",
            runner=_runner,
        )

    def _run_screener_precompute_rest_job_safe(self, *, source_job_id: int, parent_job_id: int | None = None) -> dict:
        batch_results: list[dict] = []
        total_created = 0
        total_failed = 0
        batch_plan = [
            {
                "label": "cn_full_market_rest",
                "markets": ["CN"],
                "template_keys": REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
                "universes": ["full_market"],
                "include_watchlist": False,
            },
            {
                "label": "watchlist",
                "markets": ["CN"],
                "template_keys": WATCHLIST_PRECOMPUTE_TEMPLATES,
                "universes": ["watchlist"],
                "include_watchlist": True,
            },
        ]

        def _runner(emitted_job_id: int) -> dict:
            nonlocal total_created, total_failed
            total_batches = len(batch_plan)
            for index, batch in enumerate(batch_plan, start=1):
                template_keys = [key for key in (batch.get("template_keys") or []) if key]
                if not template_keys:
                    continue
                with SessionLocal() as progress_db:
                    DataJobRepository(progress_db).update_job(
                        emitted_job_id,
                        message=f"Running screener rest batch {index}/{total_batches}: {batch['label']}.",
                        progress={
                            "step": "screener_precompute_rest",
                            "batch": batch["label"],
                            "batch_index": index,
                            "batch_total": total_batches,
                            "snapshots_created_so_far": total_created,
                            "failed_so_far": total_failed,
                        },
                    )
                with SessionLocal() as db:
                    batch_result = refresh_precomputed_screener_snapshots(
                        db,
                        source_job_id=emitted_job_id,
                        markets=batch.get("markets") or ["CN"],
                        include_watchlist=bool(batch.get("include_watchlist")),
                        lake_only=False,
                        template_keys=template_keys,
                        universes=batch.get("universes") or None,
                        include_all_market=bool(batch.get("include_all_market", False)),
                    )
                batch_result["batch"] = batch["label"]
                batch_results.append(batch_result)
                total_created += int(batch_result.get("count", 0) or 0)
                total_failed += int(batch_result.get("failed_count", 0) or 0)
            return {
                "status": "success" if total_created > 0 else "failed",
                "count": total_created,
                "failed_count": total_failed,
                "batches": batch_results,
            }

        return self._run_named_screener_precompute_job(
            job_type=SCREENER_PRECOMPUTE_REST_JOB_TYPE,
            source_job_id=source_job_id,
            parent_job_id=parent_job_id,
            message="Precomputing secondary CN screener templates after close review.",
            runner=_runner,
        )

    def _run_named_screener_precompute_job(
        self,
        *,
        job_type: str,
        source_job_id: int,
        parent_job_id: int | None,
        message: str,
        runner,
    ) -> dict:
        child_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[job_type],
                    stale_after_hours=1,
                    message_prefix="Close-review cleanup closed a stale screener child precompute job.",
                )
                if job_repo.has_running_job(job_type):
                    return {"status": "skipped", "count": 0, "failed_count": 0, "job_type": job_type}
                job = job_repo.create_job(
                    job_type=job_type,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "depends_on": [parent_job_id] if parent_job_id is not None else [],
                        "pipeline_step": job_type,
                    },
                    message=message,
                )
                child_job_id = job.id
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    child_job_id,
                    message=message,
                    progress={
                        "step": job_type,
                        "source_job_id": source_job_id,
                        "parent_job_id": parent_job_id,
                    },
                )
            result = runner(child_job_id)
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    child_job_id,
                    status="success" if int(result.get("count", 0) or 0) > 0 else "failed",
                    message=message,
                    result=result,
                )
            return {"job_type": job_type, **result}
        except Exception as exc:
            try:
                if child_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            child_job_id,
                            status="failed",
                            message=f"{message} ({exc})",
                        )
            except Exception:
                pass
            return {"job_type": job_type, "status": "failed", "count": 0, "failed_count": 1, "error": str(exc)}

    def _run_post_close_followups_safe(self, source_job_id: int) -> None:
        try:
            self._run_cn_signal_training_job_safe(source_job_id)
            self._run_cn_fundamental_sync_job_safe(source_job_id)
            self._run_cn_concept_sync_job_safe(source_job_id)
            self._run_news_enrichment_job_safe(source_job_id)
            self._run_screener_precompute_job_safe(source_job_id)
            self._refresh_workspace_snapshots_safe(source_job_id)
            self._run_market_snapshot_job_safe(source_job_id)
        except Exception:
            return

    def _run_ai_daily_report_job_safe(self, source_job_id: int, parent_job_id: int | None = None) -> None:
        ai_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[AI_DAILY_REPORT_JOB_TYPE],
                    stale_after_hours=2,
                    message_prefix="Close-review cleanup closed a stale AI daily report job.",
                )
                if job_repo.has_running_job(AI_DAILY_REPORT_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=AI_DAILY_REPORT_JOB_TYPE,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "depends_on": [parent_job_id] if parent_job_id is not None else [],
                        "pipeline_step": "ai_daily_report_after_precompute",
                    },
                    message="Generating AI daily report after screener precompute tail jobs.",
                )
                ai_job_id = job.id
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    ai_job_id,
                    message="Regressing historical recommendations before building AI daily report.",
                    progress={"step": "recommendation_regression", "source_job_id": source_job_id},
                )
                regression_snapshot = save_ai_report_recommendation_regression_snapshot(
                    db=db,
                    source_job_id=ai_job_id,
                )
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    ai_job_id,
                    message="Building AI daily report payload with latest regression policy.",
                    progress={
                        "step": "build_report",
                        "source_job_id": source_job_id,
                        "recommendation_regression": regression_snapshot,
                    },
                )

            report = build_ai_daily_report(limit=8)
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    ai_job_id,
                    message="Saving AI daily report snapshot.",
                    progress={
                        "step": "save_report",
                        "report_date": report.get("report_date"),
                        "actionable_count": len(report.get("market_recommendations") or []),
                        "watch_count": len(report.get("market_watch_recommendations") or []),
                    },
                )
            save_ai_daily_report(report)

            push_result = None
            notifier = PushNotificationService()
            if notifier.available_channels():
                with SessionLocal() as db:
                    DataJobRepository(db).update_job(
                        ai_job_id,
                        message="Pushing AI daily report notifications.",
                        progress={
                            "step": "push_report",
                            "report_date": report.get("report_date"),
                        },
                    )
                push_messages = render_ai_daily_report_push_messages(report)
                push_results = []
                sent: list[str] = []
                failed: list[dict] = []
                for message_item in push_messages:
                    try:
                        result = notifier.send_event(
                            event_type="stock_recommendation",
                            title=message_item["title"],
                            body=message_item["body"],
                        )
                    except Exception as push_exc:
                        result = {
                            "status": "failed",
                            "sent": [],
                            "failed": [{"channel": "unknown", "error": str(push_exc)}],
                        }
                    push_results.append({"title": message_item["title"], **result})
                    sent.extend(item for item in (result.get("sent") or []) if item not in sent)
                    failed.extend(result.get("failed") or [])
                push_result = {
                    "status": "success" if sent and not failed else "partial" if sent else "failed",
                    "sent": sent,
                    "failed": failed,
                    "messages": push_results,
                }

            market_meta = report.get("market_recommendations_meta") or {}
            market_status = str(market_meta.get("status") or "").strip().lower() or "ready"
            actionable_count = len(report.get("market_recommendations") or [])
            watch_count = len(report.get("market_watch_recommendations") or [])
            scanned_count = int(market_meta.get("candidate_count") or 0)
            message = (
                f"Generated AI daily report after precompute tail jobs: "
                f"{actionable_count} actionable A-share candidate(s), {watch_count} watch candidate(s), "
                f"{scanned_count} ranked candidate(s), status {market_status}."
            )
            if push_result and push_result.get("sent"):
                message += f" Pushed to {', '.join(push_result['sent'])}."
            elif push_result and push_result.get("failed"):
                message += " Report saved, but some push channels failed."
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    ai_job_id,
                    status="success",
                    message=message,
                    result={
                        "report_date": report.get("report_date"),
                        "market_candidate_count": actionable_count,
                        "market_actionable_count": actionable_count,
                        "market_watch_count": watch_count,
                        "market_ranked_candidate_count": scanned_count,
                        "market_recommendations_meta": market_meta,
                        "push_result": push_result,
                    },
                )
        except Exception as exc:
            try:
                if ai_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            ai_job_id,
                            status="failed",
                            message=f"AI daily report generation failed after precompute: {exc}",
                        )
            except Exception:
                pass

    def _run_selection_quality_job_safe(self, source_job_id: int, parent_job_id: int | None = None) -> None:
        quality_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[SELECTION_QUALITY_JOB_TYPE],
                    stale_after_hours=2,
                    message_prefix="Close-review cleanup closed a stale selection quality job.",
                )
                if job_repo.has_running_job(SELECTION_QUALITY_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=SELECTION_QUALITY_JOB_TYPE,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "depends_on": [parent_job_id] if parent_job_id is not None else [],
                        "pipeline_step": "selection_quality_after_ai_daily_report",
                    },
                    message="Building unified selection-quality snapshot after AI daily report.",
                )
                quality_job_id = job.id
            with SessionLocal() as db:
                snapshot = save_selection_quality_snapshot(db=db, source_job_id=quality_job_id)
                DataJobRepository(db).complete_job(
                    quality_job_id,
                    status="success",
                    message=(
                        f"Selection-quality snapshot #{snapshot.get('id')} saved with "
                        f"{snapshot.get('sample_count', 0)} evaluated candidate record(s)."
                    ),
                    result=snapshot,
                )
        except Exception as exc:
            try:
                if quality_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            quality_job_id,
                            status="failed",
                            message=f"Selection-quality snapshot failed after AI daily report: {exc}",
                        )
            except Exception:
                pass
            return

    def _run_kronos_validation_job_safe(self, source_job_id: int) -> None:
        kronos_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[KRONOS_VALIDATION_JOB_TYPE],
                    stale_after_hours=2,
                    message_prefix="Close-review cleanup closed a stale Kronos validation job.",
                )
                if job_repo.has_running_job(KRONOS_VALIDATION_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=KRONOS_VALIDATION_JOB_TYPE,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "pipeline_step": "kronos_validation_after_precompute",
                    },
                    message="Validating top CN model candidates with optional Kronos adapter after precompute.",
                )
                kronos_job_id = job.id
            with SessionLocal() as db:
                result = save_kronos_validation_snapshot(
                    db=db,
                    source_job_id=kronos_job_id,
                    markets=["CN"],
                )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    kronos_job_id,
                    status=str(result.get("status") or "success"),
                    message=(
                        f"Kronos validation snapshot saved: {result.get('candidate_count', 0)} candidate(s), "
                        f"status {result.get('status') or '-'}."
                    ),
                    result=result,
                )
        except Exception as exc:
            try:
                if kronos_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            kronos_job_id,
                            status="failed",
                            message=f"Kronos validation failed after precompute: {exc}",
                        )
            except Exception:
                pass
            return

    def _run_model_selection_guidance_job_safe(self, source_job_id: int) -> None:
        guidance_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[MODEL_SELECTION_GUIDANCE_JOB_TYPE],
                    stale_after_hours=2,
                    message_prefix="Close-review cleanup closed a stale model selection guidance job.",
                )
                if job_repo.has_running_job(MODEL_SELECTION_GUIDANCE_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=MODEL_SELECTION_GUIDANCE_JOB_TYPE,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "markets": ["CN"],
                        "pipeline_step": "model_selection_guidance_snapshot",
                    },
                    message="Saving model selection guidance snapshot after screener precompute.",
                )
                guidance_job_id = job.id
            with SessionLocal() as db:
                snapshots = save_model_selection_guidance_snapshots(
                    db,
                    source_job_id=guidance_job_id,
                    markets=["CN"],
                )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    guidance_job_id,
                    status="success" if snapshots else "failed",
                    message=f"Saved {len(snapshots)} model selection guidance snapshot(s) after close review.",
                    result={"snapshots": snapshots, "count": len(snapshots), "markets": list(snapshots.keys())},
                )
        except Exception as exc:
            try:
                if guidance_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            guidance_job_id,
                            status="failed",
                            message=f"Model selection guidance snapshot failed: {exc}",
                        )
            except Exception:
                pass
            return

    def _run_model_calibration_job_safe(self, source_job_id: int) -> None:
        calibration_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[MODEL_CALIBRATION_JOB_TYPE],
                    stale_after_hours=2,
                    message_prefix="Close-review cleanup closed a stale model calibration job.",
                )
                if job_repo.has_running_job(MODEL_CALIBRATION_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=MODEL_CALIBRATION_JOB_TYPE,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "market": "CN",
                        "pipeline_step": "model_calibration_snapshot",
                    },
                    message="Saving LightGBM out-of-sample calibration snapshot after model guidance.",
                )
                calibration_job_id = job.id

            payload = build_lightgbm_prediction_evaluation(market="CN", recent_runs=12, top_n=60)
            payload["snapshot_meta"] = {
                "source": "snapshot",
                "market": "CN",
                "job_type": MODEL_CALIBRATION_JOB_TYPE,
            }
            with SessionLocal() as db:
                snapshot = WorkspaceSnapshotRepository(db).create_snapshot(
                    snapshot_type=MODEL_CALIBRATION_JOB_TYPE,
                    snapshot_date=app_today_iso(),
                    payload=payload,
                    source_job_id=calibration_job_id,
                )
                DataJobRepository(db).complete_job(
                    calibration_job_id,
                    status="success" if int(payload.get("sample_count") or 0) > 0 else "failed",
                    message=(
                        "Saved LightGBM out-of-sample calibration snapshot "
                        f"with {int(payload.get('sample_count') or 0)} sample(s)."
                    ),
                    result={
                        "snapshot_id": snapshot.id,
                        "sample_count": int(payload.get("sample_count") or 0),
                        "latest_trade_date": payload.get("latest_trade_date"),
                    },
                )
        except Exception as exc:
            try:
                if calibration_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            calibration_job_id,
                            status="failed",
                            message=f"Model calibration snapshot failed: {exc}",
                        )
            except Exception:
                pass
            return

    def _run_cn_signal_training_job_safe(self, source_job_id: int) -> None:
        cn_job_id: int | None = None
        run_name = f"cn_close_{sh_now().date().isoformat()}"
        try:
            cn_tickers = sorted(list_lake_symbols(market="CN"))
            if not cn_tickers:
                return
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[CN_SIGNAL_TRAIN_JOB_TYPE],
                    stale_after_hours=2,
                    message_prefix="Close-review cleanup closed a stale CN signal train job.",
                )
                if job_repo.has_running_job(CN_SIGNAL_TRAIN_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=CN_SIGNAL_TRAIN_JOB_TYPE,
                    status="running",
                    params={
                        "source_job_id": source_job_id,
                        "market": "CN",
                        "ticker_count": len(cn_tickers),
                        "model_type": "lightgbm",
                    },
                    message="Training A-share LightGBM signals after close review.",
                )
                cn_job_id = job.id
            trainer = SignalTrainer()
            predictions_written = trainer.train(
                run_name=run_name,
                model_type="lightgbm",
                signal_type="momentum",
                lookback_days=3,
                tickers=cn_tickers,
                market="CN",
                universe="full_market_cn_lake",
            )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    cn_job_id,
                    status="success",
                    message=(
                        f"Trained {len(cn_tickers)} A-share symbols with LightGBM and wrote {predictions_written} predictions."
                    ),
                    result={
                        "market": "CN",
                        "ticker_count": len(cn_tickers),
                        "predictions_written": predictions_written,
                    },
                )
            try:
                with SessionLocal() as db:
                    refresh_workspace_snapshots(db, source_job_id=cn_job_id)
            except Exception:
                # Snapshot refresh is a presentation cache. Do not fail the model training job
                # after predictions have been written successfully.
                pass
            notifier = PushNotificationService()
            if notifier.available_channels():
                notifier.send_event(
                    event_type="model_training",
                    title="A股 LightGBM 训练完成",
                    body=(
                        f"训练标的：{len(cn_tickers)} 只\n"
                        f"写入预测：{predictions_written} 条\n"
                        "下一步会基于最新预测刷新模型选股快照。"
                    ),
                )
        except Exception as exc:
            try:
                if cn_job_id is not None:
                    with SessionLocal() as db:
                        running_runs = db.scalars(
                            select(ModelRun).where(ModelRun.name == run_name, ModelRun.status == "running")
                        ).all()
                        for run in running_runs:
                            run.status = "failed"
                            run.finished_at = utc_now_iso()
                        if running_runs:
                            db.commit()
                        DataJobRepository(db).complete_job(
                            cn_job_id,
                            status="failed",
                            message=f"A-share LightGBM signal training failed: {exc}",
                            result={"error": str(exc)},
                        )
            except Exception:
                pass
            return

    def _run_cn_fundamental_sync_job_safe(self, source_job_id: int) -> None:
        cn_fund_job_id: int | None = None
        try:
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[CN_FUNDAMENTAL_SYNC_JOB_TYPE],
                    stale_after_hours=4,
                    message_prefix="Close-review cleanup closed a stale CN fundamentals sync job.",
                )
                if job_repo.has_running_job(CN_FUNDAMENTAL_SYNC_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=CN_FUNDAMENTAL_SYNC_JOB_TYPE,
                    status="running",
                    params={"source_job_id": source_job_id, "market": "CN"},
                    message="Syncing A-share fundamentals after close review.",
                )
                cn_fund_job_id = job.id
            result = sync_cn_fundamentals()
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    cn_fund_job_id,
                    status=str(result.get("status") or "success"),
                    message=str(result.get("message") or "A-share fundamentals sync completed."),
                    result=result,
                )
        except Exception:
            try:
                if cn_fund_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            cn_fund_job_id,
                            status="failed",
                            message="A-share fundamentals sync failed.",
                        )
            except Exception:
                pass
            return

    def _load_cn_concept_sync_tickers(self) -> list[str]:
        tickers: list[str] = []
        with SessionLocal() as db:
            watchlist_repo = WatchlistRepository(db)
            prediction_repo = PredictionRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            for item in watchlist_repo.list_items(watchlist.id):
                ticker = str(item.get("ticker") or "").strip().upper()
                if ticker.endswith((".SS", ".SZ")):
                    tickers.append(ticker)
            latest_signals = prediction_repo.list_latest_signal_decisions(limit=120)
            for row in latest_signals:
                ticker = str(row.get("ticker") or "").strip().upper()
                if ticker.endswith((".SS", ".SZ")):
                    tickers.append(ticker)
        for item in load_today_focus_pool():
            ticker = str(item.get("ticker") or "").strip().upper()
            if ticker.endswith((".SS", ".SZ")):
                tickers.append(ticker)
        deduped: list[str] = []
        seen: set[str] = set()
        for ticker in tickers:
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            deduped.append(ticker)
        return deduped[:180]

    def _run_cn_concept_sync_job_safe(self, source_job_id: int) -> None:
        cn_concept_job_id: int | None = None
        try:
            tickers = self._load_cn_concept_sync_tickers()
            if not tickers:
                return
            with SessionLocal() as db:
                job_repo = DataJobRepository(db)
                job_repo.complete_stale_running_jobs(
                    job_types=[CN_CONCEPT_SYNC_JOB_TYPE],
                    stale_after_hours=4,
                    message_prefix="Close-review cleanup closed a stale CN concept sync job.",
                )
                if job_repo.has_running_job(CN_CONCEPT_SYNC_JOB_TYPE):
                    return
                job = job_repo.create_job(
                    job_type=CN_CONCEPT_SYNC_JOB_TYPE,
                    status="running",
                    params={"source_job_id": source_job_id, "market": "CN", "ticker_count": len(tickers)},
                    message="Syncing A-share concepts for watchlist and model candidates after close review.",
                )
                cn_concept_job_id = job.id
            result = sync_cn_concepts(tickers=tickers)
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    cn_concept_job_id,
                    status=str(result.get("status") or "success"),
                    message=str(result.get("message") or "A-share concept sync completed."),
                    result={**result, "candidate_ticker_count": len(tickers)},
                )
        except Exception:
            try:
                if cn_concept_job_id is not None:
                    with SessionLocal() as db:
                        DataJobRepository(db).complete_job(
                            cn_concept_job_id,
                            status="failed",
                            message="A-share concept sync failed.",
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

    def _daily_close_review_pipeline_completion(self, db, *, target_date: str) -> dict:
        latest_lake_date = get_latest_lake_trade_date(market="CN")
        lake_symbol_count = (
            count_lake_symbols_for_trade_date(market="CN", trade_date=latest_lake_date)
            if latest_lake_date
            else 0
        )
        lake_ready = bool(
            latest_lake_date and latest_lake_date >= target_date and lake_symbol_count >= CN_MIN_FULL_MARKET_REFRESH_SYMBOLS
        )
        jobs = [
            item
            for item in DataJobRepository(db).list_recent_jobs(360)
            if str(item.get("started_at") or "")[:10] == target_date
        ]
        latest_by_type: dict[str, dict] = {}
        for item in jobs:
            job_type = str(item.get("job_type") or "").strip()
            if not job_type or job_type in latest_by_type:
                continue
            latest_by_type[job_type] = item

        required: dict[str, dict] = {}
        missing: list[str] = []
        not_success: list[dict] = []
        for job_type in CN_CLOSE_REVIEW_COMPLETION_REQUIRED_JOBS:
            item = latest_by_type.get(job_type)
            if item is None:
                missing.append(job_type)
                required[job_type] = {"status": "missing"}
                continue
            status = str(item.get("status") or "").strip().lower()
            required[job_type] = {
                "id": item.get("id"),
                "status": status,
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "duration_seconds": item.get("duration_seconds"),
                "message": item.get("message"),
            }
            if status != "success":
                not_success.append({"job_type": job_type, "id": item.get("id"), "status": status})

        completed = lake_ready and not missing and not not_success
        return {
            "completed": completed,
            "target_date": target_date,
            "lake": {
                "latest_trade_date": latest_lake_date,
                "symbol_count": lake_symbol_count,
                "ready": lake_ready,
            },
            "required_jobs": required,
            "missing_jobs": missing,
            "not_success_jobs": not_success,
        }

    def _persist_last_run(self, db, *, reason: str | None = None, completion: dict | None = None) -> None:
        config = self.get_config(db=db)
        now = sh_now()
        config["last_run_date"] = now.date().isoformat()
        config["last_run_at"] = utc_now_iso()
        if reason:
            config["last_run_reason"] = reason
        if completion is not None:
            config["last_run_completion_summary"] = {
                "target_date": completion.get("target_date"),
                "lake": completion.get("lake"),
                "missing_jobs": completion.get("missing_jobs") or [],
                "not_success_jobs": completion.get("not_success_jobs") or [],
            }
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
                job_types=[
                    CLOSE_REVIEW_JOB_TYPE,
                    "watchlist_auto_analysis",
                    "init_cn_market_data",
                    CN_SIGNAL_TRAIN_JOB_TYPE,
                    SCREENER_PRECOMPUTE_JOB_TYPE,
                    SCREENER_PRECOMPUTE_CORE_JOB_TYPE,
                    SCREENER_PRECOMPUTE_COMBO_JOB_TYPE,
                    SCREENER_PRECOMPUTE_REST_JOB_TYPE,
                    MODEL_SELECTION_GUIDANCE_JOB_TYPE,
                    MODEL_CALIBRATION_JOB_TYPE,
                    AI_DAILY_REPORT_JOB_TYPE,
                ],
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
                message="Starting close review refresh pipeline.",
            )
            self._persist_last_attempt(db, trigger=trigger)
            return config, int(job.id), cleaned_jobs


close_review_scheduler_service = CloseReviewSchedulerService()
