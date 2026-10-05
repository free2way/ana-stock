"""Isolate the stock-selection artifact root for unit tests.

``run_multi_screen`` / ``_build_multi_screen_rows_from_snapshots`` load the newest
reliability metadata and probability-calibration artifacts from
``get_settings().artifacts_dir`` whenever the request omits explicit values.  The
real ``data/artifacts/stock_selection_research`` tree may hold live producer
artifacts, so tests that pin the *fallback* behaviour (equal weights, ``None``
probability, abstention on uncalibrated rows) must point the loader at an empty
directory instead of relying on the checkout being artifact-free.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.stock_selection import reliability_artifacts


@contextmanager
def empty_artifacts_dir():
    """Resolve the default artifact root to a fresh empty directory for the block."""

    with tempfile.TemporaryDirectory() as directory:
        settings = SimpleNamespace(artifacts_dir=Path(directory))
        with patch.object(reliability_artifacts, "get_settings", return_value=settings):
            yield Path(directory)


class IsolatedArtifactsTestCase:
    """Mixin giving every test in the case a fresh, empty artifact root."""

    def setUp(self) -> None:  # noqa: D102 - unittest hook
        super().setUp()
        context = empty_artifacts_dir()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
