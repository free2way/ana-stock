from __future__ import annotations

from collections import defaultdict
from statistics import mean

from app.core.db import SessionLocal
from app.services.market_lake import load_lake_price_history
from app.services.repository import WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.time_utils import app_now_iso, app_today_iso


AI_DAILY_REPORT_HISTORY_SNAPSHOT_TYPE = "ai_daily_report_history"
RECOMMENDATION_REGRESSION_SNAPSHOT_TYPE = "ai_report_recommendation_regression"


def _safe_float(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _board_profile(ticker: str, name: str | None, limit_band_pct=None) -> str:
    normalized = str(ticker or "").strip().upper()
    normalized_name = str(name or "").strip().upper().replace(" ", "")
    code = normalized.split(".", 1)[0]
    limit_band = _safe_float(limit_band_pct)
    if normalized_name.startswith(("ST", "*ST", "S*ST", "PT")) or (limit_band is not None and limit_band <= 5.5):
        return "st"
    if normalized.endswith(".BJ") or code.startswith(("4", "8")) or (limit_band is not None and limit_band >= 29):
        return "bse"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    return "main"


def _deviation_bucket(value) -> str:
    deviation = _safe_float(value)
    if deviation is None:
        return "unknown"
    if deviation <= 0:
        return "inside_or_below_buy_zone"
    if deviation <= 5:
        return "near_buy_zone_0_5"
    if deviation <= 8:
        return "near_buy_zone_5_8"
    if deviation <= 15:
        return "extended_8_15"
    return "extended_gt_15"


def _next_session_metrics(*, ticker: str, market: str, report_date: str) -> dict | None:
    history = load_lake_price_history(market=market, ticker=ticker, limit=320)
    if not history:
        return None
    baseline = None
    next_row = None
    for row in history:
        row_date = str(row.get("date") or row.get("trade_date") or "")[:10]
        if not row_date:
            continue
        if row_date <= report_date:
            baseline = row
            continue
        if row_date > report_date:
            next_row = row
            break
    if baseline is None or next_row is None:
        return None
    base_close = _safe_float(baseline.get("close"))
    next_open = _safe_float(next_row.get("open"))
    next_high = _safe_float(next_row.get("high"))
    next_low = _safe_float(next_row.get("low"))
    next_close = _safe_float(next_row.get("close"))
    if not base_close or not next_open or not next_high or not next_low or not next_close:
        return None

    def pct(start: float, end: float) -> float:
        return round((end / start - 1.0) * 100.0, 2)

    open_to_high = pct(next_open, next_high)
    open_to_low = pct(next_open, next_low)
    open_to_close = pct(next_open, next_close)
    close_1d = pct(base_close, next_close)
    gap_open = pct(base_close, next_open)
    return {
        "ticker": ticker,
        "market": market,
        "report_date": report_date,
        "next_date": str(next_row.get("date") or next_row.get("trade_date") or "")[:10],
        "gap_open_pct": gap_open,
        "open_to_high_pct": open_to_high,
        "open_to_low_pct": open_to_low,
        "open_to_close_pct": open_to_close,
        "close_1d_pct": close_1d,
        "close_hit": close_1d > 0,
        "execution_hit": open_to_high >= 2.0 and open_to_low > -4.0,
        "gap_blocked": gap_open >= 7.0,
        "deep_intraday_drawdown": open_to_low <= -4.0,
    }


def _iter_report_candidate_rows(payload: dict, *, report_date: str) -> list[dict]:
    rows: list[dict] = []
    for pool, candidates in (
        ("actionable", payload.get("market_recommendations") or payload.get("rows") or []),
        ("watch", payload.get("market_watch_recommendations") or []),
    ):
        for index, item in enumerate(candidates[:8], start=1):
            ticker = str((item or {}).get("ticker") or "").strip().upper()
            if not ticker:
                continue
            market = str((item or {}).get("market") or "").strip().upper() or (
                "CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US"
            )
            rows.append(
                {
                    **(item or {}),
                    "ticker": ticker,
                    "market": market,
                    "report_pool": pool,
                    "report_rank": index,
                    "report_date": report_date,
                }
            )
    return rows


def _dimensions_for_row(row: dict) -> list[str]:
    ticker = str(row.get("ticker") or "").upper()
    risk_flags = [str(flag).strip().lower() for flag in (row.get("risk_flags") or []) if str(flag).strip()]
    dimensions = [
        f"pool:{row.get('report_pool') or 'unknown'}",
        f"template:{row.get('full_market_template') or row.get('report_source_label') or 'unknown'}",
        f"source:{row.get('report_source_kind') or 'unknown'}",
        f"tradability:{str(row.get('tradability_status') or 'unknown').upper()}",
        f"board:{_board_profile(ticker, row.get('name'), row.get('limit_band_pct'))}",
        f"deviation:{_deviation_bucket(row.get('close_vs_buy_zone_high_pct'))}",
        "model_score:present" if row.get("model_score") is not None or row.get("score") is not None else "model_score:missing",
    ]
    dimensions.extend(f"risk:{flag}" for flag in risk_flags[:6])
    if (row.get("lightgbm_execution_bias") or {}).get("action"):
        dimensions.append(f"bias:{(row.get('lightgbm_execution_bias') or {}).get('action')}")
    return dimensions


def _aggregate_records(records: list[dict]) -> dict:
    if not records:
        return {
            "count": 0,
            "avg_close_1d_pct": None,
            "close_hit_rate": None,
            "execution_hit_rate": None,
            "avg_open_to_high_pct": None,
            "avg_open_to_low_pct": None,
            "gap_blocked_rate": None,
            "deep_drawdown_rate": None,
            "examples": [],
        }
    return {
        "count": len(records),
        "avg_close_1d_pct": round(mean(float(item["close_1d_pct"]) for item in records), 2),
        "close_hit_rate": round(sum(1 for item in records if item.get("close_hit")) / len(records) * 100.0, 1),
        "execution_hit_rate": round(sum(1 for item in records if item.get("execution_hit")) / len(records) * 100.0, 1),
        "avg_open_to_high_pct": round(mean(float(item["open_to_high_pct"]) for item in records), 2),
        "avg_open_to_low_pct": round(mean(float(item["open_to_low_pct"]) for item in records), 2),
        "gap_blocked_rate": round(sum(1 for item in records if item.get("gap_blocked")) / len(records) * 100.0, 1),
        "deep_drawdown_rate": round(sum(1 for item in records if item.get("deep_intraday_drawdown")) / len(records) * 100.0, 1),
        "examples": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "report_date": item.get("report_date"),
                "next_date": item.get("next_date"),
                "close_1d_pct": item.get("close_1d_pct"),
                "open_to_high_pct": item.get("open_to_high_pct"),
            }
            for item in records[:6]
        ],
    }


def _policy_from_dimension_stats(stats: dict[str, dict]) -> dict:
    actionable_missing = stats.get("pool:actionable|risk:missing-model-score") or {}
    actionable_missing_model = stats.get("pool:actionable|model_score:missing") or {}
    actionable_st = stats.get("pool:actionable|board:st") or {}
    actionable_bias_watch = stats.get("pool:actionable|bias:watch") or {}
    policy = {
        "downgrade_risk_flags": [],
        "downgrade_model_score_missing": False,
        "exclude_actionable_board_profiles": [],
        "watch_bias_actionable_limit": None,
        "max_actionable_buy_zone_deviation_pct": None,
        "notes": [],
    }

    if int(actionable_missing.get("count") or 0) >= 2 and (
        float(actionable_missing.get("close_hit_rate") or 0.0) < 45.0
        or float(actionable_missing.get("avg_close_1d_pct") or 0.0) <= 0.0
    ):
        policy["downgrade_risk_flags"].append("missing-model-score")
        policy["notes"].append("历史可执行池里 missing-model-score 表现偏弱，后续只进观察池。")

    if int(actionable_missing_model.get("count") or 0) >= 2 and (
        float(actionable_missing_model.get("close_hit_rate") or 0.0) < 45.0
        or float(actionable_missing_model.get("avg_close_1d_pct") or 0.0) <= 0.0
    ):
        policy["downgrade_model_score_missing"] = True
        policy["notes"].append("缺少完整模型分的可执行候选表现偏弱，后续降级观察。")

    if int(actionable_st.get("count") or 0) >= 1 and (
        float(actionable_st.get("avg_close_1d_pct") or 0.0) <= 0.5
        or float(actionable_st.get("avg_open_to_low_pct") or 0.0) <= -2.0
    ):
        policy["exclude_actionable_board_profiles"].append("st")
        policy["notes"].append("ST 可执行候选的隔夜/盘中质量不足，默认不进可执行池。")

    if int(actionable_bias_watch.get("count") or 0) >= 2 and float(actionable_bias_watch.get("close_hit_rate") or 0.0) < 45.0:
        policy["watch_bias_actionable_limit"] = 1
        policy["notes"].append("当 LightGBM 偏观察时，可执行池最多保留 1 只。")

    extended = stats.get("pool:actionable|deviation:near_buy_zone_5_8") or {}
    if int(extended.get("count") or 0) >= 2 and float(extended.get("close_hit_rate") or 0.0) < 45.0:
        policy["max_actionable_buy_zone_deviation_pct"] = 5.0
        policy["notes"].append("买点上沿偏离 5%-8% 的可执行候选表现偏弱，收紧买点偏离。")

    return policy


def build_ai_report_recommendation_regression(*, db, history_limit: int = 80) -> dict:
    snapshots = WorkspaceSnapshotRepository(db).list_snapshots(
        AI_DAILY_REPORT_HISTORY_SNAPSHOT_TYPE,
        limit=history_limit,
    )
    records: list[dict] = []
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") or {}
        report_date = str(snapshot.get("snapshot_date") or payload.get("report_date") or "")[:10]
        if not report_date:
            continue
        bias = payload.get("lightgbm_execution_bias") or {}
        for row in _iter_report_candidate_rows(payload, report_date=report_date):
            metrics = _next_session_metrics(
                ticker=str(row.get("ticker") or ""),
                market=str(row.get("market") or "CN"),
                report_date=report_date,
            )
            if metrics is None:
                continue
            enriched = {
                **row,
                **metrics,
                "lightgbm_execution_bias": bias,
            }
            records.append(enriched)

    by_dimension: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        dims = _dimensions_for_row(record)
        for dim in dims:
            by_dimension[dim].append(record)
        for dim in dims:
            by_dimension[f"pool:{record.get('report_pool')}|{dim}"].append(record)

    stats = {
        key: _aggregate_records(value)
        for key, value in sorted(by_dimension.items())
    }
    actionable_records = [item for item in records if item.get("report_pool") == "actionable"]
    watch_records = [item for item in records if item.get("report_pool") == "watch"]
    payload = {
        "snapshot_type": RECOMMENDATION_REGRESSION_SNAPSHOT_TYPE,
        "generated_at": app_now_iso(),
        "snapshot_date": app_today_iso(),
        "history_reports": len(snapshots),
        "sample_count": len(records),
        "summary": {
            "all": _aggregate_records(records),
            "actionable": _aggregate_records(actionable_records),
            "watch": _aggregate_records(watch_records),
        },
        "dimension_stats": stats,
        "policy": _policy_from_dimension_stats(stats),
    }
    return payload


def save_ai_report_recommendation_regression_snapshot(*, db, source_job_id: int | None = None) -> dict:
    payload = build_ai_report_recommendation_regression(db=db)
    row = WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=RECOMMENDATION_REGRESSION_SNAPSHOT_TYPE,
        snapshot_date=app_today_iso(),
        payload=payload,
        source_job_id=source_job_id,
    )
    return {
        "id": row.id,
        "snapshot_type": row.snapshot_type,
        "snapshot_date": row.snapshot_date,
        "created_at": row.created_at,
        "sample_count": int(payload.get("sample_count") or 0),
        "policy": payload.get("policy") or {},
    }


def load_latest_recommendation_regression_snapshot(*, db) -> dict | None:
    return WorkspaceSnapshotRepository(db).get_latest_snapshot(RECOMMENDATION_REGRESSION_SNAPSHOT_TYPE)


def load_or_build_recommendation_regression(*, db) -> dict:
    def _load() -> dict:
        snapshot = load_latest_recommendation_regression_snapshot(db=db)
        if snapshot and isinstance(snapshot.get("payload"), dict) and int((snapshot.get("payload") or {}).get("sample_count") or 0) > 0:
            payload = dict(snapshot.get("payload") or {})
            payload["snapshot_meta"] = {
                "source": "snapshot",
                "snapshot_id": snapshot.get("id"),
                "snapshot_date": snapshot.get("snapshot_date"),
                "created_at": snapshot.get("created_at"),
            }
            return payload
        payload = build_ai_report_recommendation_regression(db=db)
        payload["snapshot_meta"] = {"source": "live"}
        return payload

    return get_or_set("recommendation_regression", "latest", ttl_seconds=600.0, loader=_load)
