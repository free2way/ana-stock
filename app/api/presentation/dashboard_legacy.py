from __future__ import annotations

from markupsafe import Markup

from app.api.presentation.templates import render_template


def render_dashboard_legacy_page(
    template_name: str,
    *,
    fragments: list[object],
) -> str:
    """Provide a template boundary for dashboard pages awaiting component migration."""
    return render_template(
        template_name,
        fragments=[Markup(str(fragment)) for fragment in fragments],
    )
