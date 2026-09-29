#!/usr/bin/env python3
"""Run the DB-free P0 regression baseline plus P1 engineering contracts."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_regime_p0_contracts import TESTS as P0_TESTS, write_contract_receipt


P1_TESTS = (
    "tests.test_p1_factor_engineering",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New immutable receipt path")
    args = parser.parse_args()
    try:
        return write_contract_receipt(
            output=args.output,
            tests=(*P0_TESTS, *P1_TESTS),
            schema_version="regime_p1_engineering_receipt_v1",
            scope="p0_regression_plus_p1_a6_local_contracts_no_production_switch",
        )
    except FileExistsError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
