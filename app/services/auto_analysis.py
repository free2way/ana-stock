import json
import threading
import time
from datetime import datetime, timedelta, timezone

from app.core.db import SessionLocal
from app.services.backtester import BacktestRunner
from app.services.dataset_build import build_dataset
from app.services.market_sync import sync_market_data
from app.services.repository import AppSettingRepository, DataJobRepository, WatchlistRepository
from app.services.trainer import SignalTrainer


AUTO_ANALYSIS_KEY = "auto_analysis_config"
AUTO_ANALYSIS_JOB_TYPE = "watchlist_auto_analysis"

DEFAULT_AUTO_ANALYSIS_CONFIG = {
    "enabled": False,
    "interval_hours": 24,
    "provider": "yfinance",
    "start_date": "2025-01-01",
    "signal_type": "momentum",
    "lookback_days": 3,
    "top_n": 1,
    "last_run_at": None,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AutoAnalysisService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_config(self) -> dict:
        with SessionLocal() as db:
            repo = AppSettingRepository(db)
            stored = repo.get(AUTO_ANALYSIS_KEY)
            if not stored:
                return DEFAULT_AUTO_ANALYSIS_CONFIG.copy()
            try:
                payload = json.loads(stored)
            except json.JSONDecodeError:
                return DEFAULT_AUTO_ANALYSIS_CONFIG.copy()
        config = DEFAULT_AUTO_ANALYSIS_CONFIG.copy()
        config.update(payload)
        config["enabled"] = bool(config.get("enabled"))
        config["interval_hours"] = max(1, _safe_int(config.get("interval_hours"), 24))
        config["lookback_days"] = max(1, _safe_int(config.get("lookback_days"), 3))
        config["top_n"] = max(1, _safe_int(config.get("top_n"), 1))
        config["provider"] = str(config.get("provider") or "yfinance").strip() or "yfinance"
        config["signal_type"] = str(config.get("signal_type") or "momentum").strip() or "momentum"
        config["start_date"] = str(config.get("start_date") or "2025-01-01").strip() or "2025-01-01"
        return config

    def save_config(self, updates: dict) -> dict:
        config = self.get_config()
        config.update({key: value for key, value in updates.items() if value is not None})
        config["enabled"] = bool(config.get("enabled"))
        config["interval_hours"] = max(1, _safe_int(config.get("interval_hours"), 24))
        config["lookback_days"] = max(1, _safe_int(config.get("lookback_days"), 3))
        config["top_n"] = max(1, _safe_int(config.get("top_n"), 1))
        with SessionLocal() as db:
            AppSettingRepository(db).set(AUTO_ANALYSIS_KEY, json.dumps(config))
        return self.get_status()

    def get_status(self) -> dict:
        config = self.get_config()
        last_run_at = config.get("last_run_at")
        next_run_at = None
        if config["enabled"]:
            if last_run_at:
                try:
                    next_run_at = (
                        datetime.fromisoformat(last_run_at) + timedelta(hours=config["interval_hours"])
                    ).replace(microsecond=0).isoformat()
                except ValueError:
                    next_run_at = utc_now_iso()
            else:
                next_run_at = utc_now_iso()
        return {
            **config,
            "next_run_at": next_run_at,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-analysis", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.wait(30):
            try:
                self.run_due_jobs()
            except Exception:
                # Keep the local scheduler alive even if one iteration fails.
                continue

    def run_due_jobs(self) -> dict | None:
        status = self.get_status()
        if not status["enabled"]:
            return None
        next_run_at = status.get("next_run_at")
        if next_run_at:
            try:
                if datetime.fromisoformat(next_run_at) > utc_now():
                    return None
            except ValueError:
                pass
        return self.run_watchlist_analysis(trigger="scheduler")

    def run_watchlist_analysis(self, trigger: str = "manual") -> dict:
        config = self.get_config()
        with SessionLocal() as db:
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            tickers = watchlist_repo.list_enabled_tickers(watchlist.id)
            job_repo = DataJobRepository(db)
            if job_repo.has_running_job(AUTO_ANALYSIS_JOB_TYPE):
                raise RuntimeError("Auto analysis is already running.")
            job = job_repo.create_job(
                job_type=AUTO_ANALYSIS_JOB_TYPE,
                status="running",
                params={
                    "trigger": trigger,
                    "tickers": tickers,
                    "provider": config["provider"],
                    "start_date": config["start_date"],
                    "signal_type": config["signal_type"],
                    "lookback_days": config["lookback_days"],
                    "top_n": config["top_n"],
                },
            )

        if not tickers:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job.id, status="failed", message="No sync-enabled watchlist stocks found.")
            raise RuntimeError("No sync-enabled watchlist stocks found.")

        run_name = f"watchlist_auto_{utc_now().strftime('%Y%m%d_%H%M%S')}"
        try:
            sync_results = sync_market_data(
                tickers=tickers,
                start_date=config["start_date"],
                provider=config["provider"],
            )
            build_result = build_dataset(normalize_only=True)
            predictions_written = SignalTrainer().train(
                run_name=run_name,
                signal_type=config["signal_type"],
                lookback_days=config["lookback_days"],
            )
            daily_rows_written = BacktestRunner().run(top_n=config["top_n"])
            message = (
                f"Auto analysis finished for {len(tickers)} watchlist stock(s): "
                f"{predictions_written} predictions, {daily_rows_written} backtest day(s)"
            )
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job.id, status="success", message=message)
                self._persist_last_run(db)
            return {
                "status": "success",
                "job_id": job.id,
                "message": message,
                "tickers": tickers,
                "run_name": run_name,
                "sync_results": sync_results,
                "build_result": build_result,
                "predictions_written": predictions_written,
                "daily_rows_written": daily_rows_written,
            }
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job.id, status="failed", message=str(exc))
            raise

    def _persist_last_run(self, db) -> None:
        repo = AppSettingRepository(db)
        config = self.get_config()
        config["last_run_at"] = utc_now_iso()
        repo.set(AUTO_ANALYSIS_KEY, json.dumps(config))


auto_analysis_service = AutoAnalysisService()
