from __future__ import annotations

import json
import random
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import TestCase

from app.services.stock_selection.experiment_framework import (
    PROMOTION_DECISION_ELIGIBLE,
    ExperimentRow,
    PerTicketTradabilityConfig,
    apply_per_ticket_gate,
    benjamini_hochberg,
    build_in_sample_rows,
    evaluate_experiment,
    experiment_promotion_evidence,
    freeze_dataset_hash,
    independent_date_count,
    layer_partition,
    load_experiment_spec,
    per_ticket_gate_summary,
    persist_experiment_report,
    spec_from_mapping,
    tightened_min_effect_pp,
)

OOS_START = date(2026, 1, 1)


def _spec(**decision_overrides: object):
    decision = {
        "primary_metric": "net_return",
        "min_effect_pp": 0.2,
        "alpha": 0.05,
        "fdr": 0.05,
        "min_independent_dates": 5,
        "strict_t_threshold": 3.5,
        "bootstrap_iterations": 500,
    }
    decision.update(decision_overrides)
    return spec_from_mapping(
        {
            "experiment_id": "fixture-experiment",
            "market": "CN",
            "hypothesis": "改动应提升 OOS 命中率",
            "change": {"kind": "factor_patch", "patch": {"factor": "momentum"}},
            "panel": {
                "oos_start": OOS_START.isoformat(),
                "oos_end": "2026-12-31",
                "universe_version": "universe-v1",
                "label_version": "label-v1",
                "cost_bps": 0.0,
                "horizons": [5],
                "top_n": 3,
            },
            "strata": ["regime"],
            "decision": decision,
        }
    )


def _rows(
    *,
    delta: float,
    seed: int,
    start: date = OOS_START,
    n_days: int = 120,
    n_tickers: int = 6,
    sigma: float = 0.004,
    delta_only_regime: str | None = None,
) -> list[ExperimentRow]:
    rng = random.Random(seed)
    rows: list[ExperimentRow] = []
    for day in range(n_days):
        feature_date = start + timedelta(days=day)
        regime = "up" if day % 2 == 0 else "down"
        arm_delta = delta if (delta_only_regime is None or regime == delta_only_regime) else 0.0
        for offset in range(n_tickers):
            net_return = arm_delta + rng.gauss(0.0, sigma)
            rows.append(
                ExperimentRow(
                    feature_date=feature_date,
                    ticker=f"T{offset}",
                    raw_score=float(n_tickers - offset),
                    net_return=net_return,
                    strata={"regime": regime},
                )
            )
    return rows


class SelectionExperimentFrameworkTests(TestCase):
    def test_significant_positive_effect_passes(self) -> None:
        report = evaluate_experiment(
            _spec(),
            treated_rows=_rows(delta=0.006, seed=11),
            control_rows=_rows(delta=0.0, seed=12),
        )
        self.assertEqual("confirmed_alive", report.classification)
        self.assertEqual("PASS", report.decision)
        self.assertGreater(report.overall.ci95[0], 0.0)
        self.assertGreaterEqual(report.overall.effect_pp, report.effective_min_effect_pp)
        self.assertTrue(all(check.passed for check in report.checks))

    def test_noise_is_classified_noise(self) -> None:
        report = evaluate_experiment(
            _spec(min_effect_pp=0.0),
            treated_rows=_rows(delta=0.0, seed=21),
            control_rows=_rows(delta=0.0, seed=22),
        )
        self.assertEqual("noise", report.classification)
        self.assertEqual("REJECT", report.decision)

    def test_reversed_effect_is_strict(self) -> None:
        report = evaluate_experiment(
            _spec(min_effect_pp=0.0),
            treated_rows=_rows(delta=-0.006, seed=31),
            control_rows=_rows(delta=0.0, seed=32),
        )
        self.assertEqual("reversed_strict", report.classification)
        self.assertEqual("REJECT", report.decision)

    def test_multi_bucket_fdr(self) -> None:
        # 只在 regime=up 的日期施加正效应，down 桶为噪声。
        report = evaluate_experiment(
            _spec(min_effect_pp=0.0),
            treated_rows=_rows(delta=0.006, seed=41, delta_only_regime="up"),
            control_rows=_rows(delta=0.0, seed=42),
        )
        buckets = {bucket.bucket: bucket for bucket in report.strata}
        self.assertEqual({"up", "down"}, set(buckets))
        up, down = buckets["up"], buckets["down"]
        self.assertIsNotNone(up.q_value)
        self.assertIsNotNone(down.q_value)
        self.assertLess(up.p_value, 0.001)
        self.assertGreater(down.p_value, 0.05)
        self.assertLess(up.q_value, down.q_value)
        # BH 家族包含总体 + 两个桶：家族校正后 up 仍显著、down 不显著。
        self.assertLessEqual(up.q_value, 0.05)
        self.assertGreater(down.q_value, 0.05)
        self.assertLessEqual(up.q_value, up.p_value * 3)

    def test_benjamini_hochberg_reference_values(self) -> None:
        self.assertEqual([0.003, 0.06, 0.2], [round(v, 6) for v in benjamini_hochberg([0.001, 0.04, 0.2])])
        self.assertEqual([], benjamini_hochberg([]))
        # q 值单调不低于原始 p，且不超过 1。
        q_values = benjamini_hochberg([0.2, 0.001, 0.04])
        self.assertTrue(all(q >= p for q, p in zip(q_values, [0.2, 0.001, 0.04])))
        self.assertTrue(all(0.0 <= q <= 1.0 for q in q_values))

    def test_attempts_tightening_rejects_same_effect(self) -> None:
        treated = _rows(delta=0.0045, seed=51)
        control = _rows(delta=0.0, seed=52)
        first = evaluate_experiment(_spec(), treated_rows=treated, control_rows=control, attempts=1)
        settled = evaluate_experiment(_spec(), treated_rows=treated, control_rows=control, attempts=1000)
        self.assertEqual("PASS", first.decision)
        self.assertEqual("REJECT", settled.decision)
        self.assertGreater(settled.effective_min_effect_pp, first.effective_min_effect_pp)
        self.assertEqual("confirmed_alive", settled.classification)
        failed = {check.key for check in settled.checks if not check.passed}
        self.assertEqual({"min_effect_pp"}, failed)

    def test_tightening_factor_monotonic_and_identity_at_first_attempt(self) -> None:
        base, factor_first = tightened_min_effect_pp(0.3, 1)
        self.assertAlmostEqual(0.3, base)
        self.assertAlmostEqual(1.0, factor_first)
        previous = base
        for attempts in (2, 5, 20, 100, 1000):
            tightened, _ = tightened_min_effect_pp(0.3, attempts)
            self.assertGreater(tightened, previous)
            previous = tightened

    def test_deterministic_report_and_files(self) -> None:
        spec = _spec()
        treated = _rows(delta=0.006, seed=61)
        control = _rows(delta=0.0, seed=62)
        first = evaluate_experiment(spec, treated_rows=treated, control_rows=control)
        second = evaluate_experiment(spec, treated_rows=treated, control_rows=control)
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.report_digest, second.report_digest)
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            first_files = persist_experiment_report(first, output_dir=Path(left))
            second_files = persist_experiment_report(second, output_dir=Path(right))
            self.assertEqual(first_files.json_path.read_bytes(), second_files.json_path.read_bytes())
            self.assertEqual(first_files.markdown_path.read_bytes(), second_files.markdown_path.read_bytes())

    def test_same_panel_is_enforced(self) -> None:
        treated = _rows(delta=0.006, seed=71)
        control = [row for row in _rows(delta=0.0, seed=72) if row.feature_date != OOS_START + timedelta(days=10)]
        with self.assertRaises(ValueError):
            evaluate_experiment(_spec(), treated_rows=treated, control_rows=control)

    def test_train_only_classification(self) -> None:
        # train 区有强效应，OOS 区为噪声 -> train_only。
        train_start = date(2025, 9, 1)
        spec = _spec(min_effect_pp=0.0)
        treated = _rows(delta=0.0, seed=81, start=train_start, n_days=200)
        control = _rows(delta=0.0, seed=82, start=train_start, n_days=200)
        treated_train = _rows(delta=0.008, seed=83, start=train_start, n_days=120)
        in_sample = build_in_sample_rows(
            treated_rows=treated_train, control_rows=control, oos_start=OOS_START
        )
        report = evaluate_experiment(
            spec,
            treated_rows=treated,
            control_rows=control,
            in_sample_rows=in_sample,
        )
        self.assertEqual("train_only", report.classification)
        self.assertEqual("REJECT", report.decision)

    def test_yaml_and_json_spec_are_equivalent(self) -> None:
        payload = {
            "experiment_id": "yaml-experiment",
            "market": "CN",
            "hypothesis": "yaml 与 json 等价",
            "change": {"kind": "factor_patch", "patch": {"factor": "momentum"}},
            "panel": {
                "oos_start": "2026-01-01",
                "oos_end": "2026-03-01",
                "universe_version": "u1",
                "label_version": "l1",
                "cost_bps": 20,
                "horizons": [1, 5],
                "top_n": 5,
            },
            "strata": ["regime", "industry"],
            "decision": {
                "primary_metric": "net_return",
                "min_effect_pp": 0.3,
                "alpha": 0.05,
                "fdr": 0.1,
                "min_independent_dates": 10,
            },
        }
        yaml_text = (
            "experiment_id: yaml-experiment\n"
            "market: CN\n"
            "hypothesis: yaml 与 json 等价\n"
            "change:\n"
            "  kind: factor_patch\n"
            "  patch:\n"
            "    factor: momentum\n"
            "panel:\n"
            "  oos_start: 2026-01-01\n"
            "  oos_end: 2026-03-01\n"
            "  universe_version: u1\n"
            "  label_version: l1\n"
            "  cost_bps: 20\n"
            "  horizons: [1, 5]\n"
            "  top_n: 5\n"
            "strata:\n"
            "  - regime\n"
            "  - industry\n"
            "decision:\n"
            "  primary_metric: net_return\n"
            "  min_effect_pp: 0.3\n"
            "  alpha: 0.05\n"
            "  fdr: 0.1\n"
            "  min_independent_dates: 10\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            yaml_path = Path(directory) / "spec.yaml"
            json_path = Path(directory) / "spec.json"
            yaml_path.write_text(yaml_text, encoding="utf-8")
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            from_yaml = load_experiment_spec(yaml_path)
            from_json = load_experiment_spec(json_path)
        self.assertEqual(from_json, from_yaml)
        self.assertEqual((1, 5), from_yaml.panel.horizons)
        self.assertEqual(("regime", "industry"), from_yaml.strata)

    def test_dataset_hash_and_independent_dates(self) -> None:
        first = freeze_dataset_hash(
            market="CN",
            factor_set_key="original_v1",
            label_version="l1",
            universe_version="u1",
            source_version="market_lake_v1:CN:abc",
        )
        second = freeze_dataset_hash(
            market="CN",
            factor_set_key="original_v1",
            label_version="l1",
            universe_version="u1",
            source_version="market_lake_v1:CN:abc",
        )
        drifted = freeze_dataset_hash(
            market="CN",
            factor_set_key="original_v1",
            label_version="l2",
            universe_version="u1",
            source_version="market_lake_v1:CN:abc",
        )
        self.assertEqual(first.dataset_hash, second.dataset_hash)
        self.assertNotEqual(first.dataset_hash, drifted.dataset_hash)
        self.assertEqual("market_lake_v1:CN:abc", first.source_version)
        daily = [OOS_START + timedelta(days=offset) for offset in range(20)]
        self.assertEqual(20, independent_date_count(daily, horizon_days=1))
        self.assertEqual(4, independent_date_count(daily, horizon_days=5))

    def test_promotion_gate_bridge(self) -> None:
        from app.services.stock_selection.promotion_gate_v2 import (
            DECISION_ELIGIBLE,
            _statistical_gate_check,
        )

        self.assertEqual(DECISION_ELIGIBLE, PROMOTION_DECISION_ELIGIBLE)
        passing = evaluate_experiment(
            _spec(),
            treated_rows=_rows(delta=0.006, seed=91),
            control_rows=_rows(delta=0.0, seed=92),
        )
        rejected = evaluate_experiment(
            _spec(min_effect_pp=0.0),
            treated_rows=_rows(delta=0.0, seed=93),
            control_rows=_rows(delta=0.0, seed=94),
        )
        eligible_check = _statistical_gate_check(experiment_promotion_evidence(passing))
        reject_check = _statistical_gate_check(experiment_promotion_evidence(rejected))
        self.assertEqual("PASS", eligible_check.status)
        self.assertEqual("FAIL", reject_check.status)
        self.assertIn("random_control_confirmed", reject_check.detail)


# ---------------------------------------------------------------------------
# A. 排序字段敏感性
# ---------------------------------------------------------------------------


def _flip_spec(**decision_overrides: object):
    raw = {
        "experiment_id": "ranking-flip-experiment",
        "market": "CN",
        "hypothesis": "排序字段符号反转必须被拦下",
        "change": {"kind": "factor_patch", "patch": {"factor": "momentum"}},
        "panel": {
            "oos_start": OOS_START.isoformat(),
            "oos_end": "2026-12-31",
            "universe_version": "universe-v1",
            "label_version": "label-v1",
            "cost_bps": 0.0,
            "horizons": [5],
            "top_n": 3,
        },
        "strata": [],
        "ranking_fields": ["field1", "field2"],
        "decision": {
            "primary_metric": "net_return",
            "min_effect_pp": 0.2,
            "alpha": 0.05,
            "fdr": 0.05,
            "min_independent_dates": 3,
            "strict_t_threshold": 3.5,
            "bootstrap_iterations": 500,
        },
    }
    for key, value in decision_overrides.items():
        raw["decision"][key] = value
    return spec_from_mapping(raw)


def _flip_rows(*, pool: str, seed: int, n_days: int = 30, sigma: float = 0.0003) -> list[ExperimentRow]:
    """构造排序字段符号反转场景。

    - W（3 票，+0.4pp）：field1 高、field2 低。
    - L（3 票，-0.4pp）：field1 低、field2 高。
    - M（3 票，0.0）：仅存在于 control（全池），两个字段都最高但被逐票门槛排除。
    treated=W+L，control=W+L+M；top_n=3。
    field1 -> treated 取 W(+)，field2 -> treated 取 L(-)，符号反转。
    """

    rng = random.Random(seed)
    groups = {
        "W": (("W1", "W2", "W3"), +0.004, {"field1": 8.0, "field2": 1.0}),
        "L": (("L1", "L2", "L3"), -0.004, {"field1": 1.0, "field2": 8.0}),
        "M": (("M1", "M2", "M3"), 0.000, {"field1": 10.0, "field2": 10.0}),
    }
    selected_groups = ("W", "L") if pool == "treated" else ("W", "L", "M")
    rows: list[ExperimentRow] = []
    for day in range(n_days):
        feature_date = OOS_START + timedelta(days=day)
        for group in selected_groups:
            tickers, base_return, scores = groups[group]
            for ticker in tickers:
                rows.append(
                    ExperimentRow(
                        feature_date=feature_date,
                        ticker=ticker,
                        raw_score=scores["field1"],
                        net_return=base_return + rng.gauss(0.0, sigma),
                        strata={"regime": "up"},
                        ranking_values=dict(scores),
                    )
                )
    return rows


class RankingSensitivityTests(TestCase):
    def test_sign_flip_blocks_confirmed_alive_and_pass(self) -> None:
        treated = _flip_rows(pool="treated", seed=101)
        control = _flip_rows(pool="control", seed=102)
        report = evaluate_experiment(_flip_spec(), treated_rows=treated, control_rows=control)
        sensitivity = dict(report.ranking_sensitivity)
        self.assertFalse(sensitivity["stable"])
        per_field = dict(sensitivity["per_field"])
        self.assertEqual({1, -1}, {per_field["field1"]["sign"], per_field["field2"]["sign"]})
        self.assertNotIn(report.classification, {"confirmed_alive", "train_only"})
        self.assertIn(report.classification, {"noise", "reversed_strict"})
        self.assertEqual("REJECT", report.decision)
        ranking_check = next(check for check in report.checks if check.key == "ranking_stability")
        self.assertFalse(ranking_check.passed)
        self.assertIn("排序不稳定", ranking_check.detail)

    def test_single_stable_field_can_pass(self) -> None:
        # 同面板只用 field1 排序 -> 同号，正常判定（证明上例确实是被敏感性检查拦下）。
        treated = _flip_rows(pool="treated", seed=111)
        control = _flip_rows(pool="control", seed=112)
        single_raw = {
            "experiment_id": "ranking-flip-experiment",
            "market": "CN",
            "hypothesis": "单字段稳定",
            "change": {"kind": "factor_patch", "patch": {"factor": "momentum"}},
            "panel": {
                "oos_start": OOS_START.isoformat(),
                "oos_end": "2026-12-31",
                "universe_version": "universe-v1",
                "label_version": "label-v1",
                "cost_bps": 0.0,
                "horizons": [5],
                "top_n": 3,
            },
            "strata": [],
            "ranking_fields": ["field1"],
            "decision": {
                "primary_metric": "net_return",
                "min_effect_pp": 0.2,
                "alpha": 0.05,
                "fdr": 0.05,
                "min_independent_dates": 3,
                "strict_t_threshold": 3.5,
                "bootstrap_iterations": 500,
            },
        }
        report = evaluate_experiment(spec_from_mapping(single_raw), treated_rows=treated, control_rows=control)
        self.assertTrue(report.ranking_sensitivity["stable"])
        self.assertEqual("confirmed_alive", report.classification)
        self.assertEqual("PASS", report.decision)

    def test_ranking_fields_validation(self) -> None:
        with self.assertRaises(ValueError):
            spec_from_mapping(
                {
                    "experiment_id": "dup",
                    "market": "CN",
                    "hypothesis": "h",
                    "change": {"kind": "k", "patch": {}},
                    "panel": {
                        "oos_start": "2026-01-01",
                        "oos_end": "2026-02-01",
                        "universe_version": "u",
                        "label_version": "l",
                        "cost_bps": 0,
                        "horizons": [5],
                        "top_n": 3,
                    },
                    "ranking_fields": ["field1", "field1"],
                }
            )
        self.assertEqual(("raw_score",), _spec().ranking_fields)


# ---------------------------------------------------------------------------
# B. 逐票可成交门槛（与 regime 解耦）
# ---------------------------------------------------------------------------


def _gate_row(
    *,
    ticker: str,
    feature_date: date = OOS_START,
    limit_up: float | None = 0.0,
    volume: float | None = 1_000_000.0,
    dollar_volume: float | None = 100_000_000.0,
    regime: str = "up",
) -> ExperimentRow:
    values: dict[str, float] = {}
    if limit_up is not None:
        values["limit_up_today"] = limit_up
    if volume is not None:
        values["volume"] = volume
    if dollar_volume is not None:
        values["dollar_volume"] = dollar_volume
    return ExperimentRow(
        feature_date=feature_date,
        ticker=ticker,
        raw_score=1.0,
        net_return=0.001,
        strata={"regime": regime},
        tradability_values=values,
    )


class PerTicketTradabilityGateTests(TestCase):
    def test_gate_excludes_limit_up_zero_volume_and_low_liquidity(self) -> None:
        rows = [
            _gate_row(ticker=f"OK{index}", dollar_volume=1_000_000_000.0 - index * 1_000_000.0)
            for index in range(10)
        ]
        rows.append(_gate_row(ticker="LIMITUP", limit_up=1.0, dollar_volume=2_000_000_000.0))
        rows.append(_gate_row(ticker="SUSPENDED", volume=0.0, dollar_volume=2_000_000_000.0))
        rows.append(_gate_row(ticker="THIN", dollar_volume=1.0))
        config = PerTicketTradabilityConfig(min_dollar_volume_percentile=20.0)
        kept = {row.ticker for row in apply_per_ticket_gate(rows, config)}
        self.assertNotIn("LIMITUP", kept)
        self.assertNotIn("SUSPENDED", kept)
        self.assertNotIn("THIN", kept)
        summary = per_ticket_gate_summary(rows, config)
        self.assertEqual(1, summary["excluded_limit_up_today"])
        self.assertEqual(1, summary["excluded_zero_volume"])
        self.assertGreaterEqual(summary["excluded_low_liquidity"], 1)

    def test_gate_is_invariant_to_regime(self) -> None:
        # 同样的逐票特征，仅 regime 不同 -> 门槛判定必须一致。
        config = PerTicketTradabilityConfig(min_dollar_volume_percentile=None)
        self.assertEqual([], apply_per_ticket_gate([_gate_row(ticker="X", limit_up=1.0, regime="market_up")], config))
        self.assertEqual([], apply_per_ticket_gate([_gate_row(ticker="Y", limit_up=1.0, regime="market_down")], config))
        keep_up = _gate_row(ticker="Z", regime="market_up")
        keep_down = _gate_row(ticker="Z", regime="market_down")
        self.assertEqual(1, len(apply_per_ticket_gate([keep_up], config)))
        self.assertEqual(1, len(apply_per_ticket_gate([keep_down], config)))

    def test_missing_fields_are_lenient_by_default_and_strict_when_required(self) -> None:
        row = _gate_row(ticker="UNKNOWN", limit_up=None, volume=None, dollar_volume=None)
        lenient = PerTicketTradabilityConfig(min_dollar_volume_percentile=None)
        self.assertEqual(1, len(apply_per_ticket_gate([row], lenient)))
        strict = PerTicketTradabilityConfig(min_dollar_volume_percentile=None, require_known_fields=True)
        self.assertEqual([], apply_per_ticket_gate([row], strict))
        summary = per_ticket_gate_summary([row], strict)
        self.assertEqual(1, summary["missing_field_counts"]["limit_up_today"])

    def test_percentile_threshold_is_per_date_and_regime_free(self) -> None:
        day_one = OOS_START
        rows = [
            _gate_row(ticker="A1", feature_date=day_one, dollar_volume=100.0, regime="up"),
            _gate_row(ticker="A2", feature_date=day_one, dollar_volume=200.0, regime="down"),
        ]
        config = PerTicketTradabilityConfig(min_dollar_volume_percentile=50.0)
        kept = {row.ticker for row in apply_per_ticket_gate(rows, config)}
        # 50 分位阈值 = 排序后索引 1 -> 200；A1 被剔除、A2 保留。
        self.assertEqual({"A2"}, kept)
        layer = layer_partition(("liquidity_bucket", "regime", "tradability_status"))
        self.assertEqual(["liquidity_bucket"], layer["per_ticket"])
        self.assertEqual(["regime", "tradability_status"], layer["regime_layer"])


class RunnerRowParsingTests(TestCase):
    def test_ranking_and_tradability_values_are_parsed_from_panel_rows(self) -> None:
        from scripts.run_selection_experiment import _row_from_mapping

        row = _row_from_mapping(
            {
                "date": "2026-08-27",
                "ticker": "600000.SS",
                "score": 78.0,
                "net_return": 0.01,
                "gross_return": 0.015,
                "trend_score": 78.0,
                "momentum_20": 2.17,
                "limit_up_today": True,
                "volume": 1_000_000.0,
                "dollar_volume": 9.9e8,
            },
            label="treated",
            index=0,
        )
        self.assertEqual(78.0, row.ranking_values["trend_score"])
        self.assertEqual(2.17, row.ranking_values["momentum_20"])
        self.assertEqual(1.0, row.tradability_values["limit_up_today"])
        self.assertEqual(9.9e8, row.tradability_values["dollar_volume"])
        self.assertEqual(78.0, row.ranking_values["raw_score"])



