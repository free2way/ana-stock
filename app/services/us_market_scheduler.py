from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from app.core.db import SessionLocal
from app.services.backtester import BacktestRunner
from app.services.market_calendar import previous_market_open_date
from app.services.market_lake import count_lake_symbols_for_trade_date, get_latest_lake_trade_date, list_lake_symbols
from app.services.market_sync import sync_market_data
from app.services.portfolio_book import load_portfolio_positions
from app.services.repository import AppSettingRepository, DataJobRepository, WatchlistRepository
from app.services.screener_snapshots import refresh_precomputed_screener_snapshots
from app.services.trainer import SignalTrainer
from app.services.time_utils import app_now
from app.services.us_market_universe import refresh_us_grouped_daily
from app.services.us_trade_universe import build_us_trade_universe
from app.services.workspace_snapshots import refresh_workspace_snapshots, save_market_workspace_snapshots


US_MARKET_SCHEDULER_CONFIG_KEY = "us_market_scheduler_config"
US_MARKET_REFRESH_JOB_TYPE = "us_market_close_refresh"
US_SCREENER_PRECOMPUTE_JOB_TYPE = "us_screener_precompute"
US_SIGNAL_TRAIN_JOB_TYPE = "us_signal_train"

DEFAULT_US_MARKET_SCHEDULER_CONFIG = {
    "enabled": True,
    "run_hour": 11,
    "run_minute": 15,
    "adjusted": True,
    "last_attempt_date": None,
    "last_attempt_at": None,
    "last_attempt_count": 0,
    "last_run_date": None,
    "last_run_at": None,
    "last_run_trade_date": None,
    "retry_cooldown_minutes": 20,
    "priority_price_sync_enabled": True,
    "priority_price_sync_limit": 80,
    # Polygon grouped daily is sometimes published later than the first
    # post-close probe. Keep retrying through the afternoon in Asia/Shanghai
    # instead of exhausting the day at 11:55.
    "max_attempts_per_day": 12,
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
        config["run_hour"] = min(23, max(0, _safe_int(config.get("run_hour"), 11)))
        config["run_minute"] = min(59, max(0, _safe_int(config.get("run_minute"), 15)))
        config["retry_cooldown_minutes"] = max(1, _safe_int(config.get("retry_cooldown_minutes"), 20))
        config["max_attempts_per_day"] = max(1, _safe_int(config.get("max_attempts_per_day"), 3))
        config["last_attempt_count"] = max(0, _safe_int(config.get("last_attempt_count"), 0))
        config["priority_price_sync_enabled"] = bool(config.get("priority_price_sync_enabled", True))
        config["priority_price_sync_limit"] = max(1, _safe_int(config.get("priority_price_sync_limit"), 80))
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
        target_trade_date = previous_market_open_date("US", app_now().date() - timedelta(days=1))
        try:
            result = refresh_us_grouped_daily(
                adjusted=bool(config.get("adjusted", True)),
                normalize=False,
                persist_per_symbol=False,
                write_lake=True,
                write_snapshot=False,
            )
            priority_sync_result = (
                self._sync_priority_us_prices(
                    target_trade_date=target_trade_date,
                    limit=int(config.get("priority_price_sync_limit") or 80),
                )
                if config.get("priority_price_sync_enabled", True)
                else {"status": "disabled"}
            )
            result = {**result, "priority_price_sync": priority_sync_result}
            status = "success" if str(result.get("status")) == "success" else str(result.get("status") or "failed")
            latest_lake_trade_date = get_latest_lake_trade_date(market="US")
            latest_lake_symbol_count = (
                count_lake_symbols_for_trade_date(market="US", trade_date=latest_lake_trade_date)
                if latest_lake_trade_date
                else 0
            )
            previous_trade_date = previous_market_open_date("US", datetime.fromisoformat(target_trade_date).date() - timedelta(days=1))
            previous_lake_symbol_count = (
                count_lake_symbols_for_trade_date(market="US", trade_date=previous_trade_date)
                if previous_trade_date
                else 0
            )
            coverage_ready = latest_lake_symbol_count > 0 and (
                previous_lake_symbol_count <= 0
                or latest_lake_symbol_count >= int(previous_lake_symbol_count * 0.95)
            )
            if status != "success" and latest_lake_trade_date == target_trade_date and coverage_ready:
                status = "success"
                result = {
                    **result,
                    "status": "success",
                    "trade_date": latest_lake_trade_date,
                    "message": (
                        f"U.S. lake already contains {latest_lake_trade_date} with {latest_lake_symbol_count} symbols; "
                        "proceeding with training and screener precompute without a fresh Polygon success."
                    ),
                    "used_existing_lake": True,
                    "lake_symbol_count": latest_lake_symbol_count,
                    "previous_lake_symbol_count": previous_lake_symbol_count,
                }
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(
                    job_id,
                    status=status,
                    message=self._refresh_message_with_priority_sync(result),
                    result=result,
                )
                if status == "success":
                    self._persist_last_run(db, trade_date=str(result.get("trade_date") or latest_lake_trade_date or ""))
            if status == "success":
                resolved_trade_date = str(result.get("trade_date") or latest_lake_trade_date or "")
                self._run_signal_training(source_job_id=job_id, trade_date=resolved_trade_date)
                self._run_screener_precompute(source_job_id=job_id)
            return {"status": status, "job_id": job_id, "refresh_result": result}
        except Exception as exc:
            with SessionLocal() as db:
                DataJobRepository(db).complete_job(job_id, status="failed", message=str(exc))
            raise

    def _priority_us_tickers(self) -> list[str]:
        tickers: list[str] = []

        def add(ticker: object, market: object = None) -> None:
            normalized = str(ticker or "").strip().upper()
            market_code = str(market or "").strip().upper()
            if not normalized or normalized.endswith((".SS", ".SZ", ".SH", ".BJ", ".HK")):
                return
            if market_code and market_code != "US":
                return
            if normalized not in tickers:
                tickers.append(normalized)

        for item in load_portfolio_positions():
            add(item.get("ticker"), item.get("market"))

        with SessionLocal() as db:
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            for item in watchlist_repo.list_items(watchlist.id):
                add(item.get("ticker"), item.get("market"))
        return tickers

    def _priority_us_price_gaps(self, *, target_trade_date: str, limit: int) -> list[dict]:
        from app.services.market_lake import load_lake_price_history

        gaps: list[dict] = []
        for ticker in self._priority_us_tickers():
            history = load_lake_price_history(market="US", ticker=ticker, limit=1)
            latest_date = str((history[-1] or {}).get("date") or "") if history else ""
            if not latest_date or (target_trade_date and latest_date < target_trade_date):
                gaps.append(
                    {
                        "ticker": ticker,
                        "latest_date": latest_date or None,
                        # Fetch from the last known date inclusively. The lake
                        # writer de-duplicates rows and this keeps adjusted data
                        # repairs safe without needing a U.S. business-day helper here.
                        "start_date": latest_date or "2025-01-01",
                    }
                )
            if len(gaps) >= limit:
                break
        return gaps

    def _stale_priority_us_tickers(self, *, target_trade_date: str, limit: int) -> list[str]:
        return [row["ticker"] for row in self._priority_us_price_gaps(target_trade_date=target_trade_date, limit=limit)]

    def _sync_priority_us_prices(self, *, target_trade_date: str, limit: int) -> dict:
        price_gaps = self._priority_us_price_gaps(target_trade_date=target_trade_date, limit=limit)
        stale_tickers = [row["ticker"] for row in price_gaps]
        if not stale_tickers:
            return {
                "status": "skipped",
                "message": "All priority U.S. portfolio/watchlist tickers already have current lake prices.",
                "target_trade_date": target_trade_date,
                "ticker_count": 0,
                "success_count": 0,
                "failure_count": 0,
            }
        try:
            results = sync_market_data(
                tickers=stale_tickers,
                start_date="2025-01-01",
                start_dates_by_ticker={
                    row["ticker"]: row["start_date"]
                    for row in price_gaps
                    if row.get("ticker") and row.get("start_date")
                },
                end_date=target_trade_date or None,
                provider="auto",
            )
        except Exception as exc:
            return {
                "status": "failed",
                "message": f"Priority U.S. single-ticker price sync failed: {exc}",
                "target_trade_date": target_trade_date,
                "tickers": stale_tickers,
                "price_gaps": price_gaps,
                "ticker_count": len(stale_tickers),
                "success_count": 0,
                "failure_count": len(stale_tickers),
            }
        success_count = sum(1 for row in results if str(row.get("status") or "").lower() == "success")
        failure_count = max(0, len(results) - success_count)
        compact_results = [
            {
                "ticker": row.get("ticker"),
                "status": row.get("status"),
                "rows": row.get("rows"),
                "provider_ticker": row.get("provider_ticker"),
                "last_synced_date": row.get("last_synced_date"),
                "message": row.get("message"),
            }
            for row in results
        ]
        return {
            "status": "success" if success_count and failure_count == 0 else "partial" if success_count else "failed",
            "message": f"Priority U.S. price fallback synced {success_count}/{len(stale_tickers)} portfolio/watchlist ticker(s).",
            "target_trade_date": target_trade_date,
            "tickers": stale_tickers,
            "price_gaps": price_gaps,
            "ticker_count": len(stale_tickers),
            "success_count": success_count,
            "failure_count": failure_count,
            "results": compact_results,
        }

    def _refresh_message_with_priority_sync(self, result: dict) -> str:
        base = str(result.get("message") or "U.S. market close refresh finished.")
        priority = result.get("priority_price_sync") if isinstance(result, dict) else None
        if not isinstance(priority, dict):
            return base
        status = str(priority.get("status") or "")
        if status in {"disabled", "skipped"}:
            return base
        return f"{base} Priority fallback: {priority.get('message') or status}"

    def _run_signal_training(self, *, source_job_id: int, trade_date: str) -> None:
        raw_us_tickers = sorted(list_lake_symbols(market="US"))
        us_tickers, universe_summary = build_us_trade_universe(tickers=raw_us_tickers, include_summary=True)
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
                    "raw_ticker_count": len(raw_us_tickers),
                    "universe_summary": universe_summary,
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
                DataJobRepository(db).complete_job(
                    job.id,
                    status="success",
                    message=(
                        f"Trained {len(us_tickers)} eligible U.S. common-stock symbols "
                        f"({len(raw_us_tickers)} raw), wrote {predictions_written} predictions "
                        f"and {daily_rows_written} backtest rows."
                    ),
                    result={
                        "market": "US",
                        "trade_date": trade_date,
                        "ticker_count": len(us_tickers),
                        "raw_ticker_count": len(raw_us_tickers),
                        "universe_summary": universe_summary,
                        "predictions_written": predictions_written,
                        "daily_rows_written": daily_rows_written,
                    },
                )
            try:
                with SessionLocal() as db:
                    refresh_workspace_snapshots(db, source_job_id=job.id)
            except Exception:
                # Snapshot refresh is a presentation cache. Do not fail a completed
                # U.S. training run after predictions/backtests have already landed.
                pass
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
