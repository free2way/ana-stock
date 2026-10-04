"""Tests for the read-only quant risk monitor (scripts/monitor_quant_risk.py)."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import monitor_quant_risk as monitor  # noqa: E402


class _Probe:
    def __init__(self, state: str, coverage: float, missing: list[str] | None = None):
        self._payload = {
            "market": "CN",
            "state": state,
            "coverage_share": coverage,
            "view_sha256": "a" * 64,
            "missing_symbols": missing or [],
        }

    def as_dict(self) -> dict:
        return dict(self._payload)


def _action(action_type: str) -> SimpleNamespace:
    return SimpleNamespace(action_type=action_type)


class MonitorInsufficientDataTests(TestCase):
    def _empty_report(self, output_dir: Path) -> dict:
        def _boom(*_args, **_kwargs):
            raise RuntimeError("no source")

        return monitor.build_report(
            markets=("CN",),
            now=datetime(2026, 10, 3, tzinfo=timezone.utc),
            output_dir=output_dir,
            lake_latest_fn=lambda _market: None,
            probe_fn=_boom,
            symbols_fn=_boom,
            actions_fn=_boom,
            run_records={"status": "insufficient_data", "note": "db unavailable", "runs": []},
            evaluations={"status": "insufficient_data", "note": "no evaluations", "evaluations": []},
            log_paths=[str(output_dir / "does-not-exist.log")],
        )

    def test_missing_sources_report_insufficient_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._empty_report(Path(tmp))
        self.assertEqual("insufficient_data", report["data_staleness"]["status"])
        self.assertEqual("insufficient_data", report["coverage"]["status"])
        self.assertEqual("insufficient_data", report["fail_closed"]["status"])
        self.assertEqual("insufficient_data", report["fail_closed"]["from_runs"]["status"])
        self.assertEqual("insufficient_data", report["model_drift"]["status"])
        # No fabricated numbers: absent sources stay absent.
        self.assertIsNone(report["data_staleness"]["markets"]["CN"]["lake_latest_trade_date"])
        self.assertIsNone(report["coverage"]["markets"]["CN"]["adjusted_view"])

    def test_empty_report_still_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            rc = monitor.main(
                ["--output-dir", str(output_dir), "--markets", "CN", "--log", str(output_dir / "none.log")]
            )
            self.assertEqual(0, rc)
            payload = json.loads((output_dir / "quant-risk-monitor.json").read_text(encoding="utf-8"))
            markdown = (output_dir / "quant-risk-monitor.md").read_text(encoding="utf-8")
        self.assertEqual(monitor.SCHEMA_VERSION, payload["schema_version"])
        self.assertIn("# Quant Risk Monitor", markdown)

    def test_missing_source_report_renders_insufficient_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._empty_report(Path(tmp))
        markdown = monitor.render_markdown(report)
        self.assertIn("insufficient_data", markdown)
        # Every fail-closed category appears exactly once in the table.
        self.assertEqual(1, markdown.count("| missing_optin_reason |"))


class MonitorRealDataNumbersTests(TestCase):
    def test_staleness_reports_numeric_lag(self) -> None:
        with patch(
            "app.services.market_freshness.latest_completed_market_date",
            return_value="2026-10-02",
        ):
            result = monitor.collect_staleness(
                ("CN",), lake_latest_fn=lambda _market: "2026-09-30"
            )
        entry = result["markets"]["CN"]
        self.assertEqual(2, entry["lag_days"])
        self.assertEqual("stale", entry["status"])
        self.assertEqual("stale", result["status"])

    def test_coverage_and_corporate_action_counts(self) -> None:
        result = monitor.collect_coverage(
            ("CN",),
            probe_fn=lambda _market, _symbols: _Probe("present", 0.985, ["BBB"]),
            symbols_fn=lambda _market: {"AAA", "BBB"},
            actions_fn=lambda _market: [
                _action("split"),
                _action("cash_dividend"),
                _action("merger"),
                _action("spinoff"),
            ],
        )
        entry = result["markets"]["CN"]
        self.assertEqual("ok", result["status"])
        self.assertEqual("present", entry["adjusted_view"]["state"])
        self.assertEqual(0.985, entry["adjusted_view"]["coverage_share"])
        self.assertEqual(2, entry["symbol_count"])
        self.assertEqual(
            {"loaded": 4, "supported": 2, "unsupported": 2,
             "unsupported_by_type": {"merger": 1, "spinoff": 1}},
            entry["corporate_actions"],
        )

    def test_coverage_deltas_use_previous_snapshot(self) -> None:
        previous = {
            "coverage": {
                "markets": {
                    "CN": {
                        "adjusted_view": {"coverage_share": 0.90},
                        "corporate_actions": {"unsupported": 5},
                    }
                }
            }
        }
        result = monitor.collect_coverage(
            ("CN",),
            probe_fn=lambda _market, _symbols: _Probe("present", 0.95),
            symbols_fn=lambda _market: {"AAA"},
            actions_fn=lambda _market: [_action("merger")],
            previous=previous,
        )
        delta = result["deltas_vs_previous"]["markets"]["CN"]
        self.assertAlmostEqual(0.05, delta["adjusted_coverage_share"])
        self.assertEqual(-4, delta["unsupported_corporate_actions"])

    def test_run_fail_closed_counts_and_optin_audit(self) -> None:
        runs = [
            {
                "source": "strategy_run",
                "id": 1,
                "name": "s1",
                "status": "failed",
                "config": {
                    "price_basis_contract": {
                        "decision": "reject",
                        "reasons": ["incomplete_adjusted_coverage"],
                    }
                },
                "summary": {},
            },
            {
                "source": "strategy_run",
                "id": 2,
                "name": "s2",
                "status": "failed",
                "config": {"unmodeled_corporate_actions": [{"symbol": "AAA"}]},
                "summary": {"unmodeled_opt_in": False, "unmodeled_corporate_actions": [{"symbol": "AAA"}]},
            },
            {
                "source": "model_run",
                "id": 3,
                "name": "m3",
                "status": "success",
                "config": {
                    "raw_fallback_allowed": True,
                    "raw_fallback_optin_audit": {"enabled": True, "operator": "alice", "reason": "why"},
                },
                "summary": {},
            },
        ]
        result = monitor.summarize_run_fail_closed(runs)
        self.assertEqual(1, result["counts"]["price_basis_reject"])
        self.assertEqual(1, result["counts"]["coverage_block"])
        self.assertEqual(1, result["counts"]["unmodeled_block"])
        self.assertEqual(1, result["optin_used"]["raw_fallback_allowed"])
        self.assertEqual(1, result["runs_with_optin_audit"])
        self.assertEqual(3, result["runs_scanned"])

    def test_scan_logs_counts_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "app.log"
            log.write_text(
                "[train] price-basis contract refused the run: incomplete_adjusted_coverage\n"
                "Trainer refused an adjusted-view label run with incomplete coverage\n"
                "Event-driven backtest refused to run: 1 event(s)\n"
                "opt-in `x` was enabled without a reason; refusing fail-closed.\n",
                encoding="utf-8",
            )
            result = monitor.scan_logs([log])
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, result["counts"]["price_basis_reject"])
        self.assertEqual(1, result["counts"]["coverage_block"])
        self.assertEqual(1, result["counts"]["unmodeled_block"])
        self.assertEqual(1, result["counts"]["missing_optin_reason"])

    def test_model_drift_compares_two_same_named_runs(self) -> None:
        evaluations = [
            {
                "name": "cn_close",
                "run_id": 11,
                "evaluation_id": 1,
                "metrics": [
                    {"horizon_days": 5, "metric_scope": "overall", "hit_rate": 0.55,
                     "avg_return": 0.020, "sample_count": 100}
                ],
            },
            {
                "name": "cn_close",
                "run_id": 12,
                "evaluation_id": 2,
                "metrics": [
                    {"horizon_days": 5, "metric_scope": "overall", "hit_rate": 0.45,
                     "avg_return": 0.005, "sample_count": 120}
                ],
            },
        ]
        drift = monitor.assess_model_drift(evaluations)
        self.assertEqual("ok", drift["status"])
        self.assertEqual("cn_close", drift["model_name"])
        self.assertTrue(drift["drift"])
        metric = drift["metrics"]["5"]
        self.assertAlmostEqual(-0.10, metric["hit_rate_delta"], places=6)
        self.assertAlmostEqual(-0.015, metric["avg_return_delta"], places=6)
        self.assertEqual(120, metric["sample_count_latest"])

    def test_model_drift_insufficient_with_single_run(self) -> None:
        evaluations = [
            {
                "name": "cn_close",
                "run_id": 12,
                "evaluation_id": 2,
                "metrics": [
                    {"horizon_days": 5, "metric_scope": "overall", "hit_rate": 0.45,
                     "avg_return": 0.005, "sample_count": 120}
                ],
            }
        ]
        drift = monitor.assess_model_drift(evaluations)
        self.assertEqual("insufficient_data", drift["status"])
        self.assertEqual(0, drift["models_compared"])

    def test_build_report_with_real_inputs_produces_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            log = output_dir / "run.log"
            log.write_text("Event-driven backtest refused to run: 1 event(s)\n", encoding="utf-8")
            with patch(
                "app.services.market_freshness.latest_completed_market_date",
                return_value="2026-10-02",
            ):
                report = monitor.build_report(
                    markets=("CN",),
                    output_dir=output_dir,
                    lake_latest_fn=lambda _market: "2026-10-01",
                    probe_fn=lambda _market, _symbols: _Probe("present", 1.0),
                    symbols_fn=lambda _market: {"AAA"},
                    actions_fn=lambda _market: [_action("split"), _action("merger")],
                    run_records={
                        "status": "ok",
                        "runs": [
                            {
                                "source": "strategy_run",
                                "id": 1,
                                "name": "s1",
                                "status": "failed",
                                "config": {},
                                "summary": {
                                    "unmodeled_corporate_actions": [{"symbol": "AAA"}],
                                    "unmodeled_opt_in": False,
                                },
                            }
                        ],
                    },
                    evaluations={
                        "status": "ok",
                        "evaluations": [
                            {"name": "m", "run_id": 1, "evaluation_id": 1,
                             "metrics": [{"horizon_days": 5, "metric_scope": "overall",
                                          "hit_rate": 0.5, "avg_return": 0.01, "sample_count": 10}]},
                            {"name": "m", "run_id": 2, "evaluation_id": 2,
                             "metrics": [{"horizon_days": 5, "metric_scope": "overall",
                                          "hit_rate": 0.52, "avg_return": 0.02, "sample_count": 12}]},
                        ],
                    },
                    log_paths=[log],
                )
        self.assertEqual("stale", report["data_staleness"]["status"])
        self.assertEqual(1, report["data_staleness"]["markets"]["CN"]["lag_days"])
        self.assertEqual("ok", report["coverage"]["status"])
        self.assertEqual(1, report["coverage"]["markets"]["CN"]["corporate_actions"]["unsupported"])
        self.assertEqual(
            2, report["fail_closed"]["combined"]["counts"]["unmodeled_block"]
        )
        self.assertEqual("ok", report["model_drift"]["status"])
        self.assertEqual(2, report["model_drift"]["latest_run_id"])
