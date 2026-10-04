"""US corporate-action coverage and exemptions (machine-readable, versioned).

Alpaca's Corporate Actions API covers ``dividend / merger / split / spinoff``
(validated 2026-10-03: any other ``ca_types`` value returns HTTP 422). Event
types outside that set are listed here explicitly - with rationale, mitigation
and a review date - so acceptance records can cite an exemption instead of
leaving a silent gap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

COVERAGE_VERSION = "us_event_coverage_2026_10_03"

ALPACA_SUPPORTED_EVENT_TYPES = ("dividend", "merger", "split", "spinoff")


@dataclass(frozen=True, slots=True)
class EventExemption:
    event_type: str
    reason: str
    mitigation: str
    review_by: str
    blocking: bool = False


US_EVENT_EXEMPTIONS: tuple[EventExemption, ...] = (
    EventExemption(
        event_type="name_change",
        reason="Alpaca Corporate Actions API rejects any ca_type outside dividend/merger/split/spinoff (HTTP 422).",
        mitigation=(
            "Ticker renames do not move prices by themselves; renamed symbols surface as a delisting + new "
            "listing pair in the lake and are covered by the sampling audit in verify_us_acceptance.py."
        ),
        review_by="2027-01-31",
    ),
    EventExemption(
        event_type="rights_offering",
        reason="Not part of the Alpaca corporate-action type set; no free US source is wired into the lake.",
        mitigation=(
            "US rights offerings are rare; unexplained >50% jumps that coincide with a rights announcement "
            "are expected in the sampling pool and reviewed by the quarterly spot check."
        ),
        review_by="2027-01-31",
    ),
    EventExemption(
        event_type="unit_split",
        reason="Warrant/unit splits are not part of the Alpaca corporate-action type set.",
        mitigation="Price impact is bounded; covered by the quarterly spot check.",
        review_by="2027-01-31",
    ),
)


def coverage_payload() -> dict:
    """Queryable coverage + exemption record for acceptance evidence."""

    supported = set(ALPACA_SUPPORTED_EVENT_TYPES)
    exempt = {item.event_type for item in US_EVENT_EXEMPTIONS}
    return {
        "coverage_version": COVERAGE_VERSION,
        "provider": "alpaca_corporate_actions",
        "supported_event_types": sorted(supported),
        "exempt_event_types": sorted(exempt),
        "exemptions": [asdict(item) for item in US_EVENT_EXEMPTIONS],
        "consistent": not (supported & exempt),
    }
