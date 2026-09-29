#!/usr/bin/env python3
"""Render the existing synthetic privacy regression fixture without DB/network."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Fixture already exists")
    with ExitStack() as guards:
        for target in ("psycopg.connect", "psycopg.Connection.connect", "socket.socket.connect", "socket.socket.connect_ex"):
            guards.enter_context(patch(target, side_effect=AssertionError("Synthetic fixture must not connect externally")))
        from tests.test_portfolio_privacy import PortfolioPrivacyTests
        from app.api.routes import portfolio
        original = portfolio.portfolio_page
        rendered = []

        def capture(*args, **kwargs):
            html = original(*args, **kwargs)
            rendered.append(html)
            return html

        with patch.object(portfolio, "portfolio_page", side_effect=capture):
            PortfolioPrivacyTests("test_portfolio_page_masks_cn_and_us_values_until_eye_toggle").test_portfolio_page_masks_cn_and_us_values_until_eye_toggle()
        if len(rendered) != 1:
            raise RuntimeError("Expected exactly one synthetic portfolio page")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        handle.write(rendered[0])
    print(args.output.resolve())


if __name__ == "__main__":
    main()
