"""Baseline re-calibration for the activation drawdown gate (tail_drawdown_1pct_v2).

The legacy gate consumed the single worst sample path drawdown.  That is an
extreme-value statistic: under the executable net-return label (no clamping,
next-open entry, real costs) it degrades as the OOS sample grows, so a gate
calibrated on the old clamped composite would fire on almost every run.
tail_drawdown_1pct_v1 already consumed a tail mean, but at the scheduled
metrics base of five evaluation days x top-20 picks (100 rows) the 1% tail
is exactly one sample, so v1 still degenerated to the single worst value.
v2 sets the floor at the worst five draws (CVaR@95% at n=100); below five
samples the statistic remains the single worst sample, so small diagnostic
fixtures keep legacy semantics.  The extreme value itself stays available
as the record-only `worst_sample_drawdown` metric.
"""
import unittest

from app.services.model_evaluation import _activation_status, _tail_drawdown_1pct


class TailDrawdownStatisticTests(unittest.TestCase):
    def test_small_sample_degenerates_to_worst_single_sample(self):
        # Below five samples the statistic IS the worst sample: small
        # fixtures keep legacy semantics.
        self.assertEqual(_tail_drawdown_1pct([-1.0, -30.0, -2.0]), -30.0)
        self.assertEqual(_tail_drawdown_1pct([-3.5]), -3.5)

    def test_boundary_between_legacy_and_v2_semantics_is_five_samples(self):
        # n=4 -> single worst sample; n=5 -> mean of exactly those five.
        self.assertEqual(_tail_drawdown_1pct([-1.0, -30.0, -2.0, -1.5]), -30.0)
        self.assertAlmostEqual(
            _tail_drawdown_1pct([-1.0, -30.0, -2.0, -1.5, -2.5]), -7.4, places=6
        )

    def test_hundred_row_scheduled_base_consumes_worst_five_not_the_extreme(self):
        # The production metrics base is five scheduled days x top-20 picks
        # (100 rows).  v1's 1% tail was exactly one entry there, so the
        # single -22.55% loser flipped the gate by itself.  v2 consumes the
        # CVaR@95% tail (worst five): the series descends to -7.9, so the
        # tail is (-22.55 - 7.9 - 7.85 - 7.8 - 7.75) / 5 = -10.77.
        drawdowns = [-22.55] + [-3.0 - 0.05 * i for i in range(99)]
        self.assertAlmostEqual(_tail_drawdown_1pct(drawdowns), -10.77, places=6)
        self.assertGreater(_tail_drawdown_1pct(drawdowns), -20.0)

    def test_large_sample_uses_mean_of_worst_one_percent_tail(self):
        # 1000 samples -> tail = max(5, 10) = 10 worst entries -> mean -25.5,
        # not the -30.0 extreme.  This is what keeps the gate stable as OOS
        # coverage grows.
        drawdowns = [-1.0] * 990 + [-21.0, -22.0, -23.0, -24.0, -25.0,
                                   -26.0, -27.0, -28.0, -29.0, -30.0]
        self.assertAlmostEqual(_tail_drawdown_1pct(drawdowns), -25.5, places=4)

    def test_single_breached_sample_no_longer_flips_large_oos_window(self):
        # One fat-tail loser inside 764 samples (eval 92 shape) must not
        # breach a -20% gate by itself anymore.
        drawdowns = [-2.0] * 763 + [-24.0]
        self.assertGreater(_tail_drawdown_1pct(drawdowns), -20.0)

    def test_broad_tail_still_breaches(self):
        # When the tail itself is broken the gate must still fire: the
        # recalibration is statistical, not a loosening.
        drawdowns = [-2.0] * 700 + [-19.0] * 40 + [-25.0] * 24
        self.assertLessEqual(_tail_drawdown_1pct(drawdowns), -20.0)

    def test_uniform_losses_pass_through_unchanged(self):
        self.assertAlmostEqual(
            _tail_drawdown_1pct([-4.0] * 500), -4.0, places=4
        )


class ActivationGateV2Tests(unittest.TestCase):
    """End-to-end gate semantics: v2 changes the statistic, not the policy."""

    def test_single_extreme_with_healthy_cvar_tail_is_eligible(self):
        drawdowns = [-22.55] + [-3.0 - 0.05 * i for i in range(99)]
        primary = {
            "avg_return": 0.8,
            "max_drawdown": round(_tail_drawdown_1pct(drawdowns), 4),
        }
        self.assertEqual(
            "eligible_for_champion_review",
            _activation_status(
                strict_sample_count=100,
                strict_coverage_days=20,
                strict_metrics={5: primary},
            ),
        )

    def test_breached_cvar_tail_still_blocks(self):
        primary = {"avg_return": 0.8, "max_drawdown": -21.0}
        self.assertEqual(
            "observation_drawdown_breach",
            _activation_status(
                strict_sample_count=100,
                strict_coverage_days=20,
                strict_metrics={5: primary},
            ),
        )

    def test_negative_net_return_still_blocks(self):
        primary = {"avg_return": -0.5, "max_drawdown": -2.0}
        self.assertEqual(
            "observation_negative_net",
            _activation_status(
                strict_sample_count=100,
                strict_coverage_days=20,
                strict_metrics={5: primary},
            ),
        )

    def test_insufficient_oos_still_blocks_before_the_gate_is_reached(self):
        primary = {"avg_return": 0.8, "max_drawdown": -1.0}
        self.assertEqual(
            "observation_insufficient_oos",
            _activation_status(
                strict_sample_count=99,
                strict_coverage_days=20,
                strict_metrics={5: primary},
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
