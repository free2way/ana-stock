from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auto_analysis import auto_analysis_service
from app.services.auth import is_authenticated, login_redirect
from app.services.backtester import BacktestRunner
from app.services.ai_daily_report import build_ai_daily_report, load_ai_daily_report, render_ai_daily_report_message, save_ai_daily_report
from app.services.cn_market_universe import (
    init_cn_market_data,
    refresh_cn_market_data,
    refresh_cn_market_data_daily,
    sync_cn_symbol_universe,
)
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.cn_concepts import sync_cn_concepts
from app.services.cn_fundamentals import sync_cn_fundamentals
from app.services.dataset_build import build_dataset
from app.services.global_fundamentals import sync_global_fundamentals
from app.services.job_response import build_job_payload, complete_job_and_build_payload, fail_job_and_build_payload
from app.services.market_sync import sync_market_data
from app.services.model_output_importer import ExternalModelOutputImporter
from app.services.push_notifications import PushNotificationService
from app.services.repository import DataJobRepository, PriceSyncStateRepository
from app.services.sample_data import seed_sample_data
from app.services.technical_snapshot_cache import rebuild_technical_snapshots
from app.services.trainer import SignalTrainer


router = APIRouter(prefix="/jobs", tags=["jobs"])


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


@router.get("/templates")
def job_templates() -> list[dict[str, str]]:
    return [
        {"job_type": "sync_market_data", "description": "Fetch and persist market data with OpenBB."},
        {"job_type": "sync_cn_symbol_universe", "description": "Sync the A-share stock universe into local symbols."},
        {"job_type": "init_cn_market_data", "description": "Initialize A-share market price history for full-market scans."},
        {"job_type": "refresh_cn_market_data", "description": "Refresh recent A-share market prices for daily full-market scans."},
        {"job_type": "refresh_cn_market_data_daily", "description": "Incrementally refresh A-share market prices from each symbol's last synced date."},
        {"job_type": "cn_close_review", "description": "Run the post-close CN incremental refresh, rebuild, AI review, and recommendations pipeline."},
        {"job_type": "rebuild_technical_snapshots", "description": "Cache technical pattern snapshots for faster full-market scans."},
        {"job_type": "sync_cn_fundamentals", "description": "Fetch and persist A-share fundamentals with TuShare Pro."},
        {"job_type": "sync_cn_concepts", "description": "Fetch and persist A-share concept memberships with TuShare Pro."},
        {"job_type": "sync_global_fundamentals", "description": "Fetch and persist US/HK fundamentals with yfinance."},
        {"job_type": "build_dataset", "description": "Normalize price files and build a Qlib dataset."},
        {"job_type": "train_model", "description": "Train a signal model with Qlib."},
        {"job_type": "import_model_output", "description": "Import external model predictions into predictions and model details."},
        {"job_type": "run_backtest", "description": "Run a backtest from stored predictions."},
    ]


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

    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="send_ai_daily_report",
        status="running",
        params={"channels": selected_channels or None},
    )
    try:
        report = load_ai_daily_report()
        if report is None:
            report = build_ai_daily_report(limit=8)
            save_ai_daily_report(report)
        notifier = PushNotificationService()
        result = notifier.send_text(
            title="A股 AI 每日决策面板",
            body=render_ai_daily_report_message(report),
            channels=selected_channels or None,
        )
        message = (
            f"Sent A-share AI daily report to {', '.join(result['sent'])}"
            if result.get("sent")
            else "No AI daily report channels were available."
        )
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
    provider = str(await _request_value(request, "provider", "yfinance")).strip() or "yfinance"
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
    provider = str(await _request_value(request, "provider", "yfinance")).strip() or "yfinance"
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
    provider = str(await _request_value(request, "provider", "yfinance")).strip() or "yfinance"
    days_back_raw = await _request_value(request, "days_back", 7)
    limit_raw = await _request_value(request, "limit", None)
    incremental_raw = await _request_value(request, "incremental", None)
    overlap_days_raw = await _request_value(request, "overlap_days", 3)
    days_back = _as_int(days_back_raw, 7)
    limit = _as_optional_int(limit_raw)
    overlap_days = _as_int(overlap_days_raw, 3)
    incremental = _as_bool(incremental_raw, default=False)

    job_repo = DataJobRepository(db)
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
    provider = str(await _request_value(request, "provider", "yfinance")).strip() or "yfinance"
    days_back_raw = await _request_value(request, "days_back", 7)
    limit_raw = await _request_value(request, "limit", None)
    overlap_days_raw = await _request_value(request, "overlap_days", 3)
    days_back = _as_int(days_back_raw, 7)
    limit = _as_optional_int(limit_raw)
    overlap_days = _as_int(overlap_days_raw, 3)

    job_repo = DataJobRepository(db)
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
    run_name = await _request_value(request, "run_name", "baseline_momentum")
    run_name = str(run_name).strip() or "baseline_momentum"
    signal_type = str(await _request_value(request, "signal_type", "momentum")).strip() or "momentum"
    lookback_raw = await _request_value(request, "lookback_days", 3)
    lookback_days = _as_int(lookback_raw, 3)
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="train",
        status="running",
        params={"run_name": run_name, "signal_type": signal_type, "lookback_days": lookback_days},
    )
    trainer = SignalTrainer()
    try:
        count = trainer.train(run_name=run_name, signal_type=signal_type, lookback_days=lookback_days)
        message = f"{run_name}: wrote {count} predictions ({signal_type}, {lookback_days}d)"
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            predictions_written=count,
            run_name=run_name,
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
    model_run_raw = await _request_value(request, "model_run_id")
    model_run_id = _as_optional_int_except(model_run_raw, {"latest"})
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(
        job_type="backtest",
        status="running",
        params={"top_n": top_n, "model_run_id": model_run_id},
    )
    runner = BacktestRunner()
    try:
        count = runner.run(top_n=top_n, model_run_id=model_run_id)
        run_label = f"model_run_id={model_run_id}" if model_run_id is not None else "latest_model"
        message = f"Wrote {count} backtest rows ({run_label}, top_n={top_n})"
        payload = complete_job_and_build_payload(
            job_repo,
            job_id=job.id,
            status="success",
            message=message,
            daily_rows_written=count,
            top_n=top_n,
            model_run_id=model_run_id,
        )
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = build_job_payload(
            status="failed",
            job_id=job.id,
            message=str(exc),
            top_n=top_n,
            model_run_id=model_run_id,
        )
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        return _maybe_redirect(redirect_to, payload)


@router.post("/run-pipeline")
async def run_pipeline(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    tickers_raw = await _request_value(request, "tickers", "")
    provider = str(await _request_value(request, "provider", "yfinance")).strip() or "yfinance"
    start_date = await _request_value(request, "start_date")
    end_date = await _request_value(request, "end_date")
    run_name = str(await _request_value(request, "run_name", "pipeline_run")).strip() or "pipeline_run"
    signal_type = str(await _request_value(request, "signal_type", "momentum")).strip() or "momentum"
    lookback_raw = await _request_value(request, "lookback_days", 3)
    top_n_raw = await _request_value(request, "top_n", 1)
    lookback_days = _as_int(lookback_raw, 3)
    top_n = _as_int(top_n_raw, 1)

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
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "top_n": top_n,
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
            signal_type=signal_type,
            lookback_days=lookback_days,
        )
        daily_rows_written = BacktestRunner().run(top_n=top_n)
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
    provider = await _request_value(request, "provider", "yfinance")
    start_date = await _request_value(request, "start_date", "2025-01-01")
    signal_type = await _request_value(request, "signal_type", "momentum")
    lookback_days = await _request_value(request, "lookback_days", 3)
    top_n = await _request_value(request, "top_n", 1)
    sync_cn_concepts_raw = await _request_value(request, "sync_cn_concepts", "")
    enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}
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
    run_hour = await _request_value(request, "run_hour", 16)
    run_minute = await _request_value(request, "run_minute", 0)
    provider = await _request_value(request, "provider", "yfinance")
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
    try:
        payload = close_review_scheduler_service.run_close_review(trigger="manual_cn_default")
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
