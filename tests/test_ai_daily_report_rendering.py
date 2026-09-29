import unittest

from app.services.ai_daily_report import (
    _build_market_strategy,
    _hydrate_security_names,
    _render_market_candidate_note,
    render_ai_daily_report_message,
    render_ai_daily_report_push_messages,
)


class _FakeSymbolRepository:
    def list_overviews_for_tickers(self, tickers: list[str]) -> dict[str, dict]:
        assert "600000.SS" in tickers
        return {
            "600000.SS": {
                "ticker": "600000.SS",
                "name": "浦发银行",
                "market": "CN",
            }
        }


class AiDailyReportRenderingTests(unittest.TestCase):
    def test_full_market_note_separates_scan_coverage_and_deep_review_limit(self) -> None:
        note = _render_market_candidate_note(
            source="fresh_snapshot", market="CN", candidate_count=500,
            snapshot_templates_ready=8, snapshot_date="2026-09-16",
            snapshot_rows=5390, unique_candidates_scored=5011,
            deep_review_candidate_count=500,
        )

        self.assertIn("扫描 5390 条模板结果", note)
        self.assertIn("覆盖并评分 5011 只证券", note)
        self.assertIn("排名前 500 只进入深度复核", note)

    def test_hydrates_code_only_name_from_symbol_master(self) -> None:
        rows = [{"ticker": "600000.SH", "name": "600000", "market": "CN"}]

        _hydrate_security_names(_FakeSymbolRepository(), rows)

        self.assertEqual("浦发银行", rows[0]["name"])

    def test_recommendations_show_name_and_hide_strong_watch_pool(self) -> None:
        report = {
            "mood": "均衡观察",
            "headline": "测试日报",
            "strategy": {"headline": "等待确认", "playbook": "控制仓位", "bullets": []},
            "portfolio_summary": {},
            "portfolio_rows": [],
            "market_recommendations": [
                {
                    "ticker": "600000.SS",
                    "name": "浦发银行",
                    "market": "CN",
                    "verdict": "BUY",
                    "buy_zone": {"low": 10.0, "high": 10.5},
                    "take_profit": {"low": 11.0, "high": 11.5},
                    "risk_flags": [],
                }
            ],
            "market_watch_recommendations": [
                {"ticker": "000001.SZ", "name": "平安银行", "verdict": "WATCH"}
            ],
            "market_recommendations_meta": {
                "status": "ready",
                "note": "可执行买入池 1 只，强势观察池 1 只。",
            },
        }

        full_message = render_ai_daily_report_message(report)
        push_messages = render_ai_daily_report_push_messages(report)
        push_text = "\n".join(f"{item['title']}\n{item['body']}" for item in push_messages)

        self.assertIn("浦发银行（600000.SS）", full_message)
        self.assertIn("浦发银行（600000.SS）", push_text)
        self.assertNotIn("强势观察池", full_message)
        self.assertNotIn("强势观察池", push_text)
        self.assertNotIn("平安银行", full_message)
        self.assertNotIn("平安银行", push_text)

    def test_strategy_labels_include_security_names(self) -> None:
        strategy = _build_market_strategy(
            rows=[{"ticker": "600000.SS", "name": "浦发银行", "verdict": "BUY"}],
            mood="偏进攻",
        )

        self.assertIn("浦发银行（600000.SS）", strategy["bullets"][0])

    def test_explicit_zero_confidence_is_not_rendered_as_missing(self) -> None:
        report = {
            "market_recommendations": [{"ticker": "600000.SS", "name": "浦发银行",
                                        "confidence": 0, "verdict": "WATCH"}],
            "market_recommendations_meta": {"status": "ready"},
        }
        text = "\n".join(item["body"] for item in render_ai_daily_report_push_messages(report))
        self.assertIn("置信度：0", text)

    def test_observation_ready_report_is_useful_but_never_actionable(self) -> None:
        report = {
            "market_recommendations": [],
            "market_watch_recommendations": [{
                "ticker": "600000.SS", "name": "浦发银行", "tradability_status": "DEFER",
                "trade_readiness_score": 78.9, "percentile": 96.0, "trend_score": 82,
                "risk_flags": ["model-observation-only"], "readiness_reason": "等待模型资格确认",
            }],
            "market_recommendations_meta": {
                "status": "observation_ready", "note": "已扫描 5579 只证券。",
            },
        }

        messages = render_ai_daily_report_push_messages(report)
        text = "\n".join(f"{item['title']}\n{item['body']}" for item in messages)
        self.assertIn("A股研究观察日报", text)
        self.assertIn("浦发银行（600000.SS）", text)
        self.assertIn("不可执行", text)
        self.assertNotIn("A股可执行买入池", text)


if __name__ == "__main__":
    unittest.main()
