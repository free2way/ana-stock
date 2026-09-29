"""Produce per-trade golden evidence; no database, suppliers, or notifications."""
import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.execution_reconciliation import implementation_identity
from tests.test_execution_reconciliation import ExecutionReconciliationTests, golden_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Refusing to overwrite an existing receipt')
    tester = ExecutionReconciliationTests()
    records = []
    for market in ('CN', 'US'):
        for scenario in ('normal', 'suspended_entry', 'missing_open', 'immature',
                         'exit_deferred', 'exit_recovered', 'corporate_action'):
            rows = deepcopy(golden_rows())
            if scenario == 'suspended_entry': rows[1]['volume'] = 0
            if scenario == 'missing_open': del rows[1]['open']
            if scenario == 'immature': rows = rows[:4]
            if scenario in ('exit_deferred', 'exit_recovered'): rows[5]['volume'] = 0
            if scenario == 'exit_deferred': rows = rows[:6]
            if scenario == 'corporate_action': rows[3]['corporate_action_status'] = 'action'
            with patch('socket.socket.connect', side_effect=AssertionError('Golden suite forbids network')):
                replay = tester.replay(market, rows)
                target, detail, _ = tester.trainer().reconciled_training_target(
                    symbol_rows=rows, index=0, horizon_days=5, market=market,
                    ticker='000001.SZ' if market == 'CN' else 'AAA')
                assert target == replay['net_return']
                account = tester.engine(market, rows) if scenario in ('normal', 'exit_deferred', 'exit_recovered') else None
            expected = None
            if replay['status'] == 'CLOSED':
                expected = float(Decimal(96) * Decimal('.9988') * Decimal('.9992') /
                                 (Decimal(100) * Decimal('1.0012') * Decimal('1.0008')) - 1)
                assert abs(expected - target) <= 1e-12
                assert abs(account.outcomes[0]['net_return'] - target) <= 1e-12
            records.append(dict(market=market, scenario=scenario, replay=replay,
                training_target=target, training_label_available_date=detail.get('label_available_date'),
                independent_decimal_return=expected,
                account_outcomes=list(account.outcomes) if account else None,
                account_fills=list(account.fills) if account else None,
                account_states=list(account.portfolio_states) if account else None))
    receipt = dict(status='PASS', scope='synthetic_per_trade_not_production',
                   return_tolerance=1e-12, implementation=implementation_identity(), records=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(receipt, handle, indent=2, ensure_ascii=False)
    print(json.dumps(dict(status='PASS', records=len(records), output=str(args.output))))


if __name__ == '__main__':
    main()
