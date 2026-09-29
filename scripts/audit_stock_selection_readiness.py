from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.production_research import audit_market_research_readiness


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit whether a market lake is ready for formal full-market stock-selection research."
    )
    parser.add_argument("--market", required=True, choices=("CN", "US"))
    parser.add_argument("--required-history-sessions", type=int, default=252)
    parser.add_argument("--artifact-root", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = audit_market_research_readiness(
        market=args.market,
        artifact_root=args.artifact_root,
        required_history_sessions=args.required_history_sessions,
    )
    print(
        json.dumps(
            {
                "status": "ready" if result.report.passed else "blocked",
                "source_version": result.source_version,
                "report": asdict(result.report),
                "evidence_version": result.evidence.evidence_version,
                "evidence_artifact": str(result.evidence.artifact_dir),
                "reused_existing": result.evidence.reused_existing,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    if not result.report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
