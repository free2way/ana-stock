from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from app.services.execution_costs import FillCostModel
from app.services.execution_reconciliation import execution_contract, replay_candidate, VERSION
from app.services.trainer import SignalTrainer
from app.services.backtesting import DailyBar, EngineConfig, EventDrivenDailyEngine, SignalCandidate


def golden_rows():
    dates = ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11',
             '2026-09-14', '2026-09-15', '2026-09-16']
    return [dict(date=day, symbol='000001.SZ', open=100., high=110., low=90., close=96.,
                 volume=1000000., price_basis='raw', suspended=False,
                 corporate_action_status='none', execution_source_reference='golden-v1',
                 upper_limit=120., lower_limit=80.) for day in dates]


class ExecutionReconciliationTests(TestCase):
    def replay(self, market='CN', rows=None, **kwargs):
        return replay_candidate(ticker='000001.SZ' if market == 'CN' else 'AAA', market=market,
            signal_date='2026-09-08', horizon_days=5,
            rows=golden_rows() if rows is None else rows,
            contract=execution_contract(market, FillCostModel(8, 12)), **kwargs)

    def trainer(self):
        trainer = SignalTrainer.__new__(SignalTrainer)
        trainer.settings = SimpleNamespace(trainer_cn_execution_commission_bps=8,
            trainer_cn_execution_slippage_bps=12, trainer_cn_label_profile=VERSION)
        return trainer

    def engine(self, market, rows):
        ticker = '000001.SZ' if market == 'CN' else 'AAA'
        return EventDrivenDailyEngine(EngineConfig(market=market, holding_days=5,
            top_n=1, initial_cash=100000., commission_bps=8, slippage_bps=12,
            max_position_weight=1)).run(
            bars=[DailyBar(ticker, row['date'], row['open'], row['high'], row['low'],
                           row['close'], row['volume']) for row in rows],
            signals=[SignalCandidate(rows[0]['date'], ticker, 1)],
            calendar_sessions=[row['date'] for row in rows])

    def test_cn_us_independent_decimal_trainer_replay_and_existing_account_engine(self):
        expected = (Decimal(96) * Decimal('.9988') * Decimal('.9992') /
                    (Decimal(100) * Decimal('1.0012') * Decimal('1.0008'))) - 1
        for market in ('CN', 'US'):
            with self.subTest(market=market):
                rows = golden_rows()
                outcome = self.replay(market, rows)
                target, detail, version = self.trainer().reconciled_training_target(
                    symbol_rows=rows, index=0, horizon_days=5, market=market,
                    ticker='000001.SZ' if market == 'CN' else 'AAA')
                account = self.engine(market, rows)
                self.assertEqual('CLOSED', outcome['status'])
                self.assertEqual(VERSION, version)
                self.assertAlmostEqual(float(expected), target, places=12)
                self.assertAlmostEqual(target, outcome['net_return'], places=12)
                self.assertAlmostEqual(target, account.outcomes[0]['net_return'], places=12)
                self.assertEqual(detail['exit_date'], account.outcomes[0]['exit_date'])
                self.assertEqual(detail['cost_model_hash'], account.cost_model_metadata['hash'])

    def test_cn_us_deferred_position_retained_and_recovered_exit_matures_later(self):
        for market in ('CN', 'US'):
            with self.subTest(market=market):
                rows = golden_rows()
                rows[5]['volume'] = 0
                pending = self.replay(market, rows[:6])
                self.assertEqual('EXIT_DEFERRED', pending['status'])
                self.assertIsNone(pending['net_return'])
                self.assertEqual(1., pending['position']['quantity'])
                self.assertTrue(any(e['type'] == 'EXIT_DEFERRED' for e in pending['events']))
                self.assertEqual('EXIT_DEFERRED', self.engine(market, rows[:6]).outcomes[0]['status'])
                closed = self.replay(market, rows)
                self.assertEqual('2026-09-16', closed['label_available_date'])
                self.assertAlmostEqual(closed['net_return'], self.engine(market, rows).outcomes[0]['net_return'], places=12)

    def test_reject_entry_suspended_and_cn_limit_up(self):
        rows = golden_rows(); rows[1]['suspended'] = True
        for market in ('CN', 'US'):
            self.assertEqual('REJECTED', self.replay(market, rows)['status'])
        rows[1]['suspended'] = False; rows[1]['upper_limit'] = 100
        self.assertEqual('limit_up_at_entry', self.replay(rows=rows)['reason'])

    def test_optional_suspension_and_cn_bounds_do_not_block_v2(self):
        rows = golden_rows()
        for row in rows:
            row.pop('suspended')
            row.pop('upper_limit')
            row.pop('lower_limit')
        result = self.replay(rows=rows)
        self.assertEqual('CLOSED', result['status'])
        self.assertEqual('raw_price_volume_company_action_v2', result['evidence_policy'])
        self.assertTrue(result['optional_evidence_gaps'])

    def test_missing_open_never_falls_back_to_close(self):
        rows = golden_rows(); del rows[1]['open']
        for market in ('CN', 'US'):
            result = self.replay(market, rows)
            self.assertEqual('UNVERIFIED', result['status'])
            self.assertIsNone(result['entry_fill'])

    def test_unknown_action_keeps_open_position_without_inventing_return(self):
        rows = golden_rows(); rows[3]['corporate_action_status'] = 'action'
        for market in ('CN', 'US'):
            result = self.replay(market, rows)
            self.assertEqual('UNVERIFIED', result['status'])
            self.assertEqual('corporate_action_requires_account_replay', result['reason'])
            self.assertIsNotNone(result['position'])
            self.assertIsNone(result['net_return'])

    def test_missing_bar_does_not_shorten_horizon_or_drop_position(self):
        rows = golden_rows(); del rows[3]
        result = self.replay(rows=rows)
        self.assertEqual('missing_market_bar', result['reason'])
        self.assertEqual('2026-09-15', result['scheduled_exit_date'])
        self.assertIsNotNone(result['position'])

    def test_immature_is_pending_not_zero(self):
        result = self.replay(rows=golden_rows()[:4])
        self.assertEqual('PENDING', result['status'])
        self.assertIsNone(result['net_return'])

    def test_tampered_cost_hash_rejected(self):
        contract = deepcopy(execution_contract('CN', FillCostModel(8, 12)))
        contract['cost']['commission_bps_one_way'] = 0
        result = replay_candidate(ticker='000001.SZ', market='CN', signal_date='2026-09-08',
            horizon_days=5, rows=golden_rows(), contract=contract)
        self.assertEqual('execution_contract_or_cost_hash_mismatch', result['reason'])

    def test_actual_sample_builder_uses_replay_and_keeps_unmatured_reason(self):
        for market in ('CN', 'US'):
            with self.subTest(market=market):
                rows = golden_rows()
                ticker = '000001.SZ' if market == 'CN' else 'AAA'
                for row in rows:
                    row['symbol'] = ticker
                samples = self.trainer()._build_lightgbm_samples(
                    rows=rows, lookback_days=1, horizon_days=5,
                    symbol_feature_context={}, market=market)
                first = next(x for x in samples if x['trade_date'] == rows[1]['date'])
                expected = replay_candidate(ticker=ticker, market=market,
                    signal_date=rows[1]['date'], horizon_days=5, rows=rows[1:],
                    contract=execution_contract(market, FillCostModel(8, 12)))
                self.assertAlmostEqual(expected['net_return'], first['target'], places=12)
                last = samples[-1]
                self.assertIsNone(last['target'])
                self.assertEqual('PENDING', last['target_profile']['status'])
