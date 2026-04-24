from __future__ import annotations

import json

from app.core.db import SessionLocal
from app.services.repository import AppSettingRepository
from app.services.time_utils import app_now_iso, app_today_iso


PORTFOLIO_BOOK_KEY = "portfolio_book"
PORTFOLIO_TRADE_LOG_KEY = "portfolio_trade_log"
SELL_REASON_OPTIONS = [
    ("止盈/保护利润", "Take profit / protect gains"),
    ("止损/风险收缩", "Stop loss / reduce risk"),
    ("调仓", "Rebalance"),
    ("复核后卖出", "Review-led exit"),
    ("事件风险", "Event risk"),
    ("其他", "Other"),
]
_SELL_REASON_LABELS_EN = {label: en_label for label, en_label in SELL_REASON_OPTIONS}
_SELL_REASON_BUCKETS = {
    "止盈/保护利润": "profit_protection",
    "止损/风险收缩": "risk_reduction",
    "调仓": "rebalance",
    "复核后卖出": "review",
    "事件风险": "event_risk",
    "其他": "other",
}


def normalize_trade_reason(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "其他"
    lowered = text.lower()
    if text == "事件风险" or any(token in lowered for token in ("event", "earnings", "news risk", "macro")):
        return "事件风险"
    if text == "止盈/保护利润" or any(token in lowered for token in ("止盈", "trim", "profit", "take profit", "protect")):
        return "止盈/保护利润"
    if text == "止损/风险收缩" or any(token in lowered for token in ("止损", "stop", "risk", "cut loss")):
        return "止损/风险收缩"
    if text == "调仓" or any(token in lowered for token in ("调仓", "rebalance", "rotate", "rotation")):
        return "调仓"
    if text == "复核后卖出" or any(token in lowered for token in ("复核", "review", "观察", "watch")):
        return "复核后卖出"
    if text in _SELL_REASON_LABELS_EN.values():
        for label, en_label in _SELL_REASON_LABELS_EN.items():
            if text == en_label:
                return label
    return "其他"


def trade_reason_bucket(value: str | None) -> str:
    return _SELL_REASON_BUCKETS.get(normalize_trade_reason(value), "other")


def trade_reason_label(value: str | None, *, lang: str = "zh") -> str:
    normalized = normalize_trade_reason(value)
    if lang == "zh":
        return normalized
    return _SELL_REASON_LABELS_EN.get(normalized, normalized)


def suggest_trade_reason(item: dict | None) -> str:
    row = item or {}
    note_text = str(row.get("note") or "").strip().lower()
    action_hint = str(row.get("action_hint_at_exit") or "").strip().lower()
    action_reason = str(row.get("action_reason_at_exit") or "").strip().lower()
    rebalance_action = str(row.get("rebalance_action_at_exit") or "").strip().lower()
    remaining_quantity = float(row.get("remaining_quantity") or 0.0)
    realized_pnl = float(row.get("realized_pnl") or 0.0)
    realized_return_pct = row.get("realized_return_pct")
    if realized_return_pct is None:
        realized_return_pct = row.get("realized_pnl_pct")
    realized_return_pct = float(realized_return_pct or 0.0)

    event_tokens = ("event", "earnings", "macro", "news", "财报", "事件", "公告")
    if any(token in text for token in event_tokens for text in (note_text, action_reason)):
        return "事件风险"
    if any(token in text for token in ("review", "watch", "复核", "观察") for text in (action_hint, action_reason)):
        return "复核后卖出"
    if remaining_quantity > 0:
        return "调仓"
    if any(token in text for token in ("rebalance", "rotate", "调仓", "仓位") for text in (action_hint, action_reason, rebalance_action, note_text)):
        return "调仓"
    if realized_pnl < 0 or realized_return_pct < 0:
        return "止损/风险收缩"
    if realized_pnl > 0 or realized_return_pct > 0:
        return "止盈/保护利润"
    return "调仓"


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
                "reason": normalize_trade_reason(item.get("reason")),
                "note": item.get("note") or "",
                "created_at": item.get("created_at"),
                "remaining_quantity": float(item.get("remaining_quantity") or 0.0),
                "audit_snapshot_at": item.get("audit_snapshot_at"),
                "action_hint_at_exit": item.get("action_hint_at_exit") or "",
                "action_priority_at_exit": item.get("action_priority_at_exit") or "",
                "action_reason_at_exit": item.get("action_reason_at_exit") or "",
                "rebalance_action_at_exit": item.get("rebalance_action_at_exit") or "",
                "risk_tag_at_exit": item.get("risk_tag_at_exit") or "",
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

    normalized_reason = normalize_trade_reason(payload.get("reason"))
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
        "reason": normalized_reason,
        "note": payload.get("note") or "",
        "created_at": app_now_iso(),
        "remaining_quantity": remaining_quantity,
        "audit_snapshot_at": app_now_iso(),
        "action_hint_at_exit": payload.get("action_hint_at_exit") or "",
        "action_priority_at_exit": payload.get("action_priority_at_exit") or "",
        "action_reason_at_exit": payload.get("action_reason_at_exit") or "",
        "rebalance_action_at_exit": payload.get("rebalance_action_at_exit") or "",
        "risk_tag_at_exit": payload.get("risk_tag_at_exit") or "",
    }
    trades.append(trade)
    save_portfolio_trades(trades)
    return {
        "trade": trade,
        "positions": updated_positions,
        "closed": remaining_quantity <= 0,
    }


def update_portfolio_trade_reason(trade_id: int | str, reason: str | None) -> list[dict]:
    normalized_id = int(trade_id)
    trades = load_portfolio_trades()
    updated: list[dict] = []
    for item in trades:
        current_id = int(item.get("id") or 0)
        if current_id == normalized_id:
            updated.append({**item, "reason": normalize_trade_reason(reason)})
        else:
            updated.append(item)
    save_portfolio_trades(updated)
    return updated


def apply_suggested_trade_reasons(*, only_missing: bool = True) -> dict:
    trades = load_portfolio_trades()
    updated = []
    changed = 0
    for item in trades:
        current = normalize_trade_reason(item.get("reason"))
        if only_missing and current != "其他":
            updated.append(item)
            continue
        suggested = suggest_trade_reason(item)
        next_row = {**item, "reason": suggested}
        if suggested != current:
            changed += 1
        updated.append(next_row)
    save_portfolio_trades(updated)
    return {
        "total": len(trades),
        "changed": changed,
    }


def _historical_audit_seed(reason: str | None) -> dict[str, str]:
    normalized = normalize_trade_reason(reason)
    if normalized == "止盈/保护利润":
        return {
            "action_hint_at_exit": "TRIM",
            "action_priority_at_exit": "historical_backfill",
            "action_reason_at_exit": "历史补录：依据结构化卖出原因推断，当时更可能偏向锁定利润。",
            "rebalance_action_at_exit": "锁定部分利润 / 压缩追涨仓位",
            "risk_tag_at_exit": "历史补录",
            "audit_snapshot_source": "historical_backfill",
        }
    if normalized == "止损/风险收缩":
        return {
            "action_hint_at_exit": "EXIT",
            "action_priority_at_exit": "historical_backfill",
            "action_reason_at_exit": "历史补录：依据结构化卖出原因推断，当时更可能偏向风险收缩。",
            "rebalance_action_at_exit": "降低风险暴露 / 退出弱势仓位",
            "risk_tag_at_exit": "历史补录",
            "audit_snapshot_source": "historical_backfill",
        }
    if normalized == "复核后卖出":
        return {
            "action_hint_at_exit": "REVIEW",
            "action_priority_at_exit": "historical_backfill",
            "action_reason_at_exit": "历史补录：依据结构化卖出原因推断，当时更可能由人工复核主导。",
            "rebalance_action_at_exit": "复核后退出 / 重新评估持仓逻辑",
            "risk_tag_at_exit": "历史补录",
            "audit_snapshot_source": "historical_backfill",
        }
    if normalized == "事件风险":
        return {
            "action_hint_at_exit": "EXIT",
            "action_priority_at_exit": "historical_backfill",
            "action_reason_at_exit": "历史补录：依据结构化卖出原因推断，当时更可能在事件风险前后收缩仓位。",
            "rebalance_action_at_exit": "事件前减仓 / 规避不确定性",
            "risk_tag_at_exit": "事件风险",
            "audit_snapshot_source": "historical_backfill",
        }
    return {
        "action_hint_at_exit": "TRIM",
        "action_priority_at_exit": "historical_backfill",
        "action_reason_at_exit": "历史补录：依据结构化卖出原因推断，当时更可能偏向调仓处理。",
        "rebalance_action_at_exit": "仓位再平衡 / 调仓处理",
        "risk_tag_at_exit": "历史补录",
        "audit_snapshot_source": "historical_backfill",
    }


def backfill_trade_audit_snapshot(trade_id: int | str) -> list[dict]:
    normalized_id = int(trade_id)
    trades = load_portfolio_trades()
    updated: list[dict] = []
    for item in trades:
        current_id = int(item.get("id") or 0)
        if current_id != normalized_id:
            updated.append(item)
            continue
        seed = _historical_audit_seed(item.get("reason"))
        updated.append(
            {
                **item,
                **seed,
                "audit_snapshot_at": item.get("audit_snapshot_at") or app_now_iso(),
            }
        )
    save_portfolio_trades(updated)
    return updated


def backfill_trade_audit_snapshots(*, only_missing: bool = True) -> dict:
    trades = load_portfolio_trades()
    updated: list[dict] = []
    changed = 0
    for item in trades:
        has_snapshot = bool(str(item.get("audit_snapshot_at") or "").strip()) or bool(
            str(item.get("action_hint_at_exit") or "").strip() or str(item.get("action_reason_at_exit") or "").strip()
        )
        if only_missing and has_snapshot:
            updated.append(item)
            continue
        seed = _historical_audit_seed(item.get("reason"))
        updated.append(
            {
                **item,
                **seed,
                "audit_snapshot_at": item.get("audit_snapshot_at") or app_now_iso(),
            }
        )
        changed += 1
    save_portfolio_trades(updated)
    return {"total": len(trades), "changed": changed}
