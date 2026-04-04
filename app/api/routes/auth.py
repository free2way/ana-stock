from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.auth import AUTH_COOKIE_NAME, DEFAULT_PASSWORD, DEFAULT_USERNAME


router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(next: str = Query("/watchlist"), error: str | None = None) -> str:
    error_html = (
        f"<div class='error'>{error}</div>"
        if error
        else ""
    )
    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Login</title>
        <style>
          :root {{
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background:
              radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
              radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
              var(--bg);
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
          }}
          .card {{
            width: min(420px, calc(100vw - 32px));
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 24px;
            box-shadow: 0 16px 40px rgba(31, 41, 55, 0.08);
          }}
          .eyebrow {{
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: #dff5ef;
            color: #0f766e;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin-bottom: 12px;
          }}
          h1 {{ margin: 0 0 8px; font-size: 34px; }}
          p {{ margin: 0 0 18px; color: var(--muted); line-height: 1.6; }}
          .stack {{ display: grid; gap: 12px; }}
          input, button {{
            width: 100%;
            border-radius: 12px;
            border: 1px solid var(--line);
            padding: 12px 14px;
            font: inherit;
            background: #fff;
          }}
          button {{
            background: var(--accent);
            color: #fff;
            border-color: var(--accent);
            font-weight: 700;
          }}
          .error {{
            margin-bottom: 14px;
            padding: 12px 14px;
            border-radius: 12px;
            background: #fee2e2;
            color: #991b1b;
            font-weight: 700;
          }}
          .hint {{ margin-top: 14px; font-size: 13px; color: var(--muted); }}
        </style>
      </head>
      <body>
        <main class="card">
          <div class="eyebrow">Secure Access</div>
          <h1>Sign In</h1>
          <p>Use the default credentials to access the local app.</p>
          {error_html}
          <form class="stack" action="/login" method="post">
            <input type="hidden" name="next" value="{next}" />
            <input type="text" name="username" placeholder="Username" value="{DEFAULT_USERNAME}" required />
            <input type="password" name="password" placeholder="Password" value="{DEFAULT_PASSWORD}" required />
            <button type="submit">Login</button>
          </form>
          <div class="hint">Default credentials: <strong>{DEFAULT_USERNAME}</strong> / <strong>{DEFAULT_PASSWORD}</strong></div>
        </main>
      </body>
    </html>
    """


@router.post("/login")
def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/watchlist"),
) -> RedirectResponse:
    if username == DEFAULT_USERNAME and password == DEFAULT_PASSWORD:
        response = RedirectResponse(url=next or "/dashboard", status_code=303)
        response.set_cookie(AUTH_COOKIE_NAME, DEFAULT_USERNAME, httponly=True, samesite="lax")
        return response
    return RedirectResponse(url=f"/login?next={next}&error=Invalid+username+or+password", status_code=303)


@router.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response
