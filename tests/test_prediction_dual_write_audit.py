from __future__ import annotations

import unittest

from app.services.prediction_dual_write_audit import (
    audit_publication_timing,
    compare_bounded_explanation_rows,
    compare_compact_prediction_rows,
    summarize_production_sequence,
    dual_write_runtime_evidence_passed,
)


class PredictionDualWriteAuditTests(unittest.TestCase):
    @staticmethod
    def _rows() -> list[dict]:
        return [
            {
                "symbol_id": rank,
                "trade_date": trade_date,
                "score": 1.0 / rank,
                "rank_value": float(rank),
            }
            for trade_date in ("2026-08-18", "2026-08-19", "2026-08-20")
            for rank in (1, 2, 3)
        ]

    def test_compact_parity_accepts_recent_full_dates_and_historical_top_k(self) -> None:
        cold = self._rows()
        hot = [
            row
            for row in cold
            if row["trade_date"] in {"2026-08-19", "2026-08-20"} or row["rank_value"] <= 2
        ]

        result = compare_compact_prediction_rows(cold, hot, full_trade_days=2, top_k=2)

        self.assertEqual("pass", result["status"])
        self.assertEqual(8, result["actual_hot_rows"])
        self.assertTrue(result["top_k_rows"]["exact_match"])

    def test_compact_parity_rejects_missing_top_k_row(self) -> None:
        cold = self._rows()
        hot = [
            row
            for row in cold
            if row["trade_date"] in {"2026-08-19", "2026-08-20"} or row["rank_value"] <= 2
        ][1:]

        result = compare_compact_prediction_rows(cold, hot, full_trade_days=2, top_k=2)

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["top_k_rows"]["exact_match"])

    def test_bounded_explanation_parity_includes_top_boundary_and_holdings(self) -> None:
        predictions = [
            {
                "symbol_id": rank,
                "trade_date": "2026-08-21",
                "score": 1.0 / rank,
                "rank_value": float(rank),
            }
            for rank in range(1, 11)
        ]
        cold = [
            {
                "symbol_id": rank,
                "trade_date": "2026-08-21",
                "feature_name": "momentum",
                "feature_value": float(rank),
                "contribution": 0.1,
                "direction": "positive",
                "display_order": 1,
            }
            for rank in range(1, 11)
        ]
        hot = [row for row in cold if int(row["symbol_id"]) in {1, 2, 3, 4, 5, 10}]

        result = compare_bounded_explanation_rows(
            predictions,
            cold,
            hot,
            top_k=3,
            boundary_radius=2,
            holding_symbol_ids={10},
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual(10, result["cold_full_rows"])
        self.assertEqual(6, result["expected_hot_rows"])
        self.assertTrue(result["selected_rows"]["exact_match"])

    def test_bounded_explanation_parity_rejects_missing_boundary_row(self) -> None:
        predictions = [
            {
                "symbol_id": rank,
                "trade_date": "2026-08-21",
                "score": 1.0 / rank,
                "rank_value": float(rank),
            }
            for rank in range(1, 7)
        ]
        cold = [
            {
                "symbol_id": rank,
                "trade_date": "2026-08-21",
                "feature_name": "momentum",
                "feature_value": float(rank),
                "contribution": 0.1,
                "direction": "positive",
                "display_order": 1,
            }
            for rank in range(1, 7)
        ]

        result = compare_bounded_explanation_rows(
            predictions,
            cold,
            cold[:4],
            top_k=3,
            boundary_radius=2,
        )

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["selected_rows"]["exact_match"])

    def test_duplicate_trade_date_only_counts_once(self) -> None:
        audited = [
            {"model_run_id": 10, "latest_trade_date": "2026-08-21", "status": "pass"},
            {"model_run_id": 9, "latest_trade_date": "2026-08-21", "status": "pass"},
            {"model_run_id": 8, "latest_trade_date": "2026-08-20", "status": "pass"},
            {"model_run_id": 7, "latest_trade_date": "2026-08-19", "status": "pass"},
            {"model_run_id": 6, "latest_trade_date": "2026-08-18", "status": "pass"},
            {"model_run_id": 5, "latest_trade_date": "2026-08-17", "status": "pass"},
        ]

        result = summarize_production_sequence(audited, required_runs=5)

        self.assertEqual("pass", result["status"])
        self.assertEqual([9], result["duplicate_trade_date_runs"])
        self.assertEqual([10, 8, 7, 6, 5], result["sequence_runs"])

    def test_gap_in_trading_dates_cannot_pass(self) -> None:
        audited = [
            {"model_run_id": 5, "latest_trade_date": "2026-08-21", "status": "pass"},
            {"model_run_id": 4, "latest_trade_date": "2026-08-20", "status": "pass"},
            {"model_run_id": 3, "latest_trade_date": "2026-08-18", "status": "pass"},
            {"model_run_id": 2, "latest_trade_date": "2026-08-17", "status": "pass"},
            {"model_run_id": 1, "latest_trade_date": "2026-08-14", "status": "pass"},
        ]

        result = summarize_production_sequence(audited, required_runs=5)

        self.assertEqual("pending", result["status"])
        self.assertFalse(result["consecutive_trade_dates"])

    @staticmethod
    def _timing(**updates) -> dict:
        timing = {
            "timing_version": "prediction-publication-timing-v2",
            "artifact_publish_ms": 100.0,
            "legacy_predictions_ms": 100.0,
            "legacy_details_ms": 100.0,
            "legacy_explanations_ms": 100.0,
            "physical_hot_ms": 100.0,
            "live_predictions_ms": 100.0,
            "postgresql_commit_ms": 10.0,
            "postgresql_write_phase_ms": 500.0,
            "publication_total_ms": 700.0,
            "process_peak_rss_bytes": 100_000_000,
            "python_tracemalloc_peak_bytes": 1_000_000,
            "python_publication_incremental_peak_bytes": 500_000,
            "postgresql_atomic_publish": True,
            "postgresql_transaction_version": "prediction-publication-transaction-v1",
        }
        timing.update(updates)
        return {"prediction_publication_timing": timing}

    def test_publication_timing_requires_v2_latency_and_memory_contract(self) -> None:
        missing = audit_publication_timing(None)
        passed = audit_publication_timing(self._timing())

        self.assertEqual("pending", missing["status"])
        self.assertEqual("pass", passed["status"])
        self.assertTrue(
            passed["checks"]["postgresql_write_phase_at_most_30_seconds"]
        )

    def test_publication_timing_rejects_over_30_second_postgresql_write(self) -> None:
        result = audit_publication_timing(
            self._timing(postgresql_write_phase_ms=30_000.001)
        )

        self.assertEqual("fail", result["status"])
        self.assertFalse(
            result["checks"]["postgresql_write_phase_at_most_30_seconds"]
        )

    def test_sequence_does_not_count_run_missing_runtime_evidence(self) -> None:
        audited = [
            {"model_run_id": 2, "latest_trade_date": "2026-08-21", "status": "pending"},
            {"model_run_id": 1, "latest_trade_date": "2026-08-20", "status": "pass"},
        ]

        result = summarize_production_sequence(audited, required_runs=2)

        self.assertEqual("pending", result["status"])
        self.assertEqual([1], result["passed_runs"])
        self.assertEqual([2], result["pending_runs"])

    def test_counted_runs_require_v2_runtime_evidence(self) -> None:
        base = {
            "audit_version": "prediction-dual-write-v2",
            "passed_runs": [2, 1],
            "runs": [
                {"model_run_id": 2, "publication_runtime": {"status": "pass"}},
                {"model_run_id": 1, "publication_runtime": {"status": "pass"}},
            ],
        }

        self.assertTrue(dual_write_runtime_evidence_passed(base))
        self.assertFalse(
            dual_write_runtime_evidence_passed(
                {**base, "audit_version": "prediction-dual-write-v1"}
            )
        )
        self.assertFalse(
            dual_write_runtime_evidence_passed(
                {
                    **base,
                    "runs": [
                        {
                            "model_run_id": 2,
                            "publication_runtime": {"status": "pending"},
                        },
                        base["runs"][1],
                    ],
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
