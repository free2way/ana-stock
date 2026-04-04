from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auto_analysis import auto_analysis_service
from app.services.auth import is_authenticated, login_redirect
from app.services.backtester import BacktestRunner
from app.services.cn_fundamentals import sync_cn_fundamentals
from app.services.dataset_build import build_dataset
from app.services.global_fundamentals import sync_global_fundamentals
from app.services.market_sync import sync_market_data
from app.services.repository import DataJobRepository, PriceSyncStateRepository
from app.services.sample_data import seed_sample_data
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
    return RedirectResponse(url=f"{redirect_to}?{urlencode(query)}", status_code=303)


async def _request_value(request: Request, key: str, default=None):
    if key in request.query_params:
        return request.query_params.get(key, default)
    form = await request.form()
    return form.get(key, default)


@router.get("/templates")
def job_templates() -> list[dict[str, str]]:
    return [
        {"job_type": "sync_market_data", "description": "Fetch and persist market data with OpenBB."},
        {"job_type": "sync_cn_fundamentals", "description": "Fetch and persist A-share fundamentals with TuShare Pro."},
        {"job_type": "sync_global_fundamentals", "description": "Fetch and persist US/HK fundamentals with yfinance."},
        {"job_type": "build_dataset", "description": "Normalize price files and build a Qlib dataset."},
        {"job_type": "train_model", "description": "Train a signal model with Qlib."},
        {"job_type": "run_backtest", "description": "Run a backtest from stored predictions."},
    ]


@router.get("/sync-states")
def sync_states(db: Session = Depends(get_db_session)) -> list[dict[str, str | int | None]]:
    repo = PriceSyncStateRepository(db)
    return repo.list_states_with_symbols()


@router.get("/recent")
def recent_jobs(limit: int = 20, db: Session = Depends(get_db_session)) -> list[dict]:
    repo = DataJobRepository(db)
    return repo.list_recent_jobs(limit=limit)


@router.get("/auto-analysis")
def auto_analysis_status(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return auto_analysis_service.get_status()


@router.post("/seed-sample-data")
async def run_seed_sample_data(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    job_repo = DataJobRepository(db)
    job = job_repo.create_job(job_type="seed_sample_data", status="running")
    try:
        results = seed_sample_data()
        job_repo.complete_job(job.id, status="success", message=f"Seeded {len(results)} symbols")
        payload = {"status": "success", "job_id": job.id, "seeded": results}
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
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
        job_repo.complete_job(job.id, status=status, message=message)
        payload = {
            "status": status,
            "job_id": job.id,
            "message": message,
            "results": results,
        }
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
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
            job_repo.complete_job(job.id, status="success", message=message)
            payload = {"status": "success", "job_id": job.id, **result, "message": message}
        elif result.get("message"):
            job_repo.complete_job(job.id, status="partial", message=result["message"])
            payload = {"status": "partial", "job_id": job.id, **result}
        else:
            message = f"Normalized {len(result['normalized_files'])} files"
            job_repo.complete_job(job.id, status="success", message=message)
            payload = {"status": "success", "job_id": job.id, **result, "message": message}
    except RuntimeError as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
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
        status = "success" if result["status"] == "success" else "partial"
        job_repo.complete_job(job.id, status=status, message=result["message"])
        payload = {"job_id": job.id, **result, "status": status}
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
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
        status = "success" if result["status"] == "success" else "partial"
        job_repo.complete_job(job.id, status=status, message=result["message"])
        payload = {"job_id": job.id, **result, "status": status}
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
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
    try:
        lookback_days = int(lookback_raw)
    except (TypeError, ValueError):
        lookback_days = 3
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
        job_repo.complete_job(job.id, status="success", message=message)
        payload = {
            "status": "success",
            "job_id": job.id,
            "predictions_written": count,
            "run_name": run_name,
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "message": message,
        }
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
        return _maybe_redirect(redirect_to, payload)


@router.post("/backtest")
async def run_backtest(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    top_n_raw = await _request_value(request, "top_n", 1)
    try:
        top_n = int(top_n_raw)
    except (TypeError, ValueError):
        top_n = 1
    model_run_raw = await _request_value(request, "model_run_id")
    try:
        model_run_id = int(model_run_raw) if model_run_raw not in (None, "", "latest") else None
    except (TypeError, ValueError):
        model_run_id = None
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
        job_repo.complete_job(job.id, status="success", message=message)
        payload = {
            "status": "success",
            "job_id": job.id,
            "daily_rows_written": count,
            "top_n": top_n,
            "model_run_id": model_run_id,
            "message": message,
        }
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {
            "status": "failed",
            "job_id": job.id,
            "message": str(exc),
            "top_n": top_n,
            "model_run_id": model_run_id,
        }
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
    try:
        lookback_days = int(lookback_raw)
    except (TypeError, ValueError):
        lookback_days = 3
    try:
        top_n = int(top_n_raw)
    except (TypeError, ValueError):
        top_n = 1

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
        job_repo.complete_job(job.id, status="success", message=message)
        payload = {
            "status": "success",
            "job_id": job.id,
            "message": message,
            "sync_results": sync_results,
            "build_result": build_result,
            "predictions_written": predictions_written,
            "daily_rows_written": daily_rows_written,
        }
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        job_repo.complete_job(job.id, status="failed", message=str(exc))
        payload = {"status": "failed", "job_id": job.id, "message": str(exc)}
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
    enabled = str(enabled_raw).lower() in {"1", "true", "yes", "on"}

    status = auto_analysis_service.save_config(
        {
            "enabled": enabled,
            "interval_hours": interval_hours,
            "provider": provider,
            "start_date": start_date,
            "signal_type": signal_type,
            "lookback_days": lookback_days,
            "top_n": top_n,
        }
    )
    payload = {
        "status": "success",
        "message": "Auto analysis settings saved.",
        "config": status,
    }
    return _maybe_redirect(redirect_to, payload)


@router.post("/run-watchlist-analysis")
async def run_watchlist_analysis(request: Request):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    redirect_to = await _request_value(request, "redirect_to")
    try:
        payload = auto_analysis_service.run_watchlist_analysis(trigger="manual")
        return _maybe_redirect(redirect_to, payload)
    except Exception as exc:
        payload = {"status": "failed", "message": str(exc)}
        return _maybe_redirect(redirect_to, payload)
