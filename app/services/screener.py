from datetime import date, datetime

from app.core.db import SessionLocal
from app.services.insight_engine import InsightEngine
from app.services.repository import (
    FundamentalSnapshotRepository,
    PredictionExplanationRepository,
    PredictionRepository,
    PriceSyncStateRepository,
    SymbolRepository,
    WatchlistRepository,
)


MODEL_TEMPLATES = {
    "technical_momentum": {
        "label": "Technical Momentum",
        "description": "Use the current insight engine to rank stocks by trend strength and volume support.",
        "market": "ALL",
        "mode": "technical",
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


class ScreenerService:
    def __init__(self) -> None:
        self.insight_engine = InsightEngine()

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
            )
        else:
            results = self._screen_technical(
                universe=universe,
                market=market,
                min_trend_score=min_trend_score,
                action_filter=action_filter,
                min_volume_ratio=min_volume_ratio,
            )
        return self._sort_results(results, sort_by=sort_by, sort_order=sort_order)[:limit]

    def _screen_technical(
        self,
        *,
        universe: str,
        market: str,
        min_trend_score: int,
        action_filter: str,
        min_volume_ratio: float,
    ) -> list[dict]:
        tickers = self._load_universe(universe=universe, market=market)
        with SessionLocal() as db:
            model_repo = PredictionRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_context_by_ticker = {
                ticker: self._build_model_highlights(
                    model_repo.get_latest_model_output_for_ticker(ticker),
                    explanation_repo.get_latest_for_ticker(ticker),
                )
                for ticker in tickers
            }
        results: list[dict] = []
        for ticker in tickers:
            insight = self.insight_engine.get_insight(ticker, lang="en")
            if not insight:
                continue
            if insight["trend_score"] < min_trend_score:
                continue
            if action_filter != "ALL" and insight["action_label"] != action_filter:
                continue
            volume_ratio = insight.get("volume_ratio") or 0.0
            if volume_ratio < min_volume_ratio:
                continue
            results.append(self._build_result_from_insight(insight, model_context_by_ticker.get(ticker)))

        results.sort(
            key=lambda item: (-(item["trend_score"] or 0), -(item["volume_ratio"] or 0), item["ticker"])
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
    ) -> list[dict]:
        tickers = self._load_universe(universe=universe, market=market)
        with SessionLocal() as db:
            repo = FundamentalSnapshotRepository(db)
            fundamentals = repo.list_latest_for_market(market, tickers=tickers)
            model_repo = PredictionRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_context_by_ticker = {
                ticker: self._build_model_highlights(
                    model_repo.get_latest_model_output_for_ticker(ticker),
                    explanation_repo.get_latest_for_ticker(ticker),
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

            insight = self.insight_engine.get_insight(item["ticker"], lang="en")
            model_context = model_context_by_ticker.get(item["ticker"])
            base = (
                self._build_result_from_insight(insight, model_context)
                if insight
                else self._build_result_from_fundamental(item, model_context)
            )
            if (base.get("trend_score") or 0) < min_trend_score and insight:
                continue
            if action_filter != "ALL" and insight and base.get("action_label") != action_filter:
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
        }
        if sort_by in numeric_fields:
            return sorted(
                results,
                key=lambda row: (self._sortable_number(row.get(sort_by)), row.get("ticker", "")),
                reverse=reverse,
            )
        return sorted(results, key=lambda row: str(row.get(sort_by, "")).lower(), reverse=reverse)

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
            if universe == "synced":
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
            "model_summary": (model_context or {}).get("summary"),
            "model_highlights": (model_context or {}).get("highlights", []),
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
            "model_summary": (model_context or {}).get("summary"),
            "model_highlights": (model_context or {}).get("highlights", []),
            "selection_reason": "Passed the selected fundamental template.",
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

    def _build_model_highlights(self, model_output: dict | None, explanations: list[dict]) -> dict:
        if not model_output:
            return {"summary": None, "highlights": []}
        highlights: list[str] = []
        for item in explanations:
            contribution = item.get("contribution")
            if contribution is None:
                continue
            feature_name = str(item.get("feature_name") or "")
            if feature_name == "recent_daily_return":
                label = "recent move"
            elif feature_name.startswith("lag_return_"):
                label = feature_name.replace("lag_return_", "lag ").replace("d", "d")
            elif feature_name == "price_vs_ma20":
                label = "price vs MA20"
            elif feature_name == "ma_alignment":
                label = "MA alignment"
            elif feature_name == "volume_ratio_20d":
                label = "volume support"
            elif feature_name.startswith("lookback_momentum_"):
                label = "lookback momentum"
            else:
                label = feature_name
            direction = "+" if contribution >= 0 else ""
            highlights.append(f"{label} {direction}{contribution:.2f}")
        summary = None
        score = model_output.get("score")
        rank_value = model_output.get("rank_value")
        universe_size = model_output.get("universe_size")
        if score is not None:
            if rank_value is not None and universe_size:
                summary = f"model {score:.3f}, rank {int(rank_value)}/{int(universe_size)}"
            else:
                summary = f"model {score:.3f}"
        return {
            "summary": summary,
            "highlights": highlights[:3],
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
