"""Append real scheduled-evaluation receipts only; no training or notifications.

Explicit --apply is required. Existing model runs and evaluation rows are never
rewritten. A blocked real receipt is not a successful execution certification.
"""
import argparse
import json
from pathlib import Path
import sys
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.db import SessionLocal
from app.services.repository import DataJobRepository
from app.services.model_evaluation import list_latest_model_evaluations
from app.services.cn_market_scheduler import CNMarketSchedulerService
from app.services.us_market_scheduler import USMarketSchedulerService
from app.services.execution_reconciliation import implementation_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Existing receipt will not be overwritten')
    if not args.apply:
        print('DRY RUN: would append CN/US scheduled evaluations; no training, no notification, no rewrite.')
        return
    with SessionLocal() as db:
        repo = DataJobRepository(db)
        if repo.has_running_job('evaluate_model_performance'):
            raise RuntimeError('An evaluation is running; do not race it')
        parent = repo.create_job(job_type='execution_contract_verification', status='running',
            params={'markets': ['CN', 'US'], 'implementation': implementation_identity()},
            message='Explicit user-requested execution contract verification; evaluation only.')
        parent_id = parent.id
    records = []
    try:
        for market, service in (('CN', CNMarketSchedulerService()), ('US', USMarketSchedulerService())):
            with SessionLocal() as db:
                old = list_latest_model_evaluations(db, market=market, limit=1)
            service._run_structured_evaluation(source_job_id=parent_id)
            with SessionLocal() as db:
                latest = list_latest_model_evaluations(db, market=market, limit=1)
            if not latest or (old and latest[0]['id'] <= old[0]['id']):
                raise RuntimeError(f'{market}: no new evaluation receipt; inspect child job')
            row = latest[0]
            summary = row['summary']
            records.append(dict(market=market, evaluation_id=row['id'], model_run_id=row['model_run_id'],
                status=row['status'], protocol=summary.get('outcome_protocol'),
                execution_verified=summary.get('execution_verified'),
                implementation=summary.get('implementation'),
                outcome_counts=summary.get('outcome_counts'),
                reasons=dict(Counter(x.get('reason') for x in summary.get('candidate_outcomes', [])))))
        status = 'success' if all(x['status'] == 'success' and x['execution_verified'] for x in records) else 'partial'
        receipt = dict(status=status, parent_job_id=parent_id,
                       scope='real_database_actual_scheduler_entry_no_service_restart', records=records)
        with SessionLocal() as db:
            DataJobRepository(db).complete_job(parent_id, status=status,
                message='Runtime contract receipts persisted; unresolved execution evidence remains blocked.', result=receipt)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as handle:
            json.dump(receipt, handle, indent=2, ensure_ascii=False)
        print(json.dumps(receipt, ensure_ascii=False))
    except Exception as exc:
        with SessionLocal() as db:
            DataJobRepository(db).complete_job(parent_id, status='failed', message=str(exc), result={'records': records})
        raise


if __name__ == '__main__':
    main()
