from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.services.dashboard_insights import _window_return_pct
from app.services.model_signal_summary import build_model_state, enrich_model_output
from app.services.repository import (
    ConceptSnapshotRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    SymbolRepository,
)
from app.services.runtime_cache import get_or_set
from app.services.symbol_details import SymbolDataService

def _concept_slug(name: str) -> str:
    return (
        name.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("&", "and")
        .replace("__", "-")
    )


def _concept_price_strength(symbol_data_service: SymbolDataService, tickers: list[str]) -> dict:
    five_day_values: list[float] = []
    twenty_day_values: list[float] = []
    turnover_ratio_values: list[float] = []
    advancing = 0
    declining = 0
    up_turnover = 0.0
    down_turnover = 0.0
    flat_turnover = 0.0
    for ticker in tickers:
        history = symbol_data_service.get_history(ticker, limit=25)
        move_5 = _window_return_pct(history, 5)
        move_20 = _window_return_pct(history, 20)
        if move_5 is not None:
            five_day_values.append(move_5)
            if move_5 > 0:
                advancing += 1
            elif move_5 < 0:
                declining += 1
        if move_20 is not None:
            twenty_day_values.append(move_20)
        if len(history) >= 2:
            latest = history[-1]
            prev = history[-2]
            latest_close = latest.get("close")
            latest_volume = latest.get("volume")
            prev_close = prev.get("close")
            latest_turnover = (
                max(0.0, float(latest_close) * float(latest_volume))
                if latest_close not in (None, 0) and latest_volume not in (None, 0)
                else 0.0
            )
            if latest_turnover > 0:
                prior_turnovers = [
                    max(0.0, float(row_close) * float(row_volume))
                    for row in history[:-1]
                    for row_close, row_volume in [(row.get("close"), row.get("volume"))]
                    if row_close not in (None, 0) and row_volume not in (None, 0)
                ][-20:]
                if prior_turnovers:
                    turnover_ratio_values.append(latest_turnover / max(sum(prior_turnovers) / len(prior_turnovers), 1.0))
                if prev_close not in (None, 0):
                    if float(latest_close) > float(prev_close):
                        up_turnover += latest_turnover
                    elif float(latest_close) < float(prev_close):
                        down_turnover += latest_turnover
                    else:
                        flat_turnover += latest_turnover
    breadth_base = advancing + declining
    breadth = round((advancing / breadth_base) * 100.0, 1) if breadth_base else None
    total_turnover = up_turnover + down_turnover + flat_turnover
    turnover_ratio_20d = round(sum(turnover_ratio_values) / len(turnover_ratio_values), 2) if turnover_ratio_values else None
    up_turnover_share_pct = round((up_turnover / total_turnover) * 100.0, 1) if total_turnover else None
    signed_turnover_pct = round(((up_turnover - down_turnover) / total_turnover) * 100.0, 1) if total_turnover else None
    flow_proxy_score = None
    if turnover_ratio_20d is not None or signed_turnover_pct is not None or breadth is not None:
        ratio_component = ((turnover_ratio_20d or 1.0) - 1.0) * 26.0
        signed_component = (signed_turnover_pct or 0.0) * 0.30
        breadth_component = ((breadth or 50.0) - 50.0) * 0.32
        flow_proxy_score = round(min(100.0, max(0.0, 50.0 + ratio_component + signed_component + breadth_component)), 1)
    return {
        "avg_move_5d": round(sum(five_day_values) / len(five_day_values), 2) if five_day_values else None,
        "avg_move_20d": round(sum(twenty_day_values) / len(twenty_day_values), 2) if twenty_day_values else None,
        "breadth_pct": breadth,
        "turnover_ratio_20d": turnover_ratio_20d,
        "up_turnover_share_pct": up_turnover_share_pct,
        "signed_turnover_pct": signed_turnover_pct,
        "flow_proxy_score": flow_proxy_score,
    }


def _build_market_context(db: Session, latest_signals: list[dict], *, lookback_runs: int = 5) -> dict:
    signature = [
        {
            "ticker": item.get("ticker"),
            "trade_date": item.get("trade_date"),
            "score": item.get("score"),
        }
        for item in latest_signals[:20]
    ]
    cache_key = json.dumps({"lookback_runs": lookback_runs, "signals": signature}, sort_keys=True, ensure_ascii=False)

    def _load() -> dict:
        tickers = [item["ticker"] for item in latest_signals if item.get("ticker")]
        symbol_repo = SymbolRepository(db)
        concept_repo = ConceptSnapshotRepository(db)
        signal_repo = PredictionRepository(db)
        trade_plan_repo = PredictionTradePlanRepository(db)
        symbol_data_service = SymbolDataService()

        market_counts: dict[str, int] = {}
        for ticker in tickers:
            symbol = symbol_repo.get_by_ticker(ticker)
            market = (symbol.market if symbol and symbol.market else "OTHER").upper()
            market_counts[market] = market_counts.get(market, 0) + 1

        market_distribution = [
            {"market": market, "count": count}
            for market, count in sorted(market_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

        concept_rows = concept_repo.list_latest_for_tickers(tickers)
        model_meta_cache: dict[str, dict] = {}

        def model_meta_for_ticker(ticker: str, *, score: float | None = None) -> dict:
            inner_key = ticker.upper()
            if inner_key in model_meta_cache:
                return model_meta_cache[inner_key]
            detail = signal_repo.get_latest_model_output_for_ticker(ticker)
            if detail is None:
                detail = {"ticker": ticker, "score": score}
            elif detail.get("score") is None and score is not None:
                detail["score"] = score
            enriched = enrich_model_output(detail, lang="en") or {"score": score, "state": build_model_state(score, lang="en")}
            trade_plan = trade_plan_repo.get_latest_for_ticker(ticker) or {}
            model_meta_cache[inner_key] = {
                "state": enriched.get("state") or build_model_state(score, lang="en"),
                "confidence": enriched.get("confidence"),
                "summary_text": enriched.get("summary_text"),
                "bullish_prob": enriched.get("bullish_prob"),
                "bearish_prob": enriched.get("bearish_prob"),
                "risk_score": enriched.get("risk_score"),
                "regime_label": enriched.get("regime_label"),
                "conviction_bucket": enriched.get("conviction_bucket"),
                "position_size_hint": enriched.get("position_size_hint"),
                "entry_style": enriched.get("entry_style"),
                "signal_label": enriched.get("signal_label"),
                "signal_strength": enriched.get("signal_strength"),
                "percentile": enriched.get("percentile"),
                "target_horizon_days": enriched.get("target_horizon_days"),
                "expected_drawdown_20d": enriched.get("expected_drawdown_20d"),
                "model_reward_risk_ratio": enriched.get("model_reward_risk_ratio"),
                "execution_tags": list(trade_plan.get("execution_tags") or []),
            }
            return model_meta_cache[inner_key]

        def _top_execution_tags(ticker_details: list[dict]) -> list[str]:
            counts: dict[str, int] = {}
            for detail in ticker_details:
                for tag in detail.get("execution_tags") or []:
                    normalized = str(tag).strip()
                    if not normalized:
                        continue
                    counts[normalized] = counts.get(normalized, 0) + 1
            return [tag for tag, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))][:2]

        concept_map: dict[str, dict] = {}
        score_lookup = {item["ticker"]: float(item.get("score") or 0.0) for item in latest_signals}
        for row in concept_rows:
            name = row["concept_name"]
            item = concept_map.setdefault(
                name,
                {
                    "concept_name": name,
                    "concept_code": row.get("concept_code"),
                    "hits": 0,
                    "score_total": 0.0,
                    "tickers": [],
                    "ticker_details": [],
                    "as_of_date": row.get("as_of_date"),
                },
            )
            item["hits"] += 1
            item["score_total"] += score_lookup.get(row["ticker"], 0.0)
            if row["ticker"] not in item["tickers"]:
                item["tickers"].append(row["ticker"])
                item["ticker_details"].append(
                    {
                        "ticker": row["ticker"],
                        "name": row.get("name"),
                        "score": score_lookup.get(row["ticker"], 0.0),
                        **model_meta_for_ticker(row["ticker"], score=score_lookup.get(row["ticker"], 0.0)),
                    }
                )

        top_concepts = sorted(
            (
                {
                    "concept_name": value["concept_name"],
                    "concept_code": value["concept_code"],
                    "hits": value["hits"],
                    "avg_score": round(value["score_total"] / max(value["hits"], 1), 4),
                    "tickers": value["tickers"],
                    "ticker_details": sorted(value["ticker_details"], key=lambda detail: detail["score"], reverse=True),
                    "as_of_date": value["as_of_date"],
                    "max_signal_strength": max((int(detail.get("signal_strength") or 0) for detail in value["ticker_details"]), default=0),
                    "buy_signal_count": sum(
                        1 for detail in value["ticker_details"]
                        if str(detail.get("signal_label") or "").strip().upper() == "BUY"
                    ),
                    "execution_tags": _top_execution_tags(value["ticker_details"]),
                    **_concept_price_strength(symbol_data_service, value["tickers"]),
                }
                for value in concept_map.values()
            ),
            key=lambda item: (-item["hits"], -item["avg_score"], item["concept_name"]),
        )[:12]
        top_hits = top_concepts[0]["hits"] if top_concepts else 0
        resonance_score = round(top_hits / max(len(latest_signals), 1) * 100, 1) if latest_signals else 0.0

        sector_heatmap = []
        for item in top_concepts[:8]:
            move_boost = int(max(item.get("avg_move_5d") or 0.0, 0.0) * 3)
            breadth_boost = int(((item.get("breadth_pct") or 0.0) / 100.0) * 16)
            intensity = min(100, 20 + item["hits"] * 18 + int(max(item["avg_score"], 0) * 120) + move_boost + breadth_boost)
            sector_heatmap.append(
                {
                    "label": item["concept_name"],
                    "slug": _concept_slug(item["concept_name"]),
                    "hits": item["hits"],
                    "avg_score": item["avg_score"],
                    "avg_move_5d": item.get("avg_move_5d"),
                    "avg_move_20d": item.get("avg_move_20d"),
                    "breadth_pct": item.get("breadth_pct"),
                    "max_signal_strength": item.get("max_signal_strength"),
                    "buy_signal_count": item.get("buy_signal_count"),
                    "turnover_ratio_20d": item.get("turnover_ratio_20d"),
                    "up_turnover_share_pct": item.get("up_turnover_share_pct"),
                    "signed_turnover_pct": item.get("signed_turnover_pct"),
                    "flow_proxy_score": item.get("flow_proxy_score"),
                    "execution_tags": item.get("execution_tags", []),
                    "intensity": intensity,
                }
            )

        snapshots = signal_repo.list_recent_prediction_snapshots(top_n=10, limit_runs=max(lookback_runs, 2))
        concept_snapshots: list[dict] = []
        for snapshot in snapshots:
            snapshot_tickers = [item["ticker"] for item in snapshot["items"]]
            snapshot_rows = concept_repo.list_latest_for_tickers(snapshot_tickers)
            counts: dict[str, int] = {}
            for row in snapshot_rows:
                counts[row["concept_name"]] = counts.get(row["concept_name"], 0) + 1
            concept_snapshots.append({"trade_date": snapshot["trade_date"], "counts": counts})

        tracker_rows: list[dict] = []
        for item in top_concepts:
            current_hits = item["hits"]
            previous_hits = 0
            streak = 0
            history: list[int] = []
            for snapshot in concept_snapshots:
                count = snapshot["counts"].get(item["concept_name"], 0)
                history.append(count)
            if len(history) > 1:
                previous_hits = history[1]
            for count in history:
                if count > 0:
                    streak += 1
                else:
                    break
            tracker_rows.append(
                {
                    "concept_name": item["concept_name"],
                    "hits": current_hits,
                    "previous_hits": previous_hits,
                    "delta_hits": current_hits - previous_hits,
                    "streak": streak,
                    "avg_score": item["avg_score"],
                    "avg_move_5d": item.get("avg_move_5d"),
                    "avg_move_20d": item.get("avg_move_20d"),
                    "breadth_pct": item.get("breadth_pct"),
                    "max_signal_strength": item.get("max_signal_strength"),
                    "buy_signal_count": item.get("buy_signal_count"),
                    "execution_tags": item.get("execution_tags", []),
                    "tickers": item["tickers"],
                    "ticker_details": item["ticker_details"],
                    "history": history,
                    "slug": _concept_slug(item["concept_name"]),
                }
            )
        tracker_rows.sort(key=lambda row: (-row["delta_hits"], -row["hits"], row["concept_name"]))

        latest_signal_map = {item["ticker"]: item for item in latest_signals}
        continuous_leaders: list[dict] = []
        ticker_hit_counts: dict[str, int] = {}
        ticker_score_history: dict[str, list[float]] = {}
        for snapshot in snapshots:
            for item in snapshot["items"]:
                ticker = item["ticker"]
                ticker_hit_counts[ticker] = ticker_hit_counts.get(ticker, 0) + 1
                ticker_score_history.setdefault(ticker, []).append(float(item.get("score") or 0.0))

        for ticker, hits in ticker_hit_counts.items():
            if hits <= 0:
                continue
            symbol = symbol_repo.get_by_ticker(ticker)
            latest_signal = latest_signal_map.get(ticker, {})
            continuous_leaders.append(
                {
                    "ticker": ticker,
                    "name": (symbol.name if symbol and symbol.name else ticker),
                    "market": (symbol.market if symbol and symbol.market else "OTHER").upper(),
                    "hits": hits,
                    "runs": lookback_runs,
                    "score": round(float(latest_signal.get("score") or 0.0), 4),
                    "score_history": ticker_score_history.get(ticker, []),
                    "trade_date": latest_signal.get("trade_date"),
                    **model_meta_for_ticker(ticker, score=float(latest_signal.get("score") or 0.0)),
                }
            )
        continuous_leaders.sort(key=lambda item: (-item["hits"], -item["score"], item["ticker"]))

        risk_counts: dict[str, int] = {}
        tagged_examples: list[dict] = []
        seen_tickers: set[str] = set()
        combined_details: list[dict] = []
        for concept in top_concepts:
            combined_details.extend(concept.get("ticker_details") or [])
        combined_details.extend(continuous_leaders)
        for detail in combined_details:
            ticker = str(detail.get("ticker") or "").upper()
            if not ticker or ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)
            tags = [str(tag).strip() for tag in (detail.get("execution_tags") or []) if str(tag).strip()]
            if not tags:
                continue
            for tag in tags:
                risk_counts[tag] = risk_counts.get(tag, 0) + 1
            tagged_examples.append(
                {
                    "ticker": detail.get("ticker"),
                    "tags": tags[:2],
                    "signal_strength": int(detail.get("signal_strength") or 0),
                }
            )
        tagged_examples.sort(key=lambda item: (-item["signal_strength"], item["ticker"] or ""))
        risk_overview = {
            "tagged_names": len(tagged_examples),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in sorted(risk_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
            ],
            "examples": tagged_examples[:3],
        }

        return {
            "market_distribution": market_distribution,
            "top_concepts": top_concepts,
            "sector_heatmap": sector_heatmap,
            "concept_tracker": tracker_rows[:12],
            "continuous_leaders": continuous_leaders[:10],
            "risk_overview": risk_overview,
            "resonance_score": resonance_score,
            "tracked_signal_count": len(latest_signals),
        }

    return get_or_set("dashboard_market_context", cache_key, ttl_seconds=60.0, loader=_load)
