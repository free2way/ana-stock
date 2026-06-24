from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.ai_chat import AI_CHAT_PROMPT_TEMPLATES, ask_ai_chat, load_ai_chat_config, masked_api_key
from app.services.auth import is_authenticated, login_redirect
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html


router = APIRouter(prefix="/ai-chat", tags=["ai-chat"])


def _answer_html(answer: str) -> str:
    escaped = html.escape(str(answer or "").strip())
    return escaped.replace("\n", "<br />")


def _template_cards(lang: str) -> str:
    cards: list[str] = []
    for item in AI_CHAT_PROMPT_TEMPLATES:
        title = item["title_zh"] if lang == "zh" else item["title_en"]
        prompt = item["prompt_zh"] if lang == "zh" else item["prompt_en"]
        cards.append(
            f"""
            <button class="template-card" type="button" data-prompt="{html.escape(prompt, quote=True)}">
              <span>{html.escape(title)}</span>
              <small>{html.escape(prompt)}</small>
            </button>
            """
        )
    return "".join(cards)


def _render_ai_chat_page(
    *,
    request: Request,
    db: Session,
    question: str = "",
    result: dict | None = None,
) -> str:
    lang = resolve_request_lang(request, default="zh")
    nav_html = render_workspace_nav_html(lang=lang, active_key="ai_chat")
    config = load_ai_chat_config(db)
    configured_label = (
        "已配置" if config.is_configured and lang == "zh" else
        "未配置" if lang == "zh" else
        "Configured" if config.is_configured else "Not configured"
    )
    answer_block = ""
    if result:
        status = str(result.get("status") or "unknown")
        answer_block = f"""
        <section class="card answer-card {html.escape(status)}">
          <div class="eyebrow">{'AI 回答' if lang == 'zh' else 'AI Answer'}</div>
          <div class="answer-meta">
            <span>{html.escape(str(result.get('provider') or config.provider_name or '-'))}</span>
            <span>{html.escape(str(result.get('model') or config.model or '-'))}</span>
            <span>{html.escape(status)}</span>
          </div>
          <div class="answer-text">{_answer_html(str(result.get('answer') or '-'))}</div>
        </section>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'AI 问答' if lang == 'zh' else 'AI Q&A'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --warn:#fbbf24; --danger:#fb7185; --blue:#60a5fa; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(96,165,250,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.14), transparent 26%),linear-gradient(180deg,#08111a 0%,#071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .hero {{ display:grid; grid-template-columns:minmax(0,1.35fr) minmax(280px,0.7fr); gap:12px; margin-bottom:12px; }}
          .workspace {{ display:grid; grid-template-columns:minmax(0,1.1fr) minmax(310px,0.9fr); gap:12px; }}
          h1 {{ margin:8px 0 8px; font-size:34px; line-height:1.04; letter-spacing:-0.04em; }}
          .topbar {{ display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }}
          .pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--ink); font-size:13px; font-weight:850; }}
          .status-chip {{ display:inline-flex; padding:7px 10px; border-radius:999px; border:1px solid rgba(255,255,255,0.08); background:rgba(61,217,182,0.12); color:#bbf7d0; font-size:12px; font-weight:900; }}
          .status-chip.missing {{ background:rgba(251,191,36,0.12); color:#fde68a; }}
          .prompt-box {{ min-height:150px; line-height:1.5; }}
          .form-actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }}
          .form-actions button {{ width:auto; min-width:150px; }}
          .template-grid {{ display:grid; gap:8px; }}
          .template-card {{ width:100%; text-align:left; padding:10px; border-radius:10px; border:1px solid var(--line); background:rgba(21,34,49,0.82); color:var(--ink); cursor:pointer; }}
          .template-card:hover {{ border-color:rgba(61,217,182,0.42); background:rgba(24,42,57,0.92); }}
          .template-card span {{ display:block; font-weight:900; margin-bottom:4px; font-size:13px; }}
          .template-card small {{ display:block; color:var(--muted); line-height:1.35; font-size:11px; }}
          .answer-card {{ border-color:rgba(61,217,182,0.26); }}
          .answer-card.error,.answer-card.not_configured {{ border-color:rgba(251,191,36,0.30); }}
          .answer-meta {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }}
          .answer-meta span {{ display:inline-flex; padding:6px 9px; border-radius:999px; background:rgba(255,255,255,0.06); color:var(--muted); font-size:12px; font-weight:800; }}
          .answer-text {{ color:var(--ink); line-height:1.72; font-size:14px; white-space:normal; }}
          .guide-list {{ display:grid; gap:8px; }}
          .guide-list div {{ padding:9px; border:1px solid rgba(255,255,255,0.06); border-radius:10px; background:rgba(15,24,35,0.72); }}
          @media (max-width: 1120px) {{ .hero,.workspace {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'AI 问答' if lang == 'zh' else 'AI Q&A'}</h1>
              <p>{'把持仓、自选、AI日报候选和模型评测作为上下文，向大模型提股票相关问题。' if lang == 'zh' else 'Ask market questions with portfolio, watchlist, AI report, and model guidance as context.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'提示：AI 只能辅助复盘和形成检查清单，不能替代仓位纪律。' if lang == 'zh' else 'Tip: AI can help review and build checklists, but it cannot replace sizing discipline.'}</div>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="topbar">
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                  <a class="pill" href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                  <a class="pill" href="/settings/ai-chat?lang={lang}">{'AI 设置' if lang == 'zh' else 'AI Settings'}</a>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                  <a class="pill" href="/ai-chat?lang=en">EN</a>
                  <a class="pill" href="/ai-chat?lang=zh">中文</a>
                </div>
              </div>
              <section class="hero">
                <article class="card">
                  <span class="eyebrow">{'股票研究助手' if lang == 'zh' else 'Stock Research Assistant'}</span>
                  <h1>{'用你的应用数据问 AI，而不是空聊' if lang == 'zh' else 'Ask AI with your app data, not in a vacuum'}</h1>
                  <p class="lead">{'页面会把当前持仓、自选、AI日报候选和模型指导摘要作为上下文发给模型。适合做盘前计划、持仓复核、模型选择和复盘。' if lang == 'zh' else 'This page sends your current portfolio, watchlist, AI report candidates, and model guidance as context. Use it for premarket planning, risk review, model selection, and post-trade review.'}</p>
                </article>
                <article class="card">
                  <span class="eyebrow">{'配置状态' if lang == 'zh' else 'Config'}</span>
                  <div class="guide-list">
                    <div><span class="status-chip{' missing' if not config.is_configured else ''}">{configured_label}</span></div>
                    <div><strong>{html.escape(config.provider_name)}</strong><div class="muted">{html.escape(config.model or '-')}</div></div>
                    <div><strong>{'API Key' if lang == 'zh' else 'API Key'}</strong><div class="muted">{html.escape(masked_api_key(config) or ('未设置' if lang == 'zh' else 'Missing'))}</div></div>
                  </div>
                </article>
              </section>
              <section class="workspace">
                <div>
                  <section class="card">
                    <div class="eyebrow">{'提问' if lang == 'zh' else 'Ask'}</div>
                    <form method="post" action="/ai-chat?lang={lang}">
                      <textarea id="question" class="prompt-box" name="question" placeholder="{'例如：请根据今天AI日报和我的持仓，给我明天的操作计划。' if lang == 'zh' else 'Example: Based on today’s AI report and my portfolio, create tomorrow’s trade plan.'}">{html.escape(question)}</textarea>
                      <div class="form-actions">
                        <button type="submit">{'发送给 AI' if lang == 'zh' else 'Ask AI'}</button>
                        <a class="pill" href="/settings/ai-chat?lang={lang}">{'配置 Provider' if lang == 'zh' else 'Configure Provider'}</a>
                      </div>
                    </form>
                  </section>
                  {answer_block}
                </div>
                <aside class="card">
                  <div class="eyebrow">{'高质量提示词模板' if lang == 'zh' else 'Prompt Templates'}</div>
                  <p class="muted">{'点击模板会自动填入左侧输入框，你可以再补充股票代码或具体问题。' if lang == 'zh' else 'Click a template to fill the prompt, then add tickers or specifics.'}</p>
                  <div class="template-grid">{_template_cards(lang)}</div>
                </aside>
              </section>
            </div>
          </main>
        </div>
        <script>
          document.querySelectorAll('.template-card').forEach((button) => {{
            button.addEventListener('click', () => {{
              const input = document.getElementById('question');
              input.value = button.dataset.prompt || '';
              input.focus();
              input.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
            }});
          }});
        </script>
      </body>
    </html>
    """


@router.get("", response_class=HTMLResponse)
def ai_chat_page(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/ai-chat")
    return _render_ai_chat_page(request=request, db=db)


@router.post("", response_class=HTMLResponse)
def ai_chat_ask(
    request: Request,
    question: str = Form(""),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/ai-chat")
    lang = resolve_request_lang(request, default="zh")
    result = ask_ai_chat(db=db, question=question, lang=lang)
    return _render_ai_chat_page(request=request, db=db, question=question, result=result)
