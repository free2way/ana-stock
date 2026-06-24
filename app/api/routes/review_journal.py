from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.recommendation_regression import load_or_build_recommendation_regression, summarize_recommendation_regression
from app.services.review_journal import delete_review_entry, get_review_entry, list_review_entries, save_review_entry
from app.services.time_utils import app_today_iso
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html


router = APIRouter(prefix="/review-journal", tags=["review-journal"])


def _e(value: object) -> str:
    return html.escape(str(value or ""))


def _field(entry: dict, key: str) -> str:
    return _e(entry.get(key) or "")


def _selected(value: object, expected: str) -> str:
    return "selected" if str(value or "") == expected else ""


def _score_label(score: object, *, lang: str) -> str:
    value = str(score or "3").strip()
    labels_zh = {
        "1": "1 分 · 严重失控",
        "2": "2 分 · 有明显违纪",
        "3": "3 分 · 基本合格",
        "4": "4 分 · 执行良好",
        "5": "5 分 · 高纪律执行",
    }
    labels_en = {
        "1": "1 · Lost discipline",
        "2": "2 · Clear rule breaks",
        "3": "3 · Acceptable",
        "4": "4 · Good execution",
        "5": "5 · Excellent discipline",
    }
    return (labels_zh if lang == "zh" else labels_en).get(value, value or "-")


def _entry_rows(entries: list[dict], *, active_date: str, lang: str) -> str:
    if not entries:
        return f"<div class='empty'>{'还没有复盘记录，先从今天开始写第一条。' if lang == 'zh' else 'No journal entries yet. Start with today.'}</div>"
    rows: list[str] = []
    for item in entries[:30]:
        journal_date = str(item.get("journal_date") or "")[:10]
        active = " active" if journal_date == active_date else ""
        title = item.get("daily_plan") or item.get("execution_review") or ("未填写计划" if lang == "zh" else "No plan yet")
        rows.append(
            f"""
            <a class="entry-row{active}" href="/review-journal?lang={lang}&date={_e(journal_date)}">
              <div>
                <strong>{_e(journal_date)}</strong>
                <span>{_e(str(title)[:46])}</span>
              </div>
              <small>{_e(_score_label(item.get('discipline_score'), lang=lang))}</small>
            </a>
            """
        )
    return "".join(rows)


def _stats(entries: list[dict], *, lang: str) -> dict:
    recent = entries[:7]
    scores: list[float] = []
    for item in recent:
        try:
            scores.append(float(item.get("discipline_score") or 0))
        except (TypeError, ValueError):
            continue
    avg_score = round(sum(scores) / len(scores), 1) if scores else None
    completed = sum(
        1
        for item in recent
        if str(item.get("execution_review") or "").strip()
        or str(item.get("what_failed") or "").strip()
        or str(item.get("lessons") or "").strip()
    )
    return {
        "avg_score": avg_score,
        "completed": completed,
        "recent_count": len(recent),
        "headline": (
            f"最近 {len(recent)} 条，平均纪律 {avg_score or '-'} / 5"
            if lang == "zh"
            else f"Recent {len(recent)} entries, average discipline {avg_score or '-'} / 5"
        ),
    }


def _fmt_pct(value: object) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "-"


def _hit_status_chip(record: dict, *, lang: str) -> str:
    if record.get("execution_hit"):
        label = "执行命中" if lang == "zh" else "Hit"
        cls = "good"
    elif record.get("gap_blocked"):
        label = "高开拦截" if lang == "zh" else "Gap blocked"
        cls = "warn"
    elif record.get("deep_intraday_drawdown"):
        label = "盘中回撤" if lang == "zh" else "Drawdown"
        cls = "bad"
    else:
        label = "未命中" if lang == "zh" else "Miss"
        cls = "bad"
    return f"<span class='status-chip {cls}'>{_e(label)}</span>"


def _render_recommendation_regression_card(db: Session, *, lang: str) -> str:
    try:
        regression = load_or_build_recommendation_regression(db=db)
    except Exception as exc:
        return f"""
        <section class="card">
          <span class="eyebrow">{'推荐命中复盘' if lang == 'zh' else 'Recommendation Review'}</span>
          <div class="empty">{'暂时无法读取推荐回归结果：' if lang == 'zh' else 'Unable to load recommendation regression: '}{_e(exc)}</div>
        </section>
        """
    guidance = summarize_recommendation_regression(regression, lang=lang)
    metrics_html = "".join(
        f"<div class='metric'><strong>{_e(item.get('value'))}</strong><span>{_e(item.get('label'))}</span></div>"
        for item in guidance.get("metrics") or []
    ) or f"<div class='empty'>{'暂无可统计指标。' if lang == 'zh' else 'No measurable metrics yet.'}</div>"
    rules_html = "".join(
        f"<div class='hint-card'>{_e(rule)}</div>"
        for rule in (guidance.get("rules") or [])[:5]
    )
    warning_html = "".join(
        f"<div class='hint-card warn-card'>{_e(item)}</div>"
        for item in (guidance.get("warnings") or [])[:4]
    )
    recent_rows = "".join(
        "<tr>"
        f"<td><a href='/insights/{_e(record.get('ticker'))}?lang={lang}' style='font-weight:850;color:var(--ink);'>{_e(record.get('name') or record.get('ticker'))}</a><div class='muted'>{_e(record.get('ticker'))}</div></td>"
        f"<td>{_e(record.get('report_date'))}<div class='muted'>{_e(record.get('next_date'))}</div></td>"
        f"<td>{_e('可执行池' if record.get('report_pool') == 'actionable' and lang == 'zh' else ('观察池' if record.get('report_pool') == 'watch' and lang == 'zh' else record.get('report_pool')))}<div class='muted'>#{_e(record.get('report_rank'))} · {_e(record.get('template') or '-')}</div></td>"
        f"<td>{_fmt_pct(record.get('gap_open_pct'))}<div class='muted'>{'高开' if lang == 'zh' else 'Gap'}</div></td>"
        f"<td>{_fmt_pct(record.get('open_to_high_pct'))}<div class='muted'>{'开盘到最高' if lang == 'zh' else 'Open to high'}</div></td>"
        f"<td>{_fmt_pct(record.get('open_to_low_pct'))}<div class='muted'>{'盘中回撤' if lang == 'zh' else 'Drawdown'}</div></td>"
        f"<td>{_fmt_pct(record.get('close_1d_pct'))}<div class='muted'>{'收盘结果' if lang == 'zh' else 'Close result'}</div></td>"
        f"<td>{_hit_status_chip(record, lang=lang)}</td>"
        "</tr>"
        for record in (regression.get("recent_records") or [])[:10]
    ) or f"<tr><td colspan='8'>{'暂无逐票验证记录，等下一次 AI 日报和次日行情补齐后会自动出现。' if lang == 'zh' else 'No per-name outcome records yet.'}</td></tr>"
    return f"""
    <section class="card">
      <span class="eyebrow">{'推荐命中复盘' if lang == 'zh' else 'Recommendation Review'}</span>
      <h2 style="margin:4px 0 8px;">{_e(guidance.get('headline'))}</h2>
      <p class="lead">{'系统每天会把 AI 日报候选和第二天真实行情对照，自动沉淀“该信什么、该避开什么”。你写明日计划前，先看这张卡。' if lang == 'zh' else 'The system compares AI report candidates against next-session reality and turns outcomes into usage rules.'}</p>
      <div class="metric-grid">{metrics_html}</div>
      <div class="journal-grid compact" style="margin-top:12px;">
        <div class="stack">{rules_html}</div>
        <div class="stack">{warning_html or f"<div class='hint-card'>{'暂无自动降权规则触发。' if lang == 'zh' else 'No auto-downgrade rules triggered yet.'}</div>"}</div>
      </div>
      <div class="table-wrap" style="margin-top:12px;">
        <table>
          <thead><tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'推荐/验证日' if lang == 'zh' else 'Report / Next'}</th><th>{'来源池' if lang == 'zh' else 'Pool'}</th><th>{'高开' if lang == 'zh' else 'Gap'}</th><th>{'冲高' if lang == 'zh' else 'High'}</th><th>{'回撤' if lang == 'zh' else 'Low'}</th><th>{'收盘' if lang == 'zh' else 'Close'}</th><th>{'结论' if lang == 'zh' else 'Outcome'}</th></tr></thead>
          <tbody>{recent_rows}</tbody>
        </table>
      </div>
    </section>
    """


def _render_page(request: Request, db: Session, *, saved: bool = False) -> str:
    lang = resolve_request_lang(request, default="zh")
    selected_date = str(request.query_params.get("date") or app_today_iso())[:10]
    entries = list_review_entries(db)
    entry = get_review_entry(db, selected_date)
    nav_html = render_workspace_nav_html(lang=lang, active_key="journal")
    stats = _stats(entries, lang=lang)
    regression_card_html = _render_recommendation_regression_card(db, lang=lang)
    saved_banner = (
        f"<div class='banner'>{'已保存复盘心得。' if lang == 'zh' else 'Journal entry saved.'}</div>"
        if saved
        else ""
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'复盘心得' if lang == 'zh' else 'Review Journal'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --warn:#fbbf24; --danger:#fb7185; --blue:#60a5fa; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(96,165,250,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.14), transparent 26%),linear-gradient(180deg,#08111a 0%,#071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .topbar {{ display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }}
          .hero {{ display:grid; grid-template-columns:minmax(0,1.15fr) minmax(280px,0.85fr); gap:12px; margin-bottom:12px; }}
          .journal-grid {{ display:grid; grid-template-columns:310px minmax(0,1fr); gap:12px; align-items:start; }}
          h1 {{ margin:8px 0 8px; font-size:34px; line-height:1.04; letter-spacing:-0.04em; }}
          .pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--ink); font-size:13px; font-weight:850; }}
          .banner {{ margin-bottom:10px; padding:10px 12px; border-radius:10px; border:1px solid rgba(61,217,182,0.25); background:rgba(61,217,182,0.10); color:#bbf7d0; font-weight:850; }}
          .metric-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-top:10px; }}
          .metric {{ padding:10px; border-radius:10px; border:1px solid rgba(255,255,255,0.06); background:rgba(15,24,35,0.72); }}
          .metric strong {{ display:block; font-size:20px; }}
          .metric span {{ color:var(--muted); font-size:11.5px; }}
          .journal-grid.compact {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
          .entry-list {{ display:grid; gap:7px; max-height:680px; overflow:auto; padding-right:2px; }}
          .entry-row {{ display:grid; gap:6px; padding:10px; border:1px solid var(--line); border-radius:10px; background:rgba(15,24,35,0.72); }}
          .entry-row.active {{ border-color:rgba(61,217,182,0.42); background:linear-gradient(90deg, rgba(61,217,182,0.14), rgba(96,165,250,0.06)); }}
          .entry-row strong {{ display:block; margin-bottom:3px; }}
          .entry-row span, .entry-row small {{ color:var(--muted); line-height:1.35; }}
          .form-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
          label {{ display:grid; gap:6px; color:var(--muted); font-size:12px; font-weight:850; }}
          textarea.tall {{ min-height:150px; }}
          .wide {{ grid-column:1 / -1; }}
          .hint-card {{ padding:11px; border-radius:10px; border:1px solid rgba(255,255,255,0.06); background:rgba(15,24,35,0.72); color:var(--muted); line-height:1.55; font-size:12.5px; }}
          .warn-card {{ border-color:rgba(251,191,36,0.25); background:rgba(251,191,36,0.08); color:#fde68a; }}
          .status-chip {{ display:inline-flex; padding:5px 8px; border-radius:8px; border:1px solid rgba(255,255,255,0.08); font-size:12px; font-weight:900; }}
          .status-chip.good {{ color:#bbf7d0; background:rgba(34,197,94,0.12); border-color:rgba(34,197,94,0.26); }}
          .status-chip.warn {{ color:#fde68a; background:rgba(251,191,36,0.10); border-color:rgba(251,191,36,0.26); }}
          .status-chip.bad {{ color:#fecdd3; background:rgba(251,113,133,0.10); border-color:rgba(251,113,133,0.26); }}
          .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }}
          .actions button, .actions .pill {{ width:auto; min-width:130px; }}
          .danger {{ border-color:rgba(251,113,133,0.28); color:#fecdd3; }}
          .empty {{ color:var(--muted); padding:12px; border:1px dashed var(--line); border-radius:10px; }}
          @media (max-width:1120px) {{ .hero,.journal-grid,.journal-grid.compact {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'复盘心得' if lang == 'zh' else 'Review Journal'}</h1>
              <p>{'把每天的计划、执行、得失和明日纪律沉淀下来，避免同一个错误反复出现。' if lang == 'zh' else 'Capture daily plans, execution, lessons, and tomorrow’s discipline.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'建议每天收盘后 5 分钟写，第二天开盘前 2 分钟复读。' if lang == 'zh' else 'Write for 5 minutes after close; reread for 2 minutes before the next open.'}</div>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="topbar">
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                  <a class="pill" href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                  <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">{'看 AI 日报' if lang == 'zh' else 'AI Daily Report'}</a>
                  <a class="pill" href="/dashboard/model-performance?lang={lang}&market=ALL">{'模型评测' if lang == 'zh' else 'Model Evaluation'}</a>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                  <a class="pill" href="/review-journal?lang=en&date={_e(selected_date)}">EN</a>
                  <a class="pill" href="/review-journal?lang=zh&date={_e(selected_date)}">中文</a>
                </div>
              </div>
              {saved_banner}
              <section class="hero">
                <article class="card">
                  <span class="eyebrow">{'交易日志' if lang == 'zh' else 'Trading Journal'}</span>
                  <h1>{'记录比预测更重要' if lang == 'zh' else 'Review Beats Prediction'}</h1>
                  <p class="lead">{'这个页面不帮你写漂亮作文，只帮你把“计划、执行、错误、改进动作”变成可复查的交易纪律。' if lang == 'zh' else 'This page turns plan, execution, mistakes, and improvements into reviewable trading discipline.'}</p>
                  <div class="metric-grid">
                    <div class="metric"><strong>{_e(stats.get('recent_count'))}</strong><span>{'近 7 条记录' if lang == 'zh' else 'Recent entries'}</span></div>
                    <div class="metric"><strong>{_e(stats.get('avg_score') or '-')}</strong><span>{'平均纪律分' if lang == 'zh' else 'Avg discipline'}</span></div>
                    <div class="metric"><strong>{_e(stats.get('completed'))}</strong><span>{'完成复盘' if lang == 'zh' else 'Reviewed'}</span></div>
                  </div>
                </article>
                <article class="card">
                  <span class="eyebrow">{'建议写法' if lang == 'zh' else 'Writing Guide'}</span>
                  <div class="stack">
                    <div class="hint-card">{'1. 先写明日计划：只写可执行动作，不写愿望。' if lang == 'zh' else '1. Write executable actions, not wishes.'}</div>
                    <div class="hint-card">{'2. 盘后记录得失：哪个买点错了，哪个纪律救了你。' if lang == 'zh' else '2. Record what worked and what broke.'}</div>
                    <div class="hint-card">{'3. 明日只改一件事：仓位、买点、止损、还是模型选择。' if lang == 'zh' else '3. Improve one thing tomorrow: sizing, entry, stop, or model choice.'}</div>
                  </div>
                </article>
              </section>
              {regression_card_html}
              <section class="journal-grid">
                <aside class="card">
                  <div class="eyebrow">{'历史记录' if lang == 'zh' else 'History'}</div>
                  <div class="actions" style="margin:0 0 10px;">
                    <a class="pill" href="/review-journal?lang={lang}&date={app_today_iso()}">{'写今天' if lang == 'zh' else 'Today'}</a>
                  </div>
                  <div class="entry-list">{_entry_rows(entries, active_date=selected_date, lang=lang)}</div>
                </aside>
                <section class="card">
                  <div class="eyebrow">{'编辑复盘' if lang == 'zh' else 'Edit Journal'}</div>
                  <form method="post" action="/review-journal/save?lang={lang}">
                    <div class="form-grid">
                      <label>{'日期' if lang == 'zh' else 'Date'}<input type="date" name="journal_date" value="{_field(entry, 'journal_date')}" /></label>
                      <label>{'市场范围' if lang == 'zh' else 'Market Scope'}
                        <select name="market_scope">
                          <option value="ALL" {_selected(entry.get('market_scope'), 'ALL')}>{'全部市场' if lang == 'zh' else 'All Markets'}</option>
                          <option value="CN" {_selected(entry.get('market_scope'), 'CN')}>A股</option>
                          <option value="US" {_selected(entry.get('market_scope'), 'US')}>{'美股' if lang == 'zh' else 'U.S.'}</option>
                        </select>
                      </label>
                      <label>{'情绪状态' if lang == 'zh' else 'Emotion'}
                        <select name="emotion">
                          <option value="calm" {_selected(entry.get('emotion'), 'calm')}>{'冷静' if lang == 'zh' else 'Calm'}</option>
                          <option value="greedy" {_selected(entry.get('emotion'), 'greedy')}>{'偏贪婪' if lang == 'zh' else 'Greedy'}</option>
                          <option value="fearful" {_selected(entry.get('emotion'), 'fearful')}>{'偏恐惧' if lang == 'zh' else 'Fearful'}</option>
                          <option value="tired" {_selected(entry.get('emotion'), 'tired')}>{'疲劳' if lang == 'zh' else 'Tired'}</option>
                        </select>
                      </label>
                      <label>{'纪律评分' if lang == 'zh' else 'Discipline Score'}
                        <select name="discipline_score">
                          {''.join(f'<option value="{score}" {_selected(entry.get("discipline_score"), score)}>{_e(_score_label(score, lang=lang))}</option>' for score in ['1','2','3','4','5'])}
                        </select>
                      </label>
                      <label class="wide">{'重点股票 / 主题' if lang == 'zh' else 'Focus Tickers / Themes'}<input name="focus_tickers" value="{_field(entry, 'focus_tickers')}" placeholder="{'例如：NVDA, AMD, 301516.SZ；AI算力、机器人' if lang == 'zh' else 'Example: NVDA, AMD, 301516.SZ; AI infra, robotics'}" /></label>
                      <label class="wide">{'每日操作计划' if lang == 'zh' else 'Daily Trade Plan'}<textarea class="tall" name="daily_plan" placeholder="{'明天只做什么？什么条件触发？最大仓位多少？' if lang == 'zh' else 'What will I do tomorrow? Trigger? Max size?'}">{_field(entry, 'daily_plan')}</textarea></label>
                      <label class="wide">{'盘后执行回顾' if lang == 'zh' else 'Execution Review'}<textarea class="tall" name="execution_review" placeholder="{'今天是否按计划执行？有没有追高、摊平、过度交易？' if lang == 'zh' else 'Did I follow the plan? Any chasing, averaging down, overtrading?'}">{_field(entry, 'execution_review')}</textarea></label>
                      <label>{'做对了什么' if lang == 'zh' else 'What Worked'}<textarea name="what_worked" placeholder="{'例如：等回踩，没有追；止盈执行到位。' if lang == 'zh' else 'Example: waited for pullback; took profit as planned.'}">{_field(entry, 'what_worked')}</textarea></label>
                      <label>{'做错了什么' if lang == 'zh' else 'What Failed'}<textarea name="what_failed" placeholder="{'例如：买点太急；仓位太散；止损犹豫。' if lang == 'zh' else 'Example: rushed entry; too fragmented; hesitated on stop.'}">{_field(entry, 'what_failed')}</textarea></label>
                      <label>{'复盘结论' if lang == 'zh' else 'Lessons'}<textarea name="lessons" placeholder="{'把今天的错误变成一句规则。' if lang == 'zh' else 'Turn today’s mistake into one rule.'}">{_field(entry, 'lessons')}</textarea></label>
                      <label>{'明日改进动作' if lang == 'zh' else 'Tomorrow Improvement'}<textarea name="tomorrow_plan" placeholder="{'明天只改一件事，例如：不开盘前15分钟追涨。' if lang == 'zh' else 'Improve one thing tomorrow, e.g. no chasing in first 15 minutes.'}">{_field(entry, 'tomorrow_plan')}</textarea></label>
                      <label class="wide">{'风险备注' if lang == 'zh' else 'Risk Notes'}<textarea name="risk_notes" placeholder="{'哪些仓位必须减？哪些票只能观察？哪些事件要避开？' if lang == 'zh' else 'What must be trimmed? What is watch-only? What events to avoid?'}">{_field(entry, 'risk_notes')}</textarea></label>
                    </div>
                    <div class="actions">
                      <button type="submit">{'保存复盘' if lang == 'zh' else 'Save Journal'}</button>
                      <a class="pill" href="/ai-chat?lang={lang}">{'用 AI 辅助复盘' if lang == 'zh' else 'Review with AI'}</a>
                    </div>
                  </form>
                  <form method="post" action="/review-journal/delete?lang={lang}" class="actions" onsubmit="return confirm('{'确认删除这条复盘？' if lang == 'zh' else 'Delete this journal entry?'}');">
                    <input type="hidden" name="journal_date" value="{_field(entry, 'journal_date')}" />
                    <button class="danger" type="submit">{'删除这条' if lang == 'zh' else 'Delete Entry'}</button>
                  </form>
                </section>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("", response_class=HTMLResponse)
def review_journal_page(request: Request, saved: int = 0, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/review-journal")
    return _render_page(request, db, saved=bool(saved))


@router.post("/save")
def save_review_journal(
    request: Request,
    journal_date: str = Form(""),
    market_scope: str = Form("ALL"),
    emotion: str = Form("calm"),
    discipline_score: str = Form("3"),
    focus_tickers: str = Form(""),
    daily_plan: str = Form(""),
    execution_review: str = Form(""),
    what_worked: str = Form(""),
    what_failed: str = Form(""),
    lessons: str = Form(""),
    tomorrow_plan: str = Form(""),
    risk_notes: str = Form(""),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/review-journal")
    lang = resolve_request_lang(request, default="zh")
    entry = save_review_entry(
        db,
        {
            "journal_date": journal_date,
            "market_scope": market_scope,
            "emotion": emotion,
            "discipline_score": discipline_score,
            "focus_tickers": focus_tickers,
            "daily_plan": daily_plan,
            "execution_review": execution_review,
            "what_worked": what_worked,
            "what_failed": what_failed,
            "lessons": lessons,
            "tomorrow_plan": tomorrow_plan,
            "risk_notes": risk_notes,
        },
    )
    return RedirectResponse(
        url=f"/review-journal?lang={lang}&date={quote(str(entry.get('journal_date') or ''))}&saved=1",
        status_code=303,
    )


@router.post("/delete")
def delete_review_journal(
    request: Request,
    journal_date: str = Form(""),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/review-journal")
    lang = resolve_request_lang(request, default="zh")
    delete_review_entry(db, journal_date)
    return RedirectResponse(url=f"/review-journal?lang={lang}", status_code=303)
