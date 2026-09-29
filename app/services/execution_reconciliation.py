"""One-candidate execution ledger shared by labels and scheduled evaluation.

Not a portfolio simulator or a certification of real broker fills. Missing
source facts fail closed; adjusted prices and inferred CN limits are forbidden.
Returns remain fractions. Positions use one reference share for reconciliation.
"""
from collections import Counter
from datetime import date
import hashlib
import json
import math
from pathlib import Path

from app.services.execution_costs import FillCostModel
from app.services.market_calendar import is_market_open_date, next_market_open_date

VERSION = 'reconciled_execution_v1'


def implementation_identity():
    root = Path(__file__).resolve().parents[2]
    paths = ('app/services/execution_reconciliation.py', 'app/services/trainer.py',
             'app/services/model_evaluation.py', 'app/services/execution_costs.py',
             'app/services/market_calendar.py', 'app/services/backtesting/engine.py',
             'app/services/cn_market_scheduler.py', 'app/services/us_market_scheduler.py',
             'app/api/routes/jobs.py')
    files = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}
    return {'files': files, 'sha256': digest(files)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    default=str).encode()).hexdigest()


def execution_contract(market, cost):
    return dict(version=VERSION, market=market, calendar_version='market_calendar_2026_v1',
                price_basis='raw', cost=cost.metadata(), entry_rule='next_session_open',
                exit_rule='signal_plus_h_close_defer_until_executable')


def validate_contract(contract, market):
    if not isinstance(contract, dict):
        raise ValueError('missing_execution_contract')
    if market not in {'CN', 'US'} or contract.get('market') != market:
        raise ValueError('contract_market_mismatch')
    try:
        cost = FillCostModel(contract['cost']['commission_bps_one_way'],
                             contract['cost']['slippage_bps_one_way'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid_cost_contract') from None
    if contract != execution_contract(market, cost):
        raise ValueError('execution_contract_or_cost_hash_mismatch')
    return cost


def _number(row, key):
    value = row.get(key)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def replay_candidate(*, ticker, market, signal_date, horizon_days, rows, contract, as_of=None):
    """No candidate drops: each input becomes one terminal/ongoing ledger row."""
    result = dict(ticker=ticker, market=market, trade_date=signal_date,
                  horizon_days=horizon_days, protocol=VERSION, contract_hash=digest(contract),
                  status='UNVERIFIED', reason=None, net_return=None, gross_return=None,
                  entry_date=None, scheduled_exit_date=None, exit_date=None,
                  entry_fill=None, exit_fill=None, invested_capital=None,
                  position=None, events=[], price_evidence_hash=digest(rows))

    def finish(status, reason):
        result.update(status=status, reason=reason)
        return result

    try:
        cost = validate_contract(contract, market)
    except ValueError as exc:
        return finish('UNVERIFIED', str(exc))
    if type(horizon_days) is not int or horizon_days < 1 or (market == 'CN' and horizon_days == 1):
        return finish('EXCLUDED', 'holding_horizon_incompatible_with_market')
    try:
        start = date.fromisoformat(signal_date)
        observed = as_of or max((str(row.get('date') or '') for row in rows), default=signal_date)
        end = date.fromisoformat(observed)
    except ValueError:
        return finish('UNVERIFIED', 'invalid_calendar_date')
    # The repository calendar explicitly covers 2026 only. Do not pretend its
    # weekend-only fallback is a verified historical exchange calendar.
    if start.year != 2026 or end.year != 2026:
        return finish('UNVERIFIED', 'calendar_year_not_verified')
    if not is_market_open_date(market, start):
        return finish('UNVERIFIED', 'signal_date_not_market_session')
    sessions = [signal_date]
    for _ in range(horizon_days):
        sessions.append(next_market_open_date(market, sessions[-1], include_self=False))
    entry_day, due = sessions[1], sessions[-1]
    result.update(entry_date=entry_day, scheduled_exit_date=due, as_of=observed,
                  cost_model_hash=cost.model_hash, cost_model_version=cost.version)
    if entry_day > observed:
        return finish('PENDING', 'entry_not_due')
    by_date = {}
    for row in rows:
        day = str(row.get('date') or '')
        if day in by_date:
            return finish('UNVERIFIED', 'duplicate_price_date')
        by_date[day] = row

    def evidence(row):
        if row is None:
            return 'missing_market_bar'
        if row.get('price_basis') != 'raw':
            return 'raw_price_basis_unverified'
        if not str(row.get('execution_source_reference') or '').strip():
            return 'daily_execution_provenance_missing'
        if row.get('corporate_action_status') not in {'none', 'action'}:
            return 'corporate_action_state_unknown'
        if row.get('corporate_action_status') == 'action':
            return 'corporate_action_requires_account_replay'
        if type(row.get('suspended')) is not bool:
            return 'daily_suspension_state_unknown'
        return None

    entry = by_date.get(entry_day)
    reason = evidence(entry)
    if reason:
        return finish('UNVERIFIED', reason)
    op, volume = _number(entry, 'open'), _number(entry, 'volume')
    if entry['suspended'] or (volume is not None and volume <= 0):
        return finish('REJECTED', 'entry_suspended_or_no_volume')
    if op is None or op <= 0 or volume is None:
        return finish('UNVERIFIED', 'missing_or_invalid_open_volume')
    if market == 'CN':
        upper = _number(entry, 'upper_limit')
        if upper is None or upper <= 0:
            return finish('UNVERIFIED', 'daily_upper_limit_unknown')
        if op >= upper:
            return finish('REJECTED', 'limit_up_at_entry')
    buy = cost.fill(op, 1, side='buy')
    capital = -buy['cash_flow']
    result.update(entry_fill=buy, invested_capital=capital,
                  position=dict(quantity=1.0, cost_basis=capital, last_mark=op,
                                mark_date=entry_day, mark_stale=False))
    result['events'].append(dict(type='BUY', date=entry_day, **buy))
    day = entry_day
    while day <= observed:
        row = by_date.get(day)
        reason = evidence(row)
        if reason:
            result['position']['mark_stale'] = True
            result['events'].append(dict(type='EVIDENCE_BLOCK', date=day, reason=reason))
            # Keep the known open position; never convert an unknown exit to 0.
            return finish('UNVERIFIED', reason)
        close, volume = _number(row, 'close'), _number(row, 'volume')
        if close is None or close <= 0 or volume is None:
            return finish('UNVERIFIED', 'missing_or_invalid_close_volume')
        result['position'].update(last_mark=close, mark_date=day, mark_stale=False)
        result['events'].append(dict(type='MARK', date=day, quantity=1.0, market_value=close))
        if day >= due:
            reject = 'exit_suspended_or_no_volume' if row['suspended'] or volume <= 0 else None
            if market == 'CN' and not reject:
                lower = _number(row, 'lower_limit')
                if lower is None or lower <= 0:
                    return finish('UNVERIFIED', 'daily_lower_limit_unknown')
                if close <= lower:
                    reject = 'limit_down_at_exit'
            if reject:
                result['events'].append(dict(type='EXIT_DEFERRED', date=day, reason=reject))
            else:
                sell = cost.fill(close, 1, side='sell')
                result['events'].append(dict(type='SELL', date=day, **sell))
                result.update(exit_date=day, exit_fill=sell, position=None,
                              gross_return=close / op - 1,
                              net_return=(sell['cash_flow'] - capital) / capital,
                              label_available_date=day)
                return finish('CLOSED', None)
        day = next_market_open_date(market, day, include_self=False)
        if not day.startswith('2026-'):
            return finish('UNVERIFIED', 'calendar_year_not_verified')
    return finish('EXIT_DEFERRED' if observed >= due else 'PENDING',
                  'exit_not_executable' if observed >= due else 'holding_period_not_mature')


def outcome_counts(rows):
    counts = dict(Counter(row['status'] for row in rows))
    if sum(counts.values()) != len(rows):
        raise AssertionError('candidate ledger count mismatch')
    return counts
