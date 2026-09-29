"""Bounded, read-only capability probes. Never starts training or writes the DB."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import re
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.core.config import get_settings


def safe_next_url(value, endpoint):
    """Credentials never follow a provider-supplied URL outside its exact endpoint."""
    candidate, origin = urlsplit(value), urlsplit(endpoint)
    if (candidate.scheme != 'https' or candidate.netloc != origin.netloc
            or candidate.path != origin.path or candidate.fragment
            or candidate.username or candidate.password):
        raise ValueError('unsafe_pagination_url')
    query = [(k, v) for k, v in parse_qsl(candidate.query, keep_blank_values=True)
             if k.lower() not in {'apikey', 'api_key', 'token'}]
    return urlunsplit((candidate.scheme, candidate.netloc, candidate.path, urlencode(query), ''))


def check_event_pages(records, ticker):
    """Pagination termination is not proof of provider event completeness."""
    seen, count = set(), 0
    for record in records:
        if record.get('http_status') != 200:
            return {'status': 'BLOCKED', 'reason': 'page_request_failed'}
        try:
            data = json.loads(record['response_body'])
            if not isinstance(data, dict) or data.get('status') != 'OK' or not isinstance(data.get('results'), list):
                raise ValueError('invalid_event_response')
            for event in data['results']:
                if not isinstance(event, dict) or event.get('ticker') != ticker or not isinstance(event.get('id'), str) or not event['id'] or event['id'] in seen:
                    raise ValueError('wrong_ticker_or_duplicate_event')
                seen.add(event['id'])
                count += 1
        except (KeyError, ValueError, TypeError) as exc:
            return {'status': 'BLOCKED', 'reason': type(exc).__name__}
    complete = bool(records) and not data.get('next_url')
    return {'status': 'PAGINATION_COMPLETE' if complete else 'BLOCKED',
            'reason': None if complete else 'pagination_incomplete',
            'unique_events': count, 'negative_event_inference_allowed': False}


def suspension_observations(record):
    """Retain reported intervals, never convert them to verified daily states."""
    if record.get('http_status') != 200:
        raise ValueError('unsuccessful_response')
    raw = record['response_body']
    if hashlib.sha256(raw.encode()).hexdigest() != record['response_sha256']:
        raise ValueError('response_hash_mismatch')
    collected = datetime.fromisoformat(record['collected_at'])
    if collected.tzinfo is None:
        raise ValueError('collection_timezone_required')
    body = json.loads(raw)
    if body.get('success') is not True or not isinstance(body.get('result'), dict):
        raise ValueError('invalid_suspension_response')
    rows = body['result'].get('data')
    if not isinstance(rows, list):
        raise ValueError('missing_suspension_rows')
    result, excluded = [], []
    for index, row in enumerate(rows):
        code = str(row.get('SECUCODE') or '')
        ref = dict(provider=record['provider'], endpoint=record['endpoint'],
                   request_params=record['params'], collected_at=record['collected_at'],
                   response_sha256=record['response_sha256'], row_index=index)
        # Keep raw BJ/B-share rows in the receipt, but not in the CN A-share sidecar.
        if not re.fullmatch(r'(?:(?:600|601|603|605|688)\d{3}\.SH|(?:000|001|002|003|300|301)\d{3}\.SZ)', code):
            excluded.append(dict(symbol=code, reason='outside_sh_sz_a_share_scope', source=ref))
            continue
        result.append(dict(symbol=code.replace('.SH', '.SS'), market='CN',
                           reported_start=row.get('SUSPEND_START_TIME'),
                           reported_end=row.get('SUSPEND_END_TIME'),
                           expected_resume=row.get('PREDICT_RESUME_DATE'),
                           reported_reason=row.get('SUSPEND_REASON'),
                           provider_timezone='Asia/Shanghai', source=ref,
                           status='OBSERVED_NOT_EXECUTION_VERIFIED',
                           actual_resume_verified=False))
    return dict(observations=result, exclusions=excluded, input_count=len(rows),
                negative_event_inference_allowed=False, daily_state_generation_allowed=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--followup', action='store_true',
                        help='Compare historical suspension filters and finish bounded dividend pagination')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Evidence already exists; use a new filename')
    s = get_settings()
    secrets = [s.alpaca_api_key, s.alpaca_api_secret, s.polygon_api_key]
    alpaca = {'APCA-API-KEY-ID': s.alpaca_api_key or '',
              'APCA-API-SECRET-KEY': s.alpaca_api_secret or ''}
    polygon = {'Authorization': 'Bearer ' + (s.polygon_api_key or '')}
    # Explicit hosts: never send credentials to configurable redirects or fallbacks.
    probes = [
        ('alpaca', 'raw_prices', 'https://data.alpaca.markets/v2/stocks/AAPL/bars',
         {'start': '2026-09-01', 'end': '2026-09-24', 'timeframe': '1Day',
          'adjustment': 'raw', 'feed': s.alpaca_data_feed, 'limit': 100}, alpaca),
        ('alpaca', 'corporate_actions', 'https://data.alpaca.markets/v1/corporate-actions',
         {'symbols': 'AAPL', 'start': '2026-01-01', 'end': '2026-09-24', 'limit': 100}, alpaca),
        ('polygon', 'raw_prices', 'https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/2026-09-01/2026-09-24',
         {'adjusted': 'false', 'limit': 100}, polygon),
        ('polygon', 'splits', 'https://api.polygon.io/stocks/v1/splits',
         {'ticker': 'AAPL', 'limit': 10}, polygon),
        ('polygon', 'dividends', 'https://api.polygon.io/stocks/v1/dividends',
         {'ticker': 'AAPL', 'limit': 10}, polygon),
        ('eastmoney_via_akshare', 'historical_suspensions',
         'https://datacenter-web.eastmoney.com/api/data/v1/get',
         {'sortColumns': 'SUSPEND_START_DATE', 'sortTypes': '-1', 'pageSize': '500',
          'pageNumber': '1', 'reportName': 'RPT_CUSTOM_SUSPEND_DATA_INTERFACE',
          'columns': 'ALL', 'source': 'WEB', 'client': 'WEB',
          'filter': '(MARKET="全部")(DATETIME=\'2026-09-24\')'}, {}),
        ('eastmoney_via_akshare', 'raw_prices',
         'https://push2his.eastmoney.com/api/qt/stock/kline/get',
         {'fields1': 'f1,f2,f3,f4,f5,f6',
          'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
          'klt': '101', 'fqt': '0', 'secid': '0.000001',
          'beg': '20260901', 'end': '20260924'}, {}),
    ]
    if args.followup:
        template = probes[5]
        probes = []
        for day in ('2026-09-01', '2026-09-24'):
            params = dict(template[3], filter=f'(MARKET="全部")(DATETIME=\'{day}\')')
            probes.append((template[0], f'halt_date_comparison_{day}', template[2], params, {}))
        probes.append(('polygon', 'dividends_paginated', 'https://api.polygon.io/stocks/v1/dividends',
                       {'ticker': 'AAPL', 'limit': 100}, polygon))

    def probe(spec):
        provider, capability, endpoint, params, headers = spec
        record = dict(provider=provider, capability=capability, endpoint=endpoint,
                      params=params, requested_at=datetime.now(timezone.utc).isoformat(),
                      coverage_verified=False)
        if headers and any(not v or v == 'Bearer ' for v in headers.values()):
            record['error'] = 'credential_missing'
        else:
            try:
                with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
                    response = client.get(endpoint, params=params, headers=headers)
                body = response.text
                for secret in secrets:
                    if secret:
                        body = body.replace(secret, '[REDACTED]')
                record.update(http_status=response.status_code, response_body=body,
                              response_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              response_hash_basis='utf8_decoded_body_after_secret_redaction',
                              request_id=response.headers.get('x-request-id'))
            except Exception as exc:
                # Exception text may contain authenticated URLs; retain type only.
                record['error'] = type(exc).__name__
        record['collected_at'] = datetime.now(timezone.utc).isoformat()
        print(json.dumps({k: record[k] for k in ('provider', 'capability', 'http_status', 'error') if k in record}), flush=True)
        return record

    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(probe, probes))
    checks = {}
    if args.followup:
        pages = [records[-1]]
        visited = set()
        for _ in range(9):
            try:
                body = json.loads(pages[-1].get('response_body', '{}'))
                if pages[-1].get('http_status') != 200 or body.get('status') != 'OK' or not body.get('next_url'):
                    break
                next_url = safe_next_url(body['next_url'], probes[-1][2])
                if next_url in visited:
                    raise ValueError('pagination_cycle')
                visited.add(next_url)
            except (ValueError, TypeError):
                checks['pagination_transport_error'] = 'invalid_or_repeated_next_url'
                break
            record = probe(('polygon', 'dividends_paginated', next_url, {}, polygon))
            records.append(record)
            pages.append(record)
        checks['dividends'] = check_event_pages(pages, 'AAPL')
        snapshots = []
        for record in records[:2]:
            try:
                body = json.loads(record['response_body'])
                rows = body['result']['data']
                snapshots.append(sorted(json.dumps(row, sort_keys=True) for row in rows))
            except (KeyError, ValueError, TypeError):
                snapshots.append(None)
        checks['suspension_date_filter'] = {
            'identical_nonempty_responses': bool(snapshots[0]) and snapshots[0] == snapshots[1],
            'historical_snapshot_verified': False,
            'negative_event_inference_allowed': False}
        for record in records[:2]:
            try:
                record['suspension_observations'] = suspension_observations(record)
            except (ValueError, KeyError, TypeError):
                record['normalization_status'] = 'BLOCKED'
    payload = {'schema': 'source_capability_probe_v1', 'records': records,
               'checks': checks,
               'training_authorized_by_receipt': False,
               'note': 'Bounded discovery only; no completeness or negative-event inference.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
