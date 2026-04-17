from __future__ import annotations

import html
import threading
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db_session
from app.models.schema import SymbolCreate
from app.services.auth import is_authenticated, login_redirect
from app.services.market_sync import sync_market_data
from app.services.runtime_cache import clear_namespace
from app.services.repository import SymbolRepository, WatchlistRepository
from app.services.social_signals import (
    add_social_account,
    add_social_posts_batch,
    remove_social_analysis_record,
    remove_social_account,
    social_signal_summary,
    start_social_us_price_sync_job,
)
from app.services.social_signal_scheduler import social_signal_scheduler_service
from app.services.symbol_catalog import infer_symbol_record
from app.services.ticker_format import infer_market_from_ticker, normalize_ticker_for_market
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
from app.services.workspace_snapshots import refresh_workspace_snapshots


router = APIRouter(prefix="/social-signals", tags=["social-signals"])


def _clear_watchlist_caches() -> None:
    clear_namespace("watchlist_items")
    clear_namespace("watchlist_analysis_fragment")
    clear_namespace("watchlist_table_fragment")
    clear_namespace("dashboard_home_panels")
    clear_namespace("dashboard_summary_bundle")
    clear_namespace("dashboard_home_summary_bundle")


def _refresh_workspace_snapshots_async() -> None:
    def _run() -> None:
        try:
            with SessionLocal() as snapshot_db:
                refresh_workspace_snapshots(snapshot_db)
        except Exception:
            return

    threading.Thread(
        target=_run,
        name="social-watchlist-refresh",
        daemon=True,
    ).start()


@router.get("", response_class=HTMLResponse)
def social_signals_page(
    request: Request,
    message: str | None = None,
    hot_scope: str = "all",
    hot_min_validation: int = 0,
    hot_market: str = "all",
    hot_handle: str = "all",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/social-signals")
    lang = resolve_request_lang(request, default="zh")
    summary = social_signal_summary(db)
    poll_status = summary.get("poll_status") or {}
    hot_scope = str(hot_scope or "all").strip().lower()
    if hot_scope not in {"all", "not_watchlist", "not_portfolio", "new_candidates"}:
        hot_scope = "all"
    hot_min_validation = max(0, min(100, int(hot_min_validation or 0)))
    hot_market = str(hot_market or "all").strip().upper()
    if hot_market not in {"ALL", "US", "CN", "HK"}:
        hot_market = "ALL"
    hot_handle = str(hot_handle or "all").strip()
    tracked_handle_values = {str(item.get("handle") or "").strip() for item in summary.get("accounts", [])}
    if hot_handle.lower() != "all" and hot_handle not in tracked_handle_values:
        hot_handle = "all"
    nav_html = render_workspace_nav_html(lang=lang, active_key="social")
    accounts_html = "".join(
        "<div class='list-row'>"
        f"<div><div class='ticker'>{_h(item.get('handle'))}</div><div class='muted'>{_h(item.get('note') or '-')}</div><div class='muted'>{'已追踪账号，可在右侧导入它的 X 帖子。' if lang == 'zh' else 'Tracked account. Import its X posts on the right.'}</div></div>"
        f"<form action='/social-signals/accounts/remove' method='post'><input type='hidden' name='handle' value='{_h(item.get('handle'), quote=True)}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{'删除' if lang == 'zh' else 'Remove'}</button></form>"
        "</div>"
        for item in summary["accounts"]
    ) or f"<div class='muted'>{'暂无追踪账号' if lang == 'zh' else 'No tracked accounts yet.'}</div>"
    account_options = "".join(
        f"<option value='{_h(item.get('handle'), quote=True)}'>{_h(item.get('handle'))} · {_h(item.get('note') or '')}</option>"
        for item in summary["accounts"]
    )
    account_stats_html = "".join(
        "<div class='list-row'>"
        f"<div><div class='ticker'>{_h(item.get('handle'))}</div><div class='muted'>{'帖子' if lang == 'zh' else 'Posts'} {int(item.get('post_count') or 0)} · {'提及' if lang == 'zh' else 'Mentions'} {int(item.get('mention_count') or 0)} · {'高优先级' if lang == 'zh' else 'Priority'} {int(item.get('actionable_count') or 0)}</div><div class='muted'>{'常提股票' if lang == 'zh' else 'Top tickers'}: {_h(', '.join(ticker + '×' + str(count) for ticker, count in (item.get('top_tickers') or [])) or '-')}</div></div>"
        "</div>"
        for item in summary.get("account_stats", [])
    ) or f"<div class='muted'>{'导入帖子后会显示账号统计。' if lang == 'zh' else 'Account statistics appear after importing posts.'}</div>"
    filtered_hot_mentions = [
        item
        for item in (summary.get("hot_mentions_24h", []) or [])
        if int(item.get("validation_score") or 0) >= hot_min_validation
        and (hot_market == "ALL" or str(item.get("market") or "").upper() == hot_market)
        and (hot_handle.lower() == "all" or str(item.get("handle") or "") == hot_handle)
        and (
            hot_scope == "all"
            or (hot_scope == "not_watchlist" and not item.get("in_watchlist"))
            or (hot_scope == "not_portfolio" and not item.get("in_portfolio"))
            or (hot_scope == "new_candidates" and not item.get("in_watchlist") and not item.get("in_portfolio"))
        )
    ]
    handle_filter_options = "".join(
        f"<option value='{_h(item.get('handle'), quote=True)}' {'selected' if hot_handle == str(item.get('handle') or '') else ''}>{_h(item.get('handle') or '-')}</option>"
        for item in summary.get("accounts", [])
    )
    hot_mentions_html = "".join(
        "<div class='list-row'>"
        f"<div><div class='ticker'><a href='/insights/{_h(item.get('ticker'), quote=True)}?lang={lang}'>{_h(item.get('ticker'))}</a> · {_h(item.get('name') or item.get('ticker'))}</div>"
        f"<div class='muted'>{_h(item.get('handle') or '-')} · {_h(item.get('market') or '-')} · {'24小时提及' if lang == 'zh' else '24h mentions'} {int(item.get('mention_count') or 0)} · {_h(item.get('system_action') or '-')}</div>"
        f"<div class='muted'>{'状态' if lang == 'zh' else 'Status'}: {_holding_watch_text(item, lang)}</div></div>"
        f"<div style='text-align:right;'><div class='score'>{int(item.get('hot_score') or 0)}</div><div class='muted'>{'验证分' if lang == 'zh' else 'Validation'} {int(item.get('validation_score') or 0)}</div><div style='margin-top:8px;'>{_hot_action_form(item, lang)}</div></div>"
        "</div>"
        for item in filtered_hot_mentions
    ) or f"<div class='muted'>{'近 24 小时暂无可聚焦的热点股票。' if lang == 'zh' else 'No notable hot names in the last 24 hours yet.'}</div>"
    resonance_html = "".join(
        "<div class='list-row'>"
        f"<div><div class='ticker'><a href='/insights/{_h(item.get('ticker'), quote=True)}?lang={lang}'>{_h(item.get('ticker'))}</a> · {_h(item.get('name') or item.get('ticker'))}</div>"
        f"<div class='muted'>{'共振账号' if lang == 'zh' else 'Accounts'} {int(item.get('handle_count') or 0)} · {'总提及' if lang == 'zh' else 'Mentions'} {int(item.get('mention_total') or 0)}</div>"
        f"<div class='muted'>{_h(item.get('handles_text') or '-')}</div><div class='muted'>{'状态' if lang == 'zh' else 'Status'}: {_holding_watch_text(item, lang)}</div></div>"
        f"<div style='text-align:right;'><div class='score'>{int(item.get('resonance_score') or 0)}</div><div class='muted'>{'最高验证分' if lang == 'zh' else 'Top validation'} {int(item.get('max_validation_score') or 0)}</div><div style='margin-top:8px;'>{_hot_action_form(item, lang)}</div></div>"
        "</div>"
        for item in (summary.get("resonance_24h", []) or [])
    ) or f"<div class='muted'>{'近 24 小时暂无多账号共振股票。' if lang == 'zh' else 'No multi-account resonance names in the last 24 hours yet.'}</div>"
    actionable_html = "".join(
        "<div class='list-row'>"
        f"<div><div class='ticker'>{_h(item.get('ticker'))} · {_h(item.get('name') or item.get('ticker'))}</div><div class='muted'>{_h(item.get('handle') or '-')} · {_h(item.get('social_view') or '-')} · {_h(' / '.join(item.get('validation_reasons') or []))}</div></div>"
        f"<div style='text-align:right;'><div class='score'>{int(item.get('validation_score') or 0)}</div><div class='muted'>{_h(item.get('system_action'))}</div></div>"
        "</div>"
        for item in summary["actionable"]
    ) or f"<div class='muted'>{'暂无高优先级社交信号' if lang == 'zh' else 'No high-priority social signals yet.'}</div>"
    mention_rows = "".join(
        "<tr>"
        f"<td>{_h(item.get('handle') or '-')}</td>"
        f"<td><a href='/insights/{_h(item.get('ticker'), quote=True)}?lang={lang}'>{_h(item.get('ticker'))}</a><div class='muted'>{_h(item.get('name') or '-')}</div><div class='muted'>{'提及' if lang == 'zh' else 'Mentions'} {int(item.get('mention_count') or 1)} · {_sync_status_text(item, lang) or ('已聚合' if lang == 'zh' else 'Aggregated')}</div></td>"
        f"<td>{_h(item.get('social_view') or '-')}</td>"
        f"<td>{int(item.get('validation_score') or 0)}</td>"
        f"<td>{_h(item.get('model_signal_label') or '-')}<div class='muted'>score {_h(item.get('model_score') if item.get('model_score') is not None else '-')}</div></td>"
        f"<td>{_h(item.get('system_action') or '-')}</td>"
        f"<td>{_h(' / '.join(item.get('validation_reasons') or []) or '-')}{_source_preview(item, lang)}</td>"
        f"<td>{_action_form(item, lang)}</td>"
        "</tr>"
        for item in summary["mentions"]
    ) or f"<tr><td colspan='8'>{'暂无股票提及' if lang == 'zh' else 'No ticker mentions yet.'}</td></tr>"
    default_handle = summary["accounts"][0].get("handle") if summary["accounts"] else ""
    tracked_handles = ", ".join(str(item.get("handle") or "") for item in summary["accounts"]) or "-"
    banner = f"<div class='banner'>{_h(message)}</div>" if message else ""
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'社交信号' if lang == 'zh' else 'Social Signals'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; min-width:0; }}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .hero,.grid {{ display:grid; gap:16px; grid-template-columns:minmax(0,1.15fr) minmax(320px,0.85fr); margin-bottom:16px; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.06em; text-transform:uppercase; margin-bottom:12px; }}
          h1 {{ margin:10px 0; font-size:40px; line-height:1.02; letter-spacing:-0.03em; }}
          .muted,.lead {{ color:var(--muted); line-height:1.55; }}
          .stack {{ display:grid; gap:12px; }}
          .list-row {{ display:flex; justify-content:space-between; gap:14px; padding:12px 0; border-top:1px solid var(--line); }}
          .list-row:first-child {{ border-top:none; padding-top:0; }}
          .ticker {{ font-weight:800; }}
          .score {{ font-size:24px; font-weight:900; color:var(--accent); }}
          input, textarea, button {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }}
          textarea {{ min-height:150px; resize:vertical; }}
          button {{ width:auto; background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; cursor:pointer; }}
          button.danger {{ background:rgba(255,107,107,0.14); color:#ffb4b4; border-color:rgba(255,107,107,0.32); }}
          .form-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }}
          table {{ width:100%; min-width:1120px; border-collapse:collapse; font-size:14px; }}
          th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:700; }}
          .banner {{ margin-bottom:16px; padding:14px 16px; border-radius:16px; background:rgba(61,217,182,0.12); color:var(--accent); border:1px solid rgba(61,217,182,0.24); font-weight:800; }}
          @media (max-width:1100px) {{ .app,.hero,.grid {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'社交信号' if lang == 'zh' else 'Social Signals'}</h1>
              <p>{'跟踪 X 账户观点，但必须经过模型与交易条件验证。' if lang == 'zh' else 'Track X account ideas, but validate them against model and trade conditions.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'每 30 分钟自动检查追踪账号；手工粘贴只作为备用入口。' if lang == 'zh' else 'Tracked accounts are checked every 30 minutes; manual paste is only a fallback.'}</div>
          </aside>
          <main class="main"><div class="wrap">
            {banner}
            <section class="hero">
              <article class="card"><span class="eyebrow">X Signal Desk</span><h1>{'X 账户自动验证台' if lang == 'zh' else 'X Account Auto Validation'}</h1><p class="lead">{'已追踪账号：' if lang == 'zh' else 'Tracked account: '} {_h(tracked_handles)}</p><p class="lead">{'系统每 30 分钟检查追踪账号，自动抽取帖子里的股票，判断账号观点方向，再和模型信号、自选股、持仓股交叉验证。美股 cashtag 如 $TSLA 会自动按 US 股票处理。' if lang == 'zh' else 'The system checks tracked accounts every 30 minutes, extracts ticker mentions, infers account view, then validates against model signals, watchlist, and portfolio. US cashtags such as $TSLA are treated as US stocks automatically.'}</p></article>
              <article class="card"><span class="eyebrow">{'自动轮询状态' if lang == 'zh' else 'Auto Poll Status'}</span><div class="stack">
                <div class="list-row"><div><div class="ticker">{'状态' if lang == 'zh' else 'Status'}</div><div class="muted">{_poll_status_text(poll_status, lang)}</div></div><div class="score">{int(poll_status.get('last_new_mentions') or 0)}</div></div>
                <div class="muted">{'上次运行' if lang == 'zh' else 'Last run'}: {_h(poll_status.get('last_run_at') or '-')}</div>
                <form action="/social-signals/poll/run" method="post"><input type="hidden" name="lang" value="{lang}" /><button type="submit">{'立即检查 3 个账号' if lang == 'zh' else 'Check accounts now'}</button></form>
              </div></article>
            </section>
            <section class="grid">
              <article class="card">
                <span class="eyebrow">{'追踪账号' if lang == 'zh' else 'Tracked Accounts'}</span>
                <form action="/social-signals/accounts/add" method="post" class="stack">
                  <input type="hidden" name="lang" value="{lang}" />
                  <div class="form-grid"><input name="handle" placeholder="@account or x.com/account" /><input name="note" placeholder="备注 / strategy label" /></div>
                  <button type="submit">{'添加账号' if lang == 'zh' else 'Add Account'}</button>
                </form>
                <div style="margin-top:16px;">{accounts_html}</div>
              </article>
              <article class="card">
                <span class="eyebrow">{'备用：手工导入帖子' if lang == 'zh' else 'Fallback: Manual Import'}</span>
                <form action="/social-signals/posts/add" method="post" class="stack">
                  <input type="hidden" name="lang" value="{lang}" />
                  <input name="handle" list="tracked-account-options" placeholder="@account" value="{_h(default_handle, quote=True)}" />
                  <datalist id="tracked-account-options">{account_options}</datalist>
                  <input name="source_url" placeholder="https://x.com/... 可选" />
                  <textarea name="content" placeholder="粘贴 X 帖子内容，支持股票代码、$TSLA、300xxx、公司名... 如需批量导入，多条帖子之间用一行 --- 分隔。"></textarea>
                  <button type="submit">{'分析帖子' if lang == 'zh' else 'Analyze Post'}</button>
                </form>
              </article>
            </section>
            <section class="card"><span class="eyebrow">{'近 24 小时最热股票' if lang == 'zh' else 'Top Stocks in 24h'}</span>
              <form action="/social-signals" method="get" class="stack" style="max-width:520px;margin-bottom:12px;">
                <input type="hidden" name="lang" value="{lang}" />
                <div class="form-grid">
                  <select name="hot_scope">
                    <option value="all" {'selected' if hot_scope == 'all' else ''}>{'全部' if lang == 'zh' else 'All'}</option>
                    <option value="new_candidates" {'selected' if hot_scope == 'new_candidates' else ''}>{'只看未进自选/持仓' if lang == 'zh' else 'Only new candidates'}</option>
                    <option value="not_watchlist" {'selected' if hot_scope == 'not_watchlist' else ''}>{'只看未进自选' if lang == 'zh' else 'Not in watchlist'}</option>
                    <option value="not_portfolio" {'selected' if hot_scope == 'not_portfolio' else ''}>{'只看未进持仓' if lang == 'zh' else 'Not in portfolio'}</option>
                  </select>
                  <input type="number" name="hot_min_validation" min="0" max="100" value="{hot_min_validation}" placeholder="{'最低验证分' if lang == 'zh' else 'Min validation'}" />
                  <select name="hot_market">
                    <option value="ALL" {'selected' if hot_market == 'ALL' else ''}>{'全部市场' if lang == 'zh' else 'All markets'}</option>
                    <option value="US" {'selected' if hot_market == 'US' else ''}>{'美股' if lang == 'zh' else 'US'}</option>
                    <option value="CN" {'selected' if hot_market == 'CN' else ''}>{'A股' if lang == 'zh' else 'CN'}</option>
                    <option value="HK" {'selected' if hot_market == 'HK' else ''}>{'港股' if lang == 'zh' else 'HK'}</option>
                  </select>
                  <select name="hot_handle">
                    <option value="all" {'selected' if hot_handle.lower() == 'all' else ''}>{'全部账号' if lang == 'zh' else 'All accounts'}</option>
                    {handle_filter_options}
                  </select>
                </div>
                <div><button type="submit">{'应用筛选' if lang == 'zh' else 'Apply Filter'}</button></div>
              </form>
              <div class="stack">{hot_mentions_html}</div>
            </section>
            <section class="card"><span class="eyebrow">{'社交共振榜' if lang == 'zh' else 'Social Resonance'}</span><div class="stack">{resonance_html}</div></section>
            <section class="card"><span class="eyebrow">{'账号统计' if lang == 'zh' else 'Account Stats'}</span><div class="stack">{account_stats_html}</div></section>
            <section class="card"><span class="eyebrow">{'高优先级社交信号' if lang == 'zh' else 'High-Priority Social Signals'}</span><div class="stack">{actionable_html}</div></section>
            <section class="card">
              <span class="eyebrow">{'股票提及与系统验证' if lang == 'zh' else 'Mentions and Validation'}</span>
              <div class="table-wrap"><table><thead><tr><th>Account</th><th>Ticker</th><th>{'观点' if lang == 'zh' else 'View'}</th><th>{'验证分' if lang == 'zh' else 'Score'}</th><th>{'模型' if lang == 'zh' else 'Model'}</th><th>{'动作' if lang == 'zh' else 'Action'}</th><th>{'原因' if lang == 'zh' else 'Reasons'}</th><th>{'操作' if lang == 'zh' else 'Ops'}</th></tr></thead><tbody>{mention_rows}</tbody></table></div>
            </section>
          </div></main>
        </div>
      </body>
    </html>
    """


@router.post("/accounts/add")
def add_account(handle: str = Form(""), note: str = Form(""), lang: str = Form("zh"), db: Session = Depends(get_db_session)) -> RedirectResponse:
    add_social_account(db, handle, note=note)
    return RedirectResponse(url=f"/social-signals?lang={lang}&message=Account+saved", status_code=303)


@router.post("/accounts/remove")
def remove_account(handle: str = Form(""), lang: str = Form("zh"), db: Session = Depends(get_db_session)) -> RedirectResponse:
    remove_social_account(db, handle)
    return RedirectResponse(url=f"/social-signals?lang={lang}&message=Account+removed", status_code=303)


@router.post("/posts/add")
def add_post(handle: str = Form(""), content: str = Form(""), source_url: str = Form(""), lang: str = Form("zh"), db: Session = Depends(get_db_session)) -> RedirectResponse:
    message = "Post analyzed"
    if content.strip():
        analyses = add_social_posts_batch(db, handle=handle, content=content, source_url=source_url)
        sync_job = start_social_us_price_sync_job(db, analyses)
        if sync_job:
            message = f"Post analyzed; US price sync queued for {sync_job['count']} ticker(s)"
    return RedirectResponse(url=f"/social-signals?lang={lang}&message={quote_plus(message)}", status_code=303)


@router.post("/poll/run")
def run_social_poll(lang: str = Form("zh")) -> RedirectResponse:
    result = social_signal_scheduler_service.run_now_async()
    message = f"Social polling queued as job {result.get('job_id')}"
    return RedirectResponse(url=f"/social-signals?lang={lang}&message={quote_plus(message)}", status_code=303)


@router.post("/mentions/remove")
def remove_mention(
    analysis_id: str = Form(""),
    ticker: str = Form(""),
    lang: str = Form("zh"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    removed = remove_social_analysis_record(db, analysis_id=analysis_id, ticker=ticker)
    message = "解析记录已删除" if removed and lang == "zh" else "Mention removed" if removed else "Record not found"
    return RedirectResponse(url=f"/social-signals?lang={lang}&message={quote_plus(message)}", status_code=303)


@router.post("/watchlist/add")
def add_to_watchlist(ticker: str = Form(...), lang: str = Form("zh"), db: Session = Depends(get_db_session)) -> RedirectResponse:
    normalized_market = infer_market_from_ticker(ticker)
    normalized_ticker = normalize_ticker_for_market(ticker, normalized_market)
    inferred = infer_symbol_record(normalized_ticker, normalized_market)
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    symbol = symbol_repo.get_by_ticker(normalized_ticker)
    if symbol is None:
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=normalized_ticker,
                name=(inferred or {}).get("name") or normalized_ticker,
                market=normalized_market,
                exchange=(inferred or {}).get("exchange"),
            )
        )
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_repo.add_symbol(watchlist.id, symbol.id)
    _clear_watchlist_caches()
    _refresh_workspace_snapshots_async()
    return RedirectResponse(
        url=f"/social-signals?lang={lang}&message={quote_plus(f'Added {normalized_ticker} to watchlist')}",
        status_code=303,
    )


@router.post("/watchlist/sync")
def sync_social_ticker(ticker: str = Form(...), lang: str = Form("zh")) -> RedirectResponse:
    sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
    return RedirectResponse(url=f"/social-signals?lang={lang}&message=Ticker+synced", status_code=303)


def _action_form(item: dict, lang: str) -> str:
    ticker = item.get("ticker")
    if not ticker:
        return "-"
    escaped_ticker = _h(ticker, quote=True)
    analysis_id = _h(item.get("analysis_id") or "", quote=True)
    return (
        "<div style='display:flex;gap:8px;flex-wrap:wrap;'>"
        f"<form action='/social-signals/watchlist/add' method='post'><input type='hidden' name='ticker' value='{escaped_ticker}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{'加入自选' if lang == 'zh' else 'Watch'}</button></form>"
        f"<form action='/social-signals/watchlist/sync' method='post'><input type='hidden' name='ticker' value='{escaped_ticker}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{'同步' if lang == 'zh' else 'Sync'}</button></form>"
        f"<form action='/social-signals/mentions/remove' method='post'><input type='hidden' name='analysis_id' value='{analysis_id}' /><input type='hidden' name='ticker' value='{escaped_ticker}' /><input type='hidden' name='lang' value='{lang}' /><button class='danger' type='submit'>{'删除' if lang == 'zh' else 'Delete'}</button></form>"
        "</div>"
    )


def _poll_status_text(status: dict, lang: str) -> str:
    if not status.get("configured"):
        return (
            "自动轮询已启动，但还没有配置 X Bearer Token。请在 .env 设置 PQW_X_BEARER_TOKEN。"
            if lang == "zh"
            else "Auto polling is running, but X Bearer Token is not configured. Set PQW_X_BEARER_TOKEN in .env."
        )
    last_status = status.get("last_status") or "-"
    message = status.get("last_message") or "-"
    if lang == "zh":
        return f"每 30 分钟自动检查；上次状态 {last_status}；{message}"
    return f"Checks every 30 minutes; last status {last_status}; {message}"


def _source_preview(item: dict, lang: str) -> str:
    latest_source_url = str(item.get("latest_source_url") or item.get("source_url") or "").strip()
    latest_content = str(item.get("latest_content") or item.get("content") or "").strip()
    latest_content = latest_content.replace("\n", " ")
    if len(latest_content) > 180:
        latest_content = latest_content[:177] + "..."
    parts: list[str] = []
    if latest_source_url:
        label = "原帖" if lang == "zh" else "Source"
        parts.append(f"<div class='muted'><a href='{_h(latest_source_url, quote=True)}' target='_blank' rel='noreferrer'>{label}</a></div>")
    if latest_content:
        prefix = "原文" if lang == "zh" else "Excerpt"
        parts.append(f"<div class='muted'>{prefix}: {_h(latest_content)}</div>")
    return "".join(parts)


def _holding_watch_text(item: dict, lang: str) -> str:
    if item.get("in_portfolio"):
        return "已在持仓" if lang == "zh" else "In portfolio"
    if item.get("in_watchlist"):
        return "已在自选" if lang == "zh" else "In watchlist"
    return "未加入列表" if lang == "zh" else "Not tracked"


def _hot_action_form(item: dict, lang: str) -> str:
    ticker = str(item.get("ticker") or "").strip()
    if not ticker:
        return ""
    escaped_ticker = _h(ticker, quote=True)
    forms: list[str] = []
    if not item.get("in_watchlist"):
        forms.append(
            f"<form action='/social-signals/watchlist/add' method='post' style='display:inline-block;margin:0 0 6px 6px;'><input type='hidden' name='ticker' value='{escaped_ticker}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{'加入自选' if lang == 'zh' else 'Watch'}</button></form>"
        )
    forms.append(
        f"<form action='/social-signals/watchlist/sync' method='post' style='display:inline-block;margin:0 0 6px 6px;'><input type='hidden' name='ticker' value='{escaped_ticker}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{'同步行情' if lang == 'zh' else 'Sync'}</button></form>"
    )
    return "".join(forms)


def _sync_status_text(item: dict, lang: str) -> str:
    if str(item.get("market") or "").upper() != "US":
        return ""
    status = str(item.get("price_sync_status") or "").strip()
    if not status:
        return "行情: 已入队/待同步" if lang == "zh" else "Price: queued/pending"
    if status == "success":
        rows = int(item.get("price_sync_rows") or 0)
        last_date = item.get("price_sync_last_date") or "-"
        return f"{'行情' if lang == 'zh' else 'Price'}: success · {rows} rows · {last_date}"
    return f"{'行情' if lang == 'zh' else 'Price'}: {status} · {_h(item.get('price_sync_message') or '-')}"


def _h(value, quote: bool = False) -> str:
    return html.escape(str(value or ""), quote=quote)
