from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest import TestCase

from app.services.stock_selection.schemas import (
    InsightDirection,
    PortfolioState,
    ResearchProtocol,
    SelectionInsight,
    UniverseSnapshot,
)


UTC = timezone.utc


class StockSelectionContractTests(TestCase):
    def test_research_protocol_requires_purge_at_least_horizon_and_hashes_stably(self) -> None:
        protocol = ResearchProtocol(
            protocol_version="cn_3d_v1",
            market="CN",
            horizon_days=3,
            purge_sessions=3,
            universe_version="cn_universe_v1",
        )
        same_protocol = ResearchProtocol(
            protocol_version="cn_3d_v1",
            market="CN",
            horizon_days=3,
            purge_sessions=3,
            universe_version="cn_universe_v1",
        )
        self.assertEqual(protocol.content_hash(), same_protocol.content_hash())

        with self.assertRaisesRegex(ValueError, "at least horizon_days"):
            ResearchProtocol(
                protocol_version="bad",
                market="CN",
                horizon_days=5,
                purge_sessions=3,
            )

    def test_selection_insight_enforces_post_close_to_next_session_order(self) -> None:
        close = datetime(2026, 8, 13, 7, 0, tzinfo=UTC)
        insight = SelectionInsight(
            insight_id="insight-1",
            market="CN",
            ticker="000001.SZ",
            data_cutoff_at=close,
            generated_at=close + timedelta(minutes=5),
            effective_from=close + timedelta(hours=17, minutes=30),
            expires_at=close + timedelta(days=3),
            horizon_days=3,
            direction=InsightDirection.UP,
            raw_score=0.8,
            cross_sectional_rank=0.99,
            source_model="factor_ridge",
            model_version="factor_ridge_v1",
            universe_version="cn_universe_v1",
        )
        self.assertEqual("000001.SZ", insight.ticker)

        with self.assertRaisesRegex(ValueError, "time order"):
            SelectionInsight(
                insight_id="lookahead",
                market="CN",
                ticker="000001.SZ",
                data_cutoff_at=close + timedelta(days=1),
                generated_at=close,
                effective_from=close + timedelta(hours=1),
                expires_at=close + timedelta(days=3),
                horizon_days=3,
                direction=InsightDirection.UP,
                raw_score=0.8,
                cross_sectional_rank=0.99,
                source_model="bad",
                model_version="bad-v1",
                universe_version="cn_universe_v1",
            )

    def test_universe_exclusions_and_portfolio_accounting_fail_closed(self) -> None:
        source_as_of = datetime(2026, 8, 13, 7, 0, tzinfo=UTC)
        excluded = UniverseSnapshot(
            snapshot_id="universe-1",
            market="US",
            trade_date=date(2026, 8, 12),
            ticker="DELISTED",
            included=False,
            exclusion_reason_codes=("delisted",),
            source_as_of=source_as_of,
            universe_version="us_universe_v1",
        )
        self.assertFalse(excluded.included)

        with self.assertRaisesRegex(ValueError, "at least one reason"):
            UniverseSnapshot(
                snapshot_id="universe-2",
                market="US",
                trade_date=date(2026, 8, 12),
                ticker="MISSING-REASON",
                included=False,
                source_as_of=source_as_of,
                universe_version="us_universe_v1",
            )

        state = PortfolioState(
            state_id="state-1",
            strategy_run_id="run-1",
            event_time=source_as_of,
            cash=60.0,
            position_market_value=40.0,
            nav=100.0,
            gross_exposure=0.4,
            net_exposure=0.4,
        )
        self.assertEqual(100.0, state.nav)

        with self.assertRaisesRegex(ValueError, "accounting identity"):
            PortfolioState(
                state_id="state-bad",
                strategy_run_id="run-1",
                event_time=source_as_of,
                cash=60.0,
                position_market_value=40.0,
                nav=101.0,
                gross_exposure=0.4,
                net_exposure=0.4,
            )
