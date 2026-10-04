"""Tests for the shared structured opt-in audit builder."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.optin_audit import (  # noqa: E402
    DEFAULT_OPERATOR,
    MissingOptinReasonError,
    build_optin_audit,
)


class OptinAuditTests(TestCase):
    def test_disabled_optin_has_no_reason_requirement(self) -> None:
        record = build_optin_audit(enabled=False, source="flag")
        self.assertFalse(record["enabled"])
        self.assertIsNone(record["reason"])
        self.assertEqual(DEFAULT_OPERATOR, record["operator"])
        self.assertEqual("default", record["operator_source"])

    def test_enabled_optin_without_reason_refuses(self) -> None:
        with patch.dict("os.environ", {"PQW_OPTIN_REASON": ""}, clear=False):
            with self.assertRaises(MissingOptinReasonError):
                build_optin_audit(enabled=True, source="flag")

    def test_unexercised_optin_still_requires_a_reason(self) -> None:
        # Reachability is irrelevant: an *enabled* opt-in always requires a
        # reason, so callers cannot downgrade the check per run.
        with patch.dict("os.environ", {"PQW_OPTIN_REASON": ""}, clear=False):
            with self.assertRaises(MissingOptinReasonError):
                build_optin_audit(enabled=True, source="flag")

    def test_run_parameter_reason_and_operator_win(self) -> None:
        record = build_optin_audit(
            enabled=True,
            source="flag",
            reason="run reason",
            operator="alice",
            scope={"market": "CN"},
            decided_at=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(record["enabled"])
        self.assertEqual("run reason", record["reason"])
        self.assertEqual("alice", record["operator"])
        self.assertEqual("run_parameter", record["operator_source"])
        self.assertEqual({"market": "CN"}, record["scope"])
        self.assertEqual("2026-10-03T12:00:00+00:00", record["decided_at"])

    def test_environment_fallback_supplies_reason_and_operator(self) -> None:
        with patch.dict(
            "os.environ",
            {"PQW_OPTIN_REASON": "env reason", "PQW_OPTIN_OPERATOR": "env-operator"},
            clear=False,
        ):
            record = build_optin_audit(enabled=True, source="flag")
        self.assertEqual("env reason", record["reason"])
        self.assertEqual("env-operator", record["operator"])
        self.assertEqual("env:PQW_OPTIN_OPERATOR", record["operator_source"])

    def test_unknown_operator_is_recorded_explicitly(self) -> None:
        with patch.dict(
            "os.environ", {"PQW_OPTIN_REASON": "r", "PQW_OPTIN_OPERATOR": ""}, clear=False
        ):
            record = build_optin_audit(enabled=True, source="flag")
        self.assertEqual("unknown", record["operator"])
        self.assertEqual("default", record["operator_source"])
