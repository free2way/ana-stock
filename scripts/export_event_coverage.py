"""Export the corporate-action coverage matrix (B-7/E-6 companion evidence).

Combines the live action stores with the declared exemption list so every event
type is either ingested (with counts) or explicitly exempted.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.corporate_actions import load_actions  # noqa: E402
from app.services.us_event_exemptions import coverage_payload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    markets: dict[str, dict] = {}
    for market in ("CN", "US"):
        actions = load_actions(market)
        counts = Counter(action.action_type for action in actions)
        dates = [action.effective_date for action in actions]
        markets[market] = {
            "actions": len(actions),
            "by_type": dict(sorted(counts.items())),
            "first_effective_date": min(dates).isoformat() if dates else None,
            "last_effective_date": max(dates).isoformat() if dates else None,
        }
    payload = {
        "schema_version": "corporate_action_coverage_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "markets": markets,
        "us_provider_coverage": coverage_payload(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({
        "CN": markets["CN"]["by_type"],
        "US": markets["US"]["by_type"],
        "us_supported": payload["us_provider_coverage"]["supported_event_types"],
        "us_exempt": payload["us_provider_coverage"]["exempt_event_types"],
        "consistent": payload["us_provider_coverage"]["consistent"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
