import hashlib
import unittest
from app.services.stock_selection.us_halt_observations import assess_nyse_halts, URL


def receipt(body):
    return dict(url=URL,http_status=200,body=body,sha256=hashlib.sha256(body.encode()).hexdigest(),
                collected_at='2026-09-28T11:00:00+00:00')


HEADER = 'Halt Date,Halt Time,Symbol,Name,Exchange,Reason,Resume Date,NYSE Resume Time\n'


class HaltObservationTests(unittest.TestCase):
    def test_every_row_accounted_no_invented_resume(self):
        body = HEADER + ('2026-09-24,09:31:00,A,A,Nasdaq,LULD pause,2026-09-24,09:36:00\n'
                         '2026-09-24,09:31:00,B,B,NYSE,News Pending,,\n'
                         '2026-09-24,09:31:00,C,C,NYSE,News Pending,See Subsequent Halt,See Subsequent Halt\n')
        result = assess_nyse_halts(receipt(body))
        self.assertEqual(result['row_count'],sum(result['status_counts'].values()))
        self.assertEqual(result['status_counts'],{'OBSERVED_INTERVAL':1,'REVIEW_REQUIRED':2})
        self.assertFalse(result['training_authorized'])
        self.assertFalse(result['negative_event_inference_allowed'])
        self.assertEqual(result['observations'][1]['raw']['Resume Date'],'')

    def test_duplicate_and_bad_time_are_explicit(self):
        row = '2026-09-24,09:31:00,A,A,NYSE,LULD pause,2026-09-24,09:30:00\n'
        result = assess_nyse_halts(receipt(HEADER+row+row))
        self.assertEqual(result['issue_counts']['resume_before_halt'],2)
        self.assertEqual(result['issue_counts']['duplicate_source_row'],1)

    def test_source_schema_and_hash_fail_closed(self):
        for r in (dict(receipt(HEADER),url='https://other.test'),
                  dict(receipt(HEADER),body=HEADER+'tampered'),receipt('wrong,header\n'),
                  dict(receipt(HEADER),http_status=403)):
            with self.assertRaises(ValueError):assess_nyse_halts(r)
