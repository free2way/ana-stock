from __future__ import annotations

from unittest import TestCase

from app.services.us_event_exemptions import (
    ALPACA_SUPPORTED_EVENT_TYPES,
    COVERAGE_VERSION,
    US_EVENT_EXEMPTIONS,
    coverage_payload,
)


class USEventExemptionTests(TestCase):
    def test_exemptions_are_disjoint_from_supported_types(self) -> None:
        payload = coverage_payload()
        self.assertTrue(payload["consistent"])
        self.assertEqual(
            sorted(set(ALPACA_SUPPORTED_EVENT_TYPES)),
            payload["supported_event_types"],
        )
        self.assertTrue(set(payload["supported_event_types"]).isdisjoint(payload["exempt_event_types"]))

    def test_every_exemption_carries_reason_mitigation_and_review_date(self) -> None:
        for exemption in US_EVENT_EXEMPTIONS:
            self.assertTrue(exemption.reason)
            self.assertTrue(exemption.mitigation)
            self.assertRegex(exemption.review_by, r"^\d{4}-\d{2}-\d{2}$")
            self.assertFalse(exemption.blocking)

    def test_payload_is_versioned_and_queryable(self) -> None:
        payload = coverage_payload()
        self.assertEqual(COVERAGE_VERSION, payload["coverage_version"])
        self.assertEqual("alpaca_corporate_actions", payload["provider"])
        self.assertIn("name_change", payload["exempt_event_types"])
        self.assertIn("merger", payload["supported_event_types"])
