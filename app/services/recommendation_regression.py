from __future__ import annotations

from collections import defaultdict
from statistics import mean

from app.services.execution_costs import default_fill_cost_model
from app.services.market_lake import load_lake_price_history
from app.services.price_basis import preferred_close
from app.services.repository import WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.time_utils import app_now_iso, app_today_iso


AI_DAILY_REPORT_HISTORY_SNAPSHOT_TYPE = "ai_daily_report_history"
RECOMMENDATION_REGRESSION_SNAPSHOT_TYPE = "ai_report_recommendation_regression"

# S-12 executable口径: next-session open -> horizon close, net of these costs.
EXECUTABLE_HORIZON_DAYS = 5
EXECUTABLE_COMMISSION_BPS = 8.0
EXECUTABLE_SLIPPAGE_BPS = 12.0


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


def _numeric_bucket(value, *, cuts: tuple[float, ...], labels: tuple[str, ...]) -> str:
    number = _safe_float(value)
    if number is None:
        return "unknown"
    for cut, label in zip(cuts, labels, strict=False):
        if number < cut:
            return label
    return labels[-1] if labels else "unknown"


def _kronos_decision_bucket(row: dict) -> str:
    validation = row.get("kronos_validation") if isinstance(row.get("kronos_validation"), dict) else {}
    decision = str(
        row.get("kronos_decision")
        or (validation or {}).get("kronos_decision")
        or ""
    ).strip().lower()
    if ("支持" in decision or "support" in decision) and "不支持" not in decision:
        return "support"
    if "不支持" in decision or "avoid" in decision or "reject" in decision:
        return "reject"
    if decision:
        return "neutral"
    return "unknown"


def _next_session_metrics(*, ticker: str, market: str, report_date: str, horizon_days: int = EXECUTABLE_HORIZON_DAYS) -> dict | None:
    history = load_lake_price_history(market=market, ticker=ticker, limit=320)
    if not history:
        return None
    ordered: list[tuple[str, dict]] = []
    for row in history:
        row_date = str(row.get("date") or row.get("trade_date") or "")[:10]
        if row_date:
            ordered.append((row_date, row))
    ordered.sort(key=lambda item: item[0])
    baseline_index = None
    for index, (row_date, _row) in enumerate(ordered):
        if row_date <= report_date:
            baseline_index = index
        else:
            break
    if baseline_index is None or baseline_index + 1 >= len(ordered):
        return None
    baseline = ordered[baseline_index][1]
    entry_index = baseline_index + 1
    next_row = ordered[entry_index][1]
    # R2: close-to-close return prefers the adjusted view.  The raw close is
    # retained for the overnight gap and intraday open-to-close ratios, whose
    # other leg (raw open) is not adjusted -- mixing bases there would change
    # their meaning, not just their scale.
    raw_base_close = _safe_float(baseline.get("close"))
    base_close = preferred_close(baseline) or raw_base_close
    next_open = _safe_float(next_row.get("open"))
    next_high = _safe_float(next_row.get("high"))
    next_low = _safe_float(next_row.get("low"))
    raw_next_close = _safe_float(next_row.get("close"))
    next_close = preferred_close(next_row) or raw_next_close
    if not base_close or not next_open or not next_high or not next_low or not next_close:
        return None

    def pct(start: float, end: float) -> float:
        return round((end / start - 1.0) * 100.0, 2)

    open_to_high = pct(next_open, next_high)
    open_to_low = pct(next_open, next_low)
    open_to_close = pct(next_open, raw_next_close or next_close)
    close_1d = pct(base_close, next_close)
    gap_open = pct(raw_base_close or base_close, next_open)

    # S-12 executable口径: enter at the next session open, exit at the horizon
    # close (next session open -> expiry close), net of the round-trip cost
    # model.  This is the metric a book could actually have realized, unlike
    # the close-to-close observation口径 above.
    exit_index = min(entry_index + max(1, int(horizon_days)) - 1, len(ordered) - 1)
    exit_row = ordered[exit_index][1]
    exit_close = preferred_close(exit_row) or _safe_float(exit_row.get("close"))
    executable_metrics: dict = {
        "executable_entry_date": ordered[entry_index][0],
        "executable_exit_date": ordered[exit_index][0],
        "executable_holding_days": exit_index - entry_index + 1,
        "executable_gross_return_pct": None,
        "executable_return_pct": None,
        "executable_hit": None,
        "round_trip_cost_bps": None,
        "cost_model_version": None,
    }
    if exit_close:
        cost_model = default_fill_cost_model(
            market,
            commission_bps_one_way=EXECUTABLE_COMMISSION_BPS,
            slippage_bps_one_way=EXECUTABLE_SLIPPAGE_BPS,
        )
        round_trip = cost_model.round_trip(float(next_open), float(exit_close), quantity=1.0)
        executable_metrics.update(
            {
                "executable_gross_return_pct": round(float(round_trip["gross_return"]) * 100.0, 2),
                "executable_return_pct": round(float(round_trip["net_return"]) * 100.0, 2),
                "executable_hit": float(round_trip["net_return"]) > 0,
                "round_trip_cost_bps": round(float(cost_model.nominal_round_trip_bps), 2),
                "cost_model_version": cost_model.version,
            }
        )
    return {
        "ticker": ticker,
        "market": market,
        "report_date": report_date,
        "next_date": ordered[entry_index][0],
        "gap_open_pct": gap_open,
        "open_to_high_pct": open_to_high,
        "open_to_low_pct": open_to_low,
        "open_to_close_pct": open_to_close,
        "close_1d_pct": close_1d,
        "close_hit": close_1d > 0,
        "execution_hit": open_to_high >= 2.0 and open_to_low > -4.0,
        "gap_blocked": gap_open >= 7.0,
        "deep_intraday_drawdown": open_to_low <= -4.0,
        **executable_metrics,
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
        f"kronos:{_kronos_decision_bucket(row)}",
        f"readiness:{_numeric_bucket(row.get('trade_readiness_score'), cuts=(45.0, 60.0, 75.0, 90.0), labels=('lt45', '45_60', '60_75', '75_90', 'gte90'))}",
        f"verification:{_numeric_bucket(row.get('verification_score'), cuts=(90.0, 115.0, 140.0, 170.0), labels=('lt90', '90_115', '115_140', '140_170', 'gte170'))}",
        f"quality:{_numeric_bucket(row.get('quality_gate_score'), cuts=(45.0, 52.0, 58.0, 66.0), labels=('lt45', '45_52', '52_58', '58_66', 'gte66'))}",
        "model_score:present" if row.get("model_score") is not None or row.get("score") is not None else "model_score:missing",
    ]
    dimensions.extend(f"risk:{flag}" for flag in risk_flags[:6])
    if (row.get("lightgbm_execution_bias") or {}).get("action"):
        dimensions.append(f"bias:{(row.get('lightgbm_execution_bias') or {}).get('action')}")
    return dimensions


def _bucket_hit_rate(bucket: dict) -> float:
    """S-12: prefer the fee-adjusted executable hit rate for policy gates."""
    if bucket.get("executable_hit_rate") is not None:
        return float(bucket["executable_hit_rate"])
    return float(bucket.get("execution_hit_rate") or 0.0)


def _aggregate_records(records: list[dict]) -> dict:
    executable_records = [item for item in records if item.get("executable_return_pct") is not None]
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
            "executable_count": 0,
            "avg_executable_return_pct": None,
            "executable_hit_rate": None,
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
        # S-12: the headline hit metric is the executable, fee-adjusted口径.
        "executable_count": len(executable_records),
        "avg_executable_return_pct": (
            round(mean(float(item["executable_return_pct"]) for item in executable_records), 2)
            if executable_records
            else None
        ),
        "executable_hit_rate": (
            round(sum(1 for item in executable_records if item.get("executable_hit")) / len(executable_records) * 100.0, 1)
            if executable_records
            else None
        ),
        "examples": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "report_date": item.get("report_date"),
                "next_date": item.get("next_date"),
                "close_1d_pct": item.get("close_1d_pct"),
                "open_to_high_pct": item.get("open_to_high_pct"),
                "executable_return_pct": item.get("executable_return_pct"),
            }
            for item in records[:6]
        ],
    }


def _recent_record_view(records: list[dict], *, limit: int = 30) -> list[dict]:
    ranked = sorted(
        records,
        key=lambda item: (
            str(item.get("report_date") or ""),
            str(item.get("report_pool") or ""),
            -int(item.get("report_rank") or 0),
        ),
        reverse=True,
    )
    return [
        {
            "ticker": item.get("ticker"),
            "name": item.get("name"),
            "market": item.get("market"),
            "report_date": item.get("report_date"),
            "next_date": item.get("next_date"),
            "report_pool": item.get("report_pool"),
            "report_rank": item.get("report_rank"),
            "template": item.get("full_market_template") or item.get("report_source_label"),
            "tradability_status": item.get("tradability_status"),
            "kronos_decision": _kronos_decision_bucket(item),
            "trade_readiness_score": item.get("trade_readiness_score"),
            "verification_score": item.get("verification_score"),
            "quality_gate_score": item.get("quality_gate_score"),
            "risk_flags": list(item.get("risk_flags") or [])[:5],
            "gap_open_pct": item.get("gap_open_pct"),
            "open_to_high_pct": item.get("open_to_high_pct"),
            "open_to_low_pct": item.get("open_to_low_pct"),
            "open_to_close_pct": item.get("open_to_close_pct"),
            "close_1d_pct": item.get("close_1d_pct"),
            "close_hit": item.get("close_hit"),
            "execution_hit": item.get("execution_hit"),
            "executable_entry_date": item.get("executable_entry_date"),
            "executable_exit_date": item.get("executable_exit_date"),
            "executable_return_pct": item.get("executable_return_pct"),
            "executable_hit": item.get("executable_hit"),
            "gap_blocked": item.get("gap_blocked"),
            "deep_intraday_drawdown": item.get("deep_intraday_drawdown"),
        }
        for item in ranked[: max(1, int(limit))]
    ]


def _latest_report_dates(records: list[dict], *, limit: int = 5) -> set[str]:
    dates = sorted({str(item.get("report_date") or "") for item in records if item.get("report_date")})
    return set(dates[-max(1, int(limit)):])


def _policy_from_dimension_stats(stats: dict[str, dict], *, summary: dict | None = None) -> dict:
    actionable_missing = stats.get("pool:actionable|risk:missing-model-score") or {}
    actionable_missing_model = stats.get("pool:actionable|model_score:missing") or {}
    actionable_st = stats.get("pool:actionable|board:st") or {}
    actionable_bias_watch = stats.get("pool:actionable|bias:watch") or {}
    actionable_summary = stats.get("pool:actionable") or {}
    policy = {
        "downgrade_risk_flags": [],
        "downgrade_model_score_missing": False,
        "exclude_actionable_board_profiles": [],
        "downgrade_actionable_board_profiles": [],
        "downgrade_templates": [],
        "preferred_templates": [],
        "preferred_board_profiles": [],
        "downgrade_kronos_decisions": [],
        "preferred_kronos_decisions": [],
        "watch_bias_actionable_limit": None,
        "max_actionable_count": None,
        "max_actionable_buy_zone_deviation_pct": None,
        "min_actionable_quality_score": None,
        "notes": [],
    }

    def _is_weak(bucket: dict, *, min_count: int = 8) -> bool:
        if int(bucket.get("count") or 0) < min_count:
            return False
        execution_hit = float(bucket.get("execution_hit_rate") or 0.0)
        hit = bucket.get("executable_hit_rate")
        if hit is None:
            hit = bucket.get("close_hit_rate")
        avg_return = bucket.get("avg_executable_return_pct")
        if avg_return is None:
            avg_return = bucket.get("avg_close_1d_pct")
        return execution_hit < 45.0 or float(hit or 0.0) < 42.0 or float(avg_return or 0.0) <= -0.2

    def _is_strong(bucket: dict, *, min_count: int = 12) -> bool:
        if int(bucket.get("count") or 0) < min_count:
            return False
        execution_hit = float(bucket.get("execution_hit_rate") or 0.0)
        hit = bucket.get("executable_hit_rate")
        if hit is None:
            hit = bucket.get("close_hit_rate")
        avg_return = bucket.get("avg_executable_return_pct")
        if avg_return is None:
            avg_return = bucket.get("avg_close_1d_pct")
        return execution_hit >= 58.0 and float(hit or 0.0) >= 48.0 and float(avg_return or 0.0) >= 0.2

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

    for risk_flag in (
        "weak-market",
        "weak-breadth",
        "weak-signal-strength",
        "low-conviction",
        "drawdown-risk",
        "chase-risk",
    ):
        bucket = stats.get(f"pool:actionable|risk:{risk_flag}") or {}
        if _is_weak(bucket, min_count=6):
            policy["downgrade_risk_flags"].append(risk_flag)
            policy["notes"].append(f"历史回归显示 {risk_flag} 可执行候选兑现偏弱，后续降级观察。")

    extended = stats.get("pool:actionable|deviation:near_buy_zone_5_8") or {}
    if int(extended.get("count") or 0) >= 2 and float(extended.get("close_hit_rate") or 0.0) < 45.0:
        policy["max_actionable_buy_zone_deviation_pct"] = 5.0
        policy["notes"].append("买点上沿偏离 5%-8% 的可执行候选表现偏弱，收紧买点偏离。")

    for template in (
        "technical_momentum",
        "cn_volume_breakout",
        "cn_bollinger_squeeze_watch",
        "cn_three_white_soldiers",
        "lightgbm_top_picks",
    ):
        bucket = stats.get(f"pool:actionable|template:{template}") or {}
        if _is_weak(bucket):
            policy["downgrade_templates"].append(template)
            policy["notes"].append(f"{template} 近期可执行样本表现偏弱，后续只在额外共振时保留，否则降级观察。")
        elif _is_strong(bucket):
            policy["preferred_templates"].append(template)
            policy["notes"].append(f"{template} 近期可执行样本兑现较好，后续同等条件下优先排序。")

    for decision in ("reject", "neutral", "unknown", "support"):
        bucket = stats.get(f"pool:actionable|kronos:{decision}") or {}
        if decision in {"reject", "neutral"} and _is_weak(bucket, min_count=6):
            policy["downgrade_kronos_decisions"].append(decision)
            policy["notes"].append(f"Kronos={decision} 的可执行候选近期兑现偏弱，后续降级观察。")
        elif decision == "support" and _is_strong(bucket, min_count=8):
            policy["preferred_kronos_decisions"].append("support")
            policy["notes"].append("Kronos 支持的可执行候选近期兑现较好，后续同等条件下优先。")

    for board in ("main", "chinext", "star", "bse"):
        bucket = stats.get(f"pool:actionable|board:{board}") or {}
        if _is_weak(bucket, min_count=12):
            policy["downgrade_actionable_board_profiles"].append(board)
            policy["notes"].append(f"{board} 板块近期可执行样本回撤/胜率不理想，后续降低买入优先级。")
        elif _is_strong(bucket, min_count=12):
            policy["preferred_board_profiles"].append(board)
            policy["notes"].append(f"{board} 板块近期可执行样本强度较好，后续同等条件下优先。")

    if (
        int(actionable_summary.get("count") or 0) >= 20
        and _bucket_hit_rate(actionable_summary) < 55.0
    ):
        policy["min_actionable_quality_score"] = 52.0
        policy["notes"].append("整体可执行池命中率未到 55%，临时抬高 Top 5 质量门槛。")

    recent_actionable = (summary or {}).get("recent_actionable") or {}
    if int(recent_actionable.get("count") or 0) >= 12:
        recent_hit = _bucket_hit_rate(recent_actionable)
        recent_close = float(recent_actionable.get("close_hit_rate") or 0.0)
        recent_avg_close = float(recent_actionable.get("avg_close_1d_pct") or 0.0)
        recent_drawdown = float(recent_actionable.get("deep_drawdown_rate") or 0.0)
        if recent_hit < 50.0 or recent_drawdown >= 30.0:
            policy["min_actionable_quality_score"] = max(float(policy.get("min_actionable_quality_score") or 0.0), 56.0)
            policy["notes"].append("最近几期可执行池命中/回撤不理想，进一步抬高明日 Top 5 质量门槛。")
        if recent_close < 45.0 or recent_avg_close <= 0.0:
            policy["min_actionable_quality_score"] = max(float(policy.get("min_actionable_quality_score") or 0.0), 58.0)
            policy["max_actionable_count"] = 3
            policy["notes"].append("最近几期可执行池收盘胜率或隔夜收益偏弱，明日可执行池最多保留 3 只高质量候选。")

    recent_all = (summary or {}).get("recent_all") or {}
    recent_watch = (summary or {}).get("recent_watch") or {}
    if int(recent_all.get("count") or 0) >= 20:
        recent_all_hit = float(recent_all.get("execution_hit_rate") or 0.0)
        recent_all_drawdown = float(recent_all.get("deep_drawdown_rate") or 0.0)
        recent_watch_drawdown = float(recent_watch.get("deep_drawdown_rate") or 0.0)
        if recent_all_hit < 40.0 or recent_all_drawdown >= 40.0 or recent_watch_drawdown >= 55.0:
            policy["max_actionable_count"] = min(int(policy.get("max_actionable_count") or 3), 3)
            policy["min_actionable_quality_score"] = max(float(policy.get("min_actionable_quality_score") or 0.0), 60.0)
            policy["notes"].append("最近整体候选深回撤偏高，系统进入防守选股模式：宁缺毋滥，最多给 3 只可执行票。")

    return policy


def summarize_recommendation_regression(payload: dict | None, *, lang: str = "zh") -> dict:
    regression = payload or {}
    summary = regression.get("summary") or {}
    actionable = summary.get("actionable") or {}
    watch = summary.get("watch") or {}
    policy = regression.get("policy") or {}
    sample_count = int(regression.get("sample_count") or 0)

    def _fmt_pct(value) -> str:
        try:
            return f"{float(value):.1f}%"
        except (TypeError, ValueError):
            return "-"

    action_hit = actionable.get("execution_hit_rate")
    # S-12: the headline "复盘命中" is the executable, fee-adjusted口径 when
    # available; the intraday observation metric stays as a secondary line.
    action_executable_hit = actionable.get("executable_hit_rate")
    action_executable_return = actionable.get("avg_executable_return_pct")
    action_close = actionable.get("close_hit_rate")
    action_open_high = actionable.get("avg_open_to_high_pct")
    deep_drawdown = actionable.get("deep_drawdown_rate")
    gap_blocked = actionable.get("gap_blocked_rate")
    watch_hit = watch.get("execution_hit_rate")
    headline_hit = action_executable_hit if action_executable_hit is not None else action_hit

    if sample_count <= 0:
        return {
            "headline": "还没有足够的日报推荐回归样本" if lang == "zh" else "Not enough archived recommendation outcomes yet",
            "stance": "collect_more",
            "metrics": [],
            "rules": [
                "先连续保留 AI 日报 Top 5 和观察池，等第二天行情补齐后再评估命中率。"
                if lang == "zh"
                else "Keep archiving AI report Top 5 and watch-pool names; evaluate once next-session data arrives."
            ],
            "warnings": [],
        }

    stance = "balanced"
    if headline_hit is not None and float(headline_hit) >= 55.0 and deep_drawdown is not None and float(deep_drawdown) <= 25.0:
        stance = "trust_actionable"
    elif headline_hit is not None and float(headline_hit) < 45.0:
        stance = "tighten"
    elif watch_hit is not None and headline_hit is not None and float(watch_hit) > float(headline_hit) + 8.0:
        stance = "prefer_watch_confirm"

    if lang == "zh":
        headline_map = {
            "trust_actionable": "近期可执行池有兑现度，可以继续按“触发后买、不追高”使用",
            "tighten": "近期可执行池命中偏弱，明天需要收紧买点和仓位",
            "prefer_watch_confirm": "观察池反而更强，明天优先等二次确认，不直接追日报 Top 5",
            "balanced": "近期样本中性，继续用多模型共振 + 次日确认过滤",
        }
        rules = [
            f"可执行池次日执行命中率 {_fmt_pct(action_hit)}，开盘到最高平均 {_fmt_pct(action_open_high)}；只有开盘后 15-30 分钟继续走强才执行。",
            f"如果开盘跳空偏高，先看缺口风险；历史高开拦截率 {_fmt_pct(gap_blocked)}，不要把“买不到的涨幅”算成模型胜利。",
            f"盘中深回撤率 {_fmt_pct(deep_drawdown)}；若开盘后快速跌破开盘价 3%-4%，当天放弃，不做摊平。",
            "优先买：可执行池 + 多模型/LightGBM 支持 + 无硬风险标签 + 接近买入区。",
            "降权：缺模型分、ST/高风险标签、偏离买点过远、只有单一技术模板命中的股票。",
        ]
        warnings = list(policy.get("notes") or [])
    else:
        headline_map = {
            "trust_actionable": "Recent executable candidates are converting; keep using trigger-first entries without chasing.",
            "tighten": "Recent executable hit rate is weak; tighten entries and reduce sizing tomorrow.",
            "prefer_watch_confirm": "Watch-pool names are confirming better; wait for secondary confirmation before acting.",
            "balanced": "Recent samples are mixed; keep using confluence plus next-session confirmation.",
        }
        rules = [
            f"Executable-pool execution hit rate is {_fmt_pct(action_hit)}, with avg open-to-high {_fmt_pct(action_open_high)}; only act after 15-30 minute strength confirmation.",
            f"Check gap risk first; historical gap-block rate is {_fmt_pct(gap_blocked)}, so do not count unbuyable gaps as model wins.",
            f"Deep intraday drawdown rate is {_fmt_pct(deep_drawdown)}; if price quickly drops 3%-4% below open, pass instead of averaging down.",
            "Prefer: executable pool + model/LightGBM support + no hard risk flags + near buy zone.",
            "Downgrade: missing model score, ST/high-risk tags, far above buy zone, or single-template-only signals.",
        ]
        warnings = list(policy.get("notes") or [])

    return {
        "headline": headline_map.get(stance) or headline_map["balanced"],
        "stance": stance,
        "metrics": [
            {"label": "可执行样本" if lang == "zh" else "Executable samples", "value": str(actionable.get("count") or 0)},
            {"label": "可执行命中率(扣费)" if lang == "zh" else "Executable hit rate (net)", "value": _fmt_pct(headline_hit)},
            {"label": "可执行净收益" if lang == "zh" else "Executable net return", "value": _fmt_pct(action_executable_return)},
            {"label": "执行命中率" if lang == "zh" else "Execution hit rate", "value": _fmt_pct(action_hit)},
            {"label": "收盘命中率" if lang == "zh" else "Close hit rate", "value": _fmt_pct(action_close)},
            {"label": "观察池命中" if lang == "zh" else "Watch hit rate", "value": _fmt_pct(watch_hit)},
        ],
        "rules": rules,
        "warnings": warnings,
    }


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
    recent_dates = _latest_report_dates(records, limit=5)
    recent_records = [item for item in records if str(item.get("report_date") or "") in recent_dates]
    recent_actionable_records = [item for item in recent_records if item.get("report_pool") == "actionable"]
    recent_watch_records = [item for item in recent_records if item.get("report_pool") == "watch"]
    summary = {
        "all": _aggregate_records(records),
        "actionable": _aggregate_records(actionable_records),
        "watch": _aggregate_records(watch_records),
        "recent_all": _aggregate_records(recent_records),
        "recent_actionable": _aggregate_records(recent_actionable_records),
        "recent_watch": _aggregate_records(recent_watch_records),
    }
    payload = {
        "snapshot_type": RECOMMENDATION_REGRESSION_SNAPSHOT_TYPE,
        "generated_at": app_now_iso(),
        "snapshot_date": app_today_iso(),
        "history_reports": len(snapshots),
        "sample_count": len(records),
        "summary": summary,
        "dimension_stats": stats,
        "policy": _policy_from_dimension_stats(stats, summary=summary),
        "recent_records": _recent_record_view(records, limit=36),
    }
    payload["guidance"] = summarize_recommendation_regression(payload, lang="zh")
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
        if (
            snapshot
            and isinstance(snapshot.get("payload"), dict)
            and int((snapshot.get("payload") or {}).get("sample_count") or 0) > 0
            and (snapshot.get("payload") or {}).get("recent_records")
        ):
            payload = dict(snapshot.get("payload") or {})
            if isinstance(payload.get("dimension_stats"), dict) and isinstance(payload.get("summary"), dict):
                # Recompute the policy with the current rule set so old snapshots
                # immediately benefit from newer accuracy controls.
                payload["policy"] = _policy_from_dimension_stats(
                    payload.get("dimension_stats") or {},
                    summary=payload.get("summary") or {},
                )
                payload["guidance"] = summarize_recommendation_regression(payload, lang="zh")
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
