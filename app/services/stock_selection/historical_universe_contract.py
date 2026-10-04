"""Fail-closed historical-universe evidence contract for full-market research.

The formal full-market readiness gate requires five historical-universe
dimensions to be independently verified (see
:mod:`app.services.stock_selection.data_readiness`).  Those dimensions can only
ever be produced by real, independently checkable evidence -- never inferred
from the current survivor universe.  This module provides:

* the canonical artifact schema and its default on-disk location,
* a fail-closed loader: a missing artifact yields ``None`` (the gate keeps
  behaving exactly as before), while a present-but-invalid artifact raises so a
  misconfiguration is visible instead of silently ignored,
* a pure builder that turns raw provider rows into verified/unverified
  dimensions.  A dimension is set to ``True`` only when its own evidence checks
  pass; every unverified dimension carries a machine-readable reason.

The industry dimension is driven by the SWS (申万) effective-dated classification
change log, whose numeric industry codes are resolved to names through the
CNINFO classification trees; the unresolvable share is measured as an explicit
unknown-mapping rate rather than ignored.

No network or database access happens here: fetching lives in
``scripts/build_historical_universe_contract.py`` so the decision logic stays
unit-testable and auditable.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

HISTORICAL_UNIVERSE_CONTRACT_SCHEMA_VERSION = "historical_universe_contract_v1"
HISTORICAL_UNIVERSE_CONTRACT_FILENAME = "historical_universe_contract.json"

#: The five evidence dimensions consumed by ``assess_data_readiness``.
HISTORICAL_UNIVERSE_DIMENSIONS: tuple[str, ...] = (
    "historical_security_master_verified",
    "historical_membership_verified",
    "delisting_history_verified",
    "historical_industry_verified",
    "universe_revision_history_verified",
)

_YYYYMMDD = re.compile(r"^\d{8}$")
_DATE_FORM = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: Conservative defaults for the effective-dated industry-history dimension.
#: ``min_mapping_rate`` guards against silently treating the SWS numeric
#: industry codes (e.g. ``440101``) as if every row were resolvable to a named
#: SW category; the unmatched share is reported as the unknown-mapping rate.
INDUSTRY_MIN_CODE_MAPPING_RATE = 0.80
INDUSTRY_MIN_SYMBOL_COUNT = 100
INDUSTRY_MIN_DISTINCT_YEAR_COUNT = 10
INDUSTRY_MIN_EXCHANGE_COUNT = 2


def default_historical_universe_contract_path(artifacts_dir: Path) -> Path:
    """Conventional location of the contract artifact under the artifact root."""
    return Path(artifacts_dir) / "stock_selection_research" / HISTORICAL_UNIVERSE_CONTRACT_FILENAME


def resolve_historical_universe_contract_path(
    *,
    artifacts_dir: Path,
    explicit_path: Path | str | None = None,
    settings_path: Path | str | None = None,
) -> Path:
    """Resolve the contract path: explicit argument > configured setting > default."""
    for candidate in (explicit_path, settings_path):
        if candidate is not None and str(candidate).strip():
            return Path(candidate)
    return default_historical_universe_contract_path(artifacts_dir)


def load_historical_universe_contract(
    path: Path | str,
    *,
    market: str,
) -> dict[str, bool] | None:
    """Load and normalise a contract artifact.

    Returns ``None`` when the file does not exist so callers stay fail-closed
    with unchanged behaviour.  A file that exists must be a well-formed
    contract for the requested market: any other state raises ``ValueError``
    rather than being silently downgraded or, worse, treated as verified.
    Missing dimension keys resolve to ``False``.
    """
    contract_path = Path(path)
    if not contract_path.exists():
        return None
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"historical universe contract at {contract_path} is not readable JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"historical universe contract at {contract_path} must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != HISTORICAL_UNIVERSE_CONTRACT_SCHEMA_VERSION:
        raise ValueError(
            f"historical universe contract at {contract_path} has unsupported "
            f"schema_version={schema_version!r}"
        )
    contract_market = str(payload.get("market") or "").strip().upper()
    requested_market = str(market or "").strip().upper()
    if contract_market != requested_market:
        raise ValueError(
            f"historical universe contract at {contract_path} targets market "
            f"{contract_market!r}, not {requested_market!r}"
        )
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError(f"historical universe contract at {contract_path} is missing dimensions")
    return {name: dimensions.get(name) is True for name in HISTORICAL_UNIVERSE_DIMENSIONS}


def load_historical_universe_contract_for_market(
    *,
    market: str,
    artifacts_dir: Path,
    explicit_path: Path | str | None = None,
    settings_path: Path | str | None = None,
) -> dict[str, bool] | None:
    """Resolve the conventional path and load it; ``None`` when absent."""
    path = resolve_historical_universe_contract_path(
        artifacts_dir=artifacts_dir,
        explicit_path=explicit_path,
        settings_path=settings_path,
    )
    return load_historical_universe_contract(path, market=market)


def _to_app_ticker(ts_code: str) -> str:
    upper = str(ts_code or "").strip().upper()
    if upper.endswith(".SH"):
        return f"{upper[:-3]}.SS"
    return upper


def _valid_date(value: object) -> bool:
    text = str(value or "").strip()
    if _YYYYMMDD.match(text):
        return True
    match = _DATE_FORM.match(text)
    if not match:
        return False
    try:
        datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    return True


def _rows_codes(rows: Sequence[Mapping[str, object]]) -> list[str]:
    return [
        str(row.get("ts_code") or "").strip().upper()
        for row in rows
        if str(row.get("ts_code") or "").strip()
    ]


def _verify_security_master(
    *,
    listed: Sequence[Mapping[str, object]],
    paused: Sequence[Mapping[str, object]],
    delisted: Sequence[Mapping[str, object]],
    name_changes: Sequence[Mapping[str, object]],
    endpoint_errors: Mapping[str, str],
) -> tuple[bool, dict[str, object], str | None]:
    active = list(listed) + list(paused)
    active_codes = _rows_codes(active)
    all_codes = active_codes + _rows_codes(delisted)
    duplicate_codes = len(all_codes) - len(set(all_codes))
    missing_listing_date = sum(1 for row in active if not _valid_date(row.get("list_date")))
    namechange_error = str(endpoint_errors.get("namechange") or "").strip() or None

    changes_by_code: dict[str, list[Mapping[str, object]]] = {}
    for row in name_changes:
        code = str(row.get("ts_code") or "").strip().upper()
        if code:
            changes_by_code.setdefault(code, []).append(row)
    malformed_change_dates = 0
    overlapping_change_windows = 0
    current_name_mismatch = 0
    active_name_by_code = {
        str(row.get("ts_code") or "").strip().upper(): str(row.get("name") or "").strip()
        for row in active
    }
    for code, rows in changes_by_code.items():
        parsed: list[tuple[str, str | None, str]] = []
        for row in rows:
            start = str(row.get("start_date") or "").strip()
            end = str(row.get("end_date") or "").strip() or None
            if not _valid_date(start) or (end is not None and not _valid_date(end)):
                malformed_change_dates += 1
                continue
            parsed.append((start, end, str(row.get("name") or "").strip()))
        parsed.sort(key=lambda item: item[0])
        previous_end: str | None = None
        for start, end, _name in parsed:
            if previous_end is not None and start < previous_end:
                overlapping_change_windows += 1
            previous_end = end or previous_end
        if parsed:
            latest_name = parsed[-1][2]
            active_name = active_name_by_code.get(code)
            if active_name and latest_name and active_name != latest_name:
                current_name_mismatch += 1

    evidence: dict[str, object] = {
        "active_symbol_count": len(active_codes),
        "delisted_symbol_count": len(_rows_codes(delisted)),
        "duplicate_ts_code_count": duplicate_codes,
        "active_missing_listing_date_count": missing_listing_date,
        "namechange_code_count": len(changes_by_code),
        "namechange_malformed_date_count": malformed_change_dates,
        "namechange_overlapping_window_count": overlapping_change_windows,
        "active_name_mismatch_count": current_name_mismatch,
        "namechange_endpoint_unavailable": namechange_error is not None,
    }
    if not active_codes:
        detail = "; ".join(
            f"{key}: {value}"
            for key, value in endpoint_errors.items()
            if key in {"stock_basic_listed", "stock_basic_paused"}
        )
        reason = "no active listing rows returned by provider"
        if detail:
            reason = f"{reason} ({detail})"
        return False, evidence, reason
    if namechange_error is not None:
        return False, evidence, f"namechange endpoint unavailable: {namechange_error}"
    if duplicate_codes:
        return False, evidence, "provider returned duplicate ts_code rows across status lists"
    if missing_listing_date:
        return False, evidence, "active rows are missing a valid list_date"
    if malformed_change_dates:
        return False, evidence, "namechange rows contain invalid effective dates"
    if overlapping_change_windows:
        return False, evidence, "namechange intervals overlap within a ts_code"
    if current_name_mismatch:
        return False, evidence, "provider current name disagrees with latest namechange record"
    if not changes_by_code:
        return False, evidence, "namechange endpoint returned no effective-dated history"
    return True, evidence, None


def _verify_delisting(
    *,
    delisted: Sequence[Mapping[str, object]],
    listed: Sequence[Mapping[str, object]],
    paused: Sequence[Mapping[str, object]],
    cross_check: Mapping[str, object] | None,
    endpoint_errors: Mapping[str, str],
) -> tuple[bool, dict[str, object], str | None]:
    delisted_codes = _rows_codes(delisted)
    active_codes = set(_rows_codes(listed) + _rows_codes(paused))
    missing_delist_date = sum(1 for row in delisted if not _valid_date(row.get("delist_date")))
    provider_status_overlap = sum(1 for code in delisted_codes if code in active_codes)
    active_tickers: set[str] = set()
    db_overlap: int | None = None
    if cross_check is not None and "active_tickers" in cross_check:
        active_tickers = {str(item).strip().upper() for item in cross_check.get("active_tickers") or ()}
        db_overlap = sum(1 for code in delisted_codes if _to_app_ticker(code) in active_tickers)
    db_overlap_symbols = sorted(
        {_to_app_ticker(code) for code in delisted_codes if _to_app_ticker(code) in active_tickers}
    ) if db_overlap is not None else []
    source_counts: dict[str, int] = {}
    for row in delisted:
        key = str(row.get("source") or "unknown")
        source_counts[key] = source_counts.get(key, 0) + 1

    evidence: dict[str, object] = {
        "delisted_symbol_count": len(delisted_codes),
        "missing_delist_date_count": missing_delist_date,
        "provider_status_overlap_count": provider_status_overlap,
        "db_active_overlap_count": db_overlap,
        "db_cross_checked": db_overlap is not None,
        "db_active_overlap_symbols": db_overlap_symbols,
        "delisted_source_counts": source_counts,
    }
    if not delisted_codes:
        detail = "; ".join(f"{key}: {value}" for key, value in endpoint_errors.items())
        reason = "no delisted symbols from any source"
        if detail:
            reason = f"{reason} ({detail})"
        return False, evidence, reason
    if missing_delist_date:
        return False, evidence, "delisted rows are missing a valid delist_date"
    if provider_status_overlap:
        return False, evidence, "provider returned the same symbol as both active and delisted"
    # The local store is a current-survivor snapshot; a delisted symbol still
    # marked active there is a store-staleness defect (recorded in evidence and
    # surfaced as a warning), not a refutation of the authoritative delisting
    # history itself.
    return True, evidence, None


def _date_key(value: object) -> str | None:
    """Normalise a validated date to ``YYYYMMDD`` for ordering/comparison."""
    text = str(value or "").strip()
    if _YYYYMMDD.match(text):
        return text
    match = _DATE_FORM.match(text)
    if not match:
        return None
    return "".join(match.groups())


def _exchange_suffix(ts_code: str) -> str:
    upper = str(ts_code or "").strip().upper()
    if "." in upper:
        return upper.rsplit(".", 1)[1]
    return ""


def _verify_industry(
    *,
    industry_rows: Sequence[Mapping[str, object]],
    industry_code_names: Mapping[str, str] | None,
    min_mapping_rate: float,
    min_symbol_count: int,
    min_distinct_year_count: int,
    min_exchange_count: int,
    endpoint_errors: Mapping[str, str],
) -> tuple[bool, dict[str, object], str | None]:
    """Verify effective-dated industry history (SWS 申万 classification change log).

    The upstream file keys rows by the SWS *numeric* industry code (e.g.
    ``440101``), which is a different scheme from the ``850xxx.SI`` index codes
    used by the SW index endpoints.  Those numeric codes are resolved to names
    through the CNINFO classification trees (current + legacy).  Because part of
    the historical code space is not resolvable, the unresolvable share is
    measured as an explicit *unknown mapping rate* and gated by
    ``min_mapping_rate`` rather than being silently ignored or treated as a
    blanket pass/fail.
    """
    names = {
        str(code).strip(): str(name).strip()
        for code, name in (industry_code_names or {}).items()
        if str(code).strip() and str(name).strip()
    }
    total = len(industry_rows)
    valid_date_count = 0
    missing_industry_code = 0
    unmappable_ts_code = 0
    mapped_rows = 0
    unknown_code_rows: set[str] = set()
    symbols: set[str] = set()
    exchange_counts: dict[str, int] = {}
    years: set[str] = set()
    per_symbol_dates: dict[str, list[str]] = {}
    duplicate_effective_date_symbols = 0
    out_of_order_symbols = 0
    min_date: str | None = None
    max_date: str | None = None
    samples: list[dict[str, object]] = []

    for row in industry_rows:
        ts_code = str(row.get("ts_code") or "").strip().upper()
        industry_code = str(row.get("industry_code") or "").strip()
        date_key = _date_key(row.get("effective_date"))
        if date_key is not None:
            valid_date_count += 1
            years.add(date_key[:4])
            if min_date is None or date_key < min_date:
                min_date = date_key
            if max_date is None or date_key > max_date:
                max_date = date_key
        if not industry_code:
            missing_industry_code += 1
        if not ts_code:
            unmappable_ts_code += 1
        else:
            symbols.add(ts_code)
            suffix = _exchange_suffix(ts_code)
            exchange_counts[suffix] = exchange_counts.get(suffix, 0) + 1
        if industry_code and industry_code in names:
            mapped_rows += 1
        elif industry_code:
            unknown_code_rows.add(industry_code)
        if date_key is not None and ts_code:
            per_symbol_dates.setdefault(ts_code, []).append(date_key)
        if len(samples) < 5:
            samples.append(
                {
                    "ts_code": ts_code,
                    "effective_date": date_key,
                    "industry_code": industry_code,
                    "industry_name": names.get(industry_code) if industry_code else None,
                }
            )

    for dates in per_symbol_dates.values():
        if len(set(dates)) != len(dates):
            duplicate_effective_date_symbols += 1
        ordered = sorted(dates)
        if dates != ordered:
            out_of_order_symbols += 1

    mapping_rate = (mapped_rows / total) if total else 0.0
    unknown_mapping_rate = (1.0 - mapping_rate) if total else 1.0
    evidence: dict[str, object] = {
        "row_count": total,
        "symbol_count": len(symbols),
        "valid_effective_date_count": valid_date_count,
        "missing_effective_date_count": total - valid_date_count,
        "missing_industry_code_count": missing_industry_code,
        "unmappable_ts_code_count": unmappable_ts_code,
        "industry_code_mapped_row_count": mapped_rows,
        "industry_code_mapping_rate": round(mapping_rate, 6),
        "unknown_industry_code_mapping_rate": round(unknown_mapping_rate, 6),
        "unknown_industry_code_count": len(unknown_code_rows),
        "unknown_industry_code_sample": sorted(unknown_code_rows)[:20],
        "min_industry_code_mapping_rate": min_mapping_rate,
        "distinct_year_count": len(years),
        "min_effective_date": min_date,
        "max_effective_date": max_date,
        "exchange_counts": exchange_counts,
        "duplicate_effective_date_symbol_count": duplicate_effective_date_symbols,
        "out_of_order_symbol_count": out_of_order_symbols,
        "sample_rows": samples,
    }

    if not industry_rows:
        detail = "; ".join(
            f"{key}: {value}"
            for key, value in endpoint_errors.items()
            if "industry" in str(key).lower() or "sws" in str(key).lower()
        )
        reason = "no effective-dated industry history rows returned by provider"
        if detail:
            reason = f"{reason} ({detail})"
        return False, evidence, reason
    if valid_date_count < total:
        return False, evidence, "industry history rows are missing a valid effective date"
    if missing_industry_code:
        return False, evidence, "industry history rows are missing an industry_code"
    if unmappable_ts_code:
        return False, evidence, "industry history rows contain unmappable security codes"
    if duplicate_effective_date_symbols:
        return False, evidence, "industry history has duplicate effective dates within a symbol"
    if out_of_order_symbols:
        return False, evidence, "industry history effective dates are not monotonic within a symbol"
    if len(symbols) < min_symbol_count:
        return False, evidence, (
            f"industry history covers only {len(symbols)} symbols "
            f"(minimum {min_symbol_count})"
        )
    if len(years) < min_distinct_year_count:
        return False, evidence, (
            f"industry history spans only {len(years)} distinct years "
            f"(minimum {min_distinct_year_count})"
        )
    if len(exchange_counts) < min_exchange_count:
        return False, evidence, (
            f"industry history covers only {len(exchange_counts)} exchanges "
            f"(minimum {min_exchange_count})"
        )
    if mapping_rate < min_mapping_rate:
        return False, evidence, (
            f"unknown industry-code mapping rate {unknown_mapping_rate:.3f} exceeds "
            f"allowed {1.0 - min_mapping_rate:.3f} "
            f"(mapped {mapped_rows}/{total}, {len(unknown_code_rows)} unknown codes)"
        )
    return True, evidence, None


def build_historical_universe_contract(
    *,
    market: str,
    listed: Sequence[Mapping[str, object]] = (),
    paused: Sequence[Mapping[str, object]] = (),
    delisted: Sequence[Mapping[str, object]] = (),
    name_changes: Sequence[Mapping[str, object]] = (),
    industry_rows: Sequence[Mapping[str, object]] = (),
    industry_code_names: Mapping[str, str] | None = None,
    industry_min_mapping_rate: float = INDUSTRY_MIN_CODE_MAPPING_RATE,
    industry_min_symbol_count: int = INDUSTRY_MIN_SYMBOL_COUNT,
    industry_min_distinct_year_count: int = INDUSTRY_MIN_DISTINCT_YEAR_COUNT,
    industry_min_exchange_count: int = INDUSTRY_MIN_EXCHANGE_COUNT,
    endpoint_errors: Mapping[str, str] | None = None,
    cross_check: Mapping[str, object] | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Assemble a contract artifact from real provider rows.

    Only the dimensions whose evidence checks pass are marked ``True``; the
    remaining dimensions are ``False`` with an explicit reason.  The membership
    and revision dimensions have no real point-in-time source in this repository
    and therefore stay ``False`` unless a future implementation supplies genuine
    evidence through this same builder.  The industry dimension is driven by an
    effective-dated SWS classification history log (``industry_rows``) plus a
    numeric-code -> name mapping (``industry_code_names``); it stays ``False``
    whenever coverage, mapping-rate, or date-integrity checks fail.
    """
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    errors = {str(key): str(value) for key, value in (endpoint_errors or {}).items()}

    master_verified, master_evidence, master_reason = _verify_security_master(
        listed=listed,
        paused=paused,
        delisted=delisted,
        name_changes=name_changes,
        endpoint_errors=errors,
    )
    delisting_verified, delisting_evidence, delisting_reason = _verify_delisting(
        delisted=delisted,
        listed=listed,
        paused=paused,
        cross_check=cross_check,
        endpoint_errors=errors,
    )
    industry_verified, industry_evidence, industry_reason = _verify_industry(
        industry_rows=industry_rows,
        industry_code_names=industry_code_names,
        min_mapping_rate=industry_min_mapping_rate,
        min_symbol_count=industry_min_symbol_count,
        min_distinct_year_count=industry_min_distinct_year_count,
        min_exchange_count=industry_min_exchange_count,
        endpoint_errors=errors,
    )

    # No point-in-time membership or revision-history source exists in this
    # repository yet.  These stay fail-closed on purpose.
    membership_reason = (
        "no point-in-time index membership source: provider lacks "
        "index_member/index_member_all/index_weight permission and no membership "
        "snapshot store exists"
    )
    revision_reason = (
        "no universe revision history source: delist/membership lists are "
        "current snapshots only and the local store cannot replay historical "
        "universe vintages"
    )

    dimensions: dict[str, bool] = {
        "historical_security_master_verified": master_verified,
        "historical_membership_verified": False,
        "delisting_history_verified": delisting_verified,
        "historical_industry_verified": industry_verified,
        "universe_revision_history_verified": False,
    }
    missing_reasons: dict[str, str] = {}
    for name in HISTORICAL_UNIVERSE_DIMENSIONS:
        if not dimensions[name]:
            missing_reasons[name] = {
                "historical_security_master_verified": master_reason,
                "delisting_history_verified": delisting_reason,
                "historical_membership_verified": membership_reason,
                "historical_industry_verified": industry_reason,
                "universe_revision_history_verified": revision_reason,
            }[name] or "evidence checks did not pass"

    warnings: list[str] = []
    overlap_symbols = delisting_evidence.get("db_active_overlap_symbols") or []
    if overlap_symbols:
        warnings.append(
            "store_staleness: delisted symbols still marked active in local store: "
            + ", ".join(str(item) for item in overlap_symbols)
        )
    unknown_mapping_rate = industry_evidence.get("unknown_industry_code_mapping_rate")
    if industry_verified and isinstance(unknown_mapping_rate, (int, float)) and unknown_mapping_rate > 0:
        warnings.append(
            "industry_code_unknown_mapping: "
            f"{float(unknown_mapping_rate):.4f} of industry-history rows use SWS numeric "
            "codes that are not resolvable to a named SW category"
        )

    return {
        "schema_version": HISTORICAL_UNIVERSE_CONTRACT_SCHEMA_VERSION,
        "market": market_code,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "tushare+exchange_delisting+sws_industry" if market_code == "CN" else "unsupported_market",
        "dimensions": dimensions,
        "missing_reasons": missing_reasons,
        "warnings": warnings,
        "evidence": {
            "historical_security_master": master_evidence,
            "delisting_history": delisting_evidence,
            "historical_industry": industry_evidence,
        },
        "provider_endpoint_errors": errors,
    }


__all__ = [
    "HISTORICAL_UNIVERSE_CONTRACT_FILENAME",
    "HISTORICAL_UNIVERSE_CONTRACT_SCHEMA_VERSION",
    "HISTORICAL_UNIVERSE_DIMENSIONS",
    "INDUSTRY_MIN_CODE_MAPPING_RATE",
    "INDUSTRY_MIN_DISTINCT_YEAR_COUNT",
    "INDUSTRY_MIN_EXCHANGE_COUNT",
    "INDUSTRY_MIN_SYMBOL_COUNT",
    "build_historical_universe_contract",
    "default_historical_universe_contract_path",
    "load_historical_universe_contract",
    "load_historical_universe_contract_for_market",
    "resolve_historical_universe_contract_path",
]
