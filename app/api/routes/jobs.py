import threading
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db_session
from app.services.auto_analysis import auto_analysis_service
from app.services.auth import is_authenticated, login_redirect
from app.services.backtester import BacktestRunner
from app.services.ai_daily_report import (
    build_ai_daily_report,
    load_ai_daily_report,
    render_ai_daily_report_message,
    render_ai_daily_report_push_messages,
    save_ai_daily_report,
)
from app.services.cn_market_universe import (
    init_cn_market_data,
    refresh_cn_market_data,
    refresh_cn_market_data_daily,
    refresh_cn_market_data_lake_only,
    sync_cn_symbol_universe,
)
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.cn_concepts import sync_cn_concepts
from app.services.cn_fundamentals import sync_cn_fundamentals
from app.services.dataset_build import build_dataset
from app.services.global_fundamentals import sync_global_fundamentals
from app.services.job_response import build_job_payload, complete_job_and_build_payload, fail_job_and_build_payload
from app.services.kronos_validation import KRONOS_VALIDATION_JOB_TYPE, save_kronos_validation_snapshot
from app.services.market_sync import sync_market_data
from app.services.market_lake import list_lake_symbols, query_us_daily_summary
from app.services.market_csv_cleanup import cleanup_market_csv_files
from app.services.model_selection_guidance import save_model_selection_guidance_snapshots
from app.services.model_output_importer import ExternalModelOutputImporter
from app.services.push_notifications import PushNotificationService
from app.services.repository import DataJobRepository, PriceSyncStateRepository
from app.services.sample_data import seed_sample_data
from app.services.screener_snapshots import (
    CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES,
    REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    WATCHLIST_PRECOMPUTE_TEMPLATES,
    refresh_precomputed_multi_screener_snapshots,
    refresh_precomputed_screener_snapshots,
)
from app.services.technical_snapshot_cache import rebuild_technical_snapshots
from app.services.trainer import SignalTrainer
from app.services.us_market_universe import refresh_us_grouped_daily
from app.services.us_market_universe import refresh_us_grouped_daily_range
from app.services.us_trade_universe import build_us_trade_universe
from app.services.workspace_snapshots import refresh_workspace_snapshots
from app.services.storage_retention import clean_model_history


router = APIRouter(prefix="/jobs", tags=["jobs"])


PROVIDER_OPTIONS = {
    "price": [
        {"value": "auto", "label": "Auto", "description": "CN uses TuShare; US uses Alpaca with yfinance fallback; HK uses yfinance."},
        {"value": "alpaca", "label": "Alpaca", "description": "Preferred for U.S. daily bars when Alpaca credentials are configured."},
        {"value": "polygon_grouped_daily", "label": "Polygon Grouped Daily", "description": "Batch U.S. EOD refresh for the full market through Polygon grouped daily."},
        {"value": "tushare", "label": "TuShare", "description": "Preferred for A-share historical sync and close-review flows."},
        {"value": "yfinance", "label": "yfinance", "description": "Default for global price sync outside A-shares."},
        {"value": "openbb", "label": "OpenBB", "description": "OpenBB wrapper with fallback behavior when available."},
    ],
    "fundamental": [
        {"value": "auto", "label": "Auto", "description": "CN uses TuShare; US/HK uses OpenBB/yfinance fundamentals."},
        {"value": "tushare", "label": "TuShare", "description": "Preferred for A-share fundamental snapshots."},
        {"value": "openbb", "label": "OpenBB", "description": "Preferred for US/HK fundamental snapshots."},
    ],
    "concept": [
        {"value": "auto", "label": "Auto", "description": "CN concept mapping uses TuShare concept data."},
        {"value": "tushare", "label": "TuShare", "description": "Primary source for A-share concept memberships."},
    ],
}


def _maybe_redirect(redirect_to: str | None, payload: dict):
    if not redirect_to:
        return payload

    query = {
        "job_status": payload.get("status", "unknown"),
        "job_id": payload.get("job_id", ""),
        "job_message": payload.get("message")
        or payload.get("seeded") and f"Seeded {len(payload['seeded'])} symbols"
        or payload.get("predictions_written") and f"Wrote {payload['predictions_written']} predictions"
        or payload.get("daily_rows_written") and f"Wrote {payload['daily_rows_written']} backtest rows"
        or "Job finished",
    }
    separator = "&" if "?" in redirect_to else "?"
    return RedirectResponse(url=f"{redirect_to}{separator}{urlencode(query)}", status_code=303)


async def _request_value(request: Request, key: str, default=None):
    if key in request.query_params:
        return request.query_params.get(key, default)
    form = await request.form()
    return form.get(key, default)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _as_int(value, default: int) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value):
    try:
        return int(value) if value not in (None, "", "0") else None
    except (TypeError, ValueError):
        return None


def _as_optional_int_except(value, blocked_values: set[str] | None = None):
    blocked = blocked_values or set()
    text = str(value).strip().lower() if value is not None else ""
    if value in (None, "") or text in blocked:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_status(result: dict, *, partial_default: bool = True) -> str:
    raw = str(result.get("status") or "").strip().lower()
    if raw in {"success", "failed", "partial", "empty", "not_configured"}:
        return raw if raw != "success" or partial_default else "success"
    return "partial" if partial_default else "success"


def _run_background_job(*, job_id: int, label: str, runner) -> None:
    """Keep long-running operational jobs off the single web-worker request thread."""

    def _work() -> None:
        try:
            with SessionLocal() as worker_db:
                DataJobRepository(worker_db).update_job(
                    job_id,
                    message=f"{label} is running in the background.",
                    progress={"step": "market_refresh"},
                )
            result = runner()
            status = _result_status(result)
            message = str(result.get("message") or f"{label} finished.")
            with SessionLocal() as worker_db:
                DataJobRepository(worker_db).complete_job(
                    job_id,
                    status=status,
                    message=message,
                    result=result,
                )
        except Exception as exc:
            with SessionLocal() as worker_db:
                DataJobRepository(worker_db).complete_job(
                    job_id,
                    status="failed",
                    message=str(exc),
                    result={"error": str(exc)},
                )

    threading.Thread(
        target=_work,
        name=f"background-job-{job_id}",
        daemon=True,
    ).start()


def _existing_running_job(job_repo: DataJobRepository, job_types: set[str]) -> dict | None:
    return next(
        (
            job
            for job in job_repo.list_recent_jobs(limit=100)
            if str(job.get("job_type") or "") in job_types
            and str(job.get("status") or "").lower() == "running"
        ),
        None,
    )


def _precompute_us_screeners_result(*, job_id: int, include_watchlist: bool, lake_only: bool) -> dict:
    with SessionLocal() as worker_db:
        result = refresh_precomputed_screener_snapshots(
            worker_db,
            source_job_id=job_id,
            markets=["US"],
            include_watchlist=include_watchlist,
            lake_only=lake_only,
        )
    result["message"] = (
        f"Precomputed {result.get('count', 0)} U.S. core candidate snapshot(s): "
        "LightGBM, Next Tesla Swing, and Technical Momentum."
    )
    return result


def _train_cn_signals_result(
    *,
    job_id: int,
    run_name: str,
    model_type: str,
    signal_type: str,
    lookback_days: int,
    tickers: list[str],
) -> dict:
    predictions_written = SignalTrainer().train(
        run_name=run_name,
        model_type=model_type,
        signal_type=signal_type,
        lookback_days=lookback_days,
        tickers=tickers,
        market="CN",
        universe="full_market_cn_lake",
    )
    with SessionLocal() as db:
        refresh_workspace_snapshots(db, source_job_id=job_id)
    return {
        "status": "success",
        "message": f"{run_name}: trained {len(tickers)} A-share symbols with {model_type} and wrote {predictions_written} predictions.",
        "predictions_written": predictions_written,
        "run_name": run_name,
        "signal_type": signal_type,
        "lookback_days": lookback_days,
        "ticker_count": len(tickers),
        "market": "CN",
        "model_type": model_type,
    }


def _train_us_signals_result(
    *,
    job_id: int,
    run_name: str,
    model_type: str,
    signal_type: str,
    lookback_days: int,
    top_n: int,
    tickers: list[str],
    raw_ticker_count: int,
    universe_summary: dict,
) -> dict:
    predictions_written = SignalTrainer().train(
        run_name=run_name,
        model_type=model_type,
        signal_type=signal_type,
        lookback_days=lookback_days,
        tickers=tickers,
        market="US",
        universe="full_market_us_lake",
    )
    daily_rows_written = BacktestRunner().run(top_n=max(1, top_n))
    with SessionLocal() as db:
        try:
            refresh_workspace_snapshots(db, source_job_id=job_id)
        except Exception:
            pass
    return {
        "status": "success",
        "message": (
            f"{run_name}: trained {len(tickers)} eligible U.S. common-stock symbols "
            f"({raw_ticker_count} raw) with {model_type}, wrote {predictions_written} predictions "
            f"and {daily_rows_written} backtest rows."
        ),
        "predictions_written": predictions_written,
        "daily_rows_written": daily_rows_written,
        "run_name": run_name,
        "signal_type": signal_type,
        "lookback_days": lookback_days,
        "top_n": top_n,
        "ticker_count": len(tickers),
        "raw_ticker_count": raw_ticker_count,
        "universe_summary": universe_summary,
        "market": "US",
        "model_type": model_type,
    }


def _storage_retention_result(*, keep_runs: int, keep_snapshots: int, apply: bool) -> dict:
    with SessionLocal() as db:
        return clean_model_history(
            db,
            keep_model_runs_per_market=keep_runs,
            keep_workspace_snapshots_per_type=keep_snapshots,
            apply=apply,
        )


def _combine_screener_precompute_batches(*batch_results: dict) -> dict:
    valid_batches = [batch for batch in batch_results if batch]
    total_created = sum(int(batch.get("count", 0) or 0) for batch in valid_batches)
    total_failed = sum(int(batch.get("failed_count", 0) or 0) for batch in valid_batches)
    snapshots_created: list[dict] = []
    failed_items: list[dict] = []
    for batch in valid_batches:
        snapshots_created.extend(list(batch.get("snapshots_created") or []))
        failed_items.extend(list(batch.get("failed_templates") or []))
        failed_items.extend(list(batch.get("failed_presets") or []))
    return {
        "status": "success" if total_created > 0 and total_failed == 0 else "partial" if total_created > 0 else "failed",
        "count": total_created,
        "failed_count": total_failed,
        "snapshots_created": snapshots_created,
        "failed_templates": failed_items,
        "batches": valid_batches,
    }


def _run_cn_precompute_tail_jobs(*, source_job_id: int, parent_job_id: int | None = None) -> None:
    def _run_combo_job() -> None:
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job = job_repo.create_job(
                job_type="screener_precompute_combos",
                status="running",
                params={"markets": ["CN"], "universes": ["full_market"], "depends_on": [parent_job_id] if parent_job_id else []},
                message="Precomputing CN multi-model screener snapshots in the background.",
            )
            job_id = job.id
        try:
            with SessionLocal() as db:
                result = refresh_precomputed_multi_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                )
            with SessionLocal() as db:
                complete_job_and_build_payload(
                    DataJobRepository(db),
                    job_id=job_id,
                    status=_result_status(result),
                    message=f"Precomputed {result.get('count', 0)} CN multi-model screener snapshot(s).",
                    **{key: value for key, value in result.items() if key not in {'status', 'message'}},
                )
        except Exception as exc:
            with SessionLocal() as db:
                fail_job_and_build_payload(DataJobRepository(db), job_id=job_id, exc=exc)

    def _run_rest_job() -> None:
        with SessionLocal() as db:
            job_repo = DataJobRepository(db)
            job = job_repo.create_job(
                job_type="screener_precompute_rest",
                status="running",
                params={"markets": ["CN"], "universes": ["full_market", "watchlist"], "depends_on": [parent_job_id] if parent_job_id else []},
                message="Precomputing secondary CN screener snapshots in the background.",
            )
            job_id = job.id
        try:
            with SessionLocal() as db:
                result_full = refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                    include_watchlist=False,
                    lake_only=False,
                    template_keys=REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES + FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES,
                    universes=["full_market"],
                )
            with SessionLocal() as db:
                result_watchlist = refresh_precomputed_screener_snapshots(
                    db,
                    source_job_id=source_job_id,
                    markets=["CN"],
                    include_watchlist=True,
                    lake_only=False,
                    template_keys=WATCHLIST_PRECOMPUTE_TEMPLATES,
                    universes=["watchlist"],
                )
            result = {
                "status": "success" if int(result_full.get("count", 0) or 0) + int(result_watchlist.get("count", 0) or 0) > 0 else "failed",
                "count": int(result_full.get("count", 0) or 0) + int(result_watchlist.get("count", 0) or 0),
                "failed_count": int(result_full.get("failed_count", 0) or 0) + int(result_watchlist.get("failed_count", 0) or 0),
                "snapshots_created": list(result_full.get("snapshots_created") or []) + list(result_watchlist.get("snapshots_created") or []),
                "failed_templates": list(result_full.get("failed_templates") or []) + list(result_watchlist.get("failed_templates") or []),
                "batches": [
                    {"batch": "cn_full_market_rest", **result_full},
                    {"batch": "watchlist", **result_watchlist},
                ],
            }
            with SessionLocal() as db:
                complete_job_and_build_payload(
                    DataJobRepository(db),
                    job_id=job_id,
                    status=_result_status(result),
                    message=f"Precomputed {result.get('count', 0)} secondary CN screener snapshot(s).",
                    **{key: value for key, value in result.items() if key not in {'status', 'message'}},
                )
        except Exception as exc:
            with SessionLocal() as db:
                fail_job_and_build_payload(DataJobRepository(db), job_id=job_id, exc=exc)

    try:
        _run_combo_job()
    except Exception:
        pass
    try:
        _run_rest_job()
    except Exception:
        pass


@router.get("/templates")
def job_templates(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return [
        {"job_type": "sync_market_data", "description": "Fetch and persist market data through the unified price provider layer."},
        {"job_type": "sync_cn_symbol_universe", "description": "Sync the A-share stock universe into local symbols."},
        {"job_type": "init_cn_market_data", "description": "Initialize A-share market price history for full-market scans."},
        {"job_type": "refresh_cn_market_data", "description": "Refresh recent A-share market prices for daily full-market scans."},
        {"job_type": "refresh_cn_market_data_daily", "description": "Incrementally refresh A-share market prices from each symbol's last synced date."},
        {"job_type": "refresh_cn_market_data_lake_only", "description": "Refresh A-share market prices directly into Parquet lake without generating CSV files."},
        {"job_type": "cn_close_review", "description": "Run the post-close CN incremental refresh, rebuild, AI review, and recommendations pipeline."},
        {"job_type": "train_cn_signals", "description": "Train the LightGBM A-share multifactor signal model from the local CN Parquet market lake and write predictions."},
        {"job_type": "screener_precompute", "description": "Finish the core A-share screener precompute first, then let combo/rest snapshots continue in background child jobs."},
        {"job_type": "screener_precompute_core", "description": "Precompute the core A-share screener templates used by the dashboard and model screens first."},
        {"job_type": "screener_precompute_combos", "description": "Precompute the core multi-model A-share confluence presets after the base templates are ready."},
        {"job_type": "screener_precompute_rest", "description": "Precompute the remaining A-share screener templates and watchlist snapshots in the background."},
        {"job_type": "model_selection_guidance_snapshot", "description": "Persist the latest model-usage guidance snapshot so Dashboard and Model Performance can read it without full-market recompute."},
        {"job_type": "model_calibration_snapshot", "description": "Persist out-of-sample LightGBM execution calibration so expected returns are not based only on in-sample buckets."},
        {"job_type": "kronos_validation", "description": "Validate top model candidates with the optional Kronos K-line foundation-model adapter after screener precompute."},
        {"job_type": "selection_quality_snapshot", "description": "Persist the unified realized hit-rate ledger for AI report candidates and factor experiment runs after the AI daily report."},
        {"job_type": "social_signal_poll", "description": "Poll tracked X accounts every 4 hours and parse ticker mentions automatically."},
        {"job_type": "social_us_price_sync", "description": "Automatically sync Alpaca-backed U.S. prices for tickers mentioned in imported X posts."},
        {"job_type": "precompute_us_screeners", "description": "Precompute U.S. screener snapshots after U.S. close using the locally synced U.S. symbol pool."},
        {"job_type": "refresh_us_grouped_daily", "description": "Refresh U.S. grouped daily EOD bars from Polygon for full-market U.S. scans."},
        {"job_type": "refresh_us_grouped_daily_range", "description": "Refresh a U.S. grouped daily date range directly into Parquet lake without per-symbol CSV files."},
        {"job_type": "us_signal_train", "description": "Train the LightGBM U.S. multifactor signal model from the local U.S. Parquet market lake and write predictions."},
        {"job_type": "cleanup_market_csv", "description": "Dry-run or delete market CSV files that are already covered by Parquet market lake."},
        {"job_type": "rebuild_technical_snapshots", "description": "Cache technical pattern snapshots for faster full-market scans."},
        {"job_type": "sync_cn_fundamentals", "description": "Fetch and persist A-share fundamentals through the unified fundamental provider layer."},
        {"job_type": "sync_cn_concepts", "description": "Fetch and persist A-share concept memberships through the unified concept provider layer."},
        {"job_type": "sync_global_fundamentals", "description": "Fetch and persist US/HK fundamentals through the unified fundamental provider layer."},
        {"job_type": "build_dataset", "description": "Normalize price files and build a Qlib dataset."},
        {"job_type": "train_model", "description": "Train a signal model with Qlib."},
        {"job_type": "import_model_output", "description": "Import external model predictions into predictions and model details."},
        {"job_type": "run_backtest", "description": "Run a backtest from stored predictions."},
    ]


@router.get("/provider-options")
def provider_options(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return PROVIDER_OPTIONS


@router.get("/sync-states")
def sync_states(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = PriceSyncStateRepository(db)
    return repo.list_states_with_symbols()


@router.get("/recent")
def recent_jobs(request: Request, limit: int = 20, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = DataJobRepository(db)
    return repo.list_recent_jobs(limit=limit)


@router.get("/auto-analysis")
def auto_analysis_status(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return auto_analysis_service.get_status()


@router.get("/close-review")
def close_review_status(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return close_review_scheduler_service.get_status()


@router.post("/send-ai-daily-report")
async def send_ai_daily_report(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    channels_raw = str(await _request_value(request, "channels", "")).strip()
    selected_channels = [item.strip().lower() for item in channels_raw.split(",") if item.strip()]
    force_send = _as_bool(await _request_value(request, "force_send", False), default=False)

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="send_ai_daily_report",
        status="running",
        params={"channels": selected_channels or None, "force_send": force_send},
    )
    try:
        report = load_ai_daily_report()
        if report is None:
            report = build_ai_daily_report(limit=8)
            save_ai_daily_report(report)
        market_meta = report.get("market_recommendations_meta") or {}
        market_status = str(market_meta.get("status") or "").strip().lower()
        if market_status in {"fallback", "not_ready"} and not force_send:
            note = str(market_meta.get("note") or "").strip() or "今日 A股全市场候选尚未就绪。"
            payload = complete_job_and_build_payload(
                job_repo,
                job_id=job.id,
                status="failed",
                message=f"Skipped AI daily report send: {note}",
                blocked_reason="market_recommendations_not_ready",
                market_recommendations_meta=market_meta,
            )
            return _maybe_redirect(redirect_to, payload)
        notifier = PushNotificationService()
        push_messages = render_ai_daily_report_push_messages(report)
        sent: list[str] = []
        failed: list[dict] = []
        results: list[dict] = []
        for message_item in push_messages:
            result = notifier.send_event(
                event_type="stock_recommendation",
                title=message_item["title"],
                body=message_item["body"],
                channels=selected_channels or None,
            )
            results.append({"title": message_item["title"], **result})
            sent.extend(item for item in (result.get("sent") or []) if item not in sent)
            failed.extend(result.get("failed") or [])
        result = {
            "status": "success" if sent and not failed else "partial" if sent else "failed",
            "sent": sent,
            "failed": failed,
            "messages": results,
            "forced": force_send,
        }
        message = (
            f"Sent A-share AI daily report as {len(push_messages)} message(s) to {', '.join(result['sent'])}"
            if result.get("sent")
            else "No AI daily report channels were available."
        )
        if force_send and market_status in {"fallback", "not_ready"}:
            message = f"{message} (forced while market candidates were {market_status})"
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=result["status"],
            message=message,
            push_result=result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/seed-sample-data")
async def run_seed_sample_data(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(job_type="seed_sample_data", status="running")
    try:
        results = seed_sample_data()
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=f"Seeded {len(results)} symbols",
            seeded=results,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/sync-market-data")
async def run_sync_market_data(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    tickers_raw = await _request_value(request, "tickers", "")
    provider = str(await _request_value(request, "provider", "auto")).strip() or "auto"
    start_date = await _request_value(request, "start_date")
    end_date = await _request_value(request, "end_date")
    tickers = [item.strip().upper() for item in str(tickers_raw).split(",") if item.strip()] or None

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="sync_market_data",
        status="running",
        params={
            "tickers": tickers,
            "provider": provider,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
    try:
        results = sync_market_data(
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            provider=provider,
        )
        success_count = sum(1 for item in results if item["status"] == "success")
        failure_count = sum(1 for item in results if item["status"] == "failed")
        status = "success" if failure_count == 0 else "partial"
        message = f"Synced {success_count} symbol(s)" + (f", {failure_count} failed" if failure_count else "")
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=message,
            results=results,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/precompute-us-screeners")
async def run_precompute_us_screeners(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    include_watchlist = _as_bool(await _request_value(request, "include_watchlist", False), default=False)
    lake_only = _as_bool(await _request_value(request, "lake_only", True), default=True)
    job_repo = DataJobRepository(db)
    existing = _existing_running_job(job_repo, {"precompute_us_screeners"})
    if existing:
        return _maybe_redirect(
            redirect_to,
            build_job_payload(
                status="running",
                job_id=existing.get("id"),
                message="U.S. candidate precompute is already running in the background.",
            ),
        )
    job = job_repo.create_job(
        job_type="precompute_us_screeners",
        status="running",
        params={"markets": ["US"], "include_watchlist": include_watchlist, "lake_only": lake_only},
    )
    _run_background_job(
        job_id=job.id,
        label="U.S. candidate precompute",
        runner=lambda: _precompute_us_screeners_result(
            job_id=job.id,
            include_watchlist=include_watchlist,
            lake_only=lake_only,
        ),
    )
    return _maybe_redirect(
        redirect_to,
        build_job_payload(
            status="running",
            job_id=job.id,
            message="U.S. candidate precompute started in the background.",
        ),
    )


@router.post("/precompute-cn-screeners")
async def run_precompute_cn_screeners(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="screener_precompute",
        status="running",
        params={"markets": ["CN"], "universes": ["full_market", "watchlist"], "mode": "staged"},
    )
    try:
        result_core = refresh_precomputed_screener_snapshots(
            db,
            source_job_id=job.id,
            markets=["CN"],
            include_watchlist=False,
            lake_only=False,
            template_keys=CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
            universes=["full_market"],
        )
        result = {
            "status": _result_status(result_core),
            "count": int(result_core.get("count", 0) or 0),
            "failed_count": int(result_core.get("failed_count", 0) or 0),
            "snapshots_created": list(result_core.get("snapshots_created") or []),
            "failed_templates": list(result_core.get("failed_templates") or []),
            "batches": [{"batch": "core", **result_core}],
            "tail_jobs_scheduled": True,
        }
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=_result_status(result),
            message=(
                f"Core CN screener precompute finished: {result.get('count', 0)} snapshot(s); "
                f"combo and rest jobs continue in background."
            ),
            **{key: value for key, value in result.items() if key not in {"status", "message"}},
        )
        threading.Thread(
            target=_run_cn_precompute_tail_jobs,
            kwargs={"source_job_id": job.id, "parent_job_id": job.id},
            name=f"manual-cn-precompute-tail-{job.id}",
            daemon=True,
        ).start()
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/precompute-cn-screeners-core")
async def run_precompute_cn_screeners_core(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="screener_precompute_core",
        status="running",
        params={"markets": ["CN"], "universes": ["full_market"]},
    )
    try:
        result = refresh_precomputed_screener_snapshots(
            db,
            source_job_id=job.id,
            markets=["CN"],
            include_watchlist=False,
            lake_only=False,
            template_keys=CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
            universes=["full_market"],
        )
        status = _result_status(result)
        extra = {key: value for key, value in result.items() if key not in {"status", "message"}}
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=f"Precomputed {result.get('count', 0)} core CN screener snapshot(s).",
            **extra,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/precompute-cn-screeners-combos")
async def run_precompute_cn_screeners_combos(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="screener_precompute_combos",
        status="running",
        params={"markets": ["CN"], "universes": ["full_market"]},
    )
    try:
        result = refresh_precomputed_multi_screener_snapshots(
            db,
            source_job_id=job.id,
            markets=["CN"],
        )
        status = _result_status(result)
        extra = {key: value for key, value in result.items() if key not in {"status", "message"}}
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=f"Precomputed {result.get('count', 0)} CN multi-model screener snapshot(s).",
            **extra,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/precompute-cn-screeners-rest")
async def run_precompute_cn_screeners_rest(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="screener_precompute_rest",
        status="running",
        params={"markets": ["CN"], "universes": ["full_market", "watchlist"]},
    )
    try:
        result_full = refresh_precomputed_screener_snapshots(
            db,
            source_job_id=job.id,
            markets=["CN"],
            include_watchlist=False,
            lake_only=False,
            template_keys=REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES + FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES,
            universes=["full_market"],
        )
        result_watchlist = refresh_precomputed_screener_snapshots(
            db,
            source_job_id=job.id,
            markets=["CN"],
            include_watchlist=True,
            lake_only=False,
            template_keys=WATCHLIST_PRECOMPUTE_TEMPLATES,
            universes=["watchlist"],
        )
        result = {
            "status": "success" if int(result_full.get("count", 0) or 0) + int(result_watchlist.get("count", 0) or 0) > 0 else "failed",
            "count": int(result_full.get("count", 0) or 0) + int(result_watchlist.get("count", 0) or 0),
            "failed_count": int(result_full.get("failed_count", 0) or 0) + int(result_watchlist.get("failed_count", 0) or 0),
            "batches": [
                {"batch": "cn_full_market_rest", **result_full},
                {"batch": "watchlist", **result_watchlist},
            ],
        }
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=_result_status(result),
            message=f"Precomputed {result.get('count', 0)} secondary CN screener snapshot(s).",
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/model-selection-guidance-snapshot")
async def run_model_selection_guidance_snapshot(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    markets_raw = str(await _request_value(request, "markets", "")).strip()
    market_raw = str(await _request_value(request, "market", "")).strip()
    requested_markets = [
        item.strip().upper()
        for item in (markets_raw or market_raw or "CN").split(",")
        if item.strip()
    ] or ["CN"]
    markets = [item for item in requested_markets if item in {"CN", "US", "ALL"}] or ["CN"]
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="model_selection_guidance_snapshot",
        status="running",
        params={"markets": markets},
    )
    try:
        snapshots = save_model_selection_guidance_snapshots(
            db,
            source_job_id=job.id,
            markets=markets,
        )
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success" if snapshots else "failed",
            message=f"Saved {len(snapshots)} model selection guidance snapshot(s).",
            markets=list(snapshots.keys()),
            snapshots=snapshots,
            count=len(snapshots),
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/kronos-validation")
async def run_kronos_validation(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    markets_raw = str(await _request_value(request, "markets", "CN")).strip()
    requested_markets = [item.strip().upper() for item in markets_raw.split(",") if item.strip()]
    markets = [item for item in requested_markets if item in {"CN", "US"}] or ["CN"]
    candidate_limit = _as_int(await _request_value(request, "candidate_limit", 60), 60)
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type=KRONOS_VALIDATION_JOB_TYPE,
        status="running",
        params={"markets": markets, "candidate_limit": candidate_limit},
        message="Validating top model candidates with Kronos adapter.",
    )
    try:
        result = save_kronos_validation_snapshot(
            db=db,
            source_job_id=job.id,
            markets=markets,
            candidate_limit=candidate_limit,
        )
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=(
                f"Kronos validation snapshot saved: {result.get('candidate_count', 0)} candidate(s), "
                f"status {result.get('status') or status}."
            ),
            **{key: value for key, value in result.items() if key not in {"status", "message"}},
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/train-us-signals")
async def run_train_us_signals(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    run_name = str(await _request_value(request, "run_name", "us_close_lightgbm")).strip() or "us_close_lightgbm"
    model_type = str(await _request_value(request, "model_type", "lightgbm")).strip() or "lightgbm"
    signal_type = str(await _request_value(request, "signal_type", "momentum")).strip() or "momentum"
    lookback_days = _as_int(await _request_value(request, "lookback_days", 3), 3)
    top_n = _as_int(await _request_value(request, "top_n", 5), 5)
    background = _as_bool(await _request_value(request, "background", False), default=False)
    raw_us_tickers = sorted(list_lake_symbols(market="US"))
    us_tickers, universe_summary = build_us_trade_universe(tickers=raw_us_tickers, include_summary=True)
    job_repo = DataJobRepository(db)
    if background:
        existing = _existing_running_job(job_repo, {"us_signal_train"})
        if existing:
            return _maybe_redirect(
                redirect_to,
                build_job_payload(status="running", job_id=existing.get("id"), message="A U.S. model training job is already running in the background."),
            )
    job = job_repo.create_job(
        job_type="us_signal_train",
        status="running",
        params={
            "run_name": run_name,
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "top_n": top_n,
            "market": "US",
            "ticker_count": len(us_tickers),
            "raw_ticker_count": len(raw_us_tickers),
            "universe_summary": universe_summary,
            "model_type": model_type,
        },
    )
    if not us_tickers:
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="failed",
            message="No eligible U.S. common-stock symbols found after universe cleaning. Refresh U.S. grouped daily or relax universe thresholds.",
            predictions_written=0,
            ticker_count=0,
            raw_ticker_count=len(raw_us_tickers),
            universe_summary=universe_summary,
            market="US",
        )
        return _maybe_redirect(redirect_to, payload)
    if background:
        _run_background_job(
            job_id=job.id,
            label="U.S. signal training",
            runner=lambda: _train_us_signals_result(
                job_id=job.id,
                run_name=run_name,
                model_type=model_type,
                signal_type=signal_type,
                lookback_days=lookback_days,
                top_n=top_n,
                tickers=us_tickers,
                raw_ticker_count=len(raw_us_tickers),
                universe_summary=universe_summary,
            ),
        )
        return _maybe_redirect(
            redirect_to,
            build_job_payload(status="running", job_id=job.id, message="U.S. signal training started in the background. The task center remains available."),
        )
    trainer = SignalTrainer()
    runner = BacktestRunner()
    try:
        predictions_written = trainer.train(
            run_name=run_name,
            model_type=model_type,
            signal_type=signal_type,
            lookback_days=lookback_days,
            tickers=us_tickers,
            market="US",
            universe="full_market_us_lake",
        )
        daily_rows_written = runner.run(top_n=max(1, top_n))
        message = (
            f"{run_name}: trained {len(us_tickers)} eligible U.S. common-stock symbols "
            f"({len(raw_us_tickers)} raw) with {model_type}, wrote {predictions_written} predictions "
            f"and {daily_rows_written} backtest rows."
        )
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            predictions_written=predictions_written,
            daily_rows_written=daily_rows_written,
            run_name=run_name,
            signal_type=signal_type,
            lookback_days=lookback_days,
            ticker_count=len(us_tickers),
            raw_ticker_count=len(raw_us_tickers),
            universe_summary=universe_summary,
            market="US",
            model_type=model_type,
        )
        try:
            refresh_workspace_snapshots(db, source_job_id=job.id)
        except Exception:
            # Presentation cache refresh is best-effort; the training job should
            # stay green once predictions and backtest rows are written.
            pass
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/train-cn-signals")
async def run_train_cn_signals(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    run_name = str(await _request_value(request, "run_name", "cn_close_lightgbm")).strip() or "cn_close_lightgbm"
    model_type = str(await _request_value(request, "model_type", "lightgbm")).strip() or "lightgbm"
    signal_type = str(await _request_value(request, "signal_type", "momentum")).strip() or "momentum"
    lookback_days = _as_int(await _request_value(request, "lookback_days", 3), 3)
    background = _as_bool(await _request_value(request, "background", False), default=False)
    cn_tickers = sorted(list_lake_symbols(market="CN"))
    job_repo = DataJobRepository(db)
    if background:
        existing = _existing_running_job(job_repo, {"train_cn_signals"})
        if existing:
            return _maybe_redirect(
                redirect_to,
                build_job_payload(
                    status="running",
                    job_id=existing.get("id"),
                    message="An A-share model training job is already running in the background.",
                ),
            )
    job = job_repo.create_job(
        job_type="train_cn_signals",
        status="running",
        params={
            "run_name": run_name,
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "market": "CN",
            "ticker_count": len(cn_tickers),
            "model_type": model_type,
        },
    )
    if not cn_tickers:
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="failed",
            message="No A-share symbols found in the Parquet market lake. Refresh CN market data first.",
            predictions_written=0,
            ticker_count=0,
            market="CN",
        )
        return _maybe_redirect(redirect_to, payload)
    if background:
        _run_background_job(
            job_id=job.id,
            label="A-share signal training",
            runner=lambda: _train_cn_signals_result(
                job_id=job.id,
                run_name=run_name,
                model_type=model_type,
                signal_type=signal_type,
                lookback_days=lookback_days,
                tickers=cn_tickers,
            ),
        )
        return _maybe_redirect(
            redirect_to,
            build_job_payload(
                status="running",
                job_id=job.id,
                message="A-share signal training started in the background. The task center remains available.",
            ),
        )
    try:
        result = _train_cn_signals_result(
            job_id=job.id,
            run_name=run_name,
            model_type=model_type,
            signal_type=signal_type,
            lookback_days=lookback_days,
            tickers=cn_tickers,
        )
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=result["message"],
            **{key: value for key, value in result.items() if key not in {"status", "message"}},
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/refresh-us-grouped-daily")
async def run_refresh_us_grouped_daily(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    trade_date = str(await _request_value(request, "trade_date", "") or "").strip() or None
    adjusted = _as_bool(await _request_value(request, "adjusted", True), default=True)
    normalize = _as_bool(await _request_value(request, "normalize", False), default=False)
    persist_per_symbol = _as_bool(await _request_value(request, "persist_per_symbol", False), default=False)
    write_lake = _as_bool(await _request_value(request, "write_lake", True), default=True)
    write_snapshot = _as_bool(await _request_value(request, "write_snapshot", False), default=False)
    background = _as_bool(await _request_value(request, "background", False), default=False)
    limit = _as_optional_int(await _request_value(request, "limit", None))
    job_repo = DataJobRepository(db)
    if background:
        existing = _existing_running_job(job_repo, {"refresh_us_grouped_daily"})
        if existing:
            return _maybe_redirect(
                redirect_to,
                build_job_payload(
                    status="running",
                    job_id=existing.get("id"),
                    message="A U.S. price refresh is already running in the background.",
                ),
            )
    job = job_repo.create_job(
        job_type="refresh_us_grouped_daily",
        status="running",
        params={
            "trade_date": trade_date,
            "adjusted": adjusted,
            "limit": limit,
            "normalize": normalize,
            "persist_per_symbol": persist_per_symbol,
            "write_lake": write_lake,
            "write_snapshot": write_snapshot,
        },
    )
    if background:
        _run_background_job(
            job_id=job.id,
            label="U.S. grouped daily price refresh",
            runner=lambda: refresh_us_grouped_daily(
                trade_date=trade_date,
                adjusted=adjusted,
                limit=limit,
                normalize=normalize,
                persist_per_symbol=persist_per_symbol,
                write_lake=write_lake,
                write_snapshot=write_snapshot,
            ),
        )
        return _maybe_redirect(
            redirect_to,
            build_job_payload(
                status="running",
                job_id=job.id,
                message="U.S. price refresh started in the background. The task center remains available.",
            ),
        )
    try:
        result = refresh_us_grouped_daily(
            trade_date=trade_date,
            adjusted=adjusted,
            limit=limit,
            normalize=normalize,
            persist_per_symbol=persist_per_symbol,
            write_lake=write_lake,
            write_snapshot=write_snapshot,
        )
        status = _result_status(result)
        extra = {key: value for key, value in result.items() if key not in {"status", "message"}}
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result.get("message") or "U.S. grouped daily refresh finished.",
            **extra,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/refresh-us-grouped-daily-range")
async def run_refresh_us_grouped_daily_range(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    start_date = str(await _request_value(request, "start_date", "") or "").strip()
    end_date = str(await _request_value(request, "end_date", "") or "").strip()
    limit = _as_optional_int(await _request_value(request, "limit", None))
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="refresh_us_grouped_daily_range",
        status="running",
        params={"start_date": start_date, "end_date": end_date, "limit": limit},
    )
    try:
        result = refresh_us_grouped_daily_range(start_date=start_date, end_date=end_date, limit=limit)
        status = _result_status(result)
        extra = {key: value for key, value in result.items() if key not in {"status", "message"}}
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result.get("message") or "U.S. grouped daily range refresh finished.",
            **extra,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/refresh-cn-market-data-lake-only")
async def run_refresh_cn_market_data_lake_only(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    start_date = str(await _request_value(request, "start_date", "") or "").strip()
    end_date = str(await _request_value(request, "end_date", "") or "").strip() or None
    limit = _as_optional_int(await _request_value(request, "limit", None))
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="refresh_cn_market_data_lake_only",
        status="running",
        params={"start_date": start_date, "end_date": end_date, "limit": limit},
    )
    try:
        result = refresh_cn_market_data_lake_only(start_date=start_date, end_date=end_date, limit=limit)
        status = _result_status(result)
        extra = {key: value for key, value in result.items() if key not in {"status", "message"}}
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result.get("message") or "CN lake-only refresh finished.",
            **extra,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/cleanup-market-csv")
async def run_cleanup_market_csv(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    dry_run = _as_bool(await _request_value(request, "dry_run", True), default=True)
    confirm = str(await _request_value(request, "confirm", "") or "").strip() or None
    markets_raw = str(await _request_value(request, "markets", "CN,US") or "CN,US")
    markets = [item.strip().upper() for item in markets_raw.split(",") if item.strip()]
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="cleanup_market_csv",
        status="running",
        params={"dry_run": dry_run, "markets": markets, "confirm": bool(confirm)},
    )
    try:
        result = cleanup_market_csv_files(dry_run=dry_run, confirm=confirm, markets=markets)
        status = _result_status(result)
        extra = {key: value for key, value in result.items() if key not in {"status", "message"}}
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result.get("message") or "CSV cleanup finished.",
            **extra,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.get("/market-lake/us-daily-summary")
def market_lake_us_daily_summary(request: Request, trade_date: str | None = None, limit: int = 10):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return query_us_daily_summary(trade_date=trade_date, limit=limit)


@router.post("/sync-cn-symbol-universe")
async def run_sync_cn_symbol_universe(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="sync_cn_symbol_universe",
        status="running",
    )
    try:
        result = sync_cn_symbol_universe()
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/init-cn-market-data")
async def run_init_cn_market_data(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    provider = str(await _request_value(request, "provider", "auto")).strip() or "auto"
    days_back_raw = await _request_value(request, "days_back", 180)
    offset_raw = await _request_value(request, "offset", 0)
    batch_size_raw = await _request_value(request, "batch_size", None)
    limit_raw = await _request_value(request, "limit", None)
    pending_only_raw = await _request_value(request, "pending_only", None)
    retry_failed_raw = await _request_value(request, "retry_failed", None)
    days_back = _as_int(days_back_raw, 180)
    offset = _as_int(offset_raw, 0)
    batch_size = _as_optional_int(batch_size_raw)
    limit = _as_optional_int(limit_raw)
    pending_only = _as_bool(pending_only_raw, default=False)
    retry_failed = _as_bool(retry_failed_raw, default=False)

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="init_cn_market_data",
        status="running",
        params={
            "days_back": days_back,
            "offset": offset,
            "batch_size": batch_size,
            "limit": limit,
            "pending_only": pending_only,
            "retry_failed": retry_failed,
            "provider": provider,
        },
    )
    try:
        result = init_cn_market_data(
            days_back=days_back,
            offset=offset,
            batch_size=batch_size,
            limit=limit,
            pending_only=pending_only,
            retry_failed=retry_failed,
            provider=provider,
        )
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/refresh-cn-market-data")
async def run_refresh_cn_market_data(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    provider = str(await _request_value(request, "provider", "auto")).strip() or "auto"
    days_back_raw = await _request_value(request, "days_back", 7)
    limit_raw = await _request_value(request, "limit", None)
    incremental_raw = await _request_value(request, "incremental", None)
    overlap_days_raw = await _request_value(request, "overlap_days", 3)
    days_back = _as_int(days_back_raw, 7)
    limit = _as_optional_int(limit_raw)
    overlap_days = _as_int(overlap_days_raw, 3)
    incremental = _as_bool(incremental_raw, default=False)
    background = _as_bool(await _request_value(request, "background", False), default=False)

    job_repo = DataJobRepository(db)
    if background:
        existing = _existing_running_job(job_repo, {"refresh_cn_market_data", "refresh_cn_market_data_daily"})
        if existing:
            return _maybe_redirect(
                redirect_to,
                build_job_payload(
                    status="running",
                    job_id=existing.get("id"),
                    message="An A-share price refresh is already running in the background.",
                ),
            )
    job = job_repo.create_job(
        job_type="refresh_cn_market_data_daily" if incremental else "refresh_cn_market_data",
        status="running",
        params={
            "days_back": days_back,
            "limit": limit,
            "provider": provider,
            "incremental": incremental,
            "overlap_days": overlap_days,
        },
    )
    if background:
        _run_background_job(
            job_id=job.id,
            label="A-share price refresh",
            runner=lambda: refresh_cn_market_data(
                days_back=days_back,
                limit=limit,
                provider=provider,
                incremental=incremental,
                overlap_days=overlap_days,
            ),
        )
        return _maybe_redirect(
            redirect_to,
            build_job_payload(
                status="running",
                job_id=job.id,
                message="A-share price refresh started in the background. The task center remains available.",
            ),
        )
    try:
        result = refresh_cn_market_data(
            days_back=days_back,
            limit=limit,
            provider=provider,
            incremental=incremental,
            overlap_days=overlap_days,
        )
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/refresh-cn-market-data-daily")
async def run_refresh_cn_market_data_daily(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    provider = str(await _request_value(request, "provider", "auto")).strip() or "auto"
    days_back_raw = await _request_value(request, "days_back", 7)
    limit_raw = await _request_value(request, "limit", None)
    overlap_days_raw = await _request_value(request, "overlap_days", 3)
    days_back = _as_int(days_back_raw, 7)
    limit = _as_optional_int(limit_raw)
    overlap_days = _as_int(overlap_days_raw, 3)
    background = _as_bool(await _request_value(request, "background", False), default=False)

    job_repo = DataJobRepository(db)
    if background:
        existing = _existing_running_job(job_repo, {"refresh_cn_market_data", "refresh_cn_market_data_daily"})
        if existing:
            return _maybe_redirect(
                redirect_to,
                build_job_payload(
                    status="running",
                    job_id=existing.get("id"),
                    message="An A-share price refresh is already running in the background.",
                ),
            )
    job = job_repo.create_job(
        job_type="refresh_cn_market_data_daily",
        status="running",
        params={
            "days_back": days_back,
            "limit": limit,
            "provider": provider,
            "overlap_days": overlap_days,
        },
    )
    if background:
        _run_background_job(
            job_id=job.id,
            label="A-share incremental price refresh",
            runner=lambda: refresh_cn_market_data_daily(
                days_back=days_back,
                limit=limit,
                provider=provider,
                overlap_days=overlap_days,
            ),
        )
        return _maybe_redirect(
            redirect_to,
            build_job_payload(
                status="running",
                job_id=job.id,
                message="A-share incremental price refresh started in the background. The task center remains available.",
            ),
        )
    try:
        result = refresh_cn_market_data_daily(
            days_back=days_back,
            limit=limit,
            provider=provider,
            overlap_days=overlap_days,
        )
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/cleanup-storage-retention")
async def run_cleanup_storage_retention(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    keep_runs = _as_int(await _request_value(request, "keep_model_runs_per_market", 20), 20)
    keep_snapshots = _as_int(await _request_value(request, "keep_workspace_snapshots_per_type", 10), 10)
    confirm = str(await _request_value(request, "confirm", "") or "").strip().upper()
    apply = confirm == "PURGE"
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="cleanup_storage_retention",
        status="running",
        params={"keep_model_runs_per_market": keep_runs, "keep_workspace_snapshots_per_type": keep_snapshots, "apply": apply},
    )
    _run_background_job(
        job_id=job.id,
        label="Storage retention cleanup" if apply else "Storage retention preview",
        runner=lambda: _storage_retention_result(keep_runs=keep_runs, keep_snapshots=keep_snapshots, apply=apply),
    )
    return _maybe_redirect(
        redirect_to,
        build_job_payload(
            status="running",
            job_id=job.id,
            message=("Storage cleanup started in the background." if apply else "Storage-retention preview started in the background."),
        ),
    )


@router.post("/rebuild-technical-snapshots")
async def run_rebuild_technical_snapshots(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    market = str(await _request_value(request, "market", "CN")).strip().upper() or "CN"
    limit_raw = await _request_value(request, "limit", None)
    limit = _as_optional_int(limit_raw)

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="rebuild_technical_snapshots",
        status="running",
        params={"market": market, "limit": limit},
    )
    try:
        result = rebuild_technical_snapshots(market=market, limit=limit)
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/build-dataset")
async def run_build_dataset(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    normalize_only_raw = await _request_value(request, "normalize_only", False)
    normalize_only = str(normalize_only_raw).lower() in {"1", "true", "yes", "on"}
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="build_dataset",
        status="running",
        params={"normalize_only": normalize_only},
    )
    try:
        result = build_dataset(normalize_only=normalize_only)
        if result.get("qlib_built"):
            message = f"Built dataset with {len(result['normalized_files'])} normalized files"
            payload = complete_job_and_build_payload(
                job_repo,
                job_id=job.id,
                status="success",
                message=message,
                **result,
            )
        elif result.get("message"):
            payload = complete_job_and_build_payload(
                job_repo,
                job_id=job.id,
                status="partial",
                message=result["message"],
                **result,
            )
        else:
            message = f"Normalized {len(result['normalized_files'])} files"
            payload = complete_job_and_build_payload(
                job_repo,
                job_id=job.id,
                status="success",
                message=message,
                **result,
            )
    except RuntimeError as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)
    return _maybe_redirect(redirect_to, payload)


@router.post("/sync-cn-fundamentals")
async def run_sync_cn_fundamentals(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    tickers_raw = await _request_value(request, "tickers", "")
    tickers = [item.strip().upper() for item in str(tickers_raw).split(",") if item.strip()] or None
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="sync_cn_fundamentals",
        status="running",
        params={"tickers": tickers},
    )
    try:
        result = sync_cn_fundamentals(tickers=tickers)
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/sync-cn-concepts")
async def run_sync_cn_concepts(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    tickers_raw = await _request_value(request, "tickers", "")
    tickers = [item.strip().upper() for item in str(tickers_raw).split(",") if item.strip()] or None
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="sync_cn_concepts",
        status="running",
        params={"tickers": tickers},
    )
    try:
        result = sync_cn_concepts(tickers=tickers)
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/sync-global-fundamentals")
async def run_sync_global_fundamentals(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    tickers_raw = await _request_value(request, "tickers", "")
    tickers = [item.strip().upper() for item in str(tickers_raw).split(",") if item.strip()] or None
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="sync_global_fundamentals",
        status="running",
        params={"tickers": tickers},
    )
    try:
        result = sync_global_fundamentals(tickers=tickers)
        status = _result_status(result)
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status=status,
            message=result["message"],
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/train")
async def run_train(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    run_name = await _request_value(request, "run_name", "lightgbm_momentum")
    run_name = str(run_name).strip() or "lightgbm_momentum"
    model_type = str(await _request_value(request, "model_type", "lightgbm")).strip() or "lightgbm"
    signal_type = str(await _request_value(request, "signal_type", "momentum")).strip() or "momentum"
    lookback_raw = await _request_value(request, "lookback_days", 3)
    lookback_days = _as_int(lookback_raw, 3)
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="train",
        status="running",
        params={"run_name": run_name, "model_type": model_type, "signal_type": signal_type, "lookback_days": lookback_days},
    )
    trainer = SignalTrainer()
    try:
        count = trainer.train(run_name=run_name, model_type=model_type, signal_type=signal_type, lookback_days=lookback_days)
        message = f"{run_name}: wrote {count} predictions ({model_type}, {signal_type}, {lookback_days}d)"
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            predictions_written=count,
            run_name=run_name,
            model_type=model_type,
            signal_type=signal_type,
            lookback_days=lookback_days,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/backtest")
async def run_backtest(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    top_n_raw = await _request_value(request, "top_n", 1)
    top_n = _as_int(top_n_raw, 1)
    holding_days = _as_int(await _request_value(request, "holding_days", 3), 3)
    commission_bps = _as_float(await _request_value(request, "commission_bps", 8.0), 8.0)
    slippage_bps = _as_float(await _request_value(request, "slippage_bps", 12.0), 12.0)
    model_type = str(await _request_value(request, "model_type", "lightgbm")).strip() or "lightgbm"
    max_position_weight = _as_float(await _request_value(request, "max_position_weight", 0.2), 0.2)
    min_signal_score = _as_float(await _request_value(request, "min_signal_score", 0.05), 0.05)
    max_sector_weight = _as_float(await _request_value(request, "max_sector_weight", 0.35), 0.35)
    min_adv = _as_float(await _request_value(request, "min_adv", 50000000.0), 50000000.0)
    max_gap_pct = _as_float(await _request_value(request, "max_gap_pct", 0.08), 0.08)
    rebalance_threshold = _as_float(await _request_value(request, "rebalance_threshold", 0.02), 0.02)
    benchmark_symbol_raw = await _request_value(request, "benchmark_symbol")
    benchmark_symbol = str(benchmark_symbol_raw).strip().upper() if benchmark_symbol_raw not in (None, "") else None
    model_run_raw = await _request_value(request, "model_run_id")
    model_run_id = _as_optional_int_except(model_run_raw, {"latest"})
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="backtest",
        status="running",
        params={
            "top_n": top_n,
            "model_run_id": model_run_id,
            "holding_days": holding_days,
            "commission_bps": commission_bps,
            "slippage_bps": slippage_bps,
            "max_position_weight": max_position_weight,
            "min_signal_score": min_signal_score,
            "benchmark_symbol": benchmark_symbol,
            "max_sector_weight": max_sector_weight,
            "min_adv": min_adv,
            "max_gap_pct": max_gap_pct,
            "rebalance_threshold": rebalance_threshold,
        },
    )
    runner = BacktestRunner()
    try:
        count = runner.run(
            top_n=top_n,
            model_run_id=model_run_id,
            holding_days=holding_days,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            max_position_weight=max_position_weight,
            min_signal_score=min_signal_score,
            benchmark_symbol=benchmark_symbol,
            max_sector_weight=max_sector_weight,
            min_adv=min_adv,
            max_gap_pct=max_gap_pct,
            rebalance_threshold=rebalance_threshold,
        )
        run_label = f"model_run_id={model_run_id}" if model_run_id is not None else "latest_model"
        benchmark_label = benchmark_symbol or "universe_equal_weight"
        message = f"Wrote {count} backtest rows ({run_label}, top_n={top_n}, benchmark={benchmark_label})"
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            daily_rows_written=count,
            top_n=top_n,
            model_run_id=model_run_id,
            holding_days=holding_days,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            max_position_weight=max_position_weight,
            min_signal_score=min_signal_score,
            benchmark_symbol=benchmark_symbol,
            max_sector_weight=max_sector_weight,
            min_adv=min_adv,
            max_gap_pct=max_gap_pct,
            rebalance_threshold=rebalance_threshold,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = build_job_payload(
            status="failed",
            job_id=job.id,
            message=str(exc),
            top_n=top_n,
            model_run_id=model_run_id,
            holding_days=holding_days,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            max_position_weight=max_position_weight,
            min_signal_score=min_signal_score,
            benchmark_symbol=benchmark_symbol,
            max_sector_weight=max_sector_weight,
            min_adv=min_adv,
            max_gap_pct=max_gap_pct,
            rebalance_threshold=rebalance_threshold,
        )
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        return _maybe_redirect(redirect_to, payload)


@router.post("/run-pipeline")
async def run_pipeline(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    tickers_raw = await _request_value(request, "tickers", "")
    provider = str(await _request_value(request, "provider", "auto")).strip() or "auto"
    start_date = await _request_value(request, "start_date")
    end_date = await _request_value(request, "end_date")
    run_name = str(await _request_value(request, "run_name", "pipeline_run")).strip() or "pipeline_run"
    signal_type = str(await _request_value(request, "signal_type", "momentum")).strip() or "momentum"
    lookback_raw = await _request_value(request, "lookback_days", 3)
    top_n_raw = await _request_value(request, "top_n", 1)
    lookback_days = _as_int(lookback_raw, 3)
    top_n = _as_int(top_n_raw, 1)
    holding_days = _as_int(await _request_value(request, "holding_days", 3), 3)
    commission_bps = _as_float(await _request_value(request, "commission_bps", 8.0), 8.0)
    slippage_bps = _as_float(await _request_value(request, "slippage_bps", 12.0), 12.0)
    max_position_weight = _as_float(await _request_value(request, "max_position_weight", 0.2), 0.2)
    min_signal_score = _as_float(await _request_value(request, "min_signal_score", 0.05), 0.05)
    max_sector_weight = _as_float(await _request_value(request, "max_sector_weight", 0.35), 0.35)
    min_adv = _as_float(await _request_value(request, "min_adv", 50000000.0), 50000000.0)
    max_gap_pct = _as_float(await _request_value(request, "max_gap_pct", 0.08), 0.08)
    rebalance_threshold = _as_float(await _request_value(request, "rebalance_threshold", 0.02), 0.02)
    benchmark_symbol_raw = await _request_value(request, "benchmark_symbol")
    benchmark_symbol = str(benchmark_symbol_raw).strip().upper() if benchmark_symbol_raw not in (None, "") else None
    model_run_raw = await _request_value(request, "model_run_id")
    model_run_id = _as_optional_int_except(model_run_raw, {"latest"})

    tickers = [item.strip().upper() for item in str(tickers_raw).split(",") if item.strip()] or None

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="run_pipeline",
        status="running",
        params={
            "tickers": tickers,
            "provider": provider,
            "start_date": start_date,
            "end_date": end_date,
            "run_name": run_name,
            "model_type": model_type,
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "top_n": top_n,
            "model_run_id": model_run_id,
            "holding_days": holding_days,
            "commission_bps": commission_bps,
            "slippage_bps": slippage_bps,
            "max_position_weight": max_position_weight,
            "min_signal_score": min_signal_score,
            "benchmark_symbol": benchmark_symbol,
            "max_sector_weight": max_sector_weight,
            "min_adv": min_adv,
            "max_gap_pct": max_gap_pct,
            "rebalance_threshold": rebalance_threshold,
        },
    )

    try:
        sync_results = sync_market_data(
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            provider=provider,
        )
        build_result = build_dataset(normalize_only=True)
        predictions_written = SignalTrainer().train(
            run_name=run_name,
            model_type=model_type,
            signal_type=signal_type,
            lookback_days=lookback_days,
        )
        daily_rows_written = BacktestRunner().run(
            top_n=top_n,
            model_run_id=model_run_id,
            holding_days=holding_days,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            max_position_weight=max_position_weight,
            min_signal_score=min_signal_score,
            benchmark_symbol=benchmark_symbol,
            max_sector_weight=max_sector_weight,
            min_adv=min_adv,
            max_gap_pct=max_gap_pct,
            rebalance_threshold=rebalance_threshold,
        )
        message = (
            f"Pipeline complete: synced {len(sync_results)} ticker(s), "
            f"normalized {len(build_result['normalized_files'])} file(s), "
            f"wrote {predictions_written} predictions, backtested {daily_rows_written} day(s)"
        )
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            sync_results=sync_results,
            build_result=build_result,
            predictions_written=predictions_written,
            daily_rows_written=daily_rows_written,
            top_n=top_n,
            model_run_id=model_run_id,
            holding_days=holding_days,
            commission_bps=commission_bps,
            slippage_bps=slippage_bps,
            max_position_weight=max_position_weight,
            min_signal_score=min_signal_score,
            benchmark_symbol=benchmark_symbol,
            max_sector_weight=max_sector_weight,
            min_adv=min_adv,
            max_gap_pct=max_gap_pct,
            rebalance_threshold=rebalance_threshold,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/auto-analysis/config")
async def update_auto_analysis_config(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    enabled_raw = await _request_value(request, "enabled", "")
    interval_hours = await _request_value(request, "interval_hours", 24)
    provider = await _request_value(request, "provider", "auto")
    start_date = await _request_value(request, "start_date", "2025-01-01")
    signal_type = await _request_value(request, "signal_type", "momentum")
    lookback_days = await _request_value(request, "lookback_days", 3)
    top_n = await _request_value(request, "top_n", 1)
    sync_cn_fundamentals_raw = await _request_value(request, "sync_cn_fundamentals", "")
    sync_cn_concepts_raw = await _request_value(request, "sync_cn_concepts", "")
    enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}
    sync_cn_fundamentals_enabled = str(sync_cn_fundamentals_raw).lower() in {"1", "true", "yes", "on"}
    sync_cn_concepts_enabled = str(sync_cn_concepts_raw).lower() in {"1", "true", "yes", "on"}

    status = auto_analysis_service.save_config(
        {
            "enabled": enabled,
            "interval_hours": interval_hours,
            "provider": provider,
            "start_date": start_date,
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "top_n": top_n,
            "sync_cn_fundamentals": sync_cn_fundamentals_enabled,
            "sync_cn_concepts": sync_cn_concepts_enabled,
        }
    )
    payload = {
        "status": "success",
        "message": "Auto analysis settings saved.",
        "config": status,
    }
    return _maybe_redirect(redirect_to, payload)


@router.post("/close-review/config")
async def update_close_review_config(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    enabled_raw = await _request_value(request, "enabled", "")
    run_hour = await _request_value(request, "run_hour", 18)
    run_minute = await _request_value(request, "run_minute", 0)
    provider = await _request_value(request, "provider", "auto")
    days_back = await _request_value(request, "days_back", 7)
    overlap_days = await _request_value(request, "overlap_days", 3)
    refresh_limit = await _request_value(request, "refresh_limit", 0)
    stale_job_hours = await _request_value(request, "stale_job_hours", 12)
    retry_cooldown_minutes = await _request_value(request, "retry_cooldown_minutes", 60)
    max_attempts_per_day = await _request_value(request, "max_attempts_per_day", 4)
    enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}
    status = close_review_scheduler_service.save_config(
        {
            "enabled": enabled,
            "run_hour": run_hour,
            "run_minute": run_minute,
            "provider": provider,
            "days_back": days_back,
            "overlap_days": overlap_days,
            "refresh_limit": refresh_limit,
            "stale_job_hours": stale_job_hours,
            "retry_cooldown_minutes": retry_cooldown_minutes,
            "max_attempts_per_day": max_attempts_per_day,
        }
    )
    payload = {"status": "success", "message": "Close review settings saved.", "config": status}
    return _maybe_redirect(redirect_to, payload)


@router.post("/import-model-output")
async def run_import_model_output(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    csv_path = str(await _request_value(request, "csv_path", "")).strip()
    run_name = str(await _request_value(request, "run_name", "external_model_import")).strip() or "external_model_import"
    model_type = str(await _request_value(request, "model_type", "qlib_external")).strip() or "qlib_external"
    market = str(await _request_value(request, "market", "")).strip() or None
    universe = str(await _request_value(request, "universe", "")).strip() or None
    artifact_path = str(await _request_value(request, "artifact_path", "")).strip() or None

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="import_model_output",
        status="running",
        params={
            "csv_path": csv_path,
            "run_name": run_name,
            "model_type": model_type,
            "market": market,
            "universe": universe,
            "artifact_path": artifact_path,
        },
    )
    try:
        importer = ExternalModelOutputImporter()
        result = importer.import_csv(
            Path(csv_path),
            run_name=run_name,
            model_type=model_type,
            market=market,
            universe=universe,
            artifact_path=artifact_path,
        )
        message = f"Imported {result['predictions_written']} predictions into {run_name}"
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            **result,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = fail_job_and_build_payload(job_repo, job_id=job.id, exc=exc)
        return _maybe_redirect(redirect_to, payload)


@router.post("/run-watchlist-analysis")
async def run_watchlist_analysis(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    try:
        payload = auto_analysis_service.run_watchlist_analysis(trigger="manual_cn_default")
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = {"status": "failed", "message": str(exc)}
        return _maybe_redirect(redirect_to, payload)


@router.post("/run-close-review")
async def run_close_review(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    force = _as_bool(await _request_value(request, "force", False), default=False)
    try:
        payload = close_review_scheduler_service.run_close_review(trigger="manual_cn_default", force=force)
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = {"status": "failed", "message": str(exc)}
        return _maybe_redirect(redirect_to, payload)


@router.post("/cleanup-stale-jobs")
async def cleanup_stale_jobs(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    stale_job_hours_raw = await _request_value(request, "stale_job_hours", 12)
    stale_job_hours = max(1, _as_int(stale_job_hours_raw, 12))
    cleaned = DataJobRepository(db).complete_stale_running_jobs(
        stale_after_hours=stale_job_hours,
        message_prefix="Manual cleanup closed a stale running job.",
    )
    payload = {
        "status": "success",
        "message": f"Cleaned {cleaned} stale running job(s).",
        "cleaned_jobs": cleaned,
    }
    return _maybe_redirect(redirect_to, payload)
