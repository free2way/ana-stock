"""P1 regression: portfolio-book sells are atomic, idempotent and finite-validated."""
from __future__ import annotations

import threading
from unittest.mock import patch

from tests.postgres_safety import ApplicationPostgresTestCase

from app.services.portfolio_book import (
    PORTFOLIO_TRADE_LOG_KEY,
    load_portfolio_positions,
    load_portfolio_trades,
    sell_portfolio_position,
    upsert_portfolio_position,
)
from app.services.repository import AppSettingRepository


class PortfolioBookAtomicityTests(ApplicationPostgresTestCase):
    def _seed(self, *, ticker: str = "ASTS", quantity: float = 100.0) -> None:
        upsert_portfolio_position(
            {"ticker": ticker, "market": "US", "quantity": quantity, "cost_basis": 10.0, "fee": 0.0}
        )

    def _quantity(self, ticker: str = "ASTS") -> float:
        return next(item["quantity"] for item in load_portfolio_positions() if item["ticker"] == ticker)

    def test_failed_trade_log_write_rolls_back_the_position_deduction(self) -> None:
        self._seed()
        original_set = AppSettingRepository.set

        def flaky_set(self, key, value, *, commit: bool = True):
            if key == PORTFOLIO_TRADE_LOG_KEY:
                raise RuntimeError("simulated trade-log write failure")
            return original_set(self, key, value, commit=commit)

        with patch.object(AppSettingRepository, "set", flaky_set):
            with self.assertRaises(RuntimeError):
                sell_portfolio_position(
                    {"ticker": "ASTS", "quantity": 10, "price": 11.0, "fee": 0.0}
                )

        # No half-booked sell: the 100 shares must be intact and no trade logged.
        self.assertEqual(100.0, self._quantity())
        self.assertEqual([], load_portfolio_trades())

    def test_sell_rejects_non_finite_and_non_positive_numbers_without_state_change(self) -> None:
        for field, payload in (
            ("quantity", {"quantity": float("nan")}),
            ("quantity", {"quantity": float("inf")}),
            ("quantity", {"quantity": 0}),
            ("quantity", {"quantity": -5}),
            ("price", {"price": float("nan")}),
            ("price", {"price": float("inf")}),
            ("fee", {"fee": float("nan")}),
            ("fee", {"fee": -1}),
        ):
            with self.subTest(field=field, payload=payload):
                self._seed()
                request = {"ticker": "ASTS", "quantity": 10, "price": 11.0, "fee": 0.0}
                request.update(payload)
                with self.assertRaises(ValueError):
                    sell_portfolio_position(request)
                # Older records / comparisons must not silently drop the position.
                self.assertEqual(100.0, self._quantity())
                self.assertEqual([], load_portfolio_trades())

    def test_buy_rejects_non_finite_and_non_positive_numbers(self) -> None:
        for payload in (
            {"quantity": float("nan"), "cost_basis": 10.0},
            {"quantity": 0, "cost_basis": 10.0},
            {"quantity": 100, "cost_basis": float("inf")},
            {"quantity": 100, "cost_basis": -1.0},
            {"quantity": 100, "cost_basis": 10.0, "fee": float("nan")},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    upsert_portfolio_position({"ticker": "ASTS", "market": "US", **payload})
                self.assertEqual([], load_portfolio_positions())

    def test_repeated_sell_with_same_idempotency_key_deducts_once(self) -> None:
        self._seed()
        first = sell_portfolio_position(
            {"ticker": "ASTS", "quantity": 10, "price": 11.0, "idempotency_key": "submit-1"}
        )
        replay = sell_portfolio_position(
            {"ticker": "ASTS", "quantity": 10, "price": 11.0, "idempotency_key": "submit-1"}
        )

        self.assertEqual(90.0, first["trade"]["remaining_quantity"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["trade"]["id"], replay["trade"]["id"])
        self.assertEqual(90.0, self._quantity())
        self.assertEqual(1, len(load_portfolio_trades()))

    def test_concurrent_sells_are_serialized_by_the_book_lock(self) -> None:
        self._seed(quantity=100.0)
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []

        def worker(key: str) -> None:
            barrier.wait()
            try:
                outcome = sell_portfolio_position(
                    {"ticker": "ASTS", "quantity": 60, "price": 11.0, "idempotency_key": key}
                )
                results.append(("ok", outcome["trade"]["remaining_quantity"]))
            except ValueError as exc:
                results.append(("error", str(exc)))

        threads = [threading.Thread(target=worker, args=(f"submit-{i}",)) for i in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Exactly one sell applies; the second sees the post-deduction holding.
        self.assertEqual(1, sum(1 for status, _ in results if status == "ok"))
        self.assertEqual(1, sum(1 for status, _ in results if status == "error"))
        self.assertEqual(40.0, self._quantity())
        self.assertEqual(1, len(load_portfolio_trades()))
        self.assertGreaterEqual(self._quantity(), 0.0)


if __name__ == "__main__":
    import unittest

    unittest.main()
