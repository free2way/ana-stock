"""Shared, side-effect-free presentation fragments (no financial decisions).

This module is now a stable facade over :mod:`app.api.presentation`, the real
presentation layer:

- ``app.api.presentation.i18n``      -- bilingual text seam (``t``)
- ``app.api.presentation.fragments`` -- small shared HTML snippets
- ``app.api.presentation.styles_*``  -- page-level CSS constants

Import from here (or from ``app.api.presentation`` directly); both stay in
sync. Function identity is preserved, so ``is``-based regression contracts
keep passing.
"""

from __future__ import annotations

from app.api.presentation.fragments import (
    compact_text,
    mini_trend_bars,
    render_daily_change_chip,
)
from app.api.presentation.i18n import t

__all__ = ["compact_text", "mini_trend_bars", "render_daily_change_chip", "t"]
