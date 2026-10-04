"""Presentation layer: bilingual text seam, shared fragments, page styles.

- ``i18n.t(lang, zh, en)``          -- the single seam for inline bilingual text
- ``fragments``                     -- side-effect-free HTML snippets (facade: app.api.rendering)
- ``styles_dashboard`` / ``styles_screener`` -- page-level CSS constants
"""

from app.api.presentation.fragments import (
    compact_text,
    mini_trend_bars,
    render_daily_change_chip,
)
from app.api.presentation.i18n import t

__all__ = ["compact_text", "mini_trend_bars", "render_daily_change_chip", "t"]
