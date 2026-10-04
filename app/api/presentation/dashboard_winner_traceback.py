"""Named winner-traceback template context and navigation, without data access."""

from functools import lru_cache
from urllib.parse import urlencode

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import TEMPLATE_HREF_STYLE
from app.api.presentation.templates import template_environment
from app.services.dashboard_insights import _fmt_optional_float


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def render_winner_traceback_page(
    *, summary: dict, market: str, lang: str, min_hits: int, nav_html: str,
    snapshot_meta: dict, bucket_labels: dict[str, str],
) -> str:
    def market_label(value):
        code = str(value or "").upper()
        return {"CN": t(lang, "A股", "CN"), "US": t(lang, "美股", "US"),
                "ALL": t(lang, "全部市场", "All Markets")}.get(code, code or "-")

    def filters(options, key, selected):
        return [{"active": value == selected, "label": label,
                 "href": "/dashboard/model-performance/winner-traceback?" + urlencode(
                     {"lang": lang, "market": market, "min_hits": min_hits} | {key: value}
                 )} for value, label in options]

    rows = []
    for item in summary["rows"][:80]:
        item_market = str(item.get("market") or "").upper()
        hits = list(item.get("hits") or [])[:4]
        links = []
        for hit in hits:
            links.append({
                "label": str(hit.get("template_label") or hit.get("template") or "-"),
                "href": "/screeners?" + urlencode({
                    "lang": lang, "run": 1,
                    "model_template": str(hit.get("template") or "").strip() or "technical_momentum",
                    "market": item_market if item_market in {"CN", "US"} else market,
                    "universe": "full_market", "min_trend_score": 10,
                    "confluence_action_filter": str(hit.get("action_bucket") or "ALL").strip() or "ALL",
                }),
            })
        rows.append({
            "ticker": str(item.get("ticker") or "-"),
            "href": f"/insights/{str(item.get('ticker') or '')}?lang={lang}",
            "name": str(item.get("name") or "-"), "market": market_label(item_market),
            "signal_date": str(item.get("signal_date") or "-"),
            "winner_date": str(item.get("winner_date") or "-"),
            "return_1d": item.get("return_1d"), "hit_count": int(item.get("hit_count") or 0),
            "links": links,
            "actions": " / ".join(
                bucket_labels.get(str(hit.get("action_bucket") or ""), str(hit.get("action_bucket") or ""))
                for hit in hits if str(hit.get("action_bucket") or "").strip()
            ) or "-",
        })
    return _environment().get_template("dashboard/winner_traceback.html").render(
        lang=lang, market=market, nav_html=Markup(nav_html), style=Markup(TEMPLATE_HREF_STYLE),
        t=t, fmt=_fmt_optional_float, summary=summary, rows=rows,
        market_label=market_label(market),
        source_note=t(lang, "后台快照", "Background snapshot")
        if str(snapshot_meta.get("source") or "") == "snapshot" else t(lang, "实时回退", "Live fallback"),
        market_options=filters((("CN", t(lang, "A股", "CN")), ("US", t(lang, "美股", "US")),
                                ("ALL", t(lang, "全部", "All"))), "market", market),
        hit_filters=filters(((0, t(lang, "全部强票", "All winners")),
                             (1, t(lang, "至少命中1个模型", "At least 1 hit")),
                             (2, t(lang, "至少命中2个模型", "At least 2 hits"))), "min_hits", min_hits),
        markets=[(market_label(label), count) for label, count in summary["markets"]],
    )
