from __future__ import annotations

import json
from collections import defaultdict
from urllib.parse import urlencode

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.tables import WorkspaceSnapshot
from app.services.market_lake import load_lake_rows, query_lake_daily_movers
from app.services.market_freshness import is_snapshot_as_of_current
from app.services.repository import SymbolRepository, WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.screener import MODEL_TEMPLATES
from app.services.screener_snapshots import (
    FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    FULL_MARKET_US_PRECOMPUTE_TEMPLATES,
)
from app.services.template_evaluation import aggregate_window_stats, template_forward_return_from_history
from app.services.time_utils import app_now_iso, app_today_iso


ACTION_BUCKET_LABELS = {
    "buy_the_dip": {"zh": "回踩买点", "en": "Buy The Dip"},
    "breakout_confirmation": {"zh": "突破确认", "en": "Breakout"},
    "bullish_entry": {"zh": "偏多入场", "en": "Bullish Entry"},
    "watchlist": {"zh": "继续观察", "en": "Watchlist"},
}

PRESET_COMBOS = [
    {
        "key": "dip_confluence",
        "label": {"zh": "回踩共振", "en": "Dip Confluence"},
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum", "cn_hammer_reversal", "cn_macd_underwater_cross"],
        "min_hits": 2,
        "action_bucket": "buy_the_dip",
    },
    {
        "key": "breakout_confluence",
        "label": {"zh": "突破共振", "en": "Breakout Confluence"},
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum", "cn_volume_breakout", "cn_bullish_ma_stack"],
        "min_hits": 2,
        "action_bucket": "breakout_confirmation",
    },
    {
        "key": "trend_momentum_lightgbm",
        "label": {"zh": "强趋势+动量+LightGBM", "en": "Trend + Momentum + LightGBM"},
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
        "min_hits": 2,
        "action_bucket": "ALL",
    },
    {
        "key": "quality_growth_entry",
        "label": {"zh": "成长质量共振", "en": "Quality Growth Confluence"},
        "templates": ["lightgbm_top_picks", "cn_growth_value", "cn_high_roe_steady_growth", "technical_momentum"],
        "min_hits": 2,
        "action_bucket": "bullish_entry",
    },
]

MODEL_SELECTION_GUIDANCE_SNAPSHOT_PREFIX = "model_selection_guidance"


def normalize_guidance_market(market: str | None) -> str:
    normalized = str(market or "CN").strip().upper()
    return normalized if normalized in {"CN", "US", "ALL"} else "CN"


def model_selection_guidance_snapshot_type(market: str | None) -> str:
    return f"{MODEL_SELECTION_GUIDANCE_SNAPSHOT_PREFIX}:{normalize_guidance_market(market)}"


def build_model_selection_guidance(
    *,
    market: str = "CN",
    lookback_snapshots: int = 30,
    top_n: int = 60,
    winner_lookback_dates: int = 10,
    winner_top_n: int = 20,
    winner_min_return_pct: float = 3.0,
) -> dict:
    normalized_market = str(market or "CN").strip().upper()
    target_markets = ["CN", "US"] if normalized_market == "ALL" else [normalized_market if normalized_market in {"CN", "US"} else "CN"]
    cache_key = json.dumps(
        {
            "market": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
            "winner_lookback_dates": int(winner_lookback_dates),
            "winner_top_n": int(winner_top_n),
            "winner_min_return_pct": float(winner_min_return_pct),
            "version": 2,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return get_or_set("model_selection_guidance", cache_key, ttl_seconds=600.0, loader=lambda: _build_guidance_uncached(
        target_markets=target_markets,
        lookback_snapshots=lookback_snapshots,
        top_n=top_n,
        winner_lookback_dates=winner_lookback_dates,
        winner_top_n=winner_top_n,
        winner_min_return_pct=winner_min_return_pct,
    ))


def save_model_selection_guidance_snapshots(
    db,
    *,
    markets: list[str] | tuple[str, ...] | set[str] | None = None,
    source_job_id: int | None = None,
) -> dict[str, dict]:
    requested_markets = markets or ["CN"]
    normalized_markets: list[str] = []
    for value in requested_markets:
        market_code = normalize_guidance_market(value)
        if market_code not in normalized_markets:
            normalized_markets.append(market_code)
    repo = WorkspaceSnapshotRepository(db)
    snapshot_date = app_today_iso()
    created: dict[str, dict] = {}
    for market_code in normalized_markets:
        payload = build_model_selection_guidance(market=market_code)
        payload["schema_version"] = 1
        payload["snapshot_meta"] = {
            "source": "snapshot",
            "market": market_code,
            "generated_at": app_now_iso(),
        }
        row = repo.create_snapshot(
            snapshot_type=model_selection_guidance_snapshot_type(market_code),
            snapshot_date=snapshot_date,
            payload=payload,
            source_job_id=source_job_id,
        )
        created[market_code] = {
            "id": row.id,
            "market": market_code,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "created_at": row.created_at,
        }
    return created


def load_model_selection_guidance_snapshot(
    db,
    *,
    market: str = "CN",
    allow_fallback: bool = True,
) -> dict:
    market_code = normalize_guidance_market(market)
    snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(model_selection_guidance_snapshot_type(market_code))
    stale_snapshot_detected = False
    stale_snapshot_date = None
    if snapshot and isinstance(snapshot.get("payload"), dict):
        payload = dict(snapshot.get("payload") or {})
        if (payload.get("schema_version") or payload.get("input")) and not is_snapshot_as_of_current(snapshot.get("snapshot_date"), market_code):
            stale_snapshot_detected = True
            stale_snapshot_date = snapshot.get("snapshot_date")
        else:
            stale_snapshot_detected = False
        if stale_snapshot_detected:
            snapshot = None
        else:
            snapshot_meta = dict(payload.get("snapshot_meta") or {})
            snapshot_meta.update(
                {
                    "source": "snapshot",
                    "market": market_code,
                    "snapshot_id": snapshot.get("id"),
                    "snapshot_type": snapshot.get("snapshot_type"),
                    "snapshot_date": snapshot.get("snapshot_date"),
                    "created_at": snapshot.get("created_at"),
                    "source_job_id": snapshot.get("source_job_id"),
                    "freshness": "current",
                }
            )
            payload["snapshot_meta"] = snapshot_meta
            return payload
    payload = build_model_selection_guidance(market=market_code) if allow_fallback else {
        "markets": [market_code],
        "recommendations": [],
        "combos": [],
        "winner_attribution": [],
        "template_stats": [],
        "playbook_stats": [],
        "winner_total": 0,
    }
    payload["snapshot_meta"] = {
        "source": "live" if allow_fallback else "missing",
        "market": market_code,
        "generated_at": app_now_iso(),
        "freshness": "recomputed_after_stale_snapshot" if stale_snapshot_detected else "missing",
        "stale_snapshot_date": stale_snapshot_date,
    }
    return payload


def summarize_model_selection_guidance(payload: dict | None, *, lang: str = "zh") -> dict:
    guidance = payload or {}
    markets = [normalize_guidance_market(value) for value in (guidance.get("markets") or ["CN"])]
    target_market = markets[0] if len(markets) == 1 else "CN"
    recommendations = list(guidance.get("recommendations") or [])
    combos = list(guidance.get("combos") or [])
    top_model = recommendations[0] if recommendations else {}
    top_combo = combos[0] if combos else {}
    action_bucket = str(top_model.get("action_bucket") or "").strip()
    action_label = ACTION_BUCKET_LABELS.get(action_bucket, {}).get(lang, action_bucket or ("未归类" if lang == "zh" else "Unclassified"))
    model_title = str(top_model.get("template_label") or top_model.get("template") or ("样本继续沉淀" if lang == "zh" else "Still collecting samples"))
    if action_bucket and action_bucket not in {"ALL", "unclassified"}:
        model_title = f"{model_title} · {action_label}"
    combo_title = (
        (top_combo.get("label") or {}).get(lang)
        or (top_combo.get("label") or {}).get("zh")
        or ("组合样本继续沉淀" if lang == "zh" else "Combo samples still accumulating")
    )
    if lang == "zh":
        model_summary = (
            f"优先模型：{model_title}，1日均值 {((top_model.get('stats_1d') or {}).get('avg_return')) if top_model else '-'}%，"
            f"强票提前覆盖 {int(top_model.get('winner_capture_count') or 0)} 只。"
            if top_model
            else "优先模型：当前样本还不够，先继续观察。"
        )
        combo_summary = (
            f"优先组合：{combo_title}，1日命中率 {((top_combo.get('stats_1d') or {}).get('hit_rate')) if top_combo else '-'}%，"
            f"强票覆盖率 {top_combo.get('winner_capture_rate') if top_combo else '-'}%。"
            if top_combo
            else "优先组合：组合样本还不够，暂不强推。"
        )
    else:
        model_summary = (
            f"Priority model: {model_title}, 1D avg {((top_model.get('stats_1d') or {}).get('avg_return')) if top_model else '-'}%, "
            f"captured {int(top_model.get('winner_capture_count') or 0)} strong movers."
            if top_model
            else "Priority model: still collecting enough samples."
        )
        combo_summary = (
            f"Priority combo: {combo_title}, 1D hit rate {((top_combo.get('stats_1d') or {}).get('hit_rate')) if top_combo else '-'}%, "
            f"strong-mover coverage {top_combo.get('winner_capture_rate') if top_combo else '-'}%."
            if top_combo
            else "Priority combo: not enough combo samples yet."
        )
    return {
        "top_model": top_model,
        "top_combo": top_combo,
        "top_model_title": model_title,
        "top_combo_title": combo_title,
        "top_model_summary": model_summary,
        "top_combo_summary": combo_summary,
        "top_model_href": _recommendation_screener_href(top_model, target_market=target_market) if top_model else None,
        "top_combo_href": top_combo.get("screener_href") if top_combo else None,
        "snapshot_meta": guidance.get("snapshot_meta") or {},
    }


def _build_guidance_uncached(
    *,
    target_markets: list[str],
    lookback_snapshots: int,
    top_n: int,
    winner_lookback_dates: int,
    winner_top_n: int,
    winner_min_return_pct: float,
) -> dict:
    snapshots = _load_template_snapshots(target_markets=target_markets, lookback_snapshots=lookback_snapshots, top_n=top_n)
    all_tickers = {record["ticker"] for record in snapshots if record.get("ticker")}
    all_tickers.update(str(item.get("symbol") or "").strip().upper() for market in target_markets for item in query_lake_daily_movers(
        market=market,
        lookback_dates=winner_lookback_dates,
        top_n_per_date=winner_top_n,
        min_return_pct=winner_min_return_pct,
        min_dollar_volume=10_000_000.0 if market == "CN" else 1_000_000.0,
    ))
    histories = _load_histories(target_markets=target_markets, tickers=all_tickers)
    symbol_names = _load_symbol_names(all_tickers)

    hit_index: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    template_stats: dict[str, dict] = {}
    playbook_stats: dict[tuple[str, str], dict] = {}
    for record in snapshots:
        market_code = str(record.get("market") or "").upper()
        ticker = str(record.get("ticker") or "").upper()
        signal_date = str(record.get("signal_date") or "")[:10]
        if not market_code or not ticker or not signal_date:
            continue
        history = histories.get((market_code, ticker), [])
        returns = _forward_returns(history, signal_date=signal_date)
        hit = {
            "template": record["template"],
            "template_label": _template_label(record["template"]),
            "action": record.get("action") or "",
            "action_bucket": record.get("action_bucket") or "",
            "score": record.get("score"),
        }
        hit_index[(market_code, signal_date, ticker)].append(hit)
        _update_stat_bucket(template_stats.setdefault(record["template"], _empty_stat_bucket(record["template"])), returns)
        playbook_key = (record["template"], record.get("action_bucket") or "unclassified")
        playbook = playbook_stats.setdefault(playbook_key, _empty_stat_bucket(record["template"], action_bucket=playbook_key[1]))
        _update_stat_bucket(playbook, returns)

    movers = _build_winner_attribution(
        target_markets=target_markets,
        histories=histories,
        hit_index=hit_index,
        symbol_names=symbol_names,
        winner_lookback_dates=winner_lookback_dates,
        winner_top_n=winner_top_n,
        winner_min_return_pct=winner_min_return_pct,
    )
    for winner in movers:
        seen_templates = set()
        seen_playbooks = set()
        for hit in winner.get("hits") or []:
            template_key = str(hit.get("template") or "")
            action_bucket = str(hit.get("action_bucket") or "unclassified")
            if template_key and template_key not in seen_templates and template_key in template_stats:
                template_stats[template_key]["winner_capture_count"] += 1
                seen_templates.add(template_key)
            playbook_key = (template_key, action_bucket)
            if playbook_key in playbook_stats and playbook_key not in seen_playbooks:
                playbook_stats[playbook_key]["winner_capture_count"] += 1
                seen_playbooks.add(playbook_key)

    winner_total = len(movers)
    recommendations = _rank_recommendations(template_stats, playbook_stats, winner_total=winner_total)
    combo_stats = _evaluate_combos(
        target_markets=target_markets,
        hit_index=hit_index,
        histories=histories,
        symbol_names=symbol_names,
        winner_total=winner_total,
        winner_rows=movers,
    )
    return {
        "markets": target_markets,
        "lookback_snapshots": int(lookback_snapshots),
        "top_n": int(top_n),
        "winner_total": winner_total,
        "winner_min_return_pct": float(winner_min_return_pct),
        "recommendations": recommendations[:8],
        "combos": combo_stats,
        "winner_attribution": movers[:24],
        "template_stats": [_finalize_stat_bucket(item, winner_total=winner_total) for item in template_stats.values()],
        "playbook_stats": [_finalize_stat_bucket(item, winner_total=winner_total) for item in playbook_stats.values()],
    }


def _load_template_snapshots(*, target_markets: list[str], lookback_snapshots: int, top_n: int) -> list[dict]:
    records: list[dict] = []
    with SessionLocal() as db:
        template_set_by_market = {
            market_code: set(_templates_for_market(market_code))
            for market_code in target_markets
        }
        max_days = max(1, int(lookback_snapshots))
        stmt = (
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.snapshot_type.like("screener_result:%"))
            .order_by(WorkspaceSnapshot.id.desc())
            .limit(max_days * max(sum(len(values) for values in template_set_by_market.values()), 1) * 12)
        )
        rows = db.scalars(stmt).all()
        latest_by_template_day: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            market_code = str(payload.get("market") or "").strip().upper()
            template_key = str(payload.get("model_template") or "").strip()
            universe = str(payload.get("universe") or "").strip().lower()
            if market_code not in template_set_by_market:
                continue
            if template_key not in template_set_by_market[market_code]:
                continue
            if universe != "full_market":
                continue
            signal_date = str(row.snapshot_date or "")[:10]
            if not signal_date:
                continue
            dedupe_key = (market_code, template_key, signal_date)
            existing = latest_by_template_day.get(dedupe_key)
            if existing is None or int(row.id) > int(existing.get("id") or 0):
                latest_by_template_day[dedupe_key] = {
                    "id": row.id,
                    "market": market_code,
                    "template": template_key,
                    "signal_date": signal_date,
                    "payload": payload,
                }

        for market_code in target_markets:
            for template_key in _templates_for_market(market_code):
                snapshots = sorted(
                    (
                        item
                        for key, item in latest_by_template_day.items()
                        if key[0] == market_code and key[1] == template_key
                    ),
                    key=lambda item: (str(item.get("signal_date") or ""), int(item.get("id") or 0)),
                    reverse=True,
                )[:max_days]
                for snapshot in snapshots:
                    signal_date = str(snapshot.get("signal_date") or "")[:10]
                    payload = snapshot.get("payload") or {}
                    for row in list(payload.get("rows") or [])[: max(1, int(top_n))]:
                        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
                        if not ticker:
                            continue
                        records.append(
                            {
                                "market": market_code,
                                "signal_date": signal_date,
                                "template": template_key,
                                "ticker": ticker,
                                "name": row.get("name") or ticker,
                                "action": str(row.get("action_label") or "").strip(),
                                "action_bucket": _primary_action_bucket(template_key, row.get("action_label")),
                                "score": row.get("snapshot_score") or row.get("trend_score") or row.get("score"),
                            }
                        )
    return records


def _templates_for_market(market_code: str) -> list[str]:
    if market_code == "US":
        return [key for key in FULL_MARKET_US_PRECOMPUTE_TEMPLATES if key in MODEL_TEMPLATES]
    return [key for key in FULL_MARKET_CN_PRECOMPUTE_TEMPLATES if key in MODEL_TEMPLATES]


def _load_histories(*, target_markets: list[str], tickers: set[str]) -> dict[tuple[str, str], list[dict]]:
    histories: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if not tickers:
        return histories
    for market_code in target_markets:
        rows = load_lake_rows(markets=[market_code], tickers=tickers, limit_per_symbol=280)
        for row in rows:
            ticker = str(row.get("symbol") or "").strip().upper()
            if ticker:
                histories[(market_code, ticker)].append(row)
    for key in list(histories.keys()):
        histories[key].sort(key=lambda item: str(item.get("date") or ""))
    return histories


def _load_symbol_names(tickers: set[str]) -> dict[str, str]:
    if not tickers:
        return {}
    with SessionLocal() as db:
        rows = SymbolRepository(db).list_overviews_for_tickers(sorted(tickers))
    return {ticker: str(row.get("name") or ticker) for ticker, row in rows.items()}


def _build_winner_attribution(
    *,
    target_markets: list[str],
    histories: dict[tuple[str, str], list[dict]],
    hit_index: dict[tuple[str, str, str], list[dict]],
    symbol_names: dict[str, str],
    winner_lookback_dates: int,
    winner_top_n: int,
    winner_min_return_pct: float,
) -> list[dict]:
    winners: list[dict] = []
    for market_code in target_markets:
        movers = query_lake_daily_movers(
            market=market_code,
            lookback_dates=winner_lookback_dates,
            top_n_per_date=winner_top_n,
            min_return_pct=winner_min_return_pct,
            min_dollar_volume=10_000_000.0 if market_code == "CN" else 1_000_000.0,
        )
        for mover in movers:
            ticker = str(mover.get("symbol") or "").strip().upper()
            trade_date = str(mover.get("trade_date") or "")[:10]
            previous_date = _previous_trade_date(histories.get((market_code, ticker), []), trade_date)
            hits = hit_index.get((market_code, previous_date or "", ticker), []) if previous_date else []
            winners.append(
                {
                    "market": market_code,
                    "ticker": ticker,
                    "name": symbol_names.get(ticker) or ticker,
                    "winner_date": trade_date,
                    "signal_date": previous_date,
                    "return_1d": round(float(mover.get("return_pct") or 0.0), 2),
                    "hit_count": len({str(hit.get("template") or "") for hit in hits}),
                    "hits": hits,
                }
            )
    winners.sort(key=lambda item: (-int(item.get("hit_count") or 0), -float(item.get("return_1d") or 0.0), str(item.get("ticker") or "")))
    return winners


def _evaluate_combos(
    *,
    target_markets: list[str],
    hit_index: dict[tuple[str, str, str], list[dict]],
    histories: dict[tuple[str, str], list[dict]],
    symbol_names: dict[str, str],
    winner_total: int,
    winner_rows: list[dict],
) -> list[dict]:
    rows: list[dict] = []
    winner_keys = {(item.get("market"), item.get("signal_date"), item.get("ticker")) for item in winner_rows if item.get("signal_date")}
    for combo in PRESET_COMBOS:
        returns_by_window = {1: [], 3: [], 5: [], 10: []}
        examples: list[dict] = []
        captured_winners = 0
        combo_templates = [template for template in combo["templates"] if any(template in _templates_for_market(market) for market in target_markets)]
        if len(combo_templates) < int(combo["min_hits"]):
            continue
        for market_code, signal_date, ticker in sorted(hit_index.keys()):
            hits = hit_index[(market_code, signal_date, ticker)]
            hit_templates = {str(hit.get("template") or "") for hit in hits if str(hit.get("template") or "") in combo_templates}
            if len(hit_templates) < int(combo["min_hits"]):
                continue
            action_bucket = str(combo.get("action_bucket") or "ALL")
            if action_bucket != "ALL":
                aligned = {
                    str(hit.get("template") or "")
                    for hit in hits
                    if str(hit.get("template") or "") in combo_templates and str(hit.get("action_bucket") or "") == action_bucket
                }
                if len(aligned) < int(combo["min_hits"]):
                    continue
            returns = _forward_returns(histories.get((market_code, ticker), []), signal_date=signal_date)
            for window, value in returns.items():
                if value is not None:
                    returns_by_window[window].append(float(value))
            if (market_code, signal_date, ticker) in winner_keys:
                captured_winners += 1
            if len(examples) < 6:
                examples.append(
                    {
                        "market": market_code,
                        "ticker": ticker,
                        "name": symbol_names.get(ticker) or ticker,
                        "signal_date": signal_date,
                        "hit_templates": sorted(hit_templates),
                        "return_1d": returns.get(1),
                    }
                )
        rows.append(
            {
                **combo,
                "available_templates": combo_templates,
                "stats_1d": aggregate_window_stats(returns_by_window[1]),
                "stats_3d": aggregate_window_stats(returns_by_window[3]),
                "stats_5d": aggregate_window_stats(returns_by_window[5]),
                "stats_10d": aggregate_window_stats(returns_by_window[10]),
                "winner_capture_count": captured_winners,
                "winner_capture_rate": round((captured_winners / winner_total) * 100.0, 1) if winner_total else None,
                "examples": examples,
                "screener_href": _combo_screener_href(combo, target_markets),
            }
        )
    rows.sort(key=lambda item: (_combo_score(item), str((item.get("label") or {}).get("zh") or "")), reverse=True)
    return rows


def _forward_returns(history: list[dict], *, signal_date: str) -> dict[int, float | None]:
    return {
        window: template_forward_return_from_history(history, trade_date=signal_date, sessions=window)
        for window in (1, 3, 5, 10)
    }


def _previous_trade_date(history: list[dict], trade_date: str) -> str | None:
    previous = None
    for row in history:
        row_date = str(row.get("date") or "")[:10]
        if row_date >= trade_date:
            break
        previous = row_date
    return previous


def _empty_stat_bucket(template_key: str, *, action_bucket: str | None = None) -> dict:
    return {
        "template": template_key,
        "template_label": _template_label(template_key),
        "action_bucket": action_bucket,
        "returns": {1: [], 3: [], 5: [], 10: []},
        "sample_count": 0,
        "winner_capture_count": 0,
    }


def _update_stat_bucket(bucket: dict, returns: dict[int, float | None]) -> None:
    bucket["sample_count"] = int(bucket.get("sample_count") or 0) + 1
    for window, value in returns.items():
        if value is not None:
            (bucket.get("returns") or {}).setdefault(window, []).append(float(value))


def _finalize_stat_bucket(bucket: dict, *, winner_total: int) -> dict:
    returns = bucket.get("returns") or {}
    payload = {
        key: value
        for key, value in bucket.items()
        if key != "returns"
    }
    payload["stats_1d"] = aggregate_window_stats(list(returns.get(1) or []))
    payload["stats_3d"] = aggregate_window_stats(list(returns.get(3) or []))
    payload["stats_5d"] = aggregate_window_stats(list(returns.get(5) or []))
    payload["stats_10d"] = aggregate_window_stats(list(returns.get(10) or []))
    payload["winner_capture_rate"] = round((int(bucket.get("winner_capture_count") or 0) / winner_total) * 100.0, 1) if winner_total else None
    payload["score"] = _stat_score(payload)
    return payload


def _rank_recommendations(template_stats: dict[str, dict], playbook_stats: dict[tuple[str, str], dict], *, winner_total: int) -> list[dict]:
    template_rows = [_finalize_stat_bucket(item, winner_total=winner_total) for item in template_stats.values()]
    playbook_rows = [_finalize_stat_bucket(item, winner_total=winner_total) for item in playbook_stats.values()]
    redundant_templates = {
        str(template_item.get("template") or "")
        for template_item in template_rows
        for playbook_item in playbook_rows
        if str(template_item.get("template") or "") == str(playbook_item.get("template") or "")
        and int(template_item.get("sample_count") or 0) == int(playbook_item.get("sample_count") or 0)
        and int(template_item.get("winner_capture_count") or 0) == int(playbook_item.get("winner_capture_count") or 0)
    }
    finalized = playbook_rows + [
        item for item in template_rows if str(item.get("template") or "") not in redundant_templates
    ]
    filtered = [item for item in finalized if int(((item.get("stats_1d") or {}).get("count") or 0)) >= 8 or int(item.get("winner_capture_count") or 0) > 0]
    filtered.sort(key=lambda item: (_stat_score(item), int(item.get("sample_count") or 0), str(item.get("template") or "")), reverse=True)
    return filtered


def _stat_score(item: dict) -> float:
    stats_1d = item.get("stats_1d") or {}
    stats_3d = item.get("stats_3d") or {}
    sample_count = int(item.get("sample_count") or 0)
    capture_count = int(item.get("winner_capture_count") or 0)
    hit_rate = float(stats_1d.get("hit_rate") or 0.0)
    avg_1d = float(stats_1d.get("avg_return") or 0.0)
    avg_3d = float(stats_3d.get("avg_return") or 0.0)
    sample_bonus = min(12.0, sample_count / 12.0)
    return round(hit_rate * 0.35 + avg_1d * 5.0 + avg_3d * 1.5 + capture_count * 6.0 + sample_bonus, 2)


def _combo_score(item: dict) -> float:
    stats_1d = item.get("stats_1d") or {}
    stats_3d = item.get("stats_3d") or {}
    return round(
        float(stats_1d.get("hit_rate") or 0.0) * 0.35
        + float(stats_1d.get("avg_return") or 0.0) * 5.0
        + float(stats_3d.get("avg_return") or 0.0) * 1.5
        + int(item.get("winner_capture_count") or 0) * 7.0,
        2,
    )


def _primary_action_bucket(template_key: str, action_label: str | None) -> str:
    buckets = _action_buckets(template_key, action_label)
    if not buckets:
        return "unclassified"
    for preferred in ("buy_the_dip", "breakout_confirmation", "bullish_entry", "watchlist"):
        if preferred in buckets:
            return preferred
    return buckets[0]


def _action_buckets(template_key: str, action_label: str | None) -> list[str]:
    normalized = str(action_label or "").strip().lower().replace(" ", "_")
    buckets: list[str] = []
    if normalized in {"buy_the_dip", "pullback"}:
        buckets.extend(["buy_the_dip", "bullish_entry"])
    elif normalized in {"wait_for_breakout", "breakout"}:
        buckets.extend(["breakout_confirmation", "bullish_entry"])
    elif normalized in {"buy", "strong_buy", "technical_pattern", "fundamental_pass"}:
        buckets.append("bullish_entry")
    elif normalized in {"watch", "hold", "hold_and_watch", "wait", "avoid", "avoid_or_wait", "continue_to_watch"}:
        buckets.append("watchlist")
    if template_key in {"cn_hammer_reversal", "cn_bullish_engulfing_reversal", "cn_macd_underwater_cross"}:
        buckets.extend(["buy_the_dip", "bullish_entry"])
    if template_key in {"cn_volume_breakout", "cn_bullish_ma_stack", "cn_three_white_soldiers"}:
        buckets.extend(["breakout_confirmation", "bullish_entry"])
    if template_key in {"cn_ma_cluster_breakout_watch", "cn_bollinger_squeeze_watch"}:
        buckets.append("breakout_confirmation")
    if template_key in {"global_growth_value", "global_income_quality", "cn_growth_value", "cn_high_roe_steady_growth", "cn_low_valuation_high_dividend"}:
        buckets.append("bullish_entry")
    return list(dict.fromkeys(buckets))


def _template_label(template_key: str) -> str:
    config = MODEL_TEMPLATES.get(template_key) or {}
    return str(config.get("label") or template_key)


def _combo_screener_href(combo: dict, target_markets: list[str]) -> str:
    market = target_markets[0] if len(target_markets) == 1 else "CN"
    params = {
        "lang": "zh",
        "run": 1,
        "market": market,
        "universe": "full_market",
        "model_template": combo["templates"][0],
        "multi_model_templates": combo["templates"],
        "min_multi_model_hits": int(combo["min_hits"]),
        "confluence_action_filter": combo.get("action_bucket") or "ALL",
        "min_trend_score": 10,
        "sort_by": "confluence_rank",
        "sort_order": "desc",
    }
    return "/screeners?" + urlencode(params, doseq=True)


def _recommendation_screener_href(item: dict, *, target_market: str) -> str:
    template = str(item.get("template") or "").strip()
    if not template:
        return "/screeners?lang=zh"
    action_bucket = str(item.get("action_bucket") or "").strip()
    template_defaults = (MODEL_TEMPLATES.get(template) or {}).get("defaults") or {}
    try:
        min_trend_score = max(0, int(template_defaults.get("min_trend_score", 60)))
    except (TypeError, ValueError):
        min_trend_score = 60
    params = {
        "lang": "zh",
        "run": 1,
        "market": normalize_guidance_market(target_market),
        "universe": "full_market",
        "model_template": template,
        "min_trend_score": min_trend_score,
        "sort_by": "trade_readiness_score",
        "sort_order": "desc",
    }
    if action_bucket and action_bucket not in {"ALL", "unclassified"}:
        params["confluence_action_filter"] = action_bucket
        if action_bucket == "buy_the_dip":
            params["action_filter"] = "buy_the_dip"
        elif action_bucket == "breakout_confirmation":
            params["action_filter"] = "wait_for_breakout"
        elif action_bucket == "watchlist":
            params["action_filter"] = "hold_and_watch"
    return "/screeners?" + urlencode(params, doseq=True)
