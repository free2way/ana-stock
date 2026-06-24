from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.market_lake import load_lake_price_history
from app.services.repository import PredictionRepository, SymbolRepository, WorkspaceSnapshotRepository
from app.services.screener_snapshots import (
    build_base_precompute_params,
    build_multi_model_precompute_params,
    load_exact_screener_snapshot_rows,
)
from app.services.time_utils import app_now_iso, app_today_iso


KRONOS_VALIDATION_JOB_TYPE = "kronos_validation"
KRONOS_VALIDATION_SNAPSHOT_TYPE = "kronos_validation_snapshot"


@dataclass(frozen=True)
class KronosRuntimeState:
    status: str
    message: str
    mode: str


def load_latest_kronos_validation(db: Session | None = None) -> dict | None:
    if db is None:
        with SessionLocal() as own_db:
            return load_latest_kronos_validation(own_db)
    return WorkspaceSnapshotRepository(db).get_latest_snapshot(KRONOS_VALIDATION_SNAPSHOT_TYPE)


def annotate_rows_with_kronos(rows: list[dict], *, db: Session | None = None) -> list[dict]:
    if not rows:
        return rows
    snapshot = load_latest_kronos_validation(db)
    payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
    validation_rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
    if not isinstance(validation_rows, list):
        return rows
    validation_map = {
        str(item.get("ticker") or "").strip().upper(): item
        for item in validation_rows
        if str(item.get("ticker") or "").strip()
    }
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker and ticker in validation_map:
            row["kronos_validation"] = validation_map[ticker]
    return rows


def save_kronos_validation_snapshot(
    *,
    db: Session | None = None,
    source_job_id: int | None = None,
    markets: list[str] | None = None,
    candidate_limit: int | None = None,
) -> dict:
    owns_db = db is None
    db = db or SessionLocal()
    try:
        payload = build_kronos_validation_payload(db=db, markets=markets, candidate_limit=candidate_limit)
        row = WorkspaceSnapshotRepository(db).create_snapshot(
            snapshot_type=KRONOS_VALIDATION_SNAPSHOT_TYPE,
            snapshot_date=app_today_iso(),
            payload=payload,
            source_job_id=source_job_id,
        )
        payload["snapshot_id"] = row.id
        return payload
    finally:
        if owns_db:
            db.close()


def build_kronos_validation_payload(
    *,
    db: Session,
    markets: list[str] | None = None,
    candidate_limit: int | None = None,
) -> dict:
    settings = get_settings()
    market_set = _normalize_markets(markets)
    limit = max(1, int(candidate_limit or settings.kronos_candidate_limit or 60))
    # Over-collect so the validation pool is not wasted on newly listed names
    # that lack enough lake history for Kronos path prediction.
    candidates = _collect_validation_candidates(db=db, markets=market_set, limit=limit * 3)
    runtime = _runtime_state()
    prepared_rows = [_prepare_validation_row(candidate, runtime=runtime) for candidate in candidates]
    rows = _select_validation_rows(prepared_rows, limit=limit, runtime=runtime)
    if runtime.mode == "external_runner" and rows:
        rows = _run_external_kronos(rows)
    status = runtime.status
    if rows and runtime.status == "ready":
        status = "success"
    elif rows and runtime.status == "not_configured":
        status = "not_configured"
    return {
        "status": status,
        "mode": runtime.mode,
        "message": runtime.message,
        "model_name": settings.kronos_model_name,
        "markets": sorted(market_set),
        "candidate_count": len(rows),
        "candidate_pool_count": len(candidates),
        "validated_count": sum(1 for row in rows if str(row.get("kronos_status") or "").upper() == "READY"),
        "not_configured_count": sum(1 for row in rows if str(row.get("kronos_status") or "").upper() == "NOT_CONFIGURED"),
        "skipped_count": sum(1 for row in rows if str(row.get("kronos_status") or "").upper() == "SKIPPED"),
        "rows": rows,
        "config": {
            "candidate_limit": limit,
            "history_limit": int(settings.kronos_history_limit or 180),
            "min_history": int(settings.kronos_min_history or 60),
            "horizon_days": int(settings.kronos_prediction_horizon_days or 3),
            "device": settings.kronos_device,
            "runner_configured": bool(settings.kronos_runner_command),
            "temperature": float(settings.kronos_temperature or 0.8),
            "top_p": float(settings.kronos_top_p or 0.9),
            "sample_count": int(settings.kronos_sample_count or 3),
            "seed": int(settings.kronos_seed or 42),
        },
        "updated_at": app_now_iso(),
    }


def _select_validation_rows(rows: list[dict], *, limit: int, runtime: KronosRuntimeState) -> list[dict]:
    if runtime.status != "ready":
        return rows[:limit]
    pending = [row for row in rows if row.get("kronos_status") == "PENDING"]
    non_pending = [row for row in rows if row.get("kronos_status") != "PENDING"]
    selected = pending[:limit]
    if len(selected) < limit:
        selected.extend(non_pending[: max(0, limit - len(selected))])
    return selected


def _normalize_markets(markets: Iterable[str] | None) -> set[str]:
    market_set = {str(item or "").strip().upper() for item in (markets or ["CN"]) if str(item or "").strip()}
    market_set = {item for item in market_set if item in {"CN", "US"}}
    return market_set or {"CN"}


def _runtime_state() -> KronosRuntimeState:
    settings = get_settings()
    if not settings.kronos_enabled:
        return KronosRuntimeState(
            status="not_configured",
            mode="disabled",
            message="Kronos validation is disabled. Set PQW_KRONOS_ENABLED=true to enable the adapter.",
        )
    if settings.kronos_runner_command:
        return KronosRuntimeState(
            status="ready",
            mode="external_runner",
            message="Kronos external runner is configured.",
        )
    missing = [
        name
        for name in ("torch", "transformers", "huggingface_hub")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        return KronosRuntimeState(
            status="not_configured",
            mode="dependency_missing",
            message=(
                "Kronos adapter is wired, but runtime dependencies are missing: "
                + ", ".join(missing)
                + ". Configure PQW_KRONOS_RUNNER_COMMAND to use an isolated Kronos environment."
            ),
        )
    return KronosRuntimeState(
        status="not_configured",
        mode="runner_required",
        message=(
            "Kronos dependencies are present, but no runner command is configured. "
            "Set PQW_KRONOS_RUNNER_COMMAND so the main app can call the isolated predictor safely."
        ),
    )


def _collect_validation_candidates(*, db: Session, markets: set[str], limit: int) -> list[dict]:
    collected: dict[str, dict] = {}
    for market in sorted(markets):
        _merge_candidate_rows(collected, _load_multi_model_candidates(market), source_weight=4)
        _merge_candidate_rows(collected, _load_template_candidates(market), source_weight=2)
        if len([item for item in collected.values() if item.get("market") == market]) < max(10, limit // max(len(markets), 1)):
            _merge_candidate_rows(collected, _load_prediction_candidates(db, market=market, limit=limit * 2), source_weight=1)
    rows = list(collected.values())
    rows.sort(
        key=lambda item: (
            int(item.get("source_weight") or 0),
            int(item.get("model_hit_count") or 0),
            float(item.get("trade_readiness_score") or 0.0),
            float(item.get("model_signal_strength") or 0.0),
            float(item.get("trend_score") or 0.0),
            str(item.get("ticker") or ""),
        ),
        reverse=True,
    )
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows[:limit] if row.get("ticker")]
    overviews = SymbolRepository(db).list_overviews_for_tickers(tickers)
    for row in rows[:limit]:
        ticker = str(row.get("ticker") or "").strip().upper()
        overview = overviews.get(ticker) or {}
        row["name"] = overview.get("name") or row.get("name") or ticker
        row["market"] = str(overview.get("market") or row.get("market") or "").upper()
    return rows[:limit]


def _load_multi_model_candidates(market: str) -> list[dict]:
    rows: list[dict] = []
    for params in build_multi_model_precompute_params(markets=[market]):
        snapshot_rows = load_exact_screener_snapshot_rows(params) or []
        for row in snapshot_rows[:120]:
            item = dict(row)
            item["_candidate_source"] = str(params.get("preset_label") or params.get("preset_key") or "multi_model")
            item["_candidate_templates"] = list(params.get("multi_model_templates") or [])
            item["market"] = market
            rows.append(item)
    return rows


def _load_template_candidates(market: str) -> list[dict]:
    template_keys = ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"]
    rows: list[dict] = []
    for template_key in template_keys:
        params = build_base_precompute_params(model_template=template_key, universe="full_market", market=market)
        snapshot_rows = load_exact_screener_snapshot_rows(params) or []
        for row in snapshot_rows[:120]:
            item = dict(row)
            item["_candidate_source"] = template_key
            item["_candidate_templates"] = [template_key]
            item["market"] = market
            rows.append(item)
    return rows


def _load_prediction_candidates(db: Session, *, market: str, limit: int) -> list[dict]:
    rows = PredictionRepository(db).list_latest_signal_decisions(limit=limit, market=market)
    for row in rows:
        row["_candidate_source"] = "latest_predictions"
        row["_candidate_templates"] = ["lightgbm_latest_prediction"]
        row["trend_score"] = row.get("signal_strength") or row.get("score") or 0
    return rows


def _merge_candidate_rows(target: dict[str, dict], rows: list[dict], *, source_weight: int) -> None:
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        market = str(row.get("market") or "").strip().upper()
        if not ticker or market not in {"CN", "US"}:
            continue
        existing = target.get(ticker)
        incoming_score = _candidate_rank_score(row, source_weight=source_weight)
        if existing is None:
            item = dict(row)
            item["ticker"] = ticker
            item["market"] = market
            item["source_weight"] = source_weight
            item["_rank_score"] = incoming_score
            target[ticker] = item
            continue
        if incoming_score > float(existing.get("_rank_score") or 0.0):
            preserved_templates = list(existing.get("_candidate_templates") or [])
            preserved_sources = [str(existing.get("_candidate_source") or "").strip()]
            item = dict(row)
            item["ticker"] = ticker
            item["market"] = market
            item["source_weight"] = max(source_weight, int(existing.get("source_weight") or 0))
            item["_rank_score"] = incoming_score
            item["_candidate_templates"] = list(dict.fromkeys(preserved_templates + list(item.get("_candidate_templates") or [])))
            item["_candidate_source"] = " / ".join(
                value for value in dict.fromkeys(preserved_sources + [str(item.get("_candidate_source") or "").strip()]) if value
            )
            target[ticker] = item
        else:
            existing["source_weight"] = max(int(existing.get("source_weight") or 0), source_weight)
            existing["_candidate_templates"] = list(
                dict.fromkeys(list(existing.get("_candidate_templates") or []) + list(row.get("_candidate_templates") or []))
            )


def _candidate_rank_score(row: dict, *, source_weight: int) -> float:
    return (
        source_weight * 1000
        + int(row.get("model_hit_count") or row.get("snapshot_hits") or 0) * 80
        + float(row.get("trade_readiness_score") or 0.0) * 3
        + float(row.get("model_signal_strength") or row.get("signal_strength") or 0.0) * 2
        + float(row.get("trend_score") or row.get("score") or 0.0)
    )


def _prepare_validation_row(candidate: dict, *, runtime: KronosRuntimeState) -> dict:
    settings = get_settings()
    ticker = str(candidate.get("ticker") or "").strip().upper()
    market = str(candidate.get("market") or "").strip().upper()
    history = load_lake_price_history(market=market, ticker=ticker, limit=int(settings.kronos_history_limit or 180))
    precheck = _build_path_precheck(history)
    base = {
        "ticker": ticker,
        "name": candidate.get("name") or ticker,
        "market": market,
        "source": candidate.get("_candidate_source") or candidate.get("model_summary") or "model_candidate",
        "source_templates": list(candidate.get("_candidate_templates") or candidate.get("matched_model_templates") or []),
        "trend_score": candidate.get("trend_score"),
        "trade_readiness_score": candidate.get("trade_readiness_score"),
        "model_signal_label": candidate.get("model_signal_label") or candidate.get("signal_label"),
        "model_signal_strength": candidate.get("model_signal_strength") or candidate.get("signal_strength"),
        "action_label": candidate.get("action_label"),
        "risk_flags": candidate.get("risk_flags") or candidate.get("model_execution_tags") or [],
        "history_count": len(history),
        "latest_close": history[-1].get("close") if history else candidate.get("latest_close"),
        "latest_date": history[-1].get("date") if history else None,
        "path_precheck": precheck,
    }
    min_history = int(settings.kronos_min_history or 60)
    if runtime.status != "ready":
        history_note = f" Current lake history has {len(history)} bars; runner will require at least {min_history}."
        return {
            **base,
            "kronos_status": "NOT_CONFIGURED",
            "kronos_decision": "待接入 Kronos",
            "kronos_score": None,
            "kronos_reason": runtime.message + history_note,
        }
    if len(history) < min_history:
        return {
            **base,
            "kronos_status": "SKIPPED",
            "kronos_decision": "历史行情不足",
            "kronos_score": None,
            "kronos_reason": f"Need at least {min_history} bars; got {len(history)}.",
        }
    return {
        **base,
        "kronos_status": "PENDING",
        "kronos_decision": "等待 Kronos 预测",
        "kronos_score": None,
        "kronos_reason": "Queued for external Kronos runner.",
        "_history": history,
    }


def _build_path_precheck(history: list[dict]) -> dict:
    if len(history) < 8:
        return {"status": "insufficient_history", "score": None}
    closes = [_safe_float(row.get("close")) for row in history]
    volumes = [_safe_float(row.get("volume")) for row in history]
    latest = closes[-1]
    prev_3 = closes[-4] if len(closes) >= 4 else None
    prev_5 = closes[-6] if len(closes) >= 6 else None
    prev_20 = closes[-21] if len(closes) >= 21 else None
    momentum_3 = _pct_change(latest, prev_3)
    momentum_5 = _pct_change(latest, prev_5)
    momentum_20 = _pct_change(latest, prev_20)
    recent_volume = sum(volumes[-5:]) / max(len(volumes[-5:]), 1)
    base_volume = sum(volumes[-25:-5]) / max(len(volumes[-25:-5]), 1) if len(volumes) >= 25 else 0.0
    volume_ratio = recent_volume / base_volume if base_volume > 0 else None
    score = 50.0
    for value, weight in ((momentum_3, 1.8), (momentum_5, 1.2), (momentum_20, 0.45)):
        if value is not None:
            score += value * weight
    if volume_ratio is not None:
        score += min(18.0, max(-10.0, (volume_ratio - 1.0) * 12.0))
    return {
        "status": "ready",
        "score": round(max(0.0, min(100.0, score)), 1),
        "momentum_3d_pct": None if momentum_3 is None else round(momentum_3, 2),
        "momentum_5d_pct": None if momentum_5 is None else round(momentum_5, 2),
        "momentum_20d_pct": None if momentum_20 is None else round(momentum_20, 2),
        "volume_ratio_5v20": None if volume_ratio is None else round(volume_ratio, 2),
    }


def _run_external_kronos(rows: list[dict]) -> list[dict]:
    settings = get_settings()
    command = shlex.split(settings.kronos_runner_command or "")
    if not command:
        return rows
    pending = [row for row in rows if row.get("kronos_status") == "PENDING"]
    if not pending:
        return rows
    payload = {
        "model_name": settings.kronos_model_name,
        "device": settings.kronos_device,
        "horizon_days": int(settings.kronos_prediction_horizon_days or 3),
        "temperature": float(settings.kronos_temperature or 0.8),
        "top_p": float(settings.kronos_top_p or 0.9),
        "sample_count": int(settings.kronos_sample_count or 3),
        "seed": int(settings.kronos_seed or 42),
        "candidates": [
            {
                "ticker": row["ticker"],
                "market": row["market"],
                "history": row.pop("_history", []),
            }
            for row in pending
        ],
    }
    env = os.environ.copy()
    if settings.kronos_repo_path:
        env["PQW_KRONOS_REPO_PATH"] = str(settings.kronos_repo_path)
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=float(settings.kronos_timeout_seconds or 180.0),
            check=False,
            env=env,
        )
    except Exception as exc:
        for row in pending:
            row["kronos_status"] = "FAILED"
            row["kronos_decision"] = "Kronos 调用失败"
            row["kronos_reason"] = str(exc)
        return rows
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or f"runner exited {proc.returncode}").strip()
        for row in pending:
            row["kronos_status"] = "FAILED"
            row["kronos_decision"] = "Kronos 调用失败"
            row["kronos_reason"] = message[:500]
        return rows
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        for row in pending:
            row["kronos_status"] = "FAILED"
            row["kronos_decision"] = "Kronos 输出无法解析"
            row["kronos_reason"] = str(exc)
        return rows
    result_rows = {
        str(item.get("ticker") or "").strip().upper(): item
        for item in (result.get("rows") or result.get("predictions") or [])
        if isinstance(item, dict)
    }
    for row in pending:
        prediction = result_rows.get(row["ticker"]) or {}
        if not prediction:
            row["kronos_status"] = "FAILED"
            row["kronos_decision"] = "Kronos 未返回"
            row["kronos_reason"] = "External runner did not return this ticker."
            continue
        row.update(_normalize_runner_prediction(prediction))
    return rows


def _normalize_runner_prediction(prediction: dict) -> dict:
    score = prediction.get("kronos_score")
    if score is None:
        score = prediction.get("path_score")
    expected_return = prediction.get("expected_return_3d_pct")
    if expected_return is None:
        expected_return = prediction.get("expected_return_pct")
    max_drawdown = prediction.get("max_drawdown_pct")
    decision = str(prediction.get("kronos_decision") or prediction.get("decision") or "").strip()
    if not decision:
        if _safe_float(score) is not None and _safe_float(score) >= 65:
            decision = "Kronos 支持"
        elif _safe_float(score) is not None and _safe_float(score) <= 40:
            decision = "Kronos 不支持"
        else:
            decision = "Kronos 中性"
    return {
        "kronos_status": "READY",
        "kronos_decision": decision,
        "kronos_score": None if score is None else round(float(score), 2),
        "kronos_expected_return_1d_pct": _round_optional(prediction.get("expected_return_1d_pct")),
        "kronos_expected_return_3d_pct": _round_optional(expected_return),
        "kronos_max_drawdown_pct": _round_optional(max_drawdown),
        "kronos_reason": str(prediction.get("reason") or prediction.get("kronos_reason") or "").strip() or decision,
        "kronos_raw": {key: value for key, value in prediction.items() if key not in {"history"}},
    }


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_optional(value) -> float | None:
    number = _safe_float(value)
    return None if number is None else round(number, 2)


def _pct_change(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous is None or previous == 0:
        return None
    return ((latest / previous) - 1.0) * 100.0
