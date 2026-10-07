"""Read-only CLI: fetch one day of SEC EDGAR daily filings into the event store.

Usage::

    PQW_SEC_USER_AGENT='Your Org admin@example.com' \\
        uv run python scripts/fetch_edgar_filings.py --date 2024-01-02 --forms 8-K,4,144

Operational timing: EDGAR settles a filing day's daily index at roughly 22:00
ET, so schedule this fetch **after 23:00 America/New_York**.  The default
``--date`` follows the same rule and never points at a day whose index is still
being written: it resolves to the latest ET day whose daily index is final.

Writes ``data/artifacts/edgar_filings/<date>.json`` (+ ``.parquet``) and prints
the row count, the artifact reference and a small event sample.  Exits non-zero
(fail-closed) when EDGAR rejects the request or the artifact already exists with
different content.

Completeness check: after the fetch, the day's row count is cross-checked
against the official EDGAR daily index (``form.YYYYMMDD.idx``).  The verdict and
both counts are printed and written to ``<date>.reconciliation.json``.  A
mismatch -- or an index that could not be read -- is an **advisory warning
only**: it is logged and stored but never changes the exit code.

Availability semantics: a filing's ``available_time`` is the end of its filed
day in America/New_York; consumers additionally gate on ``knowledge_time``
(``max(available_time, ingested_time)``) so a backfill is never visible before
it was ingested.  See ``docs/data-contract-knowledge-time-zh.md``.
"""
from __future__ import annotations

import argparse
from datetime import date
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
    EdgarFilingsReconciliation,
    build_edgar_reconciliation,
    build_events,
    edgar_default_filed_date,
    edgar_index_is_final,
)

_DATE_HELP = (
    "filed date (YYYY-MM-DD); default: latest ET day whose EDGAR daily index is "
    "final (schedule the run after 23:00 America/New_York)"
)


def _reconcile(
    client: EdgarDailyFilingsClient,
    filed_date: date,
    rows: list[dict],
    forms: list[str],
) -> EdgarFilingsReconciliation:
    """Cross-check the fetched rows against the official daily index.

    An unreadable index downgrades to ``index_unavailable``; it must never turn a
    successful fetch into a failure.
    """

    try:
        index = client.fetch_daily_index(filed_date)
    except EdgarFilingsError as exc:
        return build_edgar_reconciliation(
            filed_date,
            rows,
            forms=forms,
            index_unavailable_reason=f"could not read the EDGAR daily index: {exc}",
        )
    return build_edgar_reconciliation(filed_date, rows, forms=forms, index=index)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help=_DATE_HELP)
    parser.add_argument("--forms", default=",".join(DAILY_FILING_FORMS), help="comma-separated EDGAR forms")
    parser.add_argument("--out-root", default=None, help="override artifact directory")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing artifact with different content")
    parser.add_argument("--sample-events", type=int, default=5, help="how many events to print")
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help="skip the advisory EDGAR daily-index completeness check",
    )
    args = parser.parse_args(argv)

    forms = [item.strip().upper() for item in str(args.forms).split(",") if item.strip()]
    target_date = str(args.date).strip() if args.date else edgar_default_filed_date().isoformat()
    filed_date = date.fromisoformat(target_date)
    settings = get_settings()
    client = EdgarDailyFilingsClient(settings=settings)
    store = EdgarFilingsStore(Path(args.out_root) if args.out_root else None)

    if not edgar_index_is_final(filed_date):
        print(
            f"ADVISORY: {target_date} is not final yet (EDGAR settles the daily "
            "index at ~22:00 ET); row counts may be incomplete.",
            file=sys.stderr,
        )

    try:
        rows = client.fetch_daily_filings(filed_date, form_types=forms)
        query = client.build_query(filed_date, tuple(forms), 0)
        reference = store.write_daily(target_date, rows, query=query, overwrite=args.overwrite)
    except EdgarFilingsError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2

    reconciliation = None
    if not args.no_reconcile:
        reconciliation = _reconcile(client, filed_date, rows, forms)
        store.write_reconciliation(reconciliation)

    events = build_events(rows)
    print(f"date            : {target_date}")
    print(f"forms           : {', '.join(forms)}")
    print(f"rows            : {len(rows)}")
    print(f"events          : {len(events)}")
    print(f"content_sha256  : {reference['content_sha256']}")
    print(f"idempotent      : {reference['idempotent']}")
    print(f"artifact        : {store.daily_path(target_date)}")
    if reference.get("parquet_relative_path"):
        print(f"parquet         : {store.daily_parquet_path(target_date)}")

    if reconciliation is not None:
        expected = "n/a" if reconciliation.expected_total is None else reconciliation.expected_total
        print(
            f"reconcile       : {reconciliation.status.upper()} "
            f"(index={expected} fetched={reconciliation.observed_total})"
        )
        print(f"reconcile file  : {store.reconciliation_path(target_date)}")
        if reconciliation.status == "mismatch":
            for form in reconciliation.forms:
                print(
                    f"  form {form:<5}: index={reconciliation.expected_by_form.get(form, 'n/a')} "
                    f"fetched={reconciliation.observed_by_form.get(form, 0)} "
                    f"delta={reconciliation.delta_by_form.get(form, 0):+d}"
                )
        if not reconciliation.matched:
            print(f"WARN(advisory): {reconciliation.advisory()}", file=sys.stderr)

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
