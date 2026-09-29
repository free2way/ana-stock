"""Validated caller-supplied execution evidence for research, not fill certification."""
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.services.stock_selection.executable_outcomes import ExecutionEligibility
from app.services.stock_selection.labels import PriceBar
from app.services.stock_selection.data_contracts import validate_market_ticker
from app.services.market_calendar import is_market_open_date, next_market_open_date


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def price_path_hash(bars_by_ticker: Mapping[str, list[PriceBar]]) -> str:
    return _digest({ticker: [{"trade_date": bar.trade_date.isoformat(),
                             **{key: float(getattr(bar, key)) for key in ("open", "high", "low", "close", "volume")}}
                            for bar in sorted(bars, key=lambda item: item.trade_date)]
                    for ticker, bars in sorted(bars_by_ticker.items())})


@dataclass(frozen=True)
class ResearchExecutionEvidence:
    market: str
    trading_dates: tuple[date, ...]
    price_sha256: str
    source_reference: str
    records: Mapping[tuple[str, date, int], ExecutionEligibility]

    def __post_init__(self):
        if self.market not in {"CN", "US"}:
            raise ValueError("execution evidence market must be CN or US")
        if not self.trading_dates or list(self.trading_dates) != sorted(set(self.trading_dates)):
            raise ValueError("execution evidence calendar must be nonempty, unique and ascending")
        if any(not is_market_open_date(self.market, day.isoformat()) for day in self.trading_dates):
            raise ValueError("execution evidence calendar includes a closed market date")
        if any(next_market_open_date(self.market, previous.isoformat(), include_self=False) != current.isoformat()
               for previous, current in zip(self.trading_dates, self.trading_dates[1:])):
            raise ValueError("execution evidence calendar omits a trading session")
        object.__setattr__(self, "trading_dates", tuple(self.trading_dates))
        if len(self.price_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.price_sha256):
            raise ValueError("execution evidence requires price SHA256")
        if not self.source_reference.strip():
            raise ValueError("execution evidence source_reference required")
        for (ticker, day, horizon), value in self.records.items():
            if validate_market_ticker(self.market, ticker) != ticker:
                raise ValueError("execution evidence ticker must be canonical")
            if day not in self.trading_dates or type(horizon) is not int or horizon < 1:
                raise ValueError("execution evidence date/horizon invalid")
            if any(flag is not None and type(flag) is not bool for flag in (value.entry_allowed, value.exit_allowed)):
                raise ValueError("execution eligibility flags must be boolean or null")
            if value.reason and value.entry_allowed is True and value.exit_allowed is True:
                raise ValueError("successful execution cannot carry exclusion reason")
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))

    def payload(self) -> dict:
        return {"schema_version": "research_execution_evidence_v1", "market": self.market,
                "price_basis": "raw",
                "trading_dates": [day.isoformat() for day in self.trading_dates],
                "price_sha256": self.price_sha256, "source_reference": self.source_reference,
                "records": [{"ticker": ticker, "signal_date": day.isoformat(), "horizon_days": horizon,
                             **asdict(value)} for (ticker, day, horizon), value in sorted(self.records.items())]}

    @property
    def evidence_hash(self) -> str:
        return _digest(self.payload())

    def validate_prices(self, bars_by_ticker, *, market: str):
        if market != self.market or price_path_hash(bars_by_ticker) != self.price_sha256:
            raise ValueError("execution evidence market or price hash mismatch")
        if any(bar.trade_date not in self.trading_dates for bars in bars_by_ticker.values() for bar in bars):
            raise ValueError("price date outside explicit evidence calendar")

    @classmethod
    def from_path(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != "research_execution_evidence_v1":
            raise ValueError("unsupported execution evidence schema")
        if data.get("price_basis") != "raw":
            raise ValueError("execution evidence requires raw price basis")
        records = {}
        for row in data["records"]:
            key = (row["ticker"], date.fromisoformat(row["signal_date"]), row["horizon_days"])
            if key in records:
                raise ValueError("duplicate execution evidence record")
            records[key] = ExecutionEligibility(row["entry_allowed"], row["exit_allowed"], row.get("reason"))
        return cls(data["market"], tuple(date.fromisoformat(day) for day in data["trading_dates"]),
                   data["price_sha256"], data["source_reference"], records)
