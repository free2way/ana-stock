from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.market_calendar import is_market_open_date
from app.services.stock_selection.p1_portfolio_risk import (
    P1PortfolioCandidate,
    P1PortfolioPosition,
    P1PortfolioRiskConfig,
    apply_p1_portfolio_risk_gate,
)
from app.services.stock_selection.p1_sampling import (
    P1SamplingConfig,
    build_p1_training_weights,
)
from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorDirection,
    FactorSpec,
)
from app.services.stock_selection.ranker import RankerConfig
from app.services.stock_selection.research_runner import (
    WalkForwardComparisonConfig,
    run_walk_forward_model_comparison,
)
from app.services.stock_selection.schemas import LabeledSample
from app.services.stock_selection.p2_forward_protocol import (
    P2CandidateSpec,
    P2ExperimentRecord,
    P2ForwardObservation,
    assess_p2_forward_progress,
    freeze_p2_forward_candidate,
    persist_p2_candidate_freeze,
)
from app.services.stock_selection.protocol import ExecutableSelectionProtocol


@dataclass(frozen=True)
class _Sample:
    sample_id: str
    feature_date: date


def _calendar(count: int) -> list[date]:
    start = date(2026, 1, 1)
    return [start + timedelta(days=index) for index in range(count)]


class P1SamplingTests(TestCase):
    def test_fixed_window_has_equal_total_mass_per_date(self):
        dates = _calendar(4)
        rows = [_Sample("a", dates[0]), _Sample("b", dates[1]), _Sample("c", dates[1])]
        result = build_p1_training_weights(rows, trading_dates=dates, prediction_date=dates[3])
        self.assertAlmostEqual(result.weights_by_sample_id["a"], sum(
            result.weights_by_sample_id[key] for key in ("b", "c")
        ))
        self.assertEqual("READY_RESEARCH_ONLY", result.audit["status"])

    def test_exponential_half_life_uses_trading_session_age(self):
        dates = _calendar(61)
        rows = [_Sample("old", dates[0]), _Sample("new", dates[60 - 1])]
        result = build_p1_training_weights(
            rows, trading_dates=dates, prediction_date=dates[60],
            config=P1SamplingConfig(mode="exponential_decay_v1", half_life_sessions=59),
        )
        self.assertAlmostEqual(
            result.weights_by_sample_id["old"] * 2,
            result.weights_by_sample_id["new"], places=12,
        )

    def test_regime_multiplier_and_frozen_fallback(self):
        dates = _calendar(6)
        rows = [_Sample(str(index), dates[index]) for index in range(5)]
        regimes = {value: ("risk_on" if index < 3 else "watchful") for index, value in enumerate(dates[:5])}
        matched = build_p1_training_weights(
            rows, trading_dates=dates, prediction_date=dates[5],
            config=P1SamplingConfig(
                mode="decay_plus_regime_v1", half_life_sessions=90,
                matching_regime_multiplier=1.5, minimum_matching_regime_dates=3,
            ),
            historical_regime_by_date=regimes, prediction_regime="risk_on",
        )
        date_rows = {item["date"]: item for item in matched.audit["dates"]}
        base_ratio = 0.5 ** ((5 - 0 - (5 - 3)) / 90)
        self.assertAlmostEqual(
            date_rows[dates[0].isoformat()]["total_weight"]
            / date_rows[dates[3].isoformat()]["total_weight"],
            1.5 * base_ratio,
        )
        fallback = build_p1_training_weights(
            rows, trading_dates=dates, prediction_date=dates[5],
            config=P1SamplingConfig(
                mode="decay_plus_regime_v1", minimum_matching_regime_dates=4,
            ),
            historical_regime_by_date=regimes, prediction_regime="risk_on",
        )
        self.assertEqual("FALLBACK", fallback.audit["status"])
        self.assertEqual("exponential_decay_v1", fallback.audit["applied_mode"])
        self.assertEqual("insufficient_matching_regime_dates", fallback.audit["fallback_reason"])

    def test_regime_evidence_and_temporal_boundaries_fail_closed(self):
        dates = _calendar(4)
        with self.assertRaises(ValueError):
            build_p1_training_weights(
                [_Sample("a", dates[0])], trading_dates=dates, prediction_date=dates[3],
                config=P1SamplingConfig(mode="decay_plus_regime_v1"),
                historical_regime_by_date={}, prediction_regime="risk_on",
            )
        with self.assertRaises(ValueError):
            build_p1_training_weights(
                [_Sample("future", dates[3])], trading_dates=dates, prediction_date=dates[3],
            )

    def test_row_order_does_not_change_weights_or_audit_dates(self):
        dates = _calendar(4)
        rows = [_Sample("a", dates[0]), _Sample("b", dates[1]), _Sample("c", dates[1])]
        left = build_p1_training_weights(rows, trading_dates=dates, prediction_date=dates[3])
        right = build_p1_training_weights(tuple(reversed(rows)), trading_dates=dates, prediction_date=dates[3])
        self.assertEqual(left.weights_by_sample_id, right.weights_by_sample_id)
        self.assertEqual(left.audit["dates"], right.audit["dates"])
        self.assertEqual(left.audit["config_version"], right.audit["config_version"])

    def test_sampling_is_wired_into_ridge_and_lambdarank_fold_audits(self):
        trading_dates = _calendar(18)
        samples = []
        for date_index in range(15):
            feature_date = trading_dates[date_index]
            for ticker_index in range(8):
                ticker = f"S{ticker_index:02d}"
                samples.append(LabeledSample(
                    sample_id=f"{feature_date}:{ticker}:3", market="US", ticker=ticker,
                    feature_date=feature_date,
                    label_start_date=trading_dates[date_index + 1],
                    label_end_date=trading_dates[date_index + 3],
                    label_available_date=trading_dates[date_index + 3],
                    horizon_days=3,
                    label_value=float(ticker_index) + date_index * 0.01,
                    features={"momentum": float(ticker_index), "risk": float(8 - ticker_index)},
                    dataset_version="p1-sampling-wiring-fixture-v1",
                ))
        pipeline = CrossSectionalFactorPipeline((
            FactorSpec("momentum", FactorDirection.HIGHER_BETTER),
            FactorSpec("risk", FactorDirection.LOWER_BETTER),
        ))
        sampling = P1SamplingConfig(mode="exponential_decay_v1", half_life_sessions=60)
        result = run_walk_forward_model_comparison(
            samples,
            trading_dates=trading_dates,
            prediction_dates=trading_dates[10:12],
            factor_pipeline=pipeline,
            ranker_config=RankerConfig(
                feature_names=("momentum", "risk"), horizon_days=3, min_group_size=5,
                n_estimators=10, min_child_samples=2,
            ),
            config=WalkForwardComparisonConfig(
                horizon_days=3, purge_sessions=3, minimum_training_dates=4,
                minimum_training_samples=24, training_sampling=sampling,
            ),
        )
        self.assertEqual("exponential_decay_v1", result.training_sampling_mode)
        self.assertEqual(sampling.version(), result.training_sampling_config_version)
        for fold in result.fold_audits:
            for model_key in ("ridge", "lambdarank"):
                audit = fold.model_metadata[model_key]["training_sampling"]
                self.assertEqual("exponential_decay_v1", audit["applied_mode"])
                self.assertEqual(sampling.version(), audit["config_version"])

    def test_sampling_rejects_trainers_that_do_not_consume_weights(self):
        with self.assertRaisesRegex(ValueError, "Ridge/LambdaRank"):
            WalkForwardComparisonConfig(
                horizon_days=3, purge_sessions=3,
                model_keys=("equal_weight", "top_tail"),
                training_sampling=P1SamplingConfig(mode="fixed_window_v1"),
            )


def _returns(sign: float = 1.0, count: int = 30) -> dict[str, float]:
    return {f"2026-01-{index + 1:02d}": sign * ((index % 7) - 3) / 100 for index in range(count)}


def _candidate(
    ticker: str, *, industry: str | None = "Tech", concepts: tuple[str, ...] = ("AI",),
    price: float = 10.0, atr: float | None = 1.0, atr_unit: str | None = "price",
) -> P1PortfolioCandidate:
    return P1PortfolioCandidate(
        ticker=ticker, score=1.0, entry_price=price, data_as_of=date(2026, 1, 31),
        industry=industry, concepts=concepts,
        atr_value=atr, atr_unit=atr_unit,
    )


class P1PortfolioRiskTests(TestCase):
    def _gate(self, candidates, *, positions=(), returns=None, scale=1.0, config=None):
        return apply_p1_portfolio_risk_gate(
            candidates,
            existing_positions=positions,
            returns_by_ticker=returns or {},
            portfolio_nav=1_000_000,
            cash=500_000,
            regime_position_scale=scale,
            config=config or P1PortfolioRiskConfig(market="CN"),
            decision_date=date(2026, 1, 31),
        )

    def test_existing_positions_count_toward_industry_limit(self):
        positions = (P1PortfolioPosition("HELD", 100_000, date(2026, 1, 31), "Tech", ("Cloud",)),)
        result = self._gate(
            (_candidate("A"), _candidate("B")), positions=positions,
            returns={"HELD": _returns(-1), "A": _returns(1), "B": _returns(-1)},
        )
        self.assertEqual(["A"], [item["ticker"] for item in result["selected"]])
        rejected = {item["ticker"]: item["reason_codes"] for item in result["rejected"]}
        self.assertIn("industry_concentration", rejected["B"])

    def test_concept_and_pair_correlation_limits_are_enforced(self):
        concept_config = P1PortfolioRiskConfig(market="CN", max_concept_names=1)
        concept_result = self._gate(
            (_candidate("A"), _candidate("B", industry="Finance")),
            returns={"A": _returns(1), "B": _returns(-1)}, config=concept_config,
        )
        self.assertIn("concept_concentration", concept_result["rejected"][0]["reason_codes"])
        corr_result = self._gate(
            (_candidate("A"), _candidate("B", industry="Finance", concepts=("Bank",))),
            returns={"A": _returns(1), "B": _returns(1)},
        )
        self.assertIn("pair_correlation_exceeded:A", corr_result["rejected"][0]["reason_codes"])

    def test_missing_correlation_is_explicit_and_fail_closed(self):
        result = self._gate(
            (_candidate("A"), _candidate("B", industry="Finance", concepts=("Bank",))),
            returns={"A": _returns(1), "B": _returns(1, count=5)},
        )
        self.assertIn("insufficient_correlation_history", result["rejected"][0]["reason_codes"])

    def test_price_and_relative_atr_stop_units_are_not_mixed(self):
        price_result = self._gate((_candidate("A", atr=2.0, atr_unit="price"),))
        relative_result = self._gate((_candidate("B", atr=0.05, atr_unit="relative"),))
        self.assertAlmostEqual(6.0, price_result["selected"][0]["stop_loss"])
        self.assertAlmostEqual(9.0, relative_result["selected"][0]["stop_loss"])

    def test_cn_lot_cash_and_regime_budgets_reach_quantities(self):
        result = self._gate((_candidate("A", price=333.0),))
        selected = result["selected"][0]
        self.assertEqual(300, selected["target_quantity"])
        self.assertAlmostEqual(selected["target_notional"] / 1_000_000, selected["target_weight"])
        blocked = self._gate((_candidate("B"),), scale=0.0)
        self.assertEqual(0, blocked["selected_count"])
        self.assertIn("new_position_budget_exhausted", blocked["rejected"][0]["reason_codes"])

    def test_existing_ticker_and_bad_position_value_are_rejected(self):
        position = P1PortfolioPosition("A", 100_000, date(2026, 1, 31), "Other", ("Other",))
        result = self._gate(
            (_candidate("A"),), positions=(position,),
            returns={"A": _returns(1)},
        )
        self.assertIn("already_held", result["rejected"][0]["reason_codes"])
        with self.assertRaises(ValueError):
            P1PortfolioPosition("BAD", float("nan"), date(2026, 1, 31), "Tech")

    def test_post_decision_candidate_or_return_data_is_rejected(self):
        future_candidate = replace(_candidate("A"), data_as_of=date(2026, 2, 1))
        with self.assertRaisesRegex(ValueError, "data_as_of"):
            self._gate((future_candidate,))
        with self.assertRaisesRegex(ValueError, "post-decision"):
            self._gate((_candidate("B"),), returns={"B": {"2026-02-01": 0.01}})


def _spec(*, experiment_count: int = 2) -> P2CandidateSpec:
    experiments = tuple(
        P2ExperimentRecord(
            experiment_id=f"exp-{index:02d}", candidate_id="candidate-a", status="SUCCESS",
            config_version=f"cfg-{index:02d}",
        )
        for index in range(experiment_count)
    )
    return P2CandidateSpec(
        candidate_id="candidate-a", model_version="model-v1",
        factor_set_version="factor-v1", sampling_config_version="sampling-v1",
        portfolio_risk_version="risk-v1", decision_policy_version="decision-v1",
        calibration_version="calibration-v1", evidence_versions=("evidence-b", "evidence-a"),
        experiments=experiments,
    )


def _open_dates(market: str, start: date, count: int) -> list[date]:
    values = []
    current = start
    while len(values) < count:
        if is_market_open_date(market, current):
            values.append(current)
        current += timedelta(days=1)
    return values


class P2ForwardProtocolTests(TestCase):
    def setUp(self):
        self.protocol = ExecutableSelectionProtocol(market="CN", experiment_budget=24)
        self.frozen_at = datetime(2026, 9, 22, 18, 0, tzinfo=timezone(timedelta(hours=8)))
        self.freeze = freeze_p2_forward_candidate(self.protocol, _spec(), frozen_at=self.frozen_at)

    def _observations(self, count: int, *, active_count: int, closed_lots: int):
        start = date.fromisoformat(self.freeze["forward_start_date"])
        dates = _open_dates("CN", start, count)
        base_lots, remainder = divmod(closed_lots, count)
        return tuple(
            P2ForwardObservation(
                observation_date=value,
                freeze_version=self.freeze["freeze_version"],
                protocol_id=self.freeze["protocol_id"],
                candidate_config_version=self.freeze["candidate_config_version"],
                active=index < active_count,
                closed_lots=base_lots + (1 if index < remainder else 0),
            )
            for index, value in enumerate(dates)
        )

    def test_freeze_is_deterministic_and_semantic_change_changes_version(self):
        same = freeze_p2_forward_candidate(self.protocol, _spec(), frozen_at=self.frozen_at)
        changed = freeze_p2_forward_candidate(
            self.protocol, replace(_spec(), model_version="model-v2"), frozen_at=self.frozen_at,
        )
        self.assertEqual(self.freeze["freeze_version"], same["freeze_version"])
        self.assertNotEqual(self.freeze["freeze_version"], changed["freeze_version"])
        self.assertFalse(self.freeze["protocol_approved"])
        self.assertFalse(self.freeze["production_deployed"])

    def test_freeze_enforces_experiment_budget_and_no_approved_protocol(self):
        with self.assertRaises(ValueError):
            freeze_p2_forward_candidate(self.protocol, _spec(experiment_count=25), frozen_at=self.frozen_at)
        approved = replace(
            self.protocol, approved=True, approved_by="owner",
            approved_at="2026-09-22T18:00:00+08:00",
        )
        with self.assertRaises(ValueError):
            freeze_p2_forward_candidate(approved, _spec(), frozen_at=self.frozen_at)

    def test_old_closed_or_mismatched_observations_fail_closed(self):
        valid = self._observations(1, active_count=1, closed_lots=1)[0]
        with self.assertRaises(ValueError):
            assess_p2_forward_progress(
                self.freeze, (replace(valid, observation_date=valid.observation_date - timedelta(days=1)),),
            )
        with self.assertRaises(ValueError):
            assess_p2_forward_progress(self.freeze, (valid, valid))
        with self.assertRaises(ValueError):
            assess_p2_forward_progress(self.freeze, (replace(valid, protocol_id="wrong"),))

    def test_all_three_forward_thresholds_are_required(self):
        too_few = assess_p2_forward_progress(
            self.freeze, self._observations(59, active_count=59, closed_lots=300),
        )
        self.assertEqual("COLLECTING", too_few["status"])
        inactive = assess_p2_forward_progress(
            self.freeze, self._observations(60, active_count=29, closed_lots=300),
        )
        self.assertEqual("COLLECTING", inactive["status"])
        too_few_lots = assess_p2_forward_progress(
            self.freeze, self._observations(60, active_count=30, closed_lots=199),
        )
        self.assertEqual("COLLECTING", too_few_lots["status"])

    def test_sufficient_window_only_reaches_independent_review(self):
        result = assess_p2_forward_progress(
            self.freeze, self._observations(60, active_count=30, closed_lots=200),
        )
        self.assertEqual("READY_FOR_INDEPENDENT_REVIEW", result["status"])
        self.assertEqual("BLOCKED", result["promotion_status"])
        self.assertFalse(result["protocol_approved"])
        self.assertFalse(result["production_deployed"])

    def test_economic_change_requires_new_forward_start(self):
        result = assess_p2_forward_progress(
            self.freeze, self._observations(60, active_count=30, closed_lots=200),
            economic_logic_changed=True,
        )
        self.assertEqual("RESTART_REQUIRED", result["status"])
        self.assertIn("economic_decision_logic_changed_new_freeze_required", result["blockers"])

    def test_freeze_artifact_is_content_addressed_and_idempotent(self):
        with TemporaryDirectory() as root:
            left = persist_p2_candidate_freeze(self.freeze, artifact_root=Path(root))
            right = persist_p2_candidate_freeze(self.freeze, artifact_root=Path(root))
        self.assertEqual(left["artifact"], right["artifact"])
