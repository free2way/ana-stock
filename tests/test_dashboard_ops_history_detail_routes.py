"""Task history/detail HTTP entries with isolated repository inputs."""

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import inspect
import json
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.dashboard import ops
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


def history_jobs(*, populated=True, hostile=False):
    suffix = "<script>alert(1)</script>" if hostile else ""
    if not populated:
        return []
    return [
        {
            "id": 21, "job_type": f"refresh_cn_market_data{suffix}",
            "started_at": "2026-10-02T09:00:00+08:00", "finished_at": "2026-10-02T09:01:05+08:00",
            "duration_seconds": 65, "status": "success", "message": f"refresh complete{suffix}",
        },
        {
            "id": 20, "job_type": "screener_precompute", "started_at": "2026-10-02T08:00:00+08:00",
            "finished_at": None, "duration_seconds": None, "status": "running", "message": "working",
        },
        {
            "id": 19, "job_type": ops.DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
            "started_at": "2026-10-02T07:00:00+08:00", "status": "success", "message": "retired",
        },
        {
            "id": 18, "job_type": "prior_day", "started_at": "2026-10-01T22:00:00+08:00",
            "status": "failed", "message": "old",
        },
    ]


def detail_job(*, ai=False, hostile=False):
    suffix = "<script>alert(1)</script>" if hostile else ""
    return {
        "id": 41,
        "job_type": "generate_ai_daily_report" if ai else f"refresh_us_grouped_daily{suffix}",
        "status": "success",
        "message": f"refresh complete{suffix}",
        "started_at": "2026-10-02T10:00:00+08:00",
        "finished_at": "2026-10-02T10:02:03+08:00",
        "duration_seconds": 123,
        "params": {"market": f"US{suffix}"},
        "result": {"rows_written": 200, "note": suffix},
        "definition": {"markets": ["US"]},
        "dependencies": [{"status": "satisfied"}],
        "attempts": [{"status": "success"}],
        "market_refresh_batches": [{"provider": "polygon"}],
        "quality_summary": {"coverage": 1.0},
    }


class FakeJobRepository:
    detail = None

    def __init__(self, db):
        self.db = db

    def _serialize_job(self, job):
        return dict(job)

    def get_job_detail(self, job_id):
        return deepcopy(self.detail) if self.detail is not None and job_id == 41 else None


def _test_client(*, jobs=None, detail=None, authenticated=True):
    app = FastAPI()
    app.include_router(ops.router)
    db = MagicMock()
    db.scalars.return_value.all.return_value = deepcopy(jobs or [])
    app.dependency_overrides[get_db_session] = lambda: db
    FakeJobRepository.detail = deepcopy(detail)
    auth = patch.object(ops, "is_authenticated", return_value=authenticated)
    repo = patch.object(ops, "DataJobRepository", FakeJobRepository)
    auth.start()
    repo.start()
    client = TestClient(app)
    return client, db, auth, repo


def render_history(*, lang="en", populated=True, hostile=False, authenticated=True, lookback=7):
    jobs = history_jobs(populated=populated, hostile=hostile)
    original = deepcopy(jobs)
    client, db, auth, repo = _test_client(jobs=jobs, authenticated=authenticated)
    try:
        response = client.get(
            "/dashboard/ops/history",
            params={"lang": lang, "lookback_runs": lookback, "date": "2026-10-02"},
            follow_redirects=False,
        )
    finally:
        client.close()
        repo.stop()
        auth.stop()
    if jobs != original:
        raise AssertionError("History rendering mutated jobs")
    return response, db


def render_detail(*, lang="en", ai=False, hostile=False, missing=False, retired=False, authenticated=True):
    job = None if missing else detail_job(ai=ai, hostile=hostile)
    if retired and job is not None:
        job["job_type"] = ops.DECOMMISSIONED_CN_REVIEW_JOB_TYPE
    original = deepcopy(job)
    client, db, auth, repo = _test_client(detail=job, authenticated=authenticated)
    try:
        response = client.get(f"/dashboard/ops/job/{41 if not missing else 404}", params={"lang": lang},
                              follow_redirects=False)
    finally:
        client.close()
        repo.stop()
        auth.stop()
    if job != original:
        raise AssertionError("Detail rendering mutated job")
    return response, db


class DashboardOpsHistoryDetailRouteTests(unittest.TestCase):
    def test_history_language_filter_empty_and_links(self):
        for lang in ("en", "zh"):
            response, _ = render_history(lang=lang)
            self.assertEqual(200, response.status_code)
            self.assertIn("任务历史" if lang == "zh" else "Job History", response.text)
            self.assertIn("#21", response.text)
            self.assertIn("#20", response.text)
            self.assertNotIn("#19", response.text)
            self.assertNotIn("#18", response.text)
            self.assertIn(f"/dashboard/ops/job/21?lang={lang}", response.text)
        empty, _ = render_history(populated=False)
        self.assertIn("No jobs recorded for this date.", empty.text)

    def test_history_escapes_external_values(self):
        response, _ = render_history(hostile=True)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)

    def test_detail_language_lineage_and_ai_link(self):
        for lang in ("en", "zh"):
            response, _ = render_detail(lang=lang)
            self.assertEqual(200, response.status_code)
            visible_text = " ".join(PageContract(response.text).words)
            self.assertIn("输入与执行追溯" if lang == "zh" else "Input & execution lineage", visible_text)
            self.assertIn("refresh_us_grouped_daily", response.text)
            self.assertIn("2m 3s", response.text)
            self.assertNotIn("Open today’s AI report", response.text)
        ai_response, _ = render_detail(ai=True)
        self.assertIn("Open today’s AI report", ai_response.text)
        self.assertIn("/dashboard/ai-daily-report?lang=en", ai_response.text)

    def test_detail_escapes_external_values(self):
        response, _ = render_detail(hostile=True)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)

    def test_named_templates_match_pre_refactor_contracts(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "dashboard_ops_history_detail_render_contract.json").read_text()
        )
        actual = {}
        for lang in ("en", "zh"):
            for populated in (False, True):
                response, _ = render_history(lang=lang, populated=populated)
                actual[f"history-{lang}-{populated}"] = PageContract(response.text).digest()
            for ai in (False, True):
                response, _ = render_detail(lang=lang, ai=ai)
                actual[f"detail-{lang}-{ai}"] = PageContract(response.text).digest()
        self.assertEqual(fixture, actual)

    def test_missing_retired_and_auth_are_blocked(self):
        missing, _ = render_detail(missing=True)
        retired, _ = render_detail(retired=True)
        history_auth, history_db = render_history(authenticated=False)
        detail_auth, detail_db = render_detail(authenticated=False)
        self.assertEqual(404, missing.status_code)
        self.assertEqual(404, retired.status_code)
        self.assertIn(history_auth.status_code, (302, 303, 307))
        self.assertIn(detail_auth.status_code, (302, 303, 307))
        history_db.scalars.assert_not_called()
        detail_db.scalars.assert_not_called()

    def test_routes_are_orchestration_only_and_templates_are_named(self):
        for function in (ops.dashboard_ops_history_page, ops.dashboard_ops_job_detail):
            self.assertNotIn("fragments=", inspect.getsource(function))
        root = Path(__file__).parents[1] / "app/api/templates/dashboard"
        for name in ("ops_history.html", "ops_job_detail.html"):
            template = root / name
            self.assertTrue(template.exists())
            self.assertNotIn("fragments[", template.read_text())
            self.assertNotIn("|safe", template.read_text())
