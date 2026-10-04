"""Homepage must never run expensive evaluation while rendering a request."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch


class HomeEvaluationTests(unittest.TestCase):
    def test_home_renderer_explicitly_disables_evaluation_compute(self):
        path = Path(__file__).resolve().parents[1] / 'app/api/routes/dashboard/home.py'
        module = ast.parse(path.read_text())
        renderer = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == '_render_dashboard_workspace')
        calls = [n for n in ast.walk(renderer) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'build_lightgbm_prediction_evaluation']
        self.assertTrue(calls)
        for call in calls:
            flags = [k.value for k in call.keywords if k.arg == 'allow_compute']
            self.assertEqual(len(flags), 1)
            self.assertIsInstance(flags[0], ast.Constant)
            self.assertIs(flags[0].value, False)

    def test_empty_cache_never_opens_database_or_runs_loader(self):
        from app.services import template_evaluation as service
        with patch.object(service, 'get_cached', return_value=None), \
             patch.object(service, 'get_or_set', side_effect=AssertionError('compute forbidden')), \
             patch.object(service, 'SessionLocal', side_effect=AssertionError('DB forbidden')):
            self.assertEqual(service.build_lightgbm_prediction_evaluation(market='ALL', allow_compute=False), {})
