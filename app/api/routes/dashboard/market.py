"""Market workspace, heatmap and concept-board routes."""

import csv

import html

from io import StringIO

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from fastapi.responses import HTMLResponse, Response

from sqlalchemy.orm import Session

from app.api.rendering import mini_trend_bars as _mini_trend_bars

from app.api.presentation.i18n import t
from app.api.presentation.dashboard_legacy import render_dashboard_legacy_page
from app.api.presentation.dashboard_market import render_dashboard_market_page
from app.api.presentation.dashboard_heatmap import render_dashboard_heatmap_page

from app.api.presentation.styles_dashboard import (
    CONCEPT_TRACKER_SORT_LINK_STYLE,
)

from app.services.execution_tag_filters import (
    matches_execution_tag_filter as _matches_execution_tag_filter,
    excludes_execution_tag_filter as _excludes_execution_tag_filter,
)
from app.services.market_pulse import (
    MARKET_PULSE_SOFT_RISK_TAGS,
    filter_market_pulse_signals,
    summarize_market_pulse_signals,
)
from app.services.market_heatmap import (
    filter_market_heatmap_rows,
    summarize_heatmap_execution_risks,
)

from app.core.db import get_db_session

from app.services.auth import is_authenticated, login_redirect

from app.services.model_signal_summary import build_signal_label

from app.services.repository import (
    PredictionRepository,
    WorkspaceSnapshotRepository,
)


from app.services.workspace_nav import render_workspace_nav_html

from app.services.workspace_snapshots import (
    SNAPSHOT_CONTINUOUS_LEADERS,
    SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
    load_latest_workspace_snapshot,
)


from app.api.routes.dashboard._common import (
    _clamp_lookback_runs,
    _compact_label,
    _concept_slug,
    _concept_tracker_for_summary,
    _concept_tr,
    _dashboard_home_signal,
    _dt,
    _load_summary,
    _lookback_pills,
    _percent_chip,
    _sparkline_svg,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _recent_market_heat_history(db: Session, *, limit: int = 6) -> dict[str, list[int]]:
    snapshots = WorkspaceSnapshotRepository(db).list_snapshots(
        SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
        limit=max(limit * 3, limit),
    )
    points: list[dict[str, int]] = []
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") or {}
        distribution = payload.get("market_distribution") or []
        if not isinstance(distribution, list) or not distribution:
            continue
        point = {
            str(item.get("market") or "").upper(): int(item.get("count") or 0)
            for item in distribution
            if str(item.get("market") or "").strip()
        }
        if point:
            points.append(point)
    points = points[-limit:]
    return {
        market: [int(point.get(market, 0)) for point in points]
        for market in ("CN", "US")
    }



def _heatmap_metric_label(lang: str, metric: str) -> str:
    labels = {
        "zh": {
            "model": "模型强度",
            "five_day": "5日强弱",
            "breadth": "上涨广度",
            "buy": "买点密度",
            "flow": "资金流代理",
        },
        "en": {
            "model": "Model strength",
            "five_day": "5D strength",
            "breadth": "Breadth",
            "buy": "Buy density",
            "flow": "Flow proxy",
        },
    }
    normalized = metric if metric in {"model", "five_day", "breadth", "buy", "flow"} else "model"
    return labels["zh" if lang == "zh" else "en"][normalized]



def _ticker_links_html(tickers: list[str], *, lang: str, limit: int = 18) -> str:
    normalized = [str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()]
    if not normalized:
        return "-"
    links = [
        f"<a href='/insights/{ticker}?lang={lang}'>{ticker}</a>"
        for ticker in normalized[:limit]
    ]
    if len(normalized) > limit:
        links.append(f"<span class='muted'>+{len(normalized) - limit}</span>")
    return ", ".join(links)



def _concept_tracker_rows_from_heatmap_snapshot(db: Session) -> list[dict]:
    snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    payload = (snapshot or {}).get("payload") or {}
    rows: list[dict] = []
    for item in payload.get("sector_heatmap") or []:
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        ticker_details = item.get("ticker_details") or []
        tickers = [str(detail.get("ticker") or "").strip().upper() for detail in ticker_details if detail.get("ticker")]
        hits = int(item.get("hits") or len(tickers) or 0)
        rows.append(
            {
                "concept_name": label,
                "concept_code": None,
                "slug": item.get("slug") or _concept_slug(label),
                "hits": hits,
                "previous_hits": 0,
                "delta_hits": 0,
                "streak": 1 if hits else 0,
                "history": [hits],
                "avg_move_5d": item.get("avg_move_5d"),
                "breadth_pct": item.get("breadth_pct"),
                "buy_signal_count": int(item.get("buy_signal_count") or 0),
                "max_signal_strength": int(item.get("max_signal_strength") or 0),
                "execution_tags": item.get("execution_tags") or [],
                "avg_score": float(item.get("avg_score") or 0.0),
                "tickers": tickers,
                "ticker_details": ticker_details,
            }
        )
    return rows



def _load_concept_tracker_rows(db: Session, *, lookback_runs: int) -> list[dict]:
    rows = _concept_tracker_rows_from_heatmap_snapshot(db)
    if rows:
        return rows
    summary = _load_summary(db, lookback_runs=lookback_runs)
    rows = list((summary.get("market_context") or {}).get("concept_tracker") or [])
    if rows:
        return rows
    return _concept_tracker_for_summary(db, lookback_runs=lookback_runs)



def _breadth_chip(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 65:
        bg, fg = "#dcfce7", "#166534"
    elif value >= 50:
        bg, fg = "#fef3c7", "#92400e"
    else:
        bg, fg = "#fee2e2", "#991b1b"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-size:12px;font-weight:800;white-space:nowrap;'>{value:.0f}% up</span>"
    )



def _market_concept_sort_key(sort_by: str, item: dict) -> tuple:
    if sort_by == "concept":
        return (str(item.get("concept_name") or "").lower(),)
    if sort_by == "hits":
        return (int(item.get("hits") or 0), float(item.get("avg_score") or 0.0))
    if sort_by == "delta":
        return (int(item.get("delta_hits") or 0), int(item.get("hits") or 0))
    if sort_by == "streak":
        return (int(item.get("streak") or 0), int(item.get("hits") or 0))
    if sort_by == "five_day":
        return (float(item.get("avg_move_5d") or -9999.0), float(item.get("avg_score") or 0.0))
    if sort_by == "breadth":
        return (float(item.get("breadth_pct") or -1.0), float(item.get("avg_score") or 0.0))
    if sort_by == "buy_count":
        return (int(item.get("buy_signal_count") or 0), int(item.get("max_signal_strength") or 0))
    if sort_by == "max_strength":
        return (int(item.get("max_signal_strength") or 0), int(item.get("buy_signal_count") or 0))
    if sort_by == "score":
        return (float(item.get("avg_score") or 0.0), int(item.get("hits") or 0))
    return (int(item.get("delta_hits") or 0), int(item.get("hits") or 0), str(item.get("concept_name") or "").lower())



@router.get("/market", response_class=HTMLResponse)
def dashboard_market_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    heatmap_sort: str = "hits",
    market_filter: str = "CN",
    kpi_focus: str = "ALL",
    mode: str = "monitor",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    market_filter = str(market_filter or "CN").strip().upper()
    if market_filter not in {"ALL", "CN", "US"}:
        market_filter = "CN"
    kpi_focus = str(kpi_focus or "ALL").strip().lower()
    if kpi_focus not in {"all", "focused", "buy", "risk", "boards"}:
        kpi_focus = "all"
    market_mode = str(mode or "monitor").strip().lower()
    if market_mode not in {"premarket", "monitor", "postmarket"}:
        market_mode = "monitor"
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    signal_repo = PredictionRepository(db)
    latest_signals = signal_repo.list_latest_signal_decisions(
        limit=40,
        market=None if market_filter == "ALL" else market_filter,
    )
    buy_hit_counts: dict[str, int] = {}
    if min_buy_signal_count > 0:
        buy_hit_counts = PredictionRepository(db).count_recent_signal_hits(
            tickers=[str(item.get("ticker") or "").strip().upper() for item in latest_signals if item.get("ticker")],
            signal_label="BUY",
            limit_runs=lookback_runs,
        )
    filtered_signals = filter_market_pulse_signals(
        latest_signals,
        buy_hit_counts=buy_hit_counts,
        market_filter=market_filter,
        signal_filter=signal_filter,
        min_signal_strength=min_signal_strength,
        min_buy_signal_count=min_buy_signal_count,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        lang=lang,
    )
    signal_summary = summarize_market_pulse_signals(filtered_signals, lang=lang)
    tagged_names = signal_summary["tagged_names"]
    risk_examples = signal_summary["risk_examples"]
    risk_top_tags = signal_summary["risk_top_tags"]
    signal_bucket_counts = signal_summary["signal_bucket_counts"]
    market_tone = signal_summary["market_tone"]

    market_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_WORKSPACE)
    market_snapshot_payload = (market_snapshot or {}).get("payload") if isinstance(market_snapshot, dict) else None
    mode_snapshot_type = {
        "premarket": SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
        "monitor": SNAPSHOT_MARKET_WORKSPACE_MONITOR,
        "postmarket": SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    }[market_mode]
    market_mode_snapshot = load_latest_workspace_snapshot(db, mode_snapshot_type)
    market_mode_payload = (market_mode_snapshot or {}).get("payload") if isinstance(market_mode_snapshot, dict) else None
    snapshot_boards = (
        (market_mode_payload or {}).get("boards")
        if isinstance(market_mode_payload, dict)
        else None
    ) or ((market_snapshot_payload or {}).get("rows") if isinstance(market_snapshot_payload, dict) else None)
    snapshot_ready = isinstance(snapshot_boards, list) and bool(snapshot_boards)
    if not snapshot_ready:
        snapshot_boards = []
    all_snapshot_boards = list(snapshot_boards)
    snapshot_boards = [
        board
        for board in snapshot_boards
        if market_filter == "ALL" or str(board.get("market") or "").upper() == market_filter
    ]
    heatmap_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    heatmap_payload = (heatmap_snapshot or {}).get("payload") if isinstance(heatmap_snapshot, dict) else None
    heatmap_preview_source = list((heatmap_payload or {}).get("sector_heatmap") or []) if isinstance(heatmap_payload, dict) else []
    if market_filter != "ALL":
        heatmap_preview_source = [
            item for item in heatmap_preview_source if str(item.get("market") or "").upper() == market_filter
        ]
    if signal_filter == "BUY":
        heatmap_preview_source = [item for item in heatmap_preview_source if int(item.get("buy_signal_count") or 0) > 0]
    elif signal_filter != "ALL":
        heatmap_preview_source = [
            item
            for item in heatmap_preview_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        heatmap_preview_source = [
            item for item in heatmap_preview_source if int(item.get("max_signal_strength") or 0) >= min_signal_strength
        ]
    if min_buy_signal_count > 0:
        heatmap_preview_source = [
            item for item in heatmap_preview_source if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        heatmap_preview_source = [
            item for item in heatmap_preview_source if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        heatmap_preview_source = [
            item
            for item in heatmap_preview_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    heatmap_preview_rows = heatmap_preview_source[:3]
    heatmap_updated_at = (
        (heatmap_payload or {}).get("updated_at")
        or (market_snapshot_payload or {}).get("updated_at")
        or (market_mode_snapshot or {}).get("created_at")
    )
    concept_preview_source = _load_concept_tracker_rows(db, lookback_runs=lookback_runs)
    if min_buy_signal_count > 0:
        concept_preview_source = [
            item for item in concept_preview_source if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    concept_preview_rows = sorted(
        concept_preview_source,
        key=lambda item: (
            int(item.get("delta_hits") or 0),
            int(item.get("streak") or 0),
            int(item.get("hits") or 0),
            float(item.get("avg_score") or 0.0),
        ),
        reverse=True,
    )[:3]
    continuous_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_CONTINUOUS_LEADERS)
    continuous_payload = (continuous_snapshot or {}).get("payload") if isinstance(continuous_snapshot, dict) else None
    continuous_preview_source = list((continuous_payload or {}).get("rows") or []) if isinstance(continuous_payload, dict) else []
    if market_filter != "ALL":
        continuous_preview_source = [
            item for item in continuous_preview_source if str(item.get("market") or "").upper() == market_filter
        ]
    if signal_filter != "ALL":
        continuous_preview_source = [
            item for item in continuous_preview_source if str(item.get("signal_label") or "").strip().upper() == signal_filter
        ]
    if min_signal_strength > 0:
        continuous_preview_source = [
            item for item in continuous_preview_source if int(item.get("signal_strength") or 0) >= min_signal_strength
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        continuous_preview_source = [
            item for item in continuous_preview_source if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        continuous_preview_source = [
            item
            for item in continuous_preview_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    continuous_preview_rows = sorted(
        continuous_preview_source,
        key=lambda item: (
            int(item.get("hits") or 0),
            float(item.get("score") or 0.0),
            str(item.get("ticker") or ""),
        ),
        reverse=True,
    )[:3]
    heatmap_preview_html = "".join(
        "<a class='market-mini-row' href='/dashboard/market/heatmap?{query}'>"
        "<div><strong>{label}</strong><span>{meta}</span></div>"
        "<b>{hits}</b>"
        "</a>".format(
            query=urlencode(
                {
                    "lookback_runs": lookback_runs,
                    "lang": lang,
                    "heatmap_metric": "flow" if market_filter == "US" else "model",
                    "market_filter": market_filter,
                    "signal_filter": signal_filter,
                    "min_signal_strength": min_signal_strength,
                    "min_buy_signal_count": min_buy_signal_count,
                    "execution_tag_filter": execution_tag_filter,
                    "exclude_execution_tag_filter": exclude_execution_tag_filter,
                }
            ),
            label=html.escape(
                _compact_label(
                    f"{str(item.get('market') or '').upper()} · {item.get('label')}"
                    if market_filter == "ALL"
                    else item.get("label") or "-",
                    24,
                )
            ),
            meta=html.escape(
                f"{t(lang, '买点', 'Buy')} {int(item.get('buy_signal_count') or 0)} · {t(lang, '强度', 'Strength')} {int(item.get('max_signal_strength') or 0)}"
            ),
            hits=int(item.get("hits") or 0),
        )
        for item in heatmap_preview_rows
    ) or f"<div class='empty'>{t(lang, '热力图仍在后台预计算', 'Heatmap is still being precomputed')}</div>"
    concept_preview_html = "".join(
        "<a class='market-mini-row' href='/dashboard/concepts/{slug}?{query}'>"
        "<div><strong>{label}</strong><span>{meta}</span></div>"
        "<b>{delta}</b>"
        "</a>".format(
            slug=item.get("slug") or _concept_slug(str(item.get("concept_name") or "")),
            query=urlencode(
                {
                    "lookback_runs": lookback_runs,
                    "lang": lang,
                    "market_filter": market_filter,
                    "signal_filter": signal_filter,
                    "min_signal_strength": min_signal_strength,
                    "min_buy_signal_count": min_buy_signal_count,
                    "execution_tag_filter": execution_tag_filter,
                    "exclude_execution_tag_filter": exclude_execution_tag_filter,
                }
            ),
            label=html.escape(_compact_label(item.get("concept_name") or "-", 24)),
            meta=html.escape(
                f"{t(lang, '命中', 'Hits')} {int(item.get('hits') or 0)} · {t(lang, '连续', 'Streak')} {int(item.get('streak') or 0)}"
            ),
            delta=f"{'+' if int(item.get('delta_hits') or 0) > 0 else ''}{int(item.get('delta_hits') or 0)}",
        )
        for item in concept_preview_rows
    ) or f"<div class='empty'>{t(lang, '暂无概念追踪数据', 'No concept tracking data yet')}</div>"
    continuous_preview_html = "".join(
        "<a class='market-mini-row' href='/dashboard/continuous-leaders?{query}'>"
        "<div><strong>{label}</strong><span>{meta}</span></div>"
        "<b>{hits}</b>"
        "</a>".format(
            query=urlencode(
                {
                    "lang": lang,
                    "lookback_runs": lookback_runs,
                    "continuous_market": market_filter if market_filter != "ALL" else "US",
                }
            ),
            label=html.escape(_compact_label(f"{item.get('ticker') or '-'} · {item.get('name') or '-'}", 30)),
            meta=html.escape(
                f"{t(lang, '连续命中', 'Hits')} {int(item.get('hits') or 0)} · {t(lang, '信号', 'Signal')} {item.get('signal_label') or '-'}"
            ),
            hits=int(item.get("hits") or 0),
        )
        for item in continuous_preview_rows
    ) or f"<div class='empty'>{t(lang, '暂无美股连续强势数据', 'No U.S. continuous leaders yet')}</div>"
    mode_meta = {
        "premarket": {
            "label": "盘前准备" if lang == "zh" else "Premarket",
            "eyebrow": "盘前工作台" if lang == "zh" else "Premarket Desk",
            "headline": "先定今天能不能出手" if lang == "zh" else "Decide whether risk can be added",
            "help": "先看昨日主线、可执行候选和不能追的风险票，避免开盘后被噪音带节奏。" if lang == "zh" else "Start with yesterday's leadership, actionable names, and no-chase risks before the open.",
            "steps": (
                ("主线确认" if lang == "zh" else "Leadership", "昨日强弱与热力图" if lang == "zh" else "Leadership and heatmap"),
                ("开盘候选" if lang == "zh" else "Open Setups", "行动榜单和买点候选" if lang == "zh" else "Action boards and buy setups"),
                ("放弃清单" if lang == "zh" else "No-Chase List", "跳空、流动性和事件风险" if lang == "zh" else "Gap, liquidity, and event risks"),
            ),
        },
        "monitor": {
            "label": "盘中脉冲" if lang == "zh" else "Monitor",
            "eyebrow": "市场脉冲" if lang == "zh" else "Market Pulse",
            "headline": "跟踪主线有没有延续" if lang == "zh" else "Track whether leadership persists",
            "help": "盘中只看少量关键指标：市场热度、买点数量、强风险标签和行动榜变化。" if lang == "zh" else "During the session, keep the view focused on heat, buy signals, material risks, and board changes.",
            "steps": (
                ("热力确认" if lang == "zh" else "Heat Check", "板块/行业强弱" if lang == "zh" else "Sector and industry heat"),
                ("个股下钻" if lang == "zh" else "Name Drilldown", "焦点候选和连续强势" if lang == "zh" else "Focused names and persistence"),
                ("执行风控" if lang == "zh" else "Execution Risk", "行动榜和风险标签" if lang == "zh" else "Action boards and risk tags"),
            ),
        },
        "postmarket": {
            "label": "盘后复盘" if lang == "zh" else "Postmarket",
            "eyebrow": "盘后复盘" if lang == "zh" else "Postmarket Review",
            "headline": "把今天的市场变成明天计划" if lang == "zh" else "Turn today's market into tomorrow's plan",
            "help": "收盘后重点看主线沉淀、模型共振、风险退潮和明天候选，而不是继续刷新实时噪音。" if lang == "zh" else "After the close, focus on durable themes, model confluence, fading risks, and tomorrow's candidate list.",
            "steps": (
                ("主线沉淀" if lang == "zh" else "Theme Persistence", "热力图与概念连续性" if lang == "zh" else "Heatmap and theme persistence"),
                ("明日候选" if lang == "zh" else "Tomorrow Candidates", "预计算行动榜" if lang == "zh" else "Precomputed action boards"),
                ("复盘归因" if lang == "zh" else "Review Attribution", "风险标签和信号分布" if lang == "zh" else "Risk tags and signal distribution"),
            ),
        },
    }
    active_mode_meta = mode_meta[market_mode]
    market_scope_title = {
        "ALL": "全部市场" if lang == "zh" else "All Markets",
        "CN": "A股" if lang == "zh" else "A-Shares",
        "US": "美股" if lang == "zh" else "U.S. Stocks",
    }[market_filter]
    market_scope_help = (
        "口径说明：这里的热度统一表示 0-100 的平均信号强度。市场概览使用最新一批候选股的 `signal_strength` 均值。"
        if lang == "zh"
        else "Methodology: heat is a unified 0-100 average signal-strength score. On Market Pulse it is the mean `signal_strength` of the latest candidates."
    )
    market_scope_signal_rows = {
        scope_market: signal_repo.list_latest_signal_decisions(limit=24, market=scope_market)
        for scope_market in ("CN", "US")
    }
    market_scope_history = _recent_market_heat_history(db, limit=6)
    market_scope_board_counts = {scope_market: 0 for scope_market in ("CN", "US")}
    for board in all_snapshot_boards:
        scope_market = str(board.get("market") or "").upper()
        if scope_market in market_scope_board_counts:
            market_scope_board_counts[scope_market] += len(board.get("rows") or [])
    market_scope_cards_html = ""
    for scope_market, scope_label in (("CN", "A股" if lang == "zh" else "A-Shares"), ("US", "美股" if lang == "zh" else "U.S. Stocks")):
        scope_rows = market_scope_signal_rows.get(scope_market) or []
        scope_history = market_scope_history.get(scope_market) or []
        scope_buy_count = sum(
            1
            for item in scope_rows
            if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "BUY"
        )
        scope_risk_count = 0
        scope_strength_total = 0
        for item in scope_rows:
            tags = [
                str(tag).strip()
                for tag in (item.get("risk_flags") or item.get("execution_tags") or [])
                if str(tag).strip() and str(tag).strip() not in MARKET_PULSE_SOFT_RISK_TAGS
            ]
            if tags:
                scope_risk_count += 1
            scope_strength_total += int(item.get("signal_strength") or 0)
        scope_heat = round(scope_strength_total / max(len(scope_rows), 1), 1) if scope_rows else 0.0
        latest_scope_date = next((str(item.get("trade_date") or "").strip() for item in scope_rows if item.get("trade_date")), "-")
        scope_delta = (scope_history[-1] - scope_history[0]) if len(scope_history) >= 2 else 0
        scope_delta_tone = "up" if scope_delta > 0 else "down" if scope_delta < 0 else "flat"
        scope_delta_label = (
            (f"+{scope_delta} {t(lang, '升温', 'warming')}" if scope_delta > 0 else f"{scope_delta} {t(lang, '降温', 'cooling')}")
            if scope_delta != 0
            else (t(lang, "持平", "Flat"))
        )
        scope_href = f"/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': scope_market, 'kpi_focus': kpi_focus, 'mode': market_mode, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}"
        market_scope_cards_html += (
            f"<a class='market-scope-card{' active' if market_filter == scope_market else ''}' href='{scope_href}'>"
            f"<div class='market-scope-head'><strong>{scope_label}</strong><span>{latest_scope_date}</span></div>"
            f"<div class='market-scope-headline'><span class='market-scope-chip {scope_delta_tone}'>{scope_delta_label}</span></div>"
            f"<div class='market-scope-stats'>"
            f"<div><b>{len(scope_rows)}</b><span>{t(lang, '命中', 'Hits')}</span></div>"
            f"<div><b>{scope_buy_count}</b><span>{t(lang, '买点', 'Buy')}</span></div>"
            f"<div><b>{scope_heat:.1f}</b><span>{t(lang, '热度', 'Heat')}</span></div>"
            f"</div>"
            f"<div class='market-scope-trend-wrap'><span>{t(lang, '近6次快照热度', 'Last 6 snapshots')}</span>{_mini_trend_bars(scope_history, lang=lang)}</div>"
            f"<div class='market-scope-meta'>{t(lang, '行动榜', 'Boards')} {market_scope_board_counts.get(scope_market, 0)} · {t(lang, '风险', 'Risk')} {scope_risk_count}</div>"
            "</a>"
        )
    top_signal_rows = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='subtle'>{html.escape(str(item.get('trade_date') or '-'))} · {html.escape(str(item.get('market') or '-'))} · {int(item.get('snapshot_buy_hits') or 0)} {t(lang, '次买点', 'buy hits')}</div><div class='subtle'>{html.escape(_compact_label(item.get('reason_summary') or item.get('name') or '-', 72))}</div></div>"
        f"<div class='row-right'><span class='signal {_dashboard_home_signal(item.get('score'), lang)[1]}'>{html.escape(str(item.get('signal_label') or _dashboard_home_signal(item.get('score'), lang)[0]))}</span><div class='mini-metric'>{int(item.get('signal_strength') or 0)}</div></div>"
        "</article>"
        for item in filtered_signals[:5]
    ) or f"<div class='empty'>{t(lang, '暂无符合条件的候选', 'No candidates match the current focus')}</div>"

    lookback_pills = _lookback_pills("/dashboard/market", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "market_filter": market_filter, "kpi_focus": kpi_focus, "mode": market_mode, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    market_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': market, 'kpi_focus': kpi_focus, 'mode': market_mode, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if market_filter == market else ''}'>{label}</a>"
        for market, label in (
            ("ALL", "All Markets" if lang == "en" else "全部市场"),
            ("CN", "A-Shares" if lang == "en" else "A股"),
            ("US", "U.S." if lang == "en" else "美股"),
        )
    )
    mode_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': market_filter, 'kpi_focus': kpi_focus, 'mode': mode_key, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='mode-pill{' active' if market_mode == mode_key else ''}'><b>{meta['label']}</b><span>{meta['headline']}</span></a>"
        for mode_key, meta in mode_meta.items()
    )
    mode_steps_html = "".join(
        f"<div class='mode-step'><b>{html.escape(title)}</b><span>{html.escape(text)}</span></div>"
        for title, text in active_mode_meta["steps"]
    )
    signal_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': market_filter, 'kpi_focus': kpi_focus, 'mode': market_mode, 'signal_filter': signal_mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == signal_mode else ''}'>{label}</a>"
        for signal_mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{html.escape(str(tag))} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['label']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    board_count = sum(len(board.get("rows") or []) for board in snapshot_boards)
    base_market_params = {
        "lang": lang,
        "lookback_runs": lookback_runs,
        "heatmap_sort": heatmap_sort,
        "market_filter": market_filter,
        "mode": market_mode,
        "signal_filter": signal_filter,
        "min_signal_strength": min_signal_strength,
        "min_buy_signal_count": min_buy_signal_count,
        "execution_tag_filter": execution_tag_filter,
        "exclude_execution_tag_filter": exclude_execution_tag_filter,
    }

    def _market_kpi_href(focus_key: str) -> str:
        next_focus = "all" if kpi_focus == focus_key else focus_key
        return f"/dashboard/market?{urlencode({**base_market_params, 'kpi_focus': next_focus})}#market-kpi-detail"

    market_kpi_links = {
        "focused": _market_kpi_href("focused"),
        "buy": _market_kpi_href("buy"),
        "risk": _market_kpi_href("risk"),
        "boards": _market_kpi_href("boards"),
    }
    active_focus_classes = {
        key: " active" if kpi_focus == key else ""
        for key in ("focused", "buy", "risk", "boards")
    }

    focused_rows = filtered_signals
    buy_rows = [
        item
        for item in filtered_signals
        if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "BUY"
    ]
    risk_rows = [
        item
        for item in filtered_signals
        if [str(tag).strip() for tag in (item.get("market_risk_tags") or []) if str(tag).strip()]
    ]
    board_rows_flat: list[dict] = []
    for board in snapshot_boards:
        board_title = board.get("title_zh") if lang == "zh" else board.get("title_en")
        for row in board.get("rows") or []:
            board_rows_flat.append(
                {
                    **row,
                    "board_title": board_title or board.get("key") or "-",
                }
            )
    board_rows_flat.sort(
        key=lambda item: (
            -(float(item.get("snapshot_score") or 0.0)),
            -(float(item.get("trend_score") or 0.0)),
            str(item.get("ticker") or ""),
        )
    )

    def _signal_detail_rows(rows: list[dict]) -> str:
        return "".join(
            "<tr>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('signal_label') or build_signal_label(item.get('score'), lang=lang) or '-'))}</td>"
            f"<td>{int(item.get('signal_strength') or 0)}</td>"
            f"<td>{int(item.get('snapshot_buy_hits') or 0)}</td>"
            f"<td>{' · '.join(str(tag).strip() for tag in ((item.get('market_risk_tags') or []) if kpi_focus == 'risk' else (item.get('risk_flags') or item.get('execution_tags') or [])) if str(tag).strip()) or '-'}</td>"
            f"<td>{html.escape(_compact_label(item.get('reason_summary') or item.get('summary_text') or item.get('name') or '-', 92))}</td>"
            "</tr>"
            for item in rows[:24]
        ) or f"<tr><td colspan='7'>{t(lang, '当前没有符合条件的记录。', 'No rows match the current focus.')}</td></tr>"

    def _board_detail_rows(rows: list[dict]) -> str:
        return "".join(
            "<tr>"
            f"<td>{html.escape(str(item.get('board_title') or '-'))}</td>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('action_label') or item.get('action_summary') or '-'))}</td>"
            f"<td>{float(item.get('snapshot_score') or 0.0):.2f}</td>"
            f"<td>{float(item.get('trend_score') or 0.0):.1f}</td>"
            f"<td>{float(item.get('volume_ratio') or 0.0):.2f}</td>"
            f"<td>{html.escape(_compact_label(item.get('selection_reason') or '-', 92))}</td>"
            "</tr>"
            for item in rows[:24]
        ) or f"<tr><td colspan='7'>{t(lang, '当前没有行动榜候选。', 'No board candidates are available right now.')}</td></tr>"

    kpi_focus_meta = {
        "focused": {
            "title": "焦点候选明细" if lang == "zh" else "Focused Names Detail",
            "subtitle": "当前筛选后的市场焦点候选。" if lang == "zh" else "Market candidates remaining after the current filters.",
        },
        "buy": {
            "title": "买点信号明细" if lang == "zh" else "Buy Signal Detail",
            "subtitle": "当前筛选条件下，信号标签为 BUY 的候选。" if lang == "zh" else "Candidates whose signal label is BUY under the current filters.",
        },
        "risk": {
            "title": "强风险标签明细" if lang == "zh" else "Material Risk Detail",
            "subtitle": "当前筛选后仍带较强风险提醒的候选，不再把常见软提醒全部算进来。" if lang == "zh" else "Candidates that still carry stronger risk tags after the current filters, excluding common soft warnings.",
        },
        "boards": {
            "title": "行动榜候选明细" if lang == "zh" else "Action Board Detail",
            "subtitle": "来自今日行动榜单的预计算候选。" if lang == "zh" else "Precomputed candidates from today's action boards.",
        },
    }
    kpi_detail_section = ""
    if kpi_focus != "all":
        if kpi_focus == "boards":
            detail_table = (
                "<table><thead><tr>"
                f"<th>{t(lang, '榜单', 'Board')}</th>"
                f"<th>{t(lang, '代码 / 名称', 'Ticker / Name')}</th>"
                f"<th>{t(lang, '动作', 'Action')}</th>"
                f"<th>{t(lang, '快照分', 'Snapshot Score')}</th>"
                f"<th>{t(lang, '趋势分', 'Trend Score')}</th>"
                f"<th>{t(lang, '量比', 'Volume Ratio')}</th>"
                f"<th>{t(lang, '原因', 'Reason')}</th>"
                f"</tr></thead><tbody>{_board_detail_rows(board_rows_flat)}</tbody></table>"
            )
        else:
            detail_rows = focused_rows if kpi_focus == "focused" else buy_rows if kpi_focus == "buy" else risk_rows
            detail_table = (
                "<table><thead><tr>"
                f"<th>{t(lang, '代码 / 名称', 'Ticker / Name')}</th>"
                f"<th>{t(lang, '日期', 'Date')}</th>"
                f"<th>{t(lang, '信号', 'Signal')}</th>"
                f"<th>{t(lang, '强度', 'Strength')}</th>"
                f"<th>{t(lang, '窗口 BUY 次数', 'Window BUY Hits')}</th>"
                f"<th>{t(lang, '风险标签', 'Risk Tags')}</th>"
                f"<th>{t(lang, '摘要', 'Summary')}</th>"
                f"</tr></thead><tbody>{_signal_detail_rows(detail_rows)}</tbody></table>"
            )
        meta = kpi_focus_meta[kpi_focus]
        close_link = f"/dashboard/market?{urlencode(base_market_params)}#market-kpi-detail"
        kpi_detail_section = (
            f"<section id='market-kpi-detail' class='card'>"
            f"<div class='compare-row' style='justify-content:space-between;align-items:flex-start;'>"
            f"<div><div class='eyebrow'>{t(lang, '明细展开', 'Expanded Detail')}</div>"
            f"<h2 style='margin:0 0 8px;font-size:24px;'>{meta['title']}</h2>"
            f"<div class='muted'>{meta['subtitle']}</div></div>"
            f"<a class='pill' href='{close_link}'>{t(lang, '收起', 'Collapse')}</a>"
            f"</div><div class='table-wrap' style='margin-top:14px;'>{detail_table}</div></section>"
        )
    return render_dashboard_market_page(
        {
            "lang": lang,
            "lookback_runs": lookback_runs,
            "heatmap_sort": heatmap_sort,
            "market_filter": market_filter,
            "kpi_focus": kpi_focus,
            "market_mode": market_mode,
            "signal_filter": signal_filter,
            "min_signal_strength": min_signal_strength,
            "min_buy_signal_count": min_buy_signal_count,
            "execution_tag_filter": execution_tag_filter,
            "exclude_execution_tag_filter": exclude_execution_tag_filter,
            "nav_html": nav_html,
            "mode_pills": mode_pills,
            "market_pills": market_pills,
            "active_mode_meta": active_mode_meta,
            "market_scope_title": market_scope_title,
            "market_tone": market_tone,
            "mode_steps_html": mode_steps_html,
            "filtered_signal_count": len(filtered_signals),
            "signal_bucket_counts": signal_bucket_counts,
            "tagged_names": tagged_names,
            "board_count": board_count,
            "heatmap_updated_at": heatmap_updated_at,
            "risk_top_tags_html": risk_top_tags_html,
            "risk_examples_html": risk_examples_html,
            "kpi_detail_section": kpi_detail_section,
            "heatmap_preview_html": heatmap_preview_html,
            "continuous_preview_html": continuous_preview_html,
            "concept_preview_html": concept_preview_html,
            "market_kpi_links": market_kpi_links,
            "active_focus_classes": active_focus_classes,
            "market_scope_cards_html": market_scope_cards_html,
            "market_scope_help": market_scope_help,
            "top_signal_rows": top_signal_rows,
            "lookback_pills": lookback_pills,
            "signal_pills": signal_pills,
        }
    )



@router.get("/market/heatmap", response_class=HTMLResponse)
def dashboard_market_heatmap_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    heatmap_sort: str = "hits",
    heatmap_metric: str = "model",
    heatmap_focus: str = "",
    market_filter: str = "CN",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market/heatmap")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    market_filter = str(market_filter or "CN").strip().upper()
    if market_filter not in {"ALL", "CN", "US"}:
        market_filter = "CN"
    signal_filter = signal_filter.upper()
    heatmap_metric = (heatmap_metric or "model").strip().lower()
    if heatmap_metric not in {"model", "five_day", "breadth", "buy", "flow"}:
        heatmap_metric = "model"
    if market_filter == "US" and "heatmap_metric" not in request.query_params:
        heatmap_metric = "flow"
    heatmap_focus = str(heatmap_focus or "").strip()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    heatmap_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    heatmap_payload = (heatmap_snapshot or {}).get("payload") if isinstance(heatmap_snapshot, dict) else None
    heatmap_ready = isinstance(heatmap_payload, dict) and isinstance(heatmap_payload.get("sector_heatmap"), list)
    heatmap_rows = filter_market_heatmap_rows(
        list((heatmap_payload or {}).get("sector_heatmap") or []),
        market_filter=market_filter,
        signal_filter=signal_filter,
        min_signal_strength=min_signal_strength,
        min_buy_signal_count=min_buy_signal_count,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=heatmap_sort,
    )
    risk_summary = summarize_heatmap_execution_risks(heatmap_rows)
    tagged_names = risk_summary["tagged_names"]
    risk_examples = risk_summary["risk_examples"]
    risk_top_tags = risk_summary["risk_top_tags"]
    def _heatmap_metric_value(item: dict) -> float | None:
        if heatmap_metric == "five_day":
            value = item.get("avg_move_5d")
            return None if value is None else float(value)
        if heatmap_metric == "breadth":
            value = item.get("breadth_pct")
            return None if value is None else float(value)
        if heatmap_metric == "buy":
            return float(item.get("buy_signal_count") or 0)
        if heatmap_metric == "flow":
            value = item.get("flow_proxy_score")
            return None if value is None else float(value)
        return max(float(item.get("max_signal_strength") or 0), float(item.get("avg_score") or 0))

    def _heatmap_metric_display(item: dict) -> str:
        value = _heatmap_metric_value(item)
        if value is None:
            return "-"
        if heatmap_metric == "five_day":
            return f"{'+' if value > 0 else ''}{value:.1f}%"
        if heatmap_metric == "breadth":
            return f"{value:.0f}% {t(lang, '涨', 'up')}"
        if heatmap_metric == "buy":
            return f"{int(value)} {t(lang, '买点', 'buy')}"
        if heatmap_metric == "flow":
            return f"{value:.0f}"
        return f"{value:.0f}"

    def _heatmap_weight(item: dict) -> float:
        hits = max(1, int(item.get("hits") or 0))
        buy_count = max(0, int(item.get("buy_signal_count") or 0))
        strength = max(0, int(item.get("max_signal_strength") or 0))
        return float((hits ** 1.18) + buy_count * 0.85 + strength / 22)

    def _heatmap_background(item: dict) -> str:
        value = _heatmap_metric_value(item)
        label = str(item.get("label") or "")
        palette = [
            ((34, 197, 94), (22, 101, 52)),
            ((45, 212, 191), (15, 118, 110)),
            ((96, 165, 250), (30, 64, 175)),
            ((168, 85, 247), (88, 28, 135)),
            ((251, 146, 60), (154, 52, 18)),
            ((244, 114, 182), (157, 23, 77)),
            ((250, 204, 21), (133, 77, 14)),
            ((56, 189, 248), (12, 74, 110)),
            ((129, 140, 248), (67, 56, 202)),
            ((74, 222, 128), (22, 101, 52)),
        ]
        palette_index = sum(ord(char) for char in label) % len(palette)
        primary, deep = palette[palette_index]

        def _category_gradient(level: float) -> str:
            level = max(0.18, min(1.0, level))
            p_alpha = 0.42 + level * 0.54
            d_alpha = 0.70 + level * 0.28
            shadow_alpha = 0.86 + level * 0.12
            return (
                f"linear-gradient(135deg, rgba({primary[0]},{primary[1]},{primary[2]},{p_alpha:.2f}) 0%, "
                f"rgba({deep[0]},{deep[1]},{deep[2]},{d_alpha:.2f}) 58%, "
                f"rgba(2,6,23,{shadow_alpha:.2f}) 100%)"
            )

        if value is None:
            return "linear-gradient(135deg, #64748b 0%, #334155 48%, #111827 100%)"
        if heatmap_metric == "five_day":
            return _category_gradient(0.28 + min(0.72, abs(value) / 18))
        if heatmap_metric == "breadth":
            return _category_gradient(0.22 + min(0.78, max(0.0, value) / 100))
        if heatmap_metric == "buy":
            max_buy = max([int(row.get("buy_signal_count") or 0) for row in heatmap_rows] or [1])
            return _category_gradient(0.25 + min(0.75, value / max(max_buy, 1)))
        if heatmap_metric == "flow":
            flow_values = [float(row.get("flow_proxy_score") or 0.0) for row in heatmap_rows if row.get("flow_proxy_score") is not None]
            min_flow = min(flow_values or [0.0])
            max_flow = max(flow_values or [100.0])
            normalized = (float(value) - min_flow) / max(max_flow - min_flow, 1.0)
            return _category_gradient(0.22 + normalized * 0.78)
        model_values = [
            max(float(row.get("max_signal_strength") or 0), float(row.get("avg_score") or 0))
            for row in heatmap_rows
        ]
        min_model = min(model_values or [0.0])
        max_model = max(model_values or [100.0])
        normalized = (float(value) - min_model) / max(max_model - min_model, 1.0)
        return _category_gradient(0.25 + normalized * 0.75)

    def _split_treemap(items: list[dict], x: float, y: float, width: float, height: float) -> list[dict]:
        if not items:
            return []
        if len(items) == 1:
            return [{**items[0], "_x": x, "_y": y, "_w": width, "_h": height}]
        total = sum(float(item.get("_weight") or 0) for item in items) or float(len(items))
        half = total / 2
        running = 0.0
        split_index = 1
        for index, item in enumerate(items[:-1], start=1):
            next_running = running + float(item.get("_weight") or 0)
            if abs(next_running - half) <= abs(running - half) or index == 1:
                running = next_running
                split_index = index
            else:
                break
        first = items[:split_index]
        second = items[split_index:]
        first_total = sum(float(item.get("_weight") or 0) for item in first) or 1.0
        ratio = max(0.08, min(0.92, first_total / total))
        if width >= height:
            first_width = width * ratio
            return _split_treemap(first, x, y, first_width, height) + _split_treemap(second, x + first_width, y, width - first_width, height)
        first_height = height * ratio
        return _split_treemap(first, x, y, width, first_height) + _split_treemap(second, x, y + first_height, width, height - first_height)

    for item in heatmap_rows:
        avg_move = item.get("avg_move_5d")
        breadth = item.get("breadth_pct")
        item["display_label"] = (
            f"{str(item.get('market') or '').upper()} · {item.get('label') or '-'}"
            if market_filter == "ALL"
            else item.get("label") or "-"
        )
        item["avg_move_5d_display"] = "-" if avg_move is None else f"{'+' if float(avg_move) > 0 else ''}{float(avg_move):.1f}%"
        item["breadth_display"] = "-" if breadth is None else f"{float(breadth):.0f}% {t(lang, '涨', 'up')}"
        turnover_ratio = item.get("turnover_ratio_20d")
        signed_turnover = item.get("signed_turnover_pct")
        item["flow_ratio_display"] = "-" if turnover_ratio is None else f"{float(turnover_ratio):.2f}x"
        item["flow_signed_display"] = "-" if signed_turnover is None else f"{'+' if float(signed_turnover) > 0 else ''}{float(signed_turnover):.0f}%"
        item["execution_tags_display"] = " · ".join(item.get("execution_tags") or [])
        item["metric_display"] = _heatmap_metric_display(item)
        item["background"] = _heatmap_background(item)
        item["_weight"] = _heatmap_weight(item)
    heatmap_rows_for_map = heatmap_rows[:24]
    treemap_rows = _split_treemap(heatmap_rows_for_map, 0.0, 0.0, 100.0, 100.0)

    def _heatmap_tile_href(item: dict) -> str:
        item_market = str(item.get("market") or "").upper()
        if item_market == "US":
            return "/dashboard/market/heatmap?" + urlencode(
                {
                    "lang": lang,
                    "lookback_runs": lookback_runs,
                    "heatmap_sort": heatmap_sort,
                    "heatmap_metric": heatmap_metric,
                    "heatmap_focus": item.get("label") or "",
                    "market_filter": "US",
                    "signal_filter": signal_filter,
                    "min_signal_strength": min_signal_strength,
                    "min_buy_signal_count": min_buy_signal_count,
                    "execution_tag_filter": execution_tag_filter,
                    "exclude_execution_tag_filter": exclude_execution_tag_filter,
                }
            )
        return "/dashboard/concepts/" + str(item["slug"]) + "?" + urlencode(
            {
                "lookback_runs": lookback_runs,
                "lang": lang,
                "signal_filter": signal_filter,
                "min_signal_strength": min_signal_strength,
                "min_buy_signal_count": min_buy_signal_count,
                "execution_tag_filter": execution_tag_filter,
                "exclude_execution_tag_filter": exclude_execution_tag_filter,
            }
        )

    heatmap_tiles = "".join(
        f"<a href='{_heatmap_tile_href(item)}' class='heat-tile' style='left:{item['_x']:.3f}%;top:{item['_y']:.3f}%;width:{item['_w']:.3f}%;height:{item['_h']:.3f}%;background:{item['background']};'>"
        f"<div><div class='heat-label'>{html.escape(str(item['display_label']))}</div><div class='heat-meta'>{t(lang, '面积=命中密度', 'Size = hit density')} · {t(lang, '颜色=', 'Color = ')}{html.escape(_heatmap_metric_label(lang, heatmap_metric))}</div></div>"
        f"<div><div class='heat-metric'>{html.escape(str(item['metric_display']))}</div><div class='heat-meta'>{int(item.get('hits') or 0)} {t(lang, '次命中', 'hit(s)')} · {t(lang, '买点', 'Buy')} {int(item.get('buy_signal_count') or 0)} · {t(lang, '最强', 'Max')} {int(item.get('max_signal_strength') or 0)}</div></div>"
        f"<div class='heat-meta heat-extra'>{(item['flow_ratio_display'] + ' · 净方向 ' + item['flow_signed_display']) if lang == 'zh' and heatmap_metric == 'flow' else ((item['flow_ratio_display'] + ' · Net ' + item['flow_signed_display']) if heatmap_metric == 'flow' else (item['avg_move_5d_display'] + ' · ' + item['breadth_display']))}</div>"
        f"<div class='heat-tags'>{''.join('<span>' + html.escape(str(tag)) + '</span>' for tag in (item.get('execution_tags') or [])[:3]) or (t(lang, '<span>无执行提醒</span>', '<span>No tags</span>'))}</div>"
        "</a>"
        for item in treemap_rows
    ) or f"<div class='muted'>{t(lang, '暂无热力图数据，请先等待后台完成对应市场的预计算。', 'No heatmap data yet. Wait for the market precompute job to finish.')}</div>"
    focused_heatmap_row = None
    if heatmap_focus:
        focus_key = heatmap_focus.strip().lower()
        focused_heatmap_row = next(
            (
                item
                for item in heatmap_rows
                if str(item.get("label") or "").strip().lower() == focus_key
                or str(item.get("display_label") or "").strip().lower() == focus_key
            ),
            None,
        )
    focus_detail_html = ""
    if focused_heatmap_row:
        focus_details = sorted(
            focused_heatmap_row.get("ticker_details") or [],
            key=lambda detail: (
                int(detail.get("signal_strength") or 0),
                float(detail.get("score") or 0.0),
                str(detail.get("ticker") or ""),
            ),
            reverse=True,
        )[:80]
        focus_rows_html = "".join(
            "<tr>"
            f"<td><a class='ticker-links' href='/insights/{html.escape(str(detail.get('ticker') or ''))}?lang={lang}'>{html.escape(str(detail.get('ticker') or '-'))}</a></td>"
            f"<td>{html.escape(_compact_label(str(detail.get('name') or '-'), 24))}</td>"
            f"<td>{html.escape(str(detail.get('signal_label') or '-'))} · {int(detail.get('signal_strength') or 0)}</td>"
            f"<td>{float(detail.get('score') or 0.0):.2f}</td>"
            f"<td>{html.escape(' / '.join(str(tag) for tag in (detail.get('execution_tags') or [])[:3]) or '-')}</td>"
            f"<td><a class='action-link' href='/insights/{html.escape(str(detail.get('ticker') or ''))}?lang={lang}'>{t(lang, '打开详情', 'Open')}</a></td>"
            "</tr>"
            for detail in focus_details
        ) or f"<tr><td colspan='6'>{t(lang, '这个板块暂无可展示股票。', 'No displayable names in this tile.')}</td></tr>"
        focus_clear_link = "/dashboard/market/heatmap?" + urlencode(
            {
                "lang": lang,
                "lookback_runs": lookback_runs,
                "heatmap_sort": heatmap_sort,
                "heatmap_metric": heatmap_metric,
                "market_filter": market_filter,
                "signal_filter": signal_filter,
                "min_signal_strength": min_signal_strength,
                "min_buy_signal_count": min_buy_signal_count,
                "execution_tag_filter": execution_tag_filter,
                "exclude_execution_tag_filter": exclude_execution_tag_filter,
            }
        )
        focus_detail_html = f"""
          <section class="card heat-detail-card">
            <div class="compare-row" style="justify-content:space-between;align-items:flex-start;">
              <div>
                <div class="eyebrow">{t(lang, '板块下钻', 'Sector Drilldown')}</div>
                <h2 style="margin:0 0 8px;font-size:24px;">{html.escape(str(focused_heatmap_row.get('display_label') or focused_heatmap_row.get('label') or '-'))}</h2>
                <div class="muted">{t(lang, '点击美股资金流色块后，这里展示该板块被模型命中的股票、信号强度和执行风险标签。', 'After clicking a U.S. flow tile, this panel lists the model-hit names, signal strength, and execution risk tags inside that sector.')}</div>
              </div>
              <a class="pill" href="{focus_clear_link}">{t(lang, '收起下钻', 'Collapse')}</a>
            </div>
            <div class="table-wrap heat-detail-table">
              <table>
                <thead><tr><th>Ticker</th><th>{t(lang, '名称', 'Name')}</th><th>{t(lang, '信号', 'Signal')}</th><th>Score</th><th>{t(lang, '风险标签', 'Risk Tags')}</th><th>Actions</th></tr></thead>
                <tbody>{focus_rows_html}</tbody>
              </table>
            </div>
          </section>
        """
    heatmap_scope_cards_html = ""
    all_heatmap_rows = list((heatmap_payload or {}).get("sector_heatmap") or []) if isinstance(heatmap_payload, dict) else []
    heatmap_scope_history = _recent_market_heat_history(db, limit=6)
    heatmap_scope_help = (
        (
            "口径说明：资金流代理不是主力净流入，而是把当日成交额相对近20日均值的放大倍数、上涨成交额占比、以及板块上涨广度合成为 0-100 分。适合看轮动热度，不适合替代真实席位资金。"
            if heatmap_metric == "flow"
            else "口径说明：这里的热度统一表示 0-100 的平均信号强度。热力图页面使用当前市场下各板块 `max_signal_strength` 的均值。"
        )
        if lang == "zh"
        else (
            "Methodology: flow proxy is not true institutional net flow. It combines turnover expansion vs the prior 20 sessions, the share of turnover on advancing names, and breadth into a 0-100 score. Use it for rotation heat, not broker-level money flow."
            if heatmap_metric == "flow"
            else "Methodology: heat is a unified 0-100 average signal-strength score. On the heatmap it is the mean `max_signal_strength` across tiles in the selected market."
        )
    )
    flow_rows = [item for item in heatmap_rows if item.get("flow_proxy_score") is not None]
    flow_snapshot_ready = any(
        any(key in item for key in ("flow_proxy_score", "turnover_ratio_20d", "signed_turnover_pct"))
        for item in heatmap_rows
    )
    flow_leader = max(
        flow_rows,
        key=lambda item: (float(item.get("flow_proxy_score") or 0.0), float(item.get("signed_turnover_pct") or 0.0)),
        default=None,
    )
    flow_laggard = min(
        flow_rows,
        key=lambda item: (float(item.get("signed_turnover_pct") or 0.0), float(item.get("flow_proxy_score") or 0.0)),
        default=None,
    )
    flow_summary_html = (
        (
            f"<div style='font-size:30px;font-weight:900;margin:6px 0;'>{float(flow_leader.get('flow_proxy_score') or 0.0):.0f}</div>"
            f"<div class='muted'>{t(lang, '最热资金流代理', 'Top flow proxy')}: <strong>{html.escape(str(flow_leader.get('display_label') or flow_leader.get('label') or '-'))}</strong></div>"
            f"<div class='muted' style='margin-top:8px;'>{t(lang, '放量', 'Turnover')} {html.escape(str(flow_leader.get('flow_ratio_display') or '-'))} · {t(lang, '净方向', 'Net')} {html.escape(str(flow_leader.get('flow_signed_display') or '-'))}</div>"
            f"<div class='muted' style='margin-top:8px;'>{t(lang, '降温板块', 'Cooling')}: <strong>{html.escape(str((flow_laggard or {}).get('display_label') or (flow_laggard or {}).get('label') or '-'))}</strong></div>"
        )
        if flow_leader
        else (
            f"<div class='muted'>{t(lang, '当前热力图快照还是旧版本，资金流代理会在下一次市场热力图预计算后出现。', 'This heatmap snapshot is still on the older schema. Flow proxy will appear after the next market heatmap precompute refresh.')}</div>"
            if not flow_snapshot_ready
            else f"<div class='muted'>{t(lang, '当前没有足够的资金流代理数据。', 'Not enough flow-proxy data yet.')}</div>"
        )
    )
    def _heatmap_metric_for_market_link(scope_market: str) -> str:
        # U.S. sector pages are most useful as a rotation/flow view by default.
        return "flow" if scope_market == "US" and heatmap_metric == "model" else heatmap_metric

    for scope_market, scope_label in (("CN", "A股" if lang == "zh" else "A-Shares"), ("US", "美股" if lang == "zh" else "U.S. Stocks")):
        scope_rows = [row for row in all_heatmap_rows if str(row.get("market") or "").upper() == scope_market]
        scope_history = heatmap_scope_history.get(scope_market) or []
        scope_hit_total = sum(int(row.get("hits") or 0) for row in scope_rows)
        scope_buy_total = sum(int(row.get("buy_signal_count") or 0) for row in scope_rows)
        scope_heat = round(
            sum(float(row.get("max_signal_strength") or 0.0) for row in scope_rows)
            / max(len(scope_rows), 1),
            1,
        ) if scope_rows else 0.0
        scope_top_label = (scope_rows[0].get("label") if scope_rows else None) or (t(lang, "暂无", "No leader"))
        scope_delta = (scope_history[-1] - scope_history[0]) if len(scope_history) >= 2 else 0
        scope_delta_tone = "up" if scope_delta > 0 else "down" if scope_delta < 0 else "flat"
        scope_delta_label = (
            (f"+{scope_delta} {t(lang, '升温', 'warming')}" if scope_delta > 0 else f"{scope_delta} {t(lang, '降温', 'cooling')}")
            if scope_delta != 0
            else (t(lang, "持平", "Flat"))
        )
        scope_href = f"/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': _heatmap_metric_for_market_link(scope_market), 'market_filter': scope_market, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}"
        heatmap_scope_cards_html += (
            f"<a class='market-scope-card{' active' if market_filter == scope_market else ''}' href='{scope_href}'>"
            f"<div class='market-scope-head'><strong>{scope_label}</strong><span>{_compact_label(str(scope_top_label), 18)}</span></div>"
            f"<div class='market-scope-headline'><span class='market-scope-chip {scope_delta_tone}'>{scope_delta_label}</span></div>"
            f"<div class='market-scope-stats'>"
            f"<div><b>{scope_hit_total}</b><span>{t(lang, '命中', 'Hits')}</span></div>"
            f"<div><b>{scope_buy_total}</b><span>{t(lang, '买点', 'Buy')}</span></div>"
            f"<div><b>{scope_heat:.1f}</b><span>{t(lang, '热度', 'Heat')}</span></div>"
            f"</div>"
            f"<div class='market-scope-trend-wrap'><span>{t(lang, '近6次快照热度', 'Last 6 snapshots')}</span>{_mini_trend_bars(scope_history, lang=lang)}</div>"
            f"<div class='market-scope-meta'>{t(lang, '板块数', 'Tiles')} {len(scope_rows)} · {t(lang, '标签数', 'Tags')} {sum(1 for row in scope_rows if row.get('execution_tags'))}</div>"
            "</a>"
        )
    lookback_pills = _lookback_pills("/dashboard/market/heatmap", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "heatmap_metric": heatmap_metric, "market_filter": market_filter, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    market_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': _heatmap_metric_for_market_link(market), 'market_filter': market, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if market_filter == market else ''}'>{label}</a>"
        for market, label in (
            ("ALL", "All Markets" if lang == "en" else "全部市场"),
            ("CN", "A-Shares" if lang == "en" else "A股"),
            ("US", "U.S." if lang == "en" else "美股"),
        )
    )
    heatmap_sort_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': mode, 'heatmap_metric': heatmap_metric, 'market_filter': market_filter, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if heatmap_sort == mode else ''}'>{label}</a>"
        for mode, label in (
            ("hits", _dt(lang, "sort_by_hits")),
            ("five_day", _dt(lang, "sort_by_5d")),
            ("breadth", _dt(lang, "sort_by_breadth")),
            ("score", _dt(lang, "sort_by_score")),
        )
    )
    heatmap_metric_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': mode, 'market_filter': market_filter, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if heatmap_metric == mode else ''}'>{_heatmap_metric_label(lang, mode)}</a>"
        for mode in ("model", "five_day", "breadth", "buy", "flow")
    )
    signal_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': heatmap_metric, 'market_filter': market_filter, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['label']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    loading_hint = (
        f"<div class='card'><div class='eyebrow'>{t(lang, '板块热力图', 'Sector Heatmap')}</div><p class='muted'>{t(lang, '暂无概念热力图', 'No concept heatmap yet')}</p></div>"
        if not heatmap_ready
        else ""
    )
    us_sector_tile_count = sum(1 for row in all_heatmap_rows if str(row.get("market") or "").upper() == "US")
    heatmap_method_notice = ""
    if market_filter == "US":
        heatmap_method_notice = (
            f"<div class='method-note'><strong>{t(lang, '美股资金流口径', 'U.S. Flow Method')}:</strong> "
            f"{t(lang, '默认使用资金流代理上色；当前只纳入已补齐真实 sector 元数据的美股板块，共 ', 'Defaults to flow-proxy coloring and only includes U.S. names with real sector metadata. Current sector tiles: ')}"
            f"{us_sector_tile_count}"
            f"{t(lang, ' 个。缺 sector 的股票仍会出现在连续强势、模型选股和个股详情里，但不会混入板块资金流图。', '. Names without sector metadata still appear in continuous leaders, screeners, and insights, but are not mixed into the sector-flow map.')}</div>"
        )
    return render_dashboard_heatmap_page(
        {
            "lang": lang,
            "lookback_runs": lookback_runs,
            "heatmap_sort": heatmap_sort,
            "heatmap_metric": heatmap_metric,
            "market_filter": market_filter,
            "signal_filter": signal_filter,
            "min_signal_strength": min_signal_strength,
            "min_buy_signal_count": min_buy_signal_count,
            "execution_tag_filter": execution_tag_filter,
            "exclude_execution_tag_filter": exclude_execution_tag_filter,
            "nav_html": nav_html,
            "market_pills": market_pills,
            "heatmap_method_notice": heatmap_method_notice,
            "loading_hint": loading_hint,
            "lookback_pills": lookback_pills,
            "heatmap_metric_pills": heatmap_metric_pills,
            "signal_pills": signal_pills,
            "tagged_names": tagged_names,
            "risk_top_tags_html": risk_top_tags_html,
            "risk_examples_html": risk_examples_html,
            "heatmap_sort_pills": heatmap_sort_pills,
            "heatmap_tiles": heatmap_tiles,
            "heatmap_metric_label": _heatmap_metric_label(lang, heatmap_metric),
            "focus_detail_html": focus_detail_html,
            "heatmap_scope_cards_html": heatmap_scope_cards_html,
            "heatmap_scope_help": heatmap_scope_help,
            "flow_summary_html": flow_summary_html,
            "resonance_score": float((heatmap_payload or {}).get("resonance_score") or 0.0),
            "tracked_signal_count": int((heatmap_payload or {}).get("tracked_signal_count") or 0),
        }
    )



@router.get("/market/concepts", response_class=HTMLResponse)
def dashboard_market_concepts_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    concept_sort_by: str = "delta",
    concept_sort_order: str = "desc",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market/concepts")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    concept_rows_source = _load_concept_tracker_rows(db, lookback_runs=lookback_runs)
    if signal_filter != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(int(detail.get("signal_strength") or 0) >= min_signal_strength for detail in item.get("ticker_details", []))
        ]
    if min_buy_signal_count > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    tagged_names = 0
    for item in concept_rows_source:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"concept_name": item.get("concept_name"), "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    concept_rows_source.sort(key=lambda item: _market_concept_sort_key(concept_sort_by, item))
    if concept_sort_order == "desc":
        concept_rows_source.reverse()

    def _concept_tracker_sort_link(column: str, label: str) -> str:
        next_order = "asc" if concept_sort_by == column and concept_sort_order == "desc" else "desc"
        arrow = ""
        if concept_sort_by == column:
            arrow = " ↓" if concept_sort_order == "desc" else " ↑"
        href = (
            f"/dashboard/market/concepts?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': column, 'concept_sort_order': next_order})}"
        )
        return f"<a href='{href}'>{label}{arrow}</a>"

    concept_rows = "".join(
        "<tr>"
        f"<td id='concept-{item['slug']}'><a href='/dashboard/concepts/{item['slug']}?{urlencode({'lookback_runs': lookback_runs, 'lang': lang, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}'>{item['concept_name']}</a></td>"
        f"<td>{item['hits']}</td><td>{item['previous_hits']}</td><td>{'+' if item['delta_hits'] > 0 else ''}{item['delta_hits']}</td><td>{item['streak']}</td><td>{_sparkline_svg(item['history'])}</td><td>{_percent_chip(item.get('avg_move_5d'))}</td><td>{_breadth_chip(item.get('breadth_pct'))}</td><td>{int(item.get('buy_signal_count') or 0)}</td><td>{int(item.get('max_signal_strength') or 0)}</td><td>{' · '.join(item.get('execution_tags') or []) or '-'}</td><td>{item['avg_score']:.4f}</td><td class='ticker-links'>{_ticker_links_html(item.get('tickers') or [], lang=lang)}</td>"
        "</tr>"
        for item in concept_rows_source
    ) or f"<tr><td colspan='13'>{t(lang, '暂无概念数据', 'No concept data yet')}</td></tr>"
    lookback_pills = _lookback_pills("/dashboard/market/concepts", selected=lookback_runs, extra_params={"lang": lang, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter, "concept_sort_by": concept_sort_by, "concept_sort_order": concept_sort_order})
    signal_pills = "".join(
        f"<a href='/dashboard/market/concepts?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': concept_sort_by, 'concept_sort_order': concept_sort_order})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['concept_name']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    return render_dashboard_legacy_page(
        "dashboard/legacy/market_dashboard_market_concepts_page.html",
        fragments=[
            f'{lang}',
            f"{t(lang, '概念异动追踪', 'Concept Activity Tracker')}",
            f'{CONCEPT_TRACKER_SORT_LINK_STYLE}',
            f"{t(lang, '概念追踪', 'Concept Tracker')}",
            f"{t(lang, '这里聚焦概念命中、连续性、强弱变化和概念内股票构成。', 'Use this page to track concept hits, persistence, strength shifts, and member tickers.')}",
            f'{nav_html}',
            f"{t(lang, '概念页更适合回答“哪些主题在持续强化，哪些只是短期异动”。', 'This page helps answer which themes are strengthening versus only flashing briefly.')}",
            f'{lang}',
            f'{lookback_runs}',
            f'{signal_filter}',
            f'{min_signal_strength}',
            f'{min_buy_signal_count}',
            f'{execution_tag_filter}',
            f'{exclude_execution_tag_filter}',
            f"{t(lang, '返回市场脉冲', 'Back to Market Pulse')}",
            f'{lang}',
            f'{lookback_runs}',
            f'{signal_filter}',
            f'{min_signal_strength}',
            f'{min_buy_signal_count}',
            f'{execution_tag_filter}',
            f'{exclude_execution_tag_filter}',
            f"{_dt(lang, 'sector_heatmap')}",
            f'{lookback_runs}',
            f'{signal_filter}',
            f'{min_signal_strength}',
            f'{min_buy_signal_count}',
            f'{execution_tag_filter}',
            f'{exclude_execution_tag_filter}',
            f'{concept_sort_by}',
            f'{concept_sort_order}',
            f'{lookback_runs}',
            f'{signal_filter}',
            f'{min_signal_strength}',
            f'{min_buy_signal_count}',
            f'{execution_tag_filter}',
            f'{exclude_execution_tag_filter}',
            f'{concept_sort_by}',
            f'{concept_sort_order}',
            f"{_dt(lang, 'concept_activity_tracker')}",
            f"{t(lang, '概念异动追踪', 'Concept Activity Tracker')}",
            f"{t(lang, '专门追踪概念命中、连续性、强弱和概念内股票构成。', 'A focused page for concept hits, persistence, strength, and tracked tickers.')}",
            f"{_dt(lang, 'snapshot_window')}",
            f'{lookback_pills}',
            f"{('Signal Focus' if lang == 'en' else '信号聚焦')}",
            f'{signal_pills}',
            f'{lang}',
            f'{lookback_runs}',
            f'{signal_filter}',
            f'{concept_sort_by}',
            f'{concept_sort_order}',
            f"{('Execution Tag' if lang == 'en' else '执行提醒标签')}",
            f"{(execution_tag_filter if execution_tag_filter.upper() != 'ALL' else '')}",
            f"{('Exclude Tag' if lang == 'en' else '排除标签')}",
            f"{(exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else '')}",
            f"{('Quick Tags' if lang == 'en' else '快捷标签')}",
            f"{('exclude gap-risk' if lang == 'en' else '排除 gap-risk')}",
            f"{('Clear Tags' if lang == 'en' else '清空标签')}",
            f"{('Min Buy Count' if lang == 'en' else '最少买点数')}",
            f'{min_buy_signal_count}',
            f"{('Min Strength' if lang == 'en' else '最低强度')}",
            f'{min_signal_strength}',
            f"{_concept_tr(lang, 'apply_filters')}",
            f"{_dt(lang, 'risk_overview')}",
            f"{_dt(lang, 'tagged_names')}",
            f'{tagged_names}',
            f"{_dt(lang, 'risk_examples')}",
            f"{_dt(lang, 'common_risks')}",
            f'{risk_top_tags_html}',
            f"{_dt(lang, 'risk_examples')}",
            f'{risk_examples_html}',
            f"{_dt(lang, 'concept_activity_tracker')}",
            f"{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': concept_sort_by, 'concept_sort_order': concept_sort_order})}",
            f"{_concept_tracker_sort_link('concept', _dt(lang, 'concept'))}",
            f"{_concept_tracker_sort_link('hits', _dt(lang, 'hits'))}",
            f"{_dt(lang, 'prev')}",
            f"{_concept_tracker_sort_link('delta', _dt(lang, 'delta_hits'))}",
            f"{_concept_tracker_sort_link('streak', _dt(lang, 'streak'))}",
            f"{_dt(lang, 'trend')}",
            f"{_concept_tracker_sort_link('five_day', _dt(lang, 'five_day'))}",
            f"{_concept_tracker_sort_link('breadth', _dt(lang, 'breadth'))}",
            f"{_concept_tracker_sort_link('buy_count', _concept_tr(lang, 'buy_signal_count'))}",
            f"{_concept_tracker_sort_link('max_strength', _concept_tr(lang, 'max_signal_strength'))}",
            f"{t(lang, '执行提醒', 'Execution Tags')}",
            f"{_concept_tracker_sort_link('score', _dt(lang, 'avg_score'))}",
            f"{_dt(lang, 'tickers')}",
            f'{concept_rows}',
        ],
    )



@router.get("/market/concepts/export")
def dashboard_market_concepts_export(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    concept_sort_by: str = "delta",
    concept_sort_order: str = "desc",
    db: Session = Depends(get_db_session),
) -> Response:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market/concepts")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    concept_rows_source = _load_concept_tracker_rows(db, lookback_runs=lookback_runs)
    if signal_filter != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(int(detail.get("signal_strength") or 0) >= min_signal_strength for detail in item.get("ticker_details", []))
        ]
    if min_buy_signal_count > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    concept_rows_source.sort(key=lambda item: _market_concept_sort_key(concept_sort_by, item))
    if concept_sort_order == "desc":
        concept_rows_source.reverse()

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "concept_name",
            "hits",
            "previous_hits",
            "delta_hits",
            "streak",
            "avg_move_5d",
            "breadth_pct",
            "buy_signal_count",
            "max_signal_strength",
            "execution_tags",
            "avg_score",
            "tickers",
        ],
    )
    writer.writeheader()
    for item in concept_rows_source:
        writer.writerow(
            {
                "concept_name": item.get("concept_name"),
                "hits": item.get("hits"),
                "previous_hits": item.get("previous_hits"),
                "delta_hits": item.get("delta_hits"),
                "streak": item.get("streak"),
                "avg_move_5d": item.get("avg_move_5d"),
                "breadth_pct": item.get("breadth_pct"),
                "buy_signal_count": item.get("buy_signal_count"),
                "max_signal_strength": item.get("max_signal_strength"),
                "execution_tags": ";".join(item.get("execution_tags") or []),
                "avg_score": item.get("avg_score"),
                "tickers": ",".join(item.get("tickers") or []),
            }
        )
    filename = f"concept-tracker-{lookback_runs}runs.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
