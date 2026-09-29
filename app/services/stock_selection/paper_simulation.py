"""Reproducible, market-isolated simulation of an already frozen decision."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time
import json

from sqlalchemy import select

from app.models.tables import CNPaperSimulation, USPaperSimulation
from app.services.backtesting import DailyBar, EngineConfig, EventDrivenDailyEngine, MarketCorporateAction, SignalCandidate
from app.services.json_payload_artifacts import JsonPayloadArtifactStore, payload_digest
from app.services.market_calendar import market_timezone, next_market_open_date
from app.services.stock_selection.data_contracts import validate_market_ticker
from app.services.stock_selection.protocol import ExecutableSelectionProtocol


PAPER_MODELS = {"CN": CNPaperSimulation, "US": USPaperSimulation}
MARKET_CURRENCIES = {"CN": "CNY", "US": "USD"}


def run_frozen_decision_paper_simulation(
    *, decision_id: str, decision_payload: dict,
    protocol: ExecutableSelectionProtocol, bars: list[DailyBar],
    calendar_sessions: list[str], corporate_actions: list[MarketCorporateAction] | None = None,
    db=None, store: JsonPayloadArtifactStore | None = None,
) -> dict:
    protocol.require_approved()
    market = protocol.market.upper()
    if market not in PAPER_MODELS:
        raise ValueError("paper simulation requires an independently versioned CN or US market calendar and rules")
    details = (decision_payload.get("markets") or {}).get(market)
    if not isinstance(details, dict):
        raise ValueError("frozen decision does not include protocol market")
    if str(details.get("protocol_id") or "") != protocol.protocol_id:
        raise ValueError("frozen decision protocol does not match simulation protocol")
    if not decision_id or not str(decision_id).startswith(f"decision:{market}:"):
        raise ValueError("market-specific decision_id is required")
    if not calendar_sessions or len(calendar_sessions) < 2:
        raise ValueError("explicit market calendar requires at least two sessions")
    signal_date = str(details.get("input_market_date") or "")[:10]
    date.fromisoformat(signal_date)
    if signal_date not in calendar_sessions:
        raise ValueError("decision signal date is not in explicit calendar")
    for bar in bars:
        validate_market_ticker(market, bar.ticker)
        if bar.price_basis != "raw":
            raise ValueError("executable paper simulation requires raw OHLCV and separate corporate actions")
    for action in corporate_actions or []:
        validate_market_ticker(market, action.ticker)
    raw_candidates = details.get("candidates") or []
    if len(raw_candidates) > protocol.top_n:
        raise ValueError("frozen candidate count exceeds protocol top_n")
    if details.get("decision") != "CANDIDATES" and raw_candidates:
        raise ValueError("non-candidate decision must not contain executable names")
    frozen_at = datetime.fromisoformat(str(decision_payload.get("frozen_at") or "").replace("Z", "+00:00"))
    if frozen_at.tzinfo is None or frozen_at.utcoffset() is None:
        raise ValueError("frozen_at must be timezone-aware")
    effective_date = next_market_open_date(market, signal_date, include_self=False)
    planned_open = datetime.combine(date.fromisoformat(effective_date), time(9, 30), tzinfo=market_timezone(market))
    late = frozen_at >= planned_open
    signals: list[SignalCandidate] = []
    if not late and details.get("decision") == "CANDIDATES":
        seen: set[str] = set()
        for ordinal, candidate in enumerate(raw_candidates, start=1):
            ticker = validate_market_ticker(market, candidate.get("ticker"))
            if ticker in seen:
                raise ValueError("duplicate ticker in frozen decision")
            seen.add(ticker)
            signals.append(SignalCandidate(
                signal_date=signal_date, ticker=ticker,
                score=float(candidate.get("model_score") or candidate.get("quant_rank") or 0),
                ordinal=ordinal, insight_id=f"{decision_id}:{ordinal}",
                sector=str(candidate.get("sector") or "") or None,
                qualified=True,
            ))
    engine = EventDrivenDailyEngine(EngineConfig(
        market=market, top_n=protocol.top_n, holding_days=protocol.horizon_days,
        initial_cash=protocol.initial_cash,
        commission_bps=protocol.commission_bps_one_way,
        slippage_bps=protocol.slippage_bps_one_way,
        max_position_weight=protocol.max_position_weight,
        max_sector_weight=protocol.max_sector_weight,
        max_gross_exposure=protocol.max_gross_exposure,
        max_participation_rate=protocol.max_participation_rate,
        liquidate_at_end=False, calendar_version=protocol.calendar_version,
        engine_version=protocol.engine_version,
    ))
    result = engine.run(
        bars=bars, signals=signals, calendar_sessions=calendar_sessions,
        corporate_actions=corporate_actions or [],
    )
    outcomes = list(result.outcomes)
    summary = {
        "status": "LATE_CUTOFF" if late else "ABSTAIN" if not raw_candidates else
                  "INCOMPLETE" if any(row["status"] == "EXIT_DEFERRED" for row in outcomes) else
                  "PENDING" if any(row["status"] == "PENDING" for row in outcomes) else
                  "MATURED" if any(row["status"] == "matured" for row in outcomes) else "NO_FILLS",
        "decision_id": decision_id, "market": market, "currency": MARKET_CURRENCIES[market],
        "protocol_id": protocol.protocol_id,
        "candidate_count": len(raw_candidates), "signal_count": len(signals),
        "fill_count": len(result.fills), "reject_count": len(result.rejects),
        "matured_lot_count": sum(row["status"] == "matured" for row in outcomes),
        "open_lot_count": result.open_position_count,
        "end_nav": result.end_nav, "initial_cash": result.initial_cash,
        "gate_stats": result.gate_stats,
        "cost_model": result.cost_model_metadata,
    }
    artifact_payload = {
        "schema_version": "frozen_decision_paper_simulation_v1",
        "decision_id": decision_id, "protocol_id": protocol.protocol_id,
        "calendar_sessions": calendar_sessions,
        "input_bars": [asdict(bar) for bar in bars],
        "corporate_actions": [asdict(action) for action in corporate_actions or []],
        "orders": result.orders, "fills": result.fills, "rejects": result.rejects,
        "portfolio_states": result.portfolio_states, "metrics": result.metrics,
        "outcomes": result.outcomes, "corporate_action_events": result.corporate_action_events,
        "summary": summary,
    }
    simulation_id = f"paper:{market}:{payload_digest(artifact_payload)[:32]}"
    artifact_store = store or JsonPayloadArtifactStore()
    reference = artifact_store.write(artifact_payload, namespace="stock_selection_paper_simulations")
    if artifact_store.read(reference) != json.loads(json.dumps(artifact_payload, default=str)):
        raise RuntimeError("paper simulation artifact verification failed")
    if db is not None:
        model = PAPER_MODELS[market]
        row = db.scalar(select(model).where(model.simulation_id == simulation_id))
        if row is None:
            row = model(
                simulation_id=simulation_id, decision_id=decision_id, market=market,
                protocol_id=protocol.protocol_id, status=summary["status"],
                start_date=date.fromisoformat(calendar_sessions[0]),
                end_date=date.fromisoformat(calendar_sessions[-1]),
                payload_sha256=payload_digest(artifact_payload),
                artifact_reference_json=json.dumps(reference, sort_keys=True),
                summary_json=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                created_at=datetime.now().astimezone(),
            )
            db.add(row)
            db.commit()
    return {"simulation_id": simulation_id, "artifact": reference, "summary": summary}


__all__ = ["run_frozen_decision_paper_simulation"]
