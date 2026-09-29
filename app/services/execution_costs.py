"""Shared per-fill arithmetic; not a claim of realistic exchange fee coverage.

This version reproduces the daily engine's symmetric commission/slippage model.
Minimum fees, taxes, liquidity impact and corporate actions need other evidence.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math


@dataclass(frozen=True, slots=True)
class FillCostModel:
    commission_bps_one_way: float
    slippage_bps_one_way: float

    def __post_init__(self):
        for name in ("commission_bps_one_way", "slippage_bps_one_way"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value < 10_000:
                raise ValueError(f"{name} must be finite and in [0, 10000)")
            object.__setattr__(self, name, float(value))

    @property
    def version(self) -> str:
        return "per_fill_commission_slippage_v1"

    @property
    def model_hash(self) -> str:
        payload = {"version": self.version, **asdict(self)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def nominal_round_trip_bps(self) -> float:
        # Diagnostic approximation, not the actual return deduction.
        return 2 * (self.commission_bps_one_way + self.slippage_bps_one_way)

    def metadata(self) -> dict:
        return {"version": self.version, "hash": self.model_hash, **asdict(self),
                "return_denominator": "entry_notional_plus_entry_fee"}

    def fill_price(self, reference_price: float, *, side: str) -> float:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError("reference price must be finite and positive")
        return reference_price * (1 + (1 if side == "buy" else -1) * self.slippage_bps_one_way / 10_000)

    def fill(self, reference_price: float, quantity: float, *, side: str) -> dict:
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("fill quantity must be finite and positive")
        price = self.fill_price(reference_price, side=side)
        notional = quantity * price
        fee = notional * (self.commission_bps_one_way / 10_000)
        slippage = quantity * abs(price - reference_price)
        return {"fill_price": price, "notional": notional, "fee": fee, "slippage": slippage,
                "cash_flow": -(notional + fee) if side == "buy" else notional - fee}

    def round_trip(self, entry_reference: float, exit_reference: float, *, quantity: float = 1.0) -> dict:
        entry = self.fill(entry_reference, quantity, side="buy")
        exit_fill = self.fill(exit_reference, quantity, side="sell")
        capital = -entry["cash_flow"]
        pnl = exit_fill["cash_flow"] - capital
        return {"entry": entry, "exit": exit_fill, "invested_capital": capital,
                "net_pnl": pnl, "net_return": pnl / capital,
                "gross_return": exit_reference / entry_reference - 1,
                "cost_model_version": self.version, "cost_model_hash": self.model_hash}
