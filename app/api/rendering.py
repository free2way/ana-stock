"""Shared, side-effect-free presentation fragments (no financial decisions)."""


def render_daily_change_chip(value: float | None) -> str:
    if value is None:
        return "<span class='muted'>-</span>"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "<span class='muted'>-</span>"
    bg = "rgba(148,163,184,0.12)"
    fg = "#cbd5e1"
    if numeric > 0:
        bg = "rgba(22,163,74,0.16)"
        fg = "#4ade80"
    elif numeric < 0:
        bg = "rgba(220,38,38,0.16)"
        fg = "#f87171"
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;white-space:nowrap;'>{numeric:+.2f}%</span>"
    )


def compact_text(value: str | None, limit: int = 28) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def mini_trend_bars(values: list[int], *, lang: str) -> str:
    normalized = [max(0, int(value)) for value in values]
    if not normalized:
        label = "暂无趋势" if lang == "zh" else "No trend"
        return f"<div class='mini-trend empty'><span>{label}</span></div>"
    top = max(normalized) or 1
    bars = "".join(
        f"<span style='height:{max(16, int((value / top) * 100))}%;'></span>"
        for value in normalized
    )
    return f"<div class='mini-trend'>{bars}</div>"
