from __future__ import annotations

import json

from app.core.db import SessionLocal
from app.services.repository import AppSettingRepository
from app.services.time_utils import app_now_iso, app_today_iso


PORTFOLIO_BOOK_KEY = "portfolio_book"
PORTFOLIO_TRADE_LOG_KEY = "portfolio_trade_log"


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


def load_portfolio_trades() -> list[dict]:
    with SessionLocal() as db:
        raw = AppSettingRepository(db).get(PORTFOLIO_TRADE_LOG_KEY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    trades: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        trades.append(
            {
                "id": item.get("id"),
                "side": item.get("side") or "SELL",
                "ticker": ticker,
                "name": item.get("name"),
                "market": item.get("market"),
                "quantity": float(item.get("quantity") or 0.0),
                "price": float(item.get("price") or 0.0),
                "cost_basis": float(item.get("cost_basis") or 0.0),
                "fee": float(item.get("fee") or 0.0),
                "gross_amount": float(item.get("gross_amount") or 0.0),
                "cost_amount": float(item.get("cost_amount") or 0.0),
                "realized_pnl": float(item.get("realized_pnl") or 0.0),
                "realized_pnl_pct": float(item.get("realized_pnl_pct") or 0.0),
                "trade_date": item.get("trade_date"),
                "reason": item.get("reason") or "",
                "note": item.get("note") or "",
                "created_at": item.get("created_at"),
                "remaining_quantity": float(item.get("remaining_quantity") or 0.0),
            }
        )
    return trades


def save_portfolio_trades(trades: list[dict]) -> None:
    with SessionLocal() as db:
        AppSettingRepository(db).set(PORTFOLIO_TRADE_LOG_KEY, json.dumps(trades, ensure_ascii=False))


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


def sell_portfolio_position(payload: dict) -> dict:
    ticker = str(payload.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("Ticker is required.")
    sell_quantity = float(payload.get("quantity") or 0.0)
    sell_price = float(payload.get("price") or 0.0)
    fee = max(0.0, float(payload.get("fee") or 0.0))
    if sell_quantity <= 0:
        raise ValueError("Sell quantity must be greater than zero.")
    if sell_price <= 0:
        raise ValueError("Sell price must be greater than zero.")

    positions = load_portfolio_positions()
    target = next((item for item in positions if item["ticker"] == ticker), None)
    if target is None:
        raise ValueError(f"No position found for {ticker}.")
    current_quantity = float(target.get("quantity") or 0.0)
    if sell_quantity > current_quantity:
        raise ValueError(f"Sell quantity {sell_quantity:g} exceeds current holding {current_quantity:g}.")

    cost_basis = float(target.get("cost_basis") or 0.0)
    gross_amount = sell_quantity * sell_price
    cost_amount = sell_quantity * cost_basis
    realized_pnl = gross_amount - cost_amount - fee
    realized_pnl_pct = ((sell_price / cost_basis) - 1.0) * 100.0 if cost_basis else 0.0
    remaining_quantity = current_quantity - sell_quantity

    updated_positions: list[dict] = []
    for item in positions:
        if item["ticker"] != ticker:
            updated_positions.append(item)
            continue
        if remaining_quantity > 0:
            updated_positions.append(
                {
                    **item,
                    "quantity": remaining_quantity,
                }
            )
    save_portfolio_positions(updated_positions)

    trades = load_portfolio_trades()
    trade = {
        "id": (max([int(item.get("id") or 0) for item in trades], default=0) + 1),
        "side": "SELL",
        "ticker": ticker,
        "name": target.get("name"),
        "market": target.get("market"),
        "quantity": sell_quantity,
        "price": sell_price,
        "cost_basis": cost_basis,
        "fee": fee,
        "gross_amount": gross_amount,
        "cost_amount": cost_amount,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
        "trade_date": str(payload.get("trade_date") or "").strip() or app_today_iso(),
        "reason": payload.get("reason") or "",
        "note": payload.get("note") or "",
        "created_at": app_now_iso(),
        "remaining_quantity": remaining_quantity,
    }
    trades.append(trade)
    save_portfolio_trades(trades)
    return {
        "trade": trade,
        "positions": updated_positions,
        "closed": remaining_quantity <= 0,
    }
