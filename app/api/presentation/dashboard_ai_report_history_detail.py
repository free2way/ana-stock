"""Named archived AI-report detail template and view preparation."""

from functools import lru_cache
from urllib.parse import quote

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import REPORT_POOL_TABLE_ROWS_STYLE
from app.api.presentation.templates import template_environment
from app.services.dashboard_insights import (
    _display_time,
    _fmt_optional_float,
    _outcome_status_label,
)


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def render_ai_report_history_detail_page(
    *, snapshot: dict, report: dict, message: str, outcome_rows: list[dict],
    outcome_summary: str, actionable_rows: list[dict], model_guidance_card_html: str,
    lang: str, nav_html: str,
) -> str:
    prepared_outcomes = []
    for source in outcome_rows:
        row = dict(source)
        row.update(
            name=str(row.get("name") or row.get("ticker") or "-"),
            ticker=str(row.get("ticker") or "-"),
            baseline_date=str(row.get("baseline_date") or "-"),
            baseline_close=_fmt_optional_float(row.get("baseline_close"), digits=3),
            latest_date=str(row.get("latest_date") or "-"),
            latest_close=_fmt_optional_float(row.get("latest_close"), digits=3),
            return_text=_fmt_optional_float(row.get("return_pct"), suffix="%", digits=2),
            status_label=_outcome_status_label(row.get("status"), lang=lang),
        )
        prepared_outcomes.append(row)

    prepared_actions = []
    for index, source in enumerate(actionable_rows, start=1):
        row = dict(source)
        ticker = str(row.get("ticker") or "")
        row.update(
            index=index,
            ticker=ticker,
            ticker_path=quote(ticker, safe="._-"),
            name=str(row.get("name") or ticker or "-"),
            verdict=str(row.get("verdict") or "-"),
            pool_reason=str(row.get("report_pool_reason") or "-"),
            quant_rank=str(row.get("quant_rank") or "-"),
            verification_score=str(row.get("verification_score") or "-"),
            readiness_score=str(row.get("trade_readiness_score") or "-"),
            readiness_bucket=str(row.get("readiness_bucket") or "-"),
            entry_trigger=str(row.get("entry_trigger") or "-"),
            invalidation=str(row.get("invalidation_condition") or "-"),
            latest_price=_fmt_optional_float(row.get("latest_price") or row.get("latest_close"), digits=3),
            buy_zone_delta=_fmt_optional_float(row.get("close_vs_buy_zone_high_pct"), suffix="%", digits=1),
            headline=str(row.get("headline") or row.get("summary") or "-"),
        )
        prepared_actions.append(row)

    execution_bias = report.get("lightgbm_execution_bias") or {}
    return _environment().get_template("dashboard/ai_daily_report_history_detail.html").render(
        lang=lang,
        t=t,
        snapshot=snapshot,
        report=report,
        message=message,
        execution_bias=execution_bias,
        outcome_rows=prepared_outcomes,
        outcome_summary=outcome_summary,
        actionable_rows=prepared_actions,
        saved_at=_display_time(snapshot.get("created_at"), with_tz=True),
        model_guidance_card_html=Markup(model_guidance_card_html),
        nav_html=Markup(nav_html),
        style=Markup(REPORT_POOL_TABLE_ROWS_STYLE),
    )
