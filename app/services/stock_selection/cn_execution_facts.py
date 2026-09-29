"""Bounded BaoStock research facts, kept separate from canonical market prices.

No daily price limits or complete corporate-action coverage are inferred here.
Historical facts are outcome/replay inputs, not point-in-time trading features.
"""
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re


FIELDS = "date,code,open,high,low,close,preclose,volume,adjustflag,tradestatus,isST"
SCHEMA = "cn_baostock_execution_facts_v1"
PRICE_FIELDS = ("open", "high", "low", "close", "volume")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def validate_request(tickers: list[str], start_date: str, end_date: str):
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start.isoformat() != start_date or end.isoformat() != end_date:
        raise ValueError("canonical ISO dates required")
    if not 0 <= (end - start).days <= 370:
        raise ValueError("research probe window must be 0..370 calendar days")
    if not 1 <= len(tickers) <= 20 or len(set(tickers)) != len(tickers):
        raise ValueError("research probe requires 1..20 unique CN stocks")
    for ticker in tickers:
        if not isinstance(ticker, str) or not re.fullmatch(
            r"(?:(?:600|601|603|605|688)\d{3}\.SS|(?:000|001|002|003|300|301)\d{3}\.SZ)", ticker
        ):
            raise ValueError("canonical Shanghai/Shenzhen stock required; BJ/US/HK excluded")


def _provider_code(ticker):
    return ("sh." if ticker.endswith(".SS") else "sz.") + ticker[:6]


def _flag(value):
    return {"0": False, "1": True}.get(value) if isinstance(value, str) else None


def normalize_response(response: dict, *, start_date: str, end_date: str) -> list[dict]:
    ticker = response["ticker"]
    validate_request([ticker], start_date, end_date)
    fields = response["fields"]
    if len(set(fields)) != len(fields) or set(FIELDS.split(",")) != set(fields):
        raise ValueError("unexpected, missing or duplicate provider fields")
    if len(response["rows"]) > 371:
        raise ValueError("provider exceeded bounded daily row count")
    reference = "baostock:query_history_k_data_plus:" + digest(response)
    result, seen = [], set()
    for values in response["rows"]:
        if len(values) != len(fields):
            raise ValueError("provider row width mismatch")
        raw = dict(zip(fields, values))
        day = date.fromisoformat(raw["date"]).isoformat()
        if day != raw["date"] or not start_date <= day <= end_date or day in seen:
            raise ValueError("out-of-window or duplicate provider date")
        if raw["code"] != _provider_code(ticker) or raw["adjustflag"] != "3":
            raise ValueError("provider returned wrong security or adjusted price basis")
        numbers = {}
        for field in (*PRICE_FIELDS, "preclose"):
            value = raw[field]
            if isinstance(value, bool):
                raise ValueError("boolean price/volume is invalid")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("nonfinite or negative price/volume")
            numbers[field] = number
        if numbers["high"] < max(numbers["open"], numbers["close"], numbers["low"]) or numbers["low"] > min(numbers["open"], numbers["close"]):
            raise ValueError("inconsistent OHLC")
        trading = _flag(raw["tradestatus"])
        result.append({"date": day, "symbol": ticker, **numbers,
                       "price_basis": "raw", "execution_source_reference": reference,
                       "suspended": None if trading is None else not trading,
                       "is_st": _flag(raw["isST"]),
                       "upper_limit": None, "lower_limit": None,
                       "corporate_action_status": None})
        seen.add(day)
    return sorted(result, key=lambda row: row["date"])


def collect_baostock_facts(*, tickers: list[str], start_date: str, end_date: str, sdk=None, progress=None) -> dict:
    """Call only from a bounded CLI worker: BaoStock uses a global socket/session."""
    validate_request(tickers, start_date, end_date)
    if sdk is None:
        import baostock as sdk
    progress = progress or (lambda stage: None)
    payload = {"schema_version": SCHEMA, "market": "CN", "provider": "baostock",
               "collected_at": datetime.now(timezone.utc).isoformat(),
               "sdk_version": importlib.metadata.version("baostock"),
               "request": {"tickers": tickers, "start_date": start_date, "end_date": end_date,
                           "fields": FIELDS, "frequency": "d", "adjustflag": "3"},
               "queries": [], "records": [], "stages": [], "model_backtest_status": "NOT_RUN"}
    callback = progress

    def progress(stage):
        payload["stages"].append(stage)
        callback(stage)

    try:
        progress("login_started")
        login = sdk.login()
        if str(getattr(login, "error_code", "missing")) != "0":
            raise RuntimeError("login_failed")
        progress("login_succeeded")
        for ticker in tickers:
            query = {"ticker": ticker, "status": "ERROR", "response": None}
            try:
                progress("query_started")
                rs = sdk.query_history_k_data_plus(_provider_code(ticker), FIELDS,
                    start_date=start_date, end_date=end_date, frequency="d", adjustflag="3")
                if str(getattr(rs, "error_code", "missing")) != "0":
                    raise RuntimeError("query_failed")
                rows = []
                while rs.next():
                    if str(rs.error_code) != "0":
                        raise RuntimeError("pagination_failed")
                    rows.append(rs.get_row_data())
                    if len(rows) > 371:
                        raise ValueError("provider exceeded bounded daily row count")
                if str(rs.error_code) != "0":
                    raise RuntimeError("pagination_failed")
                response = {"ticker": ticker, "fields": list(rs.fields), "rows": rows}
                query["response"] = response
                records = normalize_response(response, start_date=start_date, end_date=end_date)
                payload["records"].extend(records)
                query["status"] = "SUCCESS" if records else "EMPTY"
                progress("query_finished")
            except Exception as exc:
                # Store the type, never arbitrary SDK exception text or credentials.
                query["error_type"] = type(exc).__name__
            payload["queries"].append(query)
    except Exception as exc:
        payload["error_type"] = type(exc).__name__
    finally:
        try:
            progress("logout_started")
            sdk.logout()
            progress("logout_finished")
        except Exception:
            pass
    payload["status"] = ("SUCCESS" if len(payload["queries"]) == len(tickers)
                         and all(q["status"] == "SUCCESS" for q in payload["queries"])
                         else "PARTIAL" if payload["records"] else "UNAVAILABLE")
    return {"payload": payload, "sha256": digest(payload)}


def load_facts_bundle(path: Path) -> tuple[list[dict], str]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    if envelope.get("sha256") != digest(payload):
        raise ValueError("facts bundle hash mismatch")
    if payload.get("schema_version") != SCHEMA or payload.get("market") != "CN" or payload.get("provider") != "baostock":
        raise ValueError("unsupported facts schema/market/provider")
    request = payload["request"]
    validate_request(request["tickers"], request["start_date"], request["end_date"])
    if request.get("adjustflag") != "3" or request.get("frequency") != "d" or request.get("fields") != FIELDS:
        raise ValueError("unsupported provider request")
    collected = datetime.fromisoformat(payload["collected_at"])
    if collected.tzinfo is None or not str(payload.get("sdk_version") or "").strip():
        raise ValueError("timezone-aware collection time and SDK version required")
    expected, seen = [], set()
    for query in payload["queries"]:
        ticker = query["ticker"]
        if ticker not in request["tickers"] or ticker in seen:
            raise ValueError("unexpected or duplicate query ticker")
        seen.add(ticker)
        if query["status"] not in {"SUCCESS", "EMPTY", "ERROR"}:
            raise ValueError("unexpected query status")
        if query["status"] in {"SUCCESS", "EMPTY"}:
            if query["response"]["ticker"] != ticker:
                raise ValueError("query and response security mismatch")
            records = normalize_response(query["response"], start_date=request["start_date"], end_date=request["end_date"])
            if bool(records) != (query["status"] == "SUCCESS"):
                raise ValueError("query status contradicts returned rows")
            expected.extend(records)
    if payload["records"] != expected:
        raise ValueError("facts do not reproduce from source response")
    expected_status = ("SUCCESS" if len(seen) == len(request["tickers"])
                       and all(q["status"] == "SUCCESS" for q in payload["queries"])
                       else "PARTIAL" if expected else "UNAVAILABLE")
    if payload["status"] != expected_status:
        raise ValueError("bundle status contradicts query coverage")
    return expected, envelope["sha256"]


def attach_matching_facts(rows: list[dict], facts: list[dict]) -> tuple[list[dict], dict]:
    """Require OHLCV agreement before enriching a lake row; never replace prices."""
    indexed = {}
    for fact in facts:
        key = (fact["symbol"], fact["date"])
        if key in indexed:
            raise ValueError("duplicate daily execution facts")
        indexed[key] = fact
    counts, output, conflicts = Counter(), [], []
    for row in rows:
        merged = dict(row)
        fact = indexed.get((row["symbol"], str(row["date"])))
        if fact is None:
            counts["missing_fact_rows"] += 1
        else:
            try:
                matches = all(not isinstance(row.get(key), bool) and
                    math.isfinite(float(row[key])) and math.isclose(float(row[key]), fact[key], rel_tol=1e-8, abs_tol=1e-8)
                    for key in PRICE_FIELDS)
            except (ValueError, TypeError, KeyError):
                matches = False
            if matches:
                for key in ("price_basis", "execution_source_reference", "suspended", "is_st"):
                    # Known, contradictory preexisting facts require explicit review.
                    if row.get(key) is not None and key != "execution_source_reference" and row[key] != fact[key]:
                        matches = False
                        break
            if matches:
                for key in ("price_basis", "execution_source_reference", "suspended", "is_st"):
                    merged[key] = fact[key]
                counts["matched_rows"] += 1
            else:
                counts["conflicting_rows"] += 1
                conflicts.append({"symbol": row["symbol"], "date": str(row["date"])})
                # A conflicting raw quote must not retain a claim of certified basis.
                merged["price_basis"] = None
                merged["execution_source_reference"] = None
        output.append(merged)
    return output, {"policy": "same_symbol_date_and_ohlcv_v1", "counts": dict(counts), "conflicts": conflicts}
