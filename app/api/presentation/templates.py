from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"


@lru_cache(maxsize=1)
def template_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(template_name: str, **context: object) -> str:
    return template_environment().get_template(template_name).render(**context)
