"""Fail-closed regime diagnostics for screener snapshots; never model approval."""
from __future__ import annotations

from copy import deepcopy

from app.services.stock_selection.regime_policy import evaluate_regime_policy


def build_screener_regime_diagnostics(
    rows: list[dict],
    *,
    market: str,
    input_market_date: str | None,
    decision_cutoff_at: str,
    regime_snapshot: dict | None,
) -> dict:
    market_code = str(market or "").strip().upper()
    if market_code != "CN":
        return {
            "schema_version": "screener_regime_diagnostics_v1",
            "status": "NOT_ENABLED",
            "market": market_code or None,
            "input_count": len(rows),
            "observation_count": len(rows),
            "regime_shortlist_count": 0,
            "regime_shortlist_tickers": [],
            "formal_candidate_count": 0,
            "formal_candidate_blocker": "cn_first_regime_rollout_not_enabled_for_market",
            "candidate_semantics": "research_observation_not_trade_authorization",
        }
    policy = evaluate_regime_policy(
        regime_snapshot,
        market="CN",
        expected_market_date=str(input_market_date or ""),
        decision_cutoff_at=decision_cutoff_at,
        max_new_candidates=5,
    )
    eligible = []
    excluded_bj = 0
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker.endswith(".BJ"):
            excluded_bj += 1
            continue
        status = str(row.get("tradability_status") or "").strip().upper()
        if status == "READY" and row.get("is_tradable") is True:
            eligible.append(row)
    shortlist = eligible[: int(policy["max_new_candidates"])]
    return {
        "schema_version": "screener_regime_diagnostics_v1",
        "status": "READY" if policy["buy_gate"] != "BLOCK" else "BLOCKED",
        "market": "CN",
        "input_market_date": input_market_date,
        "input_count": len(rows),
        "observation_count": len(rows) - excluded_bj,
        "excluded_bj_count": excluded_bj,
        "tradability_ready_count": len(eligible),
        "regime_shortlist_count": len(shortlist),
        "regime_shortlist_tickers": [str(row.get("ticker") or "").strip().upper() for row in shortlist],
        "formal_candidate_count": 0,
        "formal_candidate_blocker": "screener_does_not_grant_model_qualification",
        "candidate_semantics": "research_observation_not_trade_authorization",
        "regime_policy": deepcopy(policy),
    }


__all__ = ["build_screener_regime_diagnostics"]
