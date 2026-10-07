"""Shared helpers and constants for the dashboard route package.

Lands here when more than one page cluster needs the same node."""

import html

import json

import re

from urllib.parse import urlencode


from sqlalchemy.orm import Session

from app.api.presentation.i18n import t


from app.services.ai_daily_report import (
    _security_name_is_code,
    load_ai_daily_report,
)

from app.services.dashboard_summary import load_dashboard_summary


from app.services.model_signal_summary import build_signal_label

from app.services.repository import (
    SymbolRepository,
    WatchlistRepository,
)

from app.services.runtime_cache import get_or_set
from app.services.market_lake import load_lake_rows  # noqa: F401 - patch target guarded by model-performance tests

from app.services.workspace_snapshots import build_continuous_leaders_snapshot



from app.services.dashboard_market_context import (
    _build_market_context,
    _concept_price_strength,  # noqa: F401 - compatibility export
    _concept_slug,  # noqa: F401 - compatibility export
)
from app.services.stock_selection.reason_screen_policy import (
    _reason_screen_params,  # noqa: F401 - compatibility export
    reason_screen_href as _reason_screen_href,
)


from app.services.continuous_leaders import (
    continuous_leader_sort_key as _continuous_leader_sort_key,  # noqa: F401 - compatibility export
)
from app.services.display_compaction import (
    compact_job_type as _compact_job_type,  # noqa: F401 - compatibility export
    compact_label as _compact_label,  # noqa: F401 - compatibility export
    compact_run_name as _compact_run_name,  # noqa: F401 - compatibility export
)




def _display_job_message(message: object, *, lang: str) -> str:
    text = str(message or "").strip()
    if not text:
        return "-"
    direct_patterns = [
        (
            r"^U\.S\. lake already contains (\d{4}-\d{2}-\d{2}) with (\d+) symbols; proceeding with training and screener precompute without a fresh Polygon success\.$",
            (lambda m: f"美股本地行情已补齐到 {m.group(1)}（{m.group(2)} 只），继续训练与预计算。")
            if lang == "zh"
            else (lambda m: f"U.S. lake already has {m.group(2)} symbols for {m.group(1)}; training and screener precompute continued without waiting for a fresh Polygon success."),
        ),
        (
            r"^Retraining U\.S\. LightGBM signals after full (\d{4}-\d{2}-\d{2}) grouped-daily refresh\.$",
            (lambda m: f"正在基于完整的 {m.group(1)} 美股收盘数据重训 LightGBM 模型。")
            if lang == "zh"
            else (lambda m: f"Retraining the U.S. LightGBM model from the full {m.group(1)} grouped-daily refresh."),
        ),
        (
            r"^Trained (\d+) U\.S\. symbols from full (\d{4}-\d{2}-\d{2}) refresh, wrote (\d+) predictions and (\d+) backtest rows\.$",
            (lambda m: f"已基于完整的 {m.group(2)} 美股收盘数据完成重训：{m.group(1)} 只股票，写入 {m.group(3)} 条预测、{m.group(4)} 条回测记录。")
            if lang == "zh"
            else (lambda m: f"Completed U.S. retraining from the full {m.group(2)} close: {m.group(1)} symbols, {m.group(3)} predictions, {m.group(4)} backtest rows."),
        ),
        (
            r"^Precomputing U\.S\. screener snapshots after full (\d{4}-\d{2}-\d{2}) retrain\.$",
            (lambda m: f"正在基于完整的 {m.group(1)} 美股重训结果生成筛选快照。")
            if lang == "zh"
            else (lambda m: f"Precomputing U.S. screener snapshots from the full {m.group(1)} retrain."),
        ),
        (
            r"^Precomputed (\d+) U\.S\. screener snapshot\(s\) after full (\d{4}-\d{2}-\d{2}) retrain\.$",
            (lambda m: f"已基于完整的 {m.group(2)} 美股重训结果生成 {m.group(1)} 个筛选快照。")
            if lang == "zh"
            else (lambda m: f"Precomputed {m.group(1)} U.S. screener snapshots from the full {m.group(2)} retrain."),
        ),
    ]
    for pattern, replacement in direct_patterns:
        matched = re.match(pattern, text, flags=re.IGNORECASE)
        if matched:
            return replacement(matched) if callable(replacement) else replacement
    legacy_db_word = "sql" + "ite"
    replacements = [
        (rf"共享\s*{legacy_db_word}\s*库", "共享数据库" if lang == "zh" else "shared database"),
        (rf"shared {legacy_db_word} (?:library|db|database)", "共享数据库" if lang == "zh" else "shared database"),
        (rf"\b{legacy_db_word}\s+库\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (rf"\b{legacy_db_word}\s+database\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (rf"\b{legacy_db_word}\s+db\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (rf"\b{legacy_db_word}\b", "兼容数据库" if lang == "zh" else "compatibility database"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text






def _reason_screen_link(
    label: str,
    *,
    reason: str | None,
    status: str | None,
    market: str | None,
    lang: str,
    css_class: str = "",
) -> str:
    href = html.escape(_reason_screen_href(reason=reason, status=status, market=market, lang=lang), quote=True)
    class_attr = f" class='{css_class}'" if css_class else ""
    return f"<a{class_attr} href='{href}'>{html.escape(label)}</a>"



def _dashboard_watchlist_map(db: Session) -> dict[str, dict]:
    def _load() -> dict[str, dict]:
        watchlist_repo = WatchlistRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        return watchlist_repo.list_ticker_map(watchlist.id)

    return get_or_set("dashboard_watchlist_map", "default", ttl_seconds=30.0, loader=_load)



CONCEPT_TEXT = {
    "en": {
        "back_to_dashboard": "Back to dashboard",
        "concept_detail": "Concept Detail",
        "detail_subtitle": "A deeper look at the latest model hits inside this concept.",
        "continuous_leaders": "Continuous Leaders",
        "continuous_detail": "Continuous Leader Detail",
        "continuous_subtitle": "Stocks that keep showing up across recent model snapshots, with watchlist actions and quick filtering.",
        "market_filter": "Market",
        "state_filter": "Watchlist State",
        "apply_filters": "Apply Filters",
        "hits": "Hits",
        "hits_help": "Current Top-N names inside this concept",
        "delta": "Delta",
        "delta_help": "Change versus the previous Top-N snapshot",
        "streak": "Streak",
        "streak_help": "Consecutive snapshots with at least one hit",
        "trend": "Trend",
        "trend_help": "Recent Top-N concept hit trend",
        "follow_this_concept": "Follow This Concept",
        "auto_enable_sync": "Auto-enable Sync for added stocks",
        "sync_now": "Sync concept stocks now",
        "add_concept_stocks": "Add Concept Stocks To Watchlist",
        "top_n_watch": "Top-N Watch",
        "top_n_help": "Only add top N tickers from the current sort order",
        "sync_selected_top_n": "Sync selected top N now",
        "add_top_n": "Add Top N To Watchlist",
        "top_movers_comparison": "Top Movers Comparison",
        "top_by_model": "Top by Model",
        "top_by_20d": "Top by 20D",
        "ready_first": "Ready First",
        "last": "Last",
        "ticker_breakdown": "Ticker Breakdown",
        "ticker": "Ticker",
        "name": "Name",
        "model_score": "Model Score",
        "five_day": "5D %",
        "twenty_day": "20D %",
        "breadth": "Breadth",
        "breadth_help": "Share of concept members with positive recent performance.",
        "concept_strength": "Concept Strength",
        "concept_strength_subtitle": "Price-based confirmation for whether this concept is actually moving, not just getting model attention.",
        "buy_signal_count": "Buy Signals",
        "buy_signal_count_help": "How many tracked tickers inside this concept currently show a Buy signal.",
        "max_signal_strength": "Max Signal Strength",
        "max_signal_strength_help": "The strongest model signal currently found inside this concept.",
        "watchlist": "Watchlist",
        "last_sync": "Last Sync",
        "actions": "Actions",
        "add": "Add",
        "sync": "Sync",
        "open": "Open",
        "insight": "Insight",
        "not_in_watchlist": "Not In Watchlist",
        "ready": "Ready",
        "waiting": "Waiting",
        "in_watchlist": "In Watchlist",
        "no_tickers": "No tickers yet",
        "not_enough_price_history": "Not enough price history yet.",
        "lang_en": "English",
        "lang_zh": "中文",
    },
    "zh": {
        "back_to_dashboard": "返回总览",
        "concept_detail": "概念详情",
        "detail_subtitle": "查看这个概念在最近模型 Top-N 中的命中构成。",
        "continuous_leaders": "连续强势股",
        "continuous_detail": "连续强势股详情",
        "continuous_subtitle": "查看最近几次模型快照中持续出现的股票，并直接进行筛选、自选和同步操作。",
        "market_filter": "市场",
        "state_filter": "自选状态",
        "apply_filters": "应用筛选",
        "hits": "命中数",
        "hits_help": "当前 Top-N 中属于该概念的股票数量",
        "delta": "变化值",
        "delta_help": "相对上一次 Top-N 快照的变化",
        "streak": "连续性",
        "streak_help": "连续多少次快照里至少出现过一只命中股",
        "trend": "趋势",
        "trend_help": "最近几次 Top-N 中这个概念的热度走势",
        "follow_this_concept": "跟踪这个概念",
        "auto_enable_sync": "加入后自动开启同步",
        "sync_now": "立即同步概念股票",
        "add_concept_stocks": "将概念股加入自选",
        "top_n_watch": "前 N 名跟踪",
        "top_n_help": "只加入当前排序下前 N 个股票",
        "sync_selected_top_n": "立即同步选中的前 N 名",
        "add_top_n": "将前 N 名加入自选",
        "top_movers_comparison": "强势股对比",
        "top_by_model": "按模型分数",
        "top_by_20d": "按20日强度",
        "ready_first": "优先已就绪",
        "last": "最新",
        "ticker_breakdown": "股票明细",
        "ticker": "代码",
        "name": "名称",
        "model_score": "模型分数",
        "five_day": "5日涨跌",
        "twenty_day": "20日涨跌",
        "breadth": "上涨广度",
        "breadth_help": "概念内近期表现为正的股票占比。",
        "concept_strength": "概念强弱",
        "concept_strength_subtitle": "用价格表现确认这个概念是否真的在走强，而不只是模型命中集中。",
        "buy_signal_count": "买点股数量",
        "buy_signal_count_help": "这个概念里当前显示买点信号的股票数量。",
        "max_signal_strength": "最强信号强度",
        "max_signal_strength_help": "这个概念里当前最强模型信号的强度值。",
        "watchlist": "自选状态",
        "last_sync": "最近同步",
        "actions": "操作",
        "add": "加入",
        "sync": "同步",
        "open": "打开",
        "insight": "分析页",
        "not_in_watchlist": "未加入自选",
        "ready": "已就绪",
        "waiting": "同步中",
        "in_watchlist": "已在自选",
        "no_tickers": "暂无股票",
        "not_enough_price_history": "价格历史还不够。",
        "lang_en": "English",
        "lang_zh": "中文",
    },
}



def _concept_tr(lang: str, key: str) -> str:
    return CONCEPT_TEXT["zh" if lang == "zh" else "en"][key]



DASHBOARD_TEXT = {
    "en": {
        "title": "Personal Quant Workbench",
        "hero": "Personal Quant Workbench",
        "lead": "A local research cockpit for watchlists, screeners, model signals, concept resonance, and replay-friendly stock analysis.",
        "open_watchlist": "Open Watchlist",
        "open_screener": "Open Screener",
        "data_sources": "Data Sources",
        "logout": "Logout",
        "lang_en": "English",
        "lang_zh": "中文",
        "stock_insight_search": "Stock Insight Search",
        "search_placeholder": "Type a ticker like ASTS",
        "open_insight_page": "Open Insight Page",
        "search_help": "This view turns market data into a trend score, buy zone, take-profit zone, and risk level.",
        "auto_analysis": "Auto Analysis",
        "on": "On",
        "off": "Off",
        "enabled": "Enabled",
        "disabled": "Disabled",
        "every_hours": "Every {hours} hour(s)",
        "next_run": "Next run",
        "turn_off": "Turn Off",
        "turn_on": "Turn On",
        "data_source": "Data Source",
        "current_dominant_provider": "Current dominant provider across synced symbols",
        "open_detailed_source_page": "Open detailed source page",
        "latest_model": "Latest Model",
        "status": "Status",
        "type": "Type",
        "signals": "Signals",
        "latest_date": "Latest date",
        "top_ticker": "Top ticker",
        "backtest": "Backtest",
        "run": "Run",
        "period": "Period",
        "concept_resonance": "Concept Resonance",
        "concept_resonance_help": "How concentrated the latest Top-N signals are inside the strongest tracked concept.",
        "tracked_signals": "Tracked signals",
        "snapshot_window": "Snapshot Window",
        "snapshot_help": "Heatmap, concept resonance, and activity tracking are currently based on the most recent {runs} model snapshots.",
        "quick_actions": "Quick Actions",
        "seed_sample_data": "Seed Sample Data",
        "provider": "Provider",
        "start": "Start",
        "end": "End",
        "sync_market_data": "Sync Market Data",
        "pipeline_tickers": "Pipeline Tickers",
        "pipeline_run_name": "Pipeline Run Name",
        "signal": "Signal",
        "lookback": "Lookback",
        "top_n": "Top N",
        "run_full_pipeline": "Run Full Pipeline",
        "auto_analyze_my_watchlist": "Auto analyze my watchlist",
        "interval_hours": "Interval Hours",
        "start_date": "Start Date",
        "refresh_cn_concepts": "Refresh CN concepts during auto analysis",
        "save_auto_analysis": "Save Auto Analysis",
        "run_watchlist_analysis_now": "Run Watchlist Analysis Now",
        "cn_tickers": "CN Tickers",
        "sync_cn_fundamentals": "Sync CN Fundamentals",
        "cn_concept_tickers": "CN Concept Tickers",
        "sync_cn_concepts": "Sync CN Concepts",
        "us_hk_tickers": "US / HK Tickers",
        "sync_us_hk_fundamentals": "Sync US/HK Fundamentals",
        "run_name": "Run Name",
        "run_training": "Run Training",
        "model_run_id": "Model Run ID",
        "leave_blank_latest": "Leave blank for latest",
        "run_backtest": "Run Backtest",
        "json_shortcuts": "JSON Shortcuts",
        "dashboard_summary_json": "Dashboard Summary JSON",
        "latest_signals_json": "Latest Signals JSON",
        "latest_backtest_curve_json": "Latest Backtest Curve JSON",
        "sync_states_json": "Sync States JSON",
        "latest_signals": "Latest Signals",
        "risk_overview": "Risk Overview",
        "tagged_names": "Tagged names",
        "common_risks": "Common risks",
        "risk_examples": "Examples",
        "no_execution_risks": "No execution warnings across the current focus list.",
        "ticker": "Ticker",
        "date": "Date",
        "score": "Score",
        "rank": "Rank",
        "sector_heatmap": "Sector Heatmap",
        "sector_heatmap_help": "A quick view of where the latest model picks cluster. Stronger tiles now blend Top-N concentration with concept-level 5D strength and breadth.",
        "heatmap_sort": "Heatmap Sort",
        "sort_by_hits": "Top-N Hits",
        "sort_by_5d": "5D Strength",
        "sort_by_breadth": "Breadth",
        "sort_by_score": "Avg Score",
        "signal_distribution": "Signal Distribution",
        "market": "Market",
        "top_n_hits": "Top-N Hits",
        "name": "Name",
        "tickers": "Tickers",
        "continuous_leaders": "Continuous Leaders",
        "hits": "Hits",
        "continuous_help": "Stocks that kept showing up across the most recent {runs} model snapshots. This is the quickest way to spot persistent strength instead of one-off spikes.",
        "open_continuous_leaders": "Open Continuous Leaders",
        "watchlist_state": "Watchlist State",
        "all": "All",
        "ready": "Ready",
        "waiting": "Waiting",
        "off_state": "Off",
        "apply_leader_filters": "Apply Leader Filters",
        "auto_enable_sync": "Auto-enable Sync",
        "sync_top_n_now": "Sync top N now",
        "latest_signal_date": "Latest Signal Date",
        "watchlist": "Watchlist",
        "action": "Action",
        "add": "Add",
        "sync": "Sync",
        "open": "Open",
        "add_top_n_continuous_leaders": "Add Top N Continuous Leaders",
        "concept_activity_tracker": "Concept Activity Tracker",
        "concept": "Concept",
        "prev": "Prev",
        "delta_hits": "Δ Hits",
        "streak": "Streak",
        "trend": "Trend",
        "five_day": "5D",
        "breadth": "Breadth",
        "avg_score": "Avg Score",
        "sync_states": "Sync States",
        "last_sync": "Last Sync",
        "backtest_summary": "Backtest Summary",
        "recent_model_runs": "Recent Model Runs",
        "config": "Config",
        "created": "Created",
        "backtest_this_run": "Backtest This Run",
        "equity_curve": "Equity Curve",
        "recent_jobs": "Recent Jobs",
        "started": "Started",
        "finished": "Finished",
        "params": "Params",
        "message": "Message",
        "concept_data_note": "CN concepts: {freshness} · {as_of}",
    },
    "zh": {
        "title": "个人量化工作台",
        "hero": "个人量化工作台",
        "lead": "一个本地研究控制台，用来管理自选、选股器、模型信号、概念共振和适合复盘的个股分析。",
        "open_watchlist": "打开自选股",
        "open_screener": "打开选股器",
        "data_sources": "数据来源",
        "logout": "退出登录",
        "lang_en": "English",
        "lang_zh": "中文",
        "stock_insight_search": "个股分析搜索",
        "search_placeholder": "输入股票代码，例如 ASTS",
        "open_insight_page": "打开分析页",
        "search_help": "这个页面会把行情转成趋势评分、买入区、止盈区和风险位。",
        "auto_analysis": "自动分析",
        "on": "开启",
        "off": "关闭",
        "enabled": "已启用",
        "disabled": "已停用",
        "every_hours": "每 {hours} 小时运行一次",
        "next_run": "下次运行",
        "turn_off": "关闭",
        "turn_on": "开启",
        "data_source": "数据源",
        "current_dominant_provider": "当前同步股票里最主要的数据源",
        "open_detailed_source_page": "打开数据源详情页",
        "latest_model": "最新模型",
        "status": "状态",
        "type": "类型",
        "signals": "信号",
        "latest_date": "最新日期",
        "top_ticker": "最高分股票",
        "backtest": "回测",
        "run": "运行",
        "period": "区间",
        "concept_resonance": "概念共振",
        "concept_resonance_help": "最新 Top-N 信号集中在最强概念中的程度。",
        "tracked_signals": "跟踪信号数",
        "snapshot_window": "快照窗口",
        "snapshot_help": "热力图、概念共振和概念追踪都基于最近 {runs} 次模型快照。",
        "quick_actions": "快捷操作",
        "seed_sample_data": "注入样例数据",
        "provider": "数据源",
        "start": "开始日期",
        "end": "结束日期",
        "sync_market_data": "同步行情",
        "pipeline_tickers": "流水线股票",
        "pipeline_run_name": "流水线运行名",
        "signal": "信号",
        "lookback": "回看窗口",
        "top_n": "前 N 名",
        "run_full_pipeline": "运行完整流水线",
        "auto_analyze_my_watchlist": "自动分析我的自选股",
        "interval_hours": "间隔小时数",
        "start_date": "开始日期",
        "refresh_cn_concepts": "自动分析时刷新 A 股概念",
        "save_auto_analysis": "保存自动分析设置",
        "run_watchlist_analysis_now": "立即运行自选股分析",
        "cn_tickers": "A股代码",
        "sync_cn_fundamentals": "同步 A 股基本面",
        "cn_concept_tickers": "A股概念股票",
        "sync_cn_concepts": "同步 A 股概念",
        "us_hk_tickers": "美股 / 港股代码",
        "sync_us_hk_fundamentals": "同步美股/港股基本面",
        "run_name": "运行名称",
        "run_training": "运行训练",
        "model_run_id": "模型运行 ID",
        "leave_blank_latest": "留空表示使用最新模型",
        "run_backtest": "运行回测",
        "json_shortcuts": "JSON 快捷入口",
        "dashboard_summary_json": "Dashboard 摘要 JSON",
        "latest_signals_json": "最新信号 JSON",
        "latest_backtest_curve_json": "最新回测曲线 JSON",
        "sync_states_json": "同步状态 JSON",
        "latest_signals": "最新信号",
        "risk_overview": "风险概览",
        "tagged_names": "带提醒股票数",
        "common_risks": "常见提醒",
        "risk_examples": "示例股票",
        "no_execution_risks": "当前重点股票里暂无执行提醒。",
        "ticker": "代码",
        "date": "日期",
        "score": "分数",
        "rank": "排名",
        "sector_heatmap": "板块热力图",
        "sector_heatmap_help": "快速查看最新模型命中集中在哪些概念。热力同时参考 Top-N 集中度、概念 5 日强弱和上涨广度。",
        "heatmap_sort": "热力图排序",
        "sort_by_hits": "按命中数",
        "sort_by_5d": "按 5 日强度",
        "sort_by_breadth": "按上涨广度",
        "sort_by_score": "按平均分数",
        "signal_distribution": "信号分布",
        "market": "市场",
        "top_n_hits": "Top-N 命中数",
        "name": "名称",
        "tickers": "股票",
        "continuous_leaders": "连续强势股",
        "hits": "命中数",
        "continuous_help": "最近 {runs} 次模型快照中持续出现的股票，更适合发现连续强势而不是一次性异动。",
        "open_continuous_leaders": "打开连续强势股",
        "watchlist_state": "自选状态",
        "all": "全部",
        "ready": "已就绪",
        "waiting": "同步中",
        "off_state": "未开启",
        "apply_leader_filters": "应用筛选",
        "auto_enable_sync": "自动开启同步",
        "sync_top_n_now": "立即同步前 N 名",
        "latest_signal_date": "最新信号日期",
        "watchlist": "自选",
        "action": "操作",
        "add": "加入",
        "sync": "同步",
        "open": "打开",
        "add_top_n_continuous_leaders": "将前 N 名连续强势股加入自选",
        "concept_activity_tracker": "概念异动追踪",
        "concept": "概念",
        "prev": "前值",
        "delta_hits": "变化",
        "streak": "连续性",
        "trend": "趋势",
        "five_day": "5日",
        "breadth": "广度",
        "avg_score": "平均分",
        "sync_states": "同步状态",
        "last_sync": "最近同步",
        "backtest_summary": "回测摘要",
        "recent_model_runs": "最近模型运行",
        "config": "配置",
        "created": "创建时间",
        "backtest_this_run": "回测这个运行",
        "equity_curve": "净值曲线",
        "recent_jobs": "最近任务",
        "started": "开始",
        "finished": "完成",
        "params": "参数",
        "message": "消息",
        "concept_data_note": "A股概念：{freshness} · {as_of}",
    },
}



def _dt(lang: str, key: str, **kwargs) -> str:
    value = DASHBOARD_TEXT["zh" if lang == "zh" else "en"][key]
    return value.format(**kwargs) if kwargs else value



def _lookback_options() -> list[int]:
    return [3, 5, 10]



def _clamp_lookback_runs(value: int | None) -> int:
    try:
        numeric = int(value) if value is not None else None
    except (TypeError, ValueError):
        numeric = None
    if numeric in _lookback_options():
        return int(numeric)
    return 5



def _lookback_pills(base_path: str, *, selected: int, extra_params: dict[str, str] | None = None) -> str:
    params = extra_params or {}
    lang = params.get("lang", "en")
    pills = []
    for option in _lookback_options():
        query = urlencode({**params, "lookback_runs": option})
        pills.append(
            f"<a href='{base_path}?{query}' class='compare-pill{' active' if selected == option else ''}'>"
            f"{option} {t(lang, '次', 'runs')}"
            "</a>"
        )
    return "".join(pills)



def _load_summary(db: Session, *, lookback_runs: int = 5) -> dict:
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    cache_key = json.dumps({"lookback_runs": lookback_runs}, sort_keys=True, ensure_ascii=False)

    def _load() -> dict:
        return load_dashboard_summary(
            db,
            lookback_runs=lookback_runs,
            market_context_loader=lambda latest_signals: _build_market_context(
                db,
                latest_signals,
                lookback_runs=lookback_runs,
            ),
        )

    return get_or_set("dashboard_summary_bundle", cache_key, ttl_seconds=60.0, loader=_load)



def _continuous_leaders_for_summary(db: Session) -> list[dict]:
    """Continuous-leader rows built on demand from the shared snapshot source.

    The continuous-leaders page/export prefer the workspace snapshot; when it is
    absent they fall back to ``market_context["continuous_leaders"]``. Reusing
    the exact builder behind ``SNAPSHOT_CONTINUOUS_LEADERS`` keeps that fallback
    faithful (instead of always empty) without a second selection codepath.
    """
    payload = build_continuous_leaders_snapshot(db)
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return list(rows) if isinstance(rows, list) else []


def _lightweight_market_context(db: Session, latest_signals: list[dict]) -> dict:
    risk_counts: dict[str, int] = {}
    tagged_examples: list[dict] = []
    for item in latest_signals:
        tags = [str(tag).strip() for tag in (item.get("risk_flags") or item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        tagged_examples.append(
            {
                "ticker": item.get("ticker"),
                "tags": tags[:2],
                "signal_strength": int(item.get("signal_strength") or 0),
            }
        )
    tagged_examples.sort(key=lambda entry: (-entry["signal_strength"], str(entry.get("ticker") or "")))
    return {
        "market_distribution": [],
        "top_concepts": [],
        "sector_heatmap": [],
        "concept_tracker": [],
        "continuous_leaders": _continuous_leaders_for_summary(db),
        "risk_overview": {
            "tagged_names": len(tagged_examples),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
            ],
            "examples": tagged_examples[:3],
        },
        "resonance_score": 0.0,
        "tracked_signal_count": len(latest_signals),
    }



def _load_home_summary(db: Session, *, lookback_runs: int = 5) -> dict:
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    cache_key = json.dumps({"lookback_runs": lookback_runs, "kind": "home"}, sort_keys=True, ensure_ascii=False)

    def _load() -> dict:
        return load_dashboard_summary(
            db,
            lookback_runs=lookback_runs,
            market_context_loader=lambda latest_signals: _lightweight_market_context(db, latest_signals),
        )

    return get_or_set("dashboard_home_summary_bundle", cache_key, ttl_seconds=60.0, loader=_load)



def _load_cached_ai_daily_report(db: Session) -> dict:
    report = get_or_set(
        "dashboard_ai_daily_report",
        "latest",
        ttl_seconds=45.0,
        loader=lambda: load_ai_daily_report(db=db) or {},
    )
    return _hydrate_ai_report_names(report, db=db)



def _hydrate_ai_report_names(report: dict | None, *, db: Session) -> dict:
    payload = report or {}
    row_groups = [
        payload.get("portfolio_rows") or [],
        payload.get("market_recommendations") or [],
        payload.get("market_watch_recommendations") or [],
        payload.get("market_candidates_all") or [],
        payload.get("rows") or [],
        payload.get("buy_the_dip_rows") or [],
        payload.get("us_model_recommendations") or [],
        ((payload.get("social_signal_summary") or {}).get("actionable") or []),
        payload.get("us_hotspot_validation") or [],
    ]
    tickers: list[str] = []
    for rows in row_groups:
        for item in rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            if ticker:
                tickers.append(ticker)
    if not tickers:
        return payload
    overviews = SymbolRepository(db).list_overviews_for_tickers(list(dict.fromkeys(tickers)))
    for rows in row_groups:
        for item in rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            name = str(
                item.get("name")
                or item.get("stock_name")
                or item.get("security_name")
                or item.get("display_name")
                or ""
            ).strip()
            resolved_name = str((overviews.get(ticker) or {}).get("name") or "").strip()
            if resolved_name and _security_name_is_code(name, ticker):
                item["name"] = resolved_name
            elif name:
                item["name"] = name
    return payload


















def _dashboard_home_signal(score: float | None, lang: str) -> tuple[str, str]:
    label = build_signal_label(score, lang=lang) or (t(lang, "观察", "Watch"))
    normalized = str(label).strip().lower()
    if normalized in {"buy", "买入"}:
        return label, "sig-buy"
    if normalized in {"sell", "卖出"}:
        return label, "sig-sell"
    if normalized in {"watch", "观察"}:
        return label, "sig-watch"
    return label, "sig-hold"



US_SIGNAL_TRAIN_JOB_TYPES = ("us_signal_train", "train_us_signals")



def _find_latest_job_by_type(recent_jobs: list[dict], job_type: str | list[str] | tuple[str, ...]) -> dict | None:
    if isinstance(job_type, (list, tuple, set)):
        keys = {str(item or "").strip().lower() for item in job_type if str(item or "").strip()}
    else:
        keys = {str(job_type or "").strip().lower()}
    return next((item for item in recent_jobs if str(item.get("job_type") or "").strip().lower() in keys), None)



def _summarize_screener_precompute_job(job: dict | None, *, lang: str = "zh") -> dict:
    if not isinstance(job, dict):
        return {
            "summary": "待运行" if lang == "zh" else "Pending",
            "detail": "还没有任务记录。" if lang == "zh" else "No job record yet.",
            "status": "idle",
        }
    status = str(job.get("status") or "").strip().lower() or "idle"
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    created = list(result.get("snapshots_created") or [])
    failed_templates = list(result.get("failed_templates") or [])
    failed_presets = list(result.get("failed_presets") or [])
    failed_total = int(result.get("failed_count") or 0)
    total = int(result.get("count") or 0) + failed_total
    tail_jobs_scheduled = bool(result.get("tail_jobs_scheduled"))
    message_text = str(job.get("message") or "").strip()
    if not tail_jobs_scheduled and "tail phases" in message_text.lower():
        tail_jobs_scheduled = True
    depends_on = [str(item) for item in (job.get("depends_on") or []) if str(item).strip()]
    pipeline_step = str(job.get("pipeline_step") or "").strip()
    if tail_jobs_scheduled and status == "success":
        summary = "核心已完成" if lang == "zh" else "Core Done"
    elif total > 0:
        summary = (
            f"{int(result.get('count') or 0)}/{total} 完成"
            if lang == "zh"
            else f"{int(result.get('count') or 0)}/{total} completed"
        )
    elif status == "running":
        summary = "运行中" if lang == "zh" else "Running"
    elif status == "success":
        summary = "成功" if lang == "zh" else "Success"
    else:
        summary = "待运行" if lang == "zh" else "Pending"
    detail_parts: list[str] = []
    if created:
        if created[0].get("preset_key"):
            created_names = [str(item.get("preset_label") or item.get("preset_key") or "-") for item in created[:3]]
        else:
            created_names = [str(item.get("model_template") or "-") for item in created[:3]]
        detail_parts.append(
            (
                f"已生成 {len(created)} 项：{', '.join(created_names)}"
                if lang == "zh"
                else f"Generated {len(created)} item(s): {', '.join(created_names)}"
            )
        )
    failed_items = failed_templates + failed_presets
    if failed_items:
        labels = [
            str(item.get("preset_label") or item.get("preset_key") or item.get("model_template") or "-")
            for item in failed_items[:3]
        ]
        detail_parts.append(
            (
                f"失败 {len(failed_items)} 项：{', '.join(labels)}"
                if lang == "zh"
                else f"Failed {len(failed_items)} item(s): {', '.join(labels)}"
            )
        )
    if pipeline_step:
        detail_parts.append(
            (f"阶段：{pipeline_step}" if lang == "zh" else f"Step: {pipeline_step}")
        )
    if tail_jobs_scheduled:
        detail_parts.append(
            "组合/补全已转后台继续" if lang == "zh" else "Combo/rest continue in background"
        )
    if depends_on:
        detail_parts.append(
            (
                f"依赖：{', '.join(depends_on)}"
                if lang == "zh"
                else f"Depends on: {', '.join(depends_on)}"
            )
        )
    if not detail_parts:
        detail_parts.append(job.get("message") or (t(lang, "暂无附加说明", "No additional note")))
    return {
        "summary": summary,
        "detail": " · ".join(detail_parts),
        "status": status,
    }



def _sparkline_svg(values: list[int]) -> str:
    if not values:
        return "<span class='muted'>-</span>"
    width = 108
    height = 32
    left_pad = 4
    right_pad = 4
    top_pad = 4
    bottom_pad = 4
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 1)
    step = (width - left_pad - right_pad) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((value - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
    stroke = "#0f766e" if values[-1] >= values[0] else "#b91c1c"
    return (
        f"<svg viewBox='0 0 {width} {height}' width='108' height='32' aria-label='trend sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='8' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        "</svg>"
    )



def _score_sparkline_svg(values: list[float]) -> str:
    if not values:
        return "<span class='muted'>-</span>"
    width = 108
    height = 32
    left_pad = 4
    right_pad = 4
    top_pad = 4
    bottom_pad = 4
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 0.000001)
    step = (width - left_pad - right_pad) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((value - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
    stroke = "#0f766e" if values[-1] >= values[0] else "#b91c1c"
    return (
        f"<svg viewBox='0 0 {width} {height}' width='108' height='32' aria-label='score sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='8' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        "</svg>"
    )















def _dashboard_model_badge(state: dict | None, *, confidence: int | None = None, compact: bool = False) -> str:
    if not state:
        return ""
    confidence_html = ""
    if confidence is not None:
        confidence_html = f"<span style='opacity:0.78;margin-left:6px;'>{confidence}%</span>"
    padding = "4px 8px" if compact else "6px 10px"
    font_size = "11px" if compact else "12px"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:{padding};border-radius:999px;"
        f"background:{state['bg']};color:{state['fg']};font-size:{font_size};font-weight:800;white-space:nowrap;'>"
        f"{state['label']}{confidence_html}</span>"
    )



def _signal_pill(score: float | None, *, lang: str, strength: int | None = None, compact: bool = False) -> str:
    label = build_signal_label(score, lang=lang) or ("Hold" if lang == "en" else "持有")
    key = label.lower()
    bg = "#f3f4f6"
    fg = "#374151"
    if key in {"buy", "买点"}:
        bg, fg = "#dcfce7", "#166534"
    elif key in {"watch", "观察"}:
        bg, fg = "#fef3c7", "#92400e"
    elif key in {"sell", "卖点"}:
        bg, fg = "#fee2e2", "#991b1b"
    else:
        bg, fg = "#e5e7eb", "#374151"
    suffix = f" · {int(strength)}" if strength is not None else ""
    padding = "4px 8px" if compact else "6px 10px"
    font_size = "11px" if compact else "12px"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:{padding};border-radius:999px;"
        f"background:{bg};color:{fg};font-size:{font_size};font-weight:800;white-space:nowrap;'>{label}{suffix}</span>"
    )



def _concept_ticker_watch_state(watchlist_map: dict[str, dict], ticker: str, lang: str) -> tuple[str, str, str]:
    existing = watchlist_map.get(ticker)
    if not existing:
        return (_concept_tr(lang, "not_in_watchlist"), "#f3f4f6", "#6b7280")
    if existing.get("sync_enabled") and existing.get("sync_status") == "success":
        return (_concept_tr(lang, "ready"), "#dcfce7", "#166534")
    if existing.get("sync_enabled"):
        return (_concept_tr(lang, "waiting"), "#fef3c7", "#92400e")
    return (_concept_tr(lang, "in_watchlist"), "#eef8f5", "#0f766e")



def _percent_chip(value: float | None) -> str:
    if value is None:
        return "-"
    if value > 0:
        bg, fg, prefix = "#dcfce7", "#166534", "+"
    elif value < 0:
        bg, fg, prefix = "#fee2e2", "#991b1b", ""
    else:
        bg, fg, prefix = "#f3f4f6", "#374151", ""
    return (
        f"<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-size:12px;font-weight:800;white-space:nowrap;'>{prefix}{value:.1f}%</span>"
    )
