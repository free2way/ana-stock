from __future__ import annotations

import statistics
from unittest import TestCase

from app.services.statistical_inference import (
    block_bootstrap_mean_ci,
    iid_mean_ci,
    newey_west_mean_ci,
    significance_report,
)


def _autocorrelated(seed: int = 7, count: int = 240) -> list[float]:
    # AR(1) with rho=0.7: overlapping-horizon style serial dependence.
    state = seed
    values: list[float] = []
    previous = 0.0
    for _ in range(count):
        state = (1103515245 * state + 12345) % (2**31)
        noise = (state / 2**31 - 0.5) * 2.0
        previous = 0.7 * previous + noise
        values.append(0.01 + previous * 0.01)
    return values


class NeweyWestTests(TestCase):
    def test_lag_zero_matches_iid(self) -> None:
        values = _autocorrelated()
        iid = iid_mean_ci(values)
        hac = newey_west_mean_ci(values, lag=0)
        # Same estimator family; iid uses the n-1 sample variance while the
        # Bartlett kernel uses 1/n, so the widths agree to ~1% for n=240.
        self.assertLess(abs(hac[2] / iid[2] - 1.0), 0.01)

    def test_positive_autocorrelation_widens_interval(self) -> None:
        values = _autocorrelated()
        iid = iid_mean_ci(values)
        hac = newey_west_mean_ci(values, lag=4)  # horizon 5 -> lag 4
        iid_width = iid[1] - iid[0]
        hac_width = hac[1] - hac[0]
        self.assertGreater(hac_width, iid_width)
        # both intervals centre on the sample mean
        self.assertAlmostEqual(statistics.fmean(values), (hac[0] + hac[1]) / 2, places=10)

    def test_comparison_sample_contains_all_methods(self) -> None:
        report = significance_report(_autocorrelated(), horizon_days=5)
        self.assertIn("iid_ci95", report)
        self.assertIn("newey_west_ci95", report)
        self.assertIn("block_bootstrap_ci95", report)
        self.assertEqual(4, report["newey_west_lag"])
        self.assertEqual(5, report["block_length"])
        self.assertTrue(report["iid_is_deprecated_for_promotion"])


class BlockBootstrapTests(TestCase):
    def test_bootstrap_is_deterministic_and_contains_mean(self) -> None:
        values = _autocorrelated()
        first = block_bootstrap_mean_ci(values, block_length=5, seed=42, iterations=400)
        second = block_bootstrap_mean_ci(values, block_length=5, seed=42, iterations=400)
        self.assertEqual(first, second)
        mean = statistics.fmean(values)
        self.assertLessEqual(first[0], mean)
        self.assertGreaterEqual(first[1], mean)

    def test_bootstrap_wider_than_iid_under_dependence(self) -> None:
        values = _autocorrelated()
        iid = iid_mean_ci(values)
        boot = block_bootstrap_mean_ci(values, block_length=10, seed=7, iterations=600)
        self.assertGreater(boot[1] - boot[0], (iid[1] - iid[0]) * 1.2)
