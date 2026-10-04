"""Named template and view preparation for the lightweight job-history page."""

from collections.abc import Callable
from functools import lru_cache
import json

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import RESULT_SUMMARY_STYLE
from app.api.presentation.templates import template_environment
from app.services.dashboard_insights import _display_time
from app.services.display_compaction import compact_job_type, compact_json_summary, compact_label


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def status_badge_view(status: object) -> dict[str, str]:
    value = str(status or "")
    background, foreground = {
        "success": ("#dcfce7", "#166534"),
        "failed": ("#fee2e2", "#991b1b"),
        "partial": ("#fef3c7", "#92400e"),
        "running": ("#dbeafe", "#1d4ed8"),
    }.get(value, ("#e5e7eb", "#374151"))
    return {"label": value, "background": background, "foreground": foreground}


def job_result_summary(
    job: dict,
    *,
    lang: str,
    summarize_precompute: Callable[..., dict],
) -> str:
    job_type = str(job.get("job_type") or "").lower()
    if job_type.startswith("screener_precompute"):
        summary = summarize_precompute(job, lang=lang)
        return f"{summary['summary']} · {summary['detail']}"

    result = job.get("result") or {}
    if not isinstance(result, dict):
        return "-"
    if "snapshots_created" in result:
        created = list(result.get("snapshots_created") or [])
        failed_count = int(result.get("failed_count") or 0)
        total = len(created) + failed_count
        return f"{len(created)}/{total}" if total else "-"
    if job_type == "social_us_price_sync":
        success_count = int(result.get("success_count") or 0)
        failure_count = int(result.get("failure_count") or 0)
        failed_tickers = [
            str(item).upper()
            for item in (result.get("failed_tickers") or [])
            if str(item).strip()
        ]
        base = (
            f"成功 {success_count} / 失败 {failure_count}"
            if lang == "zh"
            else f"{success_count} success / {failure_count} failed"
        )
        if failed_tickers:
            return base + (
                f"；失败代码：{', '.join(failed_tickers[:5])}"
                if lang == "zh"
                else f"; failed tickers: {', '.join(failed_tickers[:5])}"
            )
        return base
    return compact_json_summary(result, 48)


def render_ops_jobs_page(
    *,
    recent_jobs: list[dict],
    stage_rows: list[dict],
    actions_html: str,
    summarize_precompute: Callable[..., dict],
    lang: str,
    lookback_runs: int,
    nav_html: str,
) -> str:
    prepared_stages = []
    for source in stage_rows:
        stage = dict(source)
        stage["status_view"] = status_badge_view(stage.get("status"))
        prepared_stages.append(stage)

    prepared_jobs = []
    for source in recent_jobs:
        job = dict(source)
        result_summary = job_result_summary(
            job,
            lang=lang,
            summarize_precompute=summarize_precompute,
        )
        params = job.get("params")
        job.update(
            job_type_display=compact_job_type(job.get("job_type"), 22),
            status_view=status_badge_view(job.get("status")),
            started_display=_display_time(job.get("started_at")),
            finished_display=_display_time(job.get("finished_at")),
            params_title=json.dumps(params, ensure_ascii=False) if params else "-",
            params_display=compact_json_summary(params, 52),
            message_title=str(job.get("message") or "-"),
            message_display=compact_label(job.get("message") or "-", 42),
            result_summary=result_summary,
        )
        prepared_jobs.append(job)

    return _environment().get_template("dashboard/ops_jobs.html").render(
        lang=lang,
        t=t,
        jobs=prepared_jobs,
        stage_rows=prepared_stages,
        lookback_runs=lookback_runs,
        nav_html=Markup(nav_html),
        actions_html=Markup(actions_html),
        style=Markup(RESULT_SUMMARY_STYLE),
    )
