from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from app.core.db import SessionLocal
from app.services.cn_market_universe import refresh_cn_market_data_lake_only
from app.services.market_calendar import is_market_open_date, next_market_open_date
from app.services.market_lake import get_latest_lake_trade_date, list_lake_symbols
from app.services.market_refresh_audit import record_market_refresh_result
from app.services.market_risk import save_risk_guardrail_snapshots
from app.services.model_evaluation import evaluate_model_runs
from app.services.repository import AppSettingRepository, DataJobRepository
from app.services.screener_snapshots import (
    CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    WATCHLIST_PRECOMPUTE_TEMPLATES,
    refresh_precomputed_multi_screener_snapshots,
    refresh_precomputed_screener_snapshots,
)
from app.services.time_utils import app_now
from app.services.trainer import SignalTrainer
from app.services.workspace_snapshots import refresh_workspace_snapshots


CN_MARKET_SCHEDULER_CONFIG_KEY = "cn_market_scheduler_config"
CN_MARKET_REFRESH_JOB_TYPE = "refresh_cn_market_data_lake_only"
CN_POST_CLOSE_PIPELINE_JOB_TYPE = "cn_post_close_pipeline"
CN_SCREENER_CORE_JOB_TYPE = "screener_precompute_core"
CN_SCREENER_COMBOS_JOB_TYPE = "screener_precompute_combos"
CN_SCREENER_REST_JOB_TYPE = "screener_precompute_rest"
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
            result = refresh_cn_market_data_lake_only(start_date=target_date, end_date=target_date)
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
                    recent_runs=1,
                    recent_trade_dates=12,
                    top_n=20,
                    round_trip_cost_bps=20.0,
                    source_job_id=job_id,
                )
                DataJobRepository(db).complete_job(
                    job_id,
                    status=str(result.get("status") or "partial"),
                    message=result.get("message") or "Structured A-share model evaluation finished.",
                    result=result,
                )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc), result={"error": str(exc)})

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
                params={"source_job_id": source_job_id, "markets": ["CN", "US"], "trigger": "cn_market_refresh"},
                message="Computing risk guardrail snapshots after CN market refresh.",
            )
            risk_job_id = job.id
        try:
            with SessionLocal() as db:
                result = save_risk_guardrail_snapshots(db, source_job_id=risk_job_id, markets=["CN", "US"])
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
