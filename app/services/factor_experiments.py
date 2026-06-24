from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha1
from statistics import mean
from typing import Any

from sqlalchemy.orm import Session

from app.services.market_lake import get_latest_lake_trade_date, load_lake_price_history, load_lake_rows
from app.services.repository import AppSettingRepository, WorkspaceSnapshotRepository
from app.services.screener_snapshots import build_base_precompute_params
from app.services.time_utils import app_now_iso, app_today_iso


FACTOR_STRATEGIES_KEY = "factor_experiment_strategies_v1"
FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE = "factor_experiment_run"
DEFAULT_FACTOR_STRATEGY_VERSION = 2


def _factor(
    key: str,
    *,
    label_zh: str,
    label_en: str,
    category: str,
    source: str,
    direction: str,
    description_zh: str,
    usage_zh: str,
    risk_zh: str,
    market_fit_zh: str,
    default_weight: float = 0.0,
) -> dict[str, Any]:
    return {
        "key": key,
        "label_zh": label_zh,
        "label_en": label_en,
        "category": category,
        "source": source,
        "direction": direction,
        "description_zh": description_zh,
        "usage_zh": usage_zh,
        "risk_zh": risk_zh,
        "market_fit_zh": market_fit_zh,
        "default_weight": float(default_weight or 0.0),
    }


FACTOR_DEFINITIONS: list[dict[str, Any]] = [
    _factor(
        "trend_score",
        label_zh="趋势强度",
        label_en="Trend score",
        category="technical",
        source="screener",
        direction="higher_better",
        description_zh="衡量价格趋势、突破位置和近期动量的综合分。",
        usage_zh="用于优先保留已经走强、但还没有明显失控追高的股票。",
        risk_zh="单看趋势容易买到阶段末端，必须叠加涨幅、缺口和回撤约束。",
        market_fit_zh="适合强势市场、题材扩散期和趋势延续行情。",
        default_weight=18,
    ),
    _factor(
        "volume_ratio",
        label_zh="量能放大",
        label_en="Volume ratio",
        category="technical",
        source="screener",
        direction="higher_better",
        description_zh="最近成交量相对历史均量的放大程度。",
        usage_zh="用来确认资金关注度，过滤无量反弹。",
        risk_zh="极端放量可能是出货或消息兑现，需要结合收盘位置和次日承接。",
        market_fit_zh="适合突破、利润断层、题材启动和机构回补场景。",
        default_weight=12,
    ),
    _factor(
        "momentum_5",
        label_zh="5日动量",
        label_en="5D momentum",
        category="technical",
        source="screener",
        direction="higher_better",
        description_zh="近 5 个交易日价格变化，用于识别短线资金推进。",
        usage_zh="辅助判断是否处在短线主升或二次启动阶段。",
        risk_zh="过高时容易高开低走，需要配合 do not chase 规则。",
        market_fit_zh="适合短线强势行情，不适合弱势震荡中盲目追涨。",
        default_weight=8,
    ),
    _factor(
        "ma_bullish",
        label_zh="均线多头",
        label_en="Bullish MA stack",
        category="technical",
        source="technical_snapshot",
        direction="true_better",
        description_zh="短中期均线呈多头排列或被形态模板命中。",
        usage_zh="作为趋势结构过滤，减少左侧抄底。",
        risk_zh="均线确认较慢，可能错过最早启动点。",
        market_fit_zh="适合趋势延续、强趋势二次启动和利润断层后的确认。",
        default_weight=10,
    ),
    _factor(
        "net_profit_yoy",
        label_zh="利润同比",
        label_en="Profit YoY",
        category="fundamental",
        source="fundamental_snapshot",
        direction="higher_better",
        description_zh="净利润同比增长，衡量业绩弹性。",
        usage_zh="用于确认利润断层、成长质量和基本面改善。",
        risk_zh="单季高增可能来自低基数或一次性收益，要结合环比和收入质量。",
        market_fit_zh="适合业绩线、机构回补、利润断层和中线趋势策略。",
        default_weight=14,
    ),
    _factor(
        "revenue_yoy",
        label_zh="收入同比",
        label_en="Revenue YoY",
        category="fundamental",
        source="fundamental_snapshot",
        direction="higher_better",
        description_zh="营业收入同比增长，用来验证利润增长质量。",
        usage_zh="用于区分真实需求改善和费用/非经常性收益驱动。",
        risk_zh="收入增长不等于盈利改善，需结合利润率和现金流。",
        market_fit_zh="适合成长股、周期复苏和产业趋势行情。",
        default_weight=8,
    ),
    _factor(
        "roe_avg_3y",
        label_zh="三年 ROE",
        label_en="3Y ROE",
        category="fundamental",
        source="fundamental_snapshot",
        direction="higher_better",
        description_zh="三年平均净资产收益率，衡量长期质量。",
        usage_zh="用于提高候选池质量，减少纯情绪小票。",
        risk_zh="高 ROE 也可能估值过高，短线弹性未必最强。",
        market_fit_zh="适合质量成长、机构趋势和中低频选股。",
        default_weight=8,
    ),
    _factor(
        "risk_flag_count",
        label_zh="风险标签数量",
        label_en="Risk flag count",
        category="risk",
        source="tradability_filter",
        direction="lower_better",
        description_zh="风险标签数量，例如追高、事件风险、流动性不足等。",
        usage_zh="用于降低买入后被动止损概率。",
        risk_zh="过滤太严可能错过强势龙头，需按市场温度调节。",
        market_fit_zh="弱势市场应提高权重，强势题材日可适当放宽。",
        default_weight=14,
    ),
    _factor(
        "do_not_chase",
        label_zh="不可追高",
        label_en="Do not chase",
        category="risk",
        source="tradability_filter",
        direction="false_better",
        description_zh="系统判断当前价格或结构不适合追买。",
        usage_zh="作为硬风控或强扣分项，避免情绪化追涨。",
        risk_zh="可能错过超强龙头的连续加速段。",
        market_fit_zh="适合大部分业余交易员，尤其在弱势和震荡市。",
        default_weight=10,
    ),
    _factor(
        "trade_readiness_score",
        label_zh="交易就绪度",
        label_en="Trade readiness",
        category="risk",
        source="tradability_filter",
        direction="higher_better",
        description_zh="把信号强度、可交易性和风险约束合成的行动分。",
        usage_zh="用于排序最终可行动候选，避免只看模型分。",
        risk_zh="依赖底层标签质量，若标签缺失会偏保守。",
        market_fit_zh="适合日常盘后筛选的最后一道总分。",
        default_weight=18,
    ),
    _factor(
        "model_score",
        label_zh="LightGBM/模型分",
        label_en="Model score",
        category="model",
        source="model_prediction",
        direction="higher_better",
        description_zh="模型对未来收益或相对强度的评分。",
        usage_zh="作为量化 alpha 输入，但不单独决定买入。",
        risk_zh="模型可能过拟合或在新行情 regime 下失效。",
        market_fit_zh="适合和技术形态、风险过滤、多模型共振组合使用。",
        default_weight=16,
    ),
    _factor(
        "model_hit_count",
        label_zh="多模型命中数",
        label_en="Multi-model hits",
        category="model",
        source="screener_snapshot",
        direction="higher_better",
        description_zh="同一股票被多个模板或模型同时选中的数量。",
        usage_zh="用于寻找回踩、突破、质量、动量之间的共振。",
        risk_zh="多个相似模型命中不代表真正独立信号。",
        market_fit_zh="适合提高候选置信度，减少单模型噪音。",
        default_weight=14,
    ),
    _factor(
        "kronos_support",
        label_zh="Kronos 支持",
        label_en="Kronos support",
        category="model",
        source="kronos_validation",
        direction="true_better",
        description_zh="Kronos 时序验证是否支持当前候选。",
        usage_zh="作为二次确认，不替代本地量化模型。",
        risk_zh="当外部 runner 未启用或样本不足时会缺失。",
        market_fit_zh="适合对 Top 候选做验证，不适合扩大候选池。",
        default_weight=8,
    ),
    _factor(
        "gap_percent",
        label_zh="利润断层缺口",
        label_en="Profit gap percent",
        category="profit_gap",
        source="daily_ohlcv",
        direction="moderate_better",
        description_zh="业绩或重大催化后出现的向上跳空幅度。",
        usage_zh="识别利润断层起点，优先看 3%-8% 且有承接的缺口。",
        risk_zh="缺口过大容易买不到或次日兑现，缺口过小可能不是有效断层。",
        market_fit_zh="适合业绩季、政策催化和机构重新定价阶段。",
        default_weight=14,
    ),
    _factor(
        "post_gap_volume_ratio",
        label_zh="断层后量能",
        label_en="Post-gap volume",
        category="profit_gap",
        source="daily_ohlcv",
        direction="higher_better",
        description_zh="断层后成交量是否持续高于均量。",
        usage_zh="验证缺口不是一日游，而是资金继续参与。",
        risk_zh="连续爆量但价格不涨可能是分歧或出货。",
        market_fit_zh="适合利润断层确认和二次买点过滤。",
        default_weight=12,
    ),
    _factor(
        "gap_fill_days",
        label_zh="缺口回补天数",
        label_en="Gap fill days",
        category="profit_gap",
        source="daily_ohlcv",
        direction="lower_better",
        description_zh="缺口形成后被完全回补所需天数，未回补更强。",
        usage_zh="用于判断断层强度和承接质量。",
        risk_zh="短期不回补也可能只是高位横盘，仍需看趋势和量能。",
        market_fit_zh="适合利润断层后 3-20 日的观察和回踩买点。",
        default_weight=8,
    ),
]


DEFAULT_FACTOR_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "profit_gap_quality_v1",
        "version": DEFAULT_FACTOR_STRATEGY_VERSION,
        "name": "利润断层质量模板",
        "description": "寻找业绩改善、跳空重估、趋势不破、量能承接的 A 股利润断层候选。",
        "market": "CN",
        "source_params": build_base_precompute_params(
            model_template="technical_momentum",
            universe="full_market",
            market="CN",
        ),
        "filters": [
            {"factor_key": "trend_score", "op": "gte", "value": 50, "required": True},
            {"factor_key": "trade_readiness_score", "op": "gte", "value": 45, "required": True},
            {"factor_key": "do_not_chase", "op": "eq", "value": False, "required": True},
            {"factor_key": "net_profit_yoy", "op": "gte", "value": 15, "required": False},
            {"factor_key": "gap_percent", "op": "between", "min": 2.0, "max": 10.0, "required": False},
            {"factor_key": "post_gap_volume_ratio", "op": "gte", "value": 1.2, "required": False},
            {"factor_key": "ma_bullish", "op": "eq", "value": True, "required": False},
        ],
        "weights": {
            "trade_readiness_score": 22,
            "trend_score": 16,
            "model_score": 14,
            "model_hit_count": 10,
            "net_profit_yoy": 12,
            "revenue_yoy": 8,
            "volume_ratio": 10,
            "gap_percent": 10,
            "post_gap_volume_ratio": 8,
            "ma_bullish": 8,
            "risk_flag_count": 12,
            "do_not_chase": 12,
            "kronos_support": 6,
        },
        "created_at": app_now_iso(),
        "updated_at": app_now_iso(),
    },
    {
        "id": "trend_momentum_confluence_v1",
        "version": DEFAULT_FACTOR_STRATEGY_VERSION,
        "name": "强趋势 + 技术动量共振",
        "description": "偏短线的强趋势候选模板，适合盘后从模型命中股票里再压缩候选池。",
        "market": "CN",
        "source_params": build_base_precompute_params(
            model_template="technical_momentum",
            universe="full_market",
            market="CN",
        ),
        "filters": [
            {"factor_key": "trend_score", "op": "gte", "value": 62, "required": True},
            {"factor_key": "volume_ratio", "op": "gte", "value": 1.1, "required": False},
            {"factor_key": "risk_flag_count", "op": "lte", "value": 2, "required": True},
            {"factor_key": "trade_readiness_score", "op": "gte", "value": 58, "required": True},
        ],
        "weights": {
            "trade_readiness_score": 24,
            "trend_score": 20,
            "momentum_5": 12,
            "volume_ratio": 12,
            "model_hit_count": 10,
            "risk_flag_count": 14,
            "do_not_chase": 10,
        },
        "created_at": app_now_iso(),
        "updated_at": app_now_iso(),
    },
]


def list_factor_definitions() -> list[dict[str, Any]]:
    return deepcopy(FACTOR_DEFINITIONS)


def factor_definition_map() -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in FACTOR_DEFINITIONS}


def _load_strategy_payload(db: Session) -> dict[str, Any]:
    raw = AppSettingRepository(db).get(FACTOR_STRATEGIES_KEY)
    if not raw:
        return {"strategies": deepcopy(DEFAULT_FACTOR_STRATEGIES)}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"strategies": deepcopy(DEFAULT_FACTOR_STRATEGIES)}
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return {"strategies": deepcopy(DEFAULT_FACTOR_STRATEGIES)}
    default_by_id = {str(item.get("id") or ""): item for item in DEFAULT_FACTOR_STRATEGIES}
    merged: list[dict[str, Any]] = []
    existing_ids: set[str] = set()
    for item in strategies:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        existing_ids.add(item_id)
        default_item = default_by_id.get(item_id)
        if default_item and _safe_int(item.get("version")) < DEFAULT_FACTOR_STRATEGY_VERSION:
            upgraded = deepcopy(default_item)
            upgraded["created_at"] = item.get("created_at") or upgraded.get("created_at")
            upgraded["updated_at"] = app_now_iso()
            merged.append(upgraded)
        else:
            merged.append(item)
    for default in DEFAULT_FACTOR_STRATEGIES:
        if default["id"] not in existing_ids:
            merged.append(deepcopy(default))
    return {"strategies": merged}


def list_factor_strategies(db: Session) -> list[dict[str, Any]]:
    return _load_strategy_payload(db)["strategies"]


def get_factor_strategy(db: Session, strategy_id: str | None) -> dict[str, Any] | None:
    normalized = str(strategy_id or "").strip()
    strategies = list_factor_strategies(db)
    if not normalized and strategies:
        return strategies[0]
    for strategy in strategies:
        if str(strategy.get("id") or "") == normalized:
            return strategy
    return None


def save_factor_strategy(db: Session, strategy: dict[str, Any]) -> dict[str, Any]:
    payload = _load_strategy_payload(db)
    strategies = payload["strategies"]
    normalized = deepcopy(strategy)
    normalized["id"] = str(normalized.get("id") or _strategy_id(normalized.get("name") or "strategy"))
    normalized.setdefault("created_at", app_now_iso())
    normalized["updated_at"] = app_now_iso()
    updated: list[dict[str, Any]] = []
    found = False
    for item in strategies:
        if str(item.get("id") or "") == normalized["id"]:
            updated.append(normalized)
            found = True
        else:
            updated.append(item)
    if not found:
        updated.append(normalized)
    AppSettingRepository(db).set(FACTOR_STRATEGIES_KEY, json.dumps({"strategies": updated}, ensure_ascii=False))
    return normalized


def _strategy_id(name: str) -> str:
    digest = sha1(f"{name}:{app_now_iso()}".encode("utf-8")).hexdigest()[:10]
    return f"factor_strategy_{digest}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "支持", "是"}:
        return True
    if text in {"0", "false", "no", "n", "否"}:
        return False
    return None


def evaluate_factor_value(row: dict[str, Any], factor_key: str) -> Any:
    key = str(factor_key or "").strip()
    if key == "risk_flag_count":
        flags = row.get("risk_flags") or row.get("model_execution_tags") or []
        if isinstance(flags, str):
            return 0 if flags.strip() in {"", "-"} else len([part for part in flags.split(",") if part.strip()])
        if isinstance(flags, list):
            return len([item for item in flags if str(item).strip()])
        return 0
    if key == "trade_readiness_score":
        direct = _first_float(row, ["trade_readiness_score", "readiness_score"])
        if direct is not None:
            return direct
        trend = _to_float(row.get("trend_score"))
        status = str(row.get("tradability_status") or "").strip().upper()
        if trend is None:
            return None
        penalty = 25.0 if status in {"BLOCKED", "DO_NOT_CHASE"} else 0.0
        return _clamp(trend - penalty, 0, 100)
    if key == "do_not_chase":
        status = str(row.get("tradability_status") or "").upper()
        bucket = str(row.get("readiness_bucket") or "").upper()
        reason = str(row.get("block_reason") or row.get("readiness_reason") or "").lower()
        return status == "DO_NOT_CHASE" or bucket == "DO_NOT_CHASE" or "chase" in reason or "追高" in reason
    if key == "ma_bullish":
        direct = _to_bool(row.get("ma_bullish"))
        if direct is not None:
            return direct
        patterns = row.get("matched_patterns") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        pattern_text = " ".join(str(item) for item in patterns)
        return any(token in pattern_text for token in ["均线多头", "bullish_ma", "ma_stack", "MA多头"])
    if key == "kronos_support":
        validation = row.get("kronos_validation") or {}
        if isinstance(validation, dict):
            verdict = str(validation.get("verdict") or validation.get("status") or "").lower()
            score = _to_float(validation.get("score") or validation.get("expected_return_pct"))
            return verdict in {"support", "supported", "bullish", "positive"} or (score is not None and score > 0)
        return False
    if key == "post_gap_volume_ratio":
        return _first_float(row, ["post_gap_volume_ratio", "gap_volume_ratio", "volume_ratio"])
    if key == "gap_percent":
        return _first_float(row, ["gap_percent", "gap_pct", "open_gap_pct", "gap_up_pct", "next_open_gap_pct"])
    if key == "gap_fill_days":
        return _first_float(row, ["gap_fill_days", "gap_recovery_days"])
    if key == "model_score":
        return _first_float(row, ["model_score", "score", "model_percentile"])
    return row.get(key)


def _first_float(row: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _condition_passed(value: Any, condition: dict[str, Any]) -> tuple[bool, str | None]:
    required = bool(condition.get("required", True))
    if value in (None, ""):
        return (not required), "missing"
    op = str(condition.get("op") or "gte").lower()
    if op in {"eq", "neq"}:
        expected = condition.get("value")
        if isinstance(expected, bool):
            actual = _to_bool(value)
        else:
            actual = value
        passed = actual == expected
        return (not passed if op == "neq" else passed), None
    numeric_value = _to_float(value)
    if numeric_value is None:
        return (not required), "non_numeric"
    if op == "gte":
        return numeric_value >= float(condition.get("value") or 0), None
    if op == "lte":
        return numeric_value <= float(condition.get("value") or 0), None
    if op == "gt":
        return numeric_value > float(condition.get("value") or 0), None
    if op == "lt":
        return numeric_value < float(condition.get("value") or 0), None
    if op == "between":
        return (
            numeric_value >= float(condition.get("min") or 0)
            and numeric_value <= float(condition.get("max") or 0)
        ), None
    return True, None


def _normalize_score(factor_key: str, value: Any) -> float | None:
    if value in (None, ""):
        return None
    if factor_key in {"do_not_chase"}:
        actual = _to_bool(value)
        return 0.0 if actual else 100.0
    if factor_key in {"ma_bullish", "kronos_support"}:
        actual = _to_bool(value)
        return 100.0 if actual else 0.0
    numeric = _to_float(value)
    if numeric is None:
        return None
    if factor_key in {"trend_score", "trade_readiness_score", "model_signal_strength", "model_percentile"}:
        return _clamp(numeric, 0, 100)
    if factor_key == "model_score":
        return _clamp(numeric * 100 if numeric <= 1.5 else numeric, 0, 100)
    if factor_key == "model_hit_count":
        return _clamp((numeric / 3.0) * 100.0, 0, 100)
    if factor_key == "risk_flag_count":
        return _clamp(100.0 - numeric * 25.0, 0, 100)
    if factor_key == "volume_ratio":
        return _clamp((numeric / 3.0) * 100.0, 0, 100)
    if factor_key == "momentum_5":
        return _clamp((numeric + 4.0) / 16.0 * 100.0, 0, 100)
    if factor_key in {"net_profit_yoy", "revenue_yoy"}:
        return _clamp((numeric + 10.0) / 90.0 * 100.0, 0, 100)
    if factor_key == "roe_avg_3y":
        return _clamp((numeric / 25.0) * 100.0, 0, 100)
    if factor_key == "gap_percent":
        if numeric <= 0:
            return 0.0
        if numeric <= 8:
            return _clamp((numeric / 8.0) * 100.0, 0, 100)
        return _clamp(100.0 - (numeric - 8.0) * 9.0, 35, 100)
    if factor_key == "post_gap_volume_ratio":
        return _clamp((numeric / 3.0) * 100.0, 0, 100)
    if factor_key == "gap_fill_days":
        return _clamp(100.0 - numeric * 12.0, 0, 100)
    return _clamp(numeric, 0, 100)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def apply_factor_strategy(rows: list[dict[str, Any]], strategy: dict[str, Any], *, limit: int = 80) -> dict[str, Any]:
    filters = [item for item in strategy.get("filters") or [] if isinstance(item, dict)]
    weights = {str(key): float(value or 0) for key, value in (strategy.get("weights") or {}).items()}
    evaluated_rows: list[dict[str, Any]] = []
    blocked_count = 0
    missing_counts: dict[str, int] = {}
    for row in rows:
        factor_values = {factor_key: evaluate_factor_value(row, factor_key) for factor_key in set(weights) | {str(f.get("factor_key")) for f in filters}}
        failed: list[str] = []
        for condition in filters:
            factor_key = str(condition.get("factor_key") or "").strip()
            passed, reason = _condition_passed(factor_values.get(factor_key), condition)
            if reason == "missing":
                missing_counts[factor_key] = missing_counts.get(factor_key, 0) + 1
            if not passed and bool(condition.get("required", True)):
                failed.append(factor_key)
        if failed:
            blocked_count += 1
            continue
        weighted_total = 0.0
        weight_total = 0.0
        factor_scores: dict[str, float | None] = {}
        for factor_key, weight in weights.items():
            if weight <= 0:
                continue
            score = _normalize_score(factor_key, factor_values.get(factor_key))
            factor_scores[factor_key] = None if score is None else round(score, 2)
            if score is None:
                continue
            weighted_total += score * weight
            weight_total += weight
        factor_score = weighted_total / weight_total if weight_total > 0 else 0.0
        enriched = dict(row)
        enriched["factor_values"] = _json_safe(factor_values)
        enriched["factor_scores"] = factor_scores
        enriched["factor_score"] = round(factor_score, 2)
        evaluated_rows.append(enriched)
    evaluated_rows.sort(
        key=lambda item: (
            float(item.get("factor_score") or 0.0),
            float(item.get("trade_readiness_score") or 0.0),
            float(item.get("trend_score") or 0.0),
        ),
        reverse=True,
    )
    return {
        "rows": evaluated_rows[: max(1, int(limit))],
        "matched_count": len(evaluated_rows),
        "blocked_count": blocked_count,
        "missing_counts": missing_counts,
    }


def run_factor_experiment(
    db: Session,
    *,
    strategy_id: str,
    source_rows: list[dict[str, Any]],
    source_params: dict[str, Any] | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    strategy = get_factor_strategy(db, strategy_id)
    if strategy is None:
        raise ValueError("Factor strategy not found.")
    applied = apply_factor_strategy(source_rows, strategy, limit=limit)
    rows = applied["rows"]
    attach_forward_outcomes(rows)
    metrics = summarize_factor_outcomes(rows)
    payload = {
        "strategy": strategy,
        "source_params": source_params or strategy.get("source_params") or {},
        "source_count": len(source_rows),
        "matched_count": applied["matched_count"],
        "blocked_count": applied["blocked_count"],
        "missing_counts": applied["missing_counts"],
        "metrics": metrics,
        "rows": rows,
        "created_at": app_now_iso(),
    }
    snapshot = WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE,
        snapshot_date=app_today_iso(),
        payload=payload,
    )
    return {"snapshot": snapshot, "payload": payload}


def list_factor_experiment_runs(db: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    return WorkspaceSnapshotRepository(db).list_snapshots(FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE, limit=limit)


def get_factor_experiment_run(db: Session, snapshot_id: int) -> dict[str, Any] | None:
    return WorkspaceSnapshotRepository(db).get_snapshot(
        int(snapshot_id),
        snapshot_type=FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE,
    )


def refresh_factor_experiment_run(db: Session, snapshot_id: int) -> dict[str, Any]:
    snapshot = get_factor_experiment_run(db, snapshot_id)
    if snapshot is None:
        raise ValueError("Factor experiment run not found.")
    payload = deepcopy(snapshot.get("payload") or {})
    rows = [dict(row) for row in payload.get("rows") or [] if isinstance(row, dict)]
    for row in rows:
        existing_outcome = row.get("forward_outcome") if isinstance(row.get("forward_outcome"), dict) else {}
        signal_trade_date = (
            str(row.get("factor_signal_trade_date") or "").strip()
            or str(existing_outcome.get("trade_date") or "").strip()
            or str(row.get("trade_date") or row.get("snapshot_date") or "").strip()
        )
        if signal_trade_date:
            row["factor_signal_trade_date"] = signal_trade_date[:10]
    attach_forward_outcomes(rows)
    payload["rows"] = rows
    payload["metrics"] = summarize_factor_outcomes(rows)
    payload["refreshed_at"] = app_now_iso()
    payload["refreshed_from_snapshot_id"] = int(snapshot_id)
    refreshed_snapshot = WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=FACTOR_EXPERIMENT_RUN_SNAPSHOT_TYPE,
        snapshot_date=app_today_iso(),
        payload=payload,
    )
    return {"snapshot": refreshed_snapshot, "payload": payload}


def compute_forward_outcome(row: dict[str, Any], *, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    market = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SZ", ".SS", ".SH")) else "US")
    if market not in {"CN", "US"} or not ticker:
        return {"status": "missing_symbol"}
    previous_outcome = row.get("forward_outcome") if isinstance(row.get("forward_outcome"), dict) else {}
    latest_trade_date = (
        str(row.get("factor_signal_trade_date") or "").strip()
        or str(previous_outcome.get("trade_date") or "").strip()
        or str(row.get("trade_date") or row.get("snapshot_date") or "").strip()
        or get_latest_lake_trade_date(market=market, ticker=ticker)
        or get_latest_lake_trade_date(market=market)
    )
    history = history if history is not None else load_lake_price_history(market=market, ticker=ticker, limit=80)
    if not history or not latest_trade_date:
        return {"status": "missing_history", "trade_date": latest_trade_date}
    index = _history_index_for_date(history, latest_trade_date)
    if index is None:
        return {"status": "trade_date_not_found", "trade_date": latest_trade_date}
    signal_bar = history[index]
    signal_close = _to_float(signal_bar.get("close"))
    if signal_close is None or signal_close <= 0:
        return {"status": "bad_signal_close", "trade_date": latest_trade_date}
    outcome: dict[str, Any] = {"status": "ok", "trade_date": signal_bar.get("date"), "signal_close": signal_close}
    next_bar = history[index + 1] if index + 1 < len(history) else None
    if next_bar:
        next_open = _to_float(next_bar.get("open"))
        next_high = _to_float(next_bar.get("high"))
        next_low = _to_float(next_bar.get("low"))
        next_close = _to_float(next_bar.get("close"))
        outcome["next_trade_date"] = next_bar.get("date")
        if next_open and next_open > 0:
            outcome["next_open_gap_pct"] = round((next_open / signal_close - 1.0) * 100.0, 2)
            if next_high is not None:
                outcome["next_open_to_high_pct"] = round((next_high / next_open - 1.0) * 100.0, 2)
            if next_low is not None:
                outcome["next_open_to_low_pct"] = round((next_low / next_open - 1.0) * 100.0, 2)
        if next_close is not None:
            outcome["return_1d_pct"] = round((next_close / signal_close - 1.0) * 100.0, 2)
        outcome["gap_unbuyable"] = bool((outcome.get("next_open_gap_pct") or 0) >= (8.0 if market == "CN" else 6.0))
    for horizon in (3, 5):
        target_index = index + horizon
        if target_index < len(history):
            target_close = _to_float(history[target_index].get("close"))
            if target_close is not None:
                outcome[f"return_{horizon}d_pct"] = round((target_close / signal_close - 1.0) * 100.0, 2)
        lows: list[float] = []
        for bar in history[index + 1 : min(len(history), index + horizon + 1)]:
            low_value = _to_float(bar.get("low"))
            if low_value is not None:
                lows.append(low_value)
        if lows:
            outcome[f"max_drawdown_{horizon}d_pct"] = round((min(lows) / signal_close - 1.0) * 100.0, 2)
    return outcome


def attach_forward_outcomes(rows: list[dict[str, Any]], *, history_limit: int = 90) -> None:
    histories = _load_factor_histories(rows, history_limit=history_limit)
    for row in rows:
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        market = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SZ", ".SS", ".SH")) else "US")
        history = histories.get((market, ticker))
        row["forward_outcome"] = compute_forward_outcome(row, history=history)


def _load_factor_histories(rows: list[dict[str, Any]], *, history_limit: int) -> dict[tuple[str, str], list[dict[str, Any]]]:
    tickers_by_market: dict[str, set[str]] = {"CN": set(), "US": set()}
    for row in rows:
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        market = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SZ", ".SS", ".SH")) else "US")
        if market in tickers_by_market and ticker:
            tickers_by_market[market].add(ticker)
    histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market, tickers in tickers_by_market.items():
        if not tickers:
            continue
        lake_rows = load_lake_rows(markets=[market], tickers=tickers, limit_per_symbol=max(10, int(history_limit)))
        for item in lake_rows:
            ticker = str(item.get("symbol") or "").strip().upper()
            if ticker:
                histories.setdefault((market, ticker), []).append(item)
    for key, history in histories.items():
        history.sort(key=lambda item: str(item.get("date") or ""))
    return histories


def _history_index_for_date(history: list[dict[str, Any]], trade_date: str) -> int | None:
    normalized = str(trade_date or "")[:10]
    exact = [index for index, bar in enumerate(history) if str(bar.get("date") or "")[:10] == normalized]
    if exact:
        return exact[0]
    candidates = [index for index, bar in enumerate(history) if str(bar.get("date") or "")[:10] <= normalized]
    return candidates[-1] if candidates else None


def summarize_factor_outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {"evaluated_count": len(rows)}
    for horizon in (1, 3, 5):
        key = f"return_{horizon}d_pct"
        values = [
            _to_float((row.get("forward_outcome") or {}).get(key))
            for row in rows
            if _to_float((row.get("forward_outcome") or {}).get(key)) is not None
        ]
        clean = [value for value in values if value is not None]
        metrics[f"available_{horizon}d"] = len(clean)
        metrics[f"avg_return_{horizon}d_pct"] = round(mean(clean), 2) if clean else None
        metrics[f"hit_rate_{horizon}d_pct"] = round(len([value for value in clean if value > 0]) / len(clean) * 100.0, 1) if clean else None
    drawdowns = [
        _to_float((row.get("forward_outcome") or {}).get("max_drawdown_5d_pct"))
        for row in rows
        if _to_float((row.get("forward_outcome") or {}).get("max_drawdown_5d_pct")) is not None
    ]
    metrics["avg_max_drawdown_5d_pct"] = round(mean(drawdowns), 2) if drawdowns else None
    next_open_gaps = [
        _to_float((row.get("forward_outcome") or {}).get("next_open_gap_pct"))
        for row in rows
        if _to_float((row.get("forward_outcome") or {}).get("next_open_gap_pct")) is not None
    ]
    metrics["avg_next_open_gap_pct"] = round(mean(next_open_gaps), 2) if next_open_gaps else None
    if rows:
        metrics["gap_unbuyable_rate_pct"] = round(
            len([row for row in rows if (row.get("forward_outcome") or {}).get("gap_unbuyable")]) / len(rows) * 100.0,
            1,
        )
    else:
        metrics["gap_unbuyable_rate_pct"] = None
    return metrics


def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = [str(item) for item in value[:8]]
        elif isinstance(value, dict):
            safe[key] = {str(k): v for k, v in list(value.items())[:12] if isinstance(v, (str, int, float, bool)) or v is None}
        else:
            safe[key] = str(value)
    return safe
