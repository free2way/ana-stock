from __future__ import annotations

import json

from app.core.db import SessionLocal
from app.services.repository import AppSettingRepository


PORTFOLIO_BOOK_KEY = "portfolio_book"


def load_portfolio_positions() -> list[dict]:
    with SessionLocal() as db:
        raw = AppSettingRepository(db).get(PORTFOLIO_BOOK_KEY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    positions: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        positions.append(
            {
                "ticker": ticker,
                "name": item.get("name"),
                "market": item.get("market"),
                "quantity": float(item.get("quantity") or 0.0),
                "cost_basis": float(item.get("cost_basis") or 0.0),
                "note": item.get("note") or "",
            }
        )
    return positions


def save_portfolio_positions(positions: list[dict]) -> None:
    with SessionLocal() as db:
        AppSettingRepository(db).set(PORTFOLIO_BOOK_KEY, json.dumps(positions, ensure_ascii=False))


def upsert_portfolio_position(payload: dict) -> list[dict]:
    positions = load_portfolio_positions()
    ticker = str(payload.get("ticker") or "").strip().upper()
    updated: list[dict] = []
    replaced = False
    for item in positions:
        if item["ticker"] == ticker:
            updated.append(
                {
                    "ticker": ticker,
                    "name": payload.get("name") or item.get("name"),
                    "market": payload.get("market") or item.get("market"),
                    "quantity": float(payload.get("quantity") or 0.0),
                    "cost_basis": float(payload.get("cost_basis") or 0.0),
                    "note": payload.get("note") or "",
                }
            )
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(
            {
                "ticker": ticker,
                "name": payload.get("name"),
                "market": payload.get("market"),
                "quantity": float(payload.get("quantity") or 0.0),
                "cost_basis": float(payload.get("cost_basis") or 0.0),
                "note": payload.get("note") or "",
            }
        )
    save_portfolio_positions(updated)
    return updated


def remove_portfolio_position(ticker: str) -> list[dict]:
    normalized = str(ticker or "").strip().upper()
    positions = [item for item in load_portfolio_positions() if item["ticker"] != normalized]
    save_portfolio_positions(positions)
    return positions
