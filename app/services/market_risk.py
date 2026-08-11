from __future__ import annotations

import math
from typing import Any

import duckdb
from sqlalchemy.orm import Session

from app.services.market_lake import load_lake_price_history, market_lake_root
from app.services.portfolio_book import load_portfolio_positions
from app.services.repository import WorkspaceSnapshotRepository
from app.services.time_utils import app_now_iso


MARKET_RISK_SNAPSHOT_PREFIX = "market_regime_snapshot"
PORTFOLIO_RISK_ALERT_SNAPSHOT_TYPE = "portfolio_risk_alert"


def market_risk_snapshot_type(market: str) -> str:
    return f"{MARKET_RISK_SNAPSHOT_PREFIX}:{str(market or '').strip().upper()}"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _round(value: Any, digits: int = 2) -> float | None:
    number = _safe_float(value)
    return round(number, digits) if number is not None else None


def _market_from_ticker(ticker: str, explicit: str | None = None) -> str:
    market = str(explicit or "").strip().upper()
    if market in {"CN", "US"}:
        return market
    symbol = str(ticker or "").strip().upper()
    if symbol.endswith((".SS", ".SZ", ".BJ")):
        return "CN"
    return "US"


def _risk_row_score(row: dict[str, Any]) -> int:
    score = 0
    if (_safe_float(row.get("median_ret_pct")) or 0.0) <= -2.0:
        score += 3
    if (_safe_float(row.get("avg_ret_pct")) or 0.0) <= -2.5:
        score += 2
    if (_safe_float(row.get("down3_pct")) or 0.0) >= 35.0:
        score += 3
    if (_safe_float(row.get("down5_pct")) or 0.0) >= 18.0:
        score += 3
    if (_safe_float(row.get("near_20d_low_pct")) or 0.0) >= 58.0:
        score += 2
    if (_safe_float(row.get("avg_range_pct")) or 0.0) >= 7.0:
        score += 1
    return score


def _classify_market(market: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "risk_regime": "unknown",
            "regime": "unknown",
            "buy_gate": "REVIEW",
            "risk_level": 50,
            "headline": "缺少行情数据，禁止盲目进攻。",
            "playbook": "先补齐行情刷新，再评估模型候选。",
            "flags": ["missing-market-data"],
        }

    latest = rows[0]
    previous = rows[1] if len(rows) > 1 else {}
    recent = rows[:5]
    latest_score = _risk_row_score(latest)
    recent_crash_days = sum(1 for row in recent if _risk_row_score(row) >= 6)
    had_recent_crash = any(_risk_row_score(row) >= 6 for row in rows[1:5])
    recent_high_dispersion = any(
        abs((_safe_float(row.get("avg_ret_pct")) or 0.0) - (_safe_float(row.get("median_ret_pct")) or 0.0)) >= 3.0
        and (_safe_float(row.get("median_ret_pct")) or 0.0) <= 0.0
        for row in rows[:3]
    )
    latest_avg = _safe_float(latest.get("avg_ret_pct")) or 0.0
    latest_median = _safe_float(latest.get("median_ret_pct")) or 0.0
    latest_up_pct = _safe_float(latest.get("up_pct")) or 0.0
    prev_avg = _safe_float(previous.get("avg_ret_pct")) or 0.0
    dispersion = abs((_safe_float(latest.get("avg_ret_pct")) or 0.0) - (_safe_float(latest.get("median_ret_pct")) or 0.0))

    flags: list[str] = []
    if latest_score >= 6:
        risk_regime = "crash"
        flags.append("broad-selloff")
    elif had_recent_crash and latest_avg < -0.5 and latest_up_pct < 40.0 and prev_avg > 0.0:
        risk_regime = "rebound_failed"
        flags.extend(["recent-crash", "rebound-failed"])
    elif had_recent_crash and latest_avg > 0.0 and latest_up_pct >= 50.0:
        risk_regime = "post_crash_rebound"
        flags.extend(["recent-crash", "post-crash-rebound"])
    elif latest_score >= 3 or recent_crash_days >= 2:
        risk_regime = "high_volatility"
        flags.append("high-volatility")
    elif market == "US" and (dispersion >= 2.0 and latest_median <= 0.0 or recent_high_dispersion):
        risk_regime = "high_dispersion"
        flags.append("index-masking-weak-breadth")
    elif latest_avg > 0.3 and latest_median > 0.0 and latest_up_pct >= 58.0:
        risk_regime = "risk_on"
    else:
        risk_regime = "watchful"

    if market == "US" and dispersion >= 2.0:
        flags.append("high-dispersion")
    if market == "US" and recent_high_dispersion:
        flags.append("recent-high-dispersion")
    if latest_median < 0.0 and latest_avg > 0.0:
        flags.append("average-masks-weak-median")
    if (_safe_float(latest.get("near_20d_low_pct")) or 0.0) >= 50.0:
        flags.append("many-near-20d-low")
    if (_safe_float(latest.get("down5_pct")) or 0.0) >= 10.0:
        flags.append("fat-left-tail")

    if risk_regime in {"crash", "rebound_failed"}:
        regime = "defensive"
        buy_gate = "BLOCK"
        max_position_scale = 0.0
        headline = "市场处于暴跌/反抽失败风险，暂停新开仓。"
        playbook = "先处理持仓止损、减仓和利润保护；模型候选只作观察，不作为买入信号。"
    elif risk_regime in {"post_crash_rebound", "high_volatility", "high_dispersion"}:
        regime = "watchful"
        buy_gate = "REVIEW"
        max_position_scale = 0.35
        headline = "市场波动仍高，反弹需要确认。"
        playbook = "不追高；只允许小仓验证、回踩确认或强约束条件触发。"
    elif risk_regime == "risk_on":
        regime = "risk_on"
        buy_gate = "ALLOW"
        max_position_scale = 1.0
        headline = "市场广度较好，可按模型纪律执行。"
        playbook = "优先高共振、高流动性、接近买入区的候选。"
    else:
        regime = "watchful"
        buy_gate = "REVIEW"
        max_position_scale = 0.6
        headline = "市场没有明显顺风，需要精选。"
        playbook = "降低候选数量，等待价格确认，不做无触发追单。"

    risk_level = min(100, max(0, 18 + latest_score * 9 + recent_crash_days * 8 + (10 if risk_regime == "rebound_failed" else 0)))
    if buy_gate == "BLOCK":
        risk_level = max(risk_level, 75)
    elif buy_gate == "REVIEW":
        risk_level = max(risk_level, 45)

    return {
        "risk_regime": risk_regime,
        "regime": regime,
        "buy_gate": buy_gate,
        "risk_level": int(risk_level),
        "max_position_scale": max_position_scale,
        "headline": headline,
        "playbook": playbook,
        "flags": sorted(set(flags)),
        "latest": latest,
        "previous": previous or None,
        "recent_crash_days": recent_crash_days,
        "diagnostics": {
            "latest_risk_score": latest_score,
            "latest_avg_ret_pct": latest_avg,
            "latest_median_ret_pct": latest_median,
            "latest_up_pct": latest_up_pct,
            "avg_minus_median_pct": _round(dispersion, 2),
        },
    }


def analyze_market_regime(market: str, *, lookback_days: int = 12) -> dict[str, Any]:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    path = str(market_lake_root() / f"{market_code.lower()}_daily" / "date=*" / "*.parquet")
    sql = """
        WITH base AS (
          SELECT
            CAST(date AS DATE) AS d,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            LAG(close) OVER(PARTITION BY symbol ORDER BY CAST(date AS DATE)) AS prev_close,
            AVG(volume) OVER(PARTITION BY symbol ORDER BY CAST(date AS DATE) ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_vol20,
            MAX(close) OVER(PARTITION BY symbol ORDER BY CAST(date AS DATE) ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20,
            MIN(close) OVER(PARTITION BY symbol ORDER BY CAST(date AS DATE) ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20
          FROM read_parquet(?, hive_partitioning=true)
          WHERE close IS NOT NULL AND close > 0
        ),
        ret AS (
          SELECT
            *,
            (close / NULLIF(prev_close, 0) - 1) AS r,
            (high / NULLIF(low, 0) - 1) AS intraday_range,
            (volume / NULLIF(avg_vol20, 0)) AS vol_ratio
          FROM base
          WHERE prev_close IS NOT NULL
        ),
        dates AS (
          SELECT DISTINCT d FROM ret ORDER BY d DESC LIMIT ?
        )
        SELECT
          CAST(r.d AS VARCHAR) AS date,
          COUNT(*) AS n,
          ROUND(AVG(r.r) * 100, 2) AS avg_ret_pct,
          ROUND(MEDIAN(r.r) * 100, 2) AS median_ret_pct,
          ROUND(SUM(CASE WHEN r.r > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS up_pct,
          ROUND(SUM(CASE WHEN r.r >= 0.03 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS up3_pct,
          ROUND(SUM(CASE WHEN r.r <= -0.03 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS down3_pct,
          ROUND(SUM(CASE WHEN r.r >= 0.05 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS up5_pct,
          ROUND(SUM(CASE WHEN r.r <= -0.05 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS down5_pct,
          ROUND(AVG(r.intraday_range) * 100, 2) AS avg_range_pct,
          ROUND(MEDIAN(r.vol_ratio), 2) AS median_vol_ratio,
          ROUND(SUM(CASE WHEN r.close < r.low20 * 1.03 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS near_20d_low_pct,
          ROUND(SUM(CASE WHEN r.close > r.high20 * 0.97 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS near_20d_high_pct
        FROM ret r
        JOIN dates USING(d)
        GROUP BY r.d
        ORDER BY r.d DESC
    """
    with duckdb.connect(database=":memory:") as connection:
        result = connection.execute(sql, [path, max(3, int(lookback_days))])
        columns = [item[0] for item in result.description]
        rows = [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
    rows = [{key: (_round(value, 2) if isinstance(value, float) else value) for key, value in row.items()} for row in rows]
    classification = _classify_market(market_code, rows)
    latest_date = str(rows[0].get("date")) if rows else None
    return {
        "snapshot_type": market_risk_snapshot_type(market_code),
        "market": market_code,
        "snapshot_date": latest_date,
        "generated_at": app_now_iso(),
        "lookback_days": max(3, int(lookback_days)),
        "regime": classification["regime"],
        "risk_regime": classification["risk_regime"],
        "buy_gate": classification["buy_gate"],
        "risk_level": classification["risk_level"],
        "max_position_scale": classification["max_position_scale"],
        "headline": classification["headline"],
        "playbook": classification["playbook"],
        "flags": classification["flags"],
        "latest": classification["latest"],
        "previous": classification["previous"],
        "recent": rows,
        "recent_crash_days": classification["recent_crash_days"],
        "diagnostics": classification["diagnostics"],
    }


def save_market_risk_snapshots(
    db: Session,
    *,
    markets: list[str] | None = None,
    source_job_id: int | None = None,
    lookback_days: int = 12,
) -> dict[str, dict[str, Any]]:
    repo = WorkspaceSnapshotRepository(db)
    payloads: dict[str, dict[str, Any]] = {}
    for market in markets or ["CN", "US"]:
        market_code = str(market or "").strip().upper()
        if market_code not in {"CN", "US"}:
            continue
        payload = analyze_market_regime(market_code, lookback_days=lookback_days)
        repo.create_snapshot(
            snapshot_type=market_risk_snapshot_type(market_code),
            snapshot_date=str(payload.get("snapshot_date") or ""),
            payload=payload,
            source_job_id=source_job_id,
        )
        payloads[market_code] = payload
    return payloads


def _moving_average(history: list[dict[str, Any]], window: int) -> float | None:
    closes = [_safe_float(row.get("close")) for row in history[-window:]]
    values = [value for value in closes if value is not None]
    if len(values) < max(2, min(window, 5)):
        return None
    return sum(values) / len(values)


def analyze_portfolio_risk(*, market_risk: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    positions = load_portfolio_positions()
    market_risk = market_risk or {}
    rows: list[dict[str, Any]] = []
    totals = {"market_value": 0.0, "cost": 0.0, "pnl": 0.0}
    for position in positions:
        ticker = str(position.get("ticker") or "").strip().upper()
        market = _market_from_ticker(ticker, position.get("market"))
        history = load_lake_price_history(market=market, ticker=ticker, limit=80)
        latest = history[-1] if history else {}
        previous = history[-2] if len(history) >= 2 else {}
        latest_close = _safe_float(latest.get("close"))
        prev_close = _safe_float(previous.get("close"))
        quantity = _safe_float(position.get("quantity")) or 0.0
        cost_basis = _safe_float(position.get("cost_basis")) or 0.0
        market_value = (latest_close or 0.0) * quantity
        cost_value = cost_basis * quantity
        pnl = market_value - cost_value
        pnl_pct = ((latest_close / cost_basis) - 1.0) * 100.0 if latest_close and cost_basis else None
        ret_1d = ((latest_close / prev_close) - 1.0) * 100.0 if latest_close and prev_close else None
        closes = [_safe_float(row.get("close")) for row in history if _safe_float(row.get("close")) is not None]
        high20 = max(closes[-20:]) if closes else None
        drawdown_from_20d_high = ((latest_close / high20) - 1.0) * 100.0 if latest_close and high20 else None
        ma5 = _moving_average(history, 5)
        ma10 = _moving_average(history, 10)
        ma20 = _moving_average(history, 20)

        flags: list[str] = []
        if latest_close is None:
            flags.append("missing-latest-price")
        if ret_1d is not None and ret_1d <= -5.0:
            flags.append("single-day-drop")
        if drawdown_from_20d_high is not None and drawdown_from_20d_high <= -12.0:
            flags.append("deep-pullback-from-20d-high")
        if latest_close is not None and ma20 is not None and latest_close < ma20:
            flags.append("below-ma20")
        if latest_close is not None and ma10 is not None and latest_close < ma10:
            flags.append("below-ma10")
        market_gate = str((market_risk.get(market) or {}).get("buy_gate") or "").upper()
        if market_gate == "BLOCK":
            flags.append("market-buy-gate-blocked")
        elif market_gate == "REVIEW":
            flags.append("market-risk-review")
        if pnl_pct is not None and pnl_pct <= -8.0:
            flags.append("position-loss")
        if pnl_pct is not None and pnl_pct >= 30.0 and drawdown_from_20d_high is not None and drawdown_from_20d_high <= -8.0:
            flags.append("protect-profit")

        if "market-buy-gate-blocked" in flags or "single-day-drop" in flags or "deep-pullback-from-20d-high" in flags:
            action = "reduce_or_protect"
            action_label = "减仓/保护利润"
        elif "below-ma20" in flags or "position-loss" in flags:
            action = "review"
            action_label = "复核止损线"
        elif "market-risk-review" in flags:
            action = "hold_off_adding"
            action_label = "暂停加仓"
        else:
            action = "hold"
            action_label = "持有观察"

        totals["market_value"] += market_value
        totals["cost"] += cost_value
        totals["pnl"] += pnl
        rows.append(
            {
                "ticker": ticker,
                "name": position.get("name"),
                "market": market,
                "quantity": quantity,
                "cost_basis": cost_basis,
                "latest_date": latest.get("date"),
                "latest_close": _round(latest_close, 4),
                "ret_1d_pct": _round(ret_1d, 2),
                "drawdown_from_20d_high_pct": _round(drawdown_from_20d_high, 2),
                "ma5": _round(ma5, 4),
                "ma10": _round(ma10, 4),
                "ma20": _round(ma20, 4),
                "market_value": _round(market_value, 2),
                "pnl": _round(pnl, 2),
                "pnl_pct": _round(pnl_pct, 2),
                "risk_flags": sorted(set(flags)),
                "risk_count": len(set(flags)),
                "action": action,
                "action_label": action_label,
            }
        )

    rows.sort(key=lambda item: (-int(item.get("risk_count") or 0), -abs(float(item.get("market_value") or 0.0)), item.get("ticker") or ""))
    risk_rows = [row for row in rows if int(row.get("risk_count") or 0) > 0]
    totals["pnl_pct"] = ((totals["market_value"] / totals["cost"]) - 1.0) * 100.0 if totals["cost"] else 0.0
    return {
        "snapshot_type": PORTFOLIO_RISK_ALERT_SNAPSHOT_TYPE,
        "snapshot_date": app_now_iso()[:10],
        "generated_at": app_now_iso(),
        "position_count": len(rows),
        "risk_count": len(risk_rows),
        "high_risk_count": sum(1 for row in rows if int(row.get("risk_count") or 0) >= 3),
        "totals": {key: _round(value, 2) for key, value in totals.items()},
        "rows": rows,
        "top_risks": risk_rows[:12],
        "headline": (
            f"持仓风险偏高：{len(risk_rows)} 只需要复核。"
            if risk_rows
            else "当前持仓没有触发硬性风险标记。"
        ),
    }


def save_portfolio_risk_alert_snapshot(
    db: Session,
    *,
    market_risk: dict[str, dict[str, Any]] | None = None,
    source_job_id: int | None = None,
) -> dict[str, Any]:
    payload = analyze_portfolio_risk(market_risk=market_risk)
    WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=PORTFOLIO_RISK_ALERT_SNAPSHOT_TYPE,
        snapshot_date=str(payload.get("snapshot_date") or ""),
        payload=payload,
        source_job_id=source_job_id,
    )
    return payload


def save_risk_guardrail_snapshots(
    db: Session,
    *,
    markets: list[str] | None = None,
    source_job_id: int | None = None,
    lookback_days: int = 12,
) -> dict[str, Any]:
    market_payloads = save_market_risk_snapshots(
        db,
        markets=markets or ["CN", "US"],
        source_job_id=source_job_id,
        lookback_days=lookback_days,
    )
    portfolio_payload = save_portfolio_risk_alert_snapshot(
        db,
        market_risk=market_payloads,
        source_job_id=source_job_id,
    )
    blocked_markets = [market for market, payload in market_payloads.items() if payload.get("buy_gate") == "BLOCK"]
    review_markets = [market for market, payload in market_payloads.items() if payload.get("buy_gate") == "REVIEW"]
    return {
        "status": "success",
        "markets": market_payloads,
        "portfolio": portfolio_payload,
        "blocked_markets": blocked_markets,
        "review_markets": review_markets,
        "message": (
            f"Risk guardrail snapshots saved for {', '.join(market_payloads.keys())}; "
            f"blocked={blocked_markets or '-'}, review={review_markets or '-'}, "
            f"portfolio risks={portfolio_payload.get('risk_count', 0)}."
        ),
    }
