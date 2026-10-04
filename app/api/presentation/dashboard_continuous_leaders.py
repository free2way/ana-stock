"""Continuous-leader page context; selection is owned by the service."""

from functools import lru_cache
from urllib.parse import urlencode

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import SORT_LINK_STYLE
from app.api.presentation.templates import template_environment


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def render_continuous_leaders_page(
    *, params: dict, view: dict, rows: list[dict], labels: dict,
    nav_html: str, lookback_html: str,
) -> str:
    def sort_link(field):
        order = "asc" if params["continuous_sort_by"] == field and params["continuous_sort_order"] == "desc" else "desc"
        return "/dashboard/continuous-leaders?" + urlencode(
            params | {"continuous_sort_by": field, "continuous_sort_order": order}
        )

    prepared = []
    for source in rows:
        row = dict(source)
        details = []
        if row.get("percentile") is not None:
            details.append(f"Pct {float(row['percentile']):.1f}%")
        if row.get("model_reward_risk_ratio") is not None:
            details.append(f"R/R {float(row['model_reward_risk_ratio']):.2f}")
        details.extend(str(row[key]) for key in ("conviction_bucket", "position_size_hint", "entry_style") if row.get(key))
        if row.get("execution_tags"):
            details.append(" / ".join(row["execution_tags"][:2]))
        row.update(
            score_text=f"{row['score']:.4f}", score_details=" · ".join(details),
            signal_strength=int(row.get("signal_strength") or 0),
            badge_html=Markup(row["badge_html"]), trend_html=Markup(row["trend_html"]),
        )
        prepared.append(row)
    return _environment().get_template("dashboard/continuous_leaders.html").render(
        **params, t=t, labels=labels, view=view, rows=prepared,
        style=Markup(SORT_LINK_STYLE), nav_html=Markup(nav_html), lookback_html=Markup(lookback_html),
        sort_link=sort_link, max_top_n=max(len(rows), 1), top_n=min(max(len(rows), 1), 3),
        tickers_csv=",".join(item["ticker"] for item in rows),
        language_links=[{"lang": lang, "href": "/dashboard/continuous-leaders?" + urlencode(params | {"lang": lang}),
                         "active": (params["lang"] != "zh" if lang == "en" else params["lang"] == "zh")}
                        for lang in ("en", "zh")],
    )
