"""Shared per-fill arithmetic for research labels and the event-driven engine.

Version 2 splits buy and sell fees and adds the statutory components that the
old symmetric commission model could not express:

* CN: stamp duty (sell only), transfer fee (both sides), minimum commission.
* US: SEC fee (sell notional) and FINRA TAF (sell per share).

Every optional component defaults to zero, so a caller that only supplies
commission and slippage keeps the v1 arithmetic; richer callers opt in via
``default_fill_cost_model`` or explicit fields.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math


# Statutory / venue defaults, expressed as configurable inputs rather than
# hard-coded branches inside the arithmetic.
CN_STAMP_DUTY_BPS_ONE_WAY = 5.0  # 0.05% on sell notional (since 2023-08-28)
CN_TRANSFER_FEE_BPS_ONE_WAY = 0.1  # 0.001% on both sides
CN_MIN_COMMISSION = 5.0  # CNY per fill
US_SEC_FEE_BPS_ONE_WAY = 0.278  # SEC fee on sell notional
US_TAF_PER_SHARE = 0.000166  # FINRA TAF on sell quantity


@dataclass(frozen=True, slots=True)
class FillCostModel:
    commission_bps_one_way: float
    slippage_bps_one_way: float
    sell_stamp_duty_bps_one_way: float = 0.0
    transfer_fee_bps_one_way: float = 0.0
    sell_regulatory_fee_bps_one_way: float = 0.0
    sell_regulatory_fee_per_share: float = 0.0
    min_commission: float = 0.0

    def __post_init__(self):
        bps_fields = (
            "commission_bps_one_way",
            "slippage_bps_one_way",
            "sell_stamp_duty_bps_one_way",
            "transfer_fee_bps_one_way",
            "sell_regulatory_fee_bps_one_way",
        )
        for name in bps_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value < 10_000:
                raise ValueError(f"{name} must be finite and in [0, 10000)")
            object.__setattr__(self, name, float(value))
        for name in ("sell_regulatory_fee_per_share", "min_commission"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and not negative")
            object.__setattr__(self, name, float(value))

    @property
    def version(self) -> str:
        return "per_fill_commission_slippage_v2"

    @property
    def model_hash(self) -> str:
        payload = {"version": self.version, **asdict(self)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def nominal_round_trip_bps(self) -> float:
        # Diagnostic approximation, not the actual return deduction.
        return (
            2 * (self.commission_bps_one_way + self.slippage_bps_one_way)
            + self.sell_stamp_duty_bps_one_way
            + 2 * self.transfer_fee_bps_one_way
            + self.sell_regulatory_fee_bps_one_way
        )

    def metadata(self) -> dict:
        return {"version": self.version, "hash": self.model_hash, **asdict(self),
                "return_denominator": "entry_notional_plus_entry_fee",
                "min_commission_applies": "per_fill_when_notional_positive"}

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
        commission = notional * (self.commission_bps_one_way / 10_000)
        if self.min_commission > 0:
            commission = max(commission, self.min_commission)
        transfer_fee = notional * (self.transfer_fee_bps_one_way / 10_000)
        sell_side = side == "sell"
        stamp_duty = notional * (self.sell_stamp_duty_bps_one_way / 10_000) if sell_side else 0.0
        regulatory_fee = (
            notional * (self.sell_regulatory_fee_bps_one_way / 10_000)
            + quantity * self.sell_regulatory_fee_per_share
            if sell_side
            else 0.0
        )
        fee = commission + transfer_fee + stamp_duty + regulatory_fee
        slippage = quantity * abs(price - reference_price)
        return {"fill_price": price, "notional": notional, "fee": fee,
                "commission": commission, "transfer_fee": transfer_fee,
                "stamp_duty": stamp_duty, "regulatory_fee": regulatory_fee,
                "slippage": slippage,
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


def default_fill_cost_model(
    market: str,
    *,
    commission_bps_one_way: float,
    slippage_bps_one_way: float,
) -> FillCostModel:
    """Build the statutory default model for a market (opt-in, explicit)."""

    market_code = str(market or "").strip().upper()
    if market_code == "CN":
        return FillCostModel(
            commission_bps_one_way=commission_bps_one_way,
            slippage_bps_one_way=slippage_bps_one_way,
            sell_stamp_duty_bps_one_way=CN_STAMP_DUTY_BPS_ONE_WAY,
            transfer_fee_bps_one_way=CN_TRANSFER_FEE_BPS_ONE_WAY,
            min_commission=CN_MIN_COMMISSION,
        )
    if market_code == "US":
        return FillCostModel(
            commission_bps_one_way=commission_bps_one_way,
            slippage_bps_one_way=slippage_bps_one_way,
            sell_regulatory_fee_bps_one_way=US_SEC_FEE_BPS_ONE_WAY,
            sell_regulatory_fee_per_share=US_TAF_PER_SHARE,
        )
    return FillCostModel(
        commission_bps_one_way=commission_bps_one_way,
        slippage_bps_one_way=slippage_bps_one_way,
    )
