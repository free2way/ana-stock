from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings  # noqa: E402
from app.services.http_page_benchmark import (  # noqa: E402
    PageBenchmarkTarget,
    benchmark_authenticated_pages,
)


def _write_receipt(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only authenticated latency benchmark for production pages."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--p95-limit-ms", type=float, default=1000.0)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Optional repeatable target; defaults to dashboard_home and sync_center.",
    )
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    targets = []
    for raw_target in args.target:
        if "=" not in raw_target:
            raise ValueError("Page benchmark targets must use NAME=PATH.")
        name, path = raw_target.split("=", 1)
        if not name.strip() or not path.strip().startswith("/"):
            raise ValueError("Page benchmark targets require a name and absolute path.")
        targets.append(
            PageBenchmarkTarget(
                name=name.strip(),
                path=path.strip(),
                p95_limit_ms=args.p95_limit_ms,
            )
        )
    if not targets:
        targets = [
            PageBenchmarkTarget(
                name="dashboard_home",
                path="/dashboard?lang=zh&lookback_runs=3",
                p95_limit_ms=args.p95_limit_ms,
            ),
            PageBenchmarkTarget(
                name="sync_center",
                path="/dashboard/ops/sync?lang=zh&lookback_runs=3",
                p95_limit_ms=args.p95_limit_ms,
            ),
        ]
    result = benchmark_authenticated_pages(
        base_url=args.base_url,
        username=settings.auth_username,
        password=settings.auth_password,
        targets=targets,
        warmup_requests=args.warmup,
        measured_requests=args.requests,
        timeout_seconds=args.timeout,
    )
    _write_receipt(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
