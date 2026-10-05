from __future__ import annotations

import json
import logging
import threading
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.ai_daily_report_delivery import deliver_cn_ai_daily_report_to_feishu
from app.services.cn_fundamentals import sync_cn_fundamentals
from app.services.cn_market_universe import refresh_cn_market_data_lake_only
from app.services.hithink_market_data import import_hithink_market_dump
from app.services.market_calendar import is_market_open_date, next_market_open_date
from app.services.market_lake import get_latest_lake_trade_date, list_lake_symbols
from app.services.market_refresh_audit import record_market_refresh_result
from app.services.market_risk import save_risk_guardrail_snapshots
from app.services.model_evaluation import SCHEDULED_EVALUATION_TRADE_DATES, evaluate_model_runs
from app.services.prediction_dual_write_audit import audit_recent_compact_dual_writes
from app.services.repository import AppSettingRepository, DataJobRepository
from app.services.screener_snapshots import (
    CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    WATCHLIST_PRECOMPUTE_TEMPLATES,
    refresh_precomputed_multi_screener_snapshots,
    refresh_precomputed_screener_snapshots,
)
from app.services.stock_selection.forward_shadow import create_cn_forward_shadow_snapshot
from app.services.stock_selection.forward_shadow_evaluation import (
    create_cn_forward_shadow_evaluation_snapshot,
)
from app.services.time_utils import app_now
from app.services.trainer import SignalTrainer
from app.services.workspace_snapshots import refresh_workspace_snapshots


CN_MARKET_SCHEDULER_CONFIG_KEY = "cn_market_scheduler_config"
CN_MARKET_REFRESH_JOB_TYPE = "refresh_cn_market_data_lake_only"
CN_POST_CLOSE_PIPELINE_JOB_TYPE = "cn_post_close_pipeline"
CN_POINT_IN_TIME_FUNDAMENTAL_JOB_TYPE = "sync_cn_fundamentals"
CN_FORWARD_SHADOW_JOB_TYPE = "cn_stock_selection_forward_shadow"
CN_SCREENER_CORE_JOB_TYPE = "screener_precompute_core"
CN_SCREENER_COMBOS_JOB_TYPE = "screener_precompute_combos"
CN_SCREENER_REST_JOB_TYPE = "screener_precompute_rest"
CN_AI_DAILY_REPORT_FEISHU_JOB_TYPE = "send_cn_ai_daily_report_feishu"
logger = logging.getLogger(__name__)

DEFAULT_CN_MARKET_SCHEDULER_CONFIG = {
    "enabled": True,
    "run_hour": 18,
    "run_minute": 0,
    "last_run_date": None,
    "last_run_at": None,
    "last_run_trade_date": None,
}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _post_refresh_ready(result: dict, *, target_trade_date: str, latest_lake_trade_date: str | None) -> bool:
    """A partial symbol-level refresh can still be usable when the lake is current.

    Suspended/no-trade symbols are tracked separately.  Do not strand the whole
    candidate pipeline merely because those exceptions make the refresh summary
    ``partial``.
    """
    status = str((result or {}).get("status") or "").lower()
    return status in {"success", "partial"} and str(latest_lake_trade_date or "")[:10] >= str(target_trade_date or "")[:10]


def _refresh_cn_price_lake(target_date: str) -> dict:
    """Prefer one official full-market dump and fall back to the existing path.

    A newly published dump must actually contain the requested session before
    it is allowed to trigger training. An unavailable or stale dump therefore
    cannot strand the post-close pipeline.
    """

    settings = get_settings()
    hithink_result: dict | None = None
    hithink_error: str | None = None
    if settings.hithink_finance_daily_dump_enabled and settings.hithink_finance_api_key:
        try:
            hithink_result = import_hithink_market_dump(kind="daily-k-10d", write_lake=True)
            if (
                str(hithink_result.get("status") or "").lower() == "success"
                and str(hithink_result.get("last_trade_date") or "")[:10] >= str(target_date)[:10]
            ):
                return {
                    **hithink_result,
                    "market": "CN",
                    "provider_used": "hithink_finance_dump",
                    "providers_attempted": ["hithink_finance_dump"],
                    "required_as_of_date": target_date,
                    "actual_as_of_date": hithink_result.get("last_trade_date"),
                    "success_count": int(hithink_result.get("latest_symbol_count") or 0),
                    "failure_count": 0,
                }
        except Exception as exc:
            hithink_error = str(exc)

    fallback = refresh_cn_market_data_lake_only(start_date=target_date, end_date=target_date)
    return {
        **fallback,
        "providers_attempted": [
            *(["hithink_finance_dump"] if settings.hithink_finance_daily_dump_enabled and settings.hithink_finance_api_key else []),
            *list(fallback.get("providers_attempted") or []),
        ],
        "hithink_shadow_result": hithink_result,
        "hithink_shadow_error": hithink_error,
    }


class CNMarketSchedulerService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_config(self, db=None) -> dict:
        if db is None:
            with SessionLocal() as own_db:
                return self.get_config(db=own_db)
        stored = AppSettingRepository(db).get(CN_MARKET_SCHEDULER_CONFIG_KEY)
        payload = {}
        if stored:
            try:
                payload = json.loads(stored)
            except json.JSONDecodeError:
                payload = {}
        config = DEFAULT_CN_MARKET_SCHEDULER_CONFIG.copy()
        config.update(payload)
        config["enabled"] = bool(config.get("enabled"))
        config["run_hour"] = min(23, max(0, _safe_int(config.get("run_hour"), 18)))
        config["run_minute"] = min(59, max(0, _safe_int(config.get("run_minute"), 0)))
        return config

    def get_status(self, db=None) -> dict:
        config = self.get_config(db=db)
        next_run_at = None
        next_trade_date = None
        if config["enabled"]:
            now = app_now()
            next_trade_date = next_market_open_date("CN", now.date(), include_self=True)
            candidate = now.replace(
                year=int(next_trade_date[:4]),
                month=int(next_trade_date[5:7]),
                day=int(next_trade_date[8:10]),
                hour=config["run_hour"],
                minute=config["run_minute"],
                second=0,
            )
            if candidate <= now:
                next_trade_date = next_market_open_date("CN", now.date(), include_self=False)
                candidate = now.replace(
                    year=int(next_trade_date[:4]),
                    month=int(next_trade_date[5:7]),
                    day=int(next_trade_date[8:10]),
                    hour=config["run_hour"],
                    minute=config["run_minute"],
                    second=0,
                )
            next_run_at = candidate.isoformat()
        return {**config, "next_run_at": next_run_at, "next_trade_date": next_trade_date}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="cn-market-scheduler", daemon=True)
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
                logger.exception("A-share post-close scheduler iteration failed; it will retry on the next poll.")
                continue

    def run_due_job(self) -> dict | None:
        config = self.get_config()
        if not config["enabled"]:
            return None
        now = app_now()
        if not is_market_open_date("CN", now.date()):
            return None
        if (now.hour, now.minute) < (config["run_hour"], config["run_minute"]):
            return None
        trade_date = now.date().isoformat()
        last_run_is_skipped = bool(config.get("last_run_skipped"))
        if config.get("last_run_trade_date") == trade_date and not last_run_is_skipped:
            return None
        latest_lake_trade_date = get_latest_lake_trade_date(market="CN")
        if str(latest_lake_trade_date or "")[:10] >= trade_date:
            return self._recover_post_close_pipeline(
                trade_date=trade_date,
                latest_lake_trade_date=str(latest_lake_trade_date),
                source="scheduler_lake_recovery",
            )
        return self.run_now(trigger="scheduler", trade_date=trade_date)

    def _recover_post_close_pipeline(self, *, trade_date: str, latest_lake_trade_date: str, source: str) -> dict:
        """Continue the dependent pipeline when a previous refresh already wrote the lake.

        A refresh can be interrupted after Parquet data is committed (for example
        by an upstream provider stall).  Treating that as a simple scheduler skip
        leaves training and candidate snapshots stale until the next trading day.
        """
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job = job_repo.create_job(
                job_type=CN_MARKET_REFRESH_JOB_TYPE,
                status="running",
                params={
                    "source": source,
                    "market": "CN",
                    "start_date": trade_date,
                    "end_date": trade_date,
                    "lake_trade_date": latest_lake_trade_date,
                    "recovered": True,
                },
                message=(
                    f"CN market lake for {latest_lake_trade_date} was already complete; "
                    "resuming the dependent model pipeline."
                ),
            )
            job_repo.complete_job(
                job.id,
                status="success",
                message=(
                    f"CN market lake for {latest_lake_trade_date} was already complete; "
                    "resuming the dependent model pipeline."
                ),
                result={"status": "success", "recovered": True, "lake_trade_date": latest_lake_trade_date},
            )
            self._persist_last_run(db=db, trade_date=trade_date, skipped=False)
            recovery_job_id = job.id
        try:
            self._start_post_close_pipeline(source_job_id=recovery_job_id, trade_date=latest_lake_trade_date)
        except Exception:
            # Keep the recovery eligible for the next 30-second scheduler poll.
            # A fresh lake alone must not permanently suppress dependent models.
            self._persist_last_run(trade_date=trade_date, skipped=True)
            raise
        self._start_risk_guardrail_async(source_job_id=recovery_job_id)
        self._start_point_in_time_fundamental_collection_async(
            source_job_id=recovery_job_id,
            trade_date=latest_lake_trade_date,
        )
        return {
            "job_id": recovery_job_id,
            "status": "recovered",
            "reason": "lake_already_fresh",
            "trade_date": trade_date,
            "message": f"CN market lake already has {latest_lake_trade_date}; post-close pipeline resumed.",
        }

    def run_now(self, trigger: str = "manual", trade_date: str | None = None) -> dict:
        config = self.get_config()
        now = app_now()
        target_date = trade_date or now.date().isoformat()
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[CN_MARKET_REFRESH_JOB_TYPE],
                stale_after_hours=4,
                message_prefix="CN market scheduler closed a stale refresh job.",
            )
            if job_repo.has_running_job(CN_MARKET_REFRESH_JOB_TYPE):
                return {
                    "job_id": None,
                    "status": "skipped",
                    "message": "A CN market refresh job is already running.",
                }
            job = job_repo.create_job(
                job_type=CN_MARKET_REFRESH_JOB_TYPE,
                status="running",
                params={
                    "source": trigger,
                    "market": "CN",
                    "start_date": target_date,
                    "end_date": target_date,
                    "run_hour": config.get("run_hour"),
                    "run_minute": config.get("run_minute"),
                },
                message=f"Refreshing CN market Parquet lake for {target_date}.",
            )
            refresh_job_id = job.id
        try:
            result = _refresh_cn_price_lake(target_date)
            record_market_refresh_result(source_job_id=refresh_job_id, result=result)
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    refresh_job_id,
                    status=str(result.get("status") or "success"),
                    message=result.get("message") or f"CN market refresh finished for {target_date}.",
                    result=result,
                )
                if trigger == "scheduler":
                    self._persist_last_run(db=db, trade_date=target_date)
            latest_lake_trade_date = get_latest_lake_trade_date(market="CN")
            if _post_refresh_ready(
                result,
                target_trade_date=target_date,
                latest_lake_trade_date=latest_lake_trade_date,
            ):
                self._start_post_close_pipeline(
                    source_job_id=refresh_job_id,
                    trade_date=str(latest_lake_trade_date or target_date),
                )
                self._start_risk_guardrail_async(source_job_id=refresh_job_id)
                self._start_point_in_time_fundamental_collection_async(
                    source_job_id=refresh_job_id,
                    trade_date=str(latest_lake_trade_date or target_date),
                )
            return {"job_id": refresh_job_id, **result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    refresh_job_id,
                    status="failed",
                    message=f"CN market refresh failed for {target_date}: {exc}",
                    result={"error": str(exc), "trade_date": target_date},
                )
            return {"job_id": refresh_job_id, "status": "failed", "message": str(exc), "trade_date": target_date}

    def _start_post_close_pipeline(self, *, source_job_id: int, trade_date: str) -> None:
        """Run training and candidate materialization off the refresh request path."""
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[CN_POST_CLOSE_PIPELINE_JOB_TYPE],
                stale_after_hours=8,
                message_prefix="CN post-close pipeline cleanup closed a stale job.",
            )
            if job_repo.has_running_job(CN_POST_CLOSE_PIPELINE_JOB_TYPE):
                return
            job = job_repo.create_job(
                job_type=CN_POST_CLOSE_PIPELINE_JOB_TYPE,
                status="running",
                params={"source_job_id": source_job_id, "market": "CN", "trade_date": trade_date},
                message="Training A-share signals and refreshing candidate snapshots after market close.",
            )
            pipeline_job_id = job.id
        threading.Thread(
            target=self._run_post_close_pipeline,
            kwargs={"pipeline_job_id": pipeline_job_id, "source_job_id": source_job_id, "trade_date": trade_date},
            name=f"cn-post-close-pipeline-{pipeline_job_id}",
            daemon=True,
        ).start()

    def _start_point_in_time_fundamental_collection_async(
        self,
        *,
        source_job_id: int,
        trade_date: str,
    ) -> None:
        """Collect the full-market shadow feature cross-section without gating production models."""

        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[CN_POINT_IN_TIME_FUNDAMENTAL_JOB_TYPE],
                stale_after_hours=8,
                message_prefix="CN point-in-time fundamental cleanup closed a stale job.",
            )
            if job_repo.has_running_job(CN_POINT_IN_TIME_FUNDAMENTAL_JOB_TYPE):
                return
            job = job_repo.create_job(
                job_type=CN_POINT_IN_TIME_FUNDAMENTAL_JOB_TYPE,
                status="running",
                params={
                    "source_job_id": source_job_id,
                    "market": "CN",
                    "trade_date": trade_date,
                    "provider": "community",
                    "scope": "full_market_shadow",
                    "batch_size": 240,
                },
                message="Collecting full-market point-in-time fundamentals for the shadow dataset.",
            )
            job_id = job.id
        threading.Thread(
            target=self._run_point_in_time_fundamental_collection,
            kwargs={"job_id": job_id, "trade_date": trade_date},
            name=f"cn-point-in-time-fundamentals-{job_id}",
            daemon=True,
        ).start()

    @staticmethod
    def _run_point_in_time_fundamental_collection(*, job_id: int, trade_date: str) -> None:
        def progress_callback(progress: dict) -> None:
            with SessionLocal() as db:
                DataJobRepository(db).update_job(
                    job_id,
                    message=(
                        f"CN point-in-time fundamentals {progress.get('next_offset', 0)}/"
                        f"{progress.get('total_tickers', 0)} for {trade_date}."
                    ),
                    progress=progress,
                )

        try:
            result = sync_cn_fundamentals(
                provider_name="community",
                offset=0,
                batch_size=240,
                max_batches=None,
                progress_callback=progress_callback,
            )
            status = str(result.get("status") or "partial").lower()
            if status not in {"success", "partial", "empty", "not_configured", "failed"}:
                status = "partial"
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status=status,
                    message=str(result.get("message") or "CN point-in-time fundamental collection finished."),
                    result={"trade_date": trade_date, **result},
                )
            CNMarketSchedulerService._run_stock_selection_forward_shadow(
                source_job_id=job_id,
                feature_date=trade_date,
            )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status="failed",
                    message=f"CN point-in-time fundamental collection failed: {exc}",
                    result={"trade_date": trade_date, "error": str(exc)},
                )
            CNMarketSchedulerService._run_stock_selection_forward_shadow(
                source_job_id=job_id,
                feature_date=trade_date,
            )

    @staticmethod
    def _run_stock_selection_forward_shadow(*, source_job_id: int, feature_date: str) -> dict:
        """Freeze one next-session shadow decision after the point-in-time collection attempt."""

        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[CN_FORWARD_SHADOW_JOB_TYPE],
                stale_after_hours=2,
                message_prefix="CN forward-shadow cleanup closed a stale job.",
            )
            if job_repo.has_running_job(CN_FORWARD_SHADOW_JOB_TYPE):
                return {
                    "status": "skipped",
                    "message": "A CN forward-shadow snapshot job is already running.",
                }
            job = job_repo.create_job(
                job_type=CN_FORWARD_SHADOW_JOB_TYPE,
                status="running",
                params={
                    "source_job_id": source_job_id,
                    "market": "CN",
                    "feature_date": feature_date,
                    "scope": "forward_shadow_only",
                },
                message="Freezing the next-session A-share point-in-time shadow snapshot.",
            )
            shadow_job_id = job.id
        try:
            with SessionLocal() as db:
                result = create_cn_forward_shadow_snapshot(
                    db,
                    feature_date=feature_date,
                    source_job_id=shadow_job_id,
                )
                payload = result.get("payload") or {}
                status = "success" if str(result.get("status")) == "success" else "partial"
                try:
                    evaluation = create_cn_forward_shadow_evaluation_snapshot(
                        db,
                        source_job_id=shadow_job_id,
                    )
                except Exception as evaluation_error:
                    evaluation = {
                        "status": "failed",
                        "message": str(evaluation_error),
                    }
                    status = "partial"
                result = {**result, "evaluation": evaluation}
                evaluation_payload = evaluation.get("payload") or {}
                message = (
                    f"CN forward shadow frozen for {result.get('snapshot_date')}: "
                    f"{int(payload.get('confirmation_date_count') or 0)}/"
                    f"{int(payload.get('minimum_confirmation_dates') or 60)} untouched dates; "
                    f"{int(evaluation_payload.get('evaluated_date_count') or 0)} matured; "
                    f"decision {payload.get('shadow_decision') or 'ABSTAIN'}."
                )
                DataJobRepository(db).complete_job(
                    shadow_job_id,
                    status=status,
                    message=message,
                    result=result,
                )
            return {"job_id": shadow_job_id, "status": status, **result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    shadow_job_id,
                    status="failed",
                    message=f"CN forward-shadow snapshot failed: {exc}",
                    result={"feature_date": feature_date, "error": str(exc)},
                )
            return {"job_id": shadow_job_id, "status": "failed", "message": str(exc)}

    def _run_post_close_pipeline(self, *, pipeline_job_id: int, source_job_id: int, trade_date: str) -> None:
        """Keep each stage visible in Task Center and isolate failures by stage."""
        stages: list[dict] = []
        try:
            training = self._run_signal_training(source_job_id=source_job_id, trade_date=trade_date)
            stages.append(training)
            if str(training.get("status")) != "success":
                raise RuntimeError(str(training.get("message") or "A-share signal training failed."))
            stages.append(self._run_screener_precompute_core(source_job_id=source_job_id, trade_date=trade_date))
            # Confluence presets consume several secondary template snapshots
            # (for example hammer reversal and growth-quality).  Materialize
            # those prerequisites before evaluating combinations.
            stages.append(self._run_screener_precompute_rest(source_job_id=source_job_id, trade_date=trade_date))
            stages.append(self._run_screener_precompute_combos(source_job_id=source_job_id, trade_date=trade_date))
            stages.append(self._run_ai_daily_report_delivery(source_job_id=source_job_id, trade_date=trade_date))
            failed = [stage for stage in stages if str(stage.get("status")) not in {"success", "partial"}]
            status = "success" if not failed else "partial"
            message = f"A-share post-close pipeline completed: {len(stages) - len(failed)}/{len(stages)} stages usable."
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    pipeline_job_id,
                    status=status,
                    message=message,
                    result={"market": "CN", "trade_date": trade_date, "stages": stages},
                )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    pipeline_job_id,
                    status="failed",
                    message=f"A-share post-close pipeline failed: {exc}",
                    result={"market": "CN", "trade_date": trade_date, "stages": stages, "error": str(exc)},
                )

    def _run_ai_daily_report_delivery(self, *, source_job_id: int, trade_date: str) -> dict:
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            latest = job_repo.get_latest_job(CN_AI_DAILY_REPORT_FEISHU_JOB_TYPE)
            latest_params = (latest or {}).get("params") or {}
            latest_result = (latest or {}).get("result") or {}
            if (
                str(latest_params.get("trade_date") or "") == trade_date
                and str((latest or {}).get("status") or "") in {"success", "partial"}
                and "feishu" in (latest_result.get("sent") or [])
            ):
                return {
                    "stage": "ai_daily_report_feishu",
                    "status": "success",
                    "trade_date": trade_date,
                    "skipped": True,
                    "message": "A-share daily report was already delivered to Feishu for this trade date.",
                }
        job_id = self._create_stage_job(
            job_type=CN_AI_DAILY_REPORT_FEISHU_JOB_TYPE,
            source_job_id=source_job_id,
            trade_date=trade_date,
            message="Building and delivering the A-share daily report to Feishu.",
        )
        if job_id is None:
            return {
                "stage": "ai_daily_report_feishu",
                "status": "failed",
                "message": "An A-share Feishu daily-report delivery job is already running.",
            }
        try:
            result = deliver_cn_ai_daily_report_to_feishu(limit=8)
            status = str(result.get("status") or "failed")
            if result.get("delivery_kind") == "readiness_notice" and status == "success":
                status = "partial"
            message = (
                f"Delivered A-share daily report for {result.get('report_date') or trade_date} to Feishu."
                if result.get("delivery_kind") == "daily_report" and result.get("sent")
                else f"Sent A-share daily-report readiness notice for {result.get('report_date') or trade_date}."
                if result.get("sent")
                else "A-share daily report could not be delivered to Feishu."
            )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status=status, message=message, result=result)
            return {"stage": "ai_daily_report_feishu", "status": status, "message": message, **result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status="failed",
                    message=f"A-share Feishu daily-report delivery failed: {exc}",
                    result={"trade_date": trade_date, "error": str(exc)},
                )
            return {"stage": "ai_daily_report_feishu", "status": "failed", "message": str(exc)}

    def _create_stage_job(self, *, job_type: str, source_job_id: int, trade_date: str, message: str):
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[job_type],
                stale_after_hours=8,
                message_prefix=f"CN post-close pipeline cleanup closed a stale {job_type} job.",
            )
            if job_repo.has_running_job(job_type):
                return None
            job = job_repo.create_job(
                job_type=job_type,
                status="running",
                params={"source_job_id": source_job_id, "market": "CN", "trade_date": trade_date},
                message=message,
            )
            return job.id

    def _run_signal_training(self, *, source_job_id: int, trade_date: str) -> dict:
        tickers = sorted(list_lake_symbols(market="CN"))
        if not tickers:
            return {"stage": "training", "status": "failed", "message": "No A-share symbols found in the market lake."}
        job_id = self._create_stage_job(
            job_type="train_cn_signals",
            source_job_id=source_job_id,
            trade_date=trade_date,
            message="Training A-share LightGBM signals after the close refresh.",
        )
        if job_id is None:
            return {"stage": "training", "status": "failed", "message": "An A-share signal training job is already running."}
        try:
            predictions_written = SignalTrainer().train(
                run_name=f"cn_close_{trade_date}",
                model_type="lightgbm",
                signal_type="momentum",
                lookback_days=3,
                tickers=tickers,
                market="CN",
                universe="full_market_cn_lake",
            )
            result = {
                "market": "CN",
                "trade_date": trade_date,
                "ticker_count": len(tickers),
                "predictions_written": predictions_written,
            }
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status="success",
                    message=f"Trained {len(tickers)} A-share symbols and wrote {predictions_written} predictions.",
                    result=result,
                )
                try:
                    refresh_workspace_snapshots(db, source_job_id=job_id)
                except Exception:
                    pass
            self._run_structured_evaluation(source_job_id=job_id)
            return {"stage": "training", "status": "success", **result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc), result={"error": str(exc)})
            return {"stage": "training", "status": "failed", "message": str(exc)}

    def _run_structured_evaluation(self, *, source_job_id: int) -> None:
        job_id = self._create_stage_job(
            job_type="evaluate_model_performance",
            source_job_id=source_job_id,
            trade_date=app_now().date().isoformat(),
            message="Persisting structured A-share model evaluation after signal training.",
        )
        if job_id is None:
            return
        try:
            with SessionLocal() as db:
                result = evaluate_model_runs(
                    db,
                    markets=["CN"],
                    require_execution_reconciliation=True,
                    recent_runs=1,
                    recent_trade_dates=SCHEDULED_EVALUATION_TRADE_DATES,
                    top_n=20,
                    # P0-3: scheduled evaluation uses the realistic round-trip
                    # band (commission ~5 + stamp 5 + open-auction slippage).
                    # The old 20 bps flattered edge and hid real losses.  The
                    # ladder is operator-tunable without a code change.
                    round_trip_cost_bps=float(getattr(get_settings(), "trainer_round_trip_cost_bps", 50.0)),
                    source_job_id=job_id,
                )
                storage_acceptance = audit_recent_compact_dual_writes(
                    db,
                    market="CN",
                    required_runs=5,
                    scan_limit=50,
                )
                result["prediction_storage_acceptance"] = storage_acceptance
                result["reliability_artifacts"] = self._refresh_reliability_artifacts(
                    db, markets=["CN"]
                )
                evaluation_status = str(result.get("status") or "partial")
                if storage_acceptance["status"] == "fail":
                    evaluation_status = "partial"
                DataJobRepository(db).complete_job(
                    job_id,
                    status=evaluation_status,
                    message=(
                        (result.get("message") or "Structured A-share model evaluation finished.")
                        + " Production storage acceptance: "
                        + f"{len(storage_acceptance['passed_runs'])}/"
                        + f"{storage_acceptance['required_runs']} consecutive trading days."
                    ),
                    result=result,
                )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc), result={"error": str(exc)})

    def _refresh_reliability_artifacts(self, db, *, markets: list[str]) -> dict:
        """Persist rolling OOS reliability + calibration after evaluation.

        A failure here must only warn and never block the committed structured
        evaluation (and the precompute stages that follow it).
        """

        try:
            from app.services.stock_selection.reliability_artifacts import (
                refresh_stock_selection_reliability_artifacts,
            )

            return refresh_stock_selection_reliability_artifacts(
                db, markets=markets, recent_runs=1
            )
        except Exception as exc:  # noqa: BLE001 - artifact refresh must not break evaluation
            logger.warning(
                "stock-selection reliability artifact refresh failed for %s: %s",
                markets,
                exc,
            )
            return {"status": "failed", "error": str(exc), "markets": list(markets)}

    def _complete_precompute_job(self, *, job_id: int, result: dict, message: str, stage: str) -> dict:
        failed_count = int(result.get("failed_count", 0) or 0)
        count = int(result.get("count", 0) or 0)
        status = "success" if count and not failed_count else "partial" if count else "failed"
        with SessionLocal() as db:
            DataJobRepository(db).complete_job(job_id, status=status, message=message.format(count=count), result=result)
        return {"stage": stage, "status": status, "count": count, "failed_count": failed_count}

    def _run_screener_precompute_core(self, *, source_job_id: int, trade_date: str) -> dict:
        job_id = self._create_stage_job(
            job_type=CN_SCREENER_CORE_JOB_TYPE,
            source_job_id=source_job_id,
            trade_date=trade_date,
            message="Precomputing core A-share candidate snapshots after signal training.",
        )
        if job_id is None:
            return {"stage": "core_candidates", "status": "failed", "message": "A core A-share precompute job is already running."}
        try:
            with SessionLocal() as db:
                result = refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                    include_watchlist=False,
                    include_all_market=False,
                    template_keys=CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
                    universes=["full_market"],
                )
            return self._complete_precompute_job(
                job_id=job_id,
                result=result,
                message="Precomputed {count} core A-share candidate snapshot(s).",
                stage="core_candidates",
            )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc), result={"error": str(exc)})
            return {"stage": "core_candidates", "status": "failed", "message": str(exc)}

    def _run_screener_precompute_combos(self, *, source_job_id: int, trade_date: str) -> dict:
        job_id = self._create_stage_job(
            job_type=CN_SCREENER_COMBOS_JOB_TYPE,
            source_job_id=source_job_id,
            trade_date=trade_date,
            message="Precomputing A-share multi-model candidate combinations.",
        )
        if job_id is None:
            return {"stage": "model_combinations", "status": "failed", "message": "An A-share combination precompute job is already running."}
        try:
            with SessionLocal() as db:
                result = refresh_precomputed_multi_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                )
            return self._complete_precompute_job(
                job_id=job_id,
                result=result,
                message="Precomputed {count} A-share multi-model candidate snapshot(s).",
                stage="model_combinations",
            )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc), result={"error": str(exc)})
            return {"stage": "model_combinations", "status": "failed", "message": str(exc)}

    def _run_screener_precompute_rest(self, *, source_job_id: int, trade_date: str) -> dict:
        job_id = self._create_stage_job(
            job_type=CN_SCREENER_REST_JOB_TYPE,
            source_job_id=source_job_id,
            trade_date=trade_date,
            message="Precomputing secondary A-share and watchlist candidate snapshots.",
        )
        if job_id is None:
            return {"stage": "secondary_candidates", "status": "failed", "message": "A secondary A-share precompute job is already running."}
        try:
            with SessionLocal() as db:
                full_market = refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                    include_watchlist=False,
                    include_all_market=False,
                    template_keys=REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
                    universes=["full_market"],
                )
            with SessionLocal() as db:
                watchlist = refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                    include_watchlist=True,
                    include_all_market=False,
                    template_keys=WATCHLIST_PRECOMPUTE_TEMPLATES,
                    universes=["watchlist"],
                )
            result = {
                "count": int(full_market.get("count", 0) or 0) + int(watchlist.get("count", 0) or 0),
                "failed_count": int(full_market.get("failed_count", 0) or 0) + int(watchlist.get("failed_count", 0) or 0),
                "batches": [full_market, watchlist],
            }
            return self._complete_precompute_job(
                job_id=job_id,
                result=result,
                message="Precomputed {count} secondary A-share candidate snapshot(s).",
                stage="secondary_candidates",
            )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc), result={"error": str(exc)})
            return {"stage": "secondary_candidates", "status": "failed", "message": str(exc)}

    def _run_risk_guardrail(self, *, source_job_id: int) -> None:
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            if job_repo.has_running_job("risk_guardrail_snapshot"):
                return
            job = job_repo.create_job(
                job_type="risk_guardrail_snapshot",
                status="running",
                params={"source_job_id": source_job_id, "markets": ["CN"], "trigger": "cn_market_refresh"},
                message="Computing risk guardrail snapshots after CN market refresh.",
            )
            risk_job_id = job.id
        try:
            with SessionLocal() as db:
                # A CN-triggered pipeline must not process US as a side effect.
                # US refresh and signoff remain independently controlled while
                # that market is on hold.
                result = save_risk_guardrail_snapshots(
                    db,
                    source_job_id=risk_job_id,
                    markets=["CN"],
                )
                DataJobRepository(db).complete_job(
                    risk_job_id,
                    status=str(result.get("status") or "success"),
                    message=result.get("message") or "Risk guardrail snapshot finished after CN market refresh.",
                    result=result,
                )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    risk_job_id,
                    status="failed",
                    message=f"Risk guardrail snapshot failed after CN market refresh: {exc}",
                    result={"error": str(exc), "source_job_id": source_job_id},
                )

    def _start_risk_guardrail_async(self, *, source_job_id: int) -> None:
        """Risk reporting must never delay model training or candidate readiness."""
        threading.Thread(
            target=self._run_risk_guardrail,
            kwargs={"source_job_id": source_job_id},
            name=f"cn-risk-guardrail-{source_job_id}",
            daemon=True,
        ).start()

    def _persist_last_run(self, *, trade_date: str, db=None, skipped: bool = False) -> None:
        if db is None:
            with SessionLocal() as own_db:
                return self._persist_last_run(db=own_db, trade_date=trade_date, skipped=skipped)
        repo = AppSettingRepository(db)
        config = self.get_config(db=db)
        now = app_now()
        config["last_run_date"] = now.date().isoformat()
        config["last_run_at"] = now.isoformat()
        config["last_run_trade_date"] = trade_date
        config["last_run_skipped"] = bool(skipped)
        repo.set(CN_MARKET_SCHEDULER_CONFIG_KEY, json.dumps(config, ensure_ascii=False))


cn_market_scheduler_service = CNMarketSchedulerService()
