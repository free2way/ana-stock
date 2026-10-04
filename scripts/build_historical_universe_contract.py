"""Build a historical-universe evidence contract artifact (fail-closed).

This validator only marks a dimension ``True`` when it has real, independently
checkable evidence.  With the current provider permissions and local stores:

* ``delisting_history_verified``   -- TuShare ``stock_basic(list_status='D')``
  delist dates, merged with the real SSE/SZSE delisting lists published by
  AKShare (independent exchange evidence) and cross-checked against the
  in-store active universe.  If TuShare's quota is exhausted the exchange
  lists alone still carry the dimension.
* ``historical_security_master_verified`` -- TuShare ``stock_basic`` (L/P/D)
  plus ``namechange`` effective-dated names, consistency-checked.
* ``historical_industry_verified`` -- the SWS (申万宏源) effective-dated
  classification change log (all-market, free) with its numeric industry codes
  resolved to names via the CNINFO 申万 classification trees (current + legacy).
  The unresolvable share is reported as an explicit unknown-mapping rate and
  gated by a threshold; the SWS XLS download is direct-first with a recorded
  ``verify=False`` fallback if the local MITM proxy rejects the certificate.
* ``historical_membership_verified``, ``universe_revision_history_verified``
  -- left ``False``: no point-in-time source exists.  Provider permission
  failures are recorded verbatim in the artifact instead of being backfilled
  with an optimistic default.

The output feeds
:func:`app.services.stock_selection.production_research.audit_market_research_readiness`
via the conventional path
``<artifacts>/stock_selection_research/historical_universe_contract.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping

from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.services.stock_selection.historical_universe_contract import (  # noqa: E402
    build_historical_universe_contract,
    resolve_historical_universe_contract_path,
)

STOCK_BASIC_FIELDS = "ts_code,symbol,name,area,industry,market,exchange,list_date,delist_date"
_RATE_LIMIT_MARKERS = ("频率超限", "频次", "超过访问频次", "每分钟", "每小时", "每天最多")

#: SWS (申万宏源) effective-dated classification change log (all-market, free).
SWS_INDUSTRY_XLS_URL = (
    "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
)
#: CNINFO 申银万国 classification trees: current (008003) + legacy (008018).
CNINFO_SW_IND_TYPES: dict[str, str] = {"current": "008003", "legacy": "008018"}
_SWS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/vnd.ms-excel,application/octet-stream,*/*",
}


def _rate_limited(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _RATE_LIMIT_MARKERS)


def _fetch_records(
    label: str,
    call: Callable[[], object],
    *,
    retries: int,
    retry_sleep_seconds: float,
    errors: dict[str, str],
) -> list[dict]:
    """Call a provider endpoint, translating failures into recorded errors."""
    for attempt in range(1, max(retries, 1) + 1):
        try:
            frame = call()
        except Exception as exc:  # noqa: BLE001 - provider errors are recorded, not raised
            if _rate_limited(exc) and attempt < retries:
                print(
                    f"[retry] {label} rate limited (attempt {attempt}/{retries}); "
                    f"sleeping {retry_sleep_seconds:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(retry_sleep_seconds)
                continue
            errors[label] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return []
        if frame is None:
            errors[label] = "endpoint returned None"
            return []
        try:
            return json.loads(frame.to_json(orient="records"))
        except Exception as exc:  # noqa: BLE001
            errors[label] = f"unserializable response: {type(exc).__name__}: {str(exc)[:200]}"
            return []
    return []


def _load_db_active_tickers(market: str) -> list[str] | None:
    try:
        with SessionLocal() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            rows = db.execute(
                text(
                    "SELECT DISTINCT upper(ticker) FROM symbols "
                    "WHERE upper(market) = :market AND is_active = 1"
                ),
                {"market": market.upper()},
            ).scalars().all()
            db.rollback()
    except Exception as exc:  # noqa: BLE001 - cross-check is optional evidence
        print(f"[warn] DB cross-check unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    return [str(item) for item in rows if str(item).strip()]


def _to_ts_code(code: object) -> str:
    digits = str(code or "").strip().split(".")[0].zfill(6)
    if not digits.isdigit() or len(digits) != 6:
        return ""
    if digits.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8", "92")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _norm_date(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    text = text.split("T", 1)[0].split(" ", 1)[0].replace("-", "").replace("/", "")
    return text if text.isdigit() and len(text) == 8 else ""


def _fetch_akshare_delistings(errors: dict[str, str]) -> list[dict]:
    """Real exchange delisting lists (SSE company_status=3 / SZSE 终止上市)."""
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        errors["akshare_delisting"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return []
    rows: list[dict] = []
    for label, source, fetch in (
        ("akshare_sse_delisted", "akshare_sse", lambda: ak.stock_info_sh_delist(symbol="全部")),
        (
            "akshare_szse_delisted",
            "akshare_szse",
            lambda: ak.stock_info_sz_delist(symbol="终止上市公司"),
        ),
    ):
        records: list[dict] = []
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                frame = fetch()
                records = (
                    []
                    if frame is None
                    else json.loads(frame.to_json(orient="records", date_format="iso"))
                )
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    time.sleep(2.0)
        if last_error is not None:
            errors[label] = f"{type(last_error).__name__}: {str(last_error)[:200]}"
            continue
        for record in records:
            code = record.get("公司代码") or record.get("证券代码")
            ts_code = _to_ts_code(code)
            delist_date = _norm_date(record.get("暂停上市日期") or record.get("终止上市日期"))
            if not ts_code or not delist_date:
                continue
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": record.get("公司简称") or record.get("证券简称"),
                    "list_date": _norm_date(record.get("上市日期")),
                    "delist_date": delist_date,
                    "source": source,
                }
            )
    return rows


def _download_sw_industry_bytes(
    *,
    errors: dict[str, str],
    caveats: list[str],
    label: str,
    timeout: float = 60.0,
) -> bytes | None:
    """Download the SWS XLS with a direct-first, proxy-aware TLS fallback.

    The local environment routes HTTPS through a MITM proxy on 127.0.0.1:7890
    whose CA is not trusted by ``requests``; a direct connection is attempted
    first, then the system proxy with verification, and only as a last resort the
    proxy with ``verify=False`` (recorded as an explicit caveat).  Failures are
    recorded in ``errors`` instead of silently returning empty data.
    """
    import requests  # local import: only needed when fetching over the network

    strategies: tuple[tuple[str, dict[str, object], str | None], ...] = (
        ("direct", {"proxies": {"http": None, "https": None}, "verify": True}, None),
        ("proxy", {}, None),
        ("proxy_insecure", {"verify": False}, "sws_xls_tls_verification_disabled"),
    )
    last_error = "unknown"
    for name, options, caveat in strategies:
        for _attempt in range(1, 3):
            try:
                with requests.Session() as session:
                    session.headers.update(_SWS_HEADERS)
                    response = session.get(
                        SWS_INDUSTRY_XLS_URL, timeout=timeout, **options
                    )
                if response.status_code == 200 and response.content:
                    if caveat is not None:
                        caveats.append(caveat)
                    return response.content
                last_error = f"{name} HTTP {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                last_error = f"{name} {type(exc).__name__}: {str(exc)[:200]}"
            time.sleep(1.5)
    errors[label] = last_error
    return None


def _normalise_sw_industry_rows(frame: object) -> list[dict]:
    """Normalise the SWS XLS frame to ``ts_code/effective_date/industry_code``."""
    rows: list[dict] = []
    records = frame.to_dict("records") if frame is not None else []
    for record in records:
        rows.append(
            {
                "ts_code": _to_ts_code(record.get("股票代码")),
                "effective_date": _norm_date(record.get("计入日期")),
                "industry_code": str(record.get("行业代码") or "").strip(),
                "update_date": _norm_date(record.get("更新日期")),
                "source": "sws_industry_hist",
            }
        )
    return rows


def _fetch_sw_industry_rows(errors: dict[str, str], caveats: list[str]) -> list[dict]:
    """Fetch + normalise the all-market SWS effective-dated industry history."""
    content = _download_sw_industry_bytes(
        errors=errors, caveats=caveats, label="sws_industry_hist"
    )
    if content is None:
        return []
    try:
        import io

        import pandas as pd  # type: ignore

        frame = pd.read_excel(
            io.BytesIO(content), dtype={"股票代码": "str", "行业代码": "str"}
        )
    except Exception as exc:  # noqa: BLE001
        errors["sws_industry_hist"] = f"unreadable XLS: {type(exc).__name__}: {str(exc)[:200]}"
        return []
    return _normalise_sw_industry_rows(frame)


def _normalise_sw_industry_code(value: object) -> str:
    text = str(value or "").strip()
    return text[1:] if text[:1] in {"S", "s"} else text


def _fetch_cninfo_sw_tree(indtype: str) -> dict[str, str]:
    """Fetch one 申银万国 CNINFO classification tree (numeric code -> name)."""
    import requests  # local import
    import py_mini_racer  # type: ignore
    from akshare.datasets import get_ths_js  # type: ignore

    js_code = py_mini_racer.MiniRacer()
    js_code.eval(open(get_ths_js("cninfo.js"), encoding="utf-8").read())
    headers = {
        "Accept": "*/*",
        "Accept-Enckey": js_code.call("getResCode1"),
        "Origin": "https://webapi.cninfo.com.cn",
        "Referer": "https://webapi.cninfo.com.cn/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    response = requests.get(
        "https://webapi.cninfo.com.cn/api/stock/p_public0002",
        params={"indcode": "", "indtype": indtype, "format": "json"},
        headers=headers,
        timeout=40,
    )
    payload = response.json()
    names: dict[str, str] = {}
    for record in payload.get("records") or []:
        code = _normalise_sw_industry_code(record.get("SORTCODE"))
        name = str(record.get("SORTNAME") or "").strip()
        if code and name:
            names.setdefault(code, name)
    return names


def _fetch_sw_industry_code_names(
    errors: dict[str, str], caveats: list[str]
) -> dict[str, str]:
    """Resolve SWS numeric industry codes to names via the CNINFO trees."""
    names: dict[str, str] = {}
    try:
        import akshare as ak  # type: ignore

        frame = ak.stock_industry_category_cninfo(symbol="申银万国行业分类标准")
        for code, name in zip(frame.get("类目编码"), frame.get("类目名称")):
            key = _normalise_sw_industry_code(code)
            if key and str(name).strip():
                names.setdefault(key, str(name).strip())
    except Exception as exc:  # noqa: BLE001
        errors["sws_industry_category_current"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        legacy = _fetch_cninfo_sw_tree(CNINFO_SW_IND_TYPES["legacy"])
        for key, value in legacy.items():
            names.setdefault(key, value)
    except Exception as exc:  # noqa: BLE001
        errors["sws_industry_category_legacy"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        caveats.append("sws_legacy_category_tree_unavailable")
    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="CN", choices=("CN", "US"))
    parser.add_argument("--output", type=Path, help="Artifact path (defaults to conventional path).")
    parser.add_argument("--artifacts-dir", type=Path, help="Artifact root used to resolve the default path.")
    parser.add_argument("--tushare-token", help="Override PQW_TUSHARE_TOKEN (never written to output).")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep-seconds", type=float, default=62.0)
    parser.add_argument("--endpoint-interval-seconds", type=float, default=0.0)
    parser.add_argument("--no-db-cross-check", action="store_true")
    parser.add_argument(
        "--no-akshare",
        action="store_true",
        help="Skip the exchange (SSE/SZSE) delisting lists used as independent delisting evidence.",
    )
    parser.add_argument(
        "--no-industry",
        action="store_true",
        help="Skip the SWS/CNINFO effective-dated industry-history evidence.",
    )
    parser.add_argument("--raw-input", type=Path, help="Read provider rows from a JSON cache instead of the network.")
    parser.add_argument("--save-raw", type=Path, help="Write the raw provider rows used to build the artifact.")
    return parser.parse_args()


def _load_raw_input(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--raw-input must contain a JSON object")
    return payload


def _fetch_provider_rows(
    args: argparse.Namespace,
    errors: dict[str, str],
    *,
    on_progress: Callable[[dict[str, list[dict]]], None] | None = None,
) -> dict[str, list[dict]]:
    import tushare as ts  # type: ignore

    rows: dict[str, list[dict]] = {"listed": [], "paused": [], "delisted": [], "name_changes": []}
    token = args.tushare_token or get_settings().tushare_token
    if not token:
        errors["tushare_token"] = "not configured"
        return rows
    pro = ts.pro_api(token)
    if pro is None:
        errors["tushare_token"] = "pro_api returned None"
        return rows

    def endpoint(key: str, label: str, call: Callable[[], object]) -> None:
        records = _fetch_records(
            label,
            call,
            retries=args.retries,
            retry_sleep_seconds=args.retry_sleep_seconds,
            errors=errors,
        )
        for record in records:
            record.setdefault("source", "tushare")
        rows[key] = records
        if on_progress is not None:
            on_progress(rows)
        if args.endpoint_interval_seconds > 0:
            time.sleep(args.endpoint_interval_seconds)

    # Delisted first so the most decisive evidence survives a later rate limit.
    endpoint(
        "delisted",
        "stock_basic_delisted",
        lambda: pro.stock_basic(exchange="", list_status="D", fields=STOCK_BASIC_FIELDS),
    )
    endpoint(
        "paused",
        "stock_basic_paused",
        lambda: pro.stock_basic(exchange="", list_status="P", fields=STOCK_BASIC_FIELDS),
    )
    endpoint(
        "listed",
        "stock_basic_listed",
        lambda: pro.stock_basic(exchange="", list_status="L", fields=STOCK_BASIC_FIELDS),
    )
    endpoint(
        "name_changes",
        "namechange",
        lambda: pro.namechange(fields="ts_code,name,start_date,end_date,ann_date,change_reason"),
    )
    return rows


def _merge_delisted(
    tushare_rows: list[dict], exchange_rows: list[dict]
) -> list[dict]:
    """Merge delisting evidence, preferring TuShare rows on ts_code collision."""
    merged: dict[str, dict] = {}
    for row in tushare_rows:
        code = str(row.get("ts_code") or "").strip().upper()
        if code:
            row.setdefault("source", "tushare")
            merged[code] = row
    for row in exchange_rows:
        code = str(row.get("ts_code") or "").strip().upper()
        if code and code not in merged:
            merged[code] = row
    return [merged[code] for code in sorted(merged)]


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".contract-", suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def main() -> None:
    args = parse_args()
    settings = get_settings()
    errors: dict[str, str] = {}

    if args.raw_input is not None:
        raw = _load_raw_input(args.raw_input)
        rows = {
            "listed": list(raw.get("listed") or []),
            "paused": list(raw.get("paused") or []),
            "delisted": list(raw.get("delisted") or []),
            "name_changes": list(raw.get("name_changes") or raw.get("namechange") or []),
            "akshare_delisted": list(raw.get("akshare_delisted") or []),
        }
        industry_rows = list(raw.get("industry_rows") or [])
        industry_code_names = {
            str(key): str(value)
            for key, value in (raw.get("industry_code_names") or {}).items()
        }
        caveats = [str(item) for item in (raw.get("caveats") or [])]
        errors.update({str(k): str(v) for k, v in (raw.get("errors") or {}).items()})
    else:
        def _save_progress(current: dict[str, list[dict]]) -> None:
            if args.save_raw is not None:
                _atomic_write_json(
                    args.save_raw,
                    {"market": args.market, **current, "errors": errors},
                )

        rows = _fetch_provider_rows(args, errors, on_progress=_save_progress)
        industry_rows = []
        industry_code_names = {}
        caveats = []

    if not args.no_akshare and args.market == "CN" and args.raw_input is None:
        rows["akshare_delisted"] = _fetch_akshare_delistings(errors)
    rows.setdefault("akshare_delisted", [])
    rows["delisted"] = _merge_delisted(rows["delisted"], rows.get("akshare_delisted") or [])

    if args.no_industry:
        industry_rows = []
        industry_code_names = {}
    elif args.market == "CN":
        if not industry_rows:
            industry_rows = _fetch_sw_industry_rows(errors, caveats)
        if not industry_code_names:
            industry_code_names = _fetch_sw_industry_code_names(errors, caveats)

    if args.save_raw is not None:
        _atomic_write_json(
            args.save_raw,
            {
                "market": args.market,
                **rows,
                "industry_rows": industry_rows,
                "industry_code_names": industry_code_names,
                "caveats": caveats,
                "errors": errors,
            },
        )

    cross_check = None
    if not args.no_db_cross_check:
        active_tickers = _load_db_active_tickers(args.market)
        if active_tickers is not None:
            cross_check = {"active_tickers": active_tickers, "source": "symbols_table"}

    payload = build_historical_universe_contract(
        market=args.market,
        listed=rows["listed"],
        paused=rows["paused"],
        delisted=rows["delisted"],
        name_changes=rows["name_changes"],
        industry_rows=industry_rows,
        industry_code_names=industry_code_names,
        endpoint_errors=errors,
        cross_check=cross_check,
    )
    for caveat in caveats:
        payload["warnings"].append(f"provider_caveat: {caveat}")

    if args.output is not None:
        output_path = args.output
    else:
        output_path = resolve_historical_universe_contract_path(
            artifacts_dir=args.artifacts_dir or settings.artifacts_dir,
            settings_path=settings.historical_universe_contract_path,
        )
    _atomic_write_json(output_path, payload)

    print(
        json.dumps(
            {
                "output": str(output_path),
                "market": payload["market"],
                "dimensions": payload["dimensions"],
                "missing_reasons": payload["missing_reasons"],
                "evidence": payload["evidence"],
                "provider_endpoint_errors": payload["provider_endpoint_errors"],
                "caveats": caveats,
                "counts": {
                    "listed": len(rows["listed"]),
                    "paused": len(rows["paused"]),
                    "delisted": len(rows["delisted"]),
                    "name_changes": len(rows["name_changes"]),
                    "akshare_delisted": len(rows.get("akshare_delisted") or []),
                    "industry_rows": len(industry_rows),
                    "industry_code_names": len(industry_code_names),
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
