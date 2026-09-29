from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings
from app.services.stock_selection.promotion_gate import (
    assess_candidate_promotion,
    load_robustness_evidence,
    persist_promotion_gate_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assess immutable stock-selection robustness evidence for promotion."
    )
    parser.add_argument("--robustness-dir", type=Path, action="append", required=True)
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-comparison-passed", action="store_true")
    baseline.add_argument("--baseline-comparison-failed", action="store_true")
    parser.add_argument("--artifact-root", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    loaded = [load_robustness_evidence(path) for path in args.robustness_dir]
    baseline_result = (
        True
        if args.baseline_comparison_passed
        else False
        if args.baseline_comparison_failed
        else None
    )
    report = assess_candidate_promotion(
        [item[0] for item in loaded],
        source_evidence_versions=[item[1] for item in loaded],
        baseline_comparison_passed=baseline_result,
    )
    root = args.artifact_root or (
        get_settings().artifacts_dir / "stock_selection_research" / "promotion_gates"
    )
    evidence = persist_promotion_gate_report(report, root=root)
    print(
        json.dumps(
            {
                "status": "success",
                "report": asdict(report),
                "evidence_version": evidence.evidence_version,
                "evidence_artifact": str(evidence.artifact_dir),
                "reused_existing": evidence.reused_existing,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
