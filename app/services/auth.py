from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse


AUTH_COOKIE_NAME = "pqw_auth"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin1234"


def is_authenticated(request: Request) -> bool:
    return request.cookies.get(AUTH_COOKIE_NAME) == DEFAULT_USERNAME


def login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='/?=&')}", status_code=303)

