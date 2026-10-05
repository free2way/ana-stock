"""Pure-function tests for the training ablation harness (multi-seed additions).

Covers the variant registry (including the new ``fit_risk_adjusted`` switch),
the ``--seed`` parser, and the cross-seed summary / direction-support helpers.
The training path itself is exercised by ``test_trainer_fit_target.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "run_training_ablation", ROOT / "scripts" / "run_training_ablation.py"
)
assert _spec is not None and _spec.loader is not None
ablation = importlib.util.module_from_spec(_spec)
sys.modules["run_training_ablation"] = ablation
_spec.loader.exec_module(ablation)


class VariantRegistryTests(TestCase):
    def test_required_variants_exist(self) -> None:
        for key in (
            "canonical",
            "winsor_off",
            "drawdown_off",
            "fit_risk_adjusted",
            "all_off",
        ):
            self.assertIn(key, ablation.VARIANTS, key)

    def test_fit_risk_adjusted_switches_fit_target(self) -> None:
        variant = ablation.VARIANTS["fit_risk_adjusted"]
        self.assertTrue(variant.overrides["trainer_fit_on_risk_adjusted"])
        self.assertNotIn("trainer_drawdown_penalty", variant.overrides)
        self.assertFalse(
            ablation.VARIANTS["all_off"].overrides["trainer_fit_on_risk_adjusted"]
        )


class SeedParserTests(TestCase):
    def test_default_is_single_historical_seed(self) -> None:
        self.assertEqual([42], ablation._resolve_seeds(None))
        self.assertEqual([42], ablation._resolve_seeds([]))

    def test_repeatable_and_comma_separated(self) -> None:
        self.assertEqual([1, 2, 3], ablation._resolve_seeds(["1", "2,3"]))
        self.assertEqual([1, 2], ablation._resolve_seeds(["1,2", "1"]))

    def test_invalid_seed_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            ablation._resolve_seeds(["abc"])
        with self.assertRaises(SystemExit):
            ablation._resolve_seeds(["-1"])


def _record(key, seed, net, ra, pdr=0.5):
    return {
        "key": key,
        "label": key,
        "seed": seed,
        "run": {
            "metrics": {
                "mean_net_return": net,
                "mean_risk_adjusted_return": ra,
                "positive_date_rate": pdr,
            }
        },
    }


class AggregationTests(TestCase):
    def test_stat_reports_mean_and_dispersion(self) -> None:
        stat = ablation._stat([1.0, 2.0, 3.0])
        self.assertEqual(3, stat["n"])
        self.assertAlmostEqual(2.0, stat["mean"])
        self.assertAlmostEqual(1.0, stat["std"], places=8)
        self.assertIsNone(ablation._stat([None, "x"]))

    def test_aggregate_groups_by_variant_across_seeds(self) -> None:
        records = [
            _record("canonical", 42, 0.02, 0.01),
            _record("canonical", 43, 0.04, 0.02),
            _record("fit_risk_adjusted", 42, 0.03, 0.03),
            _record("fit_risk_adjusted", 43, 0.05, 0.04),
        ]
        summary = ablation._aggregate_variants(records, ("mean_net_return",))
        by_key = {item["key"]: item for item in summary}
        self.assertEqual(2, by_key["canonical"]["seed_count"])
        self.assertAlmostEqual(
            0.03, by_key["canonical"]["metrics"]["mean_net_return"]["mean"]
        )
        self.assertEqual([42, 43], by_key["canonical"]["seeds"])

    def test_direction_support_requires_two_thirds_of_seeds(self) -> None:
        by_key = {
            "canonical": {42: 0.01, 43: 0.02, 44: 0.03},
            "fit_risk_adjusted": {42: 0.02, 43: 0.03, 44: 0.01},
        }
        supported = ablation._direction_support(
            by_key,
            baseline_key="canonical",
            key="fit_risk_adjusted",
            metric="mean_risk_adjusted_return",
        )
        self.assertEqual(2, supported["support_threshold"])
        self.assertTrue(supported["supported"])
        self.assertEqual("higher", supported["direction"])
        self.assertEqual({"42": 0.01, "43": 0.01, "44": -0.02}, supported["per_seed_delta"])

        weaker = ablation._direction_support(
            {
                "canonical": {42: 0.01, 43: 0.02, 44: 0.03},
                "fit_risk_adjusted": {42: 0.02, 43: 0.01, 44: 0.03},
            },
            baseline_key="canonical",
            key="fit_risk_adjusted",
            metric="mean_risk_adjusted_return",
        )
        self.assertEqual("mixed", weaker["direction"])
        self.assertFalse(weaker["supported"])


if __name__ == "__main__":
    import unittest

    unittest.main()
