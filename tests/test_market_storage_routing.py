import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.tables import (
    CNFundamentalSnapshot,
    CNLivePrediction,
    CNModelChartSignal,
    CNPrediction,
    CNPredictionTradePlan,
    HKFundamentalSnapshot,
    HKLivePrediction,
    HKModelChartSignal,
    HKPrediction,
    HKPredictionTradePlan,
    USFundamentalSnapshot,
    USLivePrediction,
    USModelChartSignal,
    USPrediction,
    USPredictionTradePlan,
)
from app.core.config import Settings
from app.services.market_storage_routing import (
    enabled_physical_markets,
    legacy_mirror_write_enabled,
    normalize_fact_market,
    physical_fact_table_contract,
    physical_fact_write_markets,
    physical_live_prediction_model,
    physical_hot_prediction_models,
    physical_model_chart_signal_model,
    physical_prediction_trade_plan_model,
    physical_snapshot_models,
)
from app.services.repository import (
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionWriteRepository,
    _legacy_snapshot_writes_enabled,
    _physical_snapshot_tables_for_market,
)


class MarketStorageRoutingTests(unittest.TestCase):
    def test_cn_and_us_route_to_different_physical_tables(self):
        self.assertIs(CNLivePrediction, physical_live_prediction_model("CN"))
        self.assertIs(USLivePrediction, physical_live_prediction_model("us"))
        self.assertIs(HKLivePrediction, physical_live_prediction_model("hk"))
        self.assertNotEqual(CNLivePrediction.__tablename__, USLivePrediction.__tablename__)
        self.assertIs(CNPrediction, physical_hot_prediction_models("CN")[0])
        self.assertIs(USPrediction, physical_hot_prediction_models("US")[0])
        self.assertIs(HKPrediction, physical_hot_prediction_models("HK")[0])
        self.assertIs(CNFundamentalSnapshot, physical_snapshot_models("CN")[0])
        self.assertIs(USFundamentalSnapshot, physical_snapshot_models("US")[0])
        self.assertIs(HKFundamentalSnapshot, physical_snapshot_models("HK")[0])
        self.assertIs(CNModelChartSignal, physical_model_chart_signal_model("CN"))
        self.assertIs(USModelChartSignal, physical_model_chart_signal_model("US"))
        self.assertIs(HKModelChartSignal, physical_model_chart_signal_model("HK"))
        self.assertIs(
            CNPredictionTradePlan,
            physical_prediction_trade_plan_model("CN"),
        )
        self.assertIs(
            USPredictionTradePlan,
            physical_prediction_trade_plan_model("US"),
        )
        self.assertIs(
            HKPredictionTradePlan,
            physical_prediction_trade_plan_model("HK"),
        )

        contract = physical_fact_table_contract()
        self.assertEqual({"CN", "HK", "US"}, set(contract))
        self.assertEqual(9, len(contract["CN"]))
        self.assertTrue(all(name.startswith("cn_") for name in contract["CN"].values()))
        self.assertTrue(all(name.startswith("us_") for name in contract["US"].values()))
        self.assertTrue(all(name.startswith("hk_") for name in contract["HK"].values()))
        self.assertEqual(27, len({name for tables in contract.values() for name in tables.values()}))

    def test_cn_and_us_physical_writes_cannot_be_disabled_by_rollout_config(self):
        self.assertEqual({"CN", "HK", "US"}, set(physical_fact_write_markets()))
        self.assertEqual(
            "CN,HK,US",
            Settings.model_fields["market_physical_live_markets"].default,
        )
        self.assertEqual(
            "CN,HK,US",
            Settings.model_fields["market_physical_hot_markets"].default,
        )
        self.assertEqual(
            "CN,HK,US",
            Settings.model_fields["market_physical_snapshot_markets"].default,
        )

    def test_us_snapshot_write_routing_is_physical_even_during_read_rollout(self):
        with patch(
            "app.services.repository.get_settings",
            return_value=SimpleNamespace(market_physical_snapshot_markets="CN"),
        ):
            tables = _physical_snapshot_tables_for_market("US")
        self.assertIsNotNone(tables)
        self.assertIs(USFundamentalSnapshot, tables[0])

    def test_cross_market_aggregate_values_are_rejected_for_fact_writes(self):
        for market in (None, "", "ALL", "MIXED"):
            with self.subTest(market=market):
                with self.assertRaises(ValueError):
                    normalize_fact_market(market)

    def test_enabled_market_configuration_fails_closed(self):
        self.assertEqual({"CN"}, enabled_physical_markets("CN"))
        self.assertEqual({"CN", "US"}, enabled_physical_markets("CN,US"))
        self.assertEqual({"CN", "HK"}, enabled_physical_markets("CN,HK"))

    def test_active_cutover_marker_overrides_legacy_snapshot_flag(self):
        with patch(
            "app.services.market_storage_routing.cn_physical_only_cutover_active",
            return_value=True,
        ), patch(
            "app.services.repository.get_settings",
            return_value=SimpleNamespace(
                market_physical_snapshot_dual_write_legacy=True
            ),
        ):
            self.assertFalse(
                _legacy_snapshot_writes_enabled(MagicMock(), object())
            )

    def test_cn_cutover_does_not_change_us_legacy_mirror_state(self):
        with patch(
            "app.services.market_storage_routing.cn_physical_only_cutover_active",
            return_value=True,
        ):
            self.assertFalse(
                legacy_mirror_write_enabled(
                    MagicMock(), market="CN", configured=True
                )
            )
            self.assertTrue(
                legacy_mirror_write_enabled(
                    MagicMock(), market="US", configured=True
                )
            )

    def test_legacy_prediction_repositories_fail_closed_after_cn_cutover(self):
        db = MagicMock()
        db.scalar.return_value = "CN"
        repositories = (
            PredictionWriteRepository(db),
            PredictionDetailRepository(db),
            PredictionExplanationRepository(db),
        )
        with patch(
            "app.services.repository.physical_only_cutover_active",
            return_value=True,
        ):
            for repository in repositories:
                with self.subTest(repository=repository.__class__.__name__):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Legacy shared prediction writes are disabled",
                    ):
                        repository.replace_for_model_run(42, [])

        db.execute.assert_not_called()

    def test_legacy_prediction_repositories_remain_available_during_dual_write(self):
        db = MagicMock()
        db.scalar.side_effect = ["CN", None]
        db.scalars.return_value.all.return_value = []
        with patch(
            "app.services.repository.physical_only_cutover_active",
            return_value=False,
        ):
            written = PredictionWriteRepository(db).replace_for_model_run(
                42,
                [],
                commit=False,
            )
        self.assertEqual(0, written)


if __name__ == "__main__":
    unittest.main()
