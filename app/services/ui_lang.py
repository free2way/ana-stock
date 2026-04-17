from fastapi import Request


LANG_COOKIE_NAME = "pqw_lang"


def normalize_lang(value: str | None, default: str = "en") -> str:
    return "zh" if str(value or "").strip().lower() == "zh" else default


def resolve_request_lang(request: Request, *, default: str = "en") -> str:
    query_value = request.query_params.get("lang")
    if query_value is not None:
        return normalize_lang(query_value, default=default)
    cookie_value = request.cookies.get(LANG_COOKIE_NAME)
    return normalize_lang(cookie_value, default=default)
