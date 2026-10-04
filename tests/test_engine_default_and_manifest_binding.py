from __future__ import annotations

import inspect
from pathlib import Path
from unittest import TestCase

from app.services.adjustment_snapshot import (
    BASE_ADJUSTMENT_VERSION,
    actions_snapshot_sha256,
    adjustment_version_binding,
)
from app.services.backtester import BacktestRunner

ROOT = Path(__file__).resolve().parents[1]


class EngineDefaultTests(TestCase):
    """A2: one default engine across entry points + manifest bound to snapshots."""

    def test_backtest_runner_defaults_to_event_engine(self) -> None:
        signature = inspect.signature(BacktestRunner.run)
        default = signature.parameters["engine_version"].default
        self.assertEqual("event_driven_daily_v2", default)

    def test_background_and_auto_analysis_pass_the_engine_explicitly(self) -> None:
        jobs = (ROOT / "app" / "api" / "routes" / "jobs.py").read_text(encoding="utf-8")
        auto = (ROOT / "app" / "services" / "auto_analysis.py").read_text(encoding="utf-8")
        self.assertIn('BacktestRunner().run(top_n=max(1, top_n), engine_version="event_driven_daily_v2")', jobs)
        self.assertIn('BacktestRunner().run(top_n=config["top_n"], engine_version="event_driven_daily_v2")', auto)
        self.assertIn('engine_version="event_driven_daily_v2"', (ROOT / "app" / "services" / "us_market_scheduler.py").read_text(encoding="utf-8"))
        self.assertIn('engine_version="event_driven_daily_v2"', (ROOT / "scripts" / "run_backtest.py").read_text(encoding="utf-8"))

    def test_no_bare_runner_calls_without_engine_version(self) -> None:
        """Every BacktestRunner call site must pin the engine explicitly.

        EventDrivenBacktestRunner calls are exempt: that runner has no engine
        switch (it is the event engine itself).
        """

        import re

        offenders: list[str] = []
        for base in ("app", "scripts"):
            for path in (ROOT / base).rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                uses_legacy_runner = re.search(r"^\s*\w*runner\w*\s*=\s*BacktestRunner\(\)\s*$", text, re.MULTILINE)
                for match in re.finditer(r"(?<![A-Za-z_])BacktestRunner\(\)\.run\(", text):
                    window = text[match.start() : match.start() + 600]
                    if "engine_version" not in window:
                        line = text[: match.start()].count("\n") + 1
                        offenders.append(f"{path.relative_to(ROOT)}:{line}")
                if uses_legacy_runner:
                    for match in re.finditer(r"\.run\(", text):
                        prefix = text[max(0, match.start() - 40) : match.start()]
                        if "EventDriven" in prefix or "BacktestRunner()" in prefix:
                            continue
                        window = text[match.start() : match.start() + 600]
                        if "engine_version" not in window:
                            line = text[: match.start()].count("\n") + 1
                            offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual([], offenders)

    def test_adjustment_version_binds_actions_and_view_hashes(self) -> None:
        binding = adjustment_version_binding("CN")
        self.assertTrue(binding["adjustment_version"].startswith(BASE_ADJUSTMENT_VERSION))
        self.assertIn("actions=", binding["adjustment_version"])
        self.assertIn("view=", binding["adjustment_version"])
        self.assertIsNotNone(binding["actions_snapshot_sha256"])
        self.assertIsNotNone(binding["adjusted_view_sha256"])
        self.assertEqual(64, len(binding["actions_snapshot_sha256"]))
        self.assertEqual(64, len(binding["adjusted_view_sha256"]))
        # deterministic across calls
        self.assertEqual(binding, adjustment_version_binding("CN"))
        # same helper reads the on-disk store
        self.assertEqual(binding["actions_snapshot_sha256"], actions_snapshot_sha256("CN"))

    def test_runner_manifest_writes_bound_versions(self) -> None:
        source = (ROOT / "app" / "services" / "backtesting" / "runner.py").read_text(encoding="utf-8")
        for token in ('"actions_snapshot_sha256"', '"adjusted_view_sha256"', "adjustment_version_binding("):
            self.assertIn(token, source)
        # the static constant must no longer be written directly
        self.assertNotIn("ADJUSTMENT_VERSION", source)
