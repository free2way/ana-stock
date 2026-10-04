"""A2/E4/E5/C11: actual scheduler -> evaluator -> PostgreSQL -> API reader.

Only provider price loading is replaced. No evaluator, protocol selection,
repository write, scheduler job creation or API serialization is mocked.
This suite MUST run through the disposable-cluster runner, never production.
"""
import json
from unittest.mock import patch

from sqlalchemy import select

from tests.postgres_safety import ApplicationPostgresTestCase
from app.core.db import SessionLocal
from app.models.tables import ModelRun, ModelEvaluation, Prediction, Symbol
from app.services.repository import DataJobRepository
from app.services.model_evaluation import list_latest_model_evaluations
from app.services.cn_market_scheduler import CNMarketSchedulerService
from app.services.us_market_scheduler import USMarketSchedulerService


class ScheduledExecutionContractTests(ApplicationPostgresTestCase):
    def exercise(self, market, *, missing_open=False, contract=None, verified_prices=False,
                 optional_facts=True):
        with SessionLocal() as db:
            parent = DataJobRepository(db).create_job(
                job_type='execution_contract_fixture', status='success', params={})
            parent_id = parent.id
            symbol = Symbol(ticker='000001.SZ' if market == 'CN' else 'AAPL',
                            name='Synthetic fixture', market=market,
                            created_at='2026-07-01T00:00:00+00:00',
                            updated_at='2026-07-01T00:00:00+00:00')
            # A version string alone is NOT execution evidence or frozen costs.
            run = ModelRun(name='contract-fixture', model_type='lightgbm_multifactor',
                           market=market, universe='fixture', status='success',
                           train_start='2025-01-01', train_end='2026-06-01',
                           test_start='2026-07-01',
                           config_json=json.dumps({
                               'target_profile': 'confirmed_next_open_fixed_exit_fill_cost_v2',
                               'evaluation_protocol': 'walk_forward_purged_v2',
                               'oos_start_date': '2026-07-01', 'purge_gap_days': 5,
                               'execution_contract': contract,
                           }), created_at='2026-07-01T00:00:00+00:00')
            db.add_all([symbol, run]); db.flush()
            run_id = run.id
            db.add(Prediction(model_run_id=run.id, symbol_id=symbol.id,
                              trade_date='2026-07-01', score=0.2, rank_value=1,
                              created_at='2026-07-01T00:00:00+00:00'))
            db.commit()
        history = [dict(date=f'2026-07-{day:02}', open=100.0, high=110.0,
                        low=99.0, close=105.0, volume=100000.0)
                   for day in range(1, 23)]
        if verified_prices:
            from app.services.market_calendar import is_market_open_date
            optional = (dict(suspended=False, upper_limit=120.0, lower_limit=80.0)
                        if optional_facts else {})
            history = [dict(row, price_basis='raw', corporate_action_status='none',
                            execution_source_reference='synthetic-golden-v1', **optional)
                       for row in history if is_market_open_date(market, row['date'])]
        if missing_open:
            for row in history:
                row.pop('open')
        service = CNMarketSchedulerService() if market == 'CN' else USMarketSchedulerService()
        with patch('app.services.model_evaluation.load_lake_price_history', return_value=history):
            service._run_structured_evaluation(source_job_id=parent_id)
        with SessionLocal() as db:
            row = db.scalar(select(ModelEvaluation).where(ModelEvaluation.model_run_id == run_id))
            self.assertIsNotNone(row, 'Scheduler must persist an auditable evaluation receipt')
            payload = list_latest_model_evaluations(db, market=market, limit=1)
            self.assertEqual(1, len(payload), 'Unverified receipt must remain visible to API readers')
            return payload[0]

    def assert_blocked_without_silent_fallback(self, payload):
        self.assertEqual('partial', payload['status'],
                         'Missing frozen contract must not become successful legacy evaluation')
        self.assertFalse(payload['summary']['execution_verified'])
        self.assertNotEqual('eligible_for_champion_review', payload['activation_status'])
        ledger = payload['summary'].get('candidate_outcomes')
        self.assertIsInstance(ledger, list, 'Each candidate/horizon needs a persisted reason')
        self.assertEqual(5, len(ledger))
        self.assertEqual({1, 3, 5, 10, 20}, {x['horizon_days'] for x in ledger})
        self.assertTrue(all(x['reason'] and x['net_return'] is None for x in ledger))
        self.assertEqual(0, payload['sample_count'])

    def test_cn_missing_contract_does_not_fall_back_to_legacy(self):
        self.assert_blocked_without_silent_fallback(self.exercise('CN'))

    def test_us_missing_contract_does_not_fall_back_to_legacy(self):
        self.assert_blocked_without_silent_fallback(self.exercise('US'))

    def test_cn_missing_open_cannot_use_close_as_fill(self):
        self.assert_blocked_without_silent_fallback(self.exercise('CN', missing_open=True))

    def test_us_missing_open_cannot_use_close_as_fill(self):
        self.assert_blocked_without_silent_fallback(self.exercise('US', missing_open=True))

    def assert_valid_contract_computes(self, market):
        from app.services.execution_costs import FillCostModel
        from app.services.execution_reconciliation import execution_contract, VERSION
        contract = execution_contract(market, FillCostModel(8, 12))
        payload = self.exercise(market, contract=contract, verified_prices=True)
        ledger = payload['summary']['candidate_outcomes']
        self.assertEqual(5, len(ledger))
        five = next(row for row in ledger if row['horizon_days'] == 5)
        self.assertEqual('CLOSED', five['status'])
        # Independent arithmetic, not the shared fee helper as oracle.
        expected = (105 * (1 - .0012) * (1 - .0008) / (100 * (1 + .0012) * (1 + .0008))) - 1
        self.assertAlmostEqual(expected, five['net_return'], places=12)
        self.assertEqual(VERSION, payload['summary']['outcome_protocol'])
        self.assertFalse(payload['summary']['legacy_fallback_used'])

    def test_cn_valid_contract_has_successful_trade_not_blanket_block(self):
        self.assert_valid_contract_computes('CN')

    def test_us_valid_contract_has_successful_trade_not_blanket_block(self):
        self.assert_valid_contract_computes('US')

    def test_cn_v2_optional_suspension_and_bounds_absent_still_computes(self):
        from app.services.execution_costs import FillCostModel
        from app.services.execution_reconciliation import execution_contract
        payload = self.exercise('CN', contract=execution_contract('CN', FillCostModel(8, 12)),
                                verified_prices=True, optional_facts=False)
        five = next(row for row in payload['summary']['candidate_outcomes']
                    if row['horizon_days'] == 5)
        self.assertEqual('CLOSED', five['status'])
        self.assertTrue(five['optional_evidence_gaps'])
