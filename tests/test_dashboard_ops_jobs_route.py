"""Lightweight job-history page HTTP entry with fixed recent-job inputs."""

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import inspect
import json
from pathlib import Path
import re
import unittest
from unittest.mock import ANY, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.dashboard import ops
from app.api.presentation.styles_dashboard import RESULT_SUMMARY_STYLE
from app.core.db import get_db_session


class PageContract(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.words = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, sorted(attrs)))

    def handle_data(self, data):
        self.words.append(data)

    def digest(self):
        value = (self.tags, re.sub(r"\s+", " ", " ".join(self.words)).strip())
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def fixture_jobs(*, populated=True, hostile=False):
    if not populated:
        return []
    suffix = "<script>alert(1)</script>" if hostile else ""
    return [
        {
            "id": 31, "job_type": f"screener_precompute{suffix}", "status": f"success{suffix}",
            "started_at": "2026-10-02T18:00:00+08:00", "finished_at": "2026-10-02T18:02:00+08:00",
            "params": {"market": f"CN{suffix}"}, "message": f"Core and tail phases scheduled{suffix}",
            "result": {"count": 2, "failed_count": 0, "tail_jobs_scheduled": True,
                       "snapshots_created": [{"model_template": f"lightgbm_top_picks{suffix}"}]},
        },
        {
            "id": 30, "job_type": "social_us_price_sync", "status": "partial",
            "started_at": "2026-10-02T17:00:00+08:00", "finished_at": "2026-10-02T17:01:00+08:00",
            "params": {"provider": "yfinance"}, "message": "one failed",
            "result": {"success_count": 9, "failure_count": 1, "failed_tickers": ["FIX"]},
        },
        {
            "id": 29, "job_type": ops.DECOMMISSIONED_CN_REVIEW_JOB_TYPE, "status": "success",
            "started_at": "2026-10-02T16:00:00+08:00", "finished_at": None,
            "params": {}, "message": "retired", "result": {},
        },
    ]


def render_jobs(*, lang="en", populated=True, hostile=False, authenticated=True, lookback=3):
    app = FastAPI()
    app.include_router(ops.router)
    db = MagicMock()
    app.dependency_overrides[get_db_session] = lambda: db
    jobs = fixture_jobs(populated=populated, hostile=hostile)
    original = deepcopy(jobs)
    with patch.object(ops, "is_authenticated", return_value=authenticated), patch.object(
        ops, "load_recent_jobs_summary", return_value=jobs
    ) as loader, TestClient(app) as client:
        response = client.get("/dashboard/ops/jobs", params={"lang": lang, "lookback_runs": lookback},
                              follow_redirects=False)
    if jobs != original:
        raise AssertionError("Rendering mutated recent jobs")
    return response, loader


class DashboardOpsJobsRouteTests(unittest.TestCase):
    def test_language_rows_filter_actions_and_lookback(self):
        for lang in ("en", "zh"):
            for lookback, expected in ((3, 3), (999, 5)):
                response, loader = render_jobs(lang=lang, lookback=lookback)
                self.assertEqual(200, response.status_code)
                loader.assert_called_once_with(ANY, limit=20)
                self.assertIn("任务记录" if lang == "zh" else "Job History", response.text)
                self.assertIn("screener_precompute", response.text)
                self.assertIn("social_us_price_sync", response.text)
                self.assertNotIn("retired", response.text)
                self.assertIn("/jobs/precompute-cn-screeners-core", response.text)
                self.assertIn(f"lookback_runs={expected}", response.text)

    def test_empty_state_keeps_stage_controls(self):
        response, _ = render_jobs(populated=False)
        self.assertIn("No jobs yet", response.text)
        self.assertIn("Run Full Precompute Chain", response.text)
        self.assertIn("No job record yet", response.text)

    def test_external_values_are_escaped(self):
        response, _ = render_jobs(hostile=True)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)

    def test_auth_short_circuits_loader(self):
        response, loader = render_jobs(authenticated=False)
        self.assertIn(response.status_code, (302, 303, 307))
        loader.assert_not_called()

    def test_named_template_matches_approved_render_contract(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "dashboard_ops_jobs_render_contract.json").read_text()
        )
        actual = {}
        for lang in ("en", "zh"):
            for populated in (False, True):
                response, _ = render_jobs(lang=lang, populated=populated)
                actual[f"{lang}-{populated}"] = PageContract(response.text).digest()
        self.assertEqual(fixture, actual)

    def test_mobile_breakpoint_collapses_sidebar_and_contains_wide_table(self):
        style = RESULT_SUMMARY_STYLE
        self.assertIn("@media (max-width:960px)", style)
        self.assertIn(".app { grid-template-columns:1fr; }", style)
        self.assertIn(".table-wrap { width:100%; overflow-x:auto;", style)

    def test_route_is_orchestration_only_and_template_is_named(self):
        source = inspect.getsource(ops.dashboard_ops_jobs_page)
        self.assertNotIn("<tr>", source)
        self.assertNotIn("fragments=", source)
        template = Path(__file__).parents[1] / "app/api/templates/dashboard/ops_jobs.html"
        self.assertTrue(template.exists())
        self.assertNotIn("fragments[", template.read_text())
        self.assertNotIn("|safe", template.read_text())
