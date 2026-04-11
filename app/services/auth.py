import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.core.config import get_settings


AUTH_COOKIE_NAME = "pqw_auth"


def _sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    configured_password = settings.auth_password
    if not configured_password:
        return False
    return hmac.compare_digest(username, settings.auth_username) and hmac.compare_digest(password, configured_password)


def build_auth_cookie_value(username: str | None = None, issued_at: int | None = None) -> str:
    settings = get_settings()
    if not settings.auth_secret:
        raise ValueError("Authentication secret is not configured.")
    cookie_username = username or settings.auth_username
    issued = issued_at or int(time.time())
    payload = f"{cookie_username}:{issued}"
    signature = _sign_payload(payload, settings.auth_secret)
    return f"{payload}:{signature}"


def _verify_auth_cookie(cookie_value: str | None) -> bool:
    settings = get_settings()
    if not cookie_value or not settings.auth_secret:
        return False
    try:
        username, issued_at_text, signature = cookie_value.split(":", 2)
        issued_at = int(issued_at_text)
    except (TypeError, ValueError):
        return False
    if not hmac.compare_digest(username, settings.auth_username):
        return False
    payload = f"{username}:{issued_at}"
    expected_signature = _sign_payload(payload, settings.auth_secret)
    if not hmac.compare_digest(signature, expected_signature):
        return False
    max_age_seconds = max(60, int(settings.auth_cookie_max_age_seconds))
    if issued_at < int(time.time()) - max_age_seconds:
        return False
    return True


def is_authenticated(request: Request) -> bool:
    return _verify_auth_cookie(request.cookies.get(AUTH_COOKIE_NAME))


def login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='/?=&')}", status_code=303)
