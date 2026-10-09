"""Endpoint-level regression tests for the readiness probe (review item E9).

``/health/ready`` must stay a public liveness/readiness summary: an unavailable
dependency returns 503 and the body carries statuses only -- never exception
text, artifact paths or directory names.  The full diagnostics live behind the
authenticated ``/health/ready/diagnostics`` endpoint (and in the log).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.main import (
    _public_readiness_summary,
    _unsatisfiable_readiness_checks,
    app,
)

DEEP_DIAGNOSTICS = {
    "database": {
        "status": "failed",
        "message": "could not connect to server: Connection refused host=db.internal port=5432",
    },
    "prediction_cold_storage": {
        "status": "ok",
        "mode": "hot_cold",
        "artifact_root": "/srv/pqw/data/artifacts/prediction_runs",
    },
    "lake_cn": {"status": "missing"},
    "lake_us": {"status": "stale"},
}


class ReadinessExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_dependency_failure_returns_503_without_internal_diagnostics(self) -> None:
        with patch(
            "app.core.db.SessionLocal",
            side_effect=RuntimeError(DEEP_DIAGNOSTICS["database"]["message"]),
        ), self.assertLogs("uvicorn.error", level="WARNING") as captured:
            response = self.client.get("/health/ready")

        self.assertEqual(503, response.status_code)
        body = response.json()
        self.assertEqual("failed", body["status"])
        self.assertEqual("failed", body["checks"]["database"]["status"])
        self.assertIn("database", body["failed"])

        # No exception text, no absolute path, no directory name anywhere.
        self.assertNotIn("Connection refused", response.text)
        self.assertNotIn("db.internal", response.text)
        self.assertNotIn("/srv/", response.text)
        self.assertNotIn("artifact_root", response.text)
        self.assertNotIn("message", response.text)
        for check in body["checks"].values():
            self.assertEqual({"status"}, set(check))

        # The detailed diagnostics are still recorded in the log.
        logged = "\n".join(captured.output)
        self.assertIn("Connection refused", logged)

    def test_diagnostics_endpoint_is_authenticated_and_returns_details(self) -> None:
        anonymous = self.client.get("/health/ready/diagnostics", follow_redirects=False)
        self.assertEqual(303, anonymous.status_code)
        self.assertIn("/login", anonymous.headers.get("location", ""))

        with patch("app.api.main.is_authenticated", return_value=True), patch(
            "app.api.main._collect_readiness_checks", return_value=dict(DEEP_DIAGNOSTICS)
        ):
            authenticated = self.client.get("/health/ready/diagnostics")

        self.assertEqual(200, authenticated.status_code)
        detail = authenticated.json()
        self.assertIn("Connection refused", detail["checks"]["database"]["message"])
        self.assertIn("artifact_root", detail["checks"]["prediction_cold_storage"])

    def test_lake_freshness_states_are_not_hard_dependency_failures(self) -> None:
        self.assertEqual([], _unsatisfiable_readiness_checks({"lake_cn": {"status": "missing"}}))
        self.assertEqual([], _unsatisfiable_readiness_checks({"lake_us": {"status": "stale"}}))
        self.assertEqual([], _unsatisfiable_readiness_checks({"database": {"status": "ok"}}))
        self.assertEqual(
            ["database"], _unsatisfiable_readiness_checks({"database": {"status": "failed"}})
        )

    def test_public_summary_keeps_statuses_and_drops_payloads(self) -> None:
        summary = _public_readiness_summary(
            {
                "database": {"status": "failed", "message": "secret host"},
                "prediction_cold_storage": {"status": "ok", "artifact_root": "/tmp/x"},
            },
            overall="failed",
            failed=["database"],
            stale=[],
            degraded=[],
        )
        self.assertEqual("failed", summary["status"])
        self.assertEqual({"database": {"status": "failed"},
                          "prediction_cold_storage": {"status": "ok"}}, summary["checks"])
        self.assertNotIn("secret host", str(summary))
        self.assertNotIn("/tmp/x", str(summary))

    def test_liveness_probe_semantics_are_unchanged(self) -> None:
        with patch("app.core.db.SessionLocal", side_effect=RuntimeError("db down")):
            response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())


if __name__ == "__main__":
    unittest.main()
