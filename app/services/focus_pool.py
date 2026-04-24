from __future__ import annotations

import json
from datetime import date

from app.core.db import SessionLocal
from app.services.repository import AppSettingRepository, SymbolRepository


def today_focus_key(target_date: str | None = None) -> str:
    return f"today_focus_pool:{target_date or date.today().isoformat()}"


def load_today_focus_pool(target_date: str | None = None) -> list[dict]:
    with SessionLocal() as db:
        raw = AppSettingRepository(db).get(today_focus_key(target_date))
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        normalized: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            normalized.append(item)
        return normalized


def save_today_focus_pool(items: list[dict], target_date: str | None = None) -> None:
    with SessionLocal() as db:
        AppSettingRepository(db).set(today_focus_key(target_date), json.dumps(items, ensure_ascii=False))


def add_to_today_focus_pool(rows: list[dict], *, top_n: int = 0, target_date: str | None = None) -> dict:
    selected = rows[:top_n] if top_n and top_n > 0 else rows
    existing = load_today_focus_pool(target_date)
    existing_map = {str(item.get("ticker") or "").upper(): item for item in existing}
    added = 0
    for row in selected:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        payload = {
            "ticker": ticker,
            "name": row.get("name"),
            "market": row.get("market"),
            "selection_reason": row.get("selection_reason"),
            "model_signal_label": row.get("model_signal_label"),
            "model_signal_strength": row.get("model_signal_strength"),
            "matched_patterns": row.get("matched_patterns") or [],
            "added_on": target_date or date.today().isoformat(),
        }
        if ticker not in existing_map:
            added += 1
        existing_map[ticker] = payload
    save_today_focus_pool(list(existing_map.values()), target_date)
    return {"added": added, "total": len(existing_map)}


def enrich_focus_pool_with_symbols(items: list[dict]) -> list[dict]:
    tickers = [str(item.get("ticker") or "").upper() for item in items if item.get("ticker")]
    if not tickers:
        return items
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        symbol_map = {ticker: symbol_repo.get_by_ticker(ticker) for ticker in tickers}
    enriched: list[dict] = []
    for item in items:
        ticker = str(item.get("ticker") or "").upper()
        symbol = symbol_map.get(ticker)
        enriched.append(
            {
                **item,
                "name": item.get("name") or (symbol.name if symbol is not None else ticker),
                "market": item.get("market") or (symbol.market if symbol is not None else None),
            }
        )
    return enriched
