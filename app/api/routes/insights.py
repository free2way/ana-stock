from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.insight_engine import InsightEngine
from app.services.repository import (
    FundamentalSnapshotRepository,
    PredictionExplanationRepository,
    PredictionRepository,
    PriceSyncStateRepository,
    SymbolRepository,
)


router = APIRouter(prefix="/insights", tags=["insights"])

TEXT = {
    "en": {
        "no_price_history": "No price history yet.",
        "not_enough_data": "Not enough data to draw the chart.",
        "volume": "Volume",
        "breakout": "Breakout",
        "risk": "Risk",
        "back_dashboard": "Back to dashboard",
        "classic_detail": "Open classic detail",
        "search_placeholder": "Search ticker e.g. ASTS",
        "analyze": "Analyze",
        "lang_en": "English",
        "lang_zh": "中文",
        "hero": "Model-Driven Stock View",
        "as_of": "As of",
        "last_sync": "Last sync",
        "trend_score": "Trend Score",
        "current_close": "Current close",
        "confidence": "Confidence",
        "horizon": "Horizon",
        "action_now": "Action Now",
        "reward_risk": "Reward / Risk",
        "reward_risk_help": "Higher is better. This compares upside to the take-profit zone against the risk level.",
        "volume_strength": "Volume Strength",
        "volume_strength_help": "Latest volume versus the 20-day average. Strong breakouts usually want this number above 1.0x.",
        "distance_to_trigger": "Distance To Trigger",
        "distance_to_trigger_help": "How far the current price is from the breakout level.",
        "buy_zone": "Buy Zone",
        "buy_zone_help": "A pullback into this area is the preferred place to start watching for entries.",
        "breakout_trigger": "Breakout Trigger",
        "breakout_trigger_help": "If price reclaims this area with strength, the trend setup improves.",
        "take_profit": "Take Profit",
        "take_profit_help": "This is a model-derived zone to think about trimming into strength.",
        "risk_level": "Risk Level",
        "risk_level_help": "A decisive break below this area weakens the setup and calls for caution.",
        "price_action": "Price Action",
        "why_model": "Why The Model Thinks This",
        "key_levels": "Key Levels",
        "support": "Support",
        "resistance": "Resistance",
        "momentum_read": "Momentum Read",
        "distance_to_entry": "Distance to entry zone",
        "future_model_note": "This panel is still price-only today, but the same view can later be driven by full Qlib model outputs.",
        "five_day_move": "5 day move",
        "twenty_day_move": "20 day move",
        "model_output": "Model Output",
        "model_score": "Model Score",
        "market_rank": "Market Rank",
        "model_run": "Model Run",
        "model_summary": "Model Summary",
        "model_summary_empty": "No trained model output is available for this stock yet.",
        "model_score_help": "Higher means the latest trained model ranks this stock more favorably inside its universe.",
        "market_rank_help": "Position inside the latest model run on the same trade date.",
        "model_run_help": "The most recent training run that produced a prediction for this stock.",
        "bullish_probability": "Bullish Probability",
        "bearish_probability": "Bearish Probability",
        "expected_return_5d": "Expected 5D Return",
        "expected_return_20d": "Expected 20D Return",
        "probability_help": "A score-derived probability proxy until full Qlib probability outputs are wired in.",
        "expected_return_help": "A score-derived return estimate for this MVP view. Later this can be replaced by direct model forecasts.",
        "regime": "Regime",
        "risk_score": "Risk Score",
        "regime_help": "A simple market state label derived from score, trend structure, and volatility context.",
        "risk_score_help": "Higher means more setup risk from weak structure, thin volume, or elevated volatility.",
        "top_drivers": "Top Drivers",
        "feature_contributions": "Feature Contributions",
        "model_positive_factors": "Model Positive Factors",
        "model_negative_factors": "Model Negative Factors",
        "positive_drivers": "Positive Drivers",
        "risk_drivers": "Risk Drivers",
        "drivers_empty": "There is not enough model context yet to explain what is driving this score.",
        "feature_contrib_empty": "No stored feature contributions are available for this model run yet.",
    },
    "zh": {
        "no_price_history": "暂无价格历史数据。",
        "not_enough_data": "数据还不够，暂时无法绘图。",
        "volume": "成交量",
        "breakout": "突破位",
        "risk": "风险位",
        "back_dashboard": "返回 dashboard",
        "classic_detail": "打开经典详情页",
        "search_placeholder": "输入股票代码，例如 ASTS",
        "analyze": "开始分析",
        "lang_en": "English",
        "lang_zh": "中文",
        "hero": "模型驱动个股分析",
        "as_of": "数据日期",
        "last_sync": "最近同步",
        "trend_score": "趋势评分",
        "current_close": "当前收盘价",
        "confidence": "模型信心",
        "horizon": "观察周期",
        "action_now": "当前动作建议",
        "reward_risk": "盈亏比",
        "reward_risk_help": "数值越高越好，表示上方目标空间相对风险位的吸引力。",
        "volume_strength": "量能强度",
        "volume_strength_help": "最新成交量相对 20 日均量的倍数。强突破通常希望这个值大于 1.0x。",
        "distance_to_trigger": "距离突破位",
        "distance_to_trigger_help": "当前价格离突破确认位还有多远。",
        "buy_zone": "买入观察区",
        "buy_zone_help": "如果价格回踩到这一区域，更适合作为重点观察的入场带。",
        "breakout_trigger": "突破确认位",
        "breakout_trigger_help": "如果价格放量站上这个区域，趋势会更强。",
        "take_profit": "止盈参考区",
        "take_profit_help": "这是模型推导出来的止盈参考区，适合考虑分批兑现。",
        "risk_level": "风险失效位",
        "risk_level_help": "如果价格明显跌破这里，当前逻辑就会被削弱。",
        "price_action": "价格走势",
        "why_model": "模型为什么这样判断",
        "key_levels": "关键价位",
        "support": "支撑位",
        "resistance": "压力位",
        "momentum_read": "动量读数",
        "distance_to_entry": "距离买入区",
        "future_model_note": "这一栏目前还是价格结构驱动，后面可以继续接到完整的 Qlib 模型输出。",
        "five_day_move": "5 日涨跌幅",
        "twenty_day_move": "20 日涨跌幅",
        "model_output": "模型输出",
        "model_score": "模型分数",
        "market_rank": "市场排名",
        "model_run": "模型运行",
        "model_summary": "模型结论",
        "model_summary_empty": "这只股票暂时还没有可展示的训练模型输出。",
        "model_score_help": "分数越高，表示最新模型在当前股票池里越看好这只股票。",
        "market_rank_help": "基于同一次模型运行、同一个交易日的排名位置。",
        "model_run_help": "最近一次为这只股票生成预测的训练运行。",
        "bullish_probability": "看多概率",
        "bearish_probability": "看空概率",
        "expected_return_5d": "预期 5 日收益",
        "expected_return_20d": "预期 20 日收益",
        "probability_help": "当前是基于分数推导的概率近似值，后续可以替换成完整 Qlib 概率输出。",
        "expected_return_help": "当前是基于分数推导的预期收益占位结果，后续可替换成模型直接预测值。",
        "regime": "市场状态",
        "risk_score": "风险评分",
        "regime_help": "基于分数、趋势结构和波动环境推导出的简化状态标签。",
        "risk_score_help": "分数越高，表示当前结构、量能或波动带来的执行风险越高。",
        "top_drivers": "核心驱动因子",
        "feature_contributions": "特征贡献",
        "model_positive_factors": "模型正向因子",
        "model_negative_factors": "模型负向因子",
        "positive_drivers": "正向驱动",
        "risk_drivers": "风险拖累",
        "drivers_empty": "当前模型上下文还不够完整，暂时无法给出更细的驱动解释。",
        "feature_contrib_empty": "当前这次模型运行还没有保存下来的特征贡献记录。",
    },
}


def tr(lang: str, key: str) -> str:
    return TEXT["zh" if lang == "zh" else "en"][key]


def _candles_svg(history: list[dict], insight: dict) -> str:
    lang = insight.get("lang", "en")
    if not history:
        return f"<p class='muted'>{tr(lang, 'no_price_history')}</p>"

    candles = history[-50:]
    width = 980
    height = 420
    left_pad = 48
    top_pad = 24
    bottom_pad = 44
    volume_height = 80
    plot_height = height - top_pad - bottom_pad - volume_height - 16
    lows = [row["low"] for row in candles if row.get("low") is not None]
    highs = [row["high"] for row in candles if row.get("high") is not None]
    volumes = [row["volume"] for row in candles if row.get("volume") is not None]
    if not lows or not highs:
        return f"<p class='muted'>{tr(lang, 'not_enough_data')}</p>"

    min_price = min(min(lows), insight["risk_level"], insight["entry_zone"]["low"])
    max_price = max(max(highs), insight["take_profit_zone"]["high"], insight["breakout_level"])
    price_span = max(max_price - min_price, 0.01)

    step = (width - left_pad * 2) / max(len(candles), 1)
    candle_width = max(6, step * 0.55)

    def y_of(price: float) -> float:
        return top_pad + plot_height * (1 - ((price - min_price) / price_span))

    volume_top = top_pad + plot_height + 16
    max_volume = max(volumes) if volumes else 1.0

    candle_parts: list[str] = []
    label_parts: list[str] = []
    volume_parts: list[str] = []
    for index, row in enumerate(candles):
        open_price = row.get("open")
        high_price = row.get("high")
        low_price = row.get("low")
        close_price = row.get("close")
        if None in (open_price, high_price, low_price, close_price):
            continue
        x = left_pad + index * step + step / 2
        wick_top = y_of(high_price)
        wick_bottom = y_of(low_price)
        body_top = y_of(max(open_price, close_price))
        body_bottom = y_of(min(open_price, close_price))
        body_height = max(2, body_bottom - body_top)
        color = "#0f766e" if close_price >= open_price else "#b91c1c"
        candle_parts.append(
            f"<line x1='{x:.2f}' y1='{wick_top:.2f}' x2='{x:.2f}' y2='{wick_bottom:.2f}' stroke='{color}' stroke-width='2' />"
        )
        candle_parts.append(
            f"<rect x='{x - candle_width / 2:.2f}' y='{body_top:.2f}' width='{candle_width:.2f}' "
            f"height='{body_height:.2f}' rx='2' fill='{color}' opacity='0.85' />"
        )
        volume_value = row.get("volume") or 0
        volume_bar_height = (volume_value / max_volume) * volume_height if max_volume else 0
        volume_parts.append(
            f"<rect x='{x - candle_width / 2:.2f}' y='{volume_top + volume_height - volume_bar_height:.2f}' "
            f"width='{candle_width:.2f}' height='{max(2, volume_bar_height):.2f}' rx='1.5' fill='{color}' opacity='0.4' />"
        )
        if index % max(1, len(candles) // 6) == 0:
            label_parts.append(
                f"<text x='{x:.2f}' y='{height - 10}' font-size='10' fill='#6b7280' text-anchor='middle'>{row['date'][5:]}</text>"
            )

    ma20_points: list[str] = []
    window: list[float] = []
    for index, row in enumerate(candles):
        close_price = row.get("close")
        if close_price is None:
            continue
        window.append(close_price)
        sample = window[-20:] if len(window) >= 20 else window
        ma20 = sum(sample) / len(sample)
        x = left_pad + index * step + step / 2
        ma20_points.append(f"{x:.2f},{y_of(ma20):.2f}")

    bands = [
        ("Entry zone", insight["entry_zone"]["low"], insight["entry_zone"]["high"], "#dff5ef"),
        ("Take profit", insight["take_profit_zone"]["low"], insight["take_profit_zone"]["high"], "#fef3c7"),
    ]
    band_parts = []
    for _, low_price, high_price, color in bands:
        y_top = y_of(high_price)
        y_bottom = y_of(low_price)
        band_parts.append(
            f"<rect x='{left_pad}' y='{y_top:.2f}' width='{width - left_pad * 2:.2f}' height='{max(4, y_bottom - y_top):.2f}' "
            f"fill='{color}' opacity='0.45' />"
        )

    lines = [
        (tr(lang, "breakout"), insight["breakout_level"], "#1d4ed8"),
        (tr(lang, "risk"), insight["risk_level"], "#b91c1c"),
    ]
    line_parts = []
    for label, price, color in lines:
        y = y_of(price)
        line_parts.append(
            f"<line x1='{left_pad}' y1='{y:.2f}' x2='{width-left_pad}' y2='{y:.2f}' stroke='{color}' stroke-dasharray='7 5' />"
        )
        line_parts.append(
            f"<text x='{width-left_pad+4}' y='{y + 4:.2f}' font-size='11' fill='{color}'>{label} {price:.2f}</text>"
        )

    return f"""
    <svg viewBox="0 0 {width} {height}" width="100%" height="420" role="img" aria-label="Candlestick chart">
      <rect x="0" y="0" width="{width}" height="{height}" rx="18" fill="#fffdf7"></rect>
      {' '.join(band_parts)}
      <line x1="{left_pad}" y1="{top_pad + plot_height}" x2="{width-left_pad}" y2="{top_pad + plot_height}" stroke="#d6cfc2" />
      <line x1="{left_pad}" y1="{top_pad}" x2="{left_pad}" y2="{top_pad + plot_height}" stroke="#d6cfc2" />
      {' '.join(candle_parts)}
      <polyline fill="none" stroke="#0f766e" stroke-width="3" points="{' '.join(ma20_points)}"></polyline>
      {' '.join(line_parts)}
      <rect x="{left_pad}" y="{volume_top}" width="{width - left_pad * 2}" height="{volume_height}" rx="10" fill="#f7f2e8"></rect>
      {' '.join(volume_parts)}
      <text x="{left_pad}" y="{volume_top - 4}" font-size="11" fill="#6b7280">{tr(lang, "volume")}</text>
      {' '.join(label_parts)}
    </svg>
    """


def _model_output_summary(model_output: dict | None, *, lang: str) -> str:
    if not model_output:
        return tr(lang, "model_summary_empty")
    score = model_output.get("score")
    percentile = model_output.get("percentile")
    run_name = model_output.get("model_run", {}).get("name") or "-"
    if score is None:
        return tr(lang, "model_summary_empty")
    confidence = min(92, max(38, int(45 + abs(float(score)) * 18)))
    if lang == "zh":
        stance = "偏多" if score >= 0 else "偏谨慎"
        percentile_text = f"市场前 {100 - percentile:.1f}% 左右" if percentile is not None else "市场相对位置暂无"
        return f"最新模型运行 {run_name} 对这只股票给出 {score:.3f} 分，结论整体{stance}，置信度约 {confidence}% 。当前大致处于 {percentile_text}。"
    stance = "bullish" if score >= 0 else "cautious"
    percentile_text = f"roughly top {100 - percentile:.1f}% of its universe" if percentile is not None else "without a percentile reading yet"
    return f"The latest model run {run_name} scores this stock at {score:.3f}, which reads as {stance} with about {confidence}% confidence, landing {percentile_text}."


def _model_confidence(score: float | None) -> int | None:
    if score is None:
        return None
    return min(92, max(38, int(45 + abs(float(score)) * 18)))


def _enrich_model_output(model_output: dict | None, *, lang: str) -> dict | None:
    if model_output is None:
        return None

    score = model_output.get("score")
    confidence = _model_confidence(score)
    if score is None:
        model_output["confidence"] = confidence
        model_output["summary_text"] = _model_output_summary(model_output, lang=lang)
        return model_output

    bounded = max(-1.5, min(1.5, float(score) * 8))
    bullish_prob = max(0.05, min(0.95, 0.5 + (bounded / 3.5) * 0.25))
    bearish_prob = max(0.05, min(0.95, 1.0 - bullish_prob))
    expected_return_5d = max(-0.25, min(0.25, float(score) * 0.35))
    expected_return_20d = max(-0.45, min(0.45, float(score) * 0.8))

    if bullish_prob >= 0.64:
        regime_label = "bullish_trend" if lang == "en" else "偏多趋势"
    elif bearish_prob >= 0.6:
        regime_label = "cautious_range" if lang == "en" else "谨慎震荡"
    else:
        regime_label = "balanced_range" if lang == "en" else "中性震荡"
    risk_score = max(8.0, min(92.0, 50.0 - float(score) * 55.0))

    model_output["confidence"] = confidence
    model_output["bullish_prob"] = round(bullish_prob * 100, 1)
    model_output["bearish_prob"] = round(bearish_prob * 100, 1)
    model_output["expected_return_5d"] = round(expected_return_5d * 100, 2)
    model_output["expected_return_20d"] = round(expected_return_20d * 100, 2)
    model_output["regime_label"] = regime_label
    model_output["risk_score"] = round(risk_score, 1)
    model_output["summary_text"] = _model_output_summary(model_output, lang=lang)
    return model_output


def _format_driver(text_en: str, text_zh: str, *, lang: str) -> str:
    return text_zh if lang == "zh" else text_en


def _build_model_drivers(*, insight: dict, model_output: dict | None, fundamentals: dict | None, lang: str) -> dict:
    positive: list[dict] = []
    risks: list[dict] = []

    def add_positive(text_en: str, text_zh: str, strength: int) -> None:
        positive.append(
            {
                "label": _format_driver(text_en, text_zh, lang=lang),
                "strength": max(20, min(95, strength)),
            }
        )

    def add_risk(text_en: str, text_zh: str, strength: int) -> None:
        risks.append(
            {
                "label": _format_driver(text_en, text_zh, lang=lang),
                "strength": max(20, min(95, strength)),
            }
        )

    trend_score = insight.get("trend_score")
    latest_close = insight.get("latest_close")
    ma20 = insight.get("ma20")
    ma60 = insight.get("ma60")
    volume_ratio = insight.get("volume_ratio")
    momentum_20 = insight.get("momentum_20")
    distance_to_breakout_pct = insight.get("distance_to_breakout_pct")

    if trend_score is not None and trend_score >= 67:
        add_positive(
                f"Trend score is strong at {trend_score}/100.",
                f"趋势评分达到 {trend_score}/100，属于偏强结构。",
                min(92, int(trend_score)),
        )
    elif trend_score is not None and trend_score <= 40:
        add_risk(
                f"Trend score is soft at {trend_score}/100.",
                f"趋势评分只有 {trend_score}/100，整体偏弱。",
                max(35, 100 - int(trend_score)),
        )

    if latest_close is not None and ma20 is not None and latest_close > ma20:
        add_positive(
                f"Price is holding above the 20-day average ({ma20:.2f}).",
                f"价格站在 20 日均线 {ma20:.2f} 之上。",
                68,
        )
    elif latest_close is not None and ma20 is not None and latest_close < ma20:
        add_risk(
                f"Price is still below the 20-day average ({ma20:.2f}).",
                f"价格仍在 20 日均线 {ma20:.2f} 下方。",
                66,
        )

    if ma20 is not None and ma60 is not None and ma20 > ma60:
        add_positive(
                "The 20-day average is above the 60-day average.",
                "20 日均线高于 60 日均线，中期结构更健康。",
                72,
        )
    elif ma20 is not None and ma60 is not None and ma20 < ma60:
        add_risk(
                "The 20-day average remains below the 60-day average.",
                "20 日均线仍低于 60 日均线，中期结构还不够强。",
                72,
        )

    if momentum_20 is not None and momentum_20 >= 10:
        add_positive(
                f"20-day momentum is healthy at {momentum_20:.2f}%.",
                f"20 日动量达到 {momentum_20:.2f}%，说明中期走势有延续性。",
                min(90, int(55 + abs(momentum_20))),
        )
    elif momentum_20 is not None and momentum_20 <= -8:
        add_risk(
                f"20-day momentum is weak at {momentum_20:.2f}%.",
                f"20 日动量只有 {momentum_20:.2f}%，中期走势偏弱。",
                min(90, int(55 + abs(momentum_20))),
        )

    if volume_ratio is not None and volume_ratio >= 1.2:
        add_positive(
                f"Volume is supportive at {volume_ratio:.2f}x the 20-day average.",
                f"量能达到 20 日均量的 {volume_ratio:.2f} 倍，资金参与度更强。",
                min(88, int(45 + volume_ratio * 20)),
        )
    elif volume_ratio is not None and volume_ratio < 0.9:
        add_risk(
                f"Volume is muted at only {volume_ratio:.2f}x the 20-day average.",
                f"量能只有 20 日均量的 {volume_ratio:.2f} 倍，突破持续性要打折扣。",
                min(82, int(55 + (1.0 - volume_ratio) * 40)),
        )

    if distance_to_breakout_pct is not None and distance_to_breakout_pct <= 3:
        add_positive(
                f"Price is only {distance_to_breakout_pct:.2f}% away from breakout resistance.",
                f"当前离突破压力位仅 {distance_to_breakout_pct:.2f}%，一旦放量更容易形成突破。",
                62,
        )
    elif distance_to_breakout_pct is not None and distance_to_breakout_pct >= 12:
        add_risk(
                f"Price is still {distance_to_breakout_pct:.2f}% below breakout resistance.",
                f"当前离突破位还有 {distance_to_breakout_pct:.2f}%，需要更多耐心。",
                58,
        )

    if model_output and model_output.get("score") is not None:
        score = float(model_output["score"])
        if score >= 0.08:
            add_positive(
                    f"The latest model score is constructive at {score:.3f}.",
                    f"最新模型分数为 {score:.3f}，整体偏多。",
                    min(92, int(55 + abs(score) * 100)),
            )
        elif score <= -0.08:
            add_risk(
                    f"The latest model score is cautious at {score:.3f}.",
                    f"最新模型分数为 {score:.3f}，模型偏谨慎。",
                    min(92, int(55 + abs(score) * 100)),
            )

    if fundamentals:
        pe_ttm = fundamentals.get("pe_ttm")
        roe_avg_3y = fundamentals.get("roe_avg_3y")
        net_profit_yoy = fundamentals.get("net_profit_yoy")
        revenue_yoy = fundamentals.get("revenue_yoy")
        dividend_yield = fundamentals.get("dividend_yield")
        debt_to_assets = fundamentals.get("debt_to_assets")

        if pe_ttm is not None and 0 < pe_ttm < 25:
            add_positive(
                    f"PE TTM stays reasonable at {pe_ttm:.1f}.",
                    f"市盈率 TTM 为 {pe_ttm:.1f}，估值仍在相对合理区间。",
                    66,
            )
        elif pe_ttm is not None and pe_ttm >= 40:
            add_risk(
                    f"PE TTM is stretched at {pe_ttm:.1f}.",
                    f"市盈率 TTM 达到 {pe_ttm:.1f}，估值压力偏大。",
                    72,
            )

        if roe_avg_3y is not None and roe_avg_3y >= 12:
            add_positive(
                    f"3Y average ROE is solid at {roe_avg_3y:.1f}%.",
                    f"三年平均 ROE 为 {roe_avg_3y:.1f}%，盈利质量不错。",
                    min(88, int(45 + roe_avg_3y)),
            )
        elif roe_avg_3y is not None and roe_avg_3y < 8:
            add_risk(
                    f"3Y average ROE is modest at {roe_avg_3y:.1f}%.",
                    f"三年平均 ROE 只有 {roe_avg_3y:.1f}%，盈利质量一般。",
                    60,
            )

        if net_profit_yoy is not None and net_profit_yoy >= 20:
            add_positive(
                    f"Net profit growth is strong at {net_profit_yoy:.1f}%.",
                    f"净利润同比达到 {net_profit_yoy:.1f}%，成长性较强。",
                    min(90, int(45 + net_profit_yoy)),
            )
        elif net_profit_yoy is not None and net_profit_yoy < 0:
            add_risk(
                    f"Net profit growth is negative at {net_profit_yoy:.1f}%.",
                    f"净利润同比为 {net_profit_yoy:.1f}%，盈利增速转弱。",
                    min(88, int(55 + abs(net_profit_yoy))),
            )

        if revenue_yoy is not None and revenue_yoy >= 12:
            add_positive(
                    f"Revenue growth is supportive at {revenue_yoy:.1f}%.",
                    f"营收同比达到 {revenue_yoy:.1f}%，收入增长对估值更有支撑。",
                    min(84, int(45 + revenue_yoy)),
            )

        if dividend_yield is not None and dividend_yield >= 3:
            add_positive(
                    f"Dividend yield adds support at {dividend_yield:.1f}%.",
                    f"股息率约为 {dividend_yield:.1f}%，对持有体验有加分。",
                    min(78, int(40 + dividend_yield * 8)),
            )

        if debt_to_assets is not None and debt_to_assets >= 70:
            add_risk(
                    f"Debt to assets is elevated at {debt_to_assets:.1f}%.",
                    f"资产负债率达到 {debt_to_assets:.1f}%，杠杆风险偏高。",
                    min(88, int(35 + debt_to_assets / 1.3)),
            )

    return {
        "positive": sorted(positive, key=lambda item: item["strength"], reverse=True)[:4],
        "risks": sorted(risks, key=lambda item: item["strength"], reverse=True)[:4],
    }


def _fundamental_summary(fundamentals: dict | None) -> dict | None:
    if not fundamentals:
        return None
    keys = (
        "report_date",
        "source",
        "pe_ttm",
        "dividend_yield",
        "market_cap",
        "roe_avg_3y",
        "net_profit_yoy",
        "revenue_yoy",
        "debt_to_assets",
    )
    return {key: fundamentals.get(key) for key in keys}


def _feature_label(feature_name: str, *, lang: str) -> str:
    if feature_name == "recent_daily_return":
        return "Recent Daily Return" if lang == "en" else "最近单日涨跌幅"
    if feature_name.startswith("lag_return_"):
        horizon = feature_name.removeprefix("lag_return_")
        return f"Lagged Return ({horizon} ago)" if lang == "en" else f"滞后收益（{horizon} 前）"
    if feature_name.startswith("lookback_momentum_"):
        horizon = feature_name.removeprefix("lookback_momentum_")
        return f"Lookback Momentum ({horizon})" if lang == "en" else f"回看动量（{horizon}）"
    if feature_name == "price_vs_ma20":
        return "Price vs MA20" if lang == "en" else "价格相对 MA20"
    if feature_name == "ma_alignment":
        return "Moving Average Alignment" if lang == "en" else "均线排列"
    if feature_name == "volume_ratio_20d":
        return "Volume Ratio (20D)" if lang == "en" else "20 日量能比"
    return feature_name


def _serialize_feature_contributions(rows: list[dict], *, lang: str) -> dict:
    positive: list[dict] = []
    negative: list[dict] = []
    for row in rows:
        contribution = row.get("contribution")
        payload = {
            "feature_name": row.get("feature_name"),
            "label": _feature_label(row.get("feature_name") or "-", lang=lang),
            "feature_value": row.get("feature_value"),
            "contribution": contribution,
            "strength": min(95, max(18, int(35 + abs(contribution or 0.0) * 6))) if contribution is not None else 18,
        }
        if (contribution or 0.0) >= 0:
            positive.append(payload)
        else:
            negative.append(payload)
    positive.sort(key=lambda item: abs(item.get("contribution") or 0.0), reverse=True)
    negative.sort(key=lambda item: abs(item.get("contribution") or 0.0), reverse=True)
    return {
        "positive": positive,
        "negative": negative,
    }


def _build_model_context(*, ticker: str, lang: str, db: Session) -> dict:
    fundamentals_repo = FundamentalSnapshotRepository(db)
    explanation_repo = PredictionExplanationRepository(db)
    model_output = _enrich_model_output(
        PredictionRepository(db).get_latest_model_output_for_ticker(ticker),
        lang=lang,
    )
    insight = InsightEngine().get_insight(ticker, lang=lang)
    fundamentals = fundamentals_repo.get_latest_for_ticker(ticker)
    feature_contributions = _serialize_feature_contributions(explanation_repo.get_latest_for_ticker(ticker), lang=lang)
    drivers = _build_model_drivers(
        insight=insight or {},
        model_output=model_output,
        fundamentals=fundamentals,
        lang=lang,
    )
    return {
        "model_output": model_output,
        "insight": insight,
        "fundamentals": fundamentals,
        "fundamental_summary": _fundamental_summary(fundamentals),
        "feature_contributions": feature_contributions,
        "drivers": drivers,
    }


@router.get("/open")
def open_insight(
    request: Request,
    ticker: str = Query(..., min_length=1),
    lang: str = Query("en"),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/insights/{ticker.strip().upper()}?lang={'zh' if lang == 'zh' else 'en'}")
    lang = "zh" if lang == "zh" else "en"
    return RedirectResponse(url=f"/insights/{ticker.strip().upper()}?lang={lang}", status_code=303)


@router.get("/{ticker}/summary")
def insight_summary(ticker: str, lang: str = Query("en")) -> dict:
    insight = InsightEngine().get_insight(ticker, lang=lang)
    if insight is None:
        raise HTTPException(status_code=404, detail="Ticker not found in local dataset.")
    return insight


@router.get("/{ticker}/model-output")
def insight_model_output(ticker: str, lang: str = Query("en"), db: Session = Depends(get_db_session)) -> dict:
    lang = "zh" if lang == "zh" else "en"
    context = _build_model_context(ticker=ticker, lang=lang, db=db)
    model_output = context["model_output"]
    if model_output is None:
        raise HTTPException(status_code=404, detail="Model output not found for ticker.")
    payload = dict(model_output)
    payload["drivers"] = context["drivers"]
    payload["fundamentals"] = context["fundamental_summary"]
    payload["feature_contributions"] = context["feature_contributions"]
    return payload


@router.get("/{ticker}", response_class=HTMLResponse)
def insight_page(
    request: Request,
    ticker: str,
    lang: str = Query("en"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        target = f"/insights/{ticker.strip().upper()}?lang={'zh' if lang == 'zh' else 'en'}"
        return login_redirect(target)
    lang = "zh" if lang == "zh" else "en"
    context = _build_model_context(ticker=ticker, lang=lang, db=db)
    insight = context["insight"]
    if insight is None:
        raise HTTPException(status_code=404, detail="Ticker not found in local dataset.")

    symbol_repo = SymbolRepository(db)
    sync_repo = PriceSyncStateRepository(db)
    overview = symbol_repo.get_overview(ticker) or {"ticker": insight["ticker"], "name": insight["ticker"], "market": "US"}
    sync_state = sync_repo.get_state_for_ticker(ticker)
    fundamentals = context["fundamentals"]
    model_output = context["model_output"]
    feature_contributions = context["feature_contributions"]
    sync_text = sync_state["last_synced_date"] if sync_state else "not synced"
    bullets = "".join(f"<li>{escape(item)}</li>" for item in insight["explanation"])
    chart = _candles_svg(insight["history"], insight)
    model_summary = _model_output_summary(model_output, lang=lang)
    model_confidence = model_output.get("confidence") if model_output else None
    model_run_name = "-"
    if model_output:
        model_run_name = model_output.get("model_run", {}).get("name") or "-"
    model_drivers = context["drivers"]
    positive_driver_items = "".join(
        (
            f"<li><div style='display:flex; justify-content:space-between; gap:12px; align-items:center;'>"
            f"<span>{escape(item['label'])}</span><strong>{item['strength']}</strong></div>"
            f"<div style='margin-top:6px; height:8px; border-radius:999px; background:#ecfdf5; overflow:hidden;'>"
            f"<div style='width:{item['strength']}%; height:100%; background:#0f766e;'></div></div></li>"
        )
        for item in model_drivers["positive"]
    )
    risk_driver_items = "".join(
        (
            f"<li><div style='display:flex; justify-content:space-between; gap:12px; align-items:center;'>"
            f"<span>{escape(item['label'])}</span><strong>{item['strength']}</strong></div>"
            f"<div style='margin-top:6px; height:8px; border-radius:999px; background:#fef2f2; overflow:hidden;'>"
            f"<div style='width:{item['strength']}%; height:100%; background:#b91c1c;'></div></div></li>"
        )
        for item in model_drivers["risks"]
    )
    positive_feature_items = "".join(
        (
            f"<li><div style='display:flex; justify-content:space-between; gap:12px; align-items:center;'>"
            f"<span>{escape(item['label'])}</span><strong>{item['contribution']:.2f}</strong></div>"
            f"<div class='muted' style='margin-top:4px;'>"
            f"{'Value' if lang == 'en' else '数值'}: {item['feature_value']:.2f}</div>"
            f"<div style='margin-top:6px; height:8px; border-radius:999px; background:#ecfdf5; overflow:hidden;'>"
            f"<div style='width:{item['strength']}%; height:100%; background:#0f766e;'></div></div></li>"
        )
        for item in feature_contributions["positive"]
    )
    negative_feature_items = "".join(
        (
            f"<li><div style='display:flex; justify-content:space-between; gap:12px; align-items:center;'>"
            f"<span>{escape(item['label'])}</span><strong>{item['contribution']:.2f}</strong></div>"
            f"<div class='muted' style='margin-top:4px;'>"
            f"{'Value' if lang == 'en' else '数值'}: {item['feature_value']:.2f}</div>"
            f"<div style='margin-top:6px; height:8px; border-radius:999px; background:#fef2f2; overflow:hidden;'>"
            f"<div style='width:{item['strength']}%; height:100%; background:#b91c1c;'></div></div></li>"
        )
        for item in feature_contributions["negative"]
    )
    lang_switch = (
        f"<a href='/insights/{insight['ticker']}?lang=en'>{tr(lang, 'lang_en')}</a> | "
        f"<a href='/insights/{insight['ticker']}?lang=zh'>{tr(lang, 'lang_zh')}</a>"
    )

    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{insight['ticker']} Insight</title>
        <style>
          :root {{
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
            --accent-soft: #dff5ef;
          }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background:
            radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
            radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
            var(--bg); }}
          .wrap {{ max-width: 1160px; margin: 0 auto; padding: 28px 20px 56px; }}
          .topbar {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-bottom:16px; }}
          .topbar a {{ color: var(--accent); text-decoration:none; }}
          .search {{ display:flex; gap:8px; flex-wrap:wrap; }}
          .grid {{ display:grid; gap:16px; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); margin-bottom:16px; }}
          .card {{ background: var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow: 0 8px 24px rgba(31,41,55,0.05); }}
          .hero {{ display:grid; gap:16px; grid-template-columns: minmax(280px, 2fr) minmax(240px, 1fr); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h1 {{ margin:0 0 8px; font-size:42px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .metric {{ font-size:30px; font-weight:700; margin:6px 0; }}
          .bigcopy {{ font-size:18px; line-height:1.6; margin:8px 0 0; }}
          .price {{ font-size:34px; font-weight:700; }}
          ul {{ margin:0; padding-left:18px; line-height:1.7; }}
          form {{ margin:0; }}
          input, select, button {{
            border-radius: 12px;
            border: 1px solid var(--line);
            padding: 10px 12px;
            font: inherit;
            background: #fff;
          }}
          button {{ background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 700; }}
          .pill {{ display:inline-block; padding:8px 12px; border-radius:999px; font-weight:700; background:#eef8f5; color:#0f766e; }}
          .chart-card {{ margin-bottom:16px; }}
          .actions {{ display:grid; gap:12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
          .label {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; }}
          .signal-hero {{ display:grid; gap:16px; grid-template-columns: minmax(260px, 1.4fr) repeat(3, minmax(180px, 1fr)); margin-bottom:16px; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="topbar">
            <a href="/dashboard">← {tr(lang, 'back_dashboard')}</a>
            <a href="/symbols/{insight['ticker']}">{tr(lang, 'classic_detail')}</a>
            <form class="search" action="/insights/open" method="get">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="text" name="ticker" value="{insight['ticker']}" placeholder="{tr(lang, 'search_placeholder')}" />
              <button type="submit">{tr(lang, 'analyze')}</button>
            </form>
            <span class="muted">{lang_switch}</span>
          </div>

          <section class="hero">
            <article class="card">
              <div class="eyebrow">{tr(lang, 'hero')}</div>
              <h1>{insight['ticker']}</h1>
              <div class="muted">{escape(overview.get('name') or insight['ticker'])} | {tr(lang, 'as_of')} {insight['as_of_date']} | {tr(lang, 'last_sync')}: {sync_text}</div>
              <p class="bigcopy">{escape(insight['recommendation'])}</p>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'trend_score')}</div>
              <div class="metric">{insight['trend_score']}/100</div>
              <div class="pill">{insight['trend_label']} · {insight['setup_label']}</div>
              <div class="muted" style="margin-top:10px;">{tr(lang, 'confidence')}: {int(insight['confidence'] * 100)}% | {tr(lang, 'horizon')}: {insight['expected_horizon']}</div>
              <div class="price" style="margin-top:12px;">${insight['latest_close']:.2f}</div>
              <div class="muted">{tr(lang, 'current_close')}</div>
            </article>
          </section>

          <section class="signal-hero">
            <article class="card">
              <div class="eyebrow">{tr(lang, 'action_now')}</div>
              <div class="metric">{escape(insight['action_label'])}</div>
              <div class="muted">{escape(insight['action_summary'])}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'reward_risk')}</div>
              <div class="metric">{f"{insight['reward_risk_ratio']:.2f}x" if insight['reward_risk_ratio'] is not None else '-'}</div>
              <div class="muted">{tr(lang, 'reward_risk_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'volume_strength')}</div>
              <div class="metric">{f"{insight['volume_ratio']:.2f}x" if insight['volume_ratio'] is not None else '-'}</div>
              <div class="muted">{tr(lang, 'volume_strength_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'distance_to_trigger')}</div>
              <div class="metric">{f"{insight['distance_to_breakout_pct']:.2f}%" if insight['distance_to_breakout_pct'] is not None else '-'}</div>
              <div class="muted">{tr(lang, 'distance_to_trigger_help')}</div>
            </article>
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">{tr(lang, 'model_output')}</div>
              <div class="metric">{f"{model_output['score']:.3f}" if model_output and model_output.get('score') is not None else '-'}</div>
              <div class="muted">{tr(lang, 'model_score_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'market_rank')}</div>
              <div class="metric">{f"{int(model_output['rank_value'])} / {int(model_output['universe_size'])}" if model_output and model_output.get('rank_value') is not None and model_output.get('universe_size') else '-'}</div>
              <div class="muted">{tr(lang, 'market_rank_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'confidence')}</div>
              <div class="metric">{f"{model_confidence}%" if model_confidence is not None else '-'}</div>
              <div class="muted">{tr(lang, 'model_score_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'model_run')}</div>
              <div class="metric" style="font-size:24px;">{escape(model_run_name)}</div>
              <div class="muted">{tr(lang, 'model_run_help')}</div>
            </article>
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">{tr(lang, 'bullish_probability')}</div>
              <div class="metric">{f"{model_output['bullish_prob']:.1f}%" if model_output and model_output.get('bullish_prob') is not None else '-'}</div>
              <div class="muted">{tr(lang, 'probability_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'bearish_probability')}</div>
              <div class="metric">{f"{model_output['bearish_prob']:.1f}%" if model_output and model_output.get('bearish_prob') is not None else '-'}</div>
              <div class="muted">{tr(lang, 'probability_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'expected_return_5d')}</div>
              <div class="metric">{f"{model_output['expected_return_5d']:.2f}%" if model_output and model_output.get('expected_return_5d') is not None else '-'}</div>
              <div class="muted">{tr(lang, 'expected_return_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'expected_return_20d')}</div>
              <div class="metric">{f"{model_output['expected_return_20d']:.2f}%" if model_output and model_output.get('expected_return_20d') is not None else '-'}</div>
              <div class="muted">{tr(lang, 'expected_return_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'regime')}</div>
              <div class="metric" style="font-size:24px;">{escape(model_output.get('regime_label') or '-') if model_output else '-'}</div>
              <div class="muted">{tr(lang, 'regime_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'risk_score')}</div>
              <div class="metric">{f"{model_output['risk_score']:.1f}" if model_output and model_output.get('risk_score') is not None else '-'}</div>
              <div class="muted">{tr(lang, 'risk_score_help')}</div>
            </article>
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">{tr(lang, 'buy_zone')}</div>
              <div class="metric">${insight['entry_zone']['low']:.2f} - ${insight['entry_zone']['high']:.2f}</div>
              <div class="muted">{tr(lang, 'buy_zone_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'breakout_trigger')}</div>
              <div class="metric">${insight['breakout_level']:.2f}</div>
              <div class="muted">{tr(lang, 'breakout_trigger_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'take_profit')}</div>
              <div class="metric">${insight['take_profit_zone']['low']:.2f} - ${insight['take_profit_zone']['high']:.2f}</div>
              <div class="muted">{tr(lang, 'take_profit_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'risk_level')}</div>
              <div class="metric">${insight['risk_level']:.2f}</div>
              <div class="muted">{tr(lang, 'risk_level_help')}</div>
            </article>
          </section>

          <section class="card chart-card">
            <div class="eyebrow">{tr(lang, 'price_action')}</div>
            {chart}
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">{tr(lang, 'why_model')}</div>
              <ul>{bullets}</ul>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'key_levels')}</div>
              <div class="actions">
                <div>
                  <div class="label">{tr(lang, 'support')}</div>
                  <div class="metric">${insight['support_level']:.2f}</div>
                </div>
                <div>
                  <div class="label">{tr(lang, 'resistance')}</div>
                  <div class="metric">${insight['resistance_level']:.2f}</div>
                </div>
                <div>
                  <div class="label">MA 20</div>
                  <div class="metric">{'$' + format(insight['ma20'], '.2f') if insight['ma20'] is not None else '-'}</div>
                </div>
                <div>
                  <div class="label">MA 60</div>
                  <div class="metric">{'$' + format(insight['ma60'], '.2f') if insight['ma60'] is not None else '-'}</div>
                </div>
              </div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'momentum_read')}</div>
              <div class="muted">{tr(lang, 'five_day_move')}: {f"{insight['momentum_5']:.2f}%" if insight['momentum_5'] is not None else '-'}</div>
              <div class="muted">{tr(lang, 'twenty_day_move')}: {f"{insight['momentum_20']:.2f}%" if insight['momentum_20'] is not None else '-'}</div>
              <div class="muted">{tr(lang, 'distance_to_entry')}: {f"{insight['distance_to_entry_pct']:.2f}%" if insight['distance_to_entry_pct'] is not None else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'model_summary')}</div>
              <div class="muted" style="line-height:1.7;">{escape(model_summary)}</div>
              <div class="muted" style="margin-top:10px;">{tr(lang, 'future_model_note')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'top_drivers')}</div>
              <div class="label" style="margin-bottom:8px;">{tr(lang, 'positive_drivers')}</div>
              {"<ul>" + positive_driver_items + "</ul>" if positive_driver_items else f"<div class='muted'>{tr(lang, 'drivers_empty')}</div>"}
              <div class="label" style="margin:14px 0 8px;">{tr(lang, 'risk_drivers')}</div>
              {"<ul>" + risk_driver_items + "</ul>" if risk_driver_items else f"<div class='muted'>{tr(lang, 'drivers_empty')}</div>"}
            </article>
            <article class="card">
              <div class="eyebrow">{tr(lang, 'feature_contributions')}</div>
              <div class="label" style="margin-bottom:8px;">{tr(lang, 'model_positive_factors')}</div>
              {"<ul>" + positive_feature_items + "</ul>" if positive_feature_items else f"<div class='muted'>{tr(lang, 'feature_contrib_empty')}</div>"}
              <div class="label" style="margin:14px 0 8px;">{tr(lang, 'model_negative_factors')}</div>
              {"<ul>" + negative_feature_items + "</ul>" if negative_feature_items else f"<div class='muted'>{tr(lang, 'feature_contrib_empty')}</div>"}
            </article>
          </section>
        </main>
      </body>
    </html>
    """
