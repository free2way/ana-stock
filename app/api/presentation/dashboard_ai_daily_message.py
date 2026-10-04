"""Named AI daily-report push-text page context."""

from functools import lru_cache

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import DASHBOARD_AI_DAILY_REPORT_MESSAGE_STYLE
from app.api.presentation.templates import template_environment


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def render_ai_daily_message_page(*, lang: str, nav_html: str, message: str) -> str:
    lang = "zh" if lang == "zh" else "en"
    return _environment().get_template("dashboard/ai_daily_report_message.html").render(
        lang=lang,
        t=t,
        nav_html=Markup(nav_html),
        style=Markup(DASHBOARD_AI_DAILY_REPORT_MESSAGE_STYLE),
        message=message,
    )
