"""Weekly review, realtime monitor, premarket plan and AI daily report routes."""

import html

import json

import re

from collections import Counter

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

from fastapi.responses import HTMLResponse, JSONResponse

from sqlalchemy.orm import Session

from app.api.presentation.i18n import t
from app.api.presentation.dashboard_legacy import render_dashboard_legacy_page
from app.api.presentation.dashboard_ai_daily_message import render_ai_daily_message_page
from app.api.presentation.dashboard_ai_report_history_detail import render_ai_report_history_detail_page

from app.api.presentation.styles_dashboard import (
    DASHBOARD_WEEKLY_REVIEW_STYLE,
    PRICE_SOURCE_TEXT_STYLE,
    BUY_ZONE_TEXT_STYLE,
    AI_REPORT_NAME_CELL_STYLE,
    WINDOW_PILL_STYLE,
)

from app.core.db import get_db_session

from app.services.ai_daily_report import (
    _report_text_with_security_names,
    _report_ticker_labels,
    build_trade_explain_text,
    format_trade_status,
    list_ai_daily_report_history,
    load_ai_daily_report_history_item,
    render_ai_daily_report_message,
)

from app.services.auth import is_authenticated, login_redirect

from app.services.kronos_validation import load_latest_kronos_validation

from app.services.market_lake import (
    load_lake_price_history,
)


from app.services.portfolio_book import (
    load_portfolio_positions,
    trade_reason_label,
)

from app.services.price_snapshot import load_latest_close

from app.services.realtime_quotes import load_cn_intraday_bars, load_us_intraday_bars, load_us_latest_trades



from app.services.social_signals import social_signal_summary

from app.services.template_evaluation import (
    aggregate_window_stats as _aggregate_window_stats,
)

from app.services.ui_lang import resolve_request_lang

from app.services.workspace_nav import render_workspace_nav_html


from app.api.routes.dashboard._common import _dashboard_watchlist_map, _display_job_message, _hydrate_ai_report_names, _load_cached_ai_daily_report, _reason_screen_href
from app.services.dashboard_insights import (
    _audit_conclusion_for_trade,
    _build_weekly_review_summary,
    _display_time,
    _fmt_optional_float,
    _forward_return_from_history,
    _report_market_rows,
    _report_outcome_rows,
    _report_outcome_summary,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

















@router.get("/weekly-review", response_class=HTMLResponse)
def dashboard_weekly_review(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/weekly-review")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="daily_report")
    summary = _build_weekly_review_summary(db, lang=lang)
    mood_counts = summary.get("mood_counts") or {}
    mood_chips = "".join(
        f"<span class='pill'>{html.escape(str(label))} · {int(count)}</span>"
        for label, count in sorted(mood_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ) or f"<span class='muted'>{t(lang, '本周还没有日报记录。', 'No AI report entries this week.')}</span>"
    repeated_rows_html = "".join(
        "<tr>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{int(item.get('count') or 0)}</td>"
        f"<td>{html.escape(str(item.get('latest_verdict') or '-'))}</td>"
        "</tr>"
        for item in (summary.get('repeated_top_tickers') or [])
    ) or f"<tr><td colspan='3'>{t(lang, '本周没有重复入选的 Top 候选。', 'No repeated Top picks this week.')}</td></tr>"
    run_rows_html = "".join(
        "<tr>"
        f"<td><a href='/dashboard/model-performance?lang={lang}&run_id={int(item.get('id') or 0)}'>#{int(item.get('id') or 0)}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('market') or '-'))}<div class='muted'>{html.escape(str(item.get('latest_trade_date') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('window_3') or {}).get('avg_return')), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('window_3') or {}).get('hit_rate')), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('window_5') or {}).get('avg_return')), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('window_5') or {}).get('hit_rate')), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('window_10') or {}).get('avg_return')), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('window_10') or {}).get('hit_rate')), suffix='%', digits=1)}</div></td>"
        "</tr>"
        for item in (summary.get('run_rows') or [])
    ) or f"<tr><td colspan='5'>{t(lang, '本周还没有成功模型 run。', 'No successful model runs this week.')}</td></tr>"
    weekly_jobs_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('job_type') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('status') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('started_at') or '-'))}</td>"
        f"<td>{html.escape(_display_job_message(item.get('message'), lang=lang))}</td>"
        "</tr>"
        for item in (summary.get('partial_or_failed_jobs') or [])
    ) or f"<tr><td colspan='4'>{t(lang, '本周没有失败或部分完成的任务。', 'No failed or partial jobs this week.')}</td></tr>"
    trade_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('price'), digits=3)}</td>"
        f"<td>{_fmt_optional_float(item.get('quantity'), digits=0)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl'), digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl_pct'), suffix='%', digits=2)}</td>"
        f"<td>{html.escape(trade_reason_label(item.get('reason'), lang=lang))}</td>"
        "</tr>"
        for item in (summary.get('trade_rows') or [])
    ) or f"<tr><td colspan='7'>{t(lang, '本周还没有卖出记录。', 'No portfolio sell records this week.')}</td></tr>"
    advice_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('bucket_label') or '-'))}</td>"
        f"<td>{int(item.get('count') or 0)}</td>"
        f"<td>{int(item.get('winner_count') or 0)}</td>"
        f"<td>{_fmt_optional_float(item.get('win_rate'), suffix='%', digits=1)}</td>"
        f"<td>{_fmt_optional_float(item.get('avg_return'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl'), digits=2)}</td>"
        "</tr>"
        for item in (summary.get('advice_effectiveness_rows') or [])
    ) or f"<tr><td colspan='6'>{t(lang, '本周还没有足够的卖出记录来评估建议有效性。', 'Not enough portfolio exits this week to evaluate advice effectiveness yet.')}</td></tr>"
    unresolved_trade_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl'), digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl_pct'), suffix='%', digits=2)}</td>"
        f"<td><a class='pill' href='/portfolio?lang={lang}'>{t(lang, '去持仓页补录', 'Fix in Portfolio')}</a></td>"
        "</tr>"
        for item in (summary.get('unresolved_trade_rows') or [])
    ) or f"<tr><td colspan='5'>{t(lang, '本周没有待补录原因的卖出记录。', 'No weekly sell records are missing a structured reason.')}</td></tr>"
    audited_trade_rows_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('action_hint_at_exit') or '-'))}<div class='muted'>{html.escape(str(item.get('action_priority_at_exit') or '-'))} · {html.escape(str(item.get('risk_tag_at_exit') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('action_reason_at_exit') or '-'))}<div class='muted'>{html.escape(str(item.get('rebalance_action_at_exit') or '-'))}</div></td>"
            f"<td>{html.escape(trade_reason_label(item.get('reason'), lang=lang))}</td>"
            f"<td>{html.escape(_audit_conclusion_for_trade(item, lang=lang)[0])}<div class='muted'>{html.escape(_audit_conclusion_for_trade(item, lang=lang)[1])}</div></td>"
            f"<td>{_fmt_optional_float(item.get('realized_pnl_pct'), suffix='%', digits=2)}<div class='muted'>"
            f"3D {_fmt_optional_float(item.get('post_sell_return_3d'), suffix='%', digits=2)} · "
            f"5D {_fmt_optional_float(item.get('post_sell_return_5d'), suffix='%', digits=2)} · "
            f"10D {_fmt_optional_float(item.get('post_sell_return_10d'), suffix='%', digits=2)}</div></td>"
            "</tr>"
        )
        for item in (summary.get('audited_trade_rows') or [])
    ) or f"<tr><td colspan='7'>{t(lang, '本周还没有带建议快照的卖出记录。新卖出会自动进入审计链。', 'No weekly exits carry an advice snapshot yet. New exits will enter the audit chain automatically.')}</td></tr>"
    job_counts = summary.get("job_status_counts") or {}
    model_windows = summary.get("model_window_summary") or {}
    trade_summary = summary.get("trade_summary") or {}
    audit_summary = summary.get("audit_summary") or {}
    structured_reason_summary = summary.get("structured_reason_summary") or {}
    report_window_summary = summary.get("report_window_summary") or {}
    recommendation_validation_rows_html = "".join(
        "<tr>"
        f"<td><a href='{html.escape(str(item.get('href') or '#'), quote=True)}'>{html.escape(str(item.get('label') or '-'))}</a><div class='muted'>{html.escape(str(item.get('title') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('note') or '-'))}</td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        "</tr>"
        for item in (summary.get("recommendation_validation_rows") or [])
    ) or f"<tr><td colspan='6'>{t(lang, '本周还没有足够的推荐验证样本。', 'Not enough recommendation validation samples yet this week.')}</td></tr>"
    structured_reason_note = ""
    coverage_pct = structured_reason_summary.get("coverage_pct")
    if coverage_pct is not None and float(coverage_pct) < 60.0:
        structured_reason_note = (
            "<div class='muted' style='margin-top:8px;color:#f6c177;'>"
            + (
                f"当前仅有 {float(coverage_pct):.1f}% 的本周卖出记录带结构化原因，历史旧记录会暂时落到“其他”，这会降低本模块的解释力。"
                if lang == "zh"
                else f"Only {float(coverage_pct):.1f}% of this week's exits carry a structured reason. Older records will stay in 'Other' for now, so interpret this section cautiously."
            )
            + "</div>"
        )
    return render_dashboard_legacy_page(
        "dashboard/legacy/reports_dashboard_weekly_review.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '每周复盘', 'Weekly Review')}",
            f'{DASHBOARD_WEEKLY_REVIEW_STYLE}',
            f"{t(lang, '每周复盘', 'Weekly Review')}",
            f"{t(lang, '把本周日报、模型表现、持仓卖出和任务质量合到一页，先形成第一版周报。', 'Pull the week’s reports, model performance, portfolio exits, and task quality into one first-pass weekly review.')}",
            f'{nav_html}',
            f'{lang}',
            f"{t(lang, '返回首页', 'Back to Dashboard')}",
            f'{lang}',
            f"{t(lang, '模型评测总览', 'Model Evaluation Overview')}",
            f'{lang}',
            f"{t(lang, '日报历史', 'Report History')}",
            f"{t(lang, '每周复盘', 'Weekly Review')}",
            f"{html.escape(str(summary.get('week_start') or '-'))}",
            f"{html.escape(str(summary.get('week_end') or '-'))}",
            f"{t(lang, '这一版周报优先回答四件事：本周模型有没有用、本周反复出现了什么股票、本周持仓卖出结果如何、本周 job 是否稳定。', 'This first version focuses on four questions: did the model work this week, which names kept resurfacing, how did portfolio exits do, and were jobs stable.')}",
            f"{t(lang, '日报数量', 'Reports')}",
            f"{int(summary.get('report_count') or 0)}",
            f"{t(lang, '本周已留档 AI 日报数量。', 'Archived AI daily reports this week.')}",
            f"{t(lang, '任务质量', 'Job Quality')}",
            f"{int(job_counts.get('success', 0))}",
            f"{'成功'}",
            f"{int(job_counts.get('success', 0))}",
            f"{'部分完成'}",
            f"{int(job_counts.get('partial', 0))}",
            f"{'失败'}",
            f"{int(job_counts.get('failed', 0))}",
            f"{t(lang, '本周已实现盈亏', 'Realized PnL')}",
            f"{_fmt_optional_float(trade_summary.get('realized_pnl'), digits=2)}",
            f"{'卖出笔数'}",
            f"{int(trade_summary.get('count') or 0)}",
            f"{'盈利笔数'}",
            f"{int(trade_summary.get('winner_count') or 0)}",
            f"{t(lang, '原因补录进度', 'Reason Coverage')}",
            f"{_fmt_optional_float(structured_reason_summary.get('coverage_pct'), suffix='%', digits=1)}",
            f"{'已结构化'}",
            f"{int(structured_reason_summary.get('count') or 0)}",
            f"{'待补录'}",
            f"{max(0, int(trade_summary.get('count') or 0) - int(structured_reason_summary.get('count') or 0))}",
            f"{t(lang, '建议审计覆盖率', 'Advice Audit Coverage')}",
            f"{_fmt_optional_float(audit_summary.get('coverage_pct'), suffix='%', digits=1)}",
            f"{'已带建议快照'}",
            f"{int(audit_summary.get('count') or 0)}",
            f"{'总卖出'}",
            f"{int(trade_summary.get('count') or 0)}",
            f"{t(lang, '模型 3D 均值', 'Model 3D Avg')}",
            f"{_fmt_optional_float((model_windows.get(3) or {}).get('avg_return'), suffix='%', digits=2)}",
            f"{'样本数'}",
            f"{int((model_windows.get(3) or {}).get('count') or 0)}",
            f"{'命中率'}",
            f"{_fmt_optional_float((model_windows.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}",
            f"{t(lang, '日报 5D 均值', 'Report 5D Avg')}",
            f"{_fmt_optional_float((report_window_summary.get(5) or {}).get('avg_return'), suffix='%', digits=2)}",
            f"{'可测样本'}",
            f"{int(summary.get('measured_report_rows') or 0)}",
            f"{'命中率'}",
            f"{_fmt_optional_float((report_window_summary.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}",
            f"{t(lang, '一、市场情绪与重复候选', '1. Mood and Repeated Picks')}",
            f'{mood_chips}',
            f"{t(lang, '股票', 'Ticker')}",
            f"{t(lang, '上榜次数', 'Times Picked')}",
            f"{t(lang, '最近结论', 'Latest Verdict')}",
            f'{repeated_rows_html}',
            f"{t(lang, '二、本周模型表现', '2. Weekly Model Performance')}",
            f"{t(lang, '主值是平均收益，下面小字是上涨命中率。', 'Main values are average returns, with positive hit rates below.')}",
            f"{t(lang, 'Run', 'Run')}",
            f"{t(lang, '市场 / 最近交易日', 'Market / Latest Trade Date')}",
            f'{run_rows_html}',
            f"{t(lang, '二点五、推荐验证', '2.5 Recommendation Validation')}",
            f"{t(lang, '把今天优先模型、优先组合和本周 AI 日报 Top 5 放到一张表里。主值是平均收益，小字是上涨命中率，用来回答“这周到底该更信哪套建议”。', 'Put the priority model, priority combo, and this week’s AI Report Top 5 onto one board. Main values are average returns and muted values are hit rates so you can judge which guidance deserved more trust this week.')}",
            f"{t(lang, '对象', 'Recommendation')}",
            f"{t(lang, '说明', 'Note')}",
            f'{recommendation_validation_rows_html}',
            f"{t(lang, '三、本周持仓卖出记录', '3. Weekly Portfolio Exits')}",
            f"{t(lang, '日期', 'Date')}",
            f"{t(lang, '股票', 'Ticker')}",
            f"{t(lang, '卖出价', 'Sell Price')}",
            f"{t(lang, '数量', 'Qty')}",
            f"{t(lang, '已实现盈亏', 'Realized PnL')}",
            f"{t(lang, '收益率', 'Return')}",
            f"{t(lang, '原因', 'Reason')}",
            f'{trade_rows_html}',
            f"{t(lang, '四、持仓建议有效性', '4. Advice Effectiveness')}",
            f"{t(lang, '第一版先按卖出原因归类，观察止盈、止损、调仓和复核动作最终带来的收益结果。', 'The first pass groups exits by reason so we can see how profit-taking, stops, rebalancing, and review-led exits actually performed.')}",
            f'{structured_reason_note}',
            f"{t(lang, '建议类型', 'Advice Type')}",
            f"{t(lang, '次数', 'Count')}",
            f"{t(lang, '盈利笔数', 'Winners')}",
            f"{t(lang, '命中率', 'Win Rate')}",
            f"{t(lang, '平均收益率', 'Avg Return')}",
            f"{t(lang, '累计已实现盈亏', 'Realized PnL')}",
            f'{advice_rows_html}',
            f"{t(lang, '五、原因待补录清单', '5. Reason Review Queue')}",
            f"{t(lang, '这张表只列本周还没有结构化卖出原因的记录，补完后“持仓建议有效性”统计会更可信。', 'This table lists the weekly exits still missing a structured reason. Once reviewed, the advice-effectiveness section becomes more trustworthy.')}",
            f"{t(lang, '日期', 'Date')}",
            f"{t(lang, '股票', 'Ticker')}",
            f"{t(lang, '已实现盈亏', 'Realized PnL')}",
            f"{t(lang, '收益率', 'Return')}",
            f"{t(lang, '操作', 'Action')}",
            f'{unresolved_trade_rows_html}',
            f"{t(lang, '六、建议审计链', '6. Advice Audit Trail')}",
            f"{t(lang, '从现在开始，新卖出会自动带上当时系统给出的动作建议、优先级、风险标签和调仓说明。这张表会进一步给出一条轻量复盘结论，回答：系统当时怎么说，你最后怎么做，结果如何。', 'From now on, new exits automatically carry the system advice snapshot at exit time. This table adds a lightweight review conclusion so you can see what the system said, what you did, and how it turned out.')}",
            f"{t(lang, '日期', 'Date')}",
            f"{t(lang, '股票', 'Ticker')}",
            f"{t(lang, '当时建议', 'Advice at Exit')}",
            f"{t(lang, '建议理由 / 调仓说明', 'Advice Reason / Rebalance')}",
            f"{t(lang, '最终动作', 'Final Action')}",
            f"{t(lang, '复盘结论', 'Review Conclusion')}",
            f"{t(lang, '结果', 'Outcome')}",
            f'{audited_trade_rows_html}',
            f"{t(lang, '七、本周任务异常', '7. Weekly Job Exceptions')}",
            f"{t(lang, '任务类型', 'Job Type')}",
            f"{t(lang, '状态', 'Status')}",
            f"{t(lang, '开始时间', 'Started At')}",
            f"{t(lang, '说明', 'Message')}",
            f'{weekly_jobs_html}',
        ],
    )



def _render_ai_report_guidance_bridge(report: dict, *, lang: str) -> str:
    guidance_summary = report.get("model_selection_guidance_summary") or {}
    attribution = report.get("market_template_attribution") or {}
    snapshot_meta = guidance_summary.get("snapshot_meta") or {}
    top_model_title = html.escape(
        str(guidance_summary.get("top_model_title") or (t(lang, "样本继续沉淀", "Still collecting samples")))
    )
    top_model_copy = html.escape(
        str(guidance_summary.get("top_model_summary") or (t(lang, "当前还没有足够样本给出明确优先模型。", "Not enough samples yet for a priority model.")))
    )
    top_combo_title = html.escape(
        str(guidance_summary.get("top_combo_title") or (t(lang, "组合样本继续沉淀", "Combo samples still accumulating")))
    )
    top_combo_copy = html.escape(
        str(guidance_summary.get("top_combo_summary") or (t(lang, "当前还没有足够组合样本。", "Not enough combo samples yet.")))
    )
    top_model_href = html.escape(str(guidance_summary.get("top_model_href") or f"/dashboard/model-performance?lang={lang}&market=CN"), quote=True)
    top_combo_href = html.escape(str(guidance_summary.get("top_combo_href") or f"/screeners?lang={lang}&market=CN&universe=full_market&run=1"), quote=True)
    source_time = html.escape(str(snapshot_meta.get("snapshot_date") or snapshot_meta.get("generated_at") or report.get("report_date") or "-"))
    source_kind = str(snapshot_meta.get("source") or "").strip()
    source_text = (
        f"来源：{'后台评测快照' if source_kind == 'snapshot' else '日报缓存 / 实时回退'} · {source_time}"
        if lang == "zh"
        else f"Source: {'evaluation snapshot' if source_kind == 'snapshot' else 'report cache / live fallback'} · {source_time}"
    )
    leaders = list(attribution.get("leaders") or [])
    leader_rows = "".join(
        "<div class='muted' style='margin-top:8px;'>"
        f"• {html.escape(str(item.get('label') or item.get('template') or '-'))} · {int(item.get('count') or 0)} "
        f"{t(lang, '只', 'names')} · 1D {_fmt_optional_float((item.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)} / "
        f"{_fmt_optional_float((item.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)} · 3D "
        f"{_fmt_optional_float((item.get('stats_3d') or {}).get('avg_return'), suffix='%', digits=2)} / "
        f"{_fmt_optional_float((item.get('stats_3d') or {}).get('hit_rate'), suffix='%', digits=1)}"
        "</div>"
        for item in leaders[:4]
    ) or f"<div class='muted'>{t(lang, '暂无模板归因样本。', 'No template attribution samples yet.')}</div>"
    return f"""
    <section class="card">
      <div class="eyebrow">{t(lang, '模型来源与近期验证', 'Model Source & Recent Validation')}</div>
      <div class="muted">{html.escape(source_text)}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-top:14px;">
        <div class="playbook" style="margin-top:0;">
          <div style="font-weight:900;margin-bottom:6px;">{t(lang, '优先模型', 'Priority model')}</div>
          <div style="font-weight:800;">{top_model_title}</div>
          <div class="muted" style="margin-top:6px;">{top_model_copy}</div>
          <div style="margin-top:10px;"><a class="pill" href="{top_model_href}">{t(lang, '按该模型筛选', 'Run this model')}</a></div>
        </div>
        <div class="playbook" style="margin-top:0;">
          <div style="font-weight:900;margin-bottom:6px;">{t(lang, '优先组合', 'Priority confluence')}</div>
          <div style="font-weight:800;">{top_combo_title}</div>
          <div class="muted" style="margin-top:6px;">{top_combo_copy}</div>
          <div style="margin-top:10px;"><a class="pill" href="{top_combo_href}">{t(lang, '按组合筛选', 'Run confluence')}</a></div>
        </div>
        <div class="playbook" style="margin-top:0;">
          <div style="font-weight:900;margin-bottom:6px;">{t(lang, 'Top 5 模板表现', 'Top 5 template evidence')}</div>
          {leader_rows}
        </div>
      </div>
      <div class="toolbar" style="margin:14px 0 0;">
        <a class="pill" href="/dashboard/model-performance?lang={lang}&market=CN">{t(lang, '模型评测总览', 'Model evaluation')}</a>
        <a class="pill" href="/dashboard/model-performance/winner-traceback?lang={lang}&market=CN">{t(lang, '强票反向归因', 'Winner traceback')}</a>
        <a class="pill" href="/screeners?lang={lang}&market=CN&universe=full_market&run=1">{t(lang, '回到模型选股', 'Back to screeners')}</a>
      </div>
    </section>
    """



def _premarket_plan_rows(report: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for source, items in (
        ("actionable", report.get("market_recommendations") or report.get("rows") or []),
        ("watch", report.get("market_watch_recommendations") or []),
    ):
        for item in list(items)[:8]:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            rows.append({**item, "_plan_source": source})
    rows.sort(
        key=lambda item: (
            0 if str(item.get("_plan_source") or "") == "actionable" else 1,
            0 if str(item.get("tradability_status") or "").upper() == "READY" else 1,
            -float(item.get("trade_readiness_score") or 0.0),
            float(abs(item.get("close_vs_buy_zone_high_pct") or 0.0)),
            -float(item.get("quant_rank") or 0.0),
            str(item.get("ticker") or ""),
        )
    )
    return rows[:8]



def _premarket_plan_action_text(item: dict, *, lang: str) -> str:
    source = str(item.get("_plan_source") or "watch")
    status = str(item.get("tradability_status") or "").upper()
    deviation = _monitor_float(item.get("close_vs_buy_zone_high_pct"))
    risk_flags = {str(flag).strip().lower() for flag in (item.get("risk_flags") or []) if str(flag).strip()}
    position_text = str(item.get("target_weight_text") or item.get("target_weight") or item.get("position_size_hint") or "").strip()
    if source == "watch" or status in {"REVIEW", "DEFER"}:
        return (
            "先观察，不抢第一笔；只有回踩买入区并重新放量时才考虑处理。"
            if lang == "zh"
            else "Watch first and avoid the first print; only act if price resets into the buy zone with renewed volume."
        )
    if deviation is not None and deviation >= 12.0:
        return (
            "已经偏离计划买点，今天只做观察，不追高。"
            if lang == "zh"
            else "Price is already extended beyond the planned entry, so treat it as watch-only and do not chase."
        )
    if "missing-model-score" in risk_flags:
        return (
            f"模型分暂不完整，按 {position_text or '小仓'} 先做验证单，开盘 15 分钟后再决定是否扩仓。"
            if lang == "zh"
            else f"Model scoring is incomplete, so start with a {position_text or 'small'} probe after the first 15 minutes before adding."
        )
    return (
        f"开盘后先等 15 分钟确认，再按 {position_text or '计划仓位'} 执行，不用抢竞价。"
        if lang == "zh"
        else f"Wait for the first 15 minutes to confirm, then execute with {position_text or 'planned size'} instead of chasing the open."
    )



def _premarket_plan_focus_text(item: dict, *, lang: str) -> str:
    trigger = str(item.get("entry_trigger") or "").strip()
    invalidation = str(item.get("invalidation_condition") or "").strip()
    if lang == "zh":
        return f"先看：{trigger or '是否守住开盘区间 / VWAP'}；不做：{invalidation or '跌破支撑或量价结构转弱'}"
    return f"Watch for: {trigger or 'VWAP / opening-range hold'}; stand down if: {invalidation or 'support fails or the tape weakens'}"



def _monitor_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None



def _monitor_market_for_ticker(ticker: str) -> str:
    normalized = str(ticker or "").strip().upper()
    if normalized.endswith((".SS", ".SZ")) or re.fullmatch(r"\d{6}", normalized):
        return "CN"
    if normalized.endswith(".HK"):
        return "HK"
    return "US"



def _monitor_source_label(source: str, *, lang: str) -> str:
    labels = {
        "zh": {
            "portfolio": "持仓",
            "watchlist": "自选",
            "ai_actionable": "AI可执行",
            "ai_watch": "AI观察",
            "us_hotspot": "美股热点",
            "social": "社交信号",
        },
        "en": {
            "portfolio": "Portfolio",
            "watchlist": "Watchlist",
            "ai_actionable": "AI Actionable",
            "ai_watch": "AI Watch",
            "us_hotspot": "US Hotspot",
            "social": "Social Signal",
        },
    }
    return labels["zh" if lang == "zh" else "en"].get(source, source)



def _monitor_status(row: dict, latest_price: float | None, *, lang: str) -> dict[str, str]:
    buy_zone = row.get("buy_zone") if isinstance(row.get("buy_zone"), dict) else {}
    buy_low = _monitor_float(buy_zone.get("low"))
    buy_high = _monitor_float(buy_zone.get("high"))
    stop_loss = _monitor_float(row.get("stop_loss"))
    sources = set(row.get("sources") or [])
    if latest_price is None or latest_price <= 0:
        return {
            "key": "no_price",
            "label": "缺行情" if lang == "zh" else "No price",
            "detail": "本地价格湖里暂时没有最新价格。" if lang == "zh" else "No latest local price is available yet.",
        }
    if stop_loss is not None and latest_price <= stop_loss:
        return {
            "key": "risk",
            "label": "触及风险位" if lang == "zh" else "Risk trigger",
            "detail": f"{t(lang, '现价已低于/接近止损位', 'Latest price is at or below the stop')} {_fmt_optional_float(stop_loss, digits=3)}",
        }
    if buy_low is not None and buy_high is not None and buy_low <= latest_price <= buy_high:
        return {
            "key": "in_zone",
            "label": "进入买入区" if lang == "zh" else "In buy zone",
            "detail": "价格进入计划买入区，仍需结合成交量与开盘走势确认。" if lang == "zh" else "Price is inside the planned buy zone; still confirm volume and tape.",
        }
    if buy_high is not None and latest_price > buy_high * 1.08:
        return {
            "key": "extended",
            "label": "不追高" if lang == "zh" else "Do not chase",
            "detail": "价格明显高于计划买入区，优先等回落或放弃。" if lang == "zh" else "Price is materially above the buy zone; wait for a pullback or pass.",
        }
    if "ai_actionable" in sources:
        return {
            "key": "ready",
            "label": "等待触发" if lang == "zh" else "Await trigger",
            "detail": "候选来自 AI 可执行池，等待触发条件确认。" if lang == "zh" else "Candidate came from the actionable AI pool; wait for trigger confirmation.",
        }
    if "ai_watch" in sources or "us_hotspot" in sources or "social" in sources:
        return {
            "key": "watch",
            "label": "观察确认" if lang == "zh" else "Watch",
            "detail": "目前偏观察，适合等待形态或风险条件改善。" if lang == "zh" else "This is still a watch item; wait for setup or risk improvement.",
        }
    return {
        "key": "track",
        "label": "跟踪" if lang == "zh" else "Track",
        "detail": "来自持仓或自选池，当前没有明确触发。" if lang == "zh" else "From portfolio/watchlist without a hard trigger yet.",
    }



def _build_realtime_monitor_rows(db: Session, *, lang: str, limit: int = 120) -> list[dict]:
    report = _load_cached_ai_daily_report(db) or {}
    focus: dict[str, dict] = {}

    def _add(item: dict, source: str) -> None:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            return
        row = focus.setdefault(
            ticker,
            {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": item.get("market") or _monitor_market_for_ticker(ticker),
                "sources": [],
                "headline": item.get("headline") or item.get("summary") or item.get("verdict") or "",
                "entry_trigger": item.get("entry_trigger") or "",
                "invalidation_condition": item.get("invalidation_condition") or "",
                "buy_zone": item.get("buy_zone") if isinstance(item.get("buy_zone"), dict) else {},
                "stop_loss": item.get("stop_loss"),
                "risk_flags": list(item.get("risk_flags") or []),
                "tradability_status": item.get("tradability_status"),
            },
        )
        if source not in row["sources"]:
            row["sources"].append(source)
        for key in ("name", "market", "headline", "entry_trigger", "invalidation_condition", "stop_loss", "tradability_status"):
            if not row.get(key) and item.get(key):
                row[key] = item.get(key)
        if not row.get("buy_zone") and isinstance(item.get("buy_zone"), dict):
            row["buy_zone"] = item.get("buy_zone")
        if not row.get("risk_flags") and item.get("risk_flags"):
            row["risk_flags"] = list(item.get("risk_flags") or [])

    for item in load_portfolio_positions():
        _add(item, "portfolio")
    for item in _dashboard_watchlist_map(db).values():
        _add(item, "watchlist")
    for item in report.get("market_recommendations") or report.get("rows") or []:
        _add(item, "ai_actionable")
    for item in report.get("market_watch_recommendations") or []:
        _add(item, "ai_watch")
    for item in report.get("us_model_recommendations") or report.get("us_hotspot_validation") or []:
        _add(item, "us_hotspot")
    social_summary = social_signal_summary(db)
    social_items = list(social_summary.get("actionable") or []) + list(social_summary.get("hot_mentions_24h") or [])
    seen_social: set[str] = set()
    for item in social_items:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen_social:
            continue
        seen_social.add(ticker)
        _add(
            {
                **item,
                "headline": item.get("system_action") or item.get("social_view") or item.get("summary") or "",
                "risk_flags": item.get("validation_reasons") or [],
            },
            "social",
        )

    source_priority = {"portfolio": 0, "ai_actionable": 1, "ai_watch": 2, "watchlist": 3, "us_hotspot": 4, "social": 5}
    us_live_quotes = load_us_latest_trades(
        [ticker for ticker, row in focus.items() if str(row.get("market") or "").upper() == "US"]
    )
    rows: list[dict] = []
    for ticker, row in focus.items():
        live_quote = us_live_quotes.get(ticker) if str(row.get("market") or "").upper() == "US" else None
        local_close = _monitor_float(load_latest_close(ticker))
        latest_price = _monitor_float((live_quote or {}).get("price")) if live_quote else local_close
        status = _monitor_status(row, latest_price, lang=lang)
        buy_zone = row.get("buy_zone") if isinstance(row.get("buy_zone"), dict) else {}
        buy_low = _monitor_float(buy_zone.get("low"))
        buy_high = _monitor_float(buy_zone.get("high"))
        source_labels = [_monitor_source_label(source, lang=lang) for source in row.get("sources") or []]
        rows.append(
            {
                **row,
                "latest_price": latest_price,
                "local_close": local_close,
                "price_source": (live_quote or {}).get("source") or "local_latest_close",
                "price_timestamp": (live_quote or {}).get("timestamp"),
                "price_feed": (live_quote or {}).get("feed"),
                "status": status,
                "buy_low": buy_low,
                "buy_high": buy_high,
                "source_labels": source_labels,
                "source_rank": min((source_priority.get(source, 9) for source in row.get("sources") or ["watchlist"]), default=9),
            }
        )
    status_rank = {"risk": 0, "in_zone": 1, "ready": 2, "extended": 3, "watch": 4, "track": 5, "no_price": 6}
    rows.sort(key=lambda item: (status_rank.get((item.get("status") or {}).get("key"), 9), item.get("source_rank", 9), str(item.get("ticker") or "")))
    return rows[: max(20, min(int(limit or 120), 300))]



@router.get("/realtime-monitor/intraday", response_class=JSONResponse)
def dashboard_realtime_monitor_intraday(
    request: Request,
    ticker: str,
    market: str = "US",
    timeframe: str = "5Min",
) -> JSONResponse:
    if not is_authenticated(request):
        return JSONResponse({"status": "unauthorized", "bars": [], "message": "Unauthorized."}, status_code=401)
    normalized_ticker = str(ticker or "").strip().upper()
    market_code = str(market or "").strip().upper() or _monitor_market_for_ticker(normalized_ticker)
    if market_code not in {"CN", "US", "HK"}:
        market_code = _monitor_market_for_ticker(normalized_ticker)
    if market_code == "CN":
        return JSONResponse(load_cn_intraday_bars(normalized_ticker, timeframe=timeframe))
    if market_code != "US":
        return JSONResponse(
            {
                "status": "unsupported",
                "ticker": normalized_ticker,
                "market": market_code,
                "bars": [],
                "message": "当前弹窗日内 K 线支持美股和 A 股；港股分钟线后续接入后会在这里打开。",
            }
        )
    return JSONResponse(load_us_intraday_bars(normalized_ticker, timeframe=timeframe))



@router.get("/realtime-monitor", response_class=HTMLResponse)
def dashboard_realtime_monitor(request: Request, limit: int = 120, market: str = "ALL", db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/realtime-monitor")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="monitor")
    all_rows = _build_realtime_monitor_rows(db, lang=lang, limit=300)
    market_filter = str(market or "ALL").strip().upper()
    if market_filter not in {"ALL", "CN", "US", "HK"}:
        market_filter = "ALL"
    rows = [row for row in all_rows if market_filter == "ALL" or str(row.get("market") or "").upper() == market_filter]
    rows = rows[: max(20, min(int(limit or 120), 300))]
    counts = Counter((row.get("status") or {}).get("key") for row in rows)
    market_counts = Counter(str(row.get("market") or "-").upper() for row in all_rows)
    generated_at = _display_time(datetime.now(timezone.utc).isoformat(), with_tz=True)

    def _zone_text(row: dict) -> str:
        low = row.get("buy_low")
        high = row.get("buy_high")
        if low is None and high is None:
            return "-"
        return f"{_fmt_optional_float(low, digits=3)} - {_fmt_optional_float(high, digits=3)}"

    def _risk_chips(row: dict) -> str:
        flags = [str(flag).strip() for flag in (row.get("risk_flags") or []) if str(flag).strip()]
        if not flags:
            return "<span class='muted'>-</span>"
        return "".join(f"<span class='risk-chip'>{html.escape(flag)}</span>" for flag in flags[:4])

    def _price_source_text(row: dict) -> str:
        source = str(row.get("price_source") or "").strip()
        if source == "alpaca_latest_trade":
            timestamp = str(row.get("price_timestamp") or "").strip()
            feed = str(row.get("price_feed") or "").strip().upper()
            prefix = "Alpaca实时" if lang == "zh" else "Alpaca live"
            detail = f"{prefix}{(' · ' + feed) if feed else ''}"
            return f"{detail}{(' · ' + timestamp[:19]) if timestamp else ''}"
        return "本地最新收盘" if lang == "zh" else "Local latest close"

    cards = "".join(
        f"""
        <article class="monitor-card {html.escape(str((row.get('status') or {}).get('key') or 'track'))}" data-monitor-status="{html.escape(str((row.get('status') or {}).get('key') or 'track'), quote=True)}">
          <div class="monitor-top">
            <div>
              <a class="ticker" href="/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}">{html.escape(str(row.get('name') or row.get('ticker') or '-'))}</a>
              <div class="name">{html.escape(str(row.get('ticker') or '-'))}</div>
            </div>
            <span class="status-chip {html.escape(str((row.get('status') or {}).get('key') or 'track'))}">{html.escape(str((row.get('status') or {}).get('label') or '-'))}</span>
          </div>
          <div class="price-row">
            <strong>{_fmt_optional_float(row.get('latest_price'), digits=3)}</strong>
            <span>{html.escape(str(row.get('market') or '-'))}</span>
            <span>{html.escape(' / '.join(row.get('source_labels') or []) or '-')}</span>
            <span>{html.escape(_price_source_text(row))}</span>
          </div>
          <div class="monitor-grid">
            <div><span>{t(lang, '买入区', 'Buy zone')}</span><strong>{html.escape(_zone_text(row))}</strong></div>
            <div><span>{t(lang, '止损', 'Stop')}</span><strong>{_fmt_optional_float(row.get('stop_loss'), digits=3)}</strong></div>
            <div><span>{t(lang, '触发条件', 'Trigger')}</span><strong>{html.escape(str(row.get('entry_trigger') or '-'))}</strong></div>
            <div><span>{t(lang, '放弃条件', 'Invalidation')}</span><strong>{html.escape(str(row.get('invalidation_condition') or '-'))}</strong></div>
          </div>
          <div class="monitor-detail">{html.escape(str((row.get('status') or {}).get('detail') or '-'))}</div>
          <div class="risk-row">{_risk_chips(row)}</div>
          <div class="card-actions">
            <button
              class="track-button"
              type="button"
              data-ticker="{html.escape(str(row.get('ticker') or ''), quote=True)}"
              data-name="{html.escape(str(row.get('name') or row.get('ticker') or ''), quote=True)}"
              data-market="{html.escape(str(row.get('market') or ''), quote=True)}"
            >{t(lang, '弹出日内K线', 'Pop Intraday Chart')}</button>
            <a class="track-link" href="/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}">{t(lang, '打开分析页', 'Open Insight')}</a>
          </div>
        </article>
        """
        for row in rows
    ) or f"""
        <article class="monitor-card">
          <div class="monitor-detail">{t(lang, '暂无可监控股票。请先添加自选/持仓，或生成 AI 日报。', 'No monitor names yet. Add watchlist/portfolio names or generate the AI report first.')}</div>
        </article>
    """
    market_pills = "".join(
        f"<a class='pill{' active' if market_filter == code else ''}' href='/dashboard/realtime-monitor?lang={lang}&market={code}&limit={limit}'>{label}: {int(count)}</a>"
        for code, label, count in (
            ("ALL", "全部" if lang == "zh" else "All", len(all_rows)),
            ("CN", "A股" if lang == "zh" else "CN", market_counts.get("CN", 0)),
            ("US", "美股" if lang == "zh" else "US", market_counts.get("US", 0)),
            ("HK", "港股" if lang == "zh" else "HK", market_counts.get("HK", 0)),
        )
    )

    return render_dashboard_legacy_page(
        "dashboard/legacy/reports_dashboard_realtime_monitor.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '重点监控台', 'Live Monitor')}",
            f'{PRICE_SOURCE_TEXT_STYLE}',
            f"{t(lang, '重点监控台', 'Live Monitor')}",
            f"{t(lang, '只盯持仓、自选、AI 日报候选，不做全市场实时流，避免把机器拖垮。', 'Tracks only portfolio, watchlist, and AI report candidates instead of streaming the whole market.')}",
            f'{nav_html}',
            f'{lang}',
            f"{t(lang, '返回首页', 'Back to Dashboard')}",
            f'{lang}',
            f"{t(lang, '盘前便签', 'Premarket Plan')}",
            f'{lang}',
            f"{t(lang, 'AI 日报', 'AI Report')}",
            f'{lang}',
            f"{t(lang, '持仓', 'Portfolio')}",
            f"{t(lang, '60 秒自动刷新', 'Auto refresh every 60s')}",
            f"{t(lang, '重点池准实时监控', 'Focused Quasi-live Monitor')}",
            f"{t(lang, '当前价格使用本地最新行情缓存，适合盘中/盘后快速判断“是否进入买入区、是否不该追高、是否触及风险位”。后续可以把价格源替换为 Alpaca/Polygon 实时报价。', 'Prices currently use local latest-price cache. This is designed to quickly flag buy-zone, do-not-chase, and risk-trigger states; Alpaca/Polygon quotes can be plugged in later.')}",
            f"{t(lang, '生成时间', 'Generated')}",
            f'{html.escape(generated_at)}',
            f"{t(lang, '股票数', 'Names')}",
            f'{len(rows)}',
            f'{market_pills}',
            f"{t(lang, '进入买入区', 'In buy zone')}",
            f"{int(counts.get('in_zone') or 0)}",
            f"{t(lang, '触及风险位', 'Risk triggers')}",
            f"{int(counts.get('risk') or 0)}",
            f"{t(lang, '不追高', 'Do not chase')}",
            f"{int(counts.get('extended') or 0)}",
            f"{t(lang, '缺行情', 'No price')}",
            f"{int(counts.get('no_price') or 0)}",
            f'{cards}',
            f"{t(lang, '日内 K 线', 'Intraday Chart')}",
            f"{t(lang, '加载中...', 'Loading...')}",
            f'{json.dumps(lang)}',
        ],
    )



@router.get("/premarket-plan", response_class=HTMLResponse)
def dashboard_premarket_plan(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/premarket-plan")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    report = _load_cached_ai_daily_report(db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先刷新行情或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "market_recommendations": [],
        "market_watch_recommendations": [],
    }
    plan_rows = _premarket_plan_rows(report)
    plan_date = html.escape(str(report.get("report_date") or report.get("generated_at") or "-"))
    strategy = report.get("strategy") or {}
    guidance_summary = report.get("model_selection_guidance_summary") or {}
    top_model_title = html.escape(str(guidance_summary.get("top_model_title") or "-"))
    top_combo_title = html.escape(str(guidance_summary.get("top_combo_title") or "-"))
    actionable_count = sum(1 for item in plan_rows if str(item.get("_plan_source") or "") == "actionable")
    watch_count = sum(1 for item in plan_rows if str(item.get("_plan_source") or "") != "actionable")
    do_not_chase_count = sum(
        1
        for item in plan_rows
        if (_monitor_float(item.get("close_vs_buy_zone_high_pct")) or 0.0) >= 12.0
    )

    def _source_label(value: str) -> str:
        if value == "actionable":
            return "可执行" if lang == "zh" else "Actionable"
        return "观察" if lang == "zh" else "Watch"

    def _source_class(value: str) -> str:
        return "actionable" if value == "actionable" else "watch"

    def _risk_text(item: dict) -> str:
        flags = [str(flag).strip() for flag in (item.get("risk_flags") or []) if str(flag).strip()]
        if flags:
            return " / ".join(flags[:4])
        return "无明显风险标签" if lang == "zh" else "No obvious risk tags"

    def _buy_zone_text(item: dict) -> str:
        zone = item.get("buy_zone") or {}
        low = zone.get("low")
        high = zone.get("high")
        if low is None and high is None:
            return "-"
        return f"{_fmt_optional_float(low, digits=3)} - {_fmt_optional_float(high, digits=3)}"

    card_rows = "".join(
        f"""
        <article class="plan-card">
          <div class="plan-top">
            <div>
              <a class="ticker" href="/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}">{html.escape(str(item.get('name') or item.get('ticker') or '-'))}</a>
              <div class="name">{html.escape(str(item.get('ticker') or '-'))}</div>
            </div>
            <span class="badge {_source_class(str(item.get('_plan_source') or 'watch'))}">{_source_label(str(item.get('_plan_source') or 'watch'))}</span>
          </div>
          <div class="headline">{html.escape(str(item.get('headline') or item.get('summary') or item.get('verdict') or '-'))}</div>
          <div class="risk" style="margin-top:10px;background:rgba(96,165,250,0.10);border-color:rgba(96,165,250,0.22);color:#bfdbfe;">{html.escape(_premarket_plan_action_text(item, lang=lang))}</div>
          <div class="plan-grid">
            <div><span>{t(lang, '触发条件', 'Trigger')}</span><strong>{html.escape(str(item.get('entry_trigger') or '-'))}</strong></div>
            <div><span>{t(lang, '放弃条件', 'Give up if')}</span><strong>{html.escape(str(item.get('invalidation_condition') or '-'))}</strong></div>
            <div><span>{t(lang, '买入区', 'Buy zone')}</span><strong>{html.escape(_buy_zone_text(item))}</strong></div>
            <div><span>{t(lang, '仓位', 'Size')}</span><strong>{html.escape(str(item.get('target_weight_text') or item.get('target_weight') or item.get('position_size_hint') or '-'))}</strong></div>
            <div><span>{t(lang, '止损', 'Stop')}</span><strong>{html.escape(str(item.get('stop_loss') or '-'))}</strong></div>
            <div><span>{t(lang, '状态', 'Status')}</span><strong>{html.escape(format_trade_status(item.get('tradability_status'), lang=lang))}</strong></div>
          </div>
          <div class="note" style="margin-top:10px;font-weight:800;color:var(--ink);">{html.escape(_premarket_plan_focus_text(item, lang=lang))}</div>
          <div class="risk">{t(lang, '风险提示', 'Risk')}：{html.escape(_risk_text(item))}</div>
          <div class="note">{html.escape(build_trade_explain_text(item, lang=lang))}</div>
        </article>
        """
        for item in plan_rows
    ) or f"""
        <article class="plan-card empty">
          <div class="headline">{t(lang, '当前没有可生成盘前计划的候选。请先运行收盘刷新、模型预计算和 AI 日报。', 'No candidates are available for a premarket plan. Run post-close refresh, model precompute, and the AI report first.')}</div>
        </article>
    """

    return render_dashboard_legacy_page(
        "dashboard/legacy/reports_dashboard_premarket_plan.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '盘前便签', 'Premarket Plan')}",
            f'{BUY_ZONE_TEXT_STYLE}',
            f"{t(lang, '盘前便签', 'Premarket')}",
            f"{t(lang, '手机端 10 秒看完：先看触发、放弃、仓位和风险，不在盘前重新翻长表。', 'A 10-second mobile plan: triggers, invalidation, sizing, and risk without reopening long tables.')}",
            f'{nav_html}',
            f'{lang}',
            f"{t(lang, '返回首页', 'Back to Dashboard')}",
            f'{lang}',
            f"{t(lang, 'AI 日报', 'AI Report')}",
            f'{lang}',
            f"{t(lang, '重点监控台', 'Live Monitor')}",
            f'{lang}',
            f"{t(lang, '模型选股', 'Screeners')}",
            f"{t(lang, '明日重点盯盘清单', 'Next-session watch plan')}",
            f"{html.escape(str(strategy.get('headline') or report.get('headline') or '-'))}",
            f"{html.escape(str(strategy.get('playbook') or report.get('headline') or '-'))}",
            f"{t(lang, '日报日期', 'Report date')}",
            f'{plan_date}',
            f"{t(lang, '优先模型', 'Model')}",
            f'{top_model_title}',
            f"{t(lang, '优先组合', 'Combo')}",
            f'{top_combo_title}',
            f"{t(lang, '可执行', 'Actionable')}",
            f'{actionable_count}',
            f"{t(lang, '观察', 'Watch')}",
            f'{watch_count}',
            f"{t(lang, '不要追高', 'Do not chase')}",
            f'{do_not_chase_count}',
            f'{card_rows}',
        ],
    )



@router.get("/ai-daily-report", response_class=HTMLResponse)
def dashboard_ai_daily_report(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="daily_report")
    report = _load_cached_ai_daily_report(db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先刷新行情或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "portfolio_summary": {},
        "portfolio_rows": [],
        "social_signal_summary": {"accounts": [], "actionable": []},
        "us_hotspot_validation": [],
        "market_recommendations": [],
        "rows": [],
        "buy_the_dip_rows": [],
    }
    if not report.get("social_signal_summary"):
        current_social_summary = social_signal_summary(db)
        report = {
            **report,
            "social_signal_summary": {
                "accounts": current_social_summary.get("accounts") or [],
                "actionable": current_social_summary.get("actionable") or [],
            },
        }

    portfolio_summary = report.get("portfolio_summary") or {}
    market_recommendation_meta = report.get("market_recommendations_meta") or {}
    market_structure = report.get("market_structure") or {}
    market_template_attribution = report.get("market_template_attribution") or {}
    us_market_recommendation_meta = report.get("us_model_recommendations_meta") or {}
    us_market_structure = report.get("us_market_structure") or {}
    lightgbm_execution_bias = report.get("lightgbm_execution_bias") or {}
    recommendation_regression = report.get("recommendation_regression") or {}
    regression_policy = recommendation_regression.get("policy") or {}
    regression_summary = recommendation_regression.get("summary") or {}
    model_guidance_card_html = _render_ai_report_guidance_bridge(report, lang=lang)
    kronos_snapshot = load_latest_kronos_validation(db) or {}
    kronos_payload = kronos_snapshot.get("payload") if isinstance(kronos_snapshot, dict) else {}
    kronos_payload = kronos_payload if isinstance(kronos_payload, dict) else {}
    kronos_status = str(kronos_payload.get("status") or "-")
    kronos_candidate_count = int(kronos_payload.get("candidate_count") or 0)
    kronos_validated_count = int(kronos_payload.get("validated_count") or 0)
    kronos_pending_count = int(kronos_payload.get("not_configured_count") or 0)
    kronos_snapshot_time = _display_time(str(kronos_snapshot.get("created_at") or "")) if kronos_snapshot else "-"
    kronos_card_html = f"""
              <section class="card">
                <div class="eyebrow">{t(lang, 'Kronos 二次验证状态', 'Kronos Validation Status')}</div>
                <div class="muted">{t(lang, '这张卡片读取最新 Kronos 验证快照；即使当前 AI 日报还没重跑，也能判断二次验证层是否接上。', 'This card reads the latest Kronos validation snapshot, so you can see whether the secondary validation layer is connected even before the report is regenerated.')}</div>
                <div class="toolbar" style="margin:12px 0 0;">
                  <span class="pill">{t(lang, '状态', 'Status')}: {html.escape(kronos_status)}</span>
                  <span class="pill">{t(lang, '候选', 'Candidates')}: {kronos_candidate_count}</span>
                  <span class="pill">{t(lang, '已验证', 'Validated')}: {kronos_validated_count}</span>
                  <span class="pill">{t(lang, '待配置', 'Needs setup')}: {kronos_pending_count}</span>
                  <span class="pill">{t(lang, '时间', 'Time')}: {html.escape(kronos_snapshot_time)}</span>
                </div>
                <div class="action-row">
                  <a class="cta" href="/settings/kronos?lang={lang}">{t(lang, '查看 Kronos 配置', 'Open Kronos settings')}</a>
                  <form action="/jobs/kronos-validation" method="post" style="display:inline;">
                    <input type="hidden" name="redirect_to" value="/dashboard/ai-daily-report?lang={lang}" />
                    <button type="submit">{t(lang, '刷新二次验证快照', 'Refresh validation snapshot')}</button>
                  </form>
                </div>
              </section>
    """
    market_candidate_status = str(market_recommendation_meta.get("status") or "").strip().lower()
    send_guard_note = ""
    force_send_cta = ""
    if market_candidate_status in {"fallback", "not_ready"}:
        send_guard_note = (
            html.escape(str(market_recommendation_meta.get("note") or "今日 A股候选未完全就绪，默认不建议直接发送日报。"))
        )
        force_send_cta = f"""
        <form action="/jobs/send-ai-daily-report" method="post" style="display:inline;">
          <input type="hidden" name="redirect_to" value="/dashboard/ai-daily-report?lang={lang}" />
          <input type="hidden" name="force_send" value="1" />
          <button type="submit" style="background:linear-gradient(135deg, rgba(245,158,11,0.88), rgba(251,191,36,0.82));">{t(lang, '仍然发送当前降级日报', 'Force send degraded report')}</button>
        </form>
        """
    def recommendation_meta_badge(meta: dict) -> str:
        status = str(meta.get("status") or "").strip().lower()
        source = str(meta.get("source") or "").strip().lower()
        note = html.escape(re.sub(r"，?强势观察池\s+\d+\s*只", "", str(meta.get("note") or "-")).strip())
        blocked_candidates = int(meta.get("blocked_candidates") or 0)
        label = {
            "ready": "今日候选已就绪",
            "fallback": "已降级到预测候选",
            "blocked": "推荐已暂停",
            "not_ready": "今日候选未就绪",
            "empty": "当前无可用候选",
        }.get(status, "候选状态")
        source_text = {
            "fresh_snapshot": "来源：今日 screener 快照",
            "predictions_fallback": "来源：最新模型预测",
            "snapshot_required": "来源：等待今日快照",
            "none": "来源：无",
        }.get(source, "来源：-")
        tone = {
            "ready": "rgba(61,217,182,0.12)",
            "fallback": "rgba(245,158,11,0.14)",
            "blocked": "rgba(239,68,68,0.14)",
            "not_ready": "rgba(239,68,68,0.14)",
            "empty": "rgba(148,163,184,0.14)",
        }.get(status, "rgba(148,163,184,0.14)")
        border = {
            "ready": "rgba(61,217,182,0.28)",
            "fallback": "rgba(245,158,11,0.32)",
            "blocked": "rgba(239,68,68,0.32)",
            "not_ready": "rgba(239,68,68,0.32)",
            "empty": "rgba(148,163,184,0.28)",
        }.get(status, "rgba(148,163,184,0.28)")
        return f"""
        <div class="playbook" style="margin-top:14px;background:{tone};border-color:{border};">
          <div style="font-weight:800;margin-bottom:6px;">{label}</div>
          <div class="muted">{source_text}</div>
          <div class="muted" style="margin-top:6px;">{note}</div>
          <div class="muted" style="margin-top:6px;">{t(lang, '被规则拦截', 'Blocked by hard rules')}: {blocked_candidates}</div>
        </div>
        """
    def regression_discipline_card() -> str:
        sample_count = int(recommendation_regression.get("sample_count") or 0)
        notes = [
            html.escape(str(item).strip())
            for item in (regression_policy.get("notes") or [])
            if str(item).strip()
        ]
        if sample_count <= 0 and not notes:
            return ""
        actionable_stats = regression_summary.get("actionable") or {}
        recent_actionable_stats = regression_summary.get("recent_actionable") or {}
        recent_all_stats = regression_summary.get("recent_all") or {}
        min_quality = regression_policy.get("min_actionable_quality_score")
        max_actionable = regression_policy.get("max_actionable_count")
        try:
            max_actionable_text_zh = f"{int(max_actionable)} 只" if max_actionable is not None else "不额外限制"
            max_actionable_text_en = f"{int(max_actionable)}" if max_actionable is not None else "No extra cap"
        except (TypeError, ValueError):
            max_actionable_text_zh = str(max_actionable) if max_actionable is not None else "不额外限制"
            max_actionable_text_en = str(max_actionable) if max_actionable is not None else "No extra cap"
        if lang == "zh":
            stance = "防守选股" if max_actionable is not None or min_quality is not None else "正常筛选"
            stance_note = (
                "最近真实兑现不够强，系统会宁缺毋滥：少给票、只给质量更高的票。"
                if stance == "防守选股"
                else "当前没有触发额外刹车，仍按常规候选纪律输出。"
            )
            metric_rows = [
                ("回放样本", f"{sample_count} 条"),
                ("全样本可执行命中", f"{_fmt_optional_float(actionable_stats.get('execution_hit_rate'), suffix='%', digits=1)} / 收盘 {_fmt_optional_float(actionable_stats.get('close_hit_rate'), suffix='%', digits=1)}"),
                ("近期可执行", f"{_fmt_optional_float(recent_actionable_stats.get('execution_hit_rate'), suffix='%', digits=1)} / 深回撤 {_fmt_optional_float(recent_actionable_stats.get('deep_drawdown_rate'), suffix='%', digits=1)}"),
                ("近期整体候选", f"{_fmt_optional_float(recent_all_stats.get('execution_hit_rate'), suffix='%', digits=1)} / 深回撤 {_fmt_optional_float(recent_all_stats.get('deep_drawdown_rate'), suffix='%', digits=1)}"),
                ("质量门槛", _fmt_optional_float(min_quality, digits=1)),
                ("最多可执行", max_actionable_text_zh),
            ]
            note_title = "系统本次调参原因"
        else:
            stance = "Defensive selection" if max_actionable is not None or min_quality is not None else "Normal selection"
            stance_note = (
                "Recent realized quality is not strong enough, so the system prefers fewer, higher-quality names."
                if stance == "Defensive selection"
                else "No extra accuracy brake is active; normal candidate discipline is used."
            )
            metric_rows = [
                ("Replay samples", f"{sample_count}"),
                ("Actionable hit", f"{_fmt_optional_float(actionable_stats.get('execution_hit_rate'), suffix='%', digits=1)} / close {_fmt_optional_float(actionable_stats.get('close_hit_rate'), suffix='%', digits=1)}"),
                ("Recent actionable", f"{_fmt_optional_float(recent_actionable_stats.get('execution_hit_rate'), suffix='%', digits=1)} / deep DD {_fmt_optional_float(recent_actionable_stats.get('deep_drawdown_rate'), suffix='%', digits=1)}"),
                ("Recent all", f"{_fmt_optional_float(recent_all_stats.get('execution_hit_rate'), suffix='%', digits=1)} / deep DD {_fmt_optional_float(recent_all_stats.get('deep_drawdown_rate'), suffix='%', digits=1)}"),
                ("Quality gate", _fmt_optional_float(min_quality, digits=1)),
                ("Max actionable", max_actionable_text_en),
            ]
            note_title = "Why the policy changed"
        metric_html = "".join(
            f"<span class='pill'>{html.escape(label)}: {html.escape(str(value))}</span>"
            for label, value in metric_rows
        )
        notes_html = "".join(f"<div class='muted'>• {item}</div>" for item in notes[:4]) or "<div class='muted'>-</div>"
        return f"""
        <section class="card">
          <div class="eyebrow">{t(lang, '选股纪律 / 准确率刹车', 'Selection Discipline / Accuracy Brake')}</div>
          <div style="font-size:20px;font-weight:900;margin-bottom:6px;">{html.escape(stance)}</div>
          <div class="muted">{html.escape(stance_note)}</div>
          <div class="toolbar" style="margin:12px 0 0;">{metric_html}</div>
          <div class="playbook" style="margin-top:12px;">
            <div style="font-weight:800;margin-bottom:6px;">{note_title}</div>
            {notes_html}
          </div>
        </section>
        """
    def structure_block(title: str, structure: dict) -> str:
        source_value = str(structure.get("source") or "").strip()
        if lang == "zh":
            source_text = {
                "market_heatmap_snapshot": "来源：后台市场快照",
                "recommendation_rows": "来源：全市场模板主题汇总",
            }.get(source_value, "来源：结构化候选汇总")
        else:
            source_text = {
                "market_heatmap_snapshot": "Source: background market snapshot",
                "recommendation_rows": "Source: full-market template theme aggregation",
            }.get(source_value, "Source: structured candidate aggregation")
        strong_rows = "".join(
            f"<div class='muted'>• {html.escape(str(item.get('label') or '-'))} · {int(item.get('count') or 0)} 只 · 均强度 {html.escape(str(item.get('avg_strength') or '-'))} · {' / '.join((item.get('tickers') or [])[:3]) or '-'}</div>"
            for item in (structure.get("strong_sectors") or [])[:3]
        ) or "<div class='muted'>-</div>"
        weak_rows = "".join(
            f"<div class='muted'>• {html.escape(str(item.get('label') or '-'))} · 风险均值 {html.escape(str(item.get('avg_risk') or '-'))} · {' / '.join((item.get('tickers') or [])[:3]) or '-'}</div>"
            for item in (structure.get("weak_sectors") or [])[:3]
        ) or "<div class='muted'>-</div>"
        risk_rows = "".join(
            f"<div class='muted'>• {html.escape(str(item.get('ticker') or '-'))} · {html.escape(str(item.get('tradability_status') or '-'))} · {html.escape(', '.join(item.get('risk_flags') or []) or '-')}</div>"
            for item in (structure.get("risk_watch") or [])[:4]
        ) or "<div class='muted'>-</div>"
        return f"""
        <article class="card">
          <div class="eyebrow">{title}</div>
          <div class="muted">{html.escape(str(structure.get('headline') or '-'))}</div>
          <div class="muted" style="margin-top:6px;">{html.escape(source_text)}</div>
          <div class="playbook" style="margin-top:14px;">
            <div style="font-weight:800;margin-bottom:6px;">{t(lang, '强方向', 'Strong sectors')}</div>
            {strong_rows}
            <div style="font-weight:800;margin:12px 0 6px;">{t(lang, '弱方向 / 风险集中', 'Weak / risk concentration')}</div>
            {weak_rows}
            <div style="font-weight:800;margin:12px 0 6px;">{t(lang, '风险清单', 'Risk watch')}</div>
            {risk_rows}
          </div>
        </article>
        """

    def template_attribution_block(title: str, attribution: dict) -> str:
        leaders = attribution.get("leaders") or []
        leader_rows = "".join(
            (
                f"<div class='muted'>• {html.escape(str(item.get('label') or '-'))} · {int(item.get('count') or 0)} 只 · 量化均分 {html.escape(str(item.get('avg_quant_rank') or '-'))} · {html.escape(' / '.join(_report_ticker_labels(item.get('tickers') or [], report)) or '-')}</div>"
                + (
                    f"<div class='muted' style='padding-left:12px;'>1D {_fmt_optional_float((item.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float((item.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}"
                    f" · 3D {_fmt_optional_float((item.get('stats_3d') or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float((item.get('stats_3d') or {}).get('hit_rate'), suffix='%', digits=1)}"
                    f" · 5D {_fmt_optional_float((item.get('stats_5d') or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float((item.get('stats_5d') or {}).get('hit_rate'), suffix='%', digits=1)}</div>"
                )
            )
            for item in leaders[:4]
        ) or "<div class='muted'>-</div>"
        return f"""
        <article class="card">
          <div class="eyebrow">{title}</div>
          <div class="muted">{html.escape(str(attribution.get('headline') or '-'))}</div>
          <div class="playbook" style="margin-top:14px;">
            <div style="font-weight:800;margin-bottom:6px;">{t(lang, '今日 Top 5 主要来源模板', 'Template drivers behind today Top 5')}</div>
            {leader_rows}
          </div>
        </article>
        """
    def _ai_report_name_cell(item: dict, *, link: bool = False) -> str:
        ticker = html.escape(str(item.get("ticker") or "-"))
        name = html.escape(str(item.get("name") or item.get("ticker") or "-"))
        href = f"/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}"
        title_html = f"<a href='{href}' style='font-weight:800;color:var(--ink);'>{name}</a>" if link else f"<div style='font-weight:800'>{name}</div>"
        return title_html + f"<div class='muted'>{ticker}</div>"
    portfolio_rows_html = "".join(
        "<tr>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}</td>"
        f"<td>{item.get('quantity') or '-'}</td>"
        f"<td>{item.get('cost_basis') or '-'}</td>"
        f"<td>{item.get('latest_price') or '-'}</td>"
        f"<td>{float(item.get('pnl') or 0.0):.2f}<div class='muted'>{float(item.get('pnl_pct') or 0.0):.2f}%</div></td>"
        f"<td>{item.get('ai_verdict') or '-'}<div class='muted'>{item.get('ai_headline') or '-'}</div></td>"
        f"<td>{item.get('action_bucket') or '-'}<div class='muted'>目标: {item.get('target_weight_text') or '-'}</div></td>"
        f"<td>{item.get('ai_strategy') or '-'}<div class='muted'>触发: {item.get('entry_trigger') or '-'}</div><div class='muted'>失效: {item.get('invalidation_condition') or '-'}</div></td>"
        "</tr>"
        for item in (report.get("portfolio_rows") or [])
    ) or f"<tr><td colspan='9'>{t(lang, '暂无持仓库数据', 'No portfolio holdings yet.')}</td></tr>"
    market_recommendation_rows = report.get("market_recommendations") or report.get("rows") or []
    rows_html = "".join(
        "<tr>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}</td>"
        f"<td>{item.get('verdict') or '-'}<div class='muted'>{html.escape(format_trade_status(item.get('tradability_status'), lang=lang))}</div></td>"
        f"<td>{item.get('confidence') if item.get('confidence') is not None else '-'}</td>"
        f"<td>{item.get('quant_rank') or '-'}<div class='muted'>验证分: {item.get('verification_score') or '-'}</div></td>"
        f"<td>{item.get('strategy') or '-'}<div class='muted'>仓位: {item.get('target_weight') or '-'}</div><div class='muted'>就绪度: {item.get('trade_readiness_score') or '-'} / {item.get('readiness_bucket') or '-'}</div><div class='muted'><a href='{html.escape(_reason_screen_href(reason=item.get('block_reason'), status=item.get('tradability_status'), market=item.get('market'), lang=lang), quote=True)}'>{html.escape(build_trade_explain_text(item, lang=lang))}</a></div><div class='muted'>{html.escape(str(item.get('report_pool_reason') or '-'))}</div><div class='muted'><a href='{html.escape(_reason_screen_href(reason=item.get('block_reason'), status=item.get('tradability_status'), market=item.get('market'), lang=lang), quote=True)}'>{t(lang, '查看同类筛选', 'Open screener')}</a></div></td>"
        f"<td>{item.get('entry_trigger') or '-'}<div class='muted'>失效: {item.get('invalidation_condition') or '-'}</div></td>"
        f"<td>{item.get('time_horizon') or '-'}<div class='muted'>滑点: {item.get('max_slippage_bps') or '-'}bps · 流动性: {item.get('liquidity_bucket') or '-'}</div></td>"
        f"<td>{item.get('verification_note') or '-'}<div class='muted'>止损: {item.get('stop_loss', '-')} · {item.get('stop_loss_type') or '-'}</div></td>"
        f"<td>{item.get('headline') or '-'}<div class='muted'>{item.get('summary') or '-'}</div></td>"
        "</tr>"
        for item in market_recommendation_rows[:5]
    ) or f"<tr><td colspan='10'>{t(lang, '当前没有满足条件的可执行买入池，今天更适合少做或只观察。', 'No executable buy-pool candidates right now. Today is better treated as a watch-first session.')}</td></tr>"
    social_payload = report.get("social_signal_summary") or {}
    social_signal_rows = social_payload.get("actionable") or []
    social_accounts = social_payload.get("accounts") or []
    social_signal_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('handle') or '-'))}</td>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('social_view') or '-'))}</td>"
        f"<td>{int(item.get('validation_score') or 0)}</td>"
        f"<td>{html.escape(str(item.get('model_signal_label') or '-'))}<div class='muted'>score {html.escape(str(item.get('model_score') if item.get('model_score') is not None else '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('system_action') or '-'))}</td>"
        f"<td>{html.escape(' / '.join(item.get('validation_reasons') or []) or '-')}</td>"
        "</tr>"
        for item in social_signal_rows[:8]
    ) or f"<tr><td colspan='7'>{t(lang, '暂无可验证社交信号。请先在社交信号页导入已追踪账号的 X 帖子。', 'No validated social signals yet. Import X posts from tracked accounts first.')}</td></tr>"
    social_account_text = ", ".join(str(item.get("handle") or "") for item in social_accounts) or "-"
    us_hotspot_rows = report.get("us_hotspot_validation") or []
    us_hotspot_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('handle') or '-'))}</td>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('social_view') or '-'))}<div class='muted'>社交分 {int(item.get('validation_score') or 0)}</div></td>"
        f"<td>{html.escape(str(item.get('template') or '-'))}<div class='muted'>Top #{int(item.get('us_rank') or 0)}</div></td>"
        f"<td>{html.escape(str(item.get('action_label') or '-'))}<div class='muted'>趋势 {html.escape(str(item.get('trend_score') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('cross_validation_note') or '-'))}</td>"
        "</tr>"
        for item in us_hotspot_rows[:8]
    ) or f"<tr><td colspan='6'>{t(lang, '暂无 X 热点美股与美股模型 Top 候选重合。请先导入 X 帖子，并运行美股预计算 job。', 'No overlap between X U.S. mentions and U.S. model top candidates yet. Import X posts and run U.S. precompute first.')}</td></tr>"

    return render_dashboard_legacy_page(
        "dashboard/legacy/reports_dashboard_ai_daily_report.html",
        fragments=[
            f'{lang}',
            f"{t(lang, 'A股 AI 每日决策面板', 'AI Daily Dashboard')}",
            f'{AI_REPORT_NAME_CELL_STYLE}',
            f"{t(lang, 'AI 日报', 'AI Report')}",
            f"{t(lang, '把 AI 每日复盘、候选动作和推送文本集中在一个稳定入口。', 'Keep the AI daily review, candidate actions, and push-ready text in one stable workspace.')}",
            f'{nav_html}',
            f'{lang}',
            f"{t(lang, '返回首页', 'Back to Dashboard')}",
            f'{lang}',
            f"{t(lang, '打开任务中心', 'Open Task Center')}",
            f'{lang}',
            f"{t(lang, '盘前便签', 'Premarket Plan')}",
            f'{lang}',
            f"{t(lang, '重点监控台', 'Live Monitor')}",
            f'{lang}',
            f"{t(lang, '打开推送文本', 'Open Push Text')}",
            f'{lang}',
            f"{t(lang, '历史记录', 'History')}",
            f"{t(lang, 'AI 每日复盘', 'AI Daily Review')}",
            f"{report.get('mood') or '-'}",
            f"{report.get('headline') or '-'}",
            f"{(report.get('strategy') or {}).get('headline') or '-'}",
            f"{(report.get('strategy') or {}).get('playbook') or '-'}",
            f"{lightgbm_execution_bias.get('title') or 'LightGBM：今天先观察'}",
            f"{lightgbm_execution_bias.get('summary') or '-'}",
            f"""{''.join((f"<div class='muted'>• {html.escape(_report_text_with_security_names(item, report))}</div>" for item in (report.get('strategy') or {}).get('bullets') or [])) or "<div class='muted'>-</div>"}""",
            f'{lang}',
            f"{t(lang, '打开 A股推送文本', 'Open push-ready text')}",
            f'{lang}',
            f"{t(lang, '发送 A股日报到已配置渠道', 'Send report to configured channels')}",
            f'{force_send_cta}',
            f"""{("<div class='muted' style='margin-top:10px;color:#fbbf24;'>" + send_guard_note + '</div>' if send_guard_note else '')}""",
            f"{t(lang, '使用方式', 'How to use')}",
            f"{t(lang, '日报现在分四段：持仓复核、A股全市场 Top 5、X 社交信号验证，以及 X 热点美股和美股模型候选交叉验证。', 'The report now has four parts: portfolio review, A-share full-market Top 5, X social validation, and X U.S. hotspot cross-validation.')}",
            f"{t(lang, '持仓摘要', 'Portfolio Summary')}",
            f"{portfolio_summary.get('headline') or '-'}",
            f"{portfolio_summary.get('action_note') or '-'}",
            f"{t(lang, '社交账号', 'Social accounts')}",
            f'{html.escape(social_account_text)}',
            f'{model_guidance_card_html}',
            f'{regression_discipline_card()}',
            f'{kronos_card_html}',
            f"{t(lang, '一、持仓库总结', '1. Portfolio Review')}",
            f"{portfolio_summary.get('headline') or '-'}",
            f'{portfolio_rows_html}',
            f"{t(lang, '二、明日可执行买入池', '2. Executable Buy Pool')}",
            f"{t(lang, '这里只保留更接近计划买点、且交易状态更适合次日执行的股票。', 'Only keep names that are still close to the planned buy zone and structurally more executable for the next session.')}",
            f'{recommendation_meta_badge(market_recommendation_meta)}',
            f"{structure_block('A股固定结构' if lang == 'zh' else 'A-Share structure', market_structure)}",
            f"{template_attribution_block('A股来源归因' if lang == 'zh' else 'A-Share template attribution', market_template_attribution)}",
            f'{rows_html}',
            f"{t(lang, '三、X 账户社交信号验证', '3. X Account Signal Validation')}",
            f"{t(lang, '这里不是直接照单买入，而是把社交观点和模型信号、触发条件、自选/持仓状态做交叉验证。', 'This does not copy trades directly; it cross-validates social views against model signals, triggers, watchlist, and portfolio state.')}",
            f'{social_signal_rows_html}',
            f"{t(lang, '四、X 热点美股验证', '4. X U.S. Hotspot Validation')}",
            f"{t(lang, '把 X 帖子里提到的美股，与后台预计算的美股模型候选做交叉验证。没有重合时不强行推荐。', 'Cross-check U.S. tickers mentioned on X against precomputed U.S. model candidates. No overlap means no forced recommendation.')}",
            f'{us_hotspot_rows_html}',
            f'{recommendation_meta_badge(us_market_recommendation_meta)}',
            f"{structure_block('美股固定结构' if lang == 'zh' else 'U.S. structure', us_market_structure)}",
        ],
    )



@router.get("/ai-daily-report/history", response_class=HTMLResponse)
def dashboard_ai_daily_report_history(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report/history")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="daily_report")
    bias_filter = str(request.query_params.get("bias_filter") or "ALL").strip().upper()
    window_filter = str(request.query_params.get("window") or "ALL").strip().upper()
    history = list_ai_daily_report_history(limit=60, db=db)

    def _bias_bucket(payload: dict) -> str:
        bias = payload.get("lightgbm_execution_bias") or {}
        title = str(bias.get("title") or "").strip().lower()
        if "突破" in title or "breakout" in title:
            return "BREAKOUT"
        if "回踩" in title or "pullback" in title:
            return "PULLBACK"
        if "观察" in title or "watch" in title:
            return "WATCH"
        return "UNKNOWN"

    tagged_history: list[dict] = []
    today = datetime.now(timezone(timedelta(hours=8))).date()

    def _within_window(snapshot_date: str) -> bool:
        if window_filter == "ALL":
            return True
        try:
            snap_date = datetime.strptime(snapshot_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return False
        if window_filter == "7D":
            return snap_date >= (today - timedelta(days=6))
        if window_filter == "30D":
            return snap_date >= (today - timedelta(days=29))
        return True

    for item in history:
        payload = item.get("payload") or {}
        snapshot_date = str(item.get("snapshot_date") or payload.get("report_date") or "")
        tagged_history.append(
            {
                **item,
                "_bias_bucket": _bias_bucket(payload),
                "_in_window": _within_window(snapshot_date),
            }
        )

    tagged_history = [item for item in tagged_history if item.get("_in_window")]
    if bias_filter != "ALL":
        history = [item for item in tagged_history if item.get("_bias_bucket") == bias_filter]
    else:
        history = tagged_history

    counts = {
        key: sum(1 for item in tagged_history if item.get("_bias_bucket") == key)
        for key in ("BREAKOUT", "PULLBACK", "WATCH")
    }
    history_cache: dict[tuple[str, str], list[dict]] = {}

    def _bucket_stats(items: list[dict]) -> dict:
        window_values: dict[int, list[float]] = {1: [], 3: [], 5: [], 10: []}
        measured_count = 0
        for item in items:
            payload = item.get("payload") or {}
            report_date = str(item.get("snapshot_date") or payload.get("report_date") or "")
            rows = _report_market_rows(payload)
            for row in rows[:5]:
                ticker = str(row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                market_code = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US")
                cache_key = (market_code, ticker)
                if cache_key not in history_cache:
                    history_cache[cache_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                history_rows = history_cache.get(cache_key) or []
                row_measured = False
                for session in (1, 3, 5, 10):
                    value = _forward_return_from_history(history_rows, trade_date=report_date, sessions=session)
                    if value is None:
                        continue
                    window_values[session].append(float(value))
                    row_measured = True
                if row_measured:
                    measured_count += 1
        return {
            "report_count": len(items),
            "measured_count": measured_count,
            "windows": {session: _aggregate_window_stats(values) for session, values in window_values.items()},
        }

    current_stats = _bucket_stats(history)
    current_windows = current_stats.get("windows") or {}
    avg_return = (current_windows.get(5) or {}).get("avg_return")
    hit_rate = (current_windows.get(5) or {}).get("hit_rate")
    measured_count = int(current_stats.get("measured_count") or 0)
    hit_count = int((current_windows.get(5) or {}).get("count") or 0)
    bucket_labels = {
        "BREAKOUT": "突破偏向" if lang == "zh" else "Breakout",
        "PULLBACK": "回踩偏向" if lang == "zh" else "Pullback",
        "WATCH": "观察偏向" if lang == "zh" else "Watch",
    }
    comparison_rows = ""
    for key in ("BREAKOUT", "PULLBACK", "WATCH"):
        bucket_items = [item for item in tagged_history if item.get("_bias_bucket") == key]
        bucket_stats = _bucket_stats(bucket_items)
        row_href = f"/dashboard/ai-daily-report/history?lang={lang}&bias_filter={key}&window={window_filter}"
        comparison_rows += (
            f"<tr style='cursor:pointer;' onclick=\"window.location.href='{row_href}'\">"
            f"<td><a href='{row_href}' style='font-weight:800;color:var(--ink);'>{bucket_labels[key]}</a></td>"
            f"<td>{int(bucket_stats.get('report_count') or 0)}</td>"
            f"<td>{int(bucket_stats.get('measured_count') or 0)}</td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not comparison_rows:
        comparison_rows = f"<tr><td colspan='7'>{t(lang, '暂无可对照的偏向统计。', 'No comparable bias statistics yet.')}</td></tr>"

    def _filter_pill(label: str, key: str) -> str:
        active = bias_filter == key
        href = f"/dashboard/ai-daily-report/history?lang={lang}&bias_filter={key}&window={window_filter}"
        style = (
            "border-color:rgba(61,217,182,0.55);color:var(--accent);"
            if active
            else ""
        )
        return f"<a class='pill' href='{href}' style='{style}'>{label}</a>"

    def _window_pill(label: str, key: str) -> str:
        active = window_filter == key
        href = f"/dashboard/ai-daily-report/history?lang={lang}&bias_filter={bias_filter}&window={key}"
        style = (
            "border-color:rgba(61,217,182,0.55);color:var(--accent);"
            if active
            else ""
        )
        return f"<a class='pill' href='{href}' style='{style}'>{label}</a>"

    filter_pills = "".join(
        [
            _filter_pill("全部" if lang == "zh" else "All", "ALL"),
            _filter_pill((f"突破偏向 {counts['BREAKOUT']}" if lang == "zh" else f"Breakout {counts['BREAKOUT']}"), "BREAKOUT"),
            _filter_pill((f"回踩偏向 {counts['PULLBACK']}" if lang == "zh" else f"Pullback {counts['PULLBACK']}"), "PULLBACK"),
            _filter_pill((f"观察偏向 {counts['WATCH']}" if lang == "zh" else f"Watch {counts['WATCH']}"), "WATCH"),
        ]
    )
    window_pills = "".join(
        [
            _window_pill("全部时间" if lang == "zh" else "All time", "ALL"),
            _window_pill("近 7 天" if lang == "zh" else "Last 7D", "7D"),
            _window_pill("近 30 天" if lang == "zh" else "Last 30D", "30D"),
        ]
    )
    summary_cards = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:16px;">
      <div class="card" style="padding:16px;border-radius:18px;box-shadow:none;">
        <div class="eyebrow">{t(lang, '当前筛选', 'Current filter')}</div>
        <div style="font-size:22px;font-weight:900;">{html.escape({'ALL':'全部','BREAKOUT':'突破','PULLBACK':'回踩','WATCH':'观察'}.get(bias_filter, bias_filter) if lang == 'zh' else bias_filter.title())}</div>
        <div class="muted">{t(lang, '历史日报条数', 'Reports')}: {len(history)} · {html.escape({'ALL':'全部时间','7D':'近 7 天','30D':'近 30 天'}.get(window_filter, window_filter) if lang == 'zh' else {'ALL':'All time','7D':'Last 7D','30D':'Last 30D'}.get(window_filter, window_filter))}</div>
      </div>
      <div class="card" style="padding:16px;border-radius:18px;box-shadow:none;">
        <div class="eyebrow">{t(lang, '5日平均收益', '5D Avg Return')}</div>
        <div style="font-size:22px;font-weight:900;">{_fmt_optional_float(avg_return, suffix='%', digits=2) if avg_return is not None else '-'}</div>
        <div class="muted">{t(lang, '基于候选池可测样本', 'Across measurable candidate-pool rows')}: {measured_count}</div>
      </div>
      <div class="card" style="padding:16px;border-radius:18px;box-shadow:none;">
        <div class="eyebrow">{t(lang, '5日上涨命中率', '5D Hit Rate')}</div>
        <div style="font-size:22px;font-weight:900;">{_fmt_optional_float(hit_rate, suffix='%', digits=1) if hit_rate is not None else '-'}</div>
        <div class="muted">{t(lang, '5日可测样本', '5D measured rows')}: {hit_count}</div>
      </div>
    </div>
    """
    rows_html = ""
    for item in history:
        payload = item.get("payload") or {}
        top5 = _report_market_rows(payload)
        lightgbm_execution_bias = payload.get("lightgbm_execution_bias") or {}
        report_stats = _bucket_stats([item])
        top5_text = ", ".join(
            (
                f"{str(row.get('name') or '').strip()}（{str(row.get('ticker') or '').strip()}）"
                if str(row.get("name") or "").strip() and str(row.get("ticker") or "").strip() and str(row.get("name") or "").strip() != str(row.get("ticker") or "").strip()
                else str(row.get("name") or row.get("ticker") or "").strip()
            )
            for row in top5[:5]
            if row.get("ticker") or row.get("name")
        ) or "-"
        actionable_count = len(payload.get("market_recommendations") or payload.get("rows") or [])
        portfolio_rows = payload.get("portfolio_rows") or []
        rows_html += (
            "<tr>"
            f"<td><a href='/dashboard/ai-daily-report/history/{int(item.get('id'))}?lang={lang}'>{html.escape(str(item.get('snapshot_date') or '-'))}</a>"
            f"<div class='muted'>#{int(item.get('id'))} · {_display_time(item.get('created_at'), with_tz=True)}</div></td>"
            f"<td>{html.escape(str(payload.get('mood') or '-'))}"
            f"<div class='muted'>{html.escape(str(payload.get('headline') or '-'))}</div>"
            f"<div class='muted' style='margin-top:6px;color:var(--ink);font-weight:700;'>{html.escape(str(lightgbm_execution_bias.get('title') or 'LightGBM：未记录'))}</div>"
            f"<div class='muted'>{html.escape(str(lightgbm_execution_bias.get('summary') or '-'))}</div></td>"
            f"<td>{len(portfolio_rows)}</td>"
            f"<td>{html.escape(top5_text)}<div class='muted'>{t(lang, '可执行', 'Executable')} {actionable_count}</div></td>"
            f"<td>{_fmt_optional_float(((report_stats.get('windows') or {}).get(1) or {}).get('avg_return'), suffix='%', digits=2)}"
            f"<div class='muted'>3D {_fmt_optional_float(((report_stats.get('windows') or {}).get(3) or {}).get('avg_return'), suffix='%', digits=2)}</div>"
            f"<div class='muted'>5D {_fmt_optional_float(((report_stats.get('windows') or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(((report_stats.get('windows') or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div>"
            f"<div class='muted'>10D {_fmt_optional_float(((report_stats.get('windows') or {}).get(10) or {}).get('avg_return'), suffix='%', digits=2)}</div></td>"
            f"<td><a class='cta' href='/dashboard/ai-daily-report/history/{int(item.get('id'))}?lang={lang}'>{t(lang, '打开', 'Open')}</a></td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = f"<tr><td colspan='6'>{t(lang, '暂无历史日报。下一次生成或发送 AI 日报后会自动保存。', 'No historical reports yet. The next generated or sent AI report will be archived automatically.')}</td></tr>"

    return render_dashboard_legacy_page(
        "dashboard/legacy/reports_dashboard_ai_daily_report_history.html",
        fragments=[
            f'{lang}',
            f"{t(lang, 'AI 日报历史记录', 'AI Report History')}",
            f'{WINDOW_PILL_STYLE}',
            f"{t(lang, '日报历史', 'Report History')}",
            f"{t(lang, '保留每次 AI 日报，方便后续对照推荐是否走出来。', 'Archive every AI report so later outcomes can be reviewed.')}",
            f'{nav_html}',
            f'{lang}',
            f"{t(lang, '返回 AI 日报', 'Back to AI Report')}",
            f'{lang}',
            f"{t(lang, '任务中心', 'Ops')}",
            f"{t(lang, '历史日报', 'Historical Reports')}",
            f"{t(lang, '日报留档', 'Report Archive')}",
            f"{t(lang, '每次生成或发送 AI 日报都会新增一条记录，不覆盖旧版本。后面我们可以在这里继续加“命中率/收益验证”。', 'Every generated or sent AI report is archived without overwriting older versions. Outcome tracking can be added here next.')}",
            f'{filter_pills}',
            f'{window_pills}',
            f'{summary_cards}',
            f"{t(lang, '点击下面任一偏向行，可以直接筛出对应日报。主值是平均收益，小字是上涨命中率。', 'Click any bias row below to filter the archive directly. Main values are average returns and muted values are hit rates.')}",
            f"{t(lang, '执行偏向', 'Bias')}",
            f"{t(lang, '日报数', 'Reports')}",
            f"{t(lang, '可测样本', 'Measured')}",
            f'{comparison_rows}',
            f"{t(lang, '日期', 'Date')}",
            f"{t(lang, '市场判断', 'Market View')}",
            f"{t(lang, '持仓数', 'Holdings')}",
            f"{t(lang, '候选池', 'Candidate Pool')}",
            f"{t(lang, '历史验证', 'Validation')}",
            f"{t(lang, '操作', 'Action')}",
            f'{rows_html}',
        ],
    )



@router.get("/ai-daily-report/history/{snapshot_id}", response_class=HTMLResponse)
def dashboard_ai_daily_report_history_detail(snapshot_id: int, request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/ai-daily-report/history/{snapshot_id}")
    lang = resolve_request_lang(request)
    snapshot = load_ai_daily_report_history_item(snapshot_id, db=db)
    if snapshot is None:
        return HTMLResponse("Not found", status_code=404)
    report = _hydrate_ai_report_names(snapshot.get("payload") or {}, db=db)
    report_date = str(snapshot.get("snapshot_date") or report.get("report_date") or "")
    outcome_rows = _report_outcome_rows(report, report_date=report_date)
    actionable_rows = []
    for source in list(report.get("market_recommendations") or report.get("rows") or [])[:5]:
        reason_href = _reason_screen_href(
            reason=source.get("block_reason"),
            status=source.get("tradability_status"),
            market=source.get("market"),
            lang=lang,
        )
        actionable_rows.append({
            **source,
            "display_trade_status": format_trade_status(source.get("tradability_status"), lang=lang),
            "explain_text": build_trade_explain_text(source, lang=lang),
            "reason_href": reason_href,
        })
    return render_ai_report_history_detail_page(
        snapshot=snapshot,
        report=report,
        message=render_ai_daily_report_message(report),
        outcome_rows=outcome_rows,
        outcome_summary=_report_outcome_summary(outcome_rows, lang=lang),
        actionable_rows=actionable_rows,
        model_guidance_card_html=_render_ai_report_guidance_bridge(report, lang=lang),
        lang=lang,
        nav_html=render_workspace_nav_html(lang=lang, active_key="daily_report"),
    )


@router.get("/ai-daily-report/message", response_class=HTMLResponse)
def dashboard_ai_daily_report_message(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report/message")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="daily_report")
    report = _load_cached_ai_daily_report(db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先刷新行情或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "rows": [],
        "buy_the_dip_rows": [],
    }
    if not report.get("social_signal_summary"):
        current_social_summary = social_signal_summary(db)
        report = {
            **report,
            "social_signal_summary": {
                "accounts": current_social_summary.get("accounts") or [],
                "actionable": current_social_summary.get("actionable") or [],
            },
        }
    message = render_ai_daily_report_message(report)
    return render_ai_daily_message_page(lang=lang, nav_html=nav_html, message=message)
