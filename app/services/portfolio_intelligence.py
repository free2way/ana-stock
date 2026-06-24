from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.model_signal_summary import build_signal_label, entry_style
from app.services.portfolio_book import load_portfolio_positions
from app.services.price_snapshot import load_latest_closes
from app.services.recommendation_regression import load_or_build_recommendation_regression
from app.services.repository import PredictionRepository, SymbolRepository
from app.services.runtime_cache import get_or_set


def _action_from_score(score: float | None, *, lang: str) -> str:
    if score is None:
        return "等待更多数据" if lang == "zh" else "Wait for more data"
    value = float(score)
    if value >= 0.18:
        return "可考虑加仓" if lang == "zh" else "Consider adding"
    if value >= 0.05:
        return "继续持有观察" if lang == "zh" else "Hold and monitor"
    if value <= -0.05:
        return "考虑减仓或退出" if lang == "zh" else "Trim or exit"
    return "暂时持有" if lang == "zh" else "Hold for now"


def _translate_signal_label(value: str | None, *, lang: str) -> str:
    normalized = str(value or "").strip().lower()
    if lang != "zh":
        mapping = {
            "买点": "Buy",
            "卖点": "Sell",
            "观察": "Watch",
            "持有": "Hold",
        }
        return mapping.get(str(value or "").strip(), value or "Hold")
    mapping = {
        "buy": "买点",
        "strong_buy": "强买点",
        "sell": "卖点",
        "strong_sell": "强卖点",
        "watch": "观察",
        "hold": "持有",
        "avoid": "回避",
        "wait": "等待确认",
    }
    return mapping.get(normalized, value or "持有")


def _translate_entry_style(value: str | None, *, lang: str) -> str:
    normalized = str(value or "").strip().lower()
    if lang != "zh":
        return value or "Wait"
    mapping = {
        "avoid": "回避，不加仓",
        "wait": "等待确认",
        "pullback": "回踩确认后再考虑",
        "buy_the_dip": "回踩确认后再考虑",
        "breakout": "突破确认后再考虑",
        "momentum": "动量延续，严控追高",
        "hold": "维持持仓",
    }
    return mapping.get(normalized, value or "等待确认")


def build_portfolio_ai_summary(
    *,
    latest_signal: dict | None,
    pnl_pct: float,
    cost_basis: float,
    lang: str,
) -> dict:
    """Translate model output into position-management language."""
    signal = latest_signal or {}
    score = signal.get("score")
    score_value = float(score) if score is not None else None
    tradability_status = str(signal.get("tradability_status") or "").strip().upper()
    invalidation_condition = str(signal.get("invalidation_condition") or "").strip()
    target_weight = signal.get("target_weight")
    verdict = build_signal_label(score_value, lang=lang) if score_value is not None else _translate_signal_label(signal.get("signal_label"), lang=lang)
    if not verdict:
        verdict = "持有" if lang == "zh" else "Hold"

    suggested_style = entry_style(
        score_value,
        lang=lang,
        signal_label_value=signal.get("signal_label"),
        signal_strength_value=signal.get("signal_strength"),
        reward_risk_ratio=signal.get("model_reward_risk_ratio"),
    ) if score_value is not None else _translate_entry_style(signal.get("entry_style"), lang=lang)
    raw_note = str(signal.get("execution_note") or "").strip()
    if raw_note:
        strategy = raw_note
    else:
        strategy = suggested_style or ("等待确认" if lang == "zh" else "Wait for confirmation")

    if lang == "zh":
        if cost_basis <= 0:
            headline = "缺少成本价，先补齐成本后再判断盈亏和风险。"
            strategy = "先补成本价，再看是否需要调仓"
        elif tradability_status == "BLOCKED":
            headline = "当前信号不支持继续进攻，优先按失效条件和仓位纪律处理。"
            strategy = "不加仓，先看失效位是否被破坏"
        elif tradability_status in {"DEFER", "REVIEW"}:
            headline = "当前更适合复核和等待确认，不建议直接放大风险。"
            strategy = raw_note or "先观察触发条件与失效位，再决定是否调整"
        elif pnl_pct <= -8:
            headline = "亏损已偏大，优先检查止损位和退出条件。"
            strategy = "先控回撤，不建议加仓摊平"
        elif pnl_pct <= -3:
            headline = "持仓小幅回撤，先观察是否跌破关键支撑。"
            strategy = "维持轻仓观察，跌破计划位则减仓"
        elif pnl_pct >= 30:
            headline = "浮盈较高，重点考虑保护利润和分批止盈。"
            strategy = "上移止损，强势冲高可分批兑现"
        elif pnl_pct >= 12:
            headline = "已有较好浮盈，继续持有但不宜盲目追高。"
            strategy = "持有跟踪，回落破位再减仓"
        elif score_value is not None and score_value <= -0.05:
            headline = "模型态度偏弱，优先复核减仓或退出条件。"
            strategy = "不加仓，等待信号修复"
        elif score_value is not None and score_value >= 0.05:
            headline = "模型仍偏正面，持仓可以继续观察趋势延续。"
            strategy = _translate_entry_style(strategy, lang=lang)
        else:
            headline = "信号暂不强，先看仓位和执行风险再决定调整。"
            strategy = _translate_entry_style(strategy, lang=lang)
    else:
        if cost_basis <= 0:
            headline = "Cost basis is missing; fill it before judging PnL risk."
            strategy = "Complete cost basis first"
        elif tradability_status == "BLOCKED":
            headline = "Current signal does not support adding risk; manage via invalidation and sizing discipline."
            strategy = "Do not add; respect invalidation first"
        elif tradability_status in {"DEFER", "REVIEW"}:
            headline = "This position needs confirmation before taking more risk."
            strategy = raw_note or "Review trigger and invalidation before adjusting"
        elif pnl_pct <= -8:
            headline = "Drawdown is elevated; review stop and exit conditions first."
            strategy = "Control drawdown; do not average down"
        elif pnl_pct >= 30:
            headline = "Unrealized gain is high; protect profit and consider staged trims."
            strategy = "Trail stops and trim into strength"
        elif score_value is not None and score_value <= -0.05:
            headline = "Model posture is weak; review trim or exit conditions."
            strategy = "Do not add until the signal repairs"
        else:
            headline = "Review sizing drift and execution risk before adjusting."

    if lang == "zh":
        extra_parts: list[str] = []
        if target_weight is not None:
            try:
                extra_parts.append(f"目标仓位 {float(target_weight) * 100.0:.1f}%")
            except (TypeError, ValueError):
                pass
        if invalidation_condition and tradability_status in {"BLOCKED", "DEFER", "REVIEW"}:
            extra_parts.append(f"失效位 {invalidation_condition}")
        if extra_parts:
            strategy = f"{strategy} · {'；'.join(extra_parts)}"
        if tradability_status == "BLOCKED":
            key_hint = "暂停加仓，优先守失效位"
        elif tradability_status in {"DEFER", "REVIEW"}:
            key_hint = "先等确认，不急着动"
        elif pnl_pct <= -8:
            key_hint = "先控回撤，别摊平"
        elif pnl_pct >= 12:
            key_hint = "优先保护利润"
        elif target_weight is not None:
            try:
                key_hint = f"围绕目标仓位 {float(target_weight) * 100.0:.1f}% 管理"
            except (TypeError, ValueError):
                key_hint = "按计划仓位管理"
        else:
            key_hint = "按计划跟踪，不追单"
    else:
        extra_parts = []
        if target_weight is not None:
            try:
                extra_parts.append(f"target {float(target_weight) * 100.0:.1f}%")
            except (TypeError, ValueError):
                pass
        if invalidation_condition and tradability_status in {"BLOCKED", "DEFER", "REVIEW"}:
            extra_parts.append(f"invalidation {invalidation_condition}")
        if extra_parts:
            strategy = f"{strategy} | {'; '.join(extra_parts)}"
        if tradability_status == "BLOCKED":
            key_hint = "Do not add; respect invalidation"
        elif tradability_status in {"DEFER", "REVIEW"}:
            key_hint = "Wait for confirmation first"
        elif pnl_pct <= -8:
            key_hint = "Control drawdown first"
        elif pnl_pct >= 12:
            key_hint = "Protect open profit first"
        elif target_weight is not None:
            try:
                key_hint = f"Manage around target {float(target_weight) * 100.0:.1f}%"
            except (TypeError, ValueError):
                key_hint = "Manage to target size"
        else:
            key_hint = "Follow the plan; avoid impulse adds"

    return {
        "ai_verdict": verdict,
        "ai_headline": headline,
        "ai_strategy": strategy,
        "key_hint": key_hint,
    }


def build_position_management_fields(
    *,
    latest_signal: dict | None,
    pnl_pct: float,
    market_value: float,
    total_market_value: float,
    cost_basis: float,
    lang: str,
) -> dict:
    signal = latest_signal or {}
    score = signal.get("score")
    score_value = float(score) if score is not None else None
    current_weight_pct = (market_value / total_market_value * 100.0) if total_market_value else 0.0

    explicit_target = signal.get("target_weight")
    if explicit_target is not None:
        target_weight_pct = float(explicit_target) * 100.0
        source = "model"
    elif cost_basis <= 0:
        target_weight_pct = min(current_weight_pct, 3.0) if current_weight_pct else 0.0
        source = "derived"
    elif pnl_pct <= -8 or (score_value is not None and score_value <= -0.05):
        target_weight_pct = max(0.0, min(current_weight_pct, 3.0))
        source = "derived"
    elif pnl_pct >= 30:
        target_weight_pct = max(3.0, min(current_weight_pct * 0.75, 8.0))
        source = "derived"
    elif score_value is not None and score_value >= 0.18:
        target_weight_pct = min(max(current_weight_pct, 8.0), 12.0)
        source = "derived"
    elif score_value is not None and score_value >= 0.05:
        target_weight_pct = min(max(current_weight_pct, 5.0), 8.0)
        source = "derived"
    else:
        target_weight_pct = min(max(current_weight_pct, 3.0), 6.0)
        source = "derived"

    raw_bucket = str(signal.get("action_bucket") or "").strip().lower()
    if raw_bucket:
        bucket_key = raw_bucket
    elif cost_basis <= 0:
        bucket_key = "complete_cost"
    elif pnl_pct <= -8 or (score_value is not None and score_value <= -0.05):
        bucket_key = "risk_reduction"
    elif pnl_pct >= 30:
        bucket_key = "profit_protection"
    elif score_value is not None and score_value >= 0.05:
        bucket_key = "hold_watch"
    else:
        bucket_key = "maintain"

    if lang == "zh":
        bucket_labels = {
            "opportunity": "机会跟踪",
            "risk_reduction": "风险收缩",
            "profit_protection": "保护利润",
            "hold_watch": "持有观察",
            "maintain": "维持仓位",
            "complete_cost": "补成本价",
            "review": "复核",
            "defer": "暂缓",
            "blocked": "暂不操作",
        }
    else:
        bucket_labels = {
            "opportunity": "Opportunity",
            "risk_reduction": "Risk Reduction",
            "profit_protection": "Profit Protection",
            "hold_watch": "Hold Watch",
            "maintain": "Maintain",
            "complete_cost": "Complete Cost",
            "review": "Review",
            "defer": "Defer",
            "blocked": "Blocked",
        }

    return {
        "target_weight_pct": target_weight_pct,
        "target_weight_text": f"{target_weight_pct:.1f}%" if target_weight_pct > 0 else "-",
        "target_weight_source": source,
        "current_weight_pct": current_weight_pct,
        "action_bucket": bucket_labels.get(bucket_key, bucket_key or "-"),
        "action_bucket_key": bucket_key,
    }


def _action_priority(*, pnl_pct: float, weight_pct: float, signal_label: str, lang: str) -> str:
    normalized_signal = str(signal_label or "").strip().lower()
    if pnl_pct <= -8 or weight_pct >= 35 or normalized_signal in {"卖点", "sell"}:
        return "高" if lang == "zh" else "High"
    if normalized_signal in {"买点", "buy"} or pnl_pct >= 12:
        return "中" if lang == "zh" else "Medium"
    return "低" if lang == "zh" else "Low"


def _risk_tag(*, pnl_pct: float, market_value: float, total_market_value: float, lang: str) -> str:
    weight_pct = (market_value / total_market_value * 100.0) if total_market_value else 0.0
    if pnl_pct <= -8:
        return "回撤偏大" if lang == "zh" else "Deep drawdown"
    if weight_pct >= 35:
        return "仓位过重" if lang == "zh" else "Heavy weight"
    if pnl_pct >= 15:
        return "浮盈较高" if lang == "zh" else "Large unrealized gain"
    return "正常跟踪" if lang == "zh" else "Normal watch"


def _action_reason(*, pnl_pct: float, weight_pct: float, signal_label: str, lang: str) -> str:
    normalized_signal = str(signal_label or "").strip().lower()
    if pnl_pct <= -8:
        return "近期回撤较大，先控制风险再决定是否继续持有。" if lang == "zh" else "Recent drawdown is large; control risk before deciding to keep holding."
    if weight_pct >= 35:
        return "单一持仓占比偏高，建议先检查是否需要分散暴露。" if lang == "zh" else "This position is too large; consider reducing concentration."
    if normalized_signal in {"卖点", "sell"}:
        return "模型态度已转弱，适合重新评估仓位纪律。" if lang == "zh" else "Model posture has weakened; re-evaluate position discipline."
    if normalized_signal in {"买点", "buy"} and pnl_pct >= 0:
        return "模型仍偏正面，且仓位处于盈利区间，可继续跟踪趋势。" if lang == "zh" else "Model remains constructive and the position is profitable, so trend-following still makes sense."
    return "当前更适合跟踪确认，而不是立刻做激进调整。" if lang == "zh" else "This looks more like a monitoring case than an aggressive change."


def _rebalance_gap_label(*, weight_pct: float, target_weight: float | None, lang: str) -> str:
    if target_weight is None:
        return "缺少目标仓位" if lang == "zh" else "Missing target weight"
    gap_pct = weight_pct - (target_weight * 100.0)
    if gap_pct >= 5:
        return "显著超配" if lang == "zh" else "Materially overweight"
    if gap_pct >= 2:
        return "轻度超配" if lang == "zh" else "Slightly overweight"
    if gap_pct <= -5:
        return "显著低配" if lang == "zh" else "Materially underweight"
    if gap_pct <= -2:
        return "轻度低配" if lang == "zh" else "Slightly underweight"
    return "接近目标" if lang == "zh" else "Near target"


def _rebalance_action(*, weight_pct: float, target_weight: float | None, signal_label: str, lang: str) -> str:
    normalized_signal = str(signal_label or "").strip().lower()
    if target_weight is None:
        return "先补齐交易计划" if lang == "zh" else "Complete the trade plan first"
    gap_pct = weight_pct - (target_weight * 100.0)
    if gap_pct >= 5:
        return "优先减仓回到计划上限" if lang == "zh" else "Trim first to get back to plan"
    if gap_pct >= 2 and normalized_signal in {"卖点", "sell"}:
        return "趁反弹减仓，避免超配承压" if lang == "zh" else "Use strength to trim the overweight"
    if gap_pct <= -5 and normalized_signal in {"买点", "buy"}:
        return "若触发条件成立，可分批补到目标仓位" if lang == "zh" else "Add in tranches if the trigger confirms"
    if gap_pct <= -2 and normalized_signal in {"买点", "buy"}:
        return "可小幅回补，先看流动性与开盘成交" if lang == "zh" else "Consider a small add after checking liquidity"
    return "维持当前仓位，等待更清晰信号" if lang == "zh" else "Keep size steady until signals improve"


def _execution_risk_summary(
    *,
    tradability_status: str | None,
    liquidity_bucket: str | None,
    max_slippage_bps: int | None,
    risk_flags: list[str],
    lang: str,
) -> str:
    parts: list[str] = []
    if tradability_status:
        parts.append(f"状态 {tradability_status}" if lang == "zh" else f"Status {tradability_status}")
    if liquidity_bucket:
        parts.append(f"流动性 {liquidity_bucket} 桶" if lang == "zh" else f"Liquidity {liquidity_bucket}")
    if max_slippage_bps is not None:
        parts.append(f"滑点上限 {max_slippage_bps}bps" if lang == "zh" else f"Slip {max_slippage_bps}bps")
    if risk_flags:
        parts.append(("风险 " if lang == "zh" else "Risk ") + "/".join(risk_flags[:2]))
    return " · ".join(parts) or ("正常执行" if lang == "zh" else "Normal execution")


def _recent_hit_rate_guard(db: Session, *, lang: str) -> dict:
    try:
        regression = load_or_build_recommendation_regression(db=db)
    except Exception:
        regression = {}
    summary = (regression or {}).get("summary") or {}
    recent = summary.get("recent_all") or {}
    recent_actionable = summary.get("recent_actionable") or {}
    recent_watch = summary.get("recent_watch") or {}

    def _float(value, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    recent_hit = _float(recent.get("execution_hit_rate"))
    recent_close = _float(recent.get("close_hit_rate"))
    recent_drawdown = _float(recent.get("deep_drawdown_rate"))
    watch_hit = _float(recent_watch.get("execution_hit_rate"))
    action_hit = _float(recent_actionable.get("execution_hit_rate"))
    active = int(recent.get("count") or 0) >= 20 and (
        recent_hit < 45.0 or recent_close < 45.0 or recent_drawdown >= 35.0
    )
    if lang == "zh":
        message = (
            f"最近推荐执行命中 {recent_hit:.1f}%、深回撤 {recent_drawdown:.1f}%，"
            "组合先防守：不新增风险，优先保护浮盈和处理亏损。"
            if active
            else f"最近推荐执行命中 {recent_hit:.1f}%，组合可按原计划复核执行。"
        )
    else:
        message = (
            f"Recent execution hit rate is {recent_hit:.1f}% with {recent_drawdown:.1f}% deep drawdown; "
            "stay defensive, avoid adding risk, protect gains and clean up losers."
            if active
            else f"Recent execution hit rate is {recent_hit:.1f}%; keep following the plan."
        )
    return {
        "active": active,
        "message": message,
        "recent_execution_hit_rate": recent_hit,
        "recent_close_hit_rate": recent_close,
        "recent_deep_drawdown_rate": recent_drawdown,
        "recent_actionable_hit_rate": action_hit,
        "recent_watch_hit_rate": watch_hit,
    }


def build_portfolio_intelligence(db: Session, *, lang: str = "zh") -> dict:
    positions = load_portfolio_positions()
    tickers = [item["ticker"] for item in positions]
    cache_key = f"{lang}|{[(item.get('ticker'), item.get('quantity'), item.get('cost_basis')) for item in positions]}"

    def _load() -> dict:
        symbol_repo = SymbolRepository(db)
        prediction_repo = PredictionRepository(db)
        hit_rate_guard = _recent_hit_rate_guard(db, lang=lang)
        overviews = symbol_repo.list_overviews_for_tickers(tickers)
        latest_outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
        latest_prices = load_latest_closes(tickers)
        sector_totals: dict[str, float] = {}
        market_totals: dict[str, float] = {}
        row_actions: list[dict] = []
        total_market_value = 0.0

        for item in positions:
            overview = overviews.get(item["ticker"]) or {
                "ticker": item["ticker"],
                "name": item.get("name"),
                "market": item.get("market"),
                "sector": None,
            }
            latest_signal = latest_outputs.get(item["ticker"])
            latest_signal = prediction_repo._build_signal_decision(latest_signal or {}) if latest_signal else None
            latest_price_raw = latest_prices.get(item["ticker"])
            latest_price_missing = latest_price_raw is None or float(latest_price_raw or 0.0) <= 0.0
            latest_price = 0.0 if latest_price_missing else float(latest_price_raw or 0.0)
            risk_flags = list((latest_signal or {}).get("risk_flags") or [])
            if not latest_price_missing:
                risk_flags = [flag for flag in risk_flags if str(flag) != "missing-latest-price"]
            quantity = float(item.get("quantity") or 0.0)
            cost_basis = float(item.get("cost_basis") or 0.0)
            market_value = latest_price * quantity
            pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis and not latest_price_missing else 0.0
            total_market_value += market_value

            sector = str(overview.get("sector") or (overview.get("industry") or ("未分类" if lang == "zh" else "Unclassified")))
            market = str(overview.get("market") or item.get("market") or "-")
            sector_totals[sector] = sector_totals.get(sector, 0.0) + market_value
            market_totals[market] = market_totals.get(market, 0.0) + market_value

            score = (latest_signal or {}).get("score")
            row_actions.append(
                {
                    "ticker": item["ticker"],
                    "name": overview.get("name") or item["ticker"],
                    "sector": sector,
                    "market": market,
                    "market_value": market_value,
                    "pnl_pct": pnl_pct,
                    "signal_label": build_signal_label(score, lang=lang) or ("持有" if lang == "zh" else "Hold"),
                    "action_hint": _action_from_score(score, lang=lang),
                    "tradability_status": (latest_signal or {}).get("tradability_status"),
                    "target_weight": (latest_signal or {}).get("target_weight"),
                    "entry_trigger": (latest_signal or {}).get("entry_trigger"),
                    "invalidation_condition": (latest_signal or {}).get("invalidation_condition"),
                    "time_horizon": (latest_signal or {}).get("time_horizon"),
                    "max_slippage_bps": (latest_signal or {}).get("max_slippage_bps"),
                    "liquidity_bucket": (latest_signal or {}).get("liquidity_bucket"),
                    "stop_loss_type": (latest_signal or {}).get("stop_loss_type"),
                    "execution_note": (latest_signal or {}).get("execution_note"),
                    "risk_flags": risk_flags,
                    "latest_price_missing": latest_price_missing,
                }
            )

        top_sector = max(sector_totals.items(), key=lambda item: item[1], default=((("未分类" if lang == "zh" else "Unclassified")), 0.0))
        top_market = max(market_totals.items(), key=lambda item: item[1], default=(("-", 0.0)))
        concentration_pct = round((top_sector[1] / total_market_value) * 100, 1) if total_market_value else 0.0
        market_rankings = [
            {
                "market": market,
                "market_value": value,
                "weight_pct": round((value / total_market_value) * 100.0, 1) if total_market_value else 0.0,
            }
            for market, value in sorted(market_totals.items(), key=lambda item: (-item[1], item[0]))
        ]
        sector_rankings = [
            {
                "sector": sector,
                "market_value": value,
                "weight_pct": round((value / total_market_value) * 100.0, 1) if total_market_value else 0.0,
            }
            for sector, value in sorted(sector_totals.items(), key=lambda item: (-item[1], item[0]))
        ]
        for row in row_actions:
            weight_pct = (float(row["market_value"] or 0.0) / total_market_value * 100.0) if total_market_value else 0.0
            row["weight_pct"] = round(weight_pct, 1)
            if row.get("latest_price_missing"):
                row["risk_tag"] = "缺行情" if lang == "zh" else "Missing price"
            else:
                row["risk_tag"] = _risk_tag(
                    pnl_pct=float(row["pnl_pct"] or 0.0),
                    market_value=float(row["market_value"] or 0.0),
                    total_market_value=total_market_value,
                    lang=lang,
                )
            row["action_priority"] = ("高" if lang == "zh" else "High") if row.get("latest_price_missing") else _action_priority(
                pnl_pct=float(row["pnl_pct"] or 0.0),
                weight_pct=weight_pct,
                signal_label=str(row["signal_label"] or ""),
                lang=lang,
            )
            row["action_reason"] = (
                "缺少最新行情，先不要按盈亏做决策；需要补行情或确认该标的是否仍可交易。"
                if lang == "zh" and row.get("latest_price_missing")
                else "Latest price is missing; do not make a PnL-based decision until data is repaired."
                if row.get("latest_price_missing")
                else _action_reason(
                    pnl_pct=float(row["pnl_pct"] or 0.0),
                    weight_pct=weight_pct,
                    signal_label=str(row["signal_label"] or ""),
                    lang=lang,
                )
            )
            target_weight = row.get("target_weight")
            target_weight_value = float(target_weight) if target_weight is not None else None
            row["target_weight_pct"] = round((target_weight_value or 0.0) * 100.0, 1) if target_weight_value is not None else None
            row["rebalance_gap_pct"] = (
                round(weight_pct - (target_weight_value * 100.0), 1) if target_weight_value is not None else None
            )
            row["rebalance_gap_label"] = _rebalance_gap_label(
                weight_pct=weight_pct,
                target_weight=target_weight_value,
                lang=lang,
            )
            row["rebalance_action"] = (
                "先修复行情源，再决定是否卖出或调仓。"
                if lang == "zh" and row.get("latest_price_missing")
                else "Repair the price feed before deciding on sell/rebalance."
                if row.get("latest_price_missing")
                else _rebalance_action(
                    weight_pct=weight_pct,
                    target_weight=target_weight_value,
                    signal_label=str(row["signal_label"] or ""),
                    lang=lang,
                )
            )
            row["execution_risk_summary"] = _execution_risk_summary(
                tradability_status=row.get("tradability_status"),
                liquidity_bucket=row.get("liquidity_bucket"),
                max_slippage_bps=row.get("max_slippage_bps"),
                risk_flags=list(row.get("risk_flags") or []),
                lang=lang,
            )
        priority_rank = {"高": 0, "High": 0, "中": 1, "Medium": 1, "低": 2, "Low": 2}
        row_actions.sort(
            key=lambda item: (
                priority_rank.get(str(item.get("action_priority") or ""), 3),
                -abs(item["market_value"]),
                item["ticker"],
            )
        )
        action_mix = {"high": 0, "medium": 0, "low": 0}
        for row in row_actions:
            value = str(row.get("action_priority") or "").lower()
            if value in {"高", "high"}:
                action_mix["high"] += 1
            elif value in {"中", "medium"}:
                action_mix["medium"] += 1
            else:
                action_mix["low"] += 1
        top_position = max(row_actions, key=lambda item: float(item.get("market_value") or 0.0), default=None)
        drawdown_count = sum(1 for row in row_actions if float(row.get("pnl_pct") or 0.0) <= -8.0)
        profit_protection_count = sum(1 for row in row_actions if float(row.get("pnl_pct") or 0.0) >= 15.0)
        trim_candidates = sum(
            1
            for row in row_actions
            if float(row.get("weight_pct") or 0.0) >= 15.0 or float(row.get("pnl_pct") or 0.0) >= 30.0
        )
        exit_candidates = sum(
            1
            for row in row_actions
            if float(row.get("pnl_pct") or 0.0) <= -8.0
            or str(row.get("signal_label") or "").strip().lower() in {"卖点", "sell"}
        )
        review_candidates = sum(
            1
            for row in row_actions
            if str(row.get("action_priority") or "").lower() in {"高", "high"}
        )
        missing_price_count = sum(1 for row in row_actions if row.get("latest_price_missing"))
        if hit_rate_guard.get("active") or drawdown_count >= 2 or concentration_pct >= 45.0 or (top_position and float(top_position.get("weight_pct") or 0.0) >= 25.0):
            risk_posture = "防守" if lang == "zh" else "Defensive"
        elif profit_protection_count >= 2 or trim_candidates >= 2:
            risk_posture = "均衡偏防守" if lang == "zh" else "Balanced / defensive"
        else:
            risk_posture = "均衡" if lang == "zh" else "Balanced"
        if lang == "zh":
            risk_summary = f"最大行业暴露 {top_sector[0]}，约占组合 {concentration_pct}%"
            posture_summary = (
                f"当前更偏{risk_posture}，优先处理 {review_candidates + missing_price_count} 只高优先级/缺行情仓位。"
                if row_actions
                else "当前没有持仓。"
            )
            if hit_rate_guard.get("active"):
                posture_summary = f"{posture_summary} {hit_rate_guard.get('message')}"
        else:
            risk_summary = f"Largest sector exposure is {top_sector[0]}, about {concentration_pct}% of the portfolio"
            posture_summary = (
                f"Current posture is {risk_posture}; {review_candidates + missing_price_count} high-priority/missing-price positions deserve review."
                if row_actions
                else "There are no portfolio positions."
            )
            if hit_rate_guard.get("active"):
                posture_summary = f"{posture_summary} {hit_rate_guard.get('message')}"

        return {
            "total_market_value": round(total_market_value, 2),
            "total_positions": len(positions),
            "top_sector": top_sector[0],
            "top_market": top_market[0],
            "concentration_pct": concentration_pct,
            "market_rankings": market_rankings[:5],
            "sector_rankings": sector_rankings[:5],
            "all_items": row_actions,
            "watch_items": row_actions[:5],
            "action_mix": action_mix,
            "top_position": {
                "ticker": top_position.get("ticker"),
                "name": top_position.get("name"),
                "weight_pct": round(float(top_position.get("weight_pct") or 0.0), 1),
                "market_value": round(float(top_position.get("market_value") or 0.0), 2),
            } if top_position else None,
            "rebalance_alerts": sum(
                1
                for row in row_actions
                if abs(float(row.get("rebalance_gap_pct") or 0.0)) >= 5.0
            ),
            "drawdown_count": drawdown_count,
            "profit_protection_count": profit_protection_count,
            "trim_candidates": trim_candidates,
            "exit_candidates": exit_candidates,
            "review_candidates": review_candidates,
            "missing_price_count": missing_price_count,
            "hit_rate_guard": hit_rate_guard,
            "risk_posture": risk_posture,
            "risk_summary": risk_summary,
            "posture_summary": posture_summary,
        }

    return get_or_set("portfolio_intelligence", cache_key, ttl_seconds=30.0, loader=_load)
