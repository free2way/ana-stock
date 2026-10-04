"""Named task history/detail templates and shared job display helpers."""

from functools import lru_cache
import html
import json

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import (
    DASHBOARD_OPS_HISTORY_PAGE_STYLE,
    DASHBOARD_OPS_JOB_DETAIL_STYLE,
)
from app.api.presentation.templates import template_environment
from app.services.dashboard_insights import _display_time


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def task_duration(job: dict | None) -> str:
    seconds = (job or {}).get("duration_seconds")
    if seconds is None:
        return "-"
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "-"
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def task_status_view(status: str, *, lang: str) -> dict[str, str]:
    normalized = str(status or "idle").lower()
    labels = {
        "success": "完成" if lang == "zh" else "Done",
        "partial": "部分完成" if lang == "zh" else "Partial",
        "failed": "失败" if lang == "zh" else "Failed",
        "running": "运行中" if lang == "zh" else "Running",
        "not_configured": "未配置" if lang == "zh" else "Not configured",
        "empty": "无新增数据" if lang == "zh" else "No new data",
        "failed_timeout": "超时失败" if lang == "zh" else "Timed out",
        "cancelled": "已取消" if lang == "zh" else "Cancelled",
        "idle": "待运行" if lang == "zh" else "Pending",
    }
    return {"css_class": normalized, "label": labels.get(normalized, normalized)}


def render_task_status_badge(status: str, *, lang: str) -> str:
    """Compatibility renderer for legacy ops pages not migrated yet."""
    view = task_status_view(status, lang=lang)
    return (
        f"<span class='job-status {html.escape(view['css_class'])}'>"
        f"{html.escape(view['label'])}</span>"
    )


def render_ops_history_page(
    *, rows: list[dict], selected_date: str, lang: str, lookback_runs: int, nav_html: str,
) -> str:
    prepared = []
    for source in rows:
        job = dict(source)
        job.update(
            started_display=_display_time(job.get("started_at")),
            finished_display=_display_time(job.get("finished_at")),
            duration_display=task_duration(job),
            status_view=task_status_view(str(job.get("status") or "idle"), lang=lang),
        )
        prepared.append(job)
    return _environment().get_template("dashboard/ops_history.html").render(
        lang=lang,
        t=t,
        rows=prepared,
        selected_date=selected_date,
        lookback_runs=lookback_runs,
        nav_html=Markup(nav_html),
        style=Markup(DASHBOARD_OPS_HISTORY_PAGE_STYLE),
    )


def render_ops_job_detail_page(*, job_id: int, job: dict, lang: str, nav_html: str) -> str:
    job_type = str(job.get("job_type") or "")
    lineage = {
        "definition": job.get("definition"),
        "dependencies": job.get("dependencies") or [],
        "attempts": job.get("attempts") or [],
        "market_refresh_batches": job.get("market_refresh_batches") or [],
        "quality_summary": job.get("quality_summary"),
    }
    return _environment().get_template("dashboard/ops_job_detail.html").render(
        lang=lang,
        t=t,
        job_id=job_id,
        job=job,
        job_type=job_type,
        show_report_link=job_type in {"generate_ai_daily_report", "send_ai_daily_report", "ai_daily_report"},
        status_view=task_status_view(str(job.get("status") or "idle"), lang=lang),
        started_display=_display_time(job.get("started_at")),
        finished_display=_display_time(job.get("finished_at")),
        duration_display=task_duration(job),
        lineage_json=json.dumps(lineage, ensure_ascii=False, indent=2, default=str),
        params_json=json.dumps(job.get("params") or {}, ensure_ascii=False, indent=2, default=str),
        result_json=json.dumps(job.get("result") or {}, ensure_ascii=False, indent=2, default=str),
        nav_html=Markup(nav_html),
        style=Markup(DASHBOARD_OPS_JOB_DETAIL_STYLE),
    )
