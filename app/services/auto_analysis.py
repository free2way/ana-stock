import json
import threading
import time
from datetime import datetime, timedelta, timezone

from app.core.db import SessionLocal
from app.services.ai_daily_report import build_ai_daily_report, save_ai_daily_report
from app.services.backtester import BacktestRunner
from app.services.cn_concepts import sync_cn_concepts
from app.services.dataset_build import build_dataset
from app.services.market_sync import sync_market_data
from app.services.push_notifications import PushNotificationService
from app.services.repository import AppSettingRepository, DataJobRepository, WatchlistRepository
from app.services.trainer import SignalTrainer


AUTO_ANALYSIS_KEY = "auto_analysis_config"
AUTO_ANALYSIS_JOB_TYPE = "watchlist_auto_analysis"

DEFAULT_AUTO_ANALYSIS_CONFIG = {
    "enabled": False,
    "interval_hours": 24,
    "provider": "tushare",
    "start_date": "2025-01-01",
    "signal_type": "momentum",
    "lookback_days": 3,
    "top_n": 1,
    "sync_cn_concepts": False,
    "default_allowed_markets": ["CN"],
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

    def get_config(self, db=None) -> dict:
        if db is None:
            with SessionLocal() as own_db:
                return self.get_config(db=own_db)
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
        config["provider"] = str(config.get("provider") or "tushare").strip() or "tushare"
        config["signal_type"] = str(config.get("signal_type") or "momentum").strip() or "momentum"
        config["start_date"] = str(config.get("start_date") or "2025-01-01").strip() or "2025-01-01"
        config["sync_cn_concepts"] = bool(config.get("sync_cn_concepts", True))
        config["default_allowed_markets"] = [
            str(item).strip().upper()
            for item in (config.get("default_allowed_markets") or ["CN"])
            if str(item).strip()
        ] or ["CN"]
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

    def get_status(self, db=None) -> dict:
        config = self.get_config(db=db)
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

    def run_watchlist_analysis(self, trigger: str = "manual", allowed_markets: list[str] | None = None) -> dict:
        config, tickers, normalized_markets, job_id = self._prepare_watchlist_analysis_run(trigger, allowed_markets)

        if not tickers:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message="No sync-enabled watchlist stocks found.")
            raise RuntimeError("No sync-enabled watchlist stocks found.")

        run_name = f"watchlist_auto_{utc_now().strftime('%Y%m%d_%H%M%S')}"
        try:
            sync_results = sync_market_data(
                tickers=tickers,
                start_date=config["start_date"],
                provider=config["provider"],
            )
            cn_concept_result = None
            if config.get("sync_cn_concepts"):
                cn_tickers = [ticker for ticker in tickers if ticker.upper().endswith((".SS", ".SZ"))]
                if cn_tickers:
                    try:
                        cn_concept_result = sync_cn_concepts(tickers=cn_tickers)
                    except Exception as exc:
                        cn_concept_result = {
                            "status": "failed",
                            "message": str(exc),
                            "rows_written": 0,
                        }
            build_result = build_dataset(normalize_only=True)
            predictions_written = SignalTrainer().train(
                run_name=run_name,
                signal_type=config["signal_type"],
                lookback_days=config["lookback_days"],
            )
            daily_rows_written = BacktestRunner().run(top_n=config["top_n"])
            ai_daily_report = build_ai_daily_report(
                limit=min(8, max(3, len(tickers))),
                tickers=tickers,
                markets=sorted(normalized_markets) if normalized_markets else None,
            )
            save_ai_daily_report(ai_daily_report)
            push_result = None
            notifier = PushNotificationService()
            if notifier.available_channels():
                from app.services.ai_daily_report import render_ai_daily_report_message

                push_result = notifier.send_text(
                    title="AI 每日决策面板",
                    body=render_ai_daily_report_message(ai_daily_report),
                )
            message = (
                f"Auto analysis finished for {len(tickers)} watchlist stock(s): "
                f"{predictions_written} predictions, {daily_rows_written} backtest day(s)"
            )
            if cn_concept_result and cn_concept_result.get("rows_written"):
                message += f", synced {cn_concept_result['rows_written']} concept row(s)"
            elif cn_concept_result and cn_concept_result.get("status") == "failed":
                message += ", concept sync skipped due to provider access limits"
            if push_result and push_result.get("sent"):
                message += f", pushed to {', '.join(push_result['sent'])}"
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="success", message=message)
                self._persist_last_run(db)
            return {
                "status": "success",
                "job_id": job_id,
                "message": message,
                "tickers": tickers,
                "run_name": run_name,
                "sync_results": sync_results,
                "cn_concept_result": cn_concept_result,
                "build_result": build_result,
                "predictions_written": predictions_written,
                "daily_rows_written": daily_rows_written,
                "ai_daily_report": ai_daily_report,
                "push_result": push_result,
            }
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc))
            raise

    def _persist_last_run(self, db) -> None:
        repo = AppSettingRepository(db)
        config = self.get_config(db=db)
        config["last_run_at"] = utc_now_iso()
        repo.set(AUTO_ANALYSIS_KEY, json.dumps(config))

    def _prepare_watchlist_analysis_run(
        self,
        trigger: str,
        allowed_markets: list[str] | None,
    ) -> tuple[dict, list[str], set[str], int]:
        with SessionLocal() as db:
            config = self.get_config(db=db)
            effective_markets = allowed_markets if allowed_markets is not None else config.get("default_allowed_markets")
            normalized_markets = {str(item).strip().upper() for item in (effective_markets or []) if str(item).strip()}
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            if normalized_markets:
                tickers = [
                    item["ticker"]
                    for item in watchlist_repo.list_items(watchlist.id)
                    if item.get("sync_enabled")
                    and str(item.get("market") or "").upper() in normalized_markets
                ]
            else:
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
                    "sync_cn_concepts": config["sync_cn_concepts"],
                    "allowed_markets": sorted(normalized_markets) if normalized_markets else None,
                },
            )
            return config, tickers, normalized_markets, int(job.id)


auto_analysis_service = AutoAnalysisService()
