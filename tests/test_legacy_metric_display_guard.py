from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from app.services.adjustment_snapshot import BASE_ADJUSTMENT_VERSION
from app.services.backtesting.runner import CALENDAR_VERSION
from app.services.market_calendar import is_market_open_date

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
FORBIDDEN_DISPLAY = ("sharpe", "annualized", "年化", "夏普")


class LegacyMetricDisplayGuardTests(TestCase):
    """E-1: uninterpretable legacy metrics never surface on pages or exports."""

    def test_no_template_renders_uninterpretable_metrics(self) -> None:
        templates = list((APP_ROOT / "api" / "templates").rglob("*.html"))
        self.assertGreater(len(templates), 0)
        offenders: list[str] = []
        for path in templates:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            for token in FORBIDDEN_DISPLAY:
                if token in text:
                    offenders.append(f"{path.name}:{token}")
        self.assertEqual([], offenders)

    def test_route_modules_do_not_render_uninterpretable_metrics(self) -> None:
        offenders: list[str] = []
        for path in (APP_ROOT / "api" / "routes").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            for token in FORBIDDEN_DISPLAY:
                if token in text:
                    offenders.append(f"{path.name}:{token}")
        self.assertEqual([], offenders)

    def test_legacy_backtester_declares_unverified_status(self) -> None:
        source = (APP_ROOT / "services" / "backtester.py").read_text(encoding="utf-8")
        self.assertIn('"engine_status": "legacy_unverified"', source)
        self.assertIn("not interpretable", source)

    def test_event_engine_manifest_carries_all_versions(self) -> None:
        # E-2: engine / cost / calendar / adjustment versions travel with the run.
        source = (APP_ROOT / "services" / "backtesting" / "runner.py").read_text(encoding="utf-8")
        for token in ("engine_version", "cost_model_version", "calendar_version", "adjustment_version"):
            self.assertIn(f'"{token}"', source, token)
        self.assertEqual("market_calendar_2027_v2", CALENDAR_VERSION)
        self.assertEqual("raw_prices_with_actions_v1", BASE_ADJUSTMENT_VERSION)
        # The calendar version constant tracks the working calendar.
        self.assertTrue(is_market_open_date("CN", "2026-10-09"))
