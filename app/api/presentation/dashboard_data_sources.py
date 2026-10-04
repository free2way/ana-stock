"""Named data-sources page context, independent from route and database access."""

from functools import lru_cache
import re
from urllib.parse import quote

from jinja2 import StrictUndefined
from markupsafe import Markup

from app.api.presentation.styles_dashboard import DASHBOARD_DATA_SOURCES_STYLE
from app.api.presentation.templates import template_environment
from app.services.dashboard_insights import _display_time


DATA_SOURCE_TEXT = {
    "en": {
        "title": "Data Sources",
        "hero": "Where This App Gets Data",
        "lead": "Use this page to understand the live provider mix, sync freshness, and the fallback strategy behind market, profile, and concept data.",
        "status_title": "Data Status",
        "status_copy": "Provider freshness and actual sync sources",
        "updated": "Updated",
        "primary_provider": "Primary Provider",
        "primary_provider_help": "The most common provider across the latest sync history.",
        "primary_provider_label": "Primary provider",
        "tracked_providers": "Tracked Providers",
        "tracked_providers_help": "How many different providers appear in current sync records.",
        "tracked_symbols": "Tracked Symbols",
        "tracked_symbols_help": "Symbols with sync metadata stored locally.",
        "synced_symbols": "Synced symbols",
        "concept_freshness": "Concept freshness",
        "freshness": "Freshness",
        "cn_concepts": "CN Concepts",
        "latest_as_of": "Latest as-of date",
        "concepts_across_symbols": "{concepts} concepts across {symbols} symbols",
        "focus": "What To Check First",
        "focus_copy": "Start with provider concentration, concept freshness, and the latest per-symbol sync rows before drilling into details.",
        "top_summary": "Data Strategy and Current Sources",
        "top_summary_copy": "This page should answer two questions quickly: where data is supposed to come from, and what provider the app actually used most recently.",
        "strategy": "Fallback Strategy",
        "strategy_copy": "These are the intended source cascades the app follows when fetching and enriching data.",
        "strategy_prices": "Price History Path",
        "strategy_profiles": "Profile Path",
        "strategy_concepts": "Concept Path",
        "strategy_supplemental": "Supplemental Sources",
        "open_jobs": "Open Task Center",
        "open_workspace": "Back to workspace",
        "provider_rows": "Latest provider mix",
        "current_mix": "Current provider mix",
        "recent_sync": "Latest sync rows",
        "per_symbol_sync_source": "Recent Per-Symbol Sync State",
        "stocks": "Stocks",
        "no_provider_usage": "No provider usage yet",
        "no_sync_history": "No sync history yet",
    },
    "zh": {
        "title": "数据来源",
        "hero": "这个应用的数据来自哪里",
        "lead": "这个页面用来解释当前数据源分布、同步新鲜度，以及行情、资料、概念数据背后的回退策略。",
        "status_title": "数据状态",
        "status_copy": "数据源新鲜度与实际同步来源",
        "updated": "最近更新",
        "primary_provider": "主要数据源",
        "primary_provider_help": "最近同步记录里占比最高的数据源。",
        "primary_provider_label": "主要数据源",
        "tracked_providers": "已跟踪数据源",
        "tracked_providers_help": "当前同步记录里出现过的不同数据源数量。",
        "tracked_symbols": "已跟踪股票",
        "tracked_symbols_help": "本地数据库中保存了同步元数据的股票数量。",
        "synced_symbols": "已同步股票数",
        "concept_freshness": "概念数据新鲜度",
        "freshness": "新鲜度",
        "cn_concepts": "A股概念",
        "latest_as_of": "最新日期",
        "concepts_across_symbols": "{concepts} 个概念，覆盖 {symbols} 只股票",
        "focus": "先看什么",
        "focus_copy": "先看数据源集中度、概念数据日期，再看最近几条逐股同步状态，确认整体是否健康。",
        "top_summary": "数据策略与当前来源",
        "top_summary_copy": "这页应该先回答两个问题：系统理论上该从哪里取数，以及最近一次实际上用了哪个数据源。",
        "strategy": "回退策略",
        "strategy_copy": "这里展示应用在拉取和补全数据时预期遵循的数据源路径。",
        "strategy_prices": "行情路径",
        "strategy_profiles": "资料路径",
        "strategy_concepts": "概念路径",
        "strategy_supplemental": "补充数据源",
        "open_jobs": "打开任务中心",
        "open_workspace": "返回工作台",
        "provider_rows": "最近数据源分布",
        "current_mix": "当前数据源分布",
        "recent_sync": "最近同步记录",
        "per_symbol_sync_source": "逐股同步来源与最新状态",
        "stocks": "股票数",
        "no_provider_usage": "暂无数据源使用记录",
        "no_sync_history": "暂无同步记录",
    },
}


def provider_strategy_view(lang: str) -> dict:
    if lang == "zh":
        return {
            "title": "Provider 策略",
            "copy": "先看系统如何自动选源，再看最近一次实际上用了哪个数据源。",
            "price_auto": "价格数据 `auto`：A 股优先 TuShare，其他市场优先 yfinance。",
            "price_openbb": "价格数据 `openbb`：走 OpenBB 包装层，并保留现有 fallback 能力。",
            "fund_auto": "基本面 `auto`：A 股走 TuShare，美股/港股走 OpenBB 或 yfinance fundamentals。",
            "concept_auto": "概念数据 `auto`：当前 A 股概念映射统一走 TuShare。",
            "execution": "执行与实时：后续会放到 `execution / realtime` 层，而不是混进研究数据层。",
            "ops_title": "当前 provider 口径",
            "ops_copy": "任务配置里现在推荐优先用 `auto` 或按市场选择默认 provider。",
        }
    return {
        "title": "Provider Strategy",
        "copy": "Check how the app chooses providers automatically first, then compare that with the provider actually used most recently.",
        "price_auto": "Price `auto`: CN prefers TuShare, while other markets default to yfinance.",
        "price_openbb": "Price `openbb`: goes through the OpenBB wrapper and keeps the existing fallback behavior.",
        "fund_auto": "Fundamentals `auto`: CN uses TuShare, while US/HK uses OpenBB or yfinance fundamentals.",
        "concept_auto": "Concept `auto`: current CN concept mapping is standardized on TuShare.",
        "execution": "Execution and realtime are reserved for the future `execution / realtime` layer instead of the research data layer.",
        "ops_title": "Current provider policy",
        "ops_copy": "Job configuration now prefers `auto` or a market-aware default provider.",
    }


@lru_cache(maxsize=1)
def _environment():
    return template_environment().overlay(undefined=StrictUndefined)


def _status_class(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "idle").lower()).strip("-")
    return normalized or "idle"


def render_data_sources_page(*, summary: dict, lang: str, lookback_runs: int, nav_html: str) -> str:
    """Render the data-sources page while autoescaping all source-derived values."""
    lang = "zh" if lang == "zh" else "en"
    labels = DATA_SOURCE_TEXT[lang]
    data_sources = summary["data_sources"]
    sync_states = summary["sync_states"]
    concept_data = data_sources["concept_data"] or {}
    synced_count = len(sync_states)
    provider_count = len(data_sources["current_provider_breakdown"])
    primary_provider = data_sources["primary_provider"] or "-"
    concept_freshness = concept_data.get("freshness") or "-"
    metrics = [
        (labels["primary_provider"], primary_provider, labels["primary_provider_help"]),
        (labels["tracked_providers"], provider_count, labels["tracked_providers_help"]),
        (labels["tracked_symbols"], synced_count, labels["tracked_symbols_help"]),
        (labels["concept_freshness"], concept_freshness,
         f"{labels['latest_as_of']}: {concept_data.get('latest_as_of_date') or '-'}"),
    ]
    sync_rows = []
    for item in sync_states[:8]:
        ticker = str(item.get("ticker") or "")
        sync_rows.append({
            **item,
            "ticker": ticker,
            "ticker_path": quote(ticker, safe="._-"),
            "display_name": item.get("name") or ticker,
            "display_provider": item.get("provider") or "-",
            "display_message": item.get("message") or "-",
            "display_date": item.get("last_synced_date") or "-",
            "display_status": item.get("status") or "-",
            "status_class": _status_class(item.get("status")),
        })
    return _environment().get_template("dashboard/data_sources.html").render(
        lang=lang,
        lookback_runs=lookback_runs,
        labels=labels,
        style=Markup(DASHBOARD_DATA_SOURCES_STYLE),
        nav_html=Markup(nav_html),
        generated_at=_display_time(summary.get("generated_at"), with_tz=True),
        primary_provider=primary_provider,
        synced_count=synced_count,
        provider_count=provider_count,
        concept_data=concept_data,
        concept_freshness=concept_freshness,
        metrics=metrics,
        strategies={
            "historical": data_sources["historical_price_strategy"],
            "profiles": data_sources["symbol_profile_strategy"],
            "concepts": data_sources["concept_strategy"],
            "supplemental": data_sources.get("supplemental_source_strategy", []),
        },
        provider_strategy=provider_strategy_view(lang),
        provider_rows=data_sources["current_provider_breakdown"][:6],
        sync_rows=sync_rows,
    )
