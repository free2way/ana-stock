from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api.presentation.screener_tactical import (
    format_lightgbm_history_bias,
    format_lightgbm_tactical_guidance,
    localize_lightgbm_tactical_results,
)
from app.api.routes import screener as screener_route
from app.services.stock_selection.lightgbm_tactical import (
    annotate_lightgbm_tactical_context,
    lightgbm_history_bias,
    lightgbm_tactical_guidance,
)


def _evaluation(*, pullback: tuple[int, float], breakout: tuple[int, float], watch: tuple[int, float], sample_count: int = 20) -> dict:
    return {
        "sample_count": sample_count,
        "windows": {
            "pullback": {1: {"count": pullback[0], "hit_rate": pullback[1]}},
            "breakout": {1: {"count": breakout[0], "hit_rate": breakout[1]}},
            "watch": {1: {"count": watch[0], "hit_rate": watch[1]}},
        },
    }


class LightGBMTacticalDecouplingTests(unittest.TestCase):
    def test_history_bias_is_language_neutral_and_copy_is_presentational(self) -> None:
        payload = _evaluation(pullback=(4, 50.0), breakout=(8, 62.5), watch=(3, 40.0))

        bias = lightgbm_history_bias(payload)

        self.assertEqual("breakout", bias.action_key)
        self.assertEqual(8, bias.sample_count)
        self.assertEqual("当前次日更偏 突破确认，命中率 62.5%。", format_lightgbm_history_bias(bias, lang="zh"))
        self.assertEqual(
            "1D currently leans Breakout with a 62.5% hit rate.",
            format_lightgbm_history_bias(bias, lang="en"),
        )
        self.assertIs(screener_route._lightgbm_history_bias, format_lightgbm_history_bias)

    def test_tactical_decision_does_not_branch_on_language(self) -> None:
        payload = _evaluation(pullback=(2, 40.0), breakout=(9, 66.7), watch=(1, 20.0))

        guidance = lightgbm_tactical_guidance(
            action_key="pullback",
            market_payload=payload,
            fallback_payload={},
        )

        self.assertEqual("avoid_early_pullback", guidance.tag_code)
        self.assertEqual("market_leans_breakout", guidance.note_code)
        zh_tag, zh_note = format_lightgbm_tactical_guidance(guidance, lang="zh")
        en_tag, en_note = format_lightgbm_tactical_guidance(guidance, lang="en")
        self.assertEqual("回踩不抢", zh_tag)
        self.assertIn("40.0%", zh_note)
        self.assertEqual("Avoid Early Pullback", en_tag)
        self.assertIn("40.0%", en_note)

    def test_structured_annotation_and_localization_are_separate_steps(self) -> None:
        evaluation = _evaluation(pullback=(7, 58.0), breakout=(3, 45.0), watch=(2, 30.0))
        evaluation["per_market"] = {"CN": evaluation}
        rows = [
            {
                "ticker": "600000.SS",
                "market": "CN",
                "model_entry_style": "pullback",
                "model_highlights": ["existing"],
            }
        ]

        annotate_lightgbm_tactical_context(
            rows,
            selected_market="CN",
            history_evaluation=evaluation,
            force_apply=True,
        )

        self.assertEqual("pullback", rows[0]["lightgbm_tactical_action"])
        self.assertEqual("wait_for_pullback", rows[0]["lightgbm_tactical_guidance"]["tag_code"])
        self.assertNotIn("lightgbm_tactical_tag", rows[0])

        localize_lightgbm_tactical_results(rows, lang="zh")

        self.assertEqual("回踩确认", rows[0]["lightgbm_tactical_tag"])
        self.assertEqual("existing", rows[0]["model_highlights"][1])

    def test_route_wrapper_only_loads_data_and_composes_domain_with_presentation(self) -> None:
        evaluation = _evaluation(pullback=(1, 20.0), breakout=(6, 70.0), watch=(2, 40.0))
        evaluation["per_market"] = {"CN": evaluation}
        rows = [{"ticker": "000001.SZ", "market": "CN", "action_label": "wait_for_breakout"}]

        with patch.object(screener_route, "build_lightgbm_prediction_evaluation", return_value=evaluation) as loader:
            screener_route._annotate_lightgbm_results(rows, selected_market="CN", lang="en", force_apply=True)

        loader.assert_called_once_with(market="CN", recent_runs=8, top_n=40)
        self.assertEqual("Follow Breakout", rows[0]["lightgbm_tactical_tag"])
        self.assertIn("70.0%", rows[0]["lightgbm_tactical_note"])


if __name__ == "__main__":
    unittest.main()
