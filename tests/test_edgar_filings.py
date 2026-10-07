"""Recorded-fixture tests for the read-only SEC EDGAR daily-filings source.

No live network is used: every response comes from ``tests/fixtures/edgar_filings``
captured from the real EDGAR full-text search endpoint.  A single opt-in live
smoke test (``ANA_EDGAR_LIVE_SMOKE=1``) exercises the real endpoint.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from app.services.providers.edgar_filings import (
    DAILY_FILING_FORMS,
    EDGAR_PROVIDER,
    EDGAR_RECONCILIATION_SCHEMA_VERSION,
    EdgarDailyFilingsClient,
    EdgarDailyIndexSnapshot,
    EdgarFilingsConflictError,
    EdgarFilingsConfigError,
    EdgarFilingsError,
    EdgarFilingsFetchError,
    EdgarFilingsParseError,
    EdgarFilingsStore,
    build_edgar_reconciliation,
    build_events,
    edgar_default_filed_date,
    edgar_index_is_final,
    event_type_for_form,
    normalize_form_types,
    parse_edgar_daily_index,
)
from scripts import fetch_edgar_filings as cli

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "edgar_filings"

UA = "AnaQuant Test test@example.com"
EFTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D401 - mimic http.client.HTTPMessage
        return super().get(key, default)


class _FakeResponse:
    def __init__(self, body: bytes, *, encoding: str = "") -> None:
        self._body = body
        self.headers = _FakeHeaders({"Content-Encoding": encoding})

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _RecordingOpener:
    """Serves queued payloads and records every request URL, in order."""

    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):  # noqa: ARG002
        self.urls.append(request.full_url)
        if not self.payloads:
            raise AssertionError("opener received more requests than queued responses")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return _FakeResponse(payload)
        return _FakeResponse(json.dumps(payload).encode("utf-8"))


def make_client(opener, **overrides) -> EdgarDailyFilingsClient:
    kwargs = {
        "user_agent": UA,
        "efts_endpoint": EFTS,
        "archive_endpoint": ARCHIVE,
        "min_request_interval_seconds": 0.0,
        "timeout_seconds": 5.0,
        # Tests that stub failures expect the very next request; retries are
        # exercised explicitly in RetryTests.
        "max_retries": 0,
        "retry_backoff_seconds": 0.0,
        "ticker_map": {},
        "opener": opener,
    }
    kwargs.update(overrides)
    return EdgarDailyFilingsClient(**kwargs)


def load_8k_rows() -> list[dict]:
    """The four recorded 8-K filings for 2024-01-02 (shared by the check tests)."""

    opener = _RecordingOpener([load_fixture("efts_8k_2024-01-02.json")])
    return make_client(opener).fetch_daily_filings("2024-01-02", form_types=["8-K"])


def load_daily_index(name: str) -> EdgarDailyIndexSnapshot:
    """Parse a recorded ``form.YYYYMMDD.idx`` fixture for 2024-01-02."""

    text = (FIXTURES / name).read_text(encoding="utf-8")
    return parse_edgar_daily_index(
        text,
        filed_date="2024-01-02",
        url="https://www.sec.gov/Archives/edgar/daily-index/2024/QTR1/form.20240102.idx",
    )


class EndpointAndParamsTests(TestCase):
    def test_efts_endpoint_receives_date_form_and_pagination_params(self) -> None:
        opener = _RecordingOpener([load_fixture("efts_8k_2024-01-02.json")])
        client = make_client(opener)
        rows = client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

        self.assertEqual(1, len(opener.urls))
        parsed = urlparse(opener.urls[0])
        self.assertEqual("efts.sec.gov", parsed.netloc)
        self.assertEqual("/LATEST/search-index", parsed.path)
        params = parse_qs(parsed.query)
        self.assertEqual(["custom"], params["dateRange"])
        self.assertEqual(["2024-01-02"], params["startdt"])
        self.assertEqual(["2024-01-02"], params["enddt"])
        self.assertEqual(["8-K"], params["forms"])
        self.assertEqual(["0"], params["from"])
        self.assertEqual(['""'], params["q"])
        self.assertEqual(4, len(rows))

    def test_forms_filter_is_sent_and_normalized(self) -> None:
        opener = _RecordingOpener([load_fixture("efts_form4_2024-01-03.json")])
        client = make_client(opener)
        client.fetch_daily_filings("2024-01-03", form_types=["4"])
        params = parse_qs(urlparse(opener.urls[0]).query)
        self.assertEqual(["4"], params["forms"])

    def test_unsupported_form_type_fails_closed(self) -> None:
        with self.assertRaises(EdgarFilingsError):
            normalize_form_types(["S-1"])
        self.assertEqual(DAILY_FILING_FORMS, normalize_form_types(None))


class ProvenanceAndFieldTests(TestCase):
    def _rows_8k(self) -> list[dict]:
        opener = _RecordingOpener([load_fixture("efts_8k_2024-01-02.json")])
        return make_client(opener).fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_required_fields_and_provenance(self) -> None:
        rows = self._rows_8k()
        for row in rows:
            self.assertEqual(EDGAR_PROVIDER, row["provider"])
            self.assertTrue(row["accession"])
            self.assertTrue(row["form_type"])
            self.assertEqual("2024-01-02", row["filed_date"])
            self.assertTrue(row["source_reference"].startswith(f"{EDGAR_PROVIDER}:efts:"))
            self.assertIn("dateRange=custom", row["source_reference"])
            self.assertIn("forms=8-K", row["source_reference"])
            self.assertIn(f"#{row['accession']}", row["source_reference"])
            # fetched_at must be timezone-aware provenance, never naive.
            self.assertIn("+00:00", row["fetched_at"])

    def test_company_cik_and_archive_url(self) -> None:
        row = next(r for r in self._rows_8k() if r["accession"] == "0000950170-24-000033")
        self.assertEqual("LBPH", row["symbol"])
        self.assertEqual("0001832168", row["cik"])
        self.assertEqual("Longboard Pharmaceuticals, Inc.", row["company_name"])
        self.assertEqual("2024-01-02", row["period"])
        self.assertEqual(
            "https://www.sec.gov/Archives/edgar/data/1832168/000095017024000033/lbph-20240102.htm",
            row["url"],
        )
        self.assertEqual({"name": "Longboard Pharmaceuticals, Inc.", "ticker": "LBPH", "cik": "0001832168"}, row["company"])

    def test_issuer_without_published_ticker_keeps_row(self) -> None:
        rows = self._rows_8k()
        somalogic = next(r for r in rows if r["accession"] == "0001104659-24-000390")
        self.assertIsNone(somalogic["symbol"])
        self.assertEqual("0001837412", somalogic["cik"])
        self.assertEqual("SomaLogic, Inc.", somalogic["company_name"])

    def test_form4_issuer_ticker_resolution(self) -> None:
        opener = _RecordingOpener([load_fixture("efts_form4_2024-01-03.json")])
        rows = make_client(opener).fetch_daily_filings("2024-01-03", form_types=["4"])
        bigbear = next(r for r in rows if r["accession"] == "0001628280-24-000262")
        self.assertEqual("BBAI", bigbear["symbol"])
        self.assertEqual("0001836981", bigbear["cik"])
        toast = next(r for r in rows if r["accession"] == "0001650164-24-000012")
        self.assertEqual("TOST", toast["symbol"])
        self.assertEqual("0001650164", toast["cik"])
        # Form 4 archive path uses the first CIK in the hit (owner mirror dir).
        self.assertEqual(
            "https://www.sec.gov/Archives/edgar/data/1932370/000162828024000262/wk-form4_1704320134.xml",
            bigbear["url"],
        )

    def test_form144_rows_have_null_period(self) -> None:
        opener = _RecordingOpener([load_fixture("efts_144_2024-01-03.json")])
        rows = make_client(opener).fetch_daily_filings("2024-01-03", form_types=["144"])
        teladoc = next(r for r in rows if r["accession"] == "0001959173-24-000078")
        self.assertEqual("TDOC", teladoc["symbol"])
        self.assertIsNone(teladoc["period"])
        arqit = next(r for r in rows if r["accession"] == "0001959173-24-000094")
        self.assertEqual("ARQQ", arqit["symbol"])


class FailClosedTests(TestCase):
    def test_missing_user_agent_is_config_error(self) -> None:
        client = EdgarDailyFilingsClient(
            user_agent="",
            efts_endpoint=EFTS,
            archive_endpoint=ARCHIVE,
            opener=_RecordingOpener([]),
        )
        with self.assertRaises(EdgarFilingsConfigError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_http_error_raises_fetch_error(self) -> None:
        opener = _RecordingOpener([HTTPError(EFTS, 403, "Forbidden", {}, None)])
        client = make_client(opener)
        with self.assertRaisesRegex(EdgarFilingsFetchError, "HTTP 403"):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_connection_error_raises_fetch_error(self) -> None:
        opener = _RecordingOpener([URLError("dns failure")])
        client = make_client(opener)
        with self.assertRaises(EdgarFilingsFetchError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_non_json_body_raises_parse_error(self) -> None:
        opener = _RecordingOpener([b"<html>rate limited</html>"])
        client = make_client(opener)
        with self.assertRaises(EdgarFilingsParseError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_missing_hits_block_raises_parse_error(self) -> None:
        opener = _RecordingOpener([{"took": 1}])
        client = make_client(opener)
        with self.assertRaises(EdgarFilingsParseError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_timed_out_response_raises_parse_error(self) -> None:
        opener = _RecordingOpener([{"timed_out": True, "hits": {"total": {"value": 0}, "hits": []}}])
        client = make_client(opener)
        with self.assertRaises(EdgarFilingsParseError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])

    def test_never_silently_returns_empty_on_failure(self) -> None:
        opener = _RecordingOpener([HTTPError(EFTS, 503, "Service Unavailable", {}, None)])
        client = make_client(opener)
        with self.assertRaises(EdgarFilingsError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])


class RetryTests(TestCase):
    def test_transient_5xx_is_retried_then_succeeds(self) -> None:
        opener = _RecordingOpener(
            [HTTPError(EFTS, 500, "Internal Server Error", {}, None), load_fixture("efts_8k_2024-01-02.json")]
        )
        client = make_client(opener, max_retries=2, retry_backoff_seconds=0.0)
        rows = client.fetch_daily_filings("2024-01-02", form_types=["8-K"])
        self.assertEqual(4, len(rows))
        self.assertEqual(2, len(opener.urls))

    def test_retries_exhausted_fails_closed(self) -> None:
        opener = _RecordingOpener([HTTPError(EFTS, 500, "boom", {}, None) for _ in range(3)])
        client = make_client(opener, max_retries=2, retry_backoff_seconds=0.0)
        with self.assertRaises(EdgarFilingsFetchError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])
        self.assertEqual(3, len(opener.urls))

    def test_client_error_is_not_retried(self) -> None:
        opener = _RecordingOpener([HTTPError(EFTS, 403, "Forbidden", {}, None)])
        client = make_client(opener, max_retries=3, retry_backoff_seconds=0.0)
        with self.assertRaises(EdgarFilingsFetchError):
            client.fetch_daily_filings("2024-01-02", form_types=["8-K"])
        self.assertEqual(1, len(opener.urls))


class PaginationTests(TestCase):
    def test_pagination_walks_every_page(self) -> None:
        fixture_hits = load_fixture("efts_form4_2024-01-03.json")["hits"]["hits"]
        page1 = {"hits": {"total": {"value": 3}, "hits": fixture_hits[:2]}}
        page2 = {"hits": {"total": {"value": 3}, "hits": fixture_hits[2:3]}}
        opener = _RecordingOpener([page1, page2])
        rows = make_client(opener).fetch_daily_filings("2024-01-03", form_types=["4"])
        self.assertEqual(3, len(rows))
        self.assertEqual(2, len(opener.urls))
        self.assertEqual("0", parse_qs(urlparse(opener.urls[0]).query)["from"][0])
        self.assertEqual("2", parse_qs(urlparse(opener.urls[1]).query)["from"][0])

    def test_exhibit_files_collapse_into_one_filing_row(self) -> None:
        primary = load_fixture("efts_8k_2024-01-02.json")["hits"]["hits"][0]
        exhibit = {
            "_id": primary["_id"].replace(":", ":ex99-1.htm"),
            "_source": {**primary["_source"], "sequence": 2, "file_type": "EX-99.1"},
        }
        page = {"hits": {"total": {"value": 2}, "hits": [primary, exhibit]}}
        rows = make_client(_RecordingOpener([page])).fetch_daily_filings("2024-01-02", form_types=["8-K"])
        self.assertEqual(1, len(rows))
        self.assertEqual(primary["_source"]["adsh"], rows[0]["accession"])
        self.assertEqual(primary["_id"].split(":", 1)[1], rows[0]["primary_document"])

    def test_page_cap_fails_closed_instead_of_truncating(self) -> None:
        full_page = load_fixture("efts_form4_2024-01-03.json")
        payload = {"hits": {"total": {"value": 9999}, "hits": full_page["hits"]["hits"]}}
        opener = _RecordingOpener([payload, payload, payload])
        client = make_client(opener, max_pages=2)
        with self.assertRaises(EdgarFilingsParseError):
            client.fetch_daily_filings("2024-01-03", form_types=["4"], max_pages=2)


class StorageTests(TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "edgar_filings"
        self.store = EdgarFilingsStore(self.root)
        opener = _RecordingOpener([load_fixture("efts_8k_2024-01-02.json")])
        self.rows = make_client(opener).fetch_daily_filings("2024-01-02", form_types=["8-K"])
        self.query = {"q": '""', "dateRange": "custom", "startdt": "2024-01-02", "enddt": "2024-01-02", "forms": "8-K", "from": "0"}

    def test_write_is_atomic_hashed_and_idempotent(self) -> None:
        reference = self.store.write_daily("2024-01-02", self.rows, query=self.query)
        self.assertFalse(reference["idempotent"])
        self.assertEqual(4, reference["row_count"])
        path = self.store.daily_path("2024-01-02")
        self.assertTrue(path.exists())
        self.assertEqual("2024-01-02.json", reference["relative_path"])
        self.assertTrue(self.store.daily_parquet_path("2024-01-02").exists())

        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(reference["content_sha256"], document["content_sha256"])
        content = {k: v for k, v in document.items() if k not in {"fetched_at", "content_sha256"}}
        self.assertEqual(reference["content_sha256"], EdgarFilingsStore.content_digest(EdgarFilingsStore.identity_content(content)))
        self.assertEqual(4, document["row_count"])

        # No temp files survive the atomic write.
        self.assertEqual([], [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")])

        # Same content is idempotent (regardless of a new fetched_at).
        again = self.store.write_daily("2024-01-02", self.rows, query=self.query, fetched_at="2030-01-01T00:00:00+00:00")
        self.assertTrue(again["idempotent"])
        self.assertEqual(reference["content_sha256"], again["content_sha256"])

    def test_refuses_to_overwrite_different_content(self) -> None:
        self.store.write_daily("2024-01-02", self.rows, query=self.query)
        with self.assertRaises(EdgarFilingsConflictError):
            self.store.write_daily("2024-01-02", self.rows[:2], query=self.query)
        updated = self.store.write_daily("2024-01-02", self.rows[:2], query=self.query, overwrite=True)
        self.assertFalse(updated["idempotent"])
        self.assertEqual(2, updated["row_count"])
        self.assertEqual(2, self.store.read_daily("2024-01-02")["row_count"])

    def test_read_daily_rejects_tampered_content(self) -> None:
        self.store.write_daily("2024-01-02", self.rows, query=self.query)
        path = self.store.daily_path("2024-01-02")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["rows"][0]["symbol"] = "FAKE"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(EdgarFilingsParseError):
            self.store.read_daily("2024-01-02")

    def test_fetched_at_is_not_part_of_content_identity(self) -> None:
        first = self.store.build_document("2024-01-02", self.rows, query=self.query)
        second = self.store.build_document("2024-01-02", self.rows, query=self.query)
        self.assertEqual(first["content_sha256"], second["content_sha256"])

    def test_re_fetch_at_a_later_instant_is_idempotent(self) -> None:
        # Two independent real fetches of the same filing day differ only in
        # their provenance clock; the artifact content must still match.
        from datetime import datetime, timezone

        early = make_client(
            _RecordingOpener([load_fixture("efts_8k_2024-01-02.json")]),
            now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        ).fetch_daily_filings("2024-01-02", form_types=["8-K"])
        late = make_client(
            _RecordingOpener([load_fixture("efts_8k_2024-01-02.json")]),
            now=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        ).fetch_daily_filings("2024-01-02", form_types=["8-K"])
        self.assertNotEqual(early[0]["fetched_at"], late[0]["fetched_at"])

        first = self.store.write_daily("2024-01-02", early, query=self.query)
        second = self.store.write_daily("2024-01-02", late, query=self.query)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])


class EventModelTests(TestCase):
    def _rows(self, fixture: str, field: date | str, forms: list[str]) -> list[dict]:
        opener = _RecordingOpener([load_fixture(fixture)])
        return make_client(opener).fetch_daily_filings(field, form_types=forms)

    def test_event_type_mapping(self) -> None:
        self.assertEqual("8-K", event_type_for_form("8-K"))
        self.assertEqual("8-K", event_type_for_form("8-K/A"))
        self.assertEqual("Form4", event_type_for_form("4"))
        self.assertEqual("Form4", event_type_for_form("4/A"))
        self.assertEqual("144", event_type_for_form("144"))

    def test_available_time_is_filed_date_not_sync_time(self) -> None:
        rows = self._rows("efts_8k_2024-01-02.json", "2024-01-02", ["8-K"])
        events = build_events(rows)
        self.assertTrue(events)
        for event in events:
            self.assertEqual("2024-01-02", event.filed_date.isoformat())
            # End of the filed day in U.S. Eastern time (EST = -05:00 in January).
            self.assertEqual("2024-01-02T23:59:59.999999-05:00", event.available_time)
            self.assertIn("available_time", event.to_record())
            self.assertEqual(EDGAR_PROVIDER, event.to_record()["provider"])
            self.assertEqual("US", event.to_record()["market"])
            self.assertTrue(event.source_reference)
            self.assertEqual(16, len(event.revision_id))

        # Re-fetching the same filings at a different sync instant must not move
        # the point-in-time availability.
        later_rows = [dict(row, fetched_at="2031-07-01T12:00:00+00:00") for row in rows]
        later_events = build_events(later_rows)
        self.assertEqual(
            [event.available_time for event in events],
            [event.available_time for event in later_events],
        )

    def test_period_drives_event_time(self) -> None:
        rows = self._rows("efts_8k_2024-01-02.json", "2024-01-02", ["8-K"])
        casas = next(row for row in rows if row["accession"] == "0001193125-23-306062")
        event = next(e for e in build_events([casas]))
        self.assertEqual("2023-12-27", event.period.isoformat())
        self.assertEqual("2023-12-27T00:00:00-05:00", event.event_time)

    def test_form144_event_has_no_event_time(self) -> None:
        rows = self._rows("efts_144_2024-01-03.json", "2024-01-03", ["144"])
        for event in build_events(rows):
            self.assertEqual("144", event.event_type)
            self.assertIsNone(event.period)
            self.assertIsNone(event.event_time)

    def test_form4_event_type_and_symbol(self) -> None:
        rows = self._rows("efts_form4_2024-01-03.json", "2024-01-03", ["4"])
        events = build_events(rows)
        by_accession = {event.accession: event for event in events}
        self.assertEqual("Form4", by_accession["0001628280-24-000262"].event_type)
        self.assertEqual("BBAI", by_accession["0001628280-24-000262"].symbol)

    def test_event_requires_source_reference(self) -> None:
        with self.assertRaises(ValueError):
            build_events([{"filed_date": "2024-01-02", "form_type": "8-K", "source_reference": ""}])


class DailyIndexTests(TestCase):
    """The advisory cross-check reads EDGAR's official ``form.YYYYMMDD.idx``."""

    def test_parses_fixed_width_index_and_ignores_header(self) -> None:
        snapshot = load_daily_index("daily_index_8k_2024-01-02.idx")
        self.assertEqual(date(2024, 1, 2), snapshot.filed_date)
        # 4x 8-K + 1x 4 + 1x 10-K: header/blank/separator lines are skipped.
        self.assertEqual(6, snapshot.row_count)
        self.assertEqual({"8-K": 4, "4": 1, "10-K": 1}, dict(snapshot.counts_by_root_form))

    def test_amendments_fold_onto_their_root_form(self) -> None:
        snapshot = load_daily_index("daily_index_8k_2024-01-02_mismatch.idx")
        # 5x 8-K + 1x 8-K/A -> 6 root-form 8-K rows, comparable to the fetch,
        # which also keeps an amendment of a requested form.
        self.assertEqual(6, snapshot.counts_by_root_form["8-K"])
        self.assertEqual(8, snapshot.row_count)

    def test_client_reads_index_text_and_builds_quarter_scoped_url(self) -> None:
        body = (FIXTURES / "daily_index_8k_2024-01-02.idx").read_bytes()
        opener = _RecordingOpener([body])
        client = make_client(opener, daily_index_endpoint="https://www.sec.gov/Archives/edgar/daily-index")
        snapshot = client.fetch_daily_index("2024-01-02")
        self.assertEqual(4, snapshot.counts_by_root_form["8-K"])
        self.assertEqual(
            "https://www.sec.gov/Archives/edgar/daily-index/2024/QTR1/form.20240102.idx",
            opener.urls[0],
        )
        self.assertTrue(client.daily_index_url("2024-12-31").endswith("/2024/QTR4/form.20241231.idx"))

    def test_unreadable_index_fails_closed_at_the_client_layer(self) -> None:
        missing = HTTPError("https://www.sec.gov/x.idx", 404, "Not Found", {}, None)
        client = make_client(_RecordingOpener([missing]))
        with self.assertRaises(EdgarFilingsFetchError):
            client.fetch_daily_index("2024-01-02")

    def test_index_read_requires_user_agent(self) -> None:
        client = EdgarDailyFilingsClient(
            user_agent="",
            efts_endpoint=EFTS,
            archive_endpoint=ARCHIVE,
            opener=_RecordingOpener([]),
        )
        with self.assertRaises(EdgarFilingsConfigError):
            client.fetch_daily_index("2024-01-02")


class ReconciliationTests(TestCase):
    def test_matching_index_is_not_a_warning(self) -> None:
        result = build_edgar_reconciliation(
            "2024-01-02",
            load_8k_rows(),
            forms=["8-K"],
            index=load_daily_index("daily_index_8k_2024-01-02.idx"),
        )
        self.assertEqual("match", result.status)
        self.assertTrue(result.matched)
        self.assertFalse(result.blocking)
        self.assertEqual(4, result.expected_total)
        self.assertEqual(4, result.observed_total)
        self.assertEqual(0, result.delta_by_form["8-K"])
        self.assertIsNone(result.advisory())
        payload = result.to_dict()
        self.assertEqual(EDGAR_RECONCILIATION_SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(4, payload["expected_total"])
        self.assertEqual(4, payload["observed_total"])

    def test_mismatch_warns_with_both_counts_but_never_blocks(self) -> None:
        result = build_edgar_reconciliation(
            "2024-01-02",
            load_8k_rows(),
            forms=["8-K"],
            index=load_daily_index("daily_index_8k_2024-01-02_mismatch.idx"),
        )
        self.assertEqual("mismatch", result.status)
        self.assertFalse(result.matched)
        # Advisory by contract: the verdict never blocks the caller.
        self.assertFalse(result.blocking)
        self.assertEqual(6, result.expected_total)
        self.assertEqual(4, result.observed_total)
        self.assertEqual(-2, result.delta_by_form["8-K"])
        advisory = result.advisory()
        self.assertIsNotNone(advisory)
        self.assertIn("index=6", advisory)
        self.assertIn("fetched=4", advisory)
        self.assertIn("not blocking", advisory)
        payload = result.to_dict()
        self.assertEqual(6, payload["expected_total"])
        self.assertEqual(4, payload["observed_total"])
        self.assertTrue(payload["notes"])

    def test_unavailable_index_keeps_fetched_counts_and_is_advisory(self) -> None:
        result = build_edgar_reconciliation(
            "2024-01-02",
            load_8k_rows(),
            forms=["8-K"],
            index_unavailable_reason="EDGAR HTTP 503",
        )
        self.assertEqual("index_unavailable", result.status)
        self.assertFalse(result.matched)
        self.assertFalse(result.blocking)
        self.assertIsNone(result.expected_total)
        self.assertEqual(4, result.observed_total)
        advisory = result.advisory()
        self.assertIsNotNone(advisory)
        self.assertIn("EDGAR HTTP 503", advisory)

    def test_only_requested_forms_are_compared(self) -> None:
        result = build_edgar_reconciliation(
            "2024-01-02",
            load_8k_rows(),
            forms=["8-K"],
            index=load_daily_index("daily_index_8k_2024-01-02.idx"),
        )
        self.assertEqual(("8-K",), result.forms)
        self.assertEqual({"8-K": 4}, dict(result.expected_by_form))
        self.assertEqual({"8-K": 0}, dict(result.delta_by_form))

    def test_reconciliation_sidecar_leaves_artifact_identity_alone(self) -> None:
        with TemporaryDirectory() as name:
            store = EdgarFilingsStore(Path(name) / "edgar_filings")
            rows = load_8k_rows()
            query = {
                "q": '""',
                "dateRange": "custom",
                "startdt": "2024-01-02",
                "enddt": "2024-01-02",
                "forms": "8-K",
                "from": "0",
            }
            reference = store.write_daily("2024-01-02", rows, query=query)
            result = build_edgar_reconciliation(
                "2024-01-02",
                rows,
                forms=["8-K"],
                index=load_daily_index("daily_index_8k_2024-01-02_mismatch.idx"),
            )
            path = store.write_reconciliation(result)
            self.assertEqual(store.reconciliation_path("2024-01-02"), path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(6, payload["expected_total"])
            self.assertEqual(4, payload["observed_total"])
            self.assertFalse(payload["blocking"])
            # Re-running the same check rewrites identical bytes.
            first = path.read_bytes()
            store.write_reconciliation(result)
            self.assertEqual(first, path.read_bytes())
            # The advisory sidecar must not be part of the filing artifact.
            document = json.loads(store.daily_path("2024-01-02").read_text(encoding="utf-8"))
            self.assertNotIn("reconciliation", document)
            self.assertEqual(reference["content_sha256"], document["content_sha256"])


class ConservativeDefaultDateTests(TestCase):
    def test_default_never_picks_an_unfinished_et_day(self) -> None:
        # 20:00 ET on Jan 2 == 01:00 UTC Jan 3.  A naive "yesterday UTC" default
        # would select the *current, still-being-written* ET day; the
        # conservative rule steps back to Jan 1.
        self.assertEqual(
            date(2024, 1, 1),
            edgar_default_filed_date(datetime(2024, 1, 3, 1, 0, tzinfo=timezone.utc)),
        )
        # 23:30 ET on Jan 2 (index final) -> Jan 2.
        self.assertEqual(
            date(2024, 1, 2),
            edgar_default_filed_date(datetime(2024, 1, 3, 4, 30, tzinfo=timezone.utc)),
        )
        # 08:00 ET on Jan 2 -> Jan 1 (Jan 2's index is not final yet).
        self.assertEqual(
            date(2024, 1, 1),
            edgar_default_filed_date(datetime(2024, 1, 2, 13, 0, tzinfo=timezone.utc)),
        )

    def test_index_finality_boundary_is_2300_et(self) -> None:
        self.assertFalse(
            edgar_index_is_final("2024-01-02", now=datetime(2024, 1, 3, 1, 0, tzinfo=timezone.utc))
        )
        self.assertTrue(
            edgar_index_is_final("2024-01-02", now=datetime(2024, 1, 3, 4, 30, tzinfo=timezone.utc))
        )


@skipUnless(os.environ.get("ANA_EDGAR_LIVE_SMOKE") == "1", "set ANA_EDGAR_LIVE_SMOKE=1 to hit the real SEC endpoint")
class LiveSmokeTests(TestCase):
    def test_real_edgar_fetch(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.sec_user_agent:
            self.skipTest("PQW_SEC_USER_AGENT is not configured")
        client = EdgarDailyFilingsClient(settings=settings)
        rows = client.fetch_daily_filings("2024-01-02", form_types=["8-K", "4", "144"])
        self.assertTrue(rows)
        self.assertTrue(all(row["provider"] == EDGAR_PROVIDER for row in rows))


class _StubEdgarClient:
    """CLI stub: replays recorded rows and one daily-index snapshot."""

    def __init__(self, rows: list[dict], index: EdgarDailyIndexSnapshot | None, error: str | None = None) -> None:
        self._rows = rows
        self._index = index
        self._error = error

    def fetch_daily_filings(self, filed_date, *, form_types=None, max_pages=None):  # noqa: ARG002
        return [dict(row) for row in self._rows]

    def fetch_daily_index(self, filed_date):  # noqa: ARG002
        if self._error:
            raise EdgarFilingsFetchError(self._error)
        return self._index

    def build_query(self, filed_date, form_types, offset):  # noqa: ARG002
        return {"startdt": "2024-01-02", "enddt": "2024-01-02", "forms": ",".join(form_types)}


class CliReconciliationTests(TestCase):
    """The CLI prints both counts, writes the sidecar, and always exits 0."""

    def _run(
        self, index: EdgarDailyIndexSnapshot | None = None, error: str | None = None
    ) -> tuple[int, str, str, dict]:
        rows = load_8k_rows()
        with TemporaryDirectory() as temp:
            with (
                patch.object(cli, "EdgarDailyFilingsClient", lambda **kwargs: _StubEdgarClient(rows, index, error)),
                patch.object(cli, "get_settings", lambda: object()),
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(
                        ["--date", "2024-01-02", "--forms", "8-K", "--out-root", temp, "--sample-events", "0"]
                    )
            sidecar = Path(temp) / "2024-01-02.reconciliation.json"
            payload = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
        return code, out.getvalue(), err.getvalue(), payload

    def test_match_prints_counts_and_reports_no_warning(self) -> None:
        code, out, err, payload = self._run(load_daily_index("daily_index_8k_2024-01-02.idx"))
        self.assertEqual(0, code)
        self.assertIn("reconcile       : MATCH (index=4 fetched=4)", out)
        self.assertEqual("", err)
        self.assertEqual("match", payload["status"])
        self.assertTrue(payload["matched"])

    def test_mismatch_warns_but_still_exits_zero(self) -> None:
        code, out, err, payload = self._run(load_daily_index("daily_index_8k_2024-01-02_mismatch.idx"))
        self.assertEqual(0, code)  # advisory only: a mismatch never blocks
        self.assertIn("MISMATCH (index=6 fetched=4)", out)
        self.assertIn("index=6 fetched=4 delta=-2", out)
        self.assertIn("WARN(advisory)", err)
        self.assertIn("not blocking", err)
        self.assertEqual("mismatch", payload["status"])
        self.assertEqual(6, payload["expected_total"])
        self.assertEqual(4, payload["observed_total"])
        self.assertFalse(payload["blocking"])

    def test_unreadable_index_is_reported_without_failing_the_run(self) -> None:
        code, out, err, payload = self._run(error="EDGAR HTTP 403 for the daily index")
        self.assertEqual(0, code)
        self.assertIn("INDEX_UNAVAILABLE (index=n/a fetched=4)", out)
        self.assertIn("WARN(advisory)", err)
        self.assertIn("EDGAR HTTP 403", err)
        self.assertEqual("index_unavailable", payload["status"])
        self.assertIsNone(payload["expected_total"])
        self.assertEqual(4, payload["observed_total"])
        self.assertFalse(payload["blocking"])
