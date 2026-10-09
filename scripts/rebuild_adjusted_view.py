"""Rebuild the adjusted price view from raw bars plus corporate actions (C1).

CN: raw TuShare bars + dividend-derived actions -> qfq/hfq series.
US: refuses to run unless explicitly forced; the legacy US lake mixes Polygon
    split-adjusted rows with Alpaca raw repairs, so rebuilding on top would
    double-adjust part of the series. Basis must be re-established first.

The build logic lives in ``app.services.adjusted_view_builder`` so the
scheduled U.S. close pipeline can reuse the exact same implementation.

Determinism: the report records per-symbol adjustment versions and a combined
digest; --verify-determinism builds twice and compares both digests.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.adjusted_view_builder import (  # noqa: E402
    build_market_view,
    write_view,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US"], required=True)
    parser.add_argument("--method", choices=["qfq", "hfq"], default="qfq")
    parser.add_argument("--limit-partitions", type=int, default=0)
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--output-root", default=str(ROOT / "data" / "lake" / "_adjusted_v2"))
    parser.add_argument("--raw-glob", default=None, help="single-basis raw namespace (required for US)")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--verify-determinism", action="store_true")
    parser.add_argument("--force-mixed-basis", action="store_true", help="US only: bypass the basis guard")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    if args.market == "US" and not (args.raw_glob or args.force_mixed_basis):
        print(json.dumps({
            "status": "blocked",
            "reason": (
                "US lake mixes Polygon adjusted rows with Alpaca raw repairs; rebuilding on top would "
                "double-adjust part of the series. Pass --raw-glob pointing at the single-basis Alpaca "
                "namespace (data/lake/_us_alpaca/raw/*.parquet) or re-establish one basis first."
            ),
        }, indent=2))
        return 2

    rows, report = build_market_view(
        args.market,
        method=args.method,
        limit_partitions=args.limit_partitions,
        limit_symbols=args.limit_symbols,
        raw_glob=args.raw_glob,
    )
    if args.verify_determinism:
        _, second = build_market_view(
            args.market,
            method=args.method,
            limit_partitions=args.limit_partitions,
            limit_symbols=args.limit_symbols,
            raw_glob=args.raw_glob,
        )
        report["determinism"] = {
            "raw_digest_match": report["raw_digest"] == second["raw_digest"],
            "version_digest_match": report["version_digest"] == second["version_digest"],
        }
        if not all(report["determinism"].values()):
            raise SystemExit("determinism check failed")
    if args.write:
        manifest = write_view(args.market, args.method, rows, report, output_root=Path(args.output_root))
        report["manifest"] = {key: manifest[key] for key in ("parquet", "parquet_sha256", "generated_at")}
    status = {"status": "ok", **report}
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(status, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
