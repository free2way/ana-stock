"""Bilingual (zh/en) text selection for the presentation layer.

All user-facing pages are bilingual. Route modules previously interleaved
hundreds of inline ternaries (``'中文' if lang == 'zh' else 'English'``),
which made the presentation logic hard to read and impossible to audit.
``t`` is the single seam for that decision: it is side-effect free, never
returns None, and always returns one of the two supplied literals.

Guidelines:
- Always pass the zh variant first, the en variant second.
- Both arguments must be plain literals -- no markup assumptions, no
  fallback logic (missing translations must be visible, not hidden).
- For longer control-flow differences (plurals, lists, layout), keep an
  ``if lang == 'zh':`` block in the caller; ``t`` is for inline text.
"""

from __future__ import annotations


def t(lang: str, zh: str, en: str) -> str:
    """Return the zh variant when ``lang`` is ``'zh'``, otherwise the en variant."""
    return zh if lang == "zh" else en


__all__ = ["t"]
