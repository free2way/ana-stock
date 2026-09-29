"""Offline safety tests; no database or real provider calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location('source_audit', Path(__file__).resolve().parents[1] / 'scripts/audit_execution_source_capabilities.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class CapabilityAuditTests(unittest.TestCase):
    def test_suspension_observations_preserve_unknown_resume_and_all_candidates(self):
        rows = [{'SECUCODE':'600001.SH','SUSPEND_START_TIME':'2026-09-01 09:30:00',
                 'SUSPEND_END_TIME':None,'PREDICT_RESUME_DATE':'2026-10-01'},
                {'SECUCODE':'920229.BJ'}, {'SECUCODE':'200016.SZ'}]
        body = json.dumps({'success':True,'result':{'data':rows}})
        record = dict(http_status=200, response_body=body,
                      response_sha256=audit.hashlib.sha256(body.encode()).hexdigest(),
                      collected_at='2026-09-28T00:00:00+00:00', provider='eastmoney',
                      endpoint='https://example.test', params={'date':'2026-09-01'})
        result = audit.suspension_observations(record)
        self.assertEqual(result['input_count'],len(result['observations'])+len(result['exclusions']))
        observation = result['observations'][0]
        self.assertEqual(observation['symbol'],'600001.SS')
        self.assertIsNone(observation['reported_end'])
        self.assertFalse(observation['actual_resume_verified'])
        self.assertNotIn('suspended',observation)
        self.assertFalse(result['daily_state_generation_allowed'])
        self.assertEqual(observation['source']['response_sha256'],record['response_sha256'])
        with self.assertRaises(ValueError):
            audit.suspension_observations(dict(record,response_body='{}'))

    def test_pagination_url_rejects_foreign_host_and_path(self):
        base = 'https://api.polygon.io/stocks/v1/dividends'
        for url in ('http://api.polygon.io/stocks/v1/dividends',
                    'https://evil.test/stocks/v1/dividends',
                    'https://api.polygon.io/other',
                    'https://user:secret@api.polygon.io/stocks/v1/dividends'):
            with self.assertRaises(ValueError):
                audit.safe_next_url(url, base)
        self.assertEqual(audit.safe_next_url(base + '?cursor=abc&apiKey=secret', base), base + '?cursor=abc')

    def test_event_pages_fail_closed(self):
        def record(events, **kw):
            return {'http_status': 200, 'response_body': json.dumps(dict(status='OK', results=events, **kw))}
        event = {'ticker': 'AAPL', 'id': 'one'}
        good = record([event])
        self.assertEqual(audit.check_event_pages([good], 'AAPL')['status'], 'PAGINATION_COMPLETE')
        self.assertFalse(audit.check_event_pages([good], 'AAPL')['negative_event_inference_allowed'])
        for pages in ([], [good, good], [record([event], next_url='more')],
                      [record([{'ticker':'MSFT','id':'one'}])], [{'http_status':403}],
                      [{'http_status':200,'response_body':'not json'}]):
            self.assertEqual(audit.check_event_pages(pages, 'AAPL')['status'], 'BLOCKED')

    def test_redacts_secret_preserves_provenance_and_never_authorizes_training(self):
        settings = SimpleNamespace(alpaca_api_key='test-key', alpaca_api_secret='test-secret',
                                   polygon_api_key='polygon-secret', alpaca_data_feed='iex')
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = SimpleNamespace(status_code=200,
            text='{"message":"test-key test-secret polygon-secret"}', headers={'x-request-id':'request-1'})
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'receipt.json'
            with patch.object(audit, 'get_settings', return_value=settings), patch.object(audit.httpx, 'Client', return_value=client), patch.object(audit.sys, 'argv', ['audit', '--output', str(output)]), contextlib.redirect_stdout(io.StringIO()):
                audit.main()
            text = output.read_text()
            for secret in ('test-key', 'test-secret', 'polygon-secret'):
                self.assertNotIn(secret, text)
            data = json.loads(text)
            self.assertFalse(data['training_authorized_by_receipt'])
            self.assertEqual(len(data['records']), 7)
            for record in data['records']:
                self.assertFalse(record['coverage_verified'])
                self.assertTrue(record['collected_at'])
                self.assertEqual(record['response_sha256'], audit.hashlib.sha256(record['response_body'].encode()).hexdigest())
            original = output.read_bytes()
            with patch.object(audit.sys, 'argv', ['audit', '--output', str(output)]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                audit.main()
            self.assertEqual(original, output.read_bytes())


if __name__ == '__main__':
    unittest.main()
