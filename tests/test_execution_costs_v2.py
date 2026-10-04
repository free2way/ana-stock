from __future__ import annotations

from unittest import TestCase

from app.services.execution_costs import (
    CN_MIN_COMMISSION,
    CN_STAMP_DUTY_BPS_ONE_WAY,
    CN_TRANSFER_FEE_BPS_ONE_WAY,
    US_SEC_FEE_BPS_ONE_WAY,
    US_TAF_PER_SHARE,
    FillCostModel,
    default_fill_cost_model,
)


class FillCostModelV2Tests(TestCase):
    def test_default_model_keeps_legacy_arithmetic(self) -> None:
        model = FillCostModel(8, 12)
        buy = model.fill(10.0, 1000, side="buy")
        sell = model.fill(10.0, 1000, side="sell")
        # Commission is symmetric in bps; notionals differ because the fill
        # price already carries the side-specific slippage.
        self.assertAlmostEqual(buy["notional"] * 8.0 / 10_000, buy["fee"], places=12)
        self.assertAlmostEqual(sell["notional"] * 8.0 / 10_000, sell["fee"], places=12)
        self.assertEqual(0.0, buy["stamp_duty"])
        self.assertEqual(0.0, sell["stamp_duty"])
        self.assertEqual("per_fill_commission_slippage_v2", model.version)

    def test_cn_defaults_split_buy_and_sell_fees(self) -> None:
        model = default_fill_cost_model("CN", commission_bps_one_way=2.5, slippage_bps_one_way=0.0)
        buy = model.fill(10.0, 1000, side="buy")
        sell = model.fill(10.0, 1000, side="sell")
        notional = 10_000.0

        # Small notional: minimum commission dominates the bps rate.
        self.assertAlmostEqual(CN_MIN_COMMISSION, buy["commission"], places=12)
        self.assertAlmostEqual(notional * CN_TRANSFER_FEE_BPS_ONE_WAY / 10_000, buy["transfer_fee"], places=12)
        self.assertEqual(0.0, buy["stamp_duty"])
        self.assertAlmostEqual(CN_MIN_COMMISSION + notional * CN_TRANSFER_FEE_BPS_ONE_WAY / 10_000, buy["fee"], places=12)

        self.assertAlmostEqual(CN_MIN_COMMISSION, sell["commission"], places=12)
        self.assertAlmostEqual(notional * CN_TRANSFER_FEE_BPS_ONE_WAY / 10_000, sell["transfer_fee"], places=12)
        self.assertAlmostEqual(notional * CN_STAMP_DUTY_BPS_ONE_WAY / 10_000, sell["stamp_duty"], places=12)
        self.assertAlmostEqual(sell["fee"], sell["commission"] + sell["transfer_fee"] + sell["stamp_duty"], places=12)
        self.assertGreater(sell["fee"], buy["fee"])

    def test_cn_min_commission_is_a_floor_not_a_fee_on_top(self) -> None:
        model = default_fill_cost_model("CN", commission_bps_one_way=2.5, slippage_bps_one_way=0.0)
        large = model.fill(100.0, 10_000, side="buy")
        self.assertAlmostEqual(large["notional"] * 2.5 / 10_000, large["commission"], places=9)

    def test_us_defaults_charge_sell_side_regulatory_fees_only(self) -> None:
        model = default_fill_cost_model("US", commission_bps_one_way=0.0, slippage_bps_one_way=0.0)
        buy = model.fill(10.0, 1000, side="buy")
        sell = model.fill(10.0, 1000, side="sell")
        expected = 10_000.0 * US_SEC_FEE_BPS_ONE_WAY / 10_000 + 1000 * US_TAF_PER_SHARE
        self.assertEqual(0.0, buy["regulatory_fee"])
        self.assertAlmostEqual(expected, sell["regulatory_fee"], places=12)
        self.assertAlmostEqual(expected, sell["fee"], places=12)

    def test_round_trip_uses_split_fees(self) -> None:
        model = default_fill_cost_model("CN", commission_bps_one_way=2.5, slippage_bps_one_way=0.0)
        summary = model.round_trip(10.0, 11.0, quantity=1000)
        self.assertAlmostEqual(
            summary["net_pnl"],
            summary["exit"]["cash_flow"] - summary["invested_capital"],
            places=9,
        )
        self.assertEqual(model.model_hash, summary["cost_model_hash"])

    def test_unknown_market_has_no_statutory_components(self) -> None:
        model = default_fill_cost_model("HK", commission_bps_one_way=5.0, slippage_bps_one_way=5.0)
        self.assertEqual(0.0, model.sell_stamp_duty_bps_one_way)
        self.assertEqual(0.0, model.min_commission)
