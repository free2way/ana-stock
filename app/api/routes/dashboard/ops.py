"""Ops pages: sync, models, today, history, jobs and job detail."""

import html

import json

import re

from collections import Counter

from urllib.parse import urlencode

from datetime import date, datetime, time

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from fastapi.responses import HTMLResponse

from sqlalchemy import select

from sqlalchemy.orm import Session

from app.api.presentation.i18n import t
from app.api.presentation.dashboard_legacy import render_dashboard_legacy_page
from app.api.presentation.dashboard_ops_history import (
    render_ops_history_page,
    render_ops_job_detail_page,
    render_task_status_badge as _task_status_badge,
    task_duration as _task_duration,
)
from app.api.presentation.dashboard_ops_jobs import render_ops_jobs_page
from app.services.display_compaction import compact_json_summary as _compact_json_summary

from app.api.presentation.styles_dashboard import (
    RISK_CARD_STYLE,
    LOAD_CN_SYNC_STATS_STYLE,
    DASHBOARD_OPS_MODELS_PAGE_STYLE,
    JOB_DETAIL_LINK_STYLE,
)

from app.core.config import get_settings

from app.core.db import get_db_session

from app.models.tables import DataJob

from app.services.ai_daily_report import (
    format_trade_gate_reason,
)

from app.services.auth import is_authenticated, login_redirect

from app.services.dashboard_summary import load_recent_jobs_summary

from app.services.market_lake import (
    get_latest_lake_trade_date,
)

from app.services.market_risk import PORTFOLIO_RISK_ALERT_SNAPSHOT_TYPE, market_risk_snapshot_type

from app.services.repository import (
    ConceptSnapshotRepository,
    DataJobRepository,
    DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
    FundamentalSnapshotRepository,
    PointInTimeFeatureSnapshotRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WorkspaceSnapshotRepository,
)

from app.services.stock_selection.feature_availability import FUNDAMENTAL_FEATURE_NAMES

from app.services.stock_selection.forward_shadow import CN_FORWARD_SHADOW_SNAPSHOT_TYPE

from app.services.stock_selection.point_in_time_features import DEFAULT_MAX_AGE_DAYS

from app.services.runtime_cache import get_or_set

from app.services.time_utils import app_today_iso, parse_app_datetime

from app.services.ui_lang import resolve_request_lang

from app.services.workspace_nav import render_workspace_nav_html

from app.services.workspace_snapshots import (
    load_latest_workspace_snapshot,
)


from app.api.routes.dashboard._common import _clamp_lookback_runs, _compact_run_name, _display_job_message, _dt, _find_latest_job_by_type, _load_home_summary, _load_summary, _summarize_screener_precompute_job
from app.services.dashboard_insights import (
    _display_time,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


SCREENER_PRECOMPUTE_STAGE_CONFIG = [
    ("screener_precompute", "总控预计算", "Staged Precompute"),
    ("screener_precompute_core", "核心模型预计算", "Core Precompute"),
    ("screener_precompute_combos", "组合预计算", "Combo Precompute"),
    ("screener_precompute_rest", "补全预计算", "Rest Precompute"),
]



def _build_screener_precompute_stage_rows(recent_jobs: list[dict], *, lang: str = "zh") -> list[dict]:
    rows: list[dict] = []
    parent_job = _find_latest_job_by_type(recent_jobs, "screener_precompute")
    for job_type, label_zh, label_en in SCREENER_PRECOMPUTE_STAGE_CONFIG:
        job = _find_latest_job_by_type(recent_jobs, job_type)
        if job is None and job_type == "screener_precompute_core" and isinstance(parent_job, dict):
            parent_summary = _summarize_screener_precompute_job(parent_job, lang=lang)
            if str(parent_summary.get("summary") or "") in {"核心已完成", "Core Done"}:
                job = {
                    "job_type": job_type,
                    "status": "success",
                    "message": "Inherited core completion from parent staged precompute job.",
                    "result": {"count": 1},
                }
        summary = _summarize_screener_precompute_job(job, lang=lang)
        rows.append(
            {
                "job_type": job_type,
                "label": label_zh if lang == "zh" else label_en,
                "job": job,
                "summary": summary["summary"],
                "detail": summary["detail"],
                "status": summary["status"],
            }
        )
    return rows



def _render_screener_precompute_action_forms(
    *,
    lang: str = "zh",
    redirect_to: str,
    compact: bool = False,
    stage_rows: list[dict] | None = None,
) -> str:
    labels = {
        "run_all": "运行整条预计算链" if lang == "zh" else "Run Full Precompute Chain",
        "run_core": "只跑核心" if lang == "zh" else "Run Core Only",
        "run_combos": "只跑组合" if lang == "zh" else "Run Combos Only",
        "run_rest": "只跑补全" if lang == "zh" else "Run Rest Only",
    }
    actions = [
        ("/jobs/precompute-cn-screeners", labels["run_all"], True, "screener_precompute"),
        ("/jobs/precompute-cn-screeners-core", labels["run_core"], False, "screener_precompute_core"),
        ("/jobs/precompute-cn-screeners-combos", labels["run_combos"], False, "screener_precompute_combos"),
        ("/jobs/precompute-cn-screeners-rest", labels["run_rest"], False, "screener_precompute_rest"),
    ]
    stage_map = {str(item.get("job_type") or ""): item for item in (stage_rows or [])}
    forms = "".join(
        "<div class='action-with-note'>"
        "<form action='{action}' method='post' class='inline-form'>"
        "<input type='hidden' name='redirect_to' value='{redirect_to}' />"
        "<button class='{button_class}' type='submit'>{label}</button>"
        "</form>"
        "<div class='subtle action-receipt'>{receipt}</div>"
        "</div>".format(
            action=action,
            redirect_to=html.escape(redirect_to, quote=True),
            button_class=("cta compact primary" if compact and is_primary else "cta compact" if compact else "cta primary" if is_primary else "cta"),
            label=html.escape(label),
            receipt=html.escape(
                (
                    f"最近：{stage_map.get(job_type, {}).get('summary') or (t(lang, '待运行', 'Pending'))}"
                    + " · "
                    + (stage_map.get(job_type, {}).get("detail") or (t(lang, "还没有任务记录。", "No job record yet.")))
                )
                if lang == "zh"
                else (
                    f"Latest: {stage_map.get(job_type, {}).get('summary') or 'Pending'}"
                    + " · "
                    + (stage_map.get(job_type, {}).get("detail") or "No job record yet.")
                )
            ),
        )
        for action, label, is_primary, job_type in actions
    )
    note = (
        "组合预计算依赖核心快照；如果组合失败，先补跑核心。"
        if lang == "zh"
        else "Combo precompute depends on core snapshots; rerun core first if combos fail."
    )
    return forms + f"<div class='subtle precompute-note'>{html.escape(note)}</div>"



def _latest_cn_refresh_summary(db: Session, recent_jobs: list[dict] | None = None, *, lang: str = "zh") -> dict:
    jobs = recent_jobs or DataJobRepository(db).list_recent_jobs(limit=10)
    refresh_job = next(
        (
            item
            for item in jobs
            if str(item.get("job_type") or "").lower()
            in {"refresh_cn_market_data_lake_only", "refresh_cn_market_data_daily", "refresh_cn_market_data"}
            and str(item.get("status") or "").lower() in {"success", "partial"}
        ),
        None,
    )
    cn_total = len([symbol for symbol in SymbolRepository(db).list_symbols() if (symbol.market or "").upper() == "CN"])
    result = (refresh_job or {}).get("result")
    if not isinstance(result, dict):
        result = {}
    message = str((refresh_job or {}).get("message") or "")
    refreshed = int(result.get("success_count") or result.get("rows_written") or 0) or None
    if refreshed is None:
        match = re.search(r"(?:wrote|Refreshed)\s+(\d+)\s+(?:row|stock)", message, re.IGNORECASE)
        refreshed = int(match.group(1)) if match else None
    summary = f"{refreshed}/{cn_total}" if refreshed is not None and cn_total else (str(refreshed) if refreshed is not None else "-")
    if refreshed is not None:
        label = f"本轮刷新 {refreshed}/{cn_total} 只 A 股" if lang == "zh" else f"Refreshed {refreshed}/{cn_total} CN symbols"
    else:
        label = "暂无全市场刷新结果" if lang == "zh" else "No CN refresh result yet"
    return {
        "job": refresh_job,
        "refreshed": refreshed,
        "total": cn_total,
        "summary": summary,
        "label": label,
    }



def _task_action_form(*, action: str, redirect_to: str, label: str, fields: dict[str, str] | None = None) -> str:
    inputs = [f"<input type='hidden' name='redirect_to' value='{html.escape(redirect_to, quote=True)}' />"]
    for key, value in (fields or {}).items():
        inputs.append(f"<input type='hidden' name='{html.escape(key, quote=True)}' value='{html.escape(value, quote=True)}' />")
    return f"<form action='{action}' method='post'>{''.join(inputs)}<button type='submit'>{html.escape(label)}</button></form>"



def _jobs_started_on_date(db: Session, selected_date: str) -> list[dict]:
    """Return runs by the application (Shanghai) calendar date, never SQLite."""

    job_repo = DataJobRepository(db)
    rows = [
        job_repo._serialize_job(job)
        for job in db.scalars(
            select(DataJob)
            .where(DataJob.started_at.startswith(selected_date))
            .order_by(DataJob.id.desc())
        ).all()
    ]
    return [
        job
        for job in rows
        if (
            (parsed := parse_app_datetime(str(job.get("started_at") or ""))) is not None
            and parsed.date().isoformat() == selected_date
            and str(job.get("job_type") or "").lower() != DECOMMISSIONED_CN_REVIEW_JOB_TYPE
        )
    ]



def _today_job_status_counts(jobs: list[dict]) -> Counter:
    return Counter(str(job.get("status") or "idle").lower() for job in jobs)



def _render_task_center_redesign(*, request: Request, lang: str, lookback_runs: int, db: Session, summary: dict) -> str:
    job_repo = DataJobRepository(db)
    redirect_to = "/dashboard/ops?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    today = app_today_iso()
    job_status = request.query_params.get("job_status")
    job_message = request.query_params.get("job_message")
    job_id = request.query_params.get("job_id")
    banner_html = ""
    if job_status or job_message:
        banner_html = (
            f"<div class='notice'>{html.escape((t(lang, '任务 ', 'Job ')) + str(job_id or '-'))} · "
            f"{html.escape(_display_job_message(job_message or job_status or '-', lang=lang))}</div>"
        )

    cn_daily_specs = [
        {
            "name": "A 股行情刷新" if lang == "zh" else "A-share price refresh",
            "note": "增量拉取最近行情并重建技术快照。" if lang == "zh" else "Incremental price refresh and technical-snapshot rebuild.",
            "types": ("refresh_cn_market_data_lake_only", "refresh_cn_market_data_daily", "refresh_cn_market_data"),
            "action": _task_action_form(
                action="/jobs/refresh-cn-market-data-daily", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
                fields={"background": "true", "days_back": "7", "overlap_days": "3", "provider": "auto"},
            ),
        },
        {
            "name": "A 股信号训练" if lang == "zh" else "A-share signal training",
            "note": "基于已刷新行情重训 LightGBM 并写入预测。" if lang == "zh" else "Retrain LightGBM and write predictions from refreshed prices.",
            "types": ("train_cn_signals",),
            "action": _task_action_form(
                action="/jobs/train-cn-signals", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
                fields={"background": "true", "run_name": "cn_manual_refresh_lightgbm", "model_type": "lightgbm", "signal_type": "momentum", "lookback_days": "3"},
            ),
        },
        {
            "name": "核心选股预计算" if lang == "zh" else "Core screener precompute",
            "note": "更新任务中心与选股页最常用的核心候选快照。" if lang == "zh" else "Refresh the core candidate snapshots used by the workspace.",
            "types": ("screener_precompute_core", "screener_precompute"),
            "action": _task_action_form(
                action="/jobs/precompute-cn-screeners-core", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
            ),
        },
    ]
    us_daily_specs = [
        {
            "name": "美股收盘行情" if lang == "zh" else "U.S. closing prices",
            "note": "通过 Polygon grouped daily 写入美股 Parquet 行情湖。" if lang == "zh" else "Write U.S. grouped daily bars into the Parquet market lake.",
            "types": ("refresh_us_grouped_daily",),
            "action": _task_action_form(
                action="/jobs/refresh-us-grouped-daily", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
                fields={"background": "true", "adjusted": "true", "write_lake": "true"},
            ),
        },
        {
            "name": "美股信号训练" if lang == "zh" else "U.S. signal training",
            "note": "清理可交易股票池，训练 LightGBM 并更新回测。" if lang == "zh" else "Clean the tradable universe, train LightGBM, and update the backtest.",
            "types": ("us_signal_train",),
            "action": _task_action_form(
                action="/jobs/train-us-signals", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
                fields={"background": "true", "run_name": "us_manual_refresh_lightgbm", "model_type": "lightgbm", "signal_type": "momentum", "lookback_days": "3", "top_n": "5"},
            ),
        },
        {
            "name": "美股候选预计算" if lang == "zh" else "U.S. candidate precompute",
            "note": "基于美股市场湖重算 LightGBM、强趋势二次启动和技术动量三套核心候选快照。" if lang == "zh" else "Recompute LightGBM, Next Tesla Swing, and Technical Momentum candidate snapshots from the U.S. market lake.",
            "types": ("precompute_us_screeners",),
            "action": _task_action_form(
                action="/jobs/precompute-us-screeners", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
                fields={"lake_only": "true"},
            ),
        },
    ]
    shared_daily_specs = [
        {
            "name": "风险预警与买入闸门" if lang == "zh" else "Risk guardrail",
            "note": "识别暴跌、反抽失败和美股高离散环境，并生成持仓风险提醒。" if lang == "zh" else "Detect crash/rebound-failure/high-dispersion regimes and portfolio risk alerts.",
            "types": ("risk_guardrail_snapshot",),
            "action": _task_action_form(
                action="/jobs/risk-guardrail-snapshot", redirect_to=redirect_to,
                label="立即运行" if lang == "zh" else "Run now",
                fields={"markets": "CN,US", "lookback_days": "12"},
            ),
        },
        {
            "name": "AI 日报生成" if lang == "zh" else "AI daily report",
            "note": "确认当日决策日报已生成，并可直接查看内容。" if lang == "zh" else "Confirm today's decision report and open its content.",
            "types": ("generate_ai_daily_report", "send_ai_daily_report", "ai_daily_report"),
            "action": _task_action_form(
                action="/jobs/send-ai-daily-report", redirect_to=redirect_to,
                label="生成并发送" if lang == "zh" else "Generate & send",
            ),
        },
    ]
    maintenance_specs = [
        {
            "name": "存储保留清理" if lang == "zh" else "Storage retention cleanup",
            "note": "页面仅允许预览；生产删除只能使用带冻结验收回执和显式令牌的逐批 CLI。" if lang == "zh" else "The page is preview-only; production deletion requires the bounded CLI, frozen acceptance receipts, and an explicit token.",
            "types": ("cleanup_storage_retention",),
            "action": (
                f"<form action='/jobs/cleanup-storage-retention' method='post' class='retention-form'>"
                f"<input type='hidden' name='redirect_to' value='{html.escape(redirect_to, quote=True)}' />"
                f"<label>{t(lang, '模型/市场', 'Models/market')}<input type='number' name='keep_model_runs_per_market' min='1' value='20' /></label>"
                f"<label>{t(lang, '快照/类型', 'Snapshots/type')}<input type='number' name='keep_workspace_snapshots_per_type' min='1' value='10' /></label>"
                f"<button type='submit'>{t(lang, '仅预览', 'Preview only')}</button></form>"
            ),
        },
        {
            "name": "结构化模型评测" if lang == "zh" else "Structured model evaluation",
            "note": "按严格滚动样本外口径写入净收益、回撤、成本和预测当日市场状态；样本不足的模型自动保持观察。" if lang == "zh" else "Persist strict walk-forward OOS returns, drawdowns, costs, and prediction-date market state; insufficient evidence remains observation-only.",
            "types": ("evaluate_model_performance",),
            "action": _task_action_form(
                action="/jobs/evaluate-model-performance", redirect_to=redirect_to,
                label="运行评测" if lang == "zh" else "Evaluate now",
                fields={"markets": "CN,US", "recent_runs": "4", "recent_trade_dates": "12", "top_n": "20", "horizons": "1,3,5,10,20", "round_trip_cost_bps": "20"},
            ),
        },
        {
            "name": "模型赛马门槛检查" if lang == "zh" else "Model challenger gate",
            "note": "仅在严格 OOS 覆盖达到 20 个交易日且样本达到 100 条后，允许启动 XGBoost / CatBoost 对照训练。" if lang == "zh" else "Allow XGBoost/CatBoost challenger training only after 20 strict-OOS sessions and 100 samples.",
            "types": ("model_champion_challenger",),
            "action": _task_action_form(
                action="/jobs/model-champion-challenger", redirect_to=redirect_to,
                label="检查并启动" if lang == "zh" else "Check & launch",
                fields={"markets": "CN,US"},
            ),
        },
        {
            "name": "刷新新闻机会" if lang == "zh" else "Refresh news opportunities",
            "note": "重新抓取自选股相关新闻，更新机会、风险、覆盖率及首页 NLP 快照。" if lang == "zh" else "Fetch watchlist news again and update opportunity, risk, coverage, and dashboard NLP snapshots.",
            "types": ("news_enrichment",),
            "action": _task_action_form(action="/jobs/refresh-news-opportunities", redirect_to=redirect_to, label="立即刷新" if lang == "zh" else "Refresh now"),
        },
        {
            "name": "同步 A 股基本面" if lang == "zh" else "Sync A-share fundamentals",
            "note": "补齐估值、盈利等增强模型字段。" if lang == "zh" else "Refresh valuation and earnings inputs used by enriched models.",
            "types": ("sync_cn_fundamentals",),
            "action": _task_action_form(action="/jobs/sync-cn-fundamentals", redirect_to=redirect_to, label="立即同步" if lang == "zh" else "Sync now"),
        },
        {
            "name": "同步 A 股概念" if lang == "zh" else "Sync A-share concepts",
            "note": "更新概念归属与板块聚合数据。" if lang == "zh" else "Refresh concept membership and sector aggregation.",
            "types": ("sync_cn_concepts",),
            "action": _task_action_form(action="/jobs/sync-cn-concepts", redirect_to=redirect_to, label="立即同步" if lang == "zh" else "Sync now"),
        },
        {
            "name": "同步美股 / 港股基本面" if lang == "zh" else "Sync U.S. / HK fundamentals",
            "note": "补齐海外股票的基本面快照与元数据。" if lang == "zh" else "Refresh overseas fundamental snapshots and metadata.",
            "types": ("sync_global_fundamentals",),
            "action": _task_action_form(action="/jobs/sync-global-fundamentals", redirect_to=redirect_to, label="立即同步" if lang == "zh" else "Sync now"),
        },
        {
            "name": "同步美股 SEC 官方基本面" if lang == "zh" else "Sync U.S. SEC official fundamentals",
            "note": "从 SEC EDGAR 补齐自选美股的财报、收入增长、利润增长与负债率；需配置 PQW_SEC_USER_AGENT。" if lang == "zh" else "Enrich the U.S. watchlist with SEC EDGAR filings, growth, and leverage; requires PQW_SEC_USER_AGENT.",
            "types": ("sync_us_sec_fundamentals",),
            "action": _task_action_form(action="/jobs/sync-us-sec-fundamentals", redirect_to=redirect_to, label="立即同步" if lang == "zh" else "Sync now"),
        },
        {
            "name": "A 股补充行情核验" if lang == "zh" else "A-share supplemental price check",
            "note": "按指定股票用 a-stock-data 的腾讯行情进行小范围补全或交叉核验；不会替代 TuShare 全市场行情湖。" if lang == "zh" else "Use a-stock-data Tencent prices for a small explicit repair or cross-check; never replaces the TuShare market lake.",
            "types": ("sync_market_data",),
            "action": (
                f"<form action='/jobs/sync-market-data' method='post' class='retention-form'>"
                f"<input type='hidden' name='redirect_to' value='{html.escape(redirect_to, quote=True)}' />"
                "<input type='hidden' name='provider' value='a_stock_data_tencent' />"
                f"<label>{t(lang, '股票代码（必填）', 'Tickers (required)')}<input name='tickers' placeholder='600519.SS, 000001.SZ' required /></label>"
                f"<button type='submit'>{t(lang, '核验并补全', 'Check & repair')}</button></form>"
            ),
        },
        {
            "name": "同步 A 股股票池" if lang == "zh" else "Sync A-share universe",
            "note": "维护本地 A 股可用股票清单。" if lang == "zh" else "Maintain the local A-share symbol universe.",
            "types": ("sync_cn_symbol_universe",),
            "action": _task_action_form(action="/jobs/sync-cn-symbol-universe", redirect_to=redirect_to, label="立即同步" if lang == "zh" else "Sync now"),
        },
    ]

    def latest(spec: dict) -> dict | None:
        candidates = [job_repo.get_latest_job(job_type) for job_type in spec["types"]]
        candidates = [job for job in candidates if job]
        return max(candidates, key=lambda job: int(job.get("id") or 0)) if candidates else None

    def render_rows(specs: list[dict]) -> str:
        rows = []
        for spec in specs:
            job = latest(spec)
            status = str((job or {}).get("status") or "idle")
            detail_link = (
                f"<a class='detail-link' href='/dashboard/ops/job/{int(job['id'])}?lang={lang}'>"
                f"{t(lang, '查看详情', 'Details')} →</a>"
                if job else "<span class='muted'>—</span>"
            )
            rows.append(
                "<tr>"
                f"<td><strong>{html.escape(spec['name'])}</strong><span class='job-note'>{html.escape(spec['note'])}</span></td>"
                f"<td>{_display_time((job or {}).get('started_at'))}</td>"
                f"<td>{_display_time((job or {}).get('finished_at'))}</td>"
                f"<td>{_task_duration(job)}</td>"
                f"<td>{_task_status_badge(status, lang=lang)}</td>"
                f"<td class='actions'>{spec['action']}{detail_link}</td>"
                "</tr>"
            )
        return "".join(rows)

    cn_daily_rows = render_rows(cn_daily_specs)
    us_daily_rows = render_rows(us_daily_specs)
    shared_daily_rows = render_rows(shared_daily_specs)
    maintenance_rows = render_rows(maintenance_specs)
    table_header = (
        f"<thead><tr><th>{t(lang, '任务', 'Job')}</th><th>{t(lang, '开始', 'Started')}</th>"
        f"<th>{t(lang, '结束', 'Finished')}</th><th>{t(lang, '持续时间', 'Duration')}</th>"
        f"<th>{t(lang, '状态', 'Status')}</th><th>{t(lang, '操作', 'Actions')}</th></tr></thead>"
    )
    def daily_table(title: str, note: str, rows: str) -> str:
        return (
            f"<div class='market-block'><div class='market-title'>{title}</div><div class='muted'>{note}</div>"
            f"<div class='table-scroll'><table>{table_header}<tbody>{rows}</tbody></table></div></div>"
        )
    daily_sections_html = "".join(
        [
            daily_table("A 股 / CN", "收盘后完成行情、训练与候选预计算。" if lang == "zh" else "Prices, training, and candidate precompute after close.", cn_daily_rows),
            daily_table("美股 / US", "美股收盘后完成 EOD、训练与候选预计算。" if lang == "zh" else "EOD, training, and candidate precompute after the U.S. close.", us_daily_rows),
            daily_table("共享产出" if lang == "zh" else "Shared output", "日报属于跨市场产出，单独展示。" if lang == "zh" else "The report is cross-market output, shown separately.", shared_daily_rows),
        ]
    )
    today_jobs = _jobs_started_on_date(db, today)
    today_counts = _today_job_status_counts(today_jobs)
    recent_count = len(today_jobs)
    today_attention_count = sum(
        count
        for status, count in today_counts.items()
        if status in {"failed", "failed_timeout", "partial", "running"}
    )
    history_href = "/dashboard/ops/history?" + urlencode({"lang": lang, "date": today, "lookback_runs": lookback_runs})
    today_href = "/dashboard/ops/today?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    today_module_html = f"""
    <section class='today-job-module'>
      <div><div class='eyebrow'>{t(lang, '固定入口', 'Pinned view')}</div><h2>{t(lang, '今天所有 Job 运行情况', 'All jobs today')}</h2>
      <p class='muted'>{t(lang, '汇总今天所有任务的状态、时间、耗时与运行回执；异常会自动突出显示。', 'A single report of today’s statuses, timing, durations, and run receipts, with exceptions highlighted.')}</p></div>
      <div class='today-job-actions'><div class='today-job-stats'><span>{t(lang, '总计', 'Total')} <strong>{recent_count}</strong></span><span class='good'>{t(lang, '完成', 'Done')} <strong>{today_counts.get('success', 0)}</strong></span><span class='warn'>{t(lang, '关注', 'Needs review')} <strong>{today_attention_count}</strong></span></div>
      <a class='outline today-job-link' href='{today_href}'>{t(lang, '查看今日运行情况', 'View today’s runs')} →</a></div>
    </section>
    """

    def risk_card(market: str) -> str:
        snapshot = load_latest_workspace_snapshot(db, market_risk_snapshot_type(market)) or {}
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        gate = str(payload.get("buy_gate") or "UNKNOWN").upper()
        gate_label = {
            "BLOCK": "暂停买入" if lang == "zh" else "Block buys",
            "REVIEW": "谨慎复核" if lang == "zh" else "Review",
            "ALLOW": "允许" if lang == "zh" else "Allow",
            "UNKNOWN": "未生成" if lang == "zh" else "Missing",
        }.get(gate, gate)
        tone = "bad" if gate == "BLOCK" else "warn" if gate == "REVIEW" else "good" if gate == "ALLOW" else "idle"
        latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else {}
        flags = [str(item) for item in (payload.get("flags") or []) if str(item).strip()]
        flag_html = "".join(f"<span class='mini-flag'>{html.escape(item)}</span>" for item in flags[:4]) or "<span class='mini-flag'>-</span>"
        return f"""
        <div class='risk-card {tone}'>
          <div class='risk-top'><span>{html.escape(market)}</span><strong>{html.escape(gate_label)}</strong></div>
          <div class='risk-headline'>{html.escape(str(payload.get('headline') or (t(lang, '尚无风险快照', 'No risk snapshot yet'))))}</div>
          <div class='risk-metrics'>
            <span>{t(lang, '模式', 'Regime')}: {html.escape(str(payload.get('risk_regime') or '-'))}</span>
            <span>{t(lang, '风险', 'Risk')}: {html.escape(str(payload.get('risk_level') or '-'))}</span>
            <span>{t(lang, '上涨家数', 'Breadth')}: {html.escape(str(latest.get('up_pct') or '-'))}%</span>
            <span>{t(lang, '日期', 'Date')}: {html.escape(str(payload.get('snapshot_date') or '-'))}</span>
          </div>
          <div class='risk-flags'>{flag_html}</div>
        </div>
        """

    portfolio_risk_snapshot = load_latest_workspace_snapshot(db, PORTFOLIO_RISK_ALERT_SNAPSHOT_TYPE) or {}
    portfolio_risk_payload = portfolio_risk_snapshot.get("payload") if isinstance(portfolio_risk_snapshot, dict) else {}
    portfolio_risk_payload = portfolio_risk_payload if isinstance(portfolio_risk_payload, dict) else {}
    portfolio_top_rows = portfolio_risk_payload.get("top_risks") or []
    portfolio_top_html = "".join(
        f"<span class='mini-flag'>{html.escape(str(row.get('ticker') or '-'))} · {html.escape(str(row.get('action_label') or '-'))}</span>"
        for row in portfolio_top_rows[:5]
        if isinstance(row, dict)
    ) or "<span class='mini-flag'>-</span>"
    risk_overview_html = f"""
    <section class='risk-overview'>
      <div class='section-head compact'><div><div class='eyebrow'>{t(lang, '风险闸门', 'Risk Guardrail')}</div><h2>{t(lang, '先判断环境，再看模型候选', 'Regime first, candidates second')}</h2></div>
      <form action='/jobs/risk-guardrail-snapshot' method='post'><input type='hidden' name='redirect_to' value='{html.escape(redirect_to, quote=True)}' /><input type='hidden' name='markets' value='CN,US' /><input type='hidden' name='lookback_days' value='12' /><button type='submit'>{t(lang, '刷新风险预警', 'Refresh guardrail')}</button></form></div>
      <div class='risk-grid'>{risk_card('CN')}{risk_card('US')}
        <div class='risk-card portfolio'>
          <div class='risk-top'><span>{t(lang, '持仓', 'Portfolio')}</span><strong>{int(portfolio_risk_payload.get('risk_count') or 0)} / {int(portfolio_risk_payload.get('position_count') or 0)}</strong></div>
          <div class='risk-headline'>{html.escape(str(portfolio_risk_payload.get('headline') or (t(lang, '尚无持仓风险快照', 'No portfolio risk snapshot yet'))))}</div>
          <div class='risk-metrics'>
            <span>{t(lang, '高风险', 'High risk')}: {int(portfolio_risk_payload.get('high_risk_count') or 0)}</span>
            <span>{t(lang, '日期', 'Date')}: {html.escape(str(portfolio_risk_payload.get('snapshot_date') or '-'))}</span>
            <span>{t(lang, '生成', 'Created')}: {html.escape(_display_time(str(portfolio_risk_snapshot.get('created_at') or '')) if portfolio_risk_snapshot else '-')}</span>
          </div>
          <div class='risk-flags'>{portfolio_top_html}</div>
        </div>
      </div>
    </section>
    """
    return render_dashboard_legacy_page(
        "dashboard/legacy/ops__render_task_center_redesign.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '任务中心', 'Task Center')}",
            f'{RISK_CARD_STYLE}',
            f"{t(lang, '任务中心', 'Task Center')}",
            f"{t(lang, '每日运行、维护和历史记录按用途分开。', 'Daily runs, maintenance, and history are separated by purpose.')}",
            f'{nav_html}',
            f"{t(lang, '今日运行台', 'Today’s runbook')}",
            f'{today}',
            f"{('今日已记录 ' + str(recent_count) + ' 个任务。先完成每日链路，再处理维护任务。' if lang == 'zh' else str(recent_count) + ' jobs recorded today. Complete the daily chain before maintenance.')}",
            f'{today_href}',
            f"{t(lang, '今天所有 Job', 'All jobs today')}",
            f'{history_href}',
            f"{t(lang, '查看历史任务', 'View job history')}",
            f'{lookback_runs}',
            f'{lookback_runs}',
            f'{banner_html}',
            f'{today_module_html}',
            f'{risk_overview_html}',
            f"{t(lang, '每日必跑', 'Daily required')}",
            f"{t(lang, '从行情到日报的必要链路', 'Required path from prices to report')}",
            f"{t(lang, '每行可手动触发，并保留本次运行明细。', 'Run each step manually and retain its execution details.')}",
            f'{daily_sections_html}',
            f"{t(lang, '数据维护', 'Maintenance')}",
            f"{t(lang, '非每日，但需要定期处理', 'Not daily, but worth maintaining')}",
            f"{t(lang, '维护任务不参与每日链路，不干扰当天判断。', 'Maintenance stays separate from the daily decision path.')}",
            f"{t(lang, '任务', 'Job')}",
            f"{t(lang, '开始', 'Started')}",
            f"{t(lang, '结束', 'Finished')}",
            f"{t(lang, '持续时间', 'Duration')}",
            f"{t(lang, '状态', 'Status')}",
            f"{t(lang, '操作', 'Actions')}",
            f'{maintenance_rows}',
        ],
    )



@router.get("/ops", response_class=HTMLResponse)
def dashboard_ops_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops")
    lang = resolve_request_lang(request)
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    return _render_task_center_redesign(
        request=request,
        lang=lang,
        lookback_runs=lookback_runs,
        db=db,
        summary={},
    )



@router.get("/ops/sync", response_class=HTMLResponse)
def dashboard_ops_sync_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/sync")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    tushare_ready = bool(get_settings().tushare_token)
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    sync_states = summary["sync_states"]
    recent_jobs = summary["recent_jobs"]
    dashboard_redirect = "/dashboard/ops/sync?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    latest_cn_refresh = _latest_cn_refresh_summary(db, recent_jobs, lang=lang)
    cn_universe_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_symbol_universe"), None)
    cn_init_job = next((item for item in recent_jobs if item["job_type"] == "init_cn_market_data"), None)
    cn_fundamental_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_fundamentals"), None)
    cn_concept_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_concepts"), None)
    def _load_cn_sync_stats() -> dict:
        symbol_repo = SymbolRepository(db)
        fundamental_repo = FundamentalSnapshotRepository(db)
        concept_repo = ConceptSnapshotRepository(db)
        technical_snapshot_repo = TechnicalSnapshotRepository(db)
        point_in_time_repo = PointInTimeFeatureSnapshotRepository(db)
        forward_shadow = WorkspaceSnapshotRepository(db).get_latest_snapshot(
            CN_FORWARD_SHADOW_SNAPSHOT_TYPE
        )
        cn_symbols = [symbol for symbol in symbol_repo.list_symbols() if (symbol.market or "").upper() == "CN"]
        cn_ticker_set = {symbol.ticker for symbol in cn_symbols}
        cn_symbol_count = len(cn_symbols)
        cn_sync_success_count = sum(
            1 for item in sync_states if item["ticker"] in cn_ticker_set and item["status"] == "success"
        )
        cn_fundamentals = fundamental_repo.list_latest_for_market("CN")
        concept_summary = concept_repo.get_latest_summary()
        cn_technical_snapshot_count = len(technical_snapshot_repo.list_latest_for_market("CN"))
        cn_progress_pct = round((cn_sync_success_count / cn_symbol_count) * 100, 1) if cn_symbol_count else 0.0
        point_in_time_coverage = point_in_time_repo.summarize_market_coverage(
            "CN",
            required_features=FUNDAMENTAL_FEATURE_NAMES,
        )
        hithink_core_coverage = point_in_time_repo.summarize_market_coverage(
            "CN",
            required_features=("pe_ttm", "net_profit_yoy", "revenue_yoy", "debt_to_assets"),
        )
        latest_cn_trade_date = get_latest_lake_trade_date(market="CN")
        point_in_time_as_of_coverage = {}
        if latest_cn_trade_date:
            point_in_time_as_of_coverage = point_in_time_repo.summarize_market_coverage_as_of(
                "CN",
                cutoff=datetime.combine(
                    date.fromisoformat(latest_cn_trade_date),
                    time(hour=16),
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
                required_features=FUNDAMENTAL_FEATURE_NAMES,
                max_age_days=dict(DEFAULT_MAX_AGE_DAYS),
            )
        next_cn_offset = cn_sync_success_count
        default_cn_batch_size = min(500, max(100, cn_symbol_count - cn_sync_success_count)) if cn_symbol_count > cn_sync_success_count else 0
        return {
            "cn_symbol_count": cn_symbol_count,
            "cn_sync_success_count": cn_sync_success_count,
            "cn_technical_snapshot_count": cn_technical_snapshot_count,
            "cn_fundamental_snapshot_count": len(cn_fundamentals),
            "cn_concept_symbol_count": int(concept_summary.get("symbol_count") or 0),
            "cn_concept_latest_as_of_date": concept_summary.get("latest_as_of_date"),
            "cn_progress_pct": cn_progress_pct,
            "next_cn_offset": next_cn_offset,
            "default_cn_batch_size": default_cn_batch_size,
            "point_in_time_coverage": point_in_time_coverage,
            "hithink_core_coverage": hithink_core_coverage,
            "point_in_time_as_of_coverage": point_in_time_as_of_coverage,
            "point_in_time_as_of_trade_date": latest_cn_trade_date,
            "forward_shadow": forward_shadow,
        }

    cn_stats = get_or_set(
        "dashboard_ops_cn_sync_stats",
        json.dumps({"sync_rows": len(sync_states)}, sort_keys=True),
        ttl_seconds=60.0,
        loader=_load_cn_sync_stats,
    )
    cn_symbol_count = int(cn_stats.get("cn_symbol_count") or 0)
    cn_sync_success_count = int(cn_stats.get("cn_sync_success_count") or 0)
    cn_technical_snapshot_count = int(cn_stats.get("cn_technical_snapshot_count") or 0)
    cn_fundamental_snapshot_count = int(cn_stats.get("cn_fundamental_snapshot_count") or 0)
    cn_concept_symbol_count = int(cn_stats.get("cn_concept_symbol_count") or 0)
    cn_concept_latest_as_of_date = str(cn_stats.get("cn_concept_latest_as_of_date") or "").strip() or "-"
    cn_progress_pct = float(cn_stats.get("cn_progress_pct") or 0.0)
    next_cn_offset = int(cn_stats.get("next_cn_offset") or 0)
    default_cn_batch_size = int(cn_stats.get("default_cn_batch_size") or 0)
    point_in_time_coverage = cn_stats.get("point_in_time_coverage") or {}
    hithink_core_coverage = cn_stats.get("hithink_core_coverage") or {}
    point_in_time_as_of_coverage = cn_stats.get("point_in_time_as_of_coverage") or {}
    point_in_time_as_of_trade_date = str(
        cn_stats.get("point_in_time_as_of_trade_date") or ""
    ).strip() or "-"
    forward_shadow = cn_stats.get("forward_shadow") or {}
    forward_shadow_payload = forward_shadow.get("payload") or {}
    forward_shadow_top = ", ".join(
        str(item.get("ticker") or "-")
        for item in (forward_shadow_payload.get("top_observations") or [])[:5]
    ) or "-"
    point_in_time_feature_rows = "".join(
        "<div class='muted' style='display:flex;justify-content:space-between;gap:12px;'>"
        f"<span>{html.escape(str(item.get('feature_name') or '-'))}</span>"
        f"<strong>{int(item.get('symbol_count') or 0)}/{int(point_in_time_coverage.get('total_symbols') or 0)} "
        f"({float(item.get('coverage_pct') or 0.0):.2f}%)</strong>"
        "</div>"
        for item in point_in_time_coverage.get("feature_coverage") or []
    )
    point_in_time_as_of_rows = "".join(
        "<div class='muted' style='display:flex;justify-content:space-between;gap:12px;'>"
        f"<span>{html.escape(str(item.get('feature_name') or '-'))}</span>"
        f"<strong>{int(item.get('symbol_count') or 0)}/{int(point_in_time_as_of_coverage.get('total_symbols') or 0)} "
        f"({float(item.get('coverage_pct') or 0.0):.2f}%)</strong>"
        "</div>"
        for item in point_in_time_as_of_coverage.get("feature_coverage") or []
    )
    cn_fundamental_result = (cn_fundamental_job or {}).get("result") or {}
    cn_fundamental_resume_offset = (
        int(cn_fundamental_result.get("next_offset") or 0)
        if cn_fundamental_result and not cn_fundamental_result.get("complete")
        else 0
    )
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    visible_sync_states = sync_states[:200]
    sync_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td>{item['provider']}</td><td>{item['last_synced_date'] or '-'}</td><td>{item['status'] or '-'}</td></tr>"
        for item in visible_sync_states
    ) or f"<tr><td colspan='4'>{t(lang, '暂无同步记录', 'No sync history yet')}</td></tr>"
    sync_state_note = (
        f"仅展示最近 {len(visible_sync_states)} 条，同步总数 {len(sync_states)}。"
        if lang == "zh"
        else f"Showing the latest {len(visible_sync_states)} rows out of {len(sync_states)} sync states."
    )
    return render_dashboard_legacy_page(
        "dashboard/legacy/ops_dashboard_ops_sync_page.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '同步中心', 'Sync Center')}",
            f'{LOAD_CN_SYNC_STATS_STYLE}',
            f"{t(lang, '任务中心', 'Ops Center')}",
            f"{t(lang, '同步、训练、回测和自动任务都从这里收口。', 'Sync, training, backtests, and automation all flow through this workspace.')}",
            f'{nav_html}',
            f"{t(lang, '同步页负责把市场数据、A 股全市场初始化和技术快照入口集中起来。', 'The sync page centralizes market data, CN universe initialization, and technical snapshots.')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '返回运维操作台', 'Back to Operations')}",
            f'{lookback_runs}',
            f'{lookback_runs}',
            f"{t(lang, '同步中心', 'Sync Center')}",
            f"{t(lang, '行情与基本面同步', 'Market and Fundamental Sync')}",
            f"{t(lang, '专门处理市场数据、概念和基本面同步。', 'A focused page for market, concept, and fundamental sync workflows.')}",
            f"""{("<section class='card' style='border-color:#f59e0b;background:#fff8eb;'>" + f"<div class='eyebrow'>{t(lang, '需要配置 TuShare', 'TuShare Required')}</div>" + (f"<p class='muted'>{t(lang, 'A 股全市场股票池、基本面和概念同步需要先配置 ', 'CN full-market universe, fundamentals, and concept sync require ')}" + '<code>PQW_TUSHARE_TOKEN</code>' + ('。当前未检测到 token，所以这几项 job 会返回未配置。' if lang == 'zh' else '. No token is currently configured, so these jobs will return not configured.') + '</p>' + (f"<p class='muted'>{t(lang, '即使已配置 token，A 股概念同步还需要 TuShare 账号具备 ', 'Even with a token configured, CN concept sync also requires TuShare access to ')}" + '<code>concept_detail</code>' + (' 接口权限；否则系统会优雅降级为未配置，而不是把收盘链路打成失败。' if lang == 'zh' else ' permission; otherwise the system degrades gracefully to not_configured instead of failing the post-close pipeline.') + '</p>')) + '</section>' if not tushare_ready else '')}""",
            f"""{("<section class='card'>" + f"<div class='eyebrow'>{t(lang, '最近股票池同步', 'Latest CN Universe Sync')}</div>" + f"<p class='muted'>{cn_universe_job['message'] or t(lang, '暂无记录', 'No recent run yet')}</p>" + '</section>' if cn_universe_job else '')}""",
            f"{t(lang, 'A股全市场初始化进度', 'CN Market Init Progress')}",
            f"{t(lang, '股票池总数', 'Universe')}",
            f'{cn_symbol_count}',
            f"{t(lang, '已同步行情', 'Price Synced')}",
            f'{cn_sync_success_count}',
            f"{t(lang, '技术缓存', 'Technical Snapshots')}",
            f'{cn_technical_snapshot_count}',
            f"{t(lang, '基本面快照', 'Fundamental Snapshots')}",
            f'{cn_fundamental_snapshot_count}',
            f"{t(lang, '概念覆盖股票', 'Concept-covered Symbols')}",
            f'{cn_concept_symbol_count}',
            f"{('截至 ' + cn_concept_latest_as_of_date if lang == 'zh' else 'As of ' + cn_concept_latest_as_of_date)}",
            f"{t(lang, '最近全市场轻刷新', 'Latest Light Refresh')}",
            f"{latest_cn_refresh.get('summary') or '-'}",
            f"{t(lang, '最近初始化任务', 'Latest Init Job')}",
            f"{(cn_init_job or {}).get('status', 'idle')}",
            f'{cn_progress_pct}',
            f'{cn_progress_pct}',
            f'{cn_sync_success_count}',
            f'{cn_symbol_count}',
            f"{latest_cn_refresh.get('label') or ''}",
            f'{lang}',
            f"{t(lang, '去全市场技术选股', 'Open Full-Market Technical Screener')}",
            f"{t(lang, 'A股增强原料状态', 'CN Enrichment Inputs')}",
            f"{t(lang, '如果这里还是 0，当前 LightGBM 本质上仍是价格量能版。先把基本面和概念原料补齐，再看增强模型效果。', 'If these stay at 0, current LightGBM is still effectively price-and-volume only. Fill the fundamental and concept inputs first before judging the enriched model.')}",
            f"{t(lang, '基本面快照', 'Fundamental Snapshots')}",
            f'{cn_fundamental_snapshot_count}',
            f"{(cn_fundamental_job or {}).get('message') or t(lang, '还没有最近执行记录。', 'No recent run recorded yet.')}",
            f"{t(lang, '七项齐全股票', 'Symbols with all seven features')}",
            f"{int(point_in_time_coverage.get('ready_symbol_count') or 0)}",
            f"{int(point_in_time_coverage.get('total_symbols') or 0)}",
            f"{float(point_in_time_coverage.get('ready_symbol_pct') or 0.0):.2f}",
            f"{t(lang, '同花顺原生核心四项齐全', 'Complete HiThink-native core four')}",
            f"{int(hithink_core_coverage.get('ready_symbol_count') or 0)}",
            f"{int(hithink_core_coverage.get('total_symbols') or 0)}",
            f"{float(hithink_core_coverage.get('ready_symbol_pct') or 0.0):.2f}",
            f"{t(lang, '横截面采集门禁', 'Cross-section collection gate')}",
            f"{point_in_time_coverage.get('cross_section_gate') or 'COLLECTING'}",
            f"{t(lang, '正式日期覆盖仍需独立审计', 'formal date coverage still requires the audit job')}",
            f'{point_in_time_feature_rows}',
            f"{t(lang, '最近收盘严格可用门禁', 'Latest-close strict as-of gate')}",
            f'{point_in_time_as_of_trade_date}',
            f"{point_in_time_as_of_coverage.get('as_of_gate') or 'COLLECTING'}",
            f"{t(lang, '七项在该截点全部可用', 'All seven usable at that cutoff')}",
            f"{int(point_in_time_as_of_coverage.get('ready_symbol_count') or 0)}",
            f"{int(point_in_time_as_of_coverage.get('total_symbols') or 0)}",
            f"{float(point_in_time_as_of_coverage.get('ready_symbol_pct') or 0.0):.2f}",
            f'{point_in_time_as_of_rows}',
            f"{t(lang, '概念覆盖', 'Concept Coverage')}",
            f'{cn_concept_symbol_count}',
            f"{(('最近日期 ' + cn_concept_latest_as_of_date if lang == 'zh' else 'Latest as-of ' + cn_concept_latest_as_of_date) if cn_concept_latest_as_of_date != '-' else (cn_concept_job or {}).get('message') or t(lang, '还没有最近执行记录。', 'No recent run recorded yet.'))}",
            f"{t(lang, 'A股前瞻 Shadow', 'CN Forward Shadow')}",
            f"{int(forward_shadow_payload.get('confirmation_date_count') or 0)}",
            f"{int(forward_shadow_payload.get('minimum_confirmation_dates') or 60)}",
            f"{t(lang, '生效交易日', 'Effective session')}",
            f"{forward_shadow_payload.get('effective_trade_date') or '-'}",
            f"{t(lang, '点时门禁', 'As-of gate')}",
            f"{forward_shadow_payload.get('as_of_gate') or 'COLLECTING'}",
            f"{t(lang, '当前决策', 'Current decision')}",
            f"{forward_shadow_payload.get('shadow_decision') or 'ABSTAIN'}",
            f"{t(lang, '仅观察，不进入生产推荐', 'observation only; excluded from production recommendations')}",
            f"{t(lang, '观察排名前五', 'Top five observations')}",
            f'{html.escape(forward_shadow_top)}',
            f"{_dt(lang, 'sync_market_data')}",
            f'{dashboard_redirect}',
            f'{lang}',
            f"{_dt(lang, 'sync_market_data')}",
            f"{t(lang, '美股收盘批量刷新', 'US Grouped Daily Refresh')}",
            f'{dashboard_redirect}',
            f"{t(lang, '通过 Polygon grouped daily 刷新美股全市场 EOD，写入 Parquet market lake。未配置 PQW_POLYGON_API_KEY 时会返回 not_configured。', 'Refresh U.S. full-market EOD via Polygon grouped daily into the Parquet market lake. Returns not_configured until PQW_POLYGON_API_KEY is set.')}",
            f"{t(lang, '留空自动取最近美股交易日', 'blank for latest US trading day')}",
            f"{('调试限制，0 代表全部' if lang == 'zh' else 'Debug limit, 0 for all')}",
            f"{t(lang, '写入 Parquet Market Lake（推荐）', 'Write Parquet Market Lake (recommended)')}",
            f"{t(lang, '刷新美股收盘行情', 'Refresh US EOD')}",
            f"{t(lang, '预计算美股模型', 'Precompute US Screeners')}",
            f'{dashboard_redirect}',
            f"{t(lang, '先基于本地已有美股池生成模型候选快照；接入 Polygon 全市场后会自动扩大覆盖。', 'Precompute model snapshots from the local U.S. symbol pool; coverage expands after Polygon full-market refresh.')}",
            f"{t(lang, '预计算美股候选', 'Precompute US Candidates')}",
            f"{t(lang, '训练美股模型', 'Train US Signals')}",
            f'{dashboard_redirect}',
            f"{t(lang, '从本地美股 Parquet market lake 读取股票池，正式写入 predictions / prediction_details。这样美股不再只是预计算候选，而是进入统一模型结果链路。', 'Read the U.S. symbol pool from the local U.S. Parquet market lake and write into predictions / prediction_details so U.S. names join the unified model-result pipeline.')}",
            f"{t(lang, '训练美股信号', 'Train US Signals')}",
            f"{t(lang, '同步 A 股股票池', 'Sync CN Market Universe')}",
            f'{dashboard_redirect}',
            f"{t(lang, '从 TuShare 主列表同步 A 股全市场股票池到本地 symbols。', 'Sync the full A-share stock universe from TuShare into local symbols.')}",
            f"{t(lang, '同步 A 股股票池', 'Sync CN Market Universe')}",
            f"{t(lang, '同花顺全市场增量', 'HiThink Full-Market Increment')}",
            f'{dashboard_redirect}',
            f"{t(lang, '最近 10 个交易日（每日推荐）', 'Latest 10 sessions (daily)')}",
            f"{t(lang, '最近约 10 年（首次初始化/修复）', 'Approximately 10 years (initial/repair)')}",
            f"{t(lang, '通过同花顺官方 Parquet 一次导入全市场，按股票和交易日去重后合并到现有 CN 数据湖。', 'Import the official full-market Parquet and merge it into the existing CN lake by symbol and trade date.')}",
            f"{t(lang, '导入同花顺行情', 'Import HiThink Market Data')}",
            f"{t(lang, '初始化 A 股全市场数据', 'Init CN Market Data')}",
            f'{dashboard_redirect}',
            f"{('回看天数' if lang == 'zh' else 'Days Back')}",
            f'{next_cn_offset}',
            f"{('起始偏移（默认接着当前进度）' if lang == 'zh' else 'Offset (resume from current progress)')}",
            f'{default_cn_batch_size}',
            f"{('本批数量（0 代表直到结束）' if lang == 'zh' else 'Batch Size (0 for remaining)')}",
            f"{('兼容限制（可留 0）' if lang == 'zh' else 'Compatibility limit (optional)')}",
            f"{t(lang, '初始化 A 股全市场数据', 'Init CN Market Data')}",
            f"{t(lang, '刷新 A 股最近行情', 'Refresh Recent CN Market Data')}",
            f'{dashboard_redirect}',
            f"{('刷新最近天数' if lang == 'zh' else 'Refresh Recent Days')}",
            f"{('股票数量限制（0 代表全部）' if lang == 'zh' else 'Limit (0 for all)')}",
            f"{t(lang, '刷新 A 股最近行情', 'Refresh Recent CN Market Data')}",
            f"{t(lang, '重建技术形态缓存', 'Rebuild Technical Snapshots')}",
            f'{dashboard_redirect}',
            f"{('股票数量限制（0 代表全部）' if lang == 'zh' else 'Limit (0 for all)')}",
            f"{t(lang, '重建技术形态缓存', 'Rebuild Technical Snapshots')}",
            f"{_dt(lang, 'sync_cn_fundamentals')}",
            f"{(cn_fundamental_job or {}).get('message') or t(lang, '当前库里还没有 A 股基本面快照，建议先跑一次。', 'No CN fundamental snapshots are in the database yet. Run this once first.')}",
            f'{dashboard_redirect}',
            f'{cn_fundamental_resume_offset}',
            f"{t(lang, '续跑偏移', 'Resume offset')}",
            f"{t(lang, '每批股票数', 'Tickers per batch')}",
            f"{t(lang, '最多批次，0 跑完', 'Max batches, 0 for all')}",
            f"{t(lang, '股票留空时从行情湖全市场分批续跑；同花顺建议每批 20 只、每次 1 批，完成后按自动游标继续。任务在后台执行，可在任务中心查看游标和失败批次。', 'Leave tickers blank to resume the full lake universe. For HiThink, use 20 tickers and one batch per run, then continue from the automatic cursor. Progress and failures remain visible in Jobs.')}",
            f"{t(lang, '分批回填点时基本面', 'Backfill Point-in-Time Fundamentals')}",
            f"{_dt(lang, 'sync_cn_concepts')}",
            f"{(cn_concept_job or {}).get('message') or t(lang, '当前库里还没有 A 股概念快照，建议在 TuShare 权限就绪后再跑。', 'No CN concept snapshots are in the database yet. Run this after TuShare concept permissions are ready.')}",
            f'{dashboard_redirect}',
            f"{_dt(lang, 'sync_cn_concepts')}",
            f"{_dt(lang, 'sync_us_hk_fundamentals')}",
            f'{dashboard_redirect}',
            f"{_dt(lang, 'sync_us_hk_fundamentals')}",
            f"{_dt(lang, 'sync_states')}",
            f'{sync_state_note}',
            f"{_dt(lang, 'ticker')}",
            f"{_dt(lang, 'provider')}",
            f"{_dt(lang, 'last_sync')}",
            f"{_dt(lang, 'status')}",
            f'{sync_rows}',
        ],
    )



@router.get("/ops/models", response_class=HTMLResponse)
def dashboard_ops_models_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/models")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_summary(db, lookback_runs=lookback_runs)
    recent_model_runs = summary["recent_model_runs"]
    latest_backtest = summary["latest_backtest"]
    latest_backtest_summary = (latest_backtest or {}).get("summary") or {}
    tradability_summary = latest_backtest_summary.get("tradability_summary") or {}
    capacity_summary = latest_backtest_summary.get("capacity_summary") or {}
    attribution_summary = latest_backtest_summary.get("attribution_summary") or {}
    portfolio_construction_summary = latest_backtest_summary.get("portfolio_construction_summary") or {}
    dashboard_redirect = "/dashboard/ops/models?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    model_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td><td title='{item['name']}'>{_compact_run_name(item['name'], 28)}</td><td>{item['status']}</td><td title='{item['config_json'] or '-'}'><code>{_compact_json_summary(item['config_json'], 64)}</code></td><td>{_display_time(item['created_at'])}</td>"
        f"<td><form action='/jobs/backtest' method='post' style='margin:0;'><input type='hidden' name='redirect_to' value='{dashboard_redirect}' /><input type='hidden' name='engine_version' value='event_driven_daily_v2' /><input type='hidden' name='top_n' value='1' /><input type='hidden' name='model_run_id' value='{item['id']}' /><button type='submit' style='padding:8px 10px;font-size:12px;'>{_dt(lang, 'backtest_this_run')}</button></form></td>"
        "</tr>"
        for item in recent_model_runs
    ) or f"<tr><td colspan='6'>{t(lang, '暂无模型运行', 'No model runs yet')}</td></tr>"
    backtest_pre = json.dumps(latest_backtest, indent=2) if latest_backtest else (t(lang, "暂无回测", "No backtest yet"))
    backtest_detail_link = (
        f"<a class='pill' href='/backtests/{int(latest_backtest['id'])}/view?lang={lang}'>"
        f"{t(lang, '查看执行审计', 'View Execution Audit')}</a>"
        if latest_backtest and latest_backtest.get("id")
        else ""
    )
    tradeability_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            ((t(lang, "候选数", "Candidates")), int(tradability_summary.get("total_candidates") or 0)),
            ((t(lang, "可成交", "Eligible")), int(tradability_summary.get("eligible_candidates") or 0)),
            ((t(lang, "最终入选", "Selected")), int(tradability_summary.get("selected_candidates") or 0)),
            ((t(lang, "阻塞数", "Blocked")), int(tradability_summary.get("blocked_candidates") or 0)),
            ((t(lang, "通过率", "Pass Rate")), f"{float(tradability_summary.get('pass_rate') or 0.0) * 100:.1f}%"),
            ((t(lang, "选中率", "Selection Rate")), f"{float(tradability_summary.get('selection_rate') or 0.0) * 100:.1f}%"),
        )
    ) or f"<div class='muted'>{t(lang, '暂无可成交摘要', 'No tradability summary yet')}</div>"
    top_block_reasons = tradability_summary.get("top_block_reasons") or []
    top_block_html = "".join(
        f"<div class='tag'>{html.escape(format_trade_gate_reason(reason, lang=lang))}: {count}</div>"
        for reason, count in top_block_reasons[:5]
    ) or f"<div class='muted'>{t(lang, '暂无阻塞原因', 'No block reasons yet')}</div>"
    capacity_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            ((t(lang, "最小 ADV", "Min ADV")), f"{float(capacity_summary.get('min_adv') or 0.0):,.0f}"),
            ((t(lang, "最大跳空", "Max Gap")), f"{float(capacity_summary.get('max_gap_pct') or 0.0) * 100:.1f}%"),
            ((t(lang, "单票上限", "Max Position")), f"{float(capacity_summary.get('max_position_weight') or 0.0) * 100:.1f}%"),
            ((t(lang, "行业上限", "Max Sector")), f"{float(capacity_summary.get('max_sector_weight') or 0.0) * 100:.1f}%"),
            ((t(lang, "平均持股数", "Avg Names")), f"{float(capacity_summary.get('avg_selected_names') or 0.0):.1f}"),
            ((t(lang, "预估总暴露", "Estimated Gross")), f"{float(capacity_summary.get('estimated_gross_exposure') or 0.0) * 100:.1f}%"),
        )
    ) or f"<div class='muted'>{t(lang, '暂无容量摘要', 'No capacity summary yet')}</div>"
    attribution_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            ((t(lang, "组合总收益", "Portfolio Return")), f"{float(attribution_summary.get('portfolio_total_return') or 0.0) * 100:.2f}%"),
            ((t(lang, "基准收益", "Benchmark Return")), f"{float(attribution_summary.get('benchmark_total_return') or 0.0) * 100:.2f}%"),
            ((t(lang, "超额收益", "Excess Return")), f"{float(attribution_summary.get('excess_total_return') or 0.0) * 100:.2f}%"),
            ((t(lang, "日均 Alpha", "Avg Daily Alpha")), f"{float(attribution_summary.get('avg_daily_alpha') or 0.0) * 100:.3f}%"),
            ((t(lang, "成本拖累", "Cost Drag")), f"{float(attribution_summary.get('cost_drag_bps') or 0.0):.1f} bps"),
        )
    ) or f"<div class='muted'>{t(lang, '暂无归因摘要', 'No attribution summary yet')}</div>"
    construction_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            ((t(lang, "权重规则", "Weighting")), portfolio_construction_summary.get("weighting_rule") or "-"),
            ((t(lang, "持仓规则", "Continuity")), portfolio_construction_summary.get("continuity_rule") or "-"),
            ((t(lang, "Top N", "Top N")), portfolio_construction_summary.get("top_n") or 0),
            ((t(lang, "持有天数", "Holding Days")), portfolio_construction_summary.get("holding_days") or 0),
            ((t(lang, "调仓阈值", "Rebalance Threshold")), f"{float(portfolio_construction_summary.get('rebalance_threshold') or 0.0) * 100:.1f}%"),
            ((t(lang, "平均持股数", "Avg Names")), f"{float(portfolio_construction_summary.get('avg_selected_names') or 0.0):.1f}"),
        )
    ) or f"<div class='muted'>{t(lang, '暂无组合构建摘要', 'No construction summary yet')}</div>"
    return render_dashboard_legacy_page(
        "dashboard/legacy/ops_dashboard_ops_models_page.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '模型运行', 'Model Runs')}",
            f'{DASHBOARD_OPS_MODELS_PAGE_STYLE}',
            f"{t(lang, '模型与回测', 'Models')}",
            f"{t(lang, '把训练、回测、可成交性和组合构建放在同一屏里审视。', 'Review training, backtests, tradability, and construction in one screen.')}",
            f'{nav_html}',
            f"{t(lang, '这个页面更像模型审查台，不只是看曲线，也看容量、阻塞和收益归因。', 'This is a model review desk, not just a curve page.')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '返回运维操作台', 'Back to Operations')}",
            f'{lookback_runs}',
            f'{lookback_runs}',
            f"{t(lang, '模型运行', 'Model Runs')}",
            f"{t(lang, '训练与回测视图', 'Training and Backtest View')}",
            f"{t(lang, '专门查看最近模型运行并从这里回测。', 'Review recent model runs and trigger backtests from here.')}",
            f"{_dt(lang, 'run_training')}",
            f'{dashboard_redirect}',
            f"{_dt(lang, 'run_name')}",
            f"{_dt(lang, 'run_training')}",
            f"{_dt(lang, 'recent_model_runs')}",
            f"{_dt(lang, 'name')}",
            f"{_dt(lang, 'status')}",
            f"{_dt(lang, 'config')}",
            f"{_dt(lang, 'created')}",
            f"{_dt(lang, 'action')}",
            f'{model_rows}',
            f"{('Tradability' if lang == 'en' else '可成交性')}",
            f"{t(lang, '这次回测到底有多少候选能通过交易门槛。', 'How many candidates actually passed the tradeability gates.')}",
            f'{tradeability_rows}',
            f'{top_block_html}',
            f"{('Capacity' if lang == 'en' else '容量')}",
            f"{capacity_summary.get('capacity_comment') or t(lang, '回测容量与流动性摘要。', 'Capacity and liquidity summary for the backtest.')}",
            f'{capacity_rows}',
            f"{('Attribution' if lang == 'en' else '归因')}",
            f"{attribution_summary.get('alpha_source_hint') or t(lang, '先看收益来自超额，还是主要来自执行假设。', 'Check whether returns come from alpha or from execution assumptions.')}",
            f'{attribution_rows}',
            f"{('Construction' if lang == 'en' else '组合构建')}",
            f"{t(lang, '把权重、换手和延续规则一起看，才能知道这条曲线是不是可实现。', 'Review weighting, turnover, and continuity rules together to judge whether the curve is implementable.')}",
            f'{construction_rows}',
            f"{_dt(lang, 'backtest_summary')}",
            f'{backtest_detail_link}',
            f'{backtest_pre}',
        ],
    )



@router.get("/ops/today", response_class=HTMLResponse)
def dashboard_ops_today_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    """Pinned, operational report for every job started during the app's current day."""

    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/today")
    lang = resolve_request_lang(request)
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    today = app_today_iso()
    jobs = _jobs_started_on_date(db, today)
    counts = _today_job_status_counts(jobs)
    failed_count = counts.get("failed", 0) + counts.get("failed_timeout", 0)
    attention_statuses = {"failed", "failed_timeout", "partial", "running", "not_configured"}
    attention_jobs = [job for job in jobs if str(job.get("status") or "idle").lower() in attention_statuses]

    if failed_count:
        conclusion = (
            f"今天有 {failed_count} 个任务失败，请优先打开失败任务查看回执与参数。"
            if lang == "zh"
            else f"{failed_count} jobs failed today. Open the failed runs first for their receipts and parameters."
        )
        conclusion_tone = "bad"
    elif counts.get("running", 0):
        conclusion = (
            f"有 {counts['running']} 个任务仍在运行，完成后本页会自动反映最新状态。"
            if lang == "zh"
            else f"{counts['running']} jobs are still running. This page will reflect the final state when they complete."
        )
        conclusion_tone = "warn"
    elif counts.get("partial", 0) or counts.get("not_configured", 0):
        needs_review = counts.get("partial", 0) + counts.get("not_configured", 0)
        conclusion = (
            f"主任务已结束，但有 {needs_review} 项需要复核（部分完成或未配置）。"
            if lang == "zh"
            else f"Runs have finished, but {needs_review} items need review (partial or not configured)."
        )
        conclusion_tone = "warn"
    elif jobs:
        conclusion = "今日任务均已完成，未发现失败或需要复核的任务。" if lang == "zh" else "All of today’s jobs completed without failures or review items."
        conclusion_tone = "good"
    else:
        conclusion = "今天尚无任务记录。" if lang == "zh" else "No jobs have been recorded today."
        conclusion_tone = "idle"

    critical_job_types = [
        ("refresh_cn_market_data_lake_only", "A 股行情刷新" if lang == "zh" else "A-share price refresh"),
        ("train_cn_signals", "A 股信号训练" if lang == "zh" else "A-share signal training"),
        ("screener_precompute_core", "核心候选预计算" if lang == "zh" else "Core screener precompute"),
        ("screener_precompute_combos", "多模型组合预计算" if lang == "zh" else "Multi-model combo precompute"),
        ("screener_precompute_rest", "次级 / 专用模板预计算" if lang == "zh" else "Specialized template precompute"),
        ("refresh_us_grouped_daily", "美股收盘行情" if lang == "zh" else "U.S. closing prices"),
        ("us_signal_train", "美股信号训练" if lang == "zh" else "U.S. signal training"),
        ("precompute_us_screeners", "美股候选预计算" if lang == "zh" else "U.S. candidate precompute"),
    ]
    latest_by_type: dict[str, dict] = {}
    for job in jobs:
        job_type = str(job.get("job_type") or "")
        if job_type not in latest_by_type or int(job.get("id") or 0) > int(latest_by_type[job_type].get("id") or 0):
            latest_by_type[job_type] = job

    def job_detail_link(job: dict | None) -> str:
        if not job:
            return "<span class='muted'>—</span>"
        return (
            f"<a class='detail-link' href='/dashboard/ops/job/{int(job['id'])}?lang={lang}'>"
            f"# {int(job['id'])} →</a>"
        )

    critical_rows = "".join(
        (
            "<tr>"
            f"<td><strong>{html.escape(label)}</strong><span class='job-type'>{html.escape(job_type)}</span></td>"
            f"<td>{_task_status_badge(str(job.get('status') or 'idle'), lang=lang) if job else '<span class=\'muted\'>—</span>'}</td>"
            f"<td>{html.escape(_display_job_message((job or {}).get('message') or '-', lang=lang))}</td>"
            f"<td>{job_detail_link(job)}</td>"
            "</tr>"
        )
        for job_type, label in critical_job_types
        for job in [latest_by_type.get(job_type)]
    )
    attention_rows = "".join(
        "<li>"
        f"<a href='/dashboard/ops/job/{int(job['id'])}?lang={lang}'><strong>#{int(job['id'])}</strong> · {html.escape(str(job.get('job_type') or '-'))}</a> "
        f"{_task_status_badge(str(job.get('status') or 'idle'), lang=lang)}"
        f"<span>{html.escape(_display_job_message(job.get('message') or '-', lang=lang))}</span>"
        "</li>"
        for job in attention_jobs
    ) or f"<li class='muted'>{t(lang, '没有需要关注的任务。', 'No jobs need attention.')}</li>"
    all_rows = "".join(
        "<tr>"
        f"<td><a href='/dashboard/ops/job/{int(job['id'])}?lang={lang}'><strong>#{int(job['id'])}</strong><span class='job-type'>{html.escape(str(job.get('job_type') or '-'))}</span></a></td>"
        f"<td>{_display_time(job.get('started_at'))}</td><td>{_display_time(job.get('finished_at'))}</td><td>{_task_duration(job)}</td>"
        f"<td>{_task_status_badge(str(job.get('status') or 'idle'), lang=lang)}</td>"
        f"<td class='message'>{html.escape(_display_job_message(job.get('message') or '-', lang=lang))}</td>"
        "</tr>"
        for job in jobs
    ) or f"<tr><td colspan='6'>{t(lang, '当天没有任务记录。', 'No jobs recorded for today.')}</td></tr>"
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    history_href = "/dashboard/ops/history?" + urlencode({"lang": lang, "date": today, "lookback_runs": lookback_runs})
    return render_dashboard_legacy_page(
        "dashboard/legacy/ops_dashboard_ops_today_page.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '今天所有 Job 运行情况', 'All Jobs Today')}",
            f'{JOB_DETAIL_LINK_STYLE}',
            f"{t(lang, '今日 Job', 'Today’s Jobs')}",
            f"{t(lang, '一个固定入口，集中核对今天所有任务。', 'One pinned place to verify every run from today.')}",
            f'{nav_html}',
            f"{t(lang, '固定模块', 'Pinned module')}",
            f"{t(lang, '今天所有 Job 运行情况', 'All jobs today')}",
            f'{today}',
            f"{t(lang, '点击 Job 可查看完整参数、结果与关联产出。', 'Open a job to inspect full parameters, results, and linked output.')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '任务中心', 'Task center')}",
            f'{history_href}',
            f"{t(lang, '按日期查看历史', 'History by date')}",
            f'{conclusion_tone}',
            f"{t(lang, '运行结论', 'Run conclusion')}",
            f'{html.escape(conclusion)}',
            f"{t(lang, '总任务', 'Total')}",
            f'{len(jobs)}',
            f"{t(lang, '完成', 'Done')}",
            f"{counts.get('success', 0)}",
            f"{t(lang, '运行中', 'Running')}",
            f"{counts.get('running', 0)}",
            f"{t(lang, '部分完成', 'Partial')}",
            f"{counts.get('partial', 0)}",
            f"{t(lang, '失败', 'Failed')}",
            f'{failed_count}',
            f"{t(lang, '关键链路', 'Critical chain')}",
            f"{t(lang, '行情、训练与候选快照', 'Prices, training, and candidate snapshots')}",
            f"{t(lang, '仅展示今日最新一次运行。', 'Shows the latest run today only.')}",
            f"{t(lang, '任务', 'Job')}",
            f"{t(lang, '状态', 'Status')}",
            f"{t(lang, '回执', 'Receipt')}",
            f"{t(lang, '详情', 'Details')}",
            f'{critical_rows}',
            f"{t(lang, '需要关注', 'Needs attention')}",
            f"{t(lang, '失败、运行中、部分完成或未配置', 'Failed, running, partial, or not configured')}",
            f'{attention_rows}',
            f"{t(lang, '全部记录', 'All records')}",
            f"{t(lang, '今天所有 Job 运行情况', 'Every job run today')}",
            f"{t(lang, '按最新记录排序。', 'Newest first.')}",
            f"{t(lang, '任务', 'Job')}",
            f"{t(lang, '开始', 'Started')}",
            f"{t(lang, '结束', 'Finished')}",
            f"{t(lang, '耗时', 'Duration')}",
            f"{t(lang, '状态', 'Status')}",
            f"{t(lang, '回执', 'Receipt')}",
            f'{all_rows}',
        ],
    )



@router.get("/ops/history", response_class=HTMLResponse)
def dashboard_ops_history_page(request: Request, lang: str = "en", lookback_runs: int = 5, date: str | None = None, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/history")
    lang = resolve_request_lang(request)
    selected_date = str(date or datetime.now().astimezone().date().isoformat())[:10]
    job_repo = DataJobRepository(db)
    rows = [
        job_repo._serialize_job(job)
        for job in db.scalars(select(DataJob).order_by(DataJob.id.desc()).limit(800)).all()
    ]
    filtered = [
        {
            **job,
            "display_message": _display_job_message(job.get("message") or "-", lang=lang),
        }
        for job in rows
        if str(job.get("started_at") or "").startswith(selected_date)
        and str(job.get("job_type") or "").lower() != DECOMMISSIONED_CN_REVIEW_JOB_TYPE
    ]
    return render_ops_history_page(
        rows=filtered,
        selected_date=selected_date,
        lang=lang,
        lookback_runs=lookback_runs,
        nav_html=render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs),
    )


@router.get("/ops/job/{job_id}", response_class=HTMLResponse)
def dashboard_ops_job_detail(job_id: int, request: Request, lang: str = "en", db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/ops/job/{job_id}")
    lang = resolve_request_lang(request)
    job = DataJobRepository(db).get_job_detail(job_id)
    if job is None or str(job.get("job_type") or "").lower() == DECOMMISSIONED_CN_REVIEW_JOB_TYPE:
        return HTMLResponse("Job not found", status_code=404)
    return render_ops_job_detail_page(
        job_id=job_id,
        job={
            **job,
            "display_message": _display_job_message(job.get("message") or "-", lang=lang),
        },
        lang=lang,
        nav_html=render_workspace_nav_html(lang=lang, active_key="ops"),
    )


@router.get("/ops/jobs", response_class=HTMLResponse)
def dashboard_ops_jobs_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/jobs")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    recent_jobs = [
        item
        for item in load_recent_jobs_summary(db, limit=20)
        if str(item.get("job_type") or "").lower() != DECOMMISSIONED_CN_REVIEW_JOB_TYPE
    ][:12]
    screener_stage_rows = _build_screener_precompute_stage_rows(recent_jobs, lang=lang)
    dashboard_redirect = "/dashboard/ops/jobs?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    return render_ops_jobs_page(
        recent_jobs=recent_jobs,
        stage_rows=screener_stage_rows,
        actions_html=_render_screener_precompute_action_forms(
            lang=lang,
            redirect_to=dashboard_redirect,
            compact=True,
            stage_rows=screener_stage_rows,
        ),
        summarize_precompute=_summarize_screener_precompute_job,
        lang=lang,
        lookback_runs=lookback_runs,
        nav_html=render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs),
    )
