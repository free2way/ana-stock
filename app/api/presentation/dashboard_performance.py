from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlencode

from markupsafe import Markup
from jinja2 import StrictUndefined

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import MARKET_LABEL_STYLE
from app.api.presentation.templates import template_environment
from app.services.dashboard_insights import _fmt_optional_float
from app.services.template_evaluation import lightgbm_bias, technical_momentum_bias


_HTML_FIELDS = frozenset({
    "nav_html", "selected_title", "selected_subtitle", "run_options_html",
    "market_options_html", "summary_cards_html", "training_diagnostic_html",
    "overview_cards_html", "reason_jump_html", "structured_evaluation_rows_html",
    "aggregate_rows_html", "aggregate_regime_rows_html", "aggregate_sector_rows_html",
    "regime_rows_html", "sector_rows_html", "watchlist_cards_html",
    "watchlist_rows_html", "recent_rows_html", "detail_rows_html",
})


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def render_recent_run_rows(
    runs_with_summaries: list[tuple[dict, dict | None]], *,
    lang: str, market: str, top_n: int, max_trade_dates: int,
) -> str:
    rows = []
    for item, summary in runs_with_summaries:
        summary = summary or {}
        rows.append({
            "id": int(item["id"]),
            "name": str(item.get("name") or "-"),
            "market": str(item.get("market") or "-"),
            "universe": str(item.get("universe") or "-"),
            "latest_trade_date": str(summary.get("latest_trade_date") or "-"),
            "pick_count": summary.get("pick_count") or 0,
            "windows": summary.get("windows") or {},
            "href": "/dashboard/model-performance?" + urlencode({
                "lang": lang, "run_id": int(item["id"]), "market": market,
                "top_n": top_n, "max_trade_dates": max_trade_dates,
            }),
        })
    return _environment().get_template("dashboard/components/performance_recent_runs.html").render(
        rows=rows, lang=lang, t=t, fmt=_fmt_optional_float,
    )


def render_model_performance_page(*, view: dict) -> str:
    """Render named page data, trusting only HTML made by our presenters.

    Scalar values use Jinja escaping. StrictUndefined catches missing context
    fields rather than silently rendering an incomplete evaluation page.
    """
    context = dict(view)
    lang = context["lang"]
    for name in _HTML_FIELDS:
        context[name] = Markup(str(context[name]))
    for name in ("guidance_ui", "evaluation_ui"):
        context[name] = {
            key: Markup(str(value))
            if (key.endswith("_html") or key.endswith("_row") or "_short_cycle_" in key)
            else value
            for key, value in context[name].items()
        }
    if not context["summary_cards_html"]:
        context["summary_cards_html"] = Markup("<div class='muted'>{}</div>").format(
            t(lang, "暂无统计结果。", "No stats yet.")
        )
    if not context["watchlist_cards_html"]:
        context["watchlist_cards_html"] = Markup("<div class='muted'>{}</div>").format(
            t(lang, "暂无自选表现统计。", "No watchlist performance stats yet.")
        )
    context.update(
        t=t, int=int, str=str, _fmt_optional_float=_fmt_optional_float,
        MARKET_LABEL_STYLE=Markup(MARKET_LABEL_STYLE),
        technical_bias=technical_momentum_bias(context["technical_momentum_eval"], lang=lang),
        lightgbm_bias_text=lightgbm_bias(context["lightgbm_eval"], lang=lang),
    )
    return _environment().get_template("dashboard/model_performance.html").render(**context)
