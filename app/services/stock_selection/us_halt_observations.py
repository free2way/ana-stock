"""Read-only NYSE CSV observations; never certify absence of a halt or a fill."""
from collections import Counter
import csv
from datetime import datetime
import hashlib
import io

URL = 'https://www.nyse.com/api/trade-halts/historical/download'
FIELDS = ['Halt Date', 'Halt Time', 'Symbol', 'Name', 'Exchange', 'Reason',
          'Resume Date', 'NYSE Resume Time']


def assess_nyse_halts(record):
    if record.get('url') != URL or record.get('http_status') != 200:
        raise ValueError('wrong_source_or_failed_request')
    collected = datetime.fromisoformat(record['collected_at'])
    if collected.tzinfo is None:
        raise ValueError('collection_timezone_required')
    body = record['body']
    # Discovery decoded utf-8-sig; permit only the exact original with/without BOM.
    if record.get('sha256') not in {
        hashlib.sha256(body.encode('utf-8')).hexdigest(),
        hashlib.sha256(body.encode('utf-8-sig')).hexdigest(),
    }:
        raise ValueError('response_hash_mismatch')
    reader = csv.DictReader(io.StringIO(body))
    if reader.fieldnames != FIELDS:
        raise ValueError('unexpected_schema')
    counts, reasons, exchanges, seen = Counter(), Counter(), Counter(), set()
    observations, dates = [], []
    for index, row in enumerate(reader):
        problems = []
        if None in row or any(value is None for value in row.values()):
            problems.append('malformed_row')
        start = None
        try:
            start = datetime.strptime(f"{row['Halt Date']} {row['Halt Time']}", '%Y-%m-%d %H:%M:%S')
            dates.append(start.date().isoformat())
        except (ValueError, TypeError):
            problems.append('invalid_halt_timestamp')
        resume_date, resume_time = row.get('Resume Date'), row.get('NYSE Resume Time')
        if not resume_date or not resume_time:
            problems.append('resume_unknown')
        elif 'See Subsequent' in resume_date or 'See Subsequent' in resume_time:
            problems.append('subsequent_halt_link_required')
        else:
            try:
                resume = datetime.strptime(f'{resume_date} {resume_time}', '%Y-%m-%d %H:%M:%S')
                if start and resume < start:
                    problems.append('resume_before_halt')
            except (ValueError, TypeError):
                problems.append('invalid_resume_timestamp')
        key = tuple(row.get(field) for field in FIELDS)
        if key in seen:
            problems.append('duplicate_source_row')
        seen.add(key)
        if not row.get('Symbol'):
            problems.append('missing_symbol')
        reasons.update(problems)
        exchanges.update([row.get('Exchange')])
        status = 'REVIEW_REQUIRED' if problems else 'OBSERVED_INTERVAL'
        counts.update([status])
        observations.append(dict(row_index=index, symbol=row.get('Symbol'),
                                 status=status, reasons=problems, raw=row))
    return dict(schema='nyse_halt_observations_v1', market='US',
                source=dict(url=URL, collected_at=record['collected_at'], sha256=record['sha256']),
                row_count=len(observations), status_counts=dict(counts),
                issue_counts=dict(reasons), exchanges=dict(exchanges),
                earliest_halt=min(dates) if dates else None,
                latest_halt=max(dates) if dates else None,
                timezone_basis='NYSE page says ET; timestamps retained as reported, no UTC conversion',
                resume_scope='NYSE Resume Time: not yet certified for every listing venue',
                observations=observations, coverage_verified=False,
                negative_event_inference_allowed=False, training_authorized=False)
