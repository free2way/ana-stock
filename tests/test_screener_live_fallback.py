"""Live-screener fallback: snapshot miss must render rows, not an empty page.

The fallback re-runs ``ScreenerService.screen`` for the requested parameter set
when both the wide precompute snapshot and the exact-parameter snapshot are
missing, and the rows it returns must be indistinguishable from the precomputed
path after the query layer is done with them.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.routes import screener as screener_route
from app.services.runtime_cache import clear_all
from app.services.stock_selection import screener_query


def _live_rows() -> list[dict]:
    """Rows shaped like ``ScreenerService.screen`` output for a CN query."""
    return [
        {
            "ticker": "LIMITUP",
            "market": "CN",
            "trend_score": 90,
            "volume_ratio": 2.0,
            "pe_ttm": 15.0,
            "limit_up_today": True,
            "tradability_status": "READY",
            "trade_readiness_score": 80.0,
        },
        {
            "ticker": "KEEP",
            "market": "CN",
            "trend_score": 70,
            "volume_ratio": 2.0,
            "pe_ttm": 15.0,
            "tradability_status": "READY",
            "trade_readiness_score": 80.0,
        },
        {
            "ticker": "BLOCKED",
            "market": "CN",
            "trend_score": 95,
            "volume_ratio": 2.0,
            "pe_ttm": 15.0,
            "tradability_status": "BLOCKED",
            "trade_readiness_score": 10.0,
        },
    ]


def _query_params() -> dict:
    return screener_query.normalize_screen_params(
        {
            "model_template": "technical_momentum",
            "universe": "watchlist",
            "market": "CN",
            "min_trend_score": 0,
            "tradability_status": "READY",
        }
    )


def _rank_ready_service() -> MagicMock:
    """Service double whose ranking collaborators are pass-throughs."""
    service = MagicMock()
    service._apply_snapshot_persistence_filter.side_effect = lambda values, **_kwargs: values
    service._apply_model_signal_filter.side_effect = lambda values, **_kwargs: values
    service._apply_execution_tag_filter.side_effect = lambda values, **_kwargs: values
    service._sort_results.side_effect = lambda values, **_kwargs: values
    return service


def _settings(*, enabled: bool = True, ttl: float = 90.0, timeout: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        screener_live_fallback_enabled=enabled,
        screener_live_fallback_ttl_seconds=ttl,
        screener_live_fallback_timeout_seconds=timeout,
    )


class ScreenerLiveFallbackQueryTests(unittest.TestCase):
    def test_snapshot_miss_returns_live_rows_instead_of_an_empty_result(self) -> None:
        params = _query_params()
        rows, ready = screener_query.load_screen_rows_from_snapshot(
            MagicMock(),
            params,
            snapshot_loader=lambda _params: None,
            live_screen_loader=lambda _service, _params: _live_rows(),
        )

        self.assertTrue(ready)
        self.assertEqual(
            ["KEEP", "LIMITUP"],
            [row["ticker"] for row in rows or []],
        )

    def test_live_rows_keep_the_precomputed_query_semantics(self) -> None:
        params = _query_params()

        precomputed = screener_query.load_precomputed_screener_rows(
            _rank_ready_service(),
            params,
            snapshot_loader=lambda _params: _live_rows(),
            apply_limit=False,
        )
        live, ready = screener_query.load_screen_rows_from_snapshot(
            MagicMock(),
            params,
            snapshot_loader=lambda _params: None,
            live_screen_loader=lambda _service, _params: _live_rows(),
            apply_limit=False,
        )

        self.assertIsNotNone(precomputed)
        self.assertTrue(ready)
        # Same gate (BLOCKED dropped by the tradability whitelist) and same
        # CN limit-up demotion as the precomputed path.
        self.assertEqual(
            [row["ticker"] for row in precomputed or []],
            [row["ticker"] for row in live or []],
        )

    def test_disabled_fallback_keeps_the_snapshot_only_behaviour(self) -> None:
        rows, ready = screener_query.load_screen_rows_from_snapshot(
            MagicMock(),
            _query_params(),
            snapshot_loader=lambda _params: None,
            live_screen_loader=None,
        )

        self.assertIsNone(rows)
        self.assertFalse(ready)

    def test_unavailable_live_result_is_reported_as_not_ready(self) -> None:
        rows, ready = screener_query.load_screen_rows_from_snapshot(
            MagicMock(),
            _query_params(),
            snapshot_loader=lambda _params: None,
            live_screen_loader=lambda _service, _params: None,
        )

        self.assertIsNone(rows)
        self.assertFalse(ready)
        self.assertFalse(
            screener_query.screen_snapshot_ready(
                MagicMock(),
                _query_params(),
                snapshot_loader=lambda _params: None,
                live_screen_loader=lambda _service, _params: None,
            )
        )

    def test_screen_snapshot_ready_reports_the_live_fallback(self) -> None:
        for function in (
            lambda: screener_query.screen_snapshot_ready(
                MagicMock(),
                _query_params(),
                snapshot_loader=lambda _params: None,
                live_screen_loader=lambda _service, _params: _live_rows(),
            ),
            lambda: bool(
                screener_query.run_screen(
                    MagicMock(),
                    _query_params(),
                    snapshot_loader=lambda _params: None,
                    live_screen_loader=lambda _service, _params: _live_rows(),
                )
            ),
        ):
            with self.subTest(function=function):
                self.assertTrue(function())

    def test_live_params_are_projected_onto_the_screen_keywords(self) -> None:
        params = _query_params()
        params["min_hit_probability"] = 0.4
        params["strategy_profile"] = "quality_confluence_v1"
        service = MagicMock()
        service.screen.return_value = [{"ticker": "AAA"}]

        rows = screener_query.live_screen_rows(service, params)

        self.assertEqual([{"ticker": "AAA"}], rows)
        kwargs = service.screen.call_args.kwargs
        self.assertEqual("technical_momentum", kwargs["model_template"])
        self.assertNotIn("lang", kwargs)
        self.assertNotIn("min_hit_probability", kwargs)
        self.assertNotIn("strategy_profile", kwargs)


class ScreenerLiveFallbackRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all()

    def tearDown(self) -> None:
        clear_all()

    def test_loader_is_disabled_by_config(self) -> None:
        with patch.object(screener_route, "get_settings", return_value=_settings(enabled=False)):
            self.assertIsNone(screener_route._live_screen_fallback_loader())

    def test_loader_screens_once_per_ttl_and_persists_the_snapshot(self) -> None:
        calls: list[dict] = []

        def _fake_screen(service: object, params: dict) -> list[dict]:
            calls.append(params)
            return _live_rows()

        with (
            patch.object(screener_route, "get_settings", return_value=_settings()),
            patch.object(screener_route, "_live_screen_rows_service", side_effect=_fake_screen),
            patch.object(screener_route, "_persist_live_screen_snapshot") as persist,
        ):
            loader = screener_route._live_screen_fallback_loader()
            self.assertIsNotNone(loader)
            params = _query_params()
            first = loader(MagicMock(), params)
            second = loader(MagicMock(), params)

        self.assertEqual(["LIMITUP", "KEEP", "BLOCKED"], [row["ticker"] for row in first or []])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, persist.call_count)
        self.assertEqual(["LIMITUP", "KEEP", "BLOCKED"], [row["ticker"] for row in second or []])
        # Callers must not share the memoised row objects.
        self.assertIsNot(first, second)

    def test_loader_returns_nothing_when_the_screen_outruns_its_budget(self) -> None:
        def _slow_screen(_service: object, _params: dict) -> list[dict]:
            time.sleep(0.4)
            return _live_rows()

        with (
            patch.object(screener_route, "get_settings", return_value=_settings(timeout=0.02)),
            patch.object(screener_route, "_live_screen_rows_service", side_effect=_slow_screen),
            patch.object(screener_route, "_persist_live_screen_snapshot") as persist,
        ):
            loader = screener_route._live_screen_fallback_loader()
            self.assertIsNone(loader(MagicMock(), _query_params()))

        self.assertEqual(0, persist.call_count)


if __name__ == "__main__":
    unittest.main()
