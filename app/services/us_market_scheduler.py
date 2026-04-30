from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from app.core.db import SessionLocal
from app.services.backtester import BacktestRunner
from app.services.market_calendar import previous_market_open_date
from app.services.market_lake import list_lake_symbols
from app.services.repository import AppSettingRepository, DataJobRepository
from app.services.screener_snapshots import refresh_precomputed_screener_snapshots
from app.services.trainer import SignalTrainer
from app.services.time_utils import app_now
from app.services.us_market_universe import refresh_us_grouped_daily
from app.services.workspace_snapshots import refresh_workspace_snapshots, save_market_workspace_snapshots


US_MARKET_SCHEDULER_CONFIG_KEY = "us_market_scheduler_config"
US_MARKET_REFRESH_JOB_TYPE = "us_market_close_refresh"
US_SCREENER_PRECOMPUTE_JOB_TYPE = "us_screener_precompute"
US_SIGNAL_TRAIN_JOB_TYPE = "us_signal_train"

DEFAULT_US_MARKET_SCHEDULER_CONFIG = {
    "enabled": True,
    "run_hour": 10,
    "run_minute": 0,
    "adjusted": True,
    "last_attempt_date": None,
    "last_attempt_at": None,
    "last_attempt_count": 0,
    "last_run_date": None,
    "last_run_at": None,
    "last_run_trade_date": None,
    "retry_cooldown_minutes": 60,
    "max_attempts_per_day": 3,
}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class USMarketSchedulerService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_config(self, db=None) -> dict:
        if db is None:
            with SessionLocal() as own_db:
                return self.get_config(db=own_db)
        stored = AppSettingRepository(db).get(US_MARKET_SCHEDULER_CONFIG_KEY)
        payload = {}
        if stored:
            try:
                payload = json.loads(stored)
            except json.JSONDecodeError:
                payload = {}
        config = DEFAULT_US_MARKET_SCHEDULER_CONFIG.copy()
        config.update(payload)
        config["enabled"] = bool(config.get("enabled"))
        config["run_hour"] = min(23, max(0, _safe_int(config.get("run_hour"), 10)))
        config["run_minute"] = min(59, max(0, _safe_int(config.get("run_minute"), 0)))
        config["retry_cooldown_minutes"] = max(1, _safe_int(config.get("retry_cooldown_minutes"), 60))
        config["max_attempts_per_day"] = max(1, _safe_int(config.get("max_attempts_per_day"), 3))
        config["last_attempt_count"] = max(0, _safe_int(config.get("last_attempt_count"), 0))
        config["adjusted"] = bool(config.get("adjusted", True))
        return config

    def get_status(self, db=None) -> dict:
        config = self.get_config(db=db)
        next_run_at = None
        target_trade_date = None
        if config["enabled"]:
            now = app_now()
            candidate = now.replace(hour=config["run_hour"], minute=config["run_minute"], second=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            while True:
                session_probe = candidate.date() - timedelta(days=1)
                target_trade_date = previous_market_open_date("US", session_probe)
                if config.get("last_run_trade_date") != target_trade_date:
                    break
                candidate += timedelta(days=1)
                candidate = candidate.replace(hour=config["run_hour"], minute=config["run_minute"], second=0)
            next_run_at = candidate.isoformat()
        return {**config, "next_run_at": next_run_at, "next_target_trade_date": target_trade_date}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="us-market-scheduler", daemon=True)
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
        target_trade_date = previous_market_open_date("US", now.date() - timedelta(days=1))
        if config.get("last_run_trade_date") == target_trade_date:
            return None
        today = now.date().isoformat()
        if config.get("last_run_date") == today:
            return None
        if config.get("last_attempt_date") == today:
            if int(config.get("last_attempt_count") or 0) >= int(config.get("max_attempts_per_day") or 3):
                return None
            last_attempt_at = str(config.get("last_attempt_at") or "").strip()
            if last_attempt_at:
                try:
                    retry_after = datetime.fromisoformat(last_attempt_at) + timedelta(
                        minutes=int(config.get("retry_cooldown_minutes") or 60)
                    )
                    if retry_after > now:
                        return None
                except ValueError:
                    pass
        return self.run_refresh(trigger="scheduler")

    def run_refresh(self, trigger: str = "manual") -> dict:
        config, job_id = self._prepare_run(trigger)
        try:
            result = refresh_us_grouped_daily(
                adjusted=bool(config.get("adjusted", True)),
                normalize=False,
                persist_per_symbol=False,
                write_lake=True,
                write_snapshot=False,
            )
            status = "success" if str(result.get("status")) == "success" else str(result.get("status") or "failed")
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status=status,
                    message=result.get("message") or "U.S. market close refresh finished.",
                    result=result,
                )
                if status == "success":
                    self._persist_last_run(db, trade_date=str(result.get("trade_date") or ""))
            if status == "success":
                self._run_signal_training(source_job_id=job_id, trade_date=str(result.get("trade_date") or ""))
                self._run_screener_precompute(source_job_id=job_id)
            return {"status": status, "job_id": job_id, "refresh_result": result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc))
            raise

    def _run_signal_training(self, *, source_job_id: int, trade_date: str) -> None:
        us_tickers = sorted(list_lake_symbols(market="US"))
        if not us_tickers:
            return
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[US_SIGNAL_TRAIN_JOB_TYPE],
                stale_after_hours=1,
                message_prefix="U.S. scheduler cleanup closed a stale U.S. signal train job.",
            )
            if job_repo.has_running_job(US_SIGNAL_TRAIN_JOB_TYPE):
                return
            job = job_repo.create_job(
                job_type=US_SIGNAL_TRAIN_JOB_TYPE,
                status="running",
                params={
                    "source_job_id": source_job_id,
                    "trade_date": trade_date,
                    "ticker_count": len(us_tickers),
                    "market": "US",
                    "model_type": "lightgbm",
                },
                message="Training U.S. LightGBM signals after U.S. close refresh.",
            )
        trainer = SignalTrainer()
        runner = BacktestRunner()
        try:
            predictions_written = trainer.train(
                run_name=f"us_close_{trade_date or app_now().date().isoformat()}",
                model_type="lightgbm",
                signal_type="momentum",
                lookback_days=3,
                tickers=us_tickers,
                market="US",
                universe="full_market_us_lake",
            )
            daily_rows_written = runner.run(top_n=5)
            with SessionLocal() as db:
                refresh_workspace_snapshots(db, source_job_id=job.id)
                DataJobRepository(db).complete_job(
                    job.id,
                    status="success",
                    message=(
                        f"Trained {len(us_tickers)} U.S. symbols, wrote {predictions_written} predictions "
                        f"and {daily_rows_written} backtest rows."
                    ),
                    result={
                        "market": "US",
                        "trade_date": trade_date,
                        "ticker_count": len(us_tickers),
                        "predictions_written": predictions_written,
                        "daily_rows_written": daily_rows_written,
                    },
                )
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job.id, status="failed", message=str(exc))

    def _run_screener_precompute(self, *, source_job_id: int) -> None:
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job_repo.complete_stale_running_jobs(
                job_types=[US_SCREENER_PRECOMPUTE_JOB_TYPE],
                stale_after_hours=1,
                message_prefix="U.S. scheduler cleanup closed a stale U.S. screener precompute job.",
            )
            if job_repo.has_running_job(US_SCREENER_PRECOMPUTE_JOB_TYPE):
                return
            job = job_repo.create_job(
                job_type=US_SCREENER_PRECOMPUTE_JOB_TYPE,
                status="running",
                params={"source_job_id": source_job_id, "lake_only": True, "markets": ["US"]},
                message="Precomputing U.S. screener model results after U.S. close refresh.",
            )
        with SessionLocal() as db:
            result = refresh_precomputed_screener_snapshots(
                db,
                source_job_id=source_job_id,
                markets=["US"],
                include_watchlist=False,
                lake_only=True,
            )
        with SessionLocal() as db:
            DataJobRepository(db).complete_job(
                job.id,
                status="success" if result.get("count", 0) > 0 else "failed",
                message=f"Precomputed {result.get('count', 0)} U.S. screener snapshot(s) after U.S. close refresh.",
                result=result,
            )
        if result.get("count", 0) > 0:
            with SessionLocal() as db:
                save_market_workspace_snapshots(db, source_job_id=job.id)

    def _prepare_run(self, trigger: str) -> tuple[dict, int]:
        with SessionLocal() as db:
            config = self.get_config(db=db)
            job_repo = DataJobRepository(db)
            if job_repo.has_running_job(US_MARKET_REFRESH_JOB_TYPE):
                raise RuntimeError("U.S. market close refresh is already running.")
            job = job_repo.create_job(
                job_type=US_MARKET_REFRESH_JOB_TYPE,
                status="running",
                params={"trigger": trigger, "adjusted": bool(config.get("adjusted", True))},
            )
            self._persist_attempt(db)
            return config, int(job.id)

    def _persist_attempt(self, db) -> None:
        config = self.get_config(db=db)
        now = app_now()
        today = now.date().isoformat()
        previous_count = int(config.get("last_attempt_count") or 0) if config.get("last_attempt_date") == today else 0
        config["last_attempt_date"] = today
        config["last_attempt_at"] = now.isoformat()
        config["last_attempt_count"] = previous_count + 1
        AppSettingRepository(db).set(US_MARKET_SCHEDULER_CONFIG_KEY, json.dumps(config))

    def _persist_last_run(self, db, *, trade_date: str) -> None:
        config = self.get_config(db=db)
        now = app_now()
        config["last_run_date"] = now.date().isoformat()
        config["last_run_at"] = now.isoformat()
        config["last_run_trade_date"] = trade_date
        AppSettingRepository(db).set(US_MARKET_SCHEDULER_CONFIG_KEY, json.dumps(config))


us_market_scheduler_service = USMarketSchedulerService()
