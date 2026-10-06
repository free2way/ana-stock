"""Read-only CLI: fetch one day of SEC EDGAR daily filings into the event store.

Usage::

    PQW_SEC_USER_AGENT='Your Org admin@example.com' \\
        uv run python scripts/fetch_edgar_filings.py --date 2024-01-02 --forms 8-K,4,144

Writes ``data/artifacts/edgar_filings/<date>.json`` (+ ``.parquet``) and prints
the row count, the artifact reference and a small event sample.  Exits non-zero
(fail-closed) when EDGAR rejects the request or the artifact already exists with
different content.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.providers.edgar_filings import (  # noqa: E402
    DAILY_FILING_FORMS,
    EdgarDailyFilingsClient,
    EdgarFilingsError,
    EdgarFilingsStore,
    build_events,
)


def _default_date() -> str:
    # EDGAR updates a trading day's index the same evening ET; default to
    # yesterday so a same-day run is never surprised by an incomplete index.
    return (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=_default_date(), help="filed date (YYYY-MM-DD), default: yesterday UTC")
    parser.add_argument("--forms", default=",".join(DAILY_FILING_FORMS), help="comma-separated EDGAR forms")
    parser.add_argument("--out-root", default=None, help="override artifact directory")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing artifact with different content")
    parser.add_argument("--sample-events", type=int, default=5, help="how many events to print")
    args = parser.parse_args(argv)

    forms = [item.strip().upper() for item in str(args.forms).split(",") if item.strip()]
    settings = get_settings()
    client = EdgarDailyFilingsClient(settings=settings)
    store = EdgarFilingsStore(Path(args.out_root) if args.out_root else None)

    try:
        rows = client.fetch_daily_filings(date.fromisoformat(args.date), form_types=forms)
        query = client.build_query(date.fromisoformat(args.date), tuple(forms), 0)
        reference = store.write_daily(args.date, rows, query=query, overwrite=args.overwrite)
    except EdgarFilingsError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2

    events = build_events(rows)
    print(f"date            : {args.date}")
    print(f"forms           : {', '.join(forms)}")
    print(f"rows            : {len(rows)}")
    print(f"events          : {len(events)}")
    print(f"content_sha256  : {reference['content_sha256']}")
    print(f"idempotent      : {reference['idempotent']}")
    print(f"artifact        : {store.daily_path(args.date)}")
    if reference.get("parquet_relative_path"):
        print(f"parquet         : {store.daily_parquet_path(args.date)}")
    for event in events[: max(0, int(args.sample_events))]:
        record = event.to_record()
        print(
            "  - "
            f"{record['filed_date']} {record['event_type']:<5} "
            f"{record.get('symbol') or '<no-ticker>':<8} "
            f"avail={record['available_time']} {record['accession']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
