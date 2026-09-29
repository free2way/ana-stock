#!/usr/bin/env python3
"""Verify DB-free P0 regressions and P1/P2 engineering governance contracts."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_regime_p0_contracts import TESTS as P0_TESTS, write_contract_receipt


P1_P2_TESTS = (
    "tests.test_execution_reconciliation",
    "tests.test_p1_factor_engineering",
    "tests.test_p1_p2_engineering",
)

CLEANUP_TESTS = (
    "tests.test_postgres_safety",
    "tests.test_us_market_scheduler",
    "tests.test_trainer_retired_baseline",
    "tests.test_cleanup_shared_helpers",
    "tests.test_portfolio_privacy",
    "tests.test_hithink_finance",
    "tests.test_community_data_sources",
    "tests.test_cleanup_contract_runner",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New immutable receipt path")
    parser.add_argument("--include-cleanup-contracts", action="store_true",
                        help="Also verify code cleanup, privacy and providers with socket connections blocked")
    args = parser.parse_args()
    try:
        with ExitStack() as guards:
            if args.include_cleanup_contracts:
                for target in ("socket.socket.connect", "socket.socket.connect_ex"):
                    guards.enter_context(patch(target, side_effect=AssertionError(
                        "Cleanup contracts must not open network connections")))
            return write_contract_receipt(
                output=args.output,
                tests=(*P0_TESTS, *P1_P2_TESTS, *(CLEANUP_TESTS if args.include_cleanup_contracts else ())),
                schema_version="regime_p1_p2_engineering_receipt_v1",
                scope=("p0_p1_p2_plus_cleanup_no_network_no_production_switch" if args.include_cleanup_contracts
                       else "p0_regression_plus_p1_a6_a7_a8_and_p2_a9_local_contracts_no_production_switch"),
            )
    except FileExistsError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
