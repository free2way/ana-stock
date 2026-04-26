from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.tables import ModelRun, Prediction, PredictionDetail, Symbol
from app.services.market_lake import load_lake_price_history
from app.services.repository import SymbolRepository, WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.screener_snapshots import build_base_precompute_params, screener_snapshot_type


def normalize_template_action(value: str | None) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def aggregate_window_stats(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "avg_return": None,
            "hit_rate": None,
            "strong_hit_rate": None,
            "miss_rate": None,
        }
    count = len(values)
    hit_count = sum(1 for item in values if item > 0)
    strong_hit_count = sum(1 for item in values if item >= 3.0)
    miss_count = sum(1 for item in values if item <= -3.0)
    return {
        "count": count,
        "avg_return": round(sum(values) / count, 2),
        "hit_rate": round((hit_count / count) * 100.0, 1),
        "strong_hit_rate": round((strong_hit_count / count) * 100.0, 1),
        "miss_rate": round((miss_count / count) * 100.0, 1),
    }


def template_forward_return_from_history(history: list[dict], *, trade_date: str, sessions: int) -> float | None:
    if not history:
        return None
    start_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") >= str(trade_date)), None)
    if start_index is None:
        return None
    end_index = start_index + int(sessions)
    if end_index >= len(history):
        return None
    start_close = history[start_index].get("close")
    end_close = history[end_index].get("close")
    if start_close in (None, 0) or end_close is None:
        return None
    try:
        return round(((float(end_close) / float(start_close)) - 1.0) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def resolve_template_group_label(*, meta: dict | None, ticker: str, market_code: str, name: str | None = None) -> str:
    overview = meta or {}
    sector = str(overview.get("sector") or "").strip()
    industry = str(overview.get("industry") or "").strip()
    if sector:
        return sector
    if industry:
        return industry
    normalized_market = str(market_code or "").upper()
    normalized_ticker = str(ticker or "").strip().upper()
    normalized_name = str(name or overview.get("name") or "").strip().upper()
    exchange = str(overview.get("exchange") or "").strip().upper()
    if normalized_market == "CN":
        code = normalized_ticker.split(".", 1)[0]
        if normalized_ticker.endswith(".BJ") or exchange in {"BSE", "BJ"}:
            return "北交所 / BSE"
        if code.startswith(("688", "689")):
            return "科创板 / STAR"
        if code.startswith(("300", "301")):
            return "创业板 / ChiNext"
        if exchange == "SZSE" or code.startswith(("000", "001", "002", "003")):
            return "深主板 / SZSE Main"
        if exchange == "SSE" or code.startswith(("600", "601", "603", "605")):
            return "沪主板 / SSE Main"
        return "A股其他 / CN Other"
    if normalized_market == "US":
        if any(keyword in normalized_name for keyword in ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "VANGUARD")):
            return "美股 ETF / US ETF"
        if any(keyword in normalized_name for keyword in ("BIO", "THERAPEUTICS", "PHARMA", "HEALTH", "MEDICAL", "DRUG", "LIFE SCIENCES")):
            return "美股医药 / US Healthcare"
        if any(keyword in normalized_name for keyword in ("BANK", "CAPITAL", "FINANCIAL", "PAYMENTS", "INSURANCE", "SOFI")) or normalized_ticker in {"SOFI", "COIN", "HOOD"}:
            return "美股金融 / US Financials"
        if any(keyword in normalized_name for keyword in ("ENERGY", "OIL", "SOLAR", "URANIUM", "MINING", "COPPER", "LITHIUM", "EXXON", "MOBIL", "CHEVRON", "PETROLEUM", "GAS")):
            return "美股能源材料 / US Energy & Materials"
        if any(keyword in normalized_name for keyword in ("RETAIL", "CONSUMER", "AUTO", "MOTORS", "TESLA", "RIVIAN", "TRAVEL", "AIRLINES", "HOTEL", "FOOD", "BEVERAGE")) or normalized_ticker in {"TSLA", "RIVN", "NIO", "LI", "XPEV"}:
            return "美股消费出行 / US Consumer & Mobility"
        if any(keyword in normalized_name for keyword in ("SEMICONDUCTOR", "NVIDIA", "PALANTIR", "SOFTWARE", "CLOUD", "DATA", "MICRO", "AI", "COMPUTE", "ROBOT", "SPACE", "TECH")) or normalized_ticker in {"AAPL", "NVDA", "AMD", "AVGO", "PLTR", "ASTS", "SMCI", "MSTR", "TSM", "MU", "MSFT", "GOOGL", "META", "AMZN"}:
            return "美股科技 / US Tech"
        return "美股综合 / US General"
    return "Unclassified"


def normalize_lightgbm_action(value: str | None) -> str:
    normalized = normalize_template_action(value)
    if normalized in {"pullback", "buy_the_dip"}:
        return "pullback"
    if normalized in {"breakout", "wait_for_breakout"}:
        return "breakout"
    if normalized in {"watch", "hold", "hold_and_watch", "wait", "avoid", "avoid_or_wait", "continue_to_watch"}:
        return "watch"
    if normalized in {"buy", "strong_buy"}:
        return "breakout"
    return ""


def normalize_lightgbm_prediction_action(*, entry_style: str | None, signal_label: str | None) -> str:
    entry = normalize_lightgbm_action(entry_style)
    if entry:
        return entry
    normalized_signal = normalize_template_action(signal_label)
    if normalized_signal in {"buy", "strong_buy"}:
        return "breakout"
    if normalized_signal in {"watch", "hold", "sell", "strong_sell"}:
        return "watch"
    return ""


def build_next_tesla_evaluation(*, market: str, lookback_snapshots: int = 15, top_n: int = 20) -> dict:
    target_markets = ["CN", "US"] if str(market or "ALL").upper() == "ALL" else [str(market or "CN").upper()]
    cache_key = json.dumps(
        {
            "market": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _loader() -> dict:
        action_buckets: dict[str, dict[int, list[float]]] = {
            "buy_the_dip": {3: [], 5: [], 10: []},
            "wait_for_breakout": {3: [], 5: [], 10: []},
        }
        sector_buckets: dict[str, dict[str, dict[int, list[float]]]] = {
            "buy_the_dip": {},
            "wait_for_breakout": {},
        }
        sector_counts: dict[str, dict[str, int]] = {
            "buy_the_dip": {},
            "wait_for_breakout": {},
        }
        samples_by_action: dict[str, list[dict]] = {"buy_the_dip": [], "wait_for_breakout": []}
        history_cache: dict[tuple[str, str], list[dict]] = {}
        symbol_meta_cache: dict[str, dict | None] = {}
        snapshot_total = 0
        clean_snapshot_total = 0
        per_market: dict[str, dict] = {
            code: {
                "snapshot_total": 0,
                "clean_snapshot_total": 0,
                "action_buckets": {
                    "buy_the_dip": {3: [], 5: [], 10: []},
                    "wait_for_breakout": {3: [], 5: [], 10: []},
                },
            }
            for code in target_markets
        }
        with SessionLocal() as db:
            snapshot_repo = WorkspaceSnapshotRepository(db)
            symbol_repo = SymbolRepository(db)
            for market_code in target_markets:
                params = build_base_precompute_params(
                    model_template="next_tesla_swing",
                    universe="full_market",
                    market=market_code,
                )
                snapshots = snapshot_repo.list_snapshots(
                    screener_snapshot_type(params),
                    limit=max(1, int(lookback_snapshots)),
                )
                for snapshot in snapshots:
                    snapshot_total += 1
                    per_market[market_code]["snapshot_total"] += 1
                    payload = snapshot.get("payload") or {}
                    rows = list(payload.get("rows") or [])[: max(1, int(top_n))]
                    trade_date = str(snapshot.get("snapshot_date") or "")[:10]
                    if not trade_date:
                        continue
                    labeled_in_snapshot = False
                    for row in rows:
                        action_key = normalize_template_action(row.get("action_label"))
                        if action_key not in action_buckets:
                            continue
                        labeled_in_snapshot = True
                        ticker = str(row.get("ticker") or "").strip().upper()
                        if not ticker:
                            continue
                        if ticker not in symbol_meta_cache:
                            symbol_meta_cache[ticker] = symbol_repo.get_overview(ticker)
                        meta = symbol_meta_cache.get(ticker) or {}
                        sector_label = resolve_template_group_label(
                            meta=meta,
                            ticker=ticker,
                            market_code=market_code,
                            name=row.get("name"),
                        )
                        history_key = (market_code, ticker)
                        if history_key not in history_cache:
                            history_cache[history_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                        history = history_cache[history_key]
                        sample = {
                            "ticker": ticker,
                            "name": row.get("name") or ticker,
                            "market": market_code,
                            "sector": sector_label,
                            "trade_date": trade_date,
                            "return_3d": template_forward_return_from_history(history, trade_date=trade_date, sessions=3),
                            "return_5d": template_forward_return_from_history(history, trade_date=trade_date, sessions=5),
                            "return_10d": template_forward_return_from_history(history, trade_date=trade_date, sessions=10),
                        }
                        sector_counts[action_key][sector_label] = sector_counts[action_key].get(sector_label, 0) + 1
                        for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                            value = sample.get(key)
                            if value is not None:
                                action_buckets[action_key][window].append(float(value))
                                per_market[market_code]["action_buckets"][action_key][window].append(float(value))
                                sector_bucket = sector_buckets[action_key].setdefault(sector_label, {3: [], 5: [], 10: []})
                                sector_bucket[window].append(float(value))
                        if len(samples_by_action[action_key]) < 8:
                            samples_by_action[action_key].append(sample)
                    if labeled_in_snapshot:
                        clean_snapshot_total += 1
                        per_market[market_code]["clean_snapshot_total"] += 1
        windows = {
            action: {window: aggregate_window_stats(values) for window, values in bucket.items()}
            for action, bucket in action_buckets.items()
        }
        sector_windows = {
            action: {
                sector: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                for sector, bucket in sector_map.items()
            }
            for action, sector_map in sector_buckets.items()
        }
        return {
            "markets": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
            "snapshot_total": snapshot_total,
            "clean_snapshot_total": clean_snapshot_total,
            "windows": windows,
            "sector_windows": sector_windows,
            "sector_counts": sector_counts,
            "samples": samples_by_action,
            "per_market": {
                code: {
                    "snapshot_total": int(payload.get("snapshot_total") or 0),
                    "clean_snapshot_total": int(payload.get("clean_snapshot_total") or 0),
                    "windows": {
                        action: {
                            window: aggregate_window_stats(values)
                            for window, values in ((payload.get("action_buckets") or {}).get(action) or {}).items()
                        }
                        for action in ("buy_the_dip", "wait_for_breakout")
                    },
                }
                for code, payload in per_market.items()
            },
        }

    return get_or_set("template_eval_next_tesla", cache_key, ttl_seconds=600.0, loader=_loader)


def next_tesla_maturity(payload: dict, *, lang: str) -> dict:
    windows = (payload or {}).get("windows") or {}
    clean_snapshot_total = int((payload or {}).get("clean_snapshot_total") or 0)
    dip_5 = int((((windows.get("buy_the_dip") or {}).get(5) or {}).get("count") or 0))
    breakout_5 = int((((windows.get("wait_for_breakout") or {}).get(5) or {}).get("count") or 0))
    mature_5d = dip_5 + breakout_5
    if mature_5d >= 20 and clean_snapshot_total >= 8:
        if lang == "zh":
            return {"level": "可比较", "tone": "good", "summary": "当前已有较成熟样本，可以开始比较回踩与突破两类打法。"}
        return {"level": "Comparable", "tone": "good", "summary": "There are enough mature samples to start comparing pullback and breakout playbooks."}
    if mature_5d >= 8 and clean_snapshot_total >= 4:
        if lang == "zh":
            return {"level": "初步参考", "tone": "mid", "summary": "当前样本开始具备参考意义，但还不适合做过强结论。"}
        return {"level": "Early Read", "tone": "mid", "summary": "Samples are becoming informative, but still too thin for strong conclusions."}
    if lang == "zh":
        return {"level": "观察期", "tone": "soft", "summary": "当前仍处于样本沉淀期，更适合作为观察面板而不是评判面板。"}
    return {"level": "Observation", "tone": "soft", "summary": "The module is still in sample-accumulation mode and is better used for observation than judgment."}


def next_tesla_market_bias(payload: dict, *, lang: str) -> str:
    windows = (payload or {}).get("windows") or {}
    dip_5 = (windows.get("buy_the_dip") or {}).get(5) or {}
    breakout_5 = (windows.get("wait_for_breakout") or {}).get(5) or {}
    dip_count = int(dip_5.get("count") or 0)
    breakout_count = int(breakout_5.get("count") or 0)
    dip_hit = float(dip_5.get("hit_rate") or 0.0)
    breakout_hit = float(breakout_5.get("hit_rate") or 0.0)
    dip_avg = float(dip_5.get("avg_return") or 0.0)
    breakout_avg = float(breakout_5.get("avg_return") or 0.0)
    if dip_count <= 0 and breakout_count <= 0:
        return "样本观察中" if lang == "zh" else "Observation only"
    if dip_count > 0 and breakout_count <= 0:
        return "偏回踩布局" if lang == "zh" else "Leaning Buy The Dip"
    if breakout_count > 0 and dip_count <= 0:
        return "偏突破确认" if lang == "zh" else "Leaning Breakout"
    if dip_hit >= breakout_hit + 5 and dip_avg >= breakout_avg - 1:
        return "偏回踩布局" if lang == "zh" else "Leaning Buy The Dip"
    if breakout_hit >= dip_hit + 5 and breakout_avg >= dip_avg - 1:
        return "偏突破确认" if lang == "zh" else "Leaning Breakout"
    return "两类并存" if lang == "zh" else "Mixed"


def build_technical_momentum_evaluation(*, market: str, lookback_snapshots: int = 15, top_n: int = 40) -> dict:
    target_markets = ["CN", "US"] if str(market or "ALL").upper() == "ALL" else [str(market or "CN").upper()]
    cache_key = json.dumps(
        {
            "template": "technical_momentum",
            "market": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _loader() -> dict:
        action_buckets: dict[str, dict[int, list[float]]] = {
            "buy": {3: [], 5: [], 10: []},
            "watch": {3: [], 5: [], 10: []},
            "hold": {3: [], 5: [], 10: []},
        }
        sector_buckets: dict[str, dict[str, dict[int, list[float]]]] = {
            "buy": {},
            "watch": {},
            "hold": {},
        }
        sector_counts: dict[str, dict[str, int]] = {
            "buy": {},
            "watch": {},
            "hold": {},
        }
        samples_by_action: dict[str, list[dict]] = {"buy": [], "watch": [], "hold": []}
        history_cache: dict[tuple[str, str], list[dict]] = {}
        symbol_meta_cache: dict[str, dict | None] = {}
        snapshot_total = 0
        labeled_snapshot_total = 0
        per_market: dict[str, dict] = {
            code: {
                "snapshot_total": 0,
                "labeled_snapshot_total": 0,
                "action_buckets": {
                    "buy": {3: [], 5: [], 10: []},
                    "watch": {3: [], 5: [], 10: []},
                    "hold": {3: [], 5: [], 10: []},
                },
                "sector_counts": {
                    "buy": {},
                    "watch": {},
                    "hold": {},
                },
            }
            for code in target_markets
        }
        with SessionLocal() as db:
            snapshot_repo = WorkspaceSnapshotRepository(db)
            symbol_repo = SymbolRepository(db)
            for market_code in target_markets:
                params = build_base_precompute_params(
                    model_template="technical_momentum",
                    universe="full_market",
                    market=market_code,
                )
                snapshots = snapshot_repo.list_snapshots(
                    screener_snapshot_type(params),
                    limit=max(1, int(lookback_snapshots)),
                )
                for snapshot in snapshots:
                    snapshot_total += 1
                    per_market[market_code]["snapshot_total"] += 1
                    payload = snapshot.get("payload") or {}
                    rows = list(payload.get("rows") or [])[: max(1, int(top_n))]
                    trade_date = str(snapshot.get("snapshot_date") or "")[:10]
                    if not trade_date:
                        continue
                    labeled = False
                    for row in rows:
                        action_key = normalize_template_action(row.get("action_label"))
                        if action_key not in action_buckets:
                            continue
                        labeled = True
                        ticker = str(row.get("ticker") or "").strip().upper()
                        if not ticker:
                            continue
                        if ticker not in symbol_meta_cache:
                            symbol_meta_cache[ticker] = symbol_repo.get_overview(ticker)
                        meta = symbol_meta_cache.get(ticker) or {}
                        sector_label = resolve_template_group_label(
                            meta=meta,
                            ticker=ticker,
                            market_code=market_code,
                            name=row.get("name"),
                        )
                        history_key = (market_code, ticker)
                        if history_key not in history_cache:
                            history_cache[history_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                        history = history_cache[history_key]
                        sample = {
                            "ticker": ticker,
                            "name": row.get("name") or ticker,
                            "market": market_code,
                            "sector": sector_label,
                            "trade_date": trade_date,
                            "return_3d": template_forward_return_from_history(history, trade_date=trade_date, sessions=3),
                            "return_5d": template_forward_return_from_history(history, trade_date=trade_date, sessions=5),
                            "return_10d": template_forward_return_from_history(history, trade_date=trade_date, sessions=10),
                        }
                        sector_counts[action_key][sector_label] = sector_counts[action_key].get(sector_label, 0) + 1
                        per_market_sector_counts = (per_market[market_code].get("sector_counts") or {}).setdefault(action_key, {})
                        per_market_sector_counts[sector_label] = per_market_sector_counts.get(sector_label, 0) + 1
                        for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                            value = sample.get(key)
                            if value is not None:
                                action_buckets[action_key][window].append(float(value))
                                per_market[market_code]["action_buckets"][action_key][window].append(float(value))
                                sector_bucket = sector_buckets[action_key].setdefault(sector_label, {3: [], 5: [], 10: []})
                                sector_bucket[window].append(float(value))
                        if len(samples_by_action[action_key]) < 8:
                            samples_by_action[action_key].append(sample)
                    if labeled:
                        labeled_snapshot_total += 1
                        per_market[market_code]["labeled_snapshot_total"] += 1
        return {
            "markets": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
            "snapshot_total": snapshot_total,
            "labeled_snapshot_total": labeled_snapshot_total,
            "windows": {
                action: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                for action, bucket in action_buckets.items()
            },
            "sector_windows": {
                action: {
                    sector: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                    for sector, bucket in sector_map.items()
                }
                for action, sector_map in sector_buckets.items()
            },
            "sector_counts": sector_counts,
            "samples": samples_by_action,
            "per_market": {
                code: {
                    "snapshot_total": int(payload.get("snapshot_total") or 0),
                    "labeled_snapshot_total": int(payload.get("labeled_snapshot_total") or 0),
                    "windows": {
                        action: {
                            window: aggregate_window_stats(values)
                            for window, values in ((payload.get("action_buckets") or {}).get(action) or {}).items()
                        }
                        for action in ("buy", "watch", "hold")
                    },
                    "sector_counts": {
                        action: dict(((payload.get("sector_counts") or {}).get(action) or {}))
                        for action in ("buy", "watch", "hold")
                    },
                }
                for code, payload in per_market.items()
            },
        }

    return get_or_set("template_eval_technical_momentum", cache_key, ttl_seconds=600.0, loader=_loader)


def build_lightgbm_evaluation(*, market: str, lookback_snapshots: int = 15, top_n: int = 40) -> dict:
    target_markets = ["CN", "US"] if str(market or "ALL").upper() == "ALL" else [str(market or "CN").upper()]
    cache_key = json.dumps(
        {
            "template": "lightgbm_top_picks",
            "version": 2,
            "market": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _loader() -> dict:
        action_buckets: dict[str, dict[int, list[float]]] = {
            "pullback": {1: [], 3: [], 5: [], 10: []},
            "breakout": {1: [], 3: [], 5: [], 10: []},
            "watch": {1: [], 3: [], 5: [], 10: []},
        }
        sector_buckets: dict[str, dict[str, dict[int, list[float]]]] = {
            "pullback": {},
            "breakout": {},
            "watch": {},
        }
        sector_counts: dict[str, dict[str, int]] = {
            "pullback": {},
            "breakout": {},
            "watch": {},
        }
        samples_by_action: dict[str, list[dict]] = {"pullback": [], "breakout": [], "watch": []}
        history_cache: dict[tuple[str, str], list[dict]] = {}
        symbol_meta_cache: dict[str, dict | None] = {}
        snapshot_total = 0
        labeled_snapshot_total = 0
        per_market: dict[str, dict] = {
            code: {
                "snapshot_total": 0,
                "labeled_snapshot_total": 0,
                "action_buckets": {
                    "pullback": {1: [], 3: [], 5: [], 10: []},
                    "breakout": {1: [], 3: [], 5: [], 10: []},
                    "watch": {1: [], 3: [], 5: [], 10: []},
                },
                "sector_counts": {
                    "pullback": {},
                    "breakout": {},
                    "watch": {},
                },
            }
            for code in target_markets
        }
        with SessionLocal() as db:
            snapshot_repo = WorkspaceSnapshotRepository(db)
            symbol_repo = SymbolRepository(db)
            for market_code in target_markets:
                params = build_base_precompute_params(
                    model_template="lightgbm_top_picks",
                    universe="full_market",
                    market=market_code,
                )
                snapshots = snapshot_repo.list_snapshots(
                    screener_snapshot_type(params),
                    limit=max(1, int(lookback_snapshots)),
                )
                for snapshot in snapshots:
                    snapshot_total += 1
                    per_market[market_code]["snapshot_total"] += 1
                    payload = snapshot.get("payload") or {}
                    rows = list(payload.get("rows") or [])[: max(1, int(top_n))]
                    trade_date = str(snapshot.get("snapshot_date") or "")[:10]
                    if not trade_date:
                        continue
                    labeled = False
                    for row in rows:
                        action_key = normalize_lightgbm_action(row.get("action_label"))
                        if action_key not in action_buckets:
                            continue
                        labeled = True
                        ticker = str(row.get("ticker") or "").strip().upper()
                        if not ticker:
                            continue
                        if ticker not in symbol_meta_cache:
                            symbol_meta_cache[ticker] = symbol_repo.get_overview(ticker)
                        meta = symbol_meta_cache.get(ticker) or {}
                        sector_label = resolve_template_group_label(
                            meta=meta,
                            ticker=ticker,
                            market_code=market_code,
                            name=row.get("name"),
                        )
                        history_key = (market_code, ticker)
                        if history_key not in history_cache:
                            history_cache[history_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                        history = history_cache[history_key]
                        sample = {
                            "ticker": ticker,
                            "name": row.get("name") or ticker,
                            "market": market_code,
                            "sector": sector_label,
                            "trade_date": trade_date,
                            "return_1d": template_forward_return_from_history(history, trade_date=trade_date, sessions=1),
                            "return_3d": template_forward_return_from_history(history, trade_date=trade_date, sessions=3),
                            "return_5d": template_forward_return_from_history(history, trade_date=trade_date, sessions=5),
                            "return_10d": template_forward_return_from_history(history, trade_date=trade_date, sessions=10),
                        }
                        sector_counts[action_key][sector_label] = sector_counts[action_key].get(sector_label, 0) + 1
                        per_market_sector_counts = (per_market[market_code].get("sector_counts") or {}).setdefault(action_key, {})
                        per_market_sector_counts[sector_label] = per_market_sector_counts.get(sector_label, 0) + 1
                        for window, key in ((1, "return_1d"), (3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                            value = sample.get(key)
                            if value is not None:
                                action_buckets[action_key][window].append(float(value))
                                per_market[market_code]["action_buckets"][action_key][window].append(float(value))
                                sector_bucket = sector_buckets[action_key].setdefault(sector_label, {1: [], 3: [], 5: [], 10: []})
                                sector_bucket[window].append(float(value))
                        if len(samples_by_action[action_key]) < 8:
                            samples_by_action[action_key].append(sample)
                    if labeled:
                        labeled_snapshot_total += 1
                        per_market[market_code]["labeled_snapshot_total"] += 1
        return {
            "markets": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
            "snapshot_total": snapshot_total,
            "labeled_snapshot_total": labeled_snapshot_total,
            "windows": {
                action: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                for action, bucket in action_buckets.items()
            },
            "sector_windows": {
                action: {
                    sector: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                    for sector, bucket in sector_map.items()
                }
                for action, sector_map in sector_buckets.items()
            },
            "sector_counts": sector_counts,
            "samples": samples_by_action,
            "per_market": {
                code: {
                    "snapshot_total": int(payload.get("snapshot_total") or 0),
                    "labeled_snapshot_total": int(payload.get("labeled_snapshot_total") or 0),
                    "windows": {
                        action: {
                            window: aggregate_window_stats(values)
                            for window, values in ((payload.get("action_buckets") or {}).get(action) or {}).items()
                        }
                        for action in ("pullback", "breakout", "watch")
                    },
                    "sector_counts": {
                        action: dict(((payload.get("sector_counts") or {}).get(action) or {}))
                        for action in ("pullback", "breakout", "watch")
                    },
                }
                for code, payload in per_market.items()
            },
        }

    return get_or_set("template_eval_lightgbm", cache_key, ttl_seconds=600.0, loader=_loader)


def normalize_pattern_template_action(value: str | None) -> str:
    normalized = normalize_template_action(value)
    if normalized in {"buy_the_dip", "pullback"}:
        return "buy_the_dip"
    if normalized in {"wait_for_breakout", "breakout", "buy", "strong_buy"}:
        return "wait_for_breakout"
    if normalized in {"hold_and_watch", "watch", "hold", "wait", "monitor_only", "technical_pattern"}:
        return "hold_and_watch"
    return ""


def build_pattern_template_evaluation(
    *,
    template_key: str,
    market: str,
    lookback_snapshots: int = 15,
    top_n: int = 40,
) -> dict:
    target_markets = ["CN"] if str(market or "CN").upper() in {"ALL", "CN"} else [str(market or "CN").upper()]
    cache_key = json.dumps(
        {
            "template": template_key,
            "market": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _loader() -> dict:
        action_buckets: dict[str, dict[int, list[float]]] = {
            "buy_the_dip": {1: [], 3: [], 5: [], 10: []},
            "wait_for_breakout": {1: [], 3: [], 5: [], 10: []},
            "hold_and_watch": {1: [], 3: [], 5: [], 10: []},
        }
        sector_buckets: dict[str, dict[str, dict[int, list[float]]]] = {
            "buy_the_dip": {},
            "wait_for_breakout": {},
            "hold_and_watch": {},
        }
        sector_counts: dict[str, dict[str, int]] = {
            "buy_the_dip": {},
            "wait_for_breakout": {},
            "hold_and_watch": {},
        }
        samples_by_action: dict[str, list[dict]] = {
            "buy_the_dip": [],
            "wait_for_breakout": [],
            "hold_and_watch": [],
        }
        history_cache: dict[tuple[str, str], list[dict]] = {}
        symbol_meta_cache: dict[str, dict | None] = {}
        snapshot_total = 0
        labeled_snapshot_total = 0
        per_market: dict[str, dict] = {
            code: {
                "snapshot_total": 0,
                "labeled_snapshot_total": 0,
                "action_buckets": {
                    "buy_the_dip": {1: [], 3: [], 5: [], 10: []},
                    "wait_for_breakout": {1: [], 3: [], 5: [], 10: []},
                    "hold_and_watch": {1: [], 3: [], 5: [], 10: []},
                },
            }
            for code in target_markets
        }
        with SessionLocal() as db:
            snapshot_repo = WorkspaceSnapshotRepository(db)
            symbol_repo = SymbolRepository(db)
            for market_code in target_markets:
                params = build_base_precompute_params(
                    model_template=template_key,
                    universe="full_market",
                    market=market_code,
                )
                snapshots = snapshot_repo.list_snapshots(
                    screener_snapshot_type(params),
                    limit=max(1, int(lookback_snapshots)),
                )
                for snapshot in snapshots:
                    snapshot_total += 1
                    per_market[market_code]["snapshot_total"] += 1
                    payload = snapshot.get("payload") or {}
                    rows = list(payload.get("rows") or [])[: max(1, int(top_n))]
                    trade_date = str(snapshot.get("snapshot_date") or "")[:10]
                    if not trade_date:
                        continue
                    labeled = False
                    for row in rows:
                        action_key = normalize_pattern_template_action(row.get("action_label"))
                        if action_key not in action_buckets:
                            continue
                        labeled = True
                        ticker = str(row.get("ticker") or "").strip().upper()
                        if not ticker:
                            continue
                        if ticker not in symbol_meta_cache:
                            symbol_meta_cache[ticker] = symbol_repo.get_overview(ticker)
                        meta = symbol_meta_cache.get(ticker) or {}
                        sector_label = resolve_template_group_label(
                            meta=meta,
                            ticker=ticker,
                            market_code=market_code,
                            name=row.get("name"),
                        )
                        history_key = (market_code, ticker)
                        if history_key not in history_cache:
                            history_cache[history_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                        history = history_cache[history_key]
                        sample = {
                            "ticker": ticker,
                            "name": row.get("name") or ticker,
                            "market": market_code,
                            "sector": sector_label,
                            "trade_date": trade_date,
                            "return_1d": template_forward_return_from_history(history, trade_date=trade_date, sessions=1),
                            "return_3d": template_forward_return_from_history(history, trade_date=trade_date, sessions=3),
                            "return_5d": template_forward_return_from_history(history, trade_date=trade_date, sessions=5),
                            "return_10d": template_forward_return_from_history(history, trade_date=trade_date, sessions=10),
                        }
                        sector_counts[action_key][sector_label] = sector_counts[action_key].get(sector_label, 0) + 1
                        for window, key in ((1, "return_1d"), (3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                            value = sample.get(key)
                            if value is not None:
                                action_buckets[action_key][window].append(float(value))
                                per_market[market_code]["action_buckets"][action_key][window].append(float(value))
                                sector_bucket = sector_buckets[action_key].setdefault(sector_label, {1: [], 3: [], 5: [], 10: []})
                                sector_bucket[window].append(float(value))
                        if len(samples_by_action[action_key]) < 8:
                            samples_by_action[action_key].append(sample)
                    if labeled:
                        labeled_snapshot_total += 1
                        per_market[market_code]["labeled_snapshot_total"] += 1
        return {
            "template": template_key,
            "markets": target_markets,
            "lookback_snapshots": int(lookback_snapshots),
            "top_n": int(top_n),
            "snapshot_total": snapshot_total,
            "labeled_snapshot_total": labeled_snapshot_total,
            "windows": {
                action: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                for action, bucket in action_buckets.items()
            },
            "sector_windows": {
                action: {
                    sector: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                    for sector, bucket in sector_map.items()
                }
                for action, sector_map in sector_buckets.items()
            },
            "sector_counts": sector_counts,
            "samples": samples_by_action,
            "per_market": {
                code: {
                    "snapshot_total": int(payload.get("snapshot_total") or 0),
                    "labeled_snapshot_total": int(payload.get("labeled_snapshot_total") or 0),
                    "windows": {
                        action: {
                            window: aggregate_window_stats(values)
                            for window, values in ((payload.get("action_buckets") or {}).get(action) or {}).items()
                        }
                        for action in ("buy_the_dip", "wait_for_breakout", "hold_and_watch")
                    },
                }
                for code, payload in per_market.items()
            },
        }

    return get_or_set("template_eval_pattern", f"{template_key}:{cache_key}", ttl_seconds=600.0, loader=_loader)


def pattern_template_maturity(payload: dict, *, lang: str) -> dict:
    windows = (payload or {}).get("windows") or {}
    labeled_snapshot_total = int((payload or {}).get("labeled_snapshot_total") or 0)
    mature_5d = sum(int((((windows.get(action) or {}).get(5) or {}).get("count") or 0)) for action in ("buy_the_dip", "wait_for_breakout", "hold_and_watch"))
    if mature_5d >= 24 and labeled_snapshot_total >= 8:
        return {
            "level": "可比较" if lang == "zh" else "Comparable",
            "tone": "good",
            "summary": "样本已经能比较不同动作的后续表现。" if lang == "zh" else "Samples are now deep enough to compare post-signal behavior across actions.",
        }
    if mature_5d >= 10 and labeled_snapshot_total >= 4:
        return {
            "level": "初步参考" if lang == "zh" else "Early Read",
            "tone": "mid",
            "summary": "样本开始有参考意义，但还不适合下过强结论。" if lang == "zh" else "Samples are becoming informative, but still too thin for strong conclusions.",
        }
    return {
        "level": "观察期" if lang == "zh" else "Observation",
        "tone": "soft",
        "summary": "当前仍在积累样本，先把它当观察模块。" if lang == "zh" else "Samples are still accumulating, so use this as an observation module first.",
    }


def pattern_template_bias(payload: dict, *, lang: str) -> str:
    windows = (payload or {}).get("windows") or {}
    ranked = []
    labels = {
        "buy_the_dip": "回踩确认" if lang == "zh" else "Pullback",
        "wait_for_breakout": "突破确认" if lang == "zh" else "Breakout",
        "hold_and_watch": "观察等待" if lang == "zh" else "Watch",
    }
    for action in ("buy_the_dip", "wait_for_breakout", "hold_and_watch"):
        stats = (windows.get(action) or {}).get(5) or {}
        ranked.append((int(stats.get("count") or 0), float(stats.get("hit_rate") or 0.0), float(stats.get("avg_return") or 0.0), labels[action]))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    count, hit_rate, _avg_return, label = ranked[0]
    if count <= 0:
        return "样本观察中" if lang == "zh" else "Observation only"
    if lang == "zh":
        return f"当前 5D 更偏 {label}，命中率 {hit_rate:.1f}%"
    return f"5D currently leans {label} with a {hit_rate:.1f}% hit rate"


def build_lightgbm_prediction_evaluation(*, market: str, recent_runs: int = 8, top_n: int = 40) -> dict:
    target_markets = ["CN", "US"] if str(market or "ALL").upper() == "ALL" else [str(market or "CN").upper()]
    cache_key = json.dumps(
        {
            "template": "lightgbm_prediction_eval",
            "version": 1,
            "market": target_markets,
            "recent_runs": int(recent_runs),
            "top_n": int(top_n),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _loader() -> dict:
        action_buckets: dict[str, dict[int, list[float]]] = {
            "pullback": {1: [], 3: [], 5: []},
            "breakout": {1: [], 3: [], 5: []},
            "watch": {1: [], 3: [], 5: []},
        }
        per_market: dict[str, dict] = {
            code: {
                "windows": {
                    "pullback": {1: [], 3: [], 5: []},
                    "breakout": {1: [], 3: [], 5: []},
                    "watch": {1: [], 3: [], 5: []},
                },
                "sample_count": 0,
            }
            for code in target_markets
        }
        samples_by_action: dict[str, list[dict]] = {"pullback": [], "breakout": [], "watch": []}
        history_cache: dict[tuple[str, str], list[dict]] = {}
        sample_count = 0
        with SessionLocal() as db:
            runs_stmt = (
                select(ModelRun)
                .where(ModelRun.model_type == "lightgbm_multifactor", ModelRun.status == "success")
                .order_by(ModelRun.id.desc())
            )
            if len(target_markets) == 1:
                runs_stmt = runs_stmt.where(ModelRun.market == target_markets[0])
            run_rows = list(db.scalars(runs_stmt.limit(max(1, int(recent_runs)))))
            run_ids = [int(run.id) for run in run_rows]
            if not run_ids:
                return {
                    "markets": target_markets,
                    "run_count": 0,
                    "sample_count": 0,
                    "latest_trade_date": None,
                    "windows": {action: {window: aggregate_window_stats([]) for window in (1, 3, 5)} for action in action_buckets},
                    "samples": samples_by_action,
                    "per_market": {
                        code: {
                            "sample_count": 0,
                            "windows": {action: {window: aggregate_window_stats([]) for window in (1, 3, 5)} for action in action_buckets},
                        }
                        for code in target_markets
                    },
                }

            date_stmt = (
                select(Prediction.model_run_id, Prediction.trade_date)
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .where(Prediction.model_run_id.in_(run_ids))
                .distinct()
                .order_by(Prediction.model_run_id.desc(), Prediction.trade_date.desc())
            )
            if len(target_markets) == 1:
                date_stmt = date_stmt.where(Symbol.market == target_markets[0])
            selected_dates_by_run: dict[int, list[str]] = defaultdict(list)
            for selected_run_id, selected_trade_date in db.execute(date_stmt).all():
                bucket = selected_dates_by_run[int(selected_run_id)]
                if len(bucket) < 3:
                    bucket.append(str(selected_trade_date))
            selected_dates = sorted(
                {
                    trade_date
                    for trade_dates in selected_dates_by_run.values()
                    for trade_date in trade_dates
                },
                reverse=True,
            )
            if not selected_dates:
                return {
                    "markets": target_markets,
                    "run_count": len(run_ids),
                    "sample_count": 0,
                    "latest_trade_date": None,
                    "windows": {action: {window: aggregate_window_stats([]) for window in (1, 3, 5)} for action in action_buckets},
                    "samples": samples_by_action,
                    "per_market": {
                        code: {
                            "sample_count": 0,
                            "windows": {action: {window: aggregate_window_stats([]) for window in (1, 3, 5)} for action in action_buckets},
                        }
                        for code in target_markets
                    },
                }

            stmt = (
                select(Prediction, PredictionDetail, Symbol, ModelRun)
                .join(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .join(ModelRun, ModelRun.id == Prediction.model_run_id)
                .where(Prediction.model_run_id.in_(run_ids))
                .where(Prediction.trade_date.in_(selected_dates))
                .order_by(ModelRun.id.desc(), Prediction.trade_date.desc(), Prediction.rank_value.asc(), Prediction.score.desc())
            )
            if len(target_markets) == 1:
                stmt = stmt.where(Symbol.market == target_markets[0])
            rows = db.execute(stmt).all()

        grouped: dict[tuple[int, str], list] = defaultdict(list)
        for row in rows:
            prediction, detail, symbol, model_run = row
            grouped[(int(model_run.id), str(prediction.trade_date))].append(row)

        latest_trade_date = None
        for (run_id, trade_date), bucket in grouped.items():
            selected = sorted(
                bucket,
                key=lambda item: (
                    float((item[0].rank_value if item[0].rank_value is not None else 999999.0)),
                    -float(item[0].score or 0.0),
                    str(item[2].ticker or ""),
                ),
            )[: max(1, int(top_n))]
            latest_trade_date = max(latest_trade_date or trade_date, trade_date)
            for prediction, detail, symbol, model_run in selected:
                action_key = normalize_lightgbm_prediction_action(
                    entry_style=detail.entry_style,
                    signal_label=detail.signal_label,
                )
                if action_key not in action_buckets:
                    continue
                ticker = str(symbol.ticker or "").strip().upper()
                market_code = str(symbol.market or model_run.market or "").upper() or "CN"
                history_key = (market_code, ticker)
                if history_key not in history_cache:
                    history_cache[history_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                history = history_cache[history_key]
                sample = {
                    "ticker": ticker,
                    "name": symbol.name or ticker,
                    "market": market_code,
                    "trade_date": str(prediction.trade_date),
                    "run_id": int(model_run.id),
                    "entry_style": detail.entry_style,
                    "signal_label": detail.signal_label,
                    "return_1d": template_forward_return_from_history(history, trade_date=str(prediction.trade_date), sessions=1),
                    "return_3d": template_forward_return_from_history(history, trade_date=str(prediction.trade_date), sessions=3),
                    "return_5d": template_forward_return_from_history(history, trade_date=str(prediction.trade_date), sessions=5),
                }
                sample_count += 1
                per_market.setdefault(market_code, {"windows": {key: {1: [], 3: [], 5: []} for key in action_buckets}, "sample_count": 0})
                per_market[market_code]["sample_count"] = int(per_market[market_code].get("sample_count") or 0) + 1
                for window, key in ((1, "return_1d"), (3, "return_3d"), (5, "return_5d")):
                    value = sample.get(key)
                    if value is None:
                        continue
                    action_buckets[action_key][window].append(float(value))
                    ((per_market[market_code].get("windows") or {}).get(action_key) or {}).get(window, []).append(float(value))
                if len(samples_by_action[action_key]) < 8:
                    samples_by_action[action_key].append(sample)

        return {
            "markets": target_markets,
            "run_count": len(run_ids),
            "sample_count": sample_count,
            "latest_trade_date": latest_trade_date,
            "windows": {
                action: {window: aggregate_window_stats(values) for window, values in bucket.items()}
                for action, bucket in action_buckets.items()
            },
            "samples": samples_by_action,
            "per_market": {
                code: {
                    "sample_count": int(payload.get("sample_count") or 0),
                    "windows": {
                        action: {
                            window: aggregate_window_stats(values)
                            for window, values in ((payload.get("windows") or {}).get(action) or {}).items()
                        }
                        for action in ("pullback", "breakout", "watch")
                    },
                }
                for code, payload in per_market.items()
            },
        }

    return get_or_set("template_eval_lightgbm_prediction", cache_key, ttl_seconds=600.0, loader=_loader)


def technical_momentum_maturity(payload: dict, *, lang: str) -> dict:
    windows = (payload or {}).get("windows") or {}
    labeled_snapshot_total = int((payload or {}).get("labeled_snapshot_total") or 0)
    buy_5 = int((((windows.get("buy") or {}).get(5) or {}).get("count") or 0))
    watch_5 = int((((windows.get("watch") or {}).get(5) or {}).get("count") or 0))
    mature_5d = buy_5 + watch_5
    if mature_5d >= 40 and labeled_snapshot_total >= 8:
        return {"level": "可比较" if lang == "zh" else "Comparable", "tone": "good", "summary": "动量模板已有可比较样本，可开始对比 BUY 与 WATCH 的后续表现。" if lang == "zh" else "Momentum template now has enough samples to compare BUY and WATCH follow-through."}
    if mature_5d >= 16 and labeled_snapshot_total >= 4:
        return {"level": "初步参考" if lang == "zh" else "Early Read", "tone": "mid", "summary": "动量模板开始具备参考意义，但还不适合下太强结论。" if lang == "zh" else "Momentum samples are becoming informative, but still too thin for strong conclusions."}
    return {"level": "观察期" if lang == "zh" else "Observation", "tone": "soft", "summary": "动量模板仍处于样本沉淀期，先看作观察看板。" if lang == "zh" else "Momentum template is still in sample-accumulation mode."}


def technical_momentum_bias(payload: dict, *, lang: str) -> str:
    windows = (payload or {}).get("windows") or {}
    buy_5 = (windows.get("buy") or {}).get(5) or {}
    watch_5 = (windows.get("watch") or {}).get(5) or {}
    buy_count = int(buy_5.get("count") or 0)
    watch_count = int(watch_5.get("count") or 0)
    buy_hit = float(buy_5.get("hit_rate") or 0.0)
    watch_hit = float(watch_5.get("hit_rate") or 0.0)
    if buy_count <= 0 and watch_count <= 0:
        return "样本观察中" if lang == "zh" else "Observation only"
    if buy_count > 0 and watch_count <= 0:
        return "偏直接跟随" if lang == "zh" else "Leaning BUY"
    if watch_count > 0 and buy_count <= 0:
        return "偏先观察" if lang == "zh" else "Leaning WATCH"
    if buy_hit >= watch_hit + 5:
        return "偏直接跟随" if lang == "zh" else "Leaning BUY"
    if watch_hit >= buy_hit + 5:
        return "偏先观察" if lang == "zh" else "Leaning WATCH"
    return "两类并存" if lang == "zh" else "Mixed"


def lightgbm_maturity(payload: dict, *, lang: str) -> dict:
    windows = (payload or {}).get("windows") or {}
    labeled_snapshot_total = int((payload or {}).get("labeled_snapshot_total") or 0)
    pullback_5 = int((((windows.get("pullback") or {}).get(5) or {}).get("count") or 0))
    breakout_5 = int((((windows.get("breakout") or {}).get(5) or {}).get("count") or 0))
    mature_5d = pullback_5 + breakout_5
    if mature_5d >= 30 and labeled_snapshot_total >= 8:
        return {"level": "可比较" if lang == "zh" else "Comparable", "tone": "good", "summary": "LightGBM 近期已有足够动作样本，可以开始比较回踩与突破两类执行风格。" if lang == "zh" else "LightGBM now has enough action-labeled samples to compare pullback versus breakout styles."}
    if mature_5d >= 12 and labeled_snapshot_total >= 4:
        return {"level": "初步参考" if lang == "zh" else "Early Read", "tone": "mid", "summary": "LightGBM 的动作样本开始具备参考意义，但暂时还不适合下过强结论。" if lang == "zh" else "LightGBM samples are becoming informative, but still too thin for strong conclusions."}
    return {"level": "观察期" if lang == "zh" else "Observation", "tone": "soft", "summary": "LightGBM 仍处于样本沉淀期，先把这块当作观察面板。" if lang == "zh" else "LightGBM is still in sample-accumulation mode."}


def lightgbm_bias(payload: dict, *, lang: str) -> str:
    windows = (payload or {}).get("windows") or {}
    pullback_5 = (windows.get("pullback") or {}).get(5) or {}
    breakout_5 = (windows.get("breakout") or {}).get(5) or {}
    pullback_count = int(pullback_5.get("count") or 0)
    breakout_count = int(breakout_5.get("count") or 0)
    pullback_hit = float(pullback_5.get("hit_rate") or 0.0)
    breakout_hit = float(breakout_5.get("hit_rate") or 0.0)
    if pullback_count <= 0 and breakout_count <= 0:
        return "样本观察中" if lang == "zh" else "Observation only"
    if pullback_count > 0 and breakout_count <= 0:
        return "偏回踩布局" if lang == "zh" else "Leaning Pullback"
    if breakout_count > 0 and pullback_count <= 0:
        return "偏突破确认" if lang == "zh" else "Leaning Breakout"
    if pullback_hit >= breakout_hit + 5:
        return "偏回踩布局" if lang == "zh" else "Leaning Pullback"
    if breakout_hit >= pullback_hit + 5:
        return "偏突破确认" if lang == "zh" else "Leaning Breakout"
    return "两类并存" if lang == "zh" else "Mixed"
