"""Page/export parity through real HTTP routes using fixed snapshot inputs."""

import csv
import hashlib
import json
from contextlib import ExitStack
from copy import deepcopy
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.dashboard import concepts, performance
from app.core.db import get_db_session
from app.services.continuous_leaders import build_continuous_leader_view


def fixture_rows():
    return [
        {"ticker": ticker, "name": f"Name {ticker}", "market": market,
         "hits": hits, "runs": 5, "score": score, "score_history": history,
         "signal_label": signal, "signal_strength": strength, "execution_tags": tags,
         "trade_date": "2026-09-30", "percentile": 90.0, "model_reward_risk_ratio": 1.8,
         "conviction_bucket": "observe", "position_size_hint": "10%", "entry_style": "pullback"}
        for ticker, market, hits, score, history, signal, strength, tags in (
            ("FIX-A", "CN", 3, 0.8, [0.2, 0.8], "BUY", 70, ["gap-risk", "earnings-soon"]),
            ("FIX-B", "US", 3, 0.8, [0.9, 0.8], " buy ", 80, ["thin-liquidity"]),
            ("FIX-C", "HK", 2, 0.9, [0.5, 0.9], "WATCH", 60, []),
            ("FIX-D", "CN", 1, 0.6, [], "HOLD", 20, ["gap-risk"]),
        )
    ]


WATCHLIST = {
    "FIX-A": {"sync_enabled": True, "sync_status": "success"},
    "FIX-B": {"sync_enabled": True, "sync_status": "failed"},
    "FIX-C": {"sync_enabled": False},
}


class TickerTable(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.in_body = False
        self.tickers = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self.in_body = True
        if self.in_body and tag == "a":
            href = dict(attrs).get("href", "")
            if href.startswith("/insights/"):
                ticker = href.split("/")[-1].split("?")[0]
                if ticker not in self.tickers:
                    self.tickers.append(ticker)

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_body = False


def render_routes(params=None, *, snapshot_rows=None, fallback_rows=None, authenticated=True):
    app = FastAPI()
    app.include_router(concepts.router)
    app.include_router(performance.router)
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    rows = fixture_rows() if snapshot_rows is None else snapshot_rows
    summary = {"market_context": {"continuous_leaders": fallback_rows or []}}
    snapshot = {"payload": {"rows": rows}}
    with ExitStack() as stack:
        loaders = []
        for module in (concepts, performance):
            stack.enter_context(patch.object(module, "is_authenticated", return_value=authenticated))
            loaders.append(stack.enter_context(patch.object(module, "_load_home_summary", return_value=summary)))
            stack.enter_context(patch.object(module, "load_latest_workspace_snapshot", return_value=snapshot))
            repo = stack.enter_context(patch.object(module, "WatchlistRepository")).return_value
            repo.get_or_create_default.return_value = SimpleNamespace(id=1)
            repo.list_ticker_map.return_value = deepcopy(WATCHLIST)
        with TestClient(app) as client:
            page = client.get("/dashboard/continuous-leaders", params=params or {}, follow_redirects=False)
            export = client.get("/dashboard/continuous-leaders/export", params=params or {}, follow_redirects=False)
        calls = [loader.call_args for loader in loaders]
    return page, export, calls


class ContinuousLeadersRouteTests(unittest.TestCase):
    def test_page_and_csv_match_pre_refactor_contract(self):
        from tests.test_dashboard_winner_traceback import PageContract

        baseline = json.loads((Path(__file__).parent / "fixtures/continuous_leaders_render_contract.json").read_text())
        actual = {}
        for lang in ("zh", "en"):
            for field in ("hits", "score", "signal", "trend", "ticker"):
                for order in ("asc", "desc"):
                    page, export, _ = render_routes({"lang": lang, "continuous_sort_by": field,
                                                    "continuous_sort_order": order})
                    actual[f"{lang}-{field}-{order}"] = {
                        "page": PageContract(page.text).digest(),
                        "csv": hashlib.sha256(export.content).hexdigest(),
                    }
        self.assertEqual(baseline, actual)

    def test_selection_does_not_mutate_cached_rows_and_risk_examples_keep_source_order(self):
        rows = fixture_rows()
        original = deepcopy((rows, WATCHLIST))
        view = build_continuous_leader_view(rows, WATCHLIST, sort_by="ticker")
        self.assertEqual(original, (rows, WATCHLIST))
        self.assertTrue(all("continuous_state_key" not in row for row in rows))
        self.assertEqual(["FIX-A", "FIX-B", "FIX-D"], [row["ticker"] for row in view["risk_examples"]])
        self.assertEqual(3, view["tagged_names"])
        self.assertEqual(("gap-risk", 2), view["risk_top_tags"][0])
        self.assertEqual(["FIX-D", "FIX-C", "FIX-B", "FIX-A"], [row["ticker"] for row in view["rows"]])

    def test_page_and_csv_match_expected_order_for_all_sorts_and_directions(self):
        expected = {"hits": ["FIX-B", "FIX-A", "FIX-C", "FIX-D"],
                    "score": ["FIX-C", "FIX-B", "FIX-A", "FIX-D"],
                    "signal": ["FIX-B", "FIX-A", "FIX-C", "FIX-D"],
                    "trend": ["FIX-A", "FIX-C", "FIX-D", "FIX-B"],
                    "ticker": ["FIX-D", "FIX-C", "FIX-B", "FIX-A"]}
        for lang in ("zh", "en"):
            for field, descending in expected.items():
                for order in ("asc", "desc"):
                    with self.subTest(lang=lang, field=field, order=order):
                        page, export, _ = render_routes({"lang": lang, "continuous_sort_by": field,
                                                        "continuous_sort_order": order})
                        wanted = descending if order == "desc" else list(reversed(descending))
                        self.assertEqual(200, page.status_code)
                        self.assertEqual(200, export.status_code)
                        self.assertEqual(wanted, TickerTable(page.text).tickers)
                        data = list(csv.DictReader(StringIO(export.text)))
                        self.assertEqual(wanted, [row["ticker"] for row in data])
                        self.assertEqual({"READY", "WAITING", "IN", "OFF"}, {row["continuous_state"] for row in data})
                        self.assertIn("text/csv", export.headers["content-type"])
                        self.assertIn("continuous_leaders_5runs.csv", export.headers["content-disposition"])

    def test_filters_and_watchlist_states_apply_identically(self):
        cases = [({"continuous_market": "cn"}, ["FIX-A", "FIX-D"]),
                 ({"continuous_market": "HK"}, ["FIX-C"]),
                 ({"continuous_state": "READY"}, ["FIX-A"]),
                 ({"continuous_state": "WAITING"}, ["FIX-B"]),
                 ({"continuous_state": "IN"}, ["FIX-C"]),
                 ({"continuous_state": "OFF"}, ["FIX-D"]),
                 ({"continuous_signal": "buy", "min_signal_strength": 75}, ["FIX-B"]),
                 ({"execution_tag_filter": " gap-risk,thin-liquidity "}, ["FIX-B", "FIX-A", "FIX-D"]),
                 ({"exclude_execution_tag_filter": "GAP-RISK"}, ["FIX-B", "FIX-C"]),
                 ({"execution_tag_filter": "gap-risk", "exclude_execution_tag_filter": "earnings-soon"}, ["FIX-D"]),
                 ({"continuous_market": "CN", "min_signal_strength": 90}, [])]
        for params, wanted in cases:
            with self.subTest(params=params):
                page, export, _ = render_routes(params)
                self.assertEqual(wanted, TickerTable(page.text).tickers)
                self.assertEqual(wanted, [row["ticker"] for row in csv.DictReader(StringIO(export.text))])

    def test_snapshot_precedence_empty_fallback_clamp_and_auth(self):
        fallback = [fixture_rows()[-1]]
        page, export, calls = render_routes({"lookback_runs": 999}, snapshot_rows=[], fallback_rows=fallback)
        self.assertEqual(["FIX-D"], TickerTable(page.text).tickers)
        self.assertEqual("FIX-D", list(csv.DictReader(StringIO(export.text)))[0]["ticker"])
        self.assertEqual(calls[0].kwargs, calls[1].kwargs)
        page, _, _ = render_routes(fallback_rows=fallback)
        self.assertEqual(4, len(TickerTable(page.text).tickers))
        page, export, _ = render_routes(snapshot_rows=[])
        self.assertEqual([], TickerTable(page.text).tickers)
        self.assertEqual([], list(csv.DictReader(StringIO(export.text))))
        page, export, calls = render_routes(authenticated=False)
        for response in (page, export):
            self.assertIn(response.status_code, (302, 303, 307))
            self.assertIn("/login", response.headers["location"])
        self.assertEqual([None, None], calls)
