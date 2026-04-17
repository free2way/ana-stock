from datetime import date, datetime
from dataclasses import asdict
import json

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.insight_engine import InsightEngine
from app.services.runtime_cache import get_or_set
from app.services.repository import (
    FundamentalSnapshotRepository,
    PredictionExplanationRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PriceSyncStateRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WatchlistRepository,
)
from app.services.model_signal_summary import build_model_state, enrich_model_output, summarize_explanations
from app.services.technical_patterns import TechnicalPatternService
from app.services.tradingview_client import TradingViewClient
from app.services.tushare_client import TushareClient


MODEL_TEMPLATES = {
    "next_tesla_swing": {
        "label": "Next Tesla Swing",
        "description": "强趋势二次启动模板：寻找处于强趋势中、回踩支撑或接近干净突破位的领涨股。",
        "market": "ALL",
        "mode": "next_tesla",
        "defaults": {
            "min_trend_score": 68,
            "min_volume_ratio": 0.8,
        },
    },
    "technical_momentum": {
        "label": "Technical Momentum",
        "description": "Use the current insight engine to rank stocks by trend strength and volume support.",
        "market": "ALL",
        "mode": "technical",
    },
    "cn_limit_up_watch": {
        "label": "昨日涨停观察",
        "description": "A-share tactical template for names that hit limit-up yesterday and still deserve today's attention.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["limit_up_yesterday"],
    },
    "cn_volume_breakout": {
        "label": "底部放量突破",
        "description": "A-share technical template for base breakouts supported by expanding volume.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["volume_breakout"],
    },
    "cn_bullish_ma_stack": {
        "label": "均线多头排列",
        "description": "A-share trend template for bullish moving-average alignment and rising medium-term structure.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["bullish_ma_stack"],
    },
    "cn_macd_underwater_cross": {
        "label": "MACD水下金叉",
        "description": "A-share early-reversal template for MACD bullish crossover below the zero line.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["macd_underwater_cross"],
    },
    "cn_ma_cluster_breakout_watch": {
        "label": "均线密集待突破",
        "description": "A-share compression template for names with tightly clustered moving averages before a directional move.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["ma_cluster"],
    },
    "cn_bollinger_squeeze_watch": {
        "label": "布林带收口待突破",
        "description": "A-share volatility compression template for names with Bollinger Band squeeze and coiled price action.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["bollinger_squeeze"],
    },
    "cn_three_white_soldiers": {
        "label": "三连阳强势延续",
        "description": "A-share candlestick template for names showing three consecutive bullish candles with improving closes.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["three_white_soldiers"],
    },
    "cn_bullish_engulfing_reversal": {
        "label": "看涨吞没反转",
        "description": "A-share reversal template for names printing a bullish engulfing candle after weakness.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["bullish_engulfing"],
    },
    "cn_hammer_reversal": {
        "label": "锤子线反转",
        "description": "A-share reversal template for names printing a hammer candle after short-term weakness.",
        "market": "CN",
        "mode": "technical_pattern",
        "required_patterns": ["hammer_reversal"],
    },
    "tv_multi_timeframe_bullish": {
        "label": "TradingView多周期共振",
        "description": "Use TradingView daily, weekly, and monthly ratings to find names with broad multi-timeframe alignment.",
        "market": "ALL",
        "mode": "technical_rating",
        "defaults": {
            "min_trend_score": 55,
            "min_volume_ratio": 0.8,
        },
    },
    "global_growth_value": {
        "label": "Global Growth at Reasonable Value",
        "description": "US and HK template using valuation, growth, ROE, and market cap filters.",
        "market": "ALL",
        "mode": "fundamental",
        "defaults": {
            "min_listing_days": 365,
            "pe_min": 0.0,
            "pe_max": 35.0,
            "min_roe_avg_3y": 8.0,
            "min_net_profit_yoy": 10.0,
            "min_revenue_yoy": 8.0,
            "max_debt_to_assets": 75.0,
            "exclude_bottom_market_cap_pct": 10.0,
        },
    },
    "global_income_quality": {
        "label": "Global Income and Quality",
        "description": "US and HK income template using dividend yield, valuation, quality, and leverage filters.",
        "market": "ALL",
        "mode": "fundamental",
        "defaults": {
            "min_listing_days": 730,
            "pe_min": 0.0,
            "pe_max": 30.0,
            "min_roe_avg_3y": 10.0,
            "min_net_profit_yoy": 0.0,
            "min_revenue_yoy": 0.0,
            "max_debt_to_assets": 70.0,
            "min_dividend_yield": 1.5,
            "exclude_bottom_market_cap_pct": 10.0,
        },
    },
    "cn_growth_value": {
        "label": "高成长低估值",
        "description": "A-share growth and value template using PE, 3Y ROE, profit growth, listing age, and market cap filters.",
        "market": "CN",
        "mode": "fundamental",
        "defaults": {
            "min_listing_days": 365,
            "pe_min": 0.0,
            "pe_max": 30.0,
            "min_roe_avg_3y": 12.0,
            "min_net_profit_yoy": 20.0,
            "exclude_bottom_market_cap_pct": 10.0,
        },
    },
    "cn_high_roe_steady_growth": {
        "label": "高ROE稳增长",
        "description": "A-share quality template focusing on durable ROE, positive revenue growth, profit growth, and manageable leverage.",
        "market": "CN",
        "mode": "fundamental",
        "defaults": {
            "min_listing_days": 730,
            "pe_min": 0.0,
            "pe_max": 60.0,
            "min_roe_avg_3y": 15.0,
            "min_net_profit_yoy": 10.0,
            "min_revenue_yoy": 10.0,
            "max_debt_to_assets": 65.0,
            "exclude_bottom_market_cap_pct": 20.0,
        },
    },
    "cn_low_valuation_high_dividend": {
        "label": "低估值高分红",
        "description": "A-share income template focusing on reasonable valuation, dividend yield, solid ROE, and moderate leverage.",
        "market": "CN",
        "mode": "fundamental",
        "defaults": {
            "min_listing_days": 1095,
            "pe_min": 0.0,
            "pe_max": 20.0,
            "min_roe_avg_3y": 10.0,
            "min_net_profit_yoy": 0.0,
            "min_revenue_yoy": 0.0,
            "max_debt_to_assets": 70.0,
            "min_dividend_yield": 3.0,
            "exclude_bottom_market_cap_pct": 10.0,
        },
    },
}


MARKET_SORT_ORDER = {
    "CN": 0,
    "HK": 1,
    "US": 2,
}


SIGNAL_FILTER_MAP = {
    "ALL": None,
    "BUY": "buy",
    "WATCH": "watch",
    "SELL": "sell",
    "HOLD": "hold",
}

PATTERN_MATCH_LABELS = {
    "limit_up_yesterday": "昨日涨停",
    "volume_breakout": "底部放量突破",
    "ma_cluster": "均线密集缠绕",
    "bullish_ma_stack": "均线多头排列",
    "macd_underwater_cross": "MACD水下金叉",
    "bollinger_squeeze": "布林带收口",
    "three_white_soldiers": "三连阳",
    "bullish_engulfing": "看涨吞没",
    "hammer_reversal": "锤子线",
}


def _matches_execution_tag_filter(tags: list[str] | None, execution_tag_filter: str) -> bool:
    normalized = str(execution_tag_filter or "").strip().lower()
    if not normalized or normalized == "all":
        return True
    requested = [part.strip() for part in normalized.split(",") if part.strip() and part.strip() != "all"]
    if not requested:
        return True
    values = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    return any(tag in values for tag in requested)


def _excludes_execution_tag_filter(tags: list[str] | None, exclude_execution_tag_filter: str) -> bool:
    normalized = str(exclude_execution_tag_filter or "").strip().lower()
    if not normalized or normalized == "all":
        return True
    requested = [part.strip() for part in normalized.split(",") if part.strip() and part.strip() != "all"]
    if not requested:
        return True
    values = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    return not any(tag in values for tag in requested)


def _normalize_action_value(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if normalized in {"trim_into_strength", "avoid_or_wait", "continue_to_watch", "continue_watching"}:
        return "wait"
    if normalized in {"buy_the_dip", "wait_for_breakout", "hold_and_watch", "wait"}:
        return normalized
    return normalized


class ScreenerService:
    def __init__(self) -> None:
        self.insight_engine = InsightEngine()
        self.technical_patterns = TechnicalPatternService()
        self.tradingview = TradingViewClient()

    def _get_cached_insight(self, ticker: str, *, limit: int | None = None, lang: str = "en") -> dict | None:
        cache_key = json.dumps(
            {
                "ticker": ticker,
                "limit": limit or 0,
                "lang": lang,
            },
            sort_keys=True,
        )
        return get_or_set(
            "screener_insight",
            cache_key,
            ttl_seconds=120.0,
            loader=lambda: (
                self.insight_engine.get_insight(ticker, limit=limit, lang=lang)
                if limit is not None
                else self.insight_engine.get_insight(ticker, lang=lang)
            ),
        )

    def screen(
        self,
        *,
        model_template: str = "technical_momentum",
        universe: str = "watchlist",
        market: str = "ALL",
        min_trend_score: int = 60,
        action_filter: str = "ALL",
        min_volume_ratio: float = 0.0,
        min_listing_days: int = 365,
        pe_min: float = 0.0,
        pe_max: float = 30.0,
        min_roe_avg_3y: float = 12.0,
        min_net_profit_yoy: float = 20.0,
        min_revenue_yoy: float = 0.0,
        max_debt_to_assets: float = 100.0,
        min_dividend_yield: float = 0.0,
        exclude_bottom_market_cap_pct: float = 10.0,
        recent_snapshot_runs: int = 0,
        min_snapshot_hits: int = 0,
        model_signal_filter: str = "ALL",
        min_model_signal_strength: float = 0.0,
        execution_tag_filter: str = "ALL",
        exclude_execution_tag_filter: str = "ALL",
        sort_by: str = "default",
        sort_order: str = "desc",
        limit: int = 50,
    ) -> list[dict]:
        template = MODEL_TEMPLATES.get(model_template, MODEL_TEMPLATES["technical_momentum"])
        if template["mode"] == "fundamental":
            fixed_market = template.get("market")
            effective_market = fixed_market if fixed_market and fixed_market != "ALL" else market
            results = self._screen_fundamental_template(
                template_key=model_template,
                universe=universe,
                market=effective_market,
                min_trend_score=min_trend_score,
                action_filter=action_filter,
                min_volume_ratio=min_volume_ratio,
                min_listing_days=min_listing_days,
                pe_min=pe_min,
                pe_max=pe_max,
                min_roe_avg_3y=min_roe_avg_3y,
                min_net_profit_yoy=min_net_profit_yoy,
                min_revenue_yoy=min_revenue_yoy,
                max_debt_to_assets=max_debt_to_assets,
                min_dividend_yield=min_dividend_yield,
                exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
                recent_snapshot_runs=recent_snapshot_runs,
                min_snapshot_hits=min_snapshot_hits,
            )
        elif template["mode"] == "technical_pattern":
            fixed_market = template.get("market")
            effective_market = fixed_market if fixed_market and fixed_market != "ALL" else market
            results = self._screen_technical_patterns(
                template_key=model_template,
                required_patterns=template.get("required_patterns", []),
                universe=universe,
                market=effective_market,
                min_trend_score=min_trend_score,
                action_filter=action_filter,
                min_volume_ratio=min_volume_ratio,
                recent_snapshot_runs=recent_snapshot_runs,
                min_snapshot_hits=min_snapshot_hits,
            )
        elif template["mode"] == "technical_rating":
            fixed_market = template.get("market")
            effective_market = fixed_market if fixed_market and fixed_market != "ALL" else market
            results = self._screen_tradingview_alignment(
                universe=universe,
                market=effective_market,
                min_trend_score=min_trend_score,
                min_volume_ratio=min_volume_ratio,
            )
        elif template["mode"] == "next_tesla":
            fixed_market = template.get("market")
            effective_market = fixed_market if fixed_market and fixed_market != "ALL" else market
            results = self._screen_next_tesla_swing(
                universe=universe,
                market=effective_market,
                min_trend_score=min_trend_score,
                min_volume_ratio=min_volume_ratio,
                recent_snapshot_runs=recent_snapshot_runs,
                min_snapshot_hits=min_snapshot_hits,
            )
        else:
            results = self._screen_technical(
                universe=universe,
                market=market,
                min_trend_score=min_trend_score,
                action_filter=action_filter,
                min_volume_ratio=min_volume_ratio,
                recent_snapshot_runs=recent_snapshot_runs,
                min_snapshot_hits=min_snapshot_hits,
            )
        results = self._apply_model_signal_filter(
            results,
            model_signal_filter=model_signal_filter,
            min_model_signal_strength=min_model_signal_strength,
        )
        results = self._apply_execution_tag_filter(
            results,
            execution_tag_filter=execution_tag_filter,
            exclude_execution_tag_filter=exclude_execution_tag_filter,
        )
        return self._sort_results(results, sort_by=sort_by, sort_order=sort_order)[:limit]

    def build_market_snapshot(
        self,
        *,
        market: str = "CN",
        limit_per_board: int = 12,
        mode: str = "monitor",
    ) -> list[dict]:
        snapshot_mode = (mode or "monitor").strip().lower()
        cache_key = json.dumps({"market": market, "limit": limit_per_board, "mode": snapshot_mode}, sort_keys=True)

        def _load() -> list[dict]:
            boards = [
                {
                    "key": "leaders",
                    "template": "technical_momentum",
                    "title_en": "Momentum Leaders",
                    "title_zh": "强势榜",
                    "description_en": "Trend and volume leaders from the latest local market data.",
                    "description_zh": "基于本地行情与量价结构筛出的趋势强股。",
                    "sort_by": "trend_score",
                },
                {
                    "key": "squeeze",
                    "template": "cn_bollinger_squeeze_watch",
                    "title_en": "Squeeze Watch",
                    "title_zh": "收口榜",
                    "description_en": "Names with compressed Bollinger Bands and coiled price action.",
                    "description_zh": "波动率收缩、价格待选择方向的候选股。",
                    "sort_by": "volume_ratio",
                },
                {
                    "key": "three_white_soldiers",
                    "template": "cn_three_white_soldiers",
                    "title_en": "Three White Soldiers",
                    "title_zh": "连阳榜",
                    "description_en": "Stocks showing three consecutive strong bullish candles.",
                    "description_zh": "连续三根强势阳线、收盘逐步抬高的股票。",
                    "sort_by": "momentum_5",
                },
                {
                    "key": "volume_breakout",
                    "template": "cn_volume_breakout",
                    "title_en": "Volume Breakout",
                    "title_zh": "放量榜",
                    "description_en": "Base breakouts supported by expanding turnover and confirmation volume.",
                    "description_zh": "底部放量突破、量能确认更充分的候选股。",
                    "sort_by": "volume_ratio",
                },
            ]
            snapshot: list[dict] = []
            for board in boards:
                rows = self.screen(
                    model_template=board["template"],
                    universe="full_market",
                    market=market,
                    min_trend_score=45 if board["template"] == "technical_momentum" else 35,
                    action_filter="ALL",
                    min_volume_ratio=0.0,
                    limit=limit_per_board,
                    sort_by=board["sort_by"],
                    sort_order="desc",
                )
                for row in rows:
                    row["snapshot_score"] = self._market_snapshot_score(board["key"], row, snapshot_mode)
                    row["snapshot_score_breakdown"] = self._market_snapshot_score_breakdown(board["key"], row, snapshot_mode)
                rows.sort(
                    key=lambda item: (
                        -(item.get("snapshot_score") or 0),
                        -(item.get("trend_score") or 0),
                        -(item.get("volume_ratio") or 0),
                        item.get("ticker", ""),
                    )
                )
                snapshot.append({**board, "rows": rows, "mode": snapshot_mode})
            return snapshot

        return get_or_set("market_snapshot", cache_key, ttl_seconds=90.0, loader=_load)

    def _market_snapshot_score(self, board_key: str, row: dict, mode: str = "monitor") -> int:
        trend_score = max(0.0, min(100.0, float(row.get("trend_score") or 0.0)))
        momentum_5 = max(-20.0, min(30.0, float(row.get("momentum_5") or 0.0)))
        volume_ratio = max(0.0, min(4.0, float(row.get("volume_ratio") or 0.0)))
        model_signal = max(0.0, min(100.0, float(row.get("model_signal_strength") or 0.0)))
        pattern_bonus = min(len(row.get("matched_patterns") or []), 3) * 4
        tradingview_bonus = self._tradingview_alignment_score(row.get("tradingview_ratings") or {})

        score = 0.0
        score += trend_score * 0.45
        score += max(momentum_5, 0.0) * 1.2
        score += volume_ratio * 8.0
        score += model_signal * 0.12
        score += pattern_bonus
        score += tradingview_bonus * 2.0

        if board_key == "leaders":
            score += max(float(row.get("momentum_20") or 0.0), 0.0) * 0.6
        elif board_key == "squeeze":
            breakout_distance = abs(float(row.get("distance_to_breakout_pct") or 0.0))
            score += max(0.0, 10.0 - breakout_distance)
        elif board_key == "three_white_soldiers":
            score += max(momentum_5, 0.0) * 0.8
        elif board_key == "volume_breakout":
            score += volume_ratio * 6.0
            score += max(0.0, 8.0 - abs(float(row.get("distance_to_breakout_pct") or 0.0)))

        if mode == "premarket":
            score += volume_ratio * 4.0
            score += max(momentum_5, 0.0) * 0.5
        elif mode == "postmarket":
            score += max(float(row.get("momentum_20") or 0.0), 0.0) * 0.9
            score += model_signal * 0.08
        else:
            score += pattern_bonus * 0.5

        return int(round(score))

    def _market_snapshot_score_breakdown(self, board_key: str, row: dict, mode: str = "monitor") -> list[str]:
        parts: list[str] = []
        parts.append(f"mode {mode}")
        if row.get("trend_score") is not None:
            parts.append(f"trend {int(float(row.get('trend_score') or 0))}")
        if row.get("volume_ratio") is not None:
            parts.append(f"volume {float(row.get('volume_ratio') or 0):.1f}x")
        if row.get("momentum_5") is not None:
            parts.append(f"5D {float(row.get('momentum_5') or 0):.1f}%")
        patterns = row.get("matched_patterns") or []
        if patterns:
            parts.append(" / ".join(patterns[:2]))
        ratings = row.get("tradingview_ratings") or {}
        if ratings:
            parts.append(f"TV {self._tradingview_alignment_score(ratings)}")
        if board_key == "squeeze" and row.get("distance_to_breakout_pct") is not None:
            parts.append(f"breakout {float(row.get('distance_to_breakout_pct') or 0):.1f}%")
        return parts[:5]

    def _screen_technical_patterns(
        self,
        *,
        template_key: str,
        required_patterns: list[str],
        universe: str,
        market: str,
        min_trend_score: int,
        action_filter: str,
        min_volume_ratio: float,
        recent_snapshot_runs: int,
        min_snapshot_hits: int,
    ) -> list[dict]:
        tickers = self._load_universe(universe=universe, market=market)
        cached_snapshot_map = {}
        results: list[dict] = []
        with SessionLocal() as db:
            model_repo = PredictionRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            trade_plan_repo = PredictionTradePlanRepository(db)
            technical_snapshot_repo = TechnicalSnapshotRepository(db)
            if universe == "full_market" and market == "CN":
                cached_snapshot_map = {
                    item["ticker"]: item
                    for item in technical_snapshot_repo.list_latest_for_market(market=market, tickers=tickers)
                }
                tickers = self._rank_cn_snapshot_candidates(
                    tickers,
                    cached_snapshot_map,
                    limit=80,
                    required_patterns=required_patterns,
                )
            model_context_cache: dict[str, dict] = {}

            def _model_context_for(ticker: str) -> dict:
                if ticker not in model_context_cache:
                    model_context_cache[ticker] = self._build_model_highlights(
                        model_repo.get_latest_model_output_for_ticker(ticker),
                        explanation_repo.get_latest_for_ticker(ticker),
                        trade_plan_repo.get_latest_for_ticker(ticker),
                    )
                return model_context_cache[ticker]

            for ticker in tickers:
                cached_snapshot = cached_snapshot_map.get(ticker)
                snapshot = self._snapshot_from_cache(cached_snapshot) if cached_snapshot is not None else self.technical_patterns.evaluate_ticker(ticker)
                if snapshot is None:
                    continue
                if required_patterns and not self._matches_required_patterns(snapshot, required_patterns):
                    continue
                insight = self._get_cached_insight(ticker, lang="en")
                if insight:
                    if insight["trend_score"] < min_trend_score:
                        continue
                    if action_filter != "ALL" and _normalize_action_value(insight["action_label"]) != _normalize_action_value(action_filter):
                        continue
                    if (insight.get("volume_ratio") or 0.0) < min_volume_ratio:
                        continue
                    row = self._build_result_from_insight(insight, _model_context_for(ticker))
                else:
                    row = self._build_result_from_fallback_pattern(snapshot, _model_context_for(ticker))
                row["matched_patterns"] = list(snapshot.matched_patterns or [])
                row["selection_reason"] = self._build_pattern_reason(template_key, row["matched_patterns"], row)
                results.append(row)

        results = self._apply_snapshot_persistence_filter(
            results,
            recent_snapshot_runs=recent_snapshot_runs,
            min_snapshot_hits=min_snapshot_hits,
        )
        results.sort(
            key=lambda item: (
                -(item.get("model_signal_strength") or 0),
                -(item.get("trend_score") or 0),
                -(item.get("volume_ratio") or 0),
                item.get("ticker", ""),
            )
        )
        return results

    def _snapshot_from_cache(self, payload: dict | None):
        if not payload:
            return None
        return type("CachedTechnicalSnapshot", (), payload)()

    def _rank_cn_snapshot_candidates(
        self,
        tickers: list[str],
        cached_snapshot_map: dict[str, dict],
        *,
        limit: int = 600,
        required_patterns: list[str] | None = None,
    ) -> list[str]:
        if not cached_snapshot_map:
            return tickers[:limit]
        ranked: list[tuple[float, str]] = []
        for ticker in tickers:
            snapshot = cached_snapshot_map.get(ticker)
            if not snapshot:
                continue
            proxy = self._snapshot_from_cache(snapshot)
            if required_patterns and not self._matches_required_patterns(proxy, required_patterns):
                continue
            score = 0.0
            score += 4.0 if snapshot.get("volume_breakout") else 0.0
            score += 3.0 if snapshot.get("bullish_ma_stack") else 0.0
            score += 2.0 if snapshot.get("ma_cluster") else 0.0
            score += 1.5 if snapshot.get("macd_underwater_cross") else 0.0
            score += min(len(snapshot.get("matched_patterns") or []), 4) * 0.6
            ranked.append((score, ticker))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = [ticker for _, ticker in ranked[:limit]]
        if required_patterns:
            return selected
        if len(selected) < min(limit, len(tickers)):
            seen = set(selected)
            selected.extend([ticker for ticker in tickers if ticker not in seen][: max(0, limit - len(selected))])
        return selected[:limit]

    def _matches_required_patterns(self, snapshot, required_patterns: list[str]) -> bool:
        matched_patterns = {str(item).strip() for item in (getattr(snapshot, "matched_patterns", None) or []) if str(item).strip()}
        for pattern in required_patterns:
            if getattr(snapshot, pattern, False):
                continue
            label = PATTERN_MATCH_LABELS.get(pattern)
            if label and label in matched_patterns:
                continue
            return False
        return True

    def _screen_technical(
        self,
        *,
        universe: str,
        market: str,
        min_trend_score: int,
        action_filter: str,
        min_volume_ratio: float,
        recent_snapshot_runs: int,
        min_snapshot_hits: int,
    ) -> list[dict]:
        tickers = self._load_universe(universe=universe, market=market)
        results: list[dict] = []
        with SessionLocal() as db:
            model_repo = PredictionRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            trade_plan_repo = PredictionTradePlanRepository(db)
            if universe == "full_market" and market == "CN":
                cached_snapshot_map = {
                    item["ticker"]: item
                    for item in TechnicalSnapshotRepository(db).list_latest_for_market(market=market, tickers=tickers)
                }
                tickers = self._rank_cn_snapshot_candidates(tickers, cached_snapshot_map, limit=120)
            model_context_cache: dict[str, dict] = {}

            def _model_context_for(ticker: str) -> dict:
                if ticker not in model_context_cache:
                    model_context_cache[ticker] = self._build_model_highlights(
                        model_repo.get_latest_model_output_for_ticker(ticker),
                        explanation_repo.get_latest_for_ticker(ticker),
                        trade_plan_repo.get_latest_for_ticker(ticker),
                    )
                return model_context_cache[ticker]

            for ticker in tickers:
                insight = self._get_cached_insight(ticker, lang="en")
                if not insight:
                    continue
                if insight["trend_score"] < min_trend_score:
                    continue
                if action_filter != "ALL" and _normalize_action_value(insight["action_label"]) != _normalize_action_value(action_filter):
                    continue
                volume_ratio = insight.get("volume_ratio") or 0.0
                if volume_ratio < min_volume_ratio:
                    continue
                results.append(self._build_result_from_insight(insight, _model_context_for(ticker)))

        results = self._apply_snapshot_persistence_filter(
            results,
            recent_snapshot_runs=recent_snapshot_runs,
            min_snapshot_hits=min_snapshot_hits,
        )
        results.sort(
            key=lambda item: (-(item["trend_score"] or 0), -(item["volume_ratio"] or 0), item["ticker"])
        )
        return results

    def _screen_tradingview_alignment(
        self,
        *,
        universe: str,
        market: str,
        min_trend_score: int,
        min_volume_ratio: float,
    ) -> list[dict]:
        candidate_tickers = self._load_universe(universe=universe, market=market)
        if universe == "full_market" and len(candidate_tickers) > 120:
            candidate_rows = self._screen_technical(
                universe=universe,
                market=market,
                min_trend_score=min_trend_score,
                action_filter="ALL",
                min_volume_ratio=min_volume_ratio,
                recent_snapshot_runs=0,
                min_snapshot_hits=0,
            )
            candidate_tickers = [row["ticker"] for row in candidate_rows[:120]]

        symbol_meta = self._load_symbol_meta(candidate_tickers)
        results: list[dict] = []
        for ticker in candidate_tickers:
            insight = self._get_cached_insight(ticker, lang="en")
            if not insight:
                continue
            if (insight.get("trend_score") or 0) < min_trend_score:
                continue
            if (insight.get("volume_ratio") or 0.0) < min_volume_ratio:
                continue

            meta = symbol_meta.get(ticker, {})
            ratings = self._load_tradingview_ratings(
                ticker=ticker,
                market=meta.get("market") or self._infer_market(ticker),
                exchange=meta.get("exchange"),
            )
            if not ratings or not self._is_bullish_multi_timeframe(ratings):
                continue

            row = self._build_result_from_insight(insight)
            row["tradingview_ratings"] = ratings
            row["selection_reason"] = self._build_tradingview_alignment_reason(ratings, row)
            results.append(row)

        results.sort(
            key=lambda item: (
                -self._tradingview_alignment_score(item.get("tradingview_ratings") or {}),
                -(item.get("trend_score") or 0),
                -(item.get("volume_ratio") or 0),
                item.get("ticker", ""),
            )
        )
        return results

    def _screen_next_tesla_swing(
        self,
        *,
        universe: str,
        market: str,
        min_trend_score: int,
        min_volume_ratio: float,
        recent_snapshot_runs: int,
        min_snapshot_hits: int,
    ) -> list[dict]:
        tickers = self._load_universe(universe=universe, market=market)
        results: list[dict] = []
        with SessionLocal() as db:
            technical_snapshot_repo = TechnicalSnapshotRepository(db)
            cached_snapshot_map = {
                item["ticker"]: item
                for item in technical_snapshot_repo.list_latest_for_market(
                    market=market if market != "ALL" else None,
                    tickers=tickers,
                )
            }
            if universe == "full_market" and market == "CN":
                tickers = self._rank_cn_snapshot_candidates(tickers, cached_snapshot_map, limit=90)
            model_repo = PredictionRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            trade_plan_repo = PredictionTradePlanRepository(db)
            model_context_cache: dict[str, dict] = {}

            def _model_context_for(ticker: str) -> dict:
                if ticker not in model_context_cache:
                    model_context_cache[ticker] = self._build_model_highlights(
                        model_repo.get_latest_model_output_for_ticker(ticker),
                        explanation_repo.get_latest_for_ticker(ticker),
                        trade_plan_repo.get_latest_for_ticker(ticker),
                    )
                return model_context_cache[ticker]

            min_trend = max(int(min_trend_score or 0), 68)
            min_volume = max(float(min_volume_ratio or 0.0), 0.8)
            for ticker in tickers:
                insight = self._get_cached_insight(ticker, limit=260, lang="en")
                if not insight:
                    continue
                setup_context = self._next_tesla_context(insight)
                if not self._matches_next_tesla_setup(
                    insight,
                    setup_context=setup_context,
                    min_trend_score=min_trend,
                    min_volume_ratio=min_volume,
                ):
                    continue
                snapshot = cached_snapshot_map.get(ticker)
                if snapshot is None:
                    evaluated = self.technical_patterns.evaluate_ticker(ticker)
                    snapshot = self._snapshot_from_cache(asdict(evaluated)) if evaluated is not None else None
                row = self._build_result_from_insight(insight, _model_context_for(ticker))
                row["matched_patterns"] = list(getattr(snapshot, "matched_patterns", None) or [])
                row["setup_bucket"] = setup_context.get("setup_bucket")
                row["distance_to_52w_high_pct"] = setup_context.get("distance_to_52w_high_pct")
                row["pullback_depth_pct"] = setup_context.get("pullback_depth_pct")
                row["selection_reason"] = self._build_next_tesla_reason(insight, setup_context, row["matched_patterns"], row)
                row["template_score"] = self._next_tesla_score(insight, setup_context, row["matched_patterns"])
                results.append(row)

        results = self._apply_snapshot_persistence_filter(
            results,
            recent_snapshot_runs=recent_snapshot_runs,
            min_snapshot_hits=min_snapshot_hits,
        )
        results.sort(
            key=lambda item: (
                -(item.get("template_score") or 0),
                -(item.get("trend_score") or 0),
                -(item.get("model_signal_strength") or 0),
                item.get("ticker", ""),
            )
        )
        return results

    def _screen_fundamental_template(
        self,
        *,
        template_key: str,
        universe: str,
        market: str,
        min_trend_score: int,
        action_filter: str,
        min_volume_ratio: float,
        min_listing_days: int,
        pe_min: float,
        pe_max: float,
        min_roe_avg_3y: float,
        min_net_profit_yoy: float,
        min_revenue_yoy: float,
        max_debt_to_assets: float,
        min_dividend_yield: float,
        exclude_bottom_market_cap_pct: float,
        recent_snapshot_runs: int,
        min_snapshot_hits: int,
    ) -> list[dict]:
        tickers = self._load_universe(universe=universe, market=market)
        with SessionLocal() as db:
            repo = FundamentalSnapshotRepository(db)
            fundamentals = repo.list_latest_for_market(market, tickers=tickers)
            model_repo = PredictionRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            trade_plan_repo = PredictionTradePlanRepository(db)
            model_context_by_ticker = {
                ticker: self._build_model_highlights(
                    model_repo.get_latest_model_output_for_ticker(ticker),
                    explanation_repo.get_latest_for_ticker(ticker),
                    trade_plan_repo.get_latest_for_ticker(ticker),
                )
                for ticker in tickers
            }

        if not fundamentals:
            return []

        threshold = self._compute_market_cap_threshold(fundamentals, exclude_bottom_market_cap_pct)
        results: list[dict] = []
        for item in fundamentals:
            if not self._passes_fundamental_template_rules(
                template_key=template_key,
                item=item,
                min_listing_days=min_listing_days,
                pe_min=pe_min,
                pe_max=pe_max,
                min_roe_avg_3y=min_roe_avg_3y,
                min_net_profit_yoy=min_net_profit_yoy,
                min_revenue_yoy=min_revenue_yoy,
                max_debt_to_assets=max_debt_to_assets,
                min_dividend_yield=min_dividend_yield,
                market_cap_threshold=threshold,
            ):
                continue

            insight = self._get_cached_insight(item["ticker"], lang="en")
            model_context = model_context_by_ticker.get(item["ticker"])
            base = (
                self._build_result_from_insight(insight, model_context)
                if insight
                else self._build_result_from_fundamental(item, model_context)
            )
            if (base.get("trend_score") or 0) < min_trend_score and insight:
                continue
            if action_filter != "ALL" and insight and _normalize_action_value(base.get("action_label")) != _normalize_action_value(action_filter):
                continue
            if (base.get("volume_ratio") or 0.0) < min_volume_ratio and insight:
                continue
            base.update(
                {
                    "report_date": item.get("report_date"),
                    "listing_date": item.get("listing_date"),
                    "pe_ttm": item.get("pe_ttm"),
                    "dividend_yield": item.get("dividend_yield"),
                    "market_cap": item.get("market_cap"),
                    "roe_avg_3y": item.get("roe_avg_3y"),
                    "net_profit_yoy": item.get("net_profit_yoy"),
                    "revenue_yoy": item.get("revenue_yoy"),
                    "debt_to_assets": item.get("debt_to_assets"),
                    "passes_template": True,
                    "selection_reason": self._build_selection_reason(template_key, item, base),
                }
            )
            results.append(base)

        results = self._apply_snapshot_persistence_filter(
            results,
            recent_snapshot_runs=recent_snapshot_runs,
            min_snapshot_hits=min_snapshot_hits,
        )
        results.sort(
            key=lambda row: (
                -(row.get("dividend_yield") or 0)
                if template_key in {"cn_low_valuation_high_dividend", "global_income_quality"}
                else -(row.get("roe_avg_3y") or 0),
                -(row.get("net_profit_yoy") or 0),
                row["ticker"],
            )
        )
        return results

    def _matches_next_tesla_setup(
        self,
        insight: dict,
        *,
        setup_context: dict,
        min_trend_score: int,
        min_volume_ratio: float,
    ) -> bool:
        trend_score = float(insight.get("trend_score") or 0.0)
        latest_close = float(insight.get("latest_close") or 0.0)
        ma20 = insight.get("ma20")
        ma60 = insight.get("ma60")
        volume_ratio = float(insight.get("volume_ratio") or 0.0)
        momentum_20 = float(insight.get("momentum_20") or 0.0)
        momentum_5 = float(insight.get("momentum_5") or 0.0)
        breakout_distance = float(insight.get("distance_to_breakout_pct") or 0.0)
        action_label = str(insight.get("action_label") or "").strip().lower()
        setup_label = str(insight.get("setup_label") or "").strip().lower()
        distance_to_52w_high = float(setup_context.get("distance_to_52w_high_pct") or 999.0)
        pullback_depth = float(setup_context.get("pullback_depth_pct") or 0.0)

        if trend_score < min_trend_score:
            return False
        if latest_close <= 0 or ma20 is None or ma60 is None:
            return False
        if not (latest_close > float(ma20) > float(ma60)):
            return False
        if volume_ratio < min_volume_ratio:
            return False
        if momentum_20 < 8.0:
            return False
        if momentum_5 < -6.0:
            return False
        if distance_to_52w_high > 15.0:
            return False
        if breakout_distance < -3.0 or breakout_distance > 8.0:
            return False
        if action_label not in {"buy_the_dip", "wait_for_breakout"}:
            return False
        if setup_label not in {"pullback_buy", "breakout_watch"}:
            return False
        if action_label == "buy_the_dip":
            if latest_close > float(ma20) * 1.05:
                return False
            if pullback_depth > 15.0:
                return False
        if action_label == "wait_for_breakout" and breakout_distance > 6.0:
            return False
        if setup_context.get("setup_bucket") == "failed_reclaim":
            return False
        return True

    def _next_tesla_context(self, insight: dict) -> dict:
        history = insight.get("history") or []
        highs = [float(row.get("high") or row.get("close") or 0.0) for row in history[-252:] if (row.get("high") or row.get("close"))]
        lows = [float(row.get("low") or row.get("close") or 0.0) for row in history[-20:] if (row.get("low") or row.get("close"))]
        latest_close = float(insight.get("latest_close") or 0.0)
        ma20 = float(insight.get("ma20") or 0.0)
        breakout_distance = float(insight.get("distance_to_breakout_pct") or 0.0)
        fifty_two_week_high = max(highs) if highs else latest_close
        distance_to_52w_high_pct = round(((fifty_two_week_high / latest_close) - 1.0) * 100.0, 2) if latest_close > 0 else None
        recent_swing_high = max(highs[-30:]) if len(highs) >= 5 else fifty_two_week_high
        pullback_depth_pct = round(((recent_swing_high - latest_close) / recent_swing_high) * 100.0, 2) if recent_swing_high and latest_close > 0 else None
        higher_low = self._is_first_higher_low(history)

        if breakout_distance <= 3.5:
            setup_bucket = "breakout_ready"
        elif latest_close >= ma20 * 0.985 and higher_low:
            setup_bucket = "pullback_reentry"
        elif latest_close >= ma20 * 0.97:
            setup_bucket = "support_hold"
        else:
            setup_bucket = "failed_reclaim"
        return {
            "distance_to_52w_high_pct": distance_to_52w_high_pct,
            "pullback_depth_pct": pullback_depth_pct,
            "setup_bucket": setup_bucket,
            "higher_low": higher_low,
        }

    def _is_first_higher_low(self, history: list[dict]) -> bool:
        recent = history[-15:] if len(history) >= 15 else history
        if len(recent) < 8:
            return False
        lows = [float(row.get("low") or row.get("close") or 0.0) for row in recent]
        earlier = lows[: len(lows) // 2]
        later = lows[len(lows) // 2 :]
        if not earlier or not later:
            return False
        return min(later) > min(earlier)

    def _apply_snapshot_persistence_filter(
        self,
        results: list[dict],
        *,
        recent_snapshot_runs: int,
        min_snapshot_hits: int,
    ) -> list[dict]:
        for row in results:
            row["snapshot_hits"] = 0
            row["snapshot_runs"] = recent_snapshot_runs
        if recent_snapshot_runs <= 0 or min_snapshot_hits <= 0 or not results:
            return results

        with SessionLocal() as db:
            snapshots = PredictionRepository(db).list_recent_prediction_snapshots(
                top_n=10,
                limit_runs=recent_snapshot_runs,
            )
        hit_counts: dict[str, int] = {}
        for snapshot in snapshots:
            for item in snapshot["items"]:
                ticker = item["ticker"]
                hit_counts[ticker] = hit_counts.get(ticker, 0) + 1

        filtered: list[dict] = []
        for row in results:
            hits = hit_counts.get(row["ticker"], 0)
            row["snapshot_hits"] = hits
            if hits >= min_snapshot_hits:
                filtered.append(row)
        return filtered

    def _sort_results(self, results: list[dict], *, sort_by: str, sort_order: str) -> list[dict]:
        reverse = sort_order != "asc"
        if sort_by == "default":
            return sorted(results, key=self._default_sort_key, reverse=False)

        numeric_fields = {
            "trend_score",
            "latest_close",
            "momentum_5",
            "momentum_20",
            "volume_ratio",
            "pe_ttm",
            "roe_avg_3y",
            "net_profit_yoy",
            "revenue_yoy",
            "dividend_yield",
            "debt_to_assets",
            "snapshot_hits",
            "model_signal_strength",
        }
        if sort_by in numeric_fields:
            return sorted(
                results,
                key=lambda row: (self._sortable_number(row.get(sort_by)), row.get("ticker", "")),
                reverse=reverse,
            )
        return sorted(results, key=lambda row: str(row.get(sort_by, "")).lower(), reverse=reverse)

    def _apply_model_signal_filter(
        self,
        results: list[dict],
        *,
        model_signal_filter: str,
        min_model_signal_strength: float,
    ) -> list[dict]:
        normalized_filter = SIGNAL_FILTER_MAP.get((model_signal_filter or "ALL").upper(), None)
        filtered: list[dict] = []
        for row in results:
            signal_label = (row.get("model_signal_label") or "").strip().lower()
            if normalized_filter and signal_label != normalized_filter:
                continue
            if (row.get("model_signal_strength") or 0.0) < min_model_signal_strength:
                continue
            filtered.append(row)
        return filtered

    def _apply_execution_tag_filter(
        self,
        results: list[dict],
        *,
        execution_tag_filter: str,
        exclude_execution_tag_filter: str,
    ) -> list[dict]:
        filtered: list[dict] = []
        for row in results:
            tags = row.get("model_execution_tags") or []
            if not _matches_execution_tag_filter(tags, execution_tag_filter):
                continue
            if not _excludes_execution_tag_filter(tags, exclude_execution_tag_filter):
                continue
            filtered.append(row)
        return filtered

    def _default_sort_key(self, row: dict) -> tuple:
        if row.get("dividend_yield") is not None:
            primary = -(row.get("dividend_yield") or 0)
        else:
            primary = -(row.get("roe_avg_3y") or row.get("trend_score") or 0)
        secondary = -(row.get("net_profit_yoy") or row.get("volume_ratio") or 0)
        market_rank = MARKET_SORT_ORDER.get(str(row.get("market") or "").upper(), 9)
        return (market_rank, primary, secondary, row.get("ticker", ""))

    def _sortable_number(self, value) -> float:
        if value is None:
            return float("-inf")
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    def _load_universe(self, *, universe: str, market: str) -> list[str]:
        market_value = market.upper()
        with SessionLocal() as db:
            if universe == "full_market":
                symbol_repo = SymbolRepository(db)
                tickers = [
                    symbol.ticker
                    for symbol in symbol_repo.list_symbols()
                    if market_value == "ALL" or (symbol.market or "").upper() == market_value
                ]
                if market_value == "CN" and not tickers:
                    tickers = self._hydrate_cn_symbol_universe(symbol_repo)
            elif universe == "synced":
                states = PriceSyncStateRepository(db).list_states_with_symbols()
                tickers = [item["ticker"] for item in states if item["status"] == "success"]
            else:
                watchlist_repo = WatchlistRepository(db)
                watchlist = watchlist_repo.get_or_create_default()
                items = watchlist_repo.list_items(watchlist.id)
                tickers = [item["ticker"] for item in items]

        if market_value == "ALL":
            return tickers
        return [ticker for ticker in tickers if self._infer_market(ticker) == market_value]

    def _hydrate_cn_symbol_universe(self, symbol_repo: SymbolRepository) -> list[str]:
        rows = TushareClient().fetch_cn_symbol_universe()
        tickers: list[str] = []
        for row in rows:
            ticker = str(row.get("ticker") or "").upper()
            if not ticker:
                continue
            symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=ticker,
                    name=row.get("name"),
                    market="CN",
                    exchange=row.get("exchange"),
                )
            )
            tickers.append(ticker)
        return tickers

    def _passes_fundamental_template_rules(
        self,
        *,
        template_key: str,
        item: dict,
        min_listing_days: int,
        pe_min: float,
        pe_max: float,
        min_roe_avg_3y: float,
        min_net_profit_yoy: float,
        min_revenue_yoy: float,
        max_debt_to_assets: float,
        min_dividend_yield: float,
        market_cap_threshold: float | None,
    ) -> bool:
        listing_days = self._listing_days(item.get("listing_date"))
        if listing_days is None or listing_days <= min_listing_days:
            return False
        pe_ttm = item.get("pe_ttm")
        if pe_ttm is None or pe_ttm <= pe_min or pe_ttm >= pe_max:
            return False
        if (item.get("roe_avg_3y") or 0) <= min_roe_avg_3y:
            return False
        if (item.get("net_profit_yoy") or 0) <= min_net_profit_yoy:
            return False
        if template_key in {"cn_high_roe_steady_growth", "global_growth_value"}:
            if (item.get("revenue_yoy") or 0) < min_revenue_yoy:
                return False
            debt_to_assets = item.get("debt_to_assets")
            if debt_to_assets is not None and debt_to_assets > max_debt_to_assets:
                return False
        if template_key in {"cn_low_valuation_high_dividend", "global_income_quality"}:
            if (item.get("dividend_yield") or 0) < min_dividend_yield:
                return False
            debt_to_assets = item.get("debt_to_assets")
            if debt_to_assets is not None and debt_to_assets > max_debt_to_assets:
                return False
        if market_cap_threshold is not None and (item.get("market_cap") or 0) <= market_cap_threshold:
            return False
        return True

    def _compute_market_cap_threshold(self, items: list[dict], bottom_pct: float) -> float | None:
        caps = sorted(cap for cap in (item.get("market_cap") for item in items) if cap is not None)
        if not caps:
            return None
        if bottom_pct <= 0:
            return None
        index = int(len(caps) * (bottom_pct / 100.0))
        index = max(0, min(index, len(caps) - 1))
        return caps[index]

    def _listing_days(self, listing_date: str | None) -> int | None:
        if not listing_date:
            return None
        try:
            listed = datetime.strptime(listing_date, "%Y-%m-%d").date()
        except ValueError:
            return None
        return (date.today() - listed).days

    def _build_result_from_insight(self, insight: dict, model_context: dict | None = None) -> dict:
        resolved_name = insight.get("company_name") or self._resolve_symbol_name(insight["ticker"]) or insight["ticker"]
        return {
            "ticker": insight["ticker"],
            "name": resolved_name,
            "market": self._infer_market(insight["ticker"]),
            "as_of_date": insight["as_of_date"],
            "trend_score": insight["trend_score"],
            "action_label": insight["action_label"],
            "action_summary": insight["action_summary"],
            "latest_close": insight["latest_close"],
            "momentum_5": insight.get("momentum_5"),
            "momentum_20": insight.get("momentum_20"),
            "volume_ratio": insight.get("volume_ratio"),
            "distance_to_breakout_pct": insight.get("distance_to_breakout_pct"),
            "snapshot_hits": 0,
            "snapshot_runs": 0,
            "model_summary": (model_context or {}).get("summary"),
            "model_highlights": (model_context or {}).get("highlights", []),
            "model_state": (model_context or {}).get("state"),
            "model_confidence": (model_context or {}).get("confidence"),
            "model_signal_label": (model_context or {}).get("signal_label"),
            "model_signal_strength": (model_context or {}).get("signal_strength"),
            "model_conviction_bucket": (model_context or {}).get("conviction_bucket"),
            "model_position_size_hint": (model_context or {}).get("position_size_hint"),
            "model_entry_style": (model_context or {}).get("entry_style"),
            "model_execution_tags": (model_context or {}).get("execution_tags", []),
            "model_percentile": (model_context or {}).get("percentile"),
            "model_horizon_days": (model_context or {}).get("target_horizon_days"),
            "model_reward_risk_ratio": (model_context or {}).get("model_reward_risk_ratio"),
            "model_expected_drawdown_20d": (model_context or {}).get("expected_drawdown_20d"),
            "matched_patterns": [],
            "selection_reason": self._build_technical_reason(insight, model_context),
        }

    def _build_result_from_fundamental(self, item: dict, model_context: dict | None = None) -> dict:
        return {
            "ticker": item["ticker"],
            "name": item.get("name") or item["ticker"],
            "market": self._infer_market(item["ticker"]),
            "as_of_date": item.get("report_date"),
            "trend_score": None,
            "action_label": "fundamental_pass",
            "action_summary": "Passed the selected fundamental template.",
            "latest_close": None,
            "momentum_5": None,
            "momentum_20": None,
            "volume_ratio": None,
            "distance_to_breakout_pct": None,
            "snapshot_hits": 0,
            "snapshot_runs": 0,
            "model_summary": (model_context or {}).get("summary"),
            "model_highlights": (model_context or {}).get("highlights", []),
            "model_state": (model_context or {}).get("state"),
            "model_confidence": (model_context or {}).get("confidence"),
            "model_signal_label": (model_context or {}).get("signal_label"),
            "model_signal_strength": (model_context or {}).get("signal_strength"),
            "model_conviction_bucket": (model_context or {}).get("conviction_bucket"),
            "model_position_size_hint": (model_context or {}).get("position_size_hint"),
            "model_entry_style": (model_context or {}).get("entry_style"),
            "model_execution_tags": (model_context or {}).get("execution_tags", []),
            "model_percentile": (model_context or {}).get("percentile"),
            "model_horizon_days": (model_context or {}).get("target_horizon_days"),
            "model_reward_risk_ratio": (model_context or {}).get("model_reward_risk_ratio"),
            "model_expected_drawdown_20d": (model_context or {}).get("expected_drawdown_20d"),
            "matched_patterns": [],
            "selection_reason": "Passed the selected fundamental template.",
        }

    def _build_result_from_fallback_pattern(self, snapshot, model_context: dict | None = None) -> dict:
        resolved_name = self._resolve_symbol_name(snapshot.ticker) or snapshot.ticker
        return {
            "ticker": snapshot.ticker,
            "name": resolved_name,
            "market": self._infer_market(snapshot.ticker),
            "as_of_date": snapshot.as_of_date,
            "trend_score": None,
            "action_label": "technical_pattern",
            "action_summary": "Matched the selected technical pattern.",
            "latest_close": None,
            "momentum_5": None,
            "momentum_20": None,
            "volume_ratio": None,
            "distance_to_breakout_pct": None,
            "snapshot_hits": 0,
            "snapshot_runs": 0,
            "model_summary": (model_context or {}).get("summary"),
            "model_highlights": (model_context or {}).get("highlights", []),
            "model_state": (model_context or {}).get("state"),
            "model_confidence": (model_context or {}).get("confidence"),
            "model_signal_label": (model_context or {}).get("signal_label"),
            "model_signal_strength": (model_context or {}).get("signal_strength"),
            "model_conviction_bucket": (model_context or {}).get("conviction_bucket"),
            "model_position_size_hint": (model_context or {}).get("position_size_hint"),
            "model_entry_style": (model_context or {}).get("entry_style"),
            "model_execution_tags": (model_context or {}).get("execution_tags", []),
            "model_percentile": (model_context or {}).get("percentile"),
            "model_horizon_days": (model_context or {}).get("target_horizon_days"),
            "model_reward_risk_ratio": (model_context or {}).get("model_reward_risk_ratio"),
            "model_expected_drawdown_20d": (model_context or {}).get("expected_drawdown_20d"),
            "matched_patterns": list(snapshot.matched_patterns or []),
            "selection_reason": ", ".join(snapshot.matched_patterns or []) or "Matched the selected technical pattern.",
        }

    def _build_technical_reason(self, insight: dict, model_context: dict | None = None) -> str:
        reasons: list[str] = []
        trend_score = insight.get("trend_score")
        if trend_score is not None:
            reasons.append(f"trend score {trend_score}")
        action_label = insight.get("action_label")
        if action_label:
            reasons.append(action_label.replace("_", " "))
        volume_ratio = insight.get("volume_ratio")
        if volume_ratio is not None:
            reasons.append(f"volume {volume_ratio}x")
        highlights = (model_context or {}).get("highlights") or []
        if highlights:
            reasons.append("model: " + ", ".join(highlights[:2]))
        return ", ".join(reasons) if reasons else "Matched the active technical rules."

    def _build_selection_reason(self, template_key: str, item: dict, base: dict) -> str:
        if template_key == "global_growth_value":
            return (
                f"PE {self._fmt(item.get('pe_ttm'))}, "
                f"ROE {self._fmt(item.get('roe_avg_3y'))}%, "
                f"revenue YoY {self._fmt(item.get('revenue_yoy'))}%"
            )
        if template_key == "global_income_quality":
            return (
                f"dividend {self._fmt(item.get('dividend_yield'))}%, "
                f"PE {self._fmt(item.get('pe_ttm'))}, "
                f"debt/assets {self._fmt(item.get('debt_to_assets'))}%"
            )
        if template_key == "cn_growth_value":
            return (
                f"PE {self._fmt(item.get('pe_ttm'))}, "
                f"ROE 3Y {self._fmt(item.get('roe_avg_3y'))}%, "
                f"profit YoY {self._fmt(item.get('net_profit_yoy'))}%"
            )
        if template_key == "cn_high_roe_steady_growth":
            return (
                f"ROE 3Y {self._fmt(item.get('roe_avg_3y'))}%, "
                f"revenue YoY {self._fmt(item.get('revenue_yoy'))}%, "
                f"debt/assets {self._fmt(item.get('debt_to_assets'))}%"
            )
        if template_key == "cn_low_valuation_high_dividend":
            return (
                f"dividend {self._fmt(item.get('dividend_yield'))}%, "
                f"PE {self._fmt(item.get('pe_ttm'))}, "
                f"ROE 3Y {self._fmt(item.get('roe_avg_3y'))}%"
            )
        return base.get("selection_reason") or "Matched the active template."

    def _build_next_tesla_reason(self, insight: dict, setup_context: dict, matched_patterns: list[str], row: dict) -> str:
        parts: list[str] = []
        action_label = str(insight.get("action_label") or "")
        if action_label:
            parts.append(action_label.replace("_", " "))
        setup_bucket = setup_context.get("setup_bucket")
        if setup_bucket:
            parts.append(setup_bucket.replace("_", " "))
        parts.append(f"trend {int(float(insight.get('trend_score') or 0))}")
        if insight.get("momentum_20") is not None:
            parts.append(f"20D {float(insight.get('momentum_20') or 0):.1f}%")
        if insight.get("volume_ratio") is not None:
            parts.append(f"volume {float(insight.get('volume_ratio') or 0):.1f}x")
        if setup_context.get("distance_to_52w_high_pct") is not None:
            parts.append(f"52W {float(setup_context.get('distance_to_52w_high_pct') or 0):.1f}%")
        if setup_context.get("pullback_depth_pct") is not None:
            parts.append(f"pullback {float(setup_context.get('pullback_depth_pct') or 0):.1f}%")
        if insight.get("distance_to_breakout_pct") is not None:
            parts.append(f"breakout {float(insight.get('distance_to_breakout_pct') or 0):.1f}%")
        if matched_patterns:
            parts.append(" / ".join(matched_patterns[:2]))
        signal_label = row.get("model_signal_label")
        if signal_label:
            parts.append(f"model {signal_label}")
        return ", ".join(parts)

    def _next_tesla_score(self, insight: dict, setup_context: dict, matched_patterns: list[str]) -> float:
        trend = float(insight.get("trend_score") or 0.0)
        momentum_20 = max(float(insight.get("momentum_20") or 0.0), 0.0)
        volume_ratio = max(float(insight.get("volume_ratio") or 0.0), 0.0)
        breakout_distance = abs(float(insight.get("distance_to_breakout_pct") or 0.0))
        distance_to_52w = abs(float(setup_context.get("distance_to_52w_high_pct") or 0.0))
        pullback_depth = abs(float(setup_context.get("pullback_depth_pct") or 0.0))
        score = trend * 0.55
        score += momentum_20 * 1.4
        score += min(volume_ratio, 3.0) * 8.0
        score += max(0.0, 8.0 - breakout_distance)
        score += max(0.0, 12.0 - distance_to_52w) * 0.8
        score += max(0.0, 12.0 - pullback_depth) * 0.6
        score += min(len(matched_patterns), 3) * 3.0
        if setup_context.get("higher_low"):
            score += 4.0
        if setup_context.get("setup_bucket") == "breakout_ready":
            score += 3.0
        return round(score, 2)

    def _build_pattern_reason(self, template_key: str, matched_patterns: list[str], row: dict) -> str:
        matched = " / ".join(matched_patterns or []) or "技术形态触发"
        signal_label = row.get("model_signal_label")
        signal_strength = row.get("model_signal_strength")
        trend = row.get("trend_score")
        extras: list[str] = [matched]
        if signal_label:
            if signal_strength is not None:
                extras.append(f"signal {signal_label} {int(float(signal_strength))}")
            else:
                extras.append(f"signal {signal_label}")
        if trend is not None:
            extras.append(f"trend {int(float(trend))}")
        return ", ".join(extras)

    def _build_tradingview_alignment_reason(self, ratings: dict[str, dict], row: dict) -> str:
        parts: list[str] = []
        for interval in ("1d", "1w", "1M"):
            payload = ratings.get(interval) or {}
            recommendation = payload.get("recommendation")
            if recommendation:
                parts.append(f"{interval} {recommendation}")
        trend = row.get("trend_score")
        if trend is not None:
            parts.append(f"trend {int(float(trend))}")
        volume_ratio = row.get("volume_ratio")
        if volume_ratio is not None:
            parts.append(f"volume {float(volume_ratio):.2f}x")
        return ", ".join(parts) if parts else "Multi-timeframe TradingView alignment."

    def _build_model_highlights(
        self,
        model_output: dict | None,
        explanations: list[dict],
        trade_plan: dict | None,
    ) -> dict:
        if not model_output:
            return {"summary": None, "highlights": []}
        enriched = enrich_model_output(dict(model_output), lang="en") or model_output
        highlights = summarize_explanations(explanations, lang="en", limit=3)
        execution_tags = list((trade_plan or {}).get("execution_tags") or [])
        summary = None
        score = enriched.get("score")
        rank_value = enriched.get("rank_value")
        universe_size = enriched.get("universe_size")
        if score is not None:
            if rank_value is not None and universe_size:
                summary = f"model {score:.3f}, rank {int(rank_value)}/{int(universe_size)}"
            else:
                summary = f"model {score:.3f}"
        return {
            "summary": summary,
            "highlights": highlights[:3],
            "state": enriched.get("state") or build_model_state(score, lang="en"),
            "confidence": enriched.get("confidence"),
            "signal_label": enriched.get("signal_label"),
            "signal_strength": enriched.get("signal_strength"),
            "conviction_bucket": enriched.get("conviction_bucket"),
            "position_size_hint": enriched.get("position_size_hint"),
            "entry_style": enriched.get("entry_style"),
            "execution_tags": execution_tags,
            "percentile": enriched.get("percentile"),
            "target_horizon_days": enriched.get("target_horizon_days"),
            "model_reward_risk_ratio": enriched.get("model_reward_risk_ratio"),
            "expected_drawdown_20d": enriched.get("expected_drawdown_20d"),
        }

    def _fmt(self, value: float | None) -> str:
        if value is None:
            return "-"
        if float(value).is_integer():
            return str(int(value))
        return f"{value:.2f}"

    def _resolve_symbol_name(self, ticker: str) -> str | None:
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker(ticker)
        if symbol is None:
            return None
        name = (symbol.name or "").strip()
        return name or None

    def _infer_market(self, ticker: str) -> str:
        upper = ticker.upper()
        if upper.endswith(".HK"):
            return "HK"
        if upper.endswith(".SS") or upper.endswith(".SZ") or upper.endswith(".SH"):
            return "CN"
        return "US"

    def _load_symbol_meta(self, tickers: list[str]) -> dict[str, dict]:
        normalized = {ticker.upper() for ticker in tickers}
        with SessionLocal() as db:
            symbols = [symbol for symbol in SymbolRepository(db).list_symbols() if symbol.ticker in normalized]
        return {
            symbol.ticker: {
                "market": (symbol.market or "").upper() or None,
                "exchange": (symbol.exchange or "").upper() or None,
            }
            for symbol in symbols
        }

    def _load_tradingview_ratings(
        self,
        *,
        ticker: str,
        market: str | None,
        exchange: str | None,
    ) -> dict[str, dict]:
        ratings: dict[str, dict] = {}
        for interval in ("1d", "1w", "1M"):
            payload = self.tradingview.get_technical_rating(
                ticker=ticker,
                market=market,
                exchange=exchange,
                interval=interval,
            )
            if not payload or payload.get("status") != "success":
                return {}
            ratings[interval] = payload
        return ratings

    def _is_bullish_multi_timeframe(self, ratings: dict[str, dict]) -> bool:
        bullish = {"BUY", "STRONG_BUY"}
        bearish = {"SELL", "STRONG_SELL"}
        recommendations = [
            str((ratings.get(interval) or {}).get("recommendation") or "").upper()
            for interval in ("1d", "1w", "1M")
        ]
        if any(rec in bearish for rec in recommendations):
            return False
        return sum(1 for rec in recommendations if rec in bullish) >= 2 and recommendations[0] in bullish

    def _tradingview_alignment_score(self, ratings: dict[str, dict]) -> int:
        scores = {
            "STRONG_BUY": 4,
            "BUY": 3,
            "NEUTRAL": 1,
            "SELL": -2,
            "STRONG_SELL": -3,
        }
        return sum(
            scores.get(str((ratings.get(interval) or {}).get("recommendation") or "").upper(), 0)
            for interval in ("1d", "1w", "1M")
        )
