"""Read-only SEC EDGAR daily-filings adapter (US event-ledger source).

Recipe borrowed from the ``global-stock-data`` EDGAR notes: query the official
EDGAR full-text search endpoint (``efts.sec.gov/LATEST/search-index``) for a
single filed date, filtered by form type.  Only the *recipe* is borrowed - no
runtime dependency is added; the module uses ``urllib`` plus ``polars`` (both
already required by the project).

Gap closed: ``docs/execution-fact-source-audit-2026-09-28-zh.md`` records the
SEC EDGAR adapter as *fundamentals only* ("不能把基本面接口当完整事件账本").
This module is that missing event source.  Every filing row carries
``accession / form_type / filed_date / period / company(ticker, cik) / url``
plus ``provider``/``source_reference``/``fetched_at`` provenance.

Point-in-time semantics: a filing becomes knowable on its ``filed_date`` (EDGAR
accepts filings during U.S. Eastern business hours), never on the local
ingestion clock.  ``EdgarFilingEvent.available_time`` is therefore the *end* of
``filed_date`` in ``America/New_York`` while ``fetched_at`` is provenance only
and must never be used as an availability timestamp.

Fail-closed: missing User-Agent, HTTP errors, timeouts, malformed JSON and
missing ``hits`` all raise :class:`EdgarFilingsError`.  A fetch never returns a
silently empty frame.

Ledger scope: this module deliberately exposes a *standalone* read-only event
structure (:class:`EdgarFilingEvent`) instead of writing into
``stock_selection/decision_ledger``.  The decision ledger records stock-selection
*publication decisions* with a T+1 effective trade date - different semantics
from corporate filings - so wiring them together would corrupt both.  A future
US event ledger can consume ``EdgarFilingEvent.to_record()`` unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import polars as pl

from app.core.config import get_settings
from app.services.ticker_format import normalize_ticker_for_market

EDGAR_PROVIDER = "sec_edgar"
EDGAR_FILINGS_SCHEMA_VERSION = "edgar_daily_filings_v1"
EDGAR_EVENT_SCHEMA_VERSION = "edgar_filing_event_v1"
EDGAR_FILINGS_SUBDIR = "edgar_filings"

#: Form types the daily flow is scoped to.  Callers may pass any subset.
DAILY_FILING_FORMS: tuple[str, ...] = ("8-K", "4", "144")

# EDGAR dates (``file_date`` / ``period_ending``) are plain calendar dates in
# U.S. Eastern time; U.S. equities trade on the same clock.
_EDGAR_TZ = ZoneInfo("America/New_York")

# Event-ledger type keyed by the *root* form so ``4/A`` collapses onto ``Form4``.
_EVENT_TYPE_BY_ROOT_FORM = {
    "8-K": "8-K",
    "4": "Form4",
    "3": "Form3",
    "5": "Form5",
    "144": "144",
}

_DISPLAY_NAME_TOKEN = re.compile(r"\((?P<inner>[^()]*)\)")
_NON_DIGIT = re.compile(r"\D")

#: Flat parquet projection of the daily JSON rows (nested ``company`` dropped).
EDGAR_FILINGS_SCHEMA: dict[str, pl.DataType] = {
    "accession": pl.String,
    "form_type": pl.String,
    "root_form": pl.String,
    "filed_date": pl.String,
    "period": pl.String,
    "symbol": pl.String,
    "cik": pl.String,
    "company_name": pl.String,
    "url": pl.String,
    "primary_document": pl.String,
    "items": pl.String,
    "provider": pl.String,
    "source_reference": pl.String,
    "fetched_at": pl.String,
}

# SEC fair-access: at most 10 requests/second across every client.  A single
# process-wide throttle keeps the whole app under that ceiling.
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


class EdgarFilingsError(RuntimeError):
    """Base class for every EDGAR filings failure (all fail-closed)."""


class EdgarFilingsConfigError(EdgarFilingsError):
    """The integration is not correctly configured (e.g. no SEC User-Agent)."""


class EdgarFilingsFetchError(EdgarFilingsError):
    """The HTTP request to EDGAR failed."""


class EdgarFilingsParseError(EdgarFilingsError):
    """EDGAR returned a body we refuse to interpret."""


class EdgarFilingsConflictError(EdgarFilingsError):
    """A daily artifact already exists with *different* content."""


def canonical_json_bytes(payload: Mapping) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _coerce_date(value: date | str | None, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise EdgarFilingsError(f"{field} must be an ISO date, got `{value}`") from exc


def event_type_for_form(form_type: str) -> str:
    """Map a raw EDGAR form type to the event-ledger type (``8-K``/``Form4``/``144``)."""

    base = str(form_type or "").strip().upper().split("/")[0]
    return _EVENT_TYPE_BY_ROOT_FORM.get(base, base)


def normalize_form_types(form_types: Iterable[str] | str | None) -> tuple[str, ...]:
    """Normalize and validate the caller's form filter, fail-closed on unknown forms."""

    if form_types is None:
        return DAILY_FILING_FORMS
    raw: Sequence[str] = [form_types] if isinstance(form_types, str) else list(form_types)
    normalized: list[str] = []
    for item in raw:
        value = str(item or "").strip().upper()
        if not value:
            continue
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise EdgarFilingsError("at least one EDGAR form type is required")
    unsupported = [item for item in normalized if item not in DAILY_FILING_FORMS]
    if unsupported:
        raise EdgarFilingsError(
            f"unsupported EDGAR form type(s) {unsupported}; expected a subset of {DAILY_FILING_FORMS}"
        )
    return tuple(normalized)


def _throttle(min_interval_seconds: float) -> None:
    global _LAST_REQUEST_AT
    interval = max(0.0, float(min_interval_seconds or 0.0))
    with _REQUEST_LOCK:
        elapsed = time.monotonic() - _LAST_REQUEST_AT
        wait_seconds = interval - elapsed
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_REQUEST_AT = time.monotonic()


def _parse_display_name(value: object) -> dict:
    """Split EDGAR's ``"Name  (TICKER, TICKER-WT)  (CIK 0000000000)"`` token.

    Returns ``{"name": str, "cik": str | None, "tickers": [str, ...]}``.  EDGAR
    wraps the tickers and CIK in the trailing parentheticals; a name containing
    parentheses is rare and is tolerated by only stripping recognized tokens.
    """

    text = str(value or "").strip()
    tickers: list[str] = []
    cik: str | None = None
    for match in _DISPLAY_NAME_TOKEN.finditer(text):
        inner = match.group("inner").strip()
        if inner.upper().startswith("CIK"):
            digits = _NON_DIGIT.sub("", inner)
            if digits:
                cik = digits.zfill(10)
            continue
        for part in inner.split(","):
            ticker = part.strip().upper()
            if ticker:
                tickers.append(ticker)
    name = _DISPLAY_NAME_TOKEN.sub(" ", text)
    name = " ".join(name.split()).strip(" ,")
    return {"name": name or None, "cik": cik, "tickers": tickers}


class EdgarDailyFilingsClient:
    """Read-only EDGAR daily-filings client (fail-closed)."""

    def __init__(
        self,
        *,
        settings=None,
        user_agent: str | None = None,
        efts_endpoint: str | None = None,
        archive_endpoint: str | None = None,
        min_request_interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        max_pages: int | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        ticker_map: Mapping[str, str] | None = None,
        opener: Callable | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if user_agent is None:
            self.user_agent = str(self.settings.sec_user_agent or "").strip()
        else:
            # An explicit empty string disables the integration (fail-closed);
            # it must not silently fall back to a different configured agent.
            self.user_agent = str(user_agent).strip()
        self.efts_endpoint = str(efts_endpoint or self.settings.sec_efts_endpoint).rstrip("?")
        self.archive_endpoint = str(
            archive_endpoint or self.settings.sec_filings_archive_endpoint
        ).rstrip("/")
        self.min_request_interval_seconds = float(
            self.settings.sec_min_request_interval_seconds
            if min_request_interval_seconds is None
            else min_request_interval_seconds
        )
        self.timeout_seconds = float(
            self.settings.sec_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self.max_pages = int(self.settings.sec_filings_max_pages if max_pages is None else max_pages)
        self.max_retries = max(
            0, int(self.settings.sec_filings_max_retries if max_retries is None else max_retries)
        )
        self.retry_backoff_seconds = float(
            self.settings.sec_filings_retry_backoff_seconds
            if retry_backoff_seconds is None
            else retry_backoff_seconds
        )
        #: Optional ``cik -> ticker`` map for private/exotic issuers whose
        #: display name omits a ticker.  Injected in tests; fetched lazily on
        #: the first miss in production.
        self._ticker_map = dict(ticker_map) if ticker_map is not None else None
        self._opener = opener or urlopen
        self._now = now or (lambda: datetime.now(tz=timezone.utc))

    # -- HTTP -----------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }

    # Retryable EDGAR transport failures: rate limiting and transient 5xx.
    _RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

    def _request_json(self, url: str) -> dict:
        if not self.user_agent:
            raise EdgarFilingsConfigError(
                "SEC User-Agent is required (e.g. `Your Org admin@example.com`); "
                "set PQW_SEC_USER_AGENT before enabling the EDGAR filings source"
            )
        body = self._read_with_retries(url)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgarFilingsParseError(f"EDGAR returned non-JSON body for {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise EdgarFilingsParseError(f"EDGAR returned a non-object JSON body for {url}")
        return payload

    def _read_with_retries(self, url: str) -> bytes:
        last_error: EdgarFilingsFetchError | None = None
        for attempt in range(self.max_retries + 1):
            _throttle(self.min_request_interval_seconds)
            request = Request(url, headers=self._headers())
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    if str(response.headers.get("Content-Encoding") or "").lower() == "gzip":
                        body = gzip.decompress(body)
                return body
            except HTTPError as exc:
                last_error = EdgarFilingsFetchError(f"EDGAR HTTP {exc.code} for {url}")
                retryable = exc.code in self._RETRYABLE_HTTP_CODES
            except URLError as exc:
                last_error = EdgarFilingsFetchError(f"EDGAR connection failed for {url}: {exc.reason}")
                retryable = True
            except (TimeoutError, OSError) as exc:
                last_error = EdgarFilingsFetchError(f"EDGAR request failed for {url}: {exc}")
                retryable = True
            if not retryable or attempt >= self.max_retries:
                raise last_error
            time.sleep(max(0.0, self.retry_backoff_seconds) * (2**attempt))
        raise last_error  # pragma: no cover - loop always returns or raises

    def _ticker_lookup(self) -> dict[str, str]:
        if self._ticker_map is not None:
            return self._ticker_map
        if not self.user_agent:
            raise EdgarFilingsConfigError("SEC User-Agent is required to resolve CIK->ticker")
        payload = self._request_json(str(self.settings.sec_company_tickers_endpoint))
        mapping: dict[str, str] = {}
        for row in payload.values() if isinstance(payload, dict) else []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            try:
                cik = str(int(row.get("cik_str"))).zfill(10)
            except (TypeError, ValueError):
                continue
            if ticker:
                mapping[cik] = ticker
        self._ticker_map = mapping
        return mapping

    # -- Query ----------------------------------------------------------------
    def build_query(self, filed_date: date, form_types: Sequence[str], offset: int) -> dict[str, str]:
        return {
            "q": '""',
            "dateRange": "custom",
            "startdt": filed_date.isoformat(),
            "enddt": filed_date.isoformat(),
            "forms": ",".join(form_types),
            "from": str(max(0, int(offset))),
        }

    def _request_url(self, query: Mapping[str, str]) -> str:
        return f"{self.efts_endpoint}?{urlencode(query)}"

    # -- Fetch ----------------------------------------------------------------
    def fetch_daily_filings(
        self,
        filed_date: date | str,
        *,
        form_types: Sequence[str] | str | None = None,
        max_pages: int | None = None,
    ) -> list[dict]:
        """Fetch every filing of the requested forms filed on ``filed_date``.

        Raises :class:`EdgarFilingsError` on any failure; never returns an empty
        frame to mask an outage.
        """

        day = _coerce_date(filed_date, field="filed_date")
        forms = normalize_form_types(form_types)
        page_cap = max(1, int(self.max_pages if max_pages is None else max_pages))
        fetched_at = self._now().isoformat()

        rows: list[dict] = []
        seen: set[str] = set()
        # Query one form at a time: EDGAR's full-text search only serves a fixed
        # result window (10k hits), and a single busy day of Form 4s can exceed
        # what a combined multi-form query would return.
        for form in forms:
            offset = 0
            total: int | None = None
            for _page in range(page_cap):
                query = self.build_query(day, [form], offset)
                request_url = self._request_url(query)
                # Provenance carries the query *without* the transport-only
                # page offset so a filing's source_reference is stable when the
                # same day is re-ingested.
                source_url = self._request_url({key: value for key, value in query.items() if key != "from"})
                payload = self._request_json(request_url)
                page_hits, total = self._extract_hits(payload, request_url)
                for hit in page_hits:
                    row = self._row_from_hit(
                        hit,
                        source_reference_url=source_url,
                        form_types=forms,
                        fetched_at=fetched_at,
                    )
                    if row is None or row["accession"] in seen:
                        continue
                    seen.add(row["accession"])
                    rows.append(row)
                if not page_hits:
                    break
                offset += len(page_hits)
                if total is not None and offset >= total:
                    break
            else:
                raise EdgarFilingsParseError(
                    f"EDGAR pagination exceeded {page_cap} pages for {day.isoformat()} form={form}; "
                    "refusing a partial daily frame"
                )
        rows.sort(key=lambda row: (row["filed_date"], row["form_type"], row["accession"]))
        return rows

    def _extract_hits(
        self,
        payload: dict,
        request_url: str,
    ) -> tuple[list[dict], int | None]:
        if payload.get("timed_out") is True:
            raise EdgarFilingsParseError(f"EDGAR reported timed_out=true for {request_url}")
        hits_block = payload.get("hits")
        if not isinstance(hits_block, dict):
            raise EdgarFilingsParseError(f"EDGAR response for {request_url} has no `hits` object")
        page_hits = hits_block.get("hits")
        if not isinstance(page_hits, list):
            raise EdgarFilingsParseError(f"EDGAR response for {request_url} has no `hits.hits` list")
        total_value = hits_block.get("total")
        total: int | None = None
        if isinstance(total_value, dict) and total_value.get("value") is not None:
            try:
                total = int(total_value["value"])
            except (TypeError, ValueError):
                total = None
        elif isinstance(total_value, int):
            total = total_value
        return [hit for hit in page_hits if isinstance(hit, dict)], total

    def _row_from_hit(
        self,
        hit: dict,
        *,
        source_reference_url: str,
        form_types: Sequence[str],
        fetched_at: str,
    ) -> dict | None:
        source = hit.get("_source")
        if not isinstance(source, dict):
            return None
        # EDGAR's full-text search indexes *files*, so one accession (filing)
        # yields a hit for its primary document plus one hit per exhibit.  Keep
        # the primary document (``sequence == 1``) and let the accession-level
        # de-duplication in ``fetch_daily_filings`` collapse the rest.
        sequence = source.get("sequence")
        if isinstance(sequence, int) and sequence != 1:
            return None
        accession = str(source.get("adsh") or "").strip()
        form_type = str(source.get("form") or "").strip().upper()
        if not form_type:
            root_forms = source.get("root_forms") or []
            form_type = str(root_forms[0]).strip().upper() if root_forms else ""
        if not accession or not form_type:
            raise EdgarFilingsParseError(f"EDGAR hit is missing accession/form: {hit!r}")
        root_forms = [str(item).strip().upper() for item in (source.get("root_forms") or []) if str(item).strip()]
        # Server-side `forms=` already filters; re-check client-side so an
        # amendment (``8-K/A``) is only kept when its root form was requested.
        base_form = form_type.split("/")[0]
        if base_form not in set(form_types) and not any(item in set(form_types) for item in root_forms):
            return None
        filed_date = _coerce_date(source.get("file_date"), field="file_date").isoformat()
        period_raw = source.get("period_ending")
        period = _coerce_date(period_raw, field="period_ending").isoformat() if period_raw else None

        ciks = [
            str(int(item)).zfill(10)
            for item in (source.get("ciks") or [])
            if str(item).strip().isdigit()
        ]
        primary_document = None
        hit_id = str(hit.get("_id") or "")
        if ":" in hit_id:
            primary_document = hit_id.split(":", 1)[1].strip() or None
        url = None
        if ciks and primary_document:
            url = self._archive_url(accession, ciks[0], primary_document)
        company = self._resolve_company(source.get("display_names") or [], ciks)

        return {
            "provider": EDGAR_PROVIDER,
            "accession": accession,
            "form_type": form_type,
            "root_form": (root_forms or [base_form])[0],
            "filed_date": filed_date,
            "period": period,
            "symbol": company["ticker"],
            "cik": company["cik"],
            "company_name": company["name"],
            "company": dict(company),
            "url": url,
            "primary_document": primary_document,
            "items": [str(item) for item in (source.get("items") or [])],
            "all_ciks": ciks,
            "source_reference": f"{EDGAR_PROVIDER}:efts:{source_reference_url}#{accession}",
            "fetched_at": fetched_at,
        }

    def _archive_url(self, accession: str, cik: str, primary_document: str) -> str:
        return f"{self.archive_endpoint}/{int(cik)}/{accession.replace('-', '')}/{primary_document}"

    def _resolve_company(self, display_names: Sequence[object], ciks: Sequence[str]) -> dict:
        entries = [_parse_display_name(name) for name in display_names]
        for entry in entries:
            if entry["tickers"]:
                ticker = normalize_ticker_for_market(entry["tickers"][0], "US")
                return {"name": entry["name"], "ticker": ticker or None, "cik": entry["cik"] or (ciks[0] if ciks else None)}
        cik = ciks[0] if ciks else None
        matched = next((entry for entry in entries if entry["cik"] and entry["cik"] == cik), None)
        name = matched["name"] if matched else (entries[0]["name"] if entries else None)
        ticker = None
        if cik:
            try:
                ticker = normalize_ticker_for_market(self._ticker_lookup().get(cik, ""), "US") or None
            except EdgarFilingsError:
                # A missing ticker must never mask the filing row; symbol stays null.
                ticker = None
        return {"name": name, "ticker": ticker, "cik": cik}


class EdgarFilingsStore:
    """Atomic, content-hashed, idempotent daily filing artifacts.

    Files live in their own directory (``artifacts/edgar_filings/<date>.json`` +
    ``.parquet``) and are never mixed into the market price lake.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            root = Path(get_settings().artifacts_dir) / EDGAR_FILINGS_SUBDIR
        self.root = Path(root)

    def daily_path(self, filed_date: date | str) -> Path:
        day = _coerce_date(filed_date, field="filed_date")
        return self.root / f"{day.isoformat()}.json"

    def daily_parquet_path(self, filed_date: date | str) -> Path:
        day = _coerce_date(filed_date, field="filed_date")
        return self.root / f"{day.isoformat()}.parquet"

    def build_document(self, filed_date: date | str, rows: Sequence[dict], *, query: Mapping) -> dict:
        day = _coerce_date(filed_date, field="filed_date")
        content = {
            "schema_version": EDGAR_FILINGS_SCHEMA_VERSION,
            "provider": EDGAR_PROVIDER,
            "filed_date": day.isoformat(),
            "query": {str(key): str(value) for key, value in dict(query).items()},
            "row_count": len(rows),
            "rows": [dict(row) for row in rows],
        }
        return {**content, "content_sha256": self.content_digest(self.identity_content(content))}

    @staticmethod
    def _identity_row(row: Mapping) -> dict:
        """Strip volatile provenance so a re-fetch of the same filings matches."""

        return {str(key): value for key, value in row.items() if key != "fetched_at"}

    @staticmethod
    def identity_content(content: Mapping) -> dict:
        rows = content.get("rows") or []
        return {**content, "rows": [EdgarFilingsStore._identity_row(row) for row in rows]}

    @staticmethod
    def content_digest(content: Mapping) -> str:
        return hashlib.sha256(canonical_json_bytes(content)).hexdigest()

    def write_daily(
        self,
        filed_date: date | str,
        rows: Sequence[dict],
        *,
        query: Mapping,
        fetched_at: str | None = None,
        overwrite: bool = False,
    ) -> dict:
        day = _coerce_date(filed_date, field="filed_date")
        document = self.build_document(day, rows, query=query)
        document["fetched_at"] = str(fetched_at or datetime.now(tz=timezone.utc).isoformat())
        target = self.daily_path(day)
        content_sha256 = str(document["content_sha256"])

        if target.exists():
            existing = self.read_daily(day)
            if existing["content_sha256"] == content_sha256:
                parquet_path = self._ensure_parquet(day, rows)
                return self._reference(day, content_sha256, len(rows), idempotent=True, parquet_path=parquet_path)
            if not overwrite:
                raise EdgarFilingsConflictError(
                    f"edgar_filings artifact {target} already exists with content_sha256="
                    f"{existing['content_sha256']} but new content hashes to {content_sha256}; "
                    "refusing to overwrite (pass overwrite=True to replace deliberately)"
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, canonical_json_bytes(document))
        parquet_path = self._ensure_parquet(day, rows)
        return self._reference(day, content_sha256, len(rows), idempotent=False, parquet_path=parquet_path)

    def _reference(
        self,
        day: date,
        content_sha256: str,
        row_count: int,
        *,
        idempotent: bool,
        parquet_path: Path | None,
    ) -> dict:
        reference = {
            "schema_version": EDGAR_FILINGS_SCHEMA_VERSION,
            "provider": EDGAR_PROVIDER,
            "filed_date": day.isoformat(),
            "relative_path": Path(self.daily_path(day).name).as_posix(),
            "content_sha256": content_sha256,
            "row_count": row_count,
            "idempotent": idempotent,
        }
        if parquet_path is not None:
            reference["parquet_relative_path"] = parquet_path.name
        return reference

    def _ensure_parquet(self, day: date, rows: Sequence[dict]) -> Path | None:
        path = self.daily_parquet_path(day)
        if not rows:
            return path if path.exists() else None
        projected = [
            {
                "accession": row.get("accession"),
                "form_type": row.get("form_type"),
                "root_form": row.get("root_form"),
                "filed_date": row.get("filed_date"),
                "period": row.get("period"),
                "symbol": row.get("symbol"),
                "cik": row.get("cik"),
                "company_name": row.get("company_name"),
                "url": row.get("url"),
                "primary_document": row.get("primary_document"),
                "items": "|".join(row.get("items") or []),
                "provider": row.get("provider"),
                "source_reference": row.get("source_reference"),
                "fetched_at": row.get("fetched_at"),
            }
            for row in rows
        ]
        frame = pl.DataFrame(projected, schema=EDGAR_FILINGS_SCHEMA, orient="row").sort(
            ["filed_date", "form_type", "accession"]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        frame.write_parquet(temporary, compression="zstd")
        os.replace(temporary, path)
        return path

    def read_daily(self, filed_date: date | str) -> dict:
        day = _coerce_date(filed_date, field="filed_date")
        target = self.daily_path(day)
        if not target.exists():
            raise EdgarFilingsError(f"no edgar_filings artifact for {day.isoformat()} at {target}")
        try:
            document = json.loads(target.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgarFilingsParseError(f"corrupt edgar_filings artifact {target}: {exc}") from exc
        stored = str(document.get("content_sha256") or "")
        content = {key: value for key, value in document.items() if key not in {"fetched_at", "content_sha256"}}
        if not stored or stored != self.content_digest(self.identity_content(content)):
            raise EdgarFilingsParseError(
                f"edgar_filings artifact {target} failed content_sha256 integrity check"
            )
        return document

    @staticmethod
    def _atomic_write(target: Path, payload: bytes) -> None:
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class EdgarFilingEvent:
    """One filing as a read-only event-ledger record.

    ``available_time`` is derived from ``filed_date`` alone so a backfill never
    pretends the filing was observed on its historical filing date *or* the
    local sync time.  ``fetched_at`` is intentionally absent: it is provenance,
    not an availability timestamp.
    """

    symbol: str | None
    event_type: str
    filed_date: date
    period: date | None
    source_reference: str
    accession: str = ""
    form_type: str = ""
    cik: str | None = None
    company_name: str | None = None
    url: str | None = None
    schema_version: str = EDGAR_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not str(self.event_type or "").strip():
            raise ValueError("edgar filing event requires an event_type")
        if not isinstance(self.filed_date, date):
            raise ValueError("edgar filing event requires a filed_date")
        if not str(self.source_reference or "").strip():
            raise ValueError("edgar filing event requires a source_reference")

    @property
    def available_time(self) -> str:
        """End of the ``filed_date`` in America/New_York (PIT availability)."""

        return datetime.combine(self.filed_date, datetime_time.max, tzinfo=_EDGAR_TZ).isoformat()

    @property
    def event_time(self) -> str | None:
        """Start of the report ``period`` in America/New_York, when known."""

        if self.period is None:
            return None
        return datetime.combine(self.period, datetime_time.min, tzinfo=_EDGAR_TZ).isoformat()

    @property
    def revision_id(self) -> str:
        payload = json.dumps(
            {
                "accession": self.accession,
                "form_type": self.form_type,
                "event_type": self.event_type,
                "filed_date": self.filed_date.isoformat(),
                "period": self.period.isoformat() if self.period else None,
                "symbol": self.symbol,
                "source_reference": self.source_reference,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_record(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "market": "US",
            "symbol": self.symbol,
            "event_type": self.event_type,
            "form_type": self.form_type,
            "filed_date": self.filed_date.isoformat(),
            "period": self.period.isoformat() if self.period else None,
            "event_time": self.event_time,
            "available_time": self.available_time,
            "accession": self.accession,
            "cik": self.cik,
            "company_name": self.company_name,
            "url": self.url,
            "provider": EDGAR_PROVIDER,
            "source_reference": self.source_reference,
            "revision_id": self.revision_id,
        }


def build_events(rows: Sequence[Mapping]) -> list[EdgarFilingEvent]:
    """Read-only construction of event records from stored filing rows."""

    events: list[EdgarFilingEvent] = []
    for row in rows:
        filed_date = _coerce_date(row.get("filed_date"), field="filed_date")
        period_raw = row.get("period")
        period = _coerce_date(period_raw, field="period_until") if period_raw else None
        form_type = str(row.get("form_type") or "")
        events.append(
            EdgarFilingEvent(
                symbol=(str(row["symbol"]).strip().upper() or None) if row.get("symbol") else None,
                event_type=event_type_for_form(form_type),
                filed_date=filed_date,
                period=period,
                source_reference=str(row.get("source_reference") or ""),
                accession=str(row.get("accession") or ""),
                form_type=form_type,
                cik=(str(row["cik"]) if row.get("cik") else None),
                company_name=(str(row["company_name"]) if row.get("company_name") else None),
                url=(str(row["url"]) if row.get("url") else None),
            )
        )
    events.sort(key=lambda event: (event.filed_date, event.event_type, event.accession))
    return events


__all__ = [
    "EDGAR_PROVIDER",
    "EDGAR_FILINGS_SCHEMA_VERSION",
    "EDGAR_EVENT_SCHEMA_VERSION",
    "EDGAR_FILINGS_SUBDIR",
    "DAILY_FILING_FORMS",
    "EDGAR_FILINGS_SCHEMA",
    "EdgarFilingsError",
    "EdgarFilingsConfigError",
    "EdgarFilingsFetchError",
    "EdgarFilingsParseError",
    "EdgarFilingsConflictError",
    "EdgarDailyFilingsClient",
    "EdgarFilingsStore",
    "EdgarFilingEvent",
    "build_events",
    "event_type_for_form",
    "normalize_form_types",
    "canonical_json_bytes",
]
