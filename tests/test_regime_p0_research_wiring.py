from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app.services.execution_costs import FillCostModel
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.market_calendar import is_market_open_date
from app.services.stock_selection.execution_evidence import ResearchExecutionEvidence, price_path_hash
from app.services.stock_selection.executable_outcomes import ExecutionEligibility, FILL_COST_OUTCOME_VERSION
from app.services.stock_selection.labels import PriceBar
from app.services.stock_selection.sample_builder import SampleBuildConfig, build_training_samples
from app.services.stock_selection.schemas import UniverseSnapshot
from app.services.stock_selection.production_data import build_production_research_dataset, PriceFeatureConfig
from app.services.stock_selection.production_research import (
    ProductionResearchRunConfig, MarketResearchInputs, run_production_research_challenger,
)
from app.services.stock_selection.universe import SecurityMetadata, UniverseRuleConfig
from scripts.run_stock_selection_research import parse_args


def fixture(day_count=30, ticker_count=8):
    days, day = [], date(2025, 1, 6)
    while len(days) < day_count:
        if is_market_open_date("US", day.isoformat()):
            days.append(day)
        day += timedelta(days=1)
    bars = {f"S{i:02}": [PriceBar(day, 100 + j * (.1 + i * .02), 102 + j * (.1 + i * .02),
                                  99 + j * (.1 + i * .02), 101 + j * (.1 + i * .02), 2_000_000)
                          for j, day in enumerate(days)] for i in range(ticker_count)}
    evidence = ResearchExecutionEvidence("US", tuple(days), price_path_hash(bars), "synthetic-fixture-only",
        {(ticker, day, 3): ExecutionEligibility(True, True) for ticker in bars for day in days[:-3]})
    return days, bars, evidence


class ResearchWiringTests(TestCase):
    def setUp(self):
        self.days, self.bars, self.evidence = fixture()
        self.cost = FillCostModel(8, 12)
        self.config = SampleBuildConfig(market="US", horizons=(3,), round_trip_cost_bps=0,
            drawdown_penalty=0, require_relative_returns=False, target_mode="net_return",
            label_version=FILL_COST_OUTCOME_VERSION, fill_cost_model=self.cost)
        self.snapshots = [UniverseSnapshot(snapshot_id=f"{ticker}:{day}", market="US", trade_date=day,
            ticker=ticker, included=True, source_as_of=datetime.combine(day, time(21), tzinfo=timezone.utc),
            universe_version="fixture", price=100, adv20=100_000_000, volume=2_000_000)
            for ticker in self.bars for day in self.days[:-3]]

    def build(self, **changes):
        args = dict(trading_dates=self.days, bars_by_ticker=self.bars, universe_snapshots=self.snapshots,
            features_by_key={(s.ticker, s.trade_date): {"momentum": 1.0} for s in self.snapshots},
            market_returns={}, industry_returns={}, entry_exclusions={}, config=self.config,
            execution_evidence=self.evidence)
        args.update(changes)
        return build_training_samples(**args)

    def test_samples_use_cash_net_and_manifest_carries_cost_and_execution_identity(self):
        result = self.build()
        first = result.samples[0]
        bars = self.bars[first.ticker]
        self.assertAlmostEqual(self.cost.round_trip(bars[1].open, bars[3].close)["net_return"], first.label_value)
        self.assertEqual(first.label_value, first.label_components["net_return"])
        self.assertEqual(self.cost.model_hash, result.manifest()["label_contract"]["cost_model"]["hash"])
        self.assertEqual(self.evidence.evidence_hash, result.label_contract["execution_evidence_hash"])

    def test_missing_evidence_never_defaults_to_executable(self):
        result = self.build(execution_evidence=replace(self.evidence, records={}))
        self.assertEqual(0, result.eligible_count)
        self.assertEqual(len(self.snapshots), len(result.samples))
        self.assertTrue(all(s.label_value is None for s in result.samples))
        self.assertEqual(len(result.samples), result.exclusion_counts["missing_execution_evidence"])

    def test_evidence_change_changes_dataset_identity(self):
        key = next(iter(self.evidence.records))
        changed = replace(self.evidence, records={**self.evidence.records, key: ExecutionEligibility(False, True, "limit_up")})
        self.assertNotEqual(self.build().dataset_version, self.build(execution_evidence=changed).dataset_version)

    def test_wrong_prices_market_or_missing_evidence_fail_before_training(self):
        with self.assertRaises(ValueError):
            self.build(execution_evidence=None)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.build(execution_evidence=replace(self.evidence, price_sha256="0" * 64))
        with self.assertRaises(ValueError):
            replace(self.evidence, market="CN")

    def test_unknown_boolean_string_and_duplicate_file_records_are_rejected(self):
        key = next(iter(self.evidence.records))
        with self.assertRaises(ValueError):
            replace(self.evidence, records={key: ExecutionEligibility("false", True)})
        with TemporaryDirectory() as root:
            path = Path(root) / "evidence.json"
            payload = self.evidence.payload()
            path.write_text(json.dumps(payload))
            self.assertEqual(self.evidence.evidence_hash, ResearchExecutionEvidence.from_path(path).evidence_hash)
            payload["records"].append(payload["records"][0])
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                ResearchExecutionEvidence.from_path(path)

    def test_config_rejects_cn_one_day_and_legacy_cost_mixing(self):
        with self.assertRaises(ValueError):
            replace(self.config, market="CN", horizons=(1,))
        with self.assertRaises(ValueError):
            replace(self.config, round_trip_cost_bps=20)
        with self.assertRaises(ValueError):
            replace(self.config, fill_cost_model=None)

    def test_dataset_preserves_explicit_net_target_in_both_protocols(self):
        rows = [dict(symbol=ticker, date=bar.trade_date.isoformat(), open=bar.open, high=bar.high,
                     low=bar.low, close=bar.close, volume=bar.volume) for ticker, bars in self.bars.items() for bar in bars]
        feature_config = PriceFeatureConfig(minimum_history_sessions=25, momentum_short_sessions=2,
            momentum_medium_sessions=3, momentum_long_sessions=3, volatility_sessions=3,
            efficiency_ratio_sessions=3, volume_sessions=3, drawdown_sessions=3)
        for new in (False, True):
            with self.subTest(new=new):
                config = self.config if new else SampleBuildConfig(market="US", horizons=(3,),
                    require_relative_returns=False, target_mode="net_return")
                dataset = build_production_research_dataset(rows, market="US",
                    metadata={ticker: SecurityMetadata(ticker, listing_date=date(2020, 1, 1)) for ticker in self.bars},
                    industries={}, universe_rules=UniverseRuleConfig("US", 1, 0, 0, 3), sample_config=config,
                    feature_config=feature_config, source_version="synthetic", execution_evidence=self.evidence if new else None)
                self.assertGreater(dataset.sample_result.eligible_count, 0)
                for sample in dataset.sample_result.samples:
                    if sample.tradable:
                        self.assertEqual(sample.label_components["net_return"], sample.label_value)

    def test_cli_requires_explicit_v2_cost_and_evidence(self):
        with patch("sys.argv", ["research", "--market", "US", "--label-protocol", "cash_net_v2"]), patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parse_args()
        with patch("sys.argv", ["research", "--market", "CN", "--horizons", "5", "--label-protocol", "cash_net_v2",
            "--target-mode", "net_return", "--commission-bps-one-way", "8", "--slippage-bps-one-way", "12",
            "--execution-evidence", "fixture.json"]):
            self.assertEqual("cash_net_v2", parse_args().label_protocol)

    def test_runner_without_evidence_cannot_load_production_data(self):
        config = ProductionResearchRunConfig(market="US", horizons=(3,), target_mode="net_return",
            round_trip_cost_bps=0, drawdown_penalty=0, fill_cost_model=self.cost)
        with patch("app.services.stock_selection.production_research.load_market_research_inputs") as loader:
            with self.assertRaisesRegex(ValueError, "enabled together"):
                run_production_research_challenger(config=config)
            loader.assert_not_called()

    def test_public_runner_trains_and_evaluates_real_models_on_synthetic_data(self):
        days, bars, evidence = fixture(155, 20)
        rows = tuple(dict(symbol=ticker, date=bar.trade_date.isoformat(), open=bar.open, high=bar.high,
                          low=bar.low, close=bar.close, volume=bar.volume) for ticker, path in bars.items() for bar in path)
        inputs = MarketResearchInputs(market="US", rows=rows,
            metadata={ticker: SecurityMetadata(ticker, listing_date=date(2020, 1, 1)) for ticker in bars},
            industries={}, source_version="synthetic-fixture-only", selection_mode="pilot",
            selected_tickers=tuple(bars), metadata_coverage_count=20, industry_coverage_count=0)
        config = ProductionResearchRunConfig(market="US", horizons=(3,), pilot_ticker_limit=20,
            prediction_date_count=2, minimum_training_dates=4, minimum_training_samples=24,
            ranker_estimators=5, history_limit_per_symbol=155, target_mode="net_return",
            round_trip_cost_bps=0, drawdown_penalty=0, fill_cost_model=self.cost)
        with TemporaryDirectory() as root, patch(
            "app.services.stock_selection.production_research.load_market_research_inputs", return_value=inputs
        ):
            result = run_production_research_challenger(config=config, artifact_root=Path(root), execution_evidence=evidence)
            comparison = result.comparisons[3]
            self.assertEqual({"equal_weight", "ridge", "lambdarank"}, set(comparison.reports))
            self.assertEqual(2, len(comparison.common_evaluated_dates))
            self.assertTrue(all(fold.leakage_violation_count == 0 for fold in comparison.fold_audits))
            self.assertTrue(all(fold.model_status["lambdarank"] == "success" for fold in comparison.fold_audits))
            self.assertEqual("BLOCKED_MISSING_EVIDENCE", comparison.regime_coverage["status"])
            self.assertTrue(all(not ids for by_date in comparison.regime_candidate_sample_ids.values() for ids in by_date.values()))
            manifest = json.loads(result.sample_artifact.manifest_path.read_text())
            self.assertEqual(evidence.evidence_hash, manifest["label_contract"]["execution_evidence_hash"])
            self.assertTrue(result.evidence_artifacts[3].manifest_path.is_file())
            restored = JsonPayloadArtifactStore(Path(root) / "execution_evidence").read(
                manifest["label_contract"]["execution_evidence_artifact"])
            self.assertEqual(evidence.payload(), restored)

    def test_calendar_cannot_omit_a_market_session(self):
        with self.assertRaisesRegex(ValueError, "omits a trading session"):
            replace(self.evidence, trading_dates=tuple(self.days[:2] + self.days[3:]), records={})

    def test_feature_window_cannot_use_negative_fixed_ma_slices(self):
        with self.assertRaises(ValueError):
            PriceFeatureConfig(minimum_history_sessions=4, momentum_short_sessions=2,
                momentum_medium_sessions=3, momentum_long_sessions=3, volatility_sessions=3,
                efficiency_ratio_sessions=3, volume_sessions=3, drawdown_sessions=3)

    def test_sample_explicit_target_must_match_its_component(self):
        first = self.build().samples[0]
        with self.assertRaisesRegex(ValueError, "net_return component"):
            replace(first, label_value=first.label_value + 0.1)
