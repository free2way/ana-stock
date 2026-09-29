"""Fail-closed source-time and security eligibility contracts for selection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class SourceDataContract:
    market: str
    source: str
    event_time_field: str
    available_time_field: str | None
    ingested_time_field: str
    preserves_revisions: bool
    historical_universe: bool
    price_basis: str
    timezone_name: str = "UTC"
    available_time_semantics: str = ""
    ingested_time_semantics: str = "local_observation_time"
    revision_strategy: str = "unknown"
    universe_strategy: str = "current_survivors_only"

    def __post_init__(self) -> None:
        if self.market.upper() not in {"CN", "US", "HK"}:
            raise ValueError("source market must be explicit CN, US, or HK")
        if self.price_basis not in {"raw", "adjusted", "not_price"}:
            raise ValueError("price_basis must be raw, adjusted, or not_price")

    @property
    def permits_historical_pit(self) -> bool:
        return bool(self.available_time_field and self.ingested_time_field and self.preserves_revisions)

    @property
    def blockers(self) -> tuple[str, ...]:
        values: list[str] = []
        if not self.available_time_field:
            values.append("available_time_missing")
        if not self.preserves_revisions:
            values.append("revision_history_not_preserved")
        if not self.historical_universe:
            values.append("historical_universe_not_preserved")
        return tuple(values)


_SOURCE_DATA_CONTRACTS = (
    SourceDataContract(
        "CN", "community_eastmoney", "event_time", "available_time", "ingested_time",
        False, False, "not_price", "Asia/Shanghai",
        "Financial notice/update time; valuation uses the requested trade-date cutoff.",
        revision_strategy="latest provider response only",
    ),
    SourceDataContract(
        "CN", "hithink_finance", "event_time", "available_time", "ingested_time",
        False, False, "not_price", "Asia/Shanghai",
        "Statement report time; valuation carries its provider timestamp.",
        revision_strategy="multiple periods retained, same-period revision chain unavailable",
    ),
    SourceDataContract(
        "CN", "tushare", "report_date", None, "created_at",
        False, False, "not_price", "Asia/Shanghai",
        "Provider row currently does not expose a field-level publication timestamp.",
        revision_strategy="upserted wide snapshot",
    ),
    SourceDataContract(
        "US", "global_stock_data_sec_edgar", "report_date", None, "created_at",
        False, False, "not_price", "America/New_York",
        "SEC filing time exists upstream but is not yet retained by this adapter.",
        revision_strategy="latest annual fact per period",
    ),
    SourceDataContract(
        "US", "openbb_fundamentals", "report_date", None, "created_at",
        False, False, "not_price", "America/New_York",
        "Provider snapshot has no verified field-level publication timestamp.",
        revision_strategy="upserted wide snapshot",
    ),
    SourceDataContract(
        "HK", "openbb_fundamentals", "report_date", None, "created_at",
        False, False, "not_price", "Asia/Hong_Kong",
        "Provider snapshot has no verified field-level publication timestamp.",
        revision_strategy="upserted wide snapshot",
    ),
)


def source_data_contracts(*, market: str | None = None) -> tuple[SourceDataContract, ...]:
    market_code = str(market or "").strip().upper()
    if market_code and market_code not in {"CN", "US", "HK"}:
        raise ValueError("market must be CN, US, or HK")
    return tuple(
        item for item in _SOURCE_DATA_CONTRACTS
        if not market_code or item.market.upper() == market_code
    )


def audit_source_data_contracts(
    observed_sources: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Compare observed physical-store sources with the declared PIT contracts.

    Input rows are aggregate facts only. The function is deliberately pure so a
    production audit can run in a read-only transaction and unit tests do not
    require a database.
    """

    registry = {
        (item.market.upper(), item.source): item for item in source_data_contracts()
    }
    observed: dict[tuple[str, str], dict[str, object]] = {}
    for row in observed_sources:
        market = str(row.get("market") or "").strip().upper()
        source = str(row.get("source") or "").strip()
        if market not in {"CN", "US", "HK"} or not source:
            raise ValueError("observed source rows require explicit market and source")
        observed[(market, source)] = {
            "row_count": int(row.get("row_count") or 0),
            "suspected_backdated_ingestion_count": int(
                row.get("suspected_backdated_ingestion_count") or 0
            ),
            "earliest_event_time": row.get("earliest_event_time"),
            "latest_created_at": row.get("latest_created_at"),
        }

    entries: list[dict[str, object]] = []
    for key in sorted(set(registry) | set(observed)):
        contract = registry.get(key)
        facts = observed.get(key, {})
        blockers = list(contract.blockers if contract else ("source_contract_missing",))
        backdated = int(facts.get("suspected_backdated_ingestion_count") or 0)
        if backdated:
            blockers.append("suspected_backdated_ingestion")
        entries.append(
            {
                "market": key[0],
                "source": key[1],
                "registered": contract is not None,
                "observed_row_count": int(facts.get("row_count") or 0),
                "suspected_backdated_ingestion_count": backdated,
                "earliest_event_time": facts.get("earliest_event_time"),
                "latest_created_at": facts.get("latest_created_at"),
                "permits_historical_pit": bool(
                    contract and contract.permits_historical_pit and not backdated
                ),
                "blockers": blockers,
                "event_time_field": contract.event_time_field if contract else None,
                "available_time_field": contract.available_time_field if contract else None,
                "ingested_time_field": contract.ingested_time_field if contract else None,
                "timezone_name": contract.timezone_name if contract else None,
                "available_time_semantics": (
                    contract.available_time_semantics if contract else None
                ),
                "ingested_time_semantics": (
                    contract.ingested_time_semantics if contract else None
                ),
                "revision_strategy": contract.revision_strategy if contract else None,
                "universe_strategy": contract.universe_strategy if contract else None,
            }
        )
    return {
        "schema_version": "source_data_contract_audit_v1",
        "verdict": "PASS" if entries and all(item["permits_historical_pit"] for item in entries) else "BLOCKED",
        "entries": entries,
    }


def validate_market_ticker(market: str, ticker: str) -> str:
    code = str(market or "").strip().upper()
    symbol = str(ticker or "").strip().upper()
    if code not in {"CN", "US", "HK"} or not symbol:
        raise ValueError("market and ticker are required")
    suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
    if code == "CN" and suffix == "SH":
        symbol = symbol[:-3] + ".SS"
        suffix = "SS"
    valid = (
        suffix in {"SS", "SZ", "BJ"} if code == "CN" else
        suffix == "HK" if code == "HK" else
        suffix not in {"SS", "SZ", "BJ", "HK"}
    )
    if not valid:
        raise ValueError(f"ticker {symbol} does not belong to {code}")
    return symbol


def feature_known_at_cutoff(
    row: Mapping[str, object], *, contract: SourceDataContract, cutoff: datetime,
) -> bool:
    """Unknown publication/ingestion timestamps must never be inferred from report period."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    if not contract.permits_historical_pit:
        return False
    market = str(row.get("market") or contract.market).upper()
    if market != contract.market.upper():
        return False
    try:
        available = row[contract.available_time_field or ""]
        ingested = row[contract.ingested_time_field]
        if not isinstance(available, datetime):
            available = datetime.fromisoformat(str(available).replace("Z", "+00:00"))
        if not isinstance(ingested, datetime):
            ingested = datetime.fromisoformat(str(ingested).replace("Z", "+00:00"))
        if any(value.tzinfo is None or value.utcoffset() is None for value in (available, ingested)):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return max(available, ingested) <= cutoff


def candidate_eligibility(
    *, market: str, ticker: str, market_health: str,
    symbol_status: str, source_status: str = "ready",
) -> dict[str, str | bool]:
    """Market-level graded is informational; each candidate has its own hard gate."""
    validate_market_ticker(market, ticker)
    health = str(market_health or "").strip().lower()
    status = str(symbol_status or "").strip().lower()
    source = str(source_status or "").strip().lower()
    if source in {"failed", "missing", "unknown", "degraded"}:
        reason = "source_unavailable"
    elif status in {"suspended", "no_trade", "inactive"}:
        reason = "symbol_not_tradable"
    elif status in {"missing_quote", "unknown", "abnormal_jump"}:
        reason = "symbol_quote_unknown"
    elif status not in {"ready", "normal", "tradable"}:
        reason = "symbol_status_unverified"
    elif health in {"failed", "missing", "unknown"}:
        reason = "market_source_unavailable"
    else:
        reason = ""
    return {"eligible": not reason, "reason": reason, "market_health": health,
            "symbol_status": status, "source_status": source}


__all__ = [
    "SourceDataContract",
    "audit_source_data_contracts",
    "candidate_eligibility",
    "feature_known_at_cutoff",
    "source_data_contracts",
    "validate_market_ticker",
]
