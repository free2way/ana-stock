"""Prediction domain repositories (signals, details, explanations, trade plans, artifacts, live publication)."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import delete, desc, func, insert, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import (
    LivePrediction,
    ModelEvaluation,
    ModelRun,
    Prediction,
    PredictionArtifact,
    PredictionDetail,
    PredictionExplanation,
    PredictionTradePlan,
    Symbol,
)
from app.services.market_context import load_market_context_snapshot
from app.services.market_storage_routing import (
    enabled_physical_markets,
    legacy_mirror_write_enabled,
    physical_fact_write_markets,
    physical_hot_prediction_models,
    physical_live_prediction_model,
    physical_only_cutover_active,
    physical_prediction_trade_plan_model,
)
from app.services.prediction_artifacts import (
    read_prediction_artifact_rows,
    read_prediction_explanation_artifact_rows,
)
from app.services.time_utils import app_now
from app.services.tradability_filter import evaluate_candidate_tradability

from app.services.repositories.shared import (
    PRODUCTION_SIGNAL_MODEL_TYPES,
    _assert_legacy_prediction_write_allowed,
    chunked_ids,
    chunked_rows,
    ticker_query_candidates,
    utc_now_iso,
)

if TYPE_CHECKING:
    from app.services.stock_selection.promotion_enforcement import (
        ServingPromotionDecision,
    )


def _promotion_enforcement():
    """Lazy import: the stock_selection package imports this module's parent."""

    from app.services.stock_selection import promotion_enforcement

    return promotion_enforcement


class PredictionRepository:
    def __init__(self, db: Session, *, cold_reads_enabled: bool | None = None) -> None:
        self.db = db
        self.cold_reads_enabled = (
            bool(get_settings().prediction_cold_reads_enabled)
            if cold_reads_enabled is None
            else bool(cold_reads_enabled)
        )
        self._market_context_cache: dict[str, dict] = {}

    # How many of the newest candidate runs are inspected when picking the
    # serving/recommendation run. A run whose unified promotion gate REJECTs is
    # skipped so it can never become the served champion; the window bounds the
    # gate work per read.
    _SERVING_RUN_WINDOW = 5

    def _select_serving_run(
        self, candidate_run_ids: list[int]
    ) -> tuple[int | None, ServingPromotionDecision | None]:
        """Pick the newest run that may serve, honouring the promotion gate.

        Returns ``(run_id, decision)``. ``run_id`` is ``None`` when enforcement
        withheld every candidate (all REJECT); the decision carries the marking
        for the caller.
        """

        first_decision: ServingPromotionDecision | None = None
        for run_id in candidate_run_ids:
            run = self.db.get(ModelRun, int(run_id))
            if run is None:
                continue
            decision = _promotion_enforcement().assess_run_for_serving(run, db=self.db)
            if first_decision is None:
                first_decision = decision
            if not decision.blocked:
                return int(run_id), decision
        return None, first_decision

    def _mark_run_predictions(
        self, rows: list[dict], *, run_id: int
    ) -> list[dict]:
        """Label a run-scoped read; never filters, so research stays queryable."""

        if not rows:
            return rows
        run = self.db.get(ModelRun, int(run_id))
        if run is None:
            return rows
        decision = _promotion_enforcement().assess_run_for_serving(
            run, db=self.db, log_warning=False
        )
        return _promotion_enforcement().annotate_rows_with_promotion(rows, decision)

    @staticmethod
    def _compute_action_bucket(candidate: dict) -> str:
        status = str(candidate.get("tradability_status") or "").upper()
        signal_label = str(candidate.get("signal_label") or "").strip().upper()
        if status == "BLOCKED":
            return "blocked"
        if status in {"REVIEW", "DEFER"} or signal_label in {"SELL", "STRONG_SELL"}:
            return "risk_reduction"
        if status == "READY":
            return "action_queue"
        return "monitor"

    @staticmethod
    def _compute_action_label(candidate: dict) -> str:
        bucket = PredictionRepository._compute_action_bucket(candidate)
        if bucket == "blocked":
            return "do_not_trade"
        if bucket == "risk_reduction":
            return "review_or_trim"
        if bucket == "action_queue":
            return "ready_to_trade"
        return "monitor_only"

    @staticmethod
    def _compute_target_weight(candidate: dict) -> float | None:
        status = str(candidate.get("tradability_status") or "").upper()
        if status == "BLOCKED":
            return None

        try:
            score = float(candidate.get("score")) if candidate.get("score") is not None else None
        except (TypeError, ValueError):
            score = None

        try:
            signal_strength = (
                float(candidate.get("signal_strength")) if candidate.get("signal_strength") is not None else None
            )
        except (TypeError, ValueError):
            signal_strength = None

        weight = 0.02
        if score is not None:
            if score >= 0.85:
                weight = 0.07
            elif score >= 0.75:
                weight = 0.05
            elif score >= 0.6:
                weight = 0.03

        if signal_strength is not None and signal_strength >= 85:
            weight += 0.01

        if status == "DEFER":
            weight = min(weight, 0.02)
        elif status == "REVIEW":
            weight = min(weight, 0.03)

        return round(min(weight, 0.1), 4)

    @staticmethod
    def _compute_priority(candidate: dict) -> int | None:
        status = str(candidate.get("tradability_status") or "").upper()
        try:
            score = float(candidate.get("score")) if candidate.get("score") is not None else None
        except (TypeError, ValueError):
            score = None

        if status == "BLOCKED":
            return 4
        if score is None:
            return 3
        if status == "READY" and score >= 0.8:
            return 1
        if status in {"READY", "REVIEW", "DEFER"}:
            return 2
        return 3

    def _build_signal_decision(self, candidate: dict) -> dict:
        market_code = str(candidate.get("market") or "").strip().upper()
        market_snapshot = None
        if market_code:
            if market_code not in self._market_context_cache:
                self._market_context_cache[market_code] = load_market_context_snapshot(self.db, market=market_code)
            market_snapshot = self._market_context_cache.get(market_code)
        decision = evaluate_candidate_tradability(candidate, market_snapshot=market_snapshot)
        payload = dict(candidate)
        payload.update(
            {
                "tradability_status": decision.tradability_status,
                "target_weight": self._compute_target_weight(
                    {**candidate, "tradability_status": decision.tradability_status}
                ),
                "priority": self._compute_priority(
                    {**candidate, "tradability_status": decision.tradability_status}
                ),
                "action_bucket": self._compute_action_bucket(
                    {
                        **candidate,
                        "tradability_status": decision.tradability_status,
                    }
                ),
                "action_label": self._compute_action_label(
                    {
                        **candidate,
                        "tradability_status": decision.tradability_status,
                    }
                ),
                "liquidity_bucket": decision.liquidity_bucket,
                "risk_flags": decision.risk_flags,
                "block_reason": decision.block_reason,
                "trade_readiness_score": decision.trade_readiness_score,
                "readiness_bucket": decision.readiness_bucket,
                "readiness_reason": decision.readiness_reason,
                "preferred_entry_style": decision.preferred_entry_style,
                "suggested_watch_action": decision.suggested_watch_action,
                "entry_trigger": decision.entry_trigger,
                "invalidation_condition": decision.invalidation_condition,
                "time_horizon": decision.time_horizon,
                "max_slippage_bps": decision.max_slippage_bps,
                "stop_loss_type": decision.stop_loss_type,
                "execution_note": decision.execution_note,
                "event_conflict": None,
                "suggested_participation_rate": decision.suggested_participation_rate,
            }
        )
        return payload

    def list_latest_signal_decisions(
        self,
        *,
        limit: int = 20,
        market: str | None = None,
        tradability: str | None = None,
    ) -> list[dict]:
        raw_candidates = self.list_latest_predictions_for_market(market=market, limit=max(limit * 3, 30))
        decisions = [self._build_signal_decision(candidate) for candidate in raw_candidates]

        normalized_tradability = str(tradability).upper() if tradability else None
        if normalized_tradability and normalized_tradability != "ALL":
            decisions = [
                item for item in decisions if str(item.get("tradability_status") or "").upper() == normalized_tradability
            ]

        decisions.sort(
            key=lambda item: (
                item.get("priority") if item.get("priority") is not None else 99,
                -(item.get("score") or 0),
                item.get("ticker") or "",
            )
        )
        return decisions[:limit]

    def list_latest_predictions_for_market(self, market: str | None, limit: int = 50) -> list[dict]:
        normalized_market = str(market or "").upper()
        live_results = self._list_live_predictions_for_market(normalized_market, limit=limit)
        if live_results:
            return live_results
        candidate_run_stmt = (
            select(ModelRun.id)
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
                select(Prediction.id)
                .where(Prediction.model_run_id == ModelRun.id)
                .limit(1)
                .exists(),
            )
        )
        if normalized_market and normalized_market != "ALL":
            # A multi-market run (``market == 'ALL'``) publishes predictions for
            # every market, so it must stay a candidate for a market-scoped read;
            # only single-market runs for other markets are excluded.
            candidate_run_stmt = candidate_run_stmt.where(
                ModelRun.market.in_([normalized_market, "ALL"])
            )
        candidate_run_ids = self.db.scalars(
            candidate_run_stmt.order_by(ModelRun.id.desc()).limit(self._SERVING_RUN_WINDOW)
        ).all()
        latest_model_run_id, serving_decision = self._select_serving_run(
            [int(row) for row in candidate_run_ids]
        )
        if latest_model_run_id is None:
            return []

        # Latest trade date is resolved per market: a multi-market run can carry
        # a newer date for one market than another, so scoping the max date to
        # the requested market keeps that market's newest rows instead of
        # returning nothing when the run-wide max date belongs to another market.
        latest_date_stmt = select(func.max(Prediction.trade_date)).where(
            Prediction.model_run_id == latest_model_run_id
        )
        if normalized_market and normalized_market != "ALL":
            latest_date_stmt = latest_date_stmt.join(
                Symbol, Symbol.id == Prediction.symbol_id
            ).where(Symbol.market == normalized_market)
        latest_date = self.db.scalar(latest_date_stmt)
        if latest_date is None:
            return []

        stmt = (
            select(Prediction, Symbol, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Prediction.model_run_id == latest_model_run_id)
            .where(Prediction.trade_date == latest_date)
            .order_by(desc(Prediction.score), Symbol.ticker.asc())
            .limit(limit)
        )
        if normalized_market and normalized_market != "ALL":
            stmt = stmt.where(Symbol.market == normalized_market)

        rows = self.db.execute(stmt).all()
        latest_evaluation = self.db.scalar(
            select(ModelEvaluation)
            .where(ModelEvaluation.model_run_id == latest_model_run_id)
            .where(ModelEvaluation.market == normalized_market if normalized_market in {"CN", "US"} else True)
            .order_by(ModelEvaluation.id.desc())
            .limit(1)
        )
        activation_status = str((latest_evaluation.activation_status if latest_evaluation else None) or "unverified")
        payload = [
            {
                "prediction_id": prediction.id,
                "model_run_id": prediction.model_run_id,
                "trade_date": prediction.trade_date,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
                "confidence": (detail.confidence if detail is not None else None),
                "signal_label": (detail.signal_label if detail is not None else None),
                "signal_strength": (detail.signal_strength if detail is not None else None),
                "expected_return_20d": (detail.expected_return_20d if detail is not None else None),
                "expected_drawdown_20d": (detail.expected_drawdown_20d if detail is not None else None),
                "model_reward_risk_ratio": (detail.model_reward_risk_ratio if detail is not None else None),
                "conviction_bucket": (detail.conviction_bucket if detail is not None else None),
                "position_size_hint": (detail.position_size_hint if detail is not None else None),
                "entry_style": (detail.entry_style if detail is not None else None),
                "percentile": (detail.percentile if detail is not None else None),
                "sector": symbol.sector,
                "industry": symbol.industry,
                "summary_text": (detail.summary_text if detail is not None else None),
                "model_activation_status": activation_status,
            }
            for prediction, symbol, detail in rows
        ]
        if serving_decision is not None:
            payload = _promotion_enforcement().annotate_rows_with_promotion(
                payload, serving_decision
            )
        return payload

    def _list_live_predictions_for_market(self, market: str, *, limit: int) -> list[dict]:
        if market not in physical_fact_write_markets():
            return []
        settings = get_settings()
        if (
            settings.market_physical_live_reads_enabled
            and market in enabled_physical_markets(settings.market_physical_live_markets)
        ):
            physical_results = self._list_live_predictions_from_table(
                market,
                table=physical_live_prediction_model(market),
                limit=limit,
            )
            if physical_results:
                return physical_results
        return self._list_live_predictions_from_table(
            market,
            table=LivePrediction,
            limit=limit,
        )

    def _list_live_predictions_from_table(
        self,
        market: str,
        *,
        table,
        limit: int,
    ) -> list[dict]:
        candidate_run_ids = self.db.scalars(
            select(ModelRun.id)
            .join(table, table.model_run_id == ModelRun.id)
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
                table.market == market,
            )
            .order_by(ModelRun.id.desc())
            .limit(self._SERVING_RUN_WINDOW)
        ).all()
        latest_run_id, serving_decision = self._select_serving_run(
            [int(row) for row in candidate_run_ids]
        )
        if latest_run_id is None:
            return []
        rows = self.db.execute(
            select(table, Symbol)
            .join(Symbol, Symbol.id == table.symbol_id)
            .where(
                table.model_run_id == int(latest_run_id),
                table.market == market,
            )
            .order_by(desc(table.score), Symbol.ticker.asc())
            .limit(max(1, int(limit)))
        ).all()
        latest_evaluation = self.db.scalar(
            select(ModelEvaluation)
            .where(
                ModelEvaluation.model_run_id == int(latest_run_id),
                ModelEvaluation.market == market,
            )
            .order_by(ModelEvaluation.id.desc())
            .limit(1)
        )
        activation_status = str((latest_evaluation.activation_status if latest_evaluation else None) or "unverified")
        payload = [
            {
                "prediction_id": None,
                "model_run_id": int(row.model_run_id),
                "trade_date": row.trade_date.isoformat(),
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "score": row.score,
                "rank_value": row.rank_value,
                "confidence": row.confidence,
                "signal_label": row.signal_label,
                "signal_strength": row.signal_strength,
                "expected_return_20d": row.expected_return_20d,
                "expected_drawdown_20d": row.expected_drawdown_20d,
                "model_reward_risk_ratio": row.model_reward_risk_ratio,
                "conviction_bucket": row.conviction_bucket,
                "position_size_hint": row.position_size_hint,
                "entry_style": row.entry_style,
                "percentile": row.percentile,
                "sector": symbol.sector,
                "industry": symbol.industry,
                "summary_text": row.summary_text,
                "model_activation_status": activation_status,
                "source_layer": str(table.__tablename__),
            }
            for row, symbol in rows
        ]
        return _promotion_enforcement().annotate_rows_with_promotion(
            payload, serving_decision
        )

    def list_predictions_for_run(
        self,
        run_id: int,
        *,
        market: str | None = None,
        tickers: list[str] | None = None,
        trade_date: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        normalized_market = str(market or "").upper()
        normalized_tickers = [str(ticker).strip().upper() for ticker in (tickers or []) if str(ticker).strip()]
        physical_results = self._list_physical_predictions_for_run(
            run_id=int(run_id),
            market=normalized_market,
            tickers=normalized_tickers,
            trade_date=trade_date,
            limit=limit,
        )
        if physical_results:
            return self._mark_run_predictions(physical_results, run_id=int(run_id))
        cold_artifact: PredictionArtifact | None = None

        effective_trade_date = trade_date
        if not effective_trade_date:
            latest_date_stmt = (
                select(func.max(Prediction.trade_date))
                .select_from(Prediction)
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .where(Prediction.model_run_id == run_id)
            )
            if normalized_market and normalized_market != "ALL":
                latest_date_stmt = latest_date_stmt.where(Symbol.market == normalized_market)
            if normalized_tickers:
                latest_date_stmt = latest_date_stmt.where(Symbol.ticker.in_(normalized_tickers))
            effective_trade_date = self.db.scalar(latest_date_stmt)
        if effective_trade_date is None and self.cold_reads_enabled:
            cold_artifact = self.db.scalar(
                select(PredictionArtifact).where(
                    PredictionArtifact.model_run_id == int(run_id),
                    PredictionArtifact.status == "verified",
                )
            )
            if cold_artifact is not None:
                effective_trade_date = cold_artifact.max_trade_date
        if effective_trade_date is None:
            return []

        latest_evaluation = self.db.scalar(
            select(ModelEvaluation)
            .where(ModelEvaluation.model_run_id == run_id)
            .where(ModelEvaluation.status.in_(("success", "partial")))
            .order_by(ModelEvaluation.id.desc())
            .limit(1)
        )
        activation_status = str((latest_evaluation.activation_status if latest_evaluation else None) or "unverified")

        stmt = (
            select(Prediction, Symbol, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Prediction.model_run_id == run_id)
            .where(Prediction.trade_date == effective_trade_date)
            .order_by(desc(Prediction.score), Symbol.ticker.asc())
        )
        if normalized_market and normalized_market != "ALL":
            stmt = stmt.where(Symbol.market == normalized_market)
        if normalized_tickers:
            stmt = stmt.where(Symbol.ticker.in_(normalized_tickers))
        if limit and limit > 0:
            stmt = stmt.limit(limit)

        rows = self.db.execute(stmt).all()
        hot_results = [
            {
                "prediction_id": prediction.id,
                "model_run_id": prediction.model_run_id,
                "trade_date": prediction.trade_date,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
                "confidence": (detail.confidence if detail is not None else None),
                "signal_label": (detail.signal_label if detail is not None else None),
                "signal_strength": (detail.signal_strength if detail is not None else None),
                "expected_return_5d": (detail.expected_return_5d if detail is not None else None),
                "expected_return_20d": (detail.expected_return_20d if detail is not None else None),
                "expected_drawdown_20d": (detail.expected_drawdown_20d if detail is not None else None),
                "model_reward_risk_ratio": (detail.model_reward_risk_ratio if detail is not None else None),
                "conviction_bucket": (detail.conviction_bucket if detail is not None else None),
                "position_size_hint": (detail.position_size_hint if detail is not None else None),
                "entry_style": (detail.entry_style if detail is not None else None),
                "percentile": (detail.percentile if detail is not None else None),
                "summary_text": (detail.summary_text if detail is not None else None),
                "sector": symbol.sector,
                "industry": symbol.industry,
                "model_activation_status": activation_status,
            }
            for prediction, symbol, detail in rows
        ]
        if hot_results:
            return self._mark_run_predictions(hot_results, run_id=int(run_id))
        if not self.cold_reads_enabled:
            return []
        if cold_artifact is None:
            cold_artifact = self.db.scalar(
                select(PredictionArtifact).where(
                    PredictionArtifact.model_run_id == int(run_id),
                    PredictionArtifact.status == "verified",
                )
            )
        if cold_artifact is None:
            return []
        try:
            cold_rows = read_prediction_artifact_rows(
                cold_artifact.artifact_path,
                trade_dates=[str(effective_trade_date)],
                include_details=True,
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return []
        symbol_ids = sorted({int(row["symbol_id"]) for row in cold_rows})
        symbol_rows = list(self.db.scalars(select(Symbol).where(Symbol.id.in_(symbol_ids))).all())
        symbols = {int(symbol.id): symbol for symbol in symbol_rows}
        cold_results: list[dict] = []
        for row in cold_rows:
            symbol = symbols.get(int(row["symbol_id"]))
            if symbol is None:
                continue
            if normalized_market and normalized_market != "ALL" and str(symbol.market or "").upper() != normalized_market:
                continue
            if normalized_tickers and str(symbol.ticker or "").upper() not in normalized_tickers:
                continue
            cold_results.append(
                {
                    "prediction_id": None,
                    "model_run_id": int(run_id),
                    "trade_date": str(row["trade_date"]),
                    "ticker": symbol.ticker,
                    "name": symbol.name,
                    "market": symbol.market,
                    "score": row.get("score"),
                    "rank_value": row.get("rank_value"),
                    "confidence": row.get("confidence"),
                    "signal_label": row.get("signal_label"),
                    "signal_strength": row.get("signal_strength"),
                    "expected_return_5d": row.get("expected_return_5d"),
                    "expected_return_20d": row.get("expected_return_20d"),
                    "expected_drawdown_20d": row.get("expected_drawdown_20d"),
                    "model_reward_risk_ratio": row.get("model_reward_risk_ratio"),
                    "conviction_bucket": row.get("conviction_bucket"),
                    "position_size_hint": row.get("position_size_hint"),
                    "entry_style": row.get("entry_style"),
                    "percentile": row.get("percentile"),
                    "summary_text": row.get("summary_text"),
                    "sector": symbol.sector,
                    "industry": symbol.industry,
                    "model_activation_status": activation_status,
                    "source_layer": "cold_parquet",
                }
            )
        cold_results.sort(key=lambda item: (-(float(item.get("score") or 0.0)), str(item.get("ticker") or "")))
        sliced = cold_results[: int(limit)] if limit and limit > 0 else cold_results
        return self._mark_run_predictions(sliced, run_id=int(run_id))

    def _list_physical_predictions_for_run(
        self,
        *,
        run_id: int,
        market: str,
        tickers: list[str],
        trade_date: str | None,
        limit: int | None,
    ) -> list[dict]:
        # Lightweight unit-test doubles do not expose a SQLAlchemy bind or the
        # physical tables. Production repositories always receive Session.
        if not isinstance(self.db, Session):
            return []
        if market not in physical_fact_write_markets():
            return []
        prediction_table, detail_table, _ = physical_hot_prediction_models(market)
        effective_trade_date: date | None
        if trade_date:
            try:
                effective_trade_date = date.fromisoformat(str(trade_date)[:10])
            except ValueError:
                return []
        else:
            latest_date_stmt = select(func.max(prediction_table.trade_date)).where(
                prediction_table.model_run_id == int(run_id)
            )
            if tickers:
                latest_date_stmt = latest_date_stmt.join(
                    Symbol, Symbol.id == prediction_table.symbol_id
                ).where(Symbol.ticker.in_(tickers))
            effective_trade_date = self.db.scalar(latest_date_stmt)
        if effective_trade_date is None:
            return []

        stmt = (
            select(prediction_table, Symbol, detail_table)
            .join(Symbol, Symbol.id == prediction_table.symbol_id)
            .outerjoin(
                detail_table,
                detail_table.prediction_id == prediction_table.id,
            )
            .where(
                prediction_table.model_run_id == int(run_id),
                prediction_table.market == market,
                prediction_table.trade_date == effective_trade_date,
                Symbol.market == market,
            )
            .order_by(desc(prediction_table.score), Symbol.ticker.asc())
        )
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_(tickers))
        if limit and limit > 0:
            stmt = stmt.limit(int(limit))
        rows = self.db.execute(stmt).all()
        if not rows:
            return []

        latest_evaluation = self.db.scalar(
            select(ModelEvaluation)
            .where(
                ModelEvaluation.model_run_id == int(run_id),
                ModelEvaluation.status.in_(("success", "partial")),
            )
            .order_by(ModelEvaluation.id.desc())
            .limit(1)
        )
        activation_status = str(
            (latest_evaluation.activation_status if latest_evaluation else None)
            or "unverified"
        )
        return [
            {
                "prediction_id": prediction.id,
                "model_run_id": prediction.model_run_id,
                "trade_date": prediction.trade_date,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
                "confidence": (detail.confidence if detail is not None else None),
                "signal_label": (detail.signal_label if detail is not None else None),
                "signal_strength": (detail.signal_strength if detail is not None else None),
                "expected_return_5d": (detail.expected_return_5d if detail is not None else None),
                "expected_return_20d": (detail.expected_return_20d if detail is not None else None),
                "expected_drawdown_20d": (detail.expected_drawdown_20d if detail is not None else None),
                "model_reward_risk_ratio": (detail.model_reward_risk_ratio if detail is not None else None),
                "conviction_bucket": (detail.conviction_bucket if detail is not None else None),
                "position_size_hint": (detail.position_size_hint if detail is not None else None),
                "entry_style": (detail.entry_style if detail is not None else None),
                "percentile": (detail.percentile if detail is not None else None),
                "summary_text": (detail.summary_text if detail is not None else None),
                "sector": symbol.sector,
                "industry": symbol.industry,
                "model_activation_status": activation_status,
                "source_layer": prediction_table.__tablename__,
            }
            for prediction, symbol, detail in rows
        ]

    def list_symbol_predictions(self, ticker: str, limit: int = 120, latest_run_only: bool = False) -> list[dict]:
        latest_model_run_id = None
        if latest_run_only:
            latest_model_run_id = self.db.scalar(
                select(func.max(Prediction.model_run_id))
                .select_from(Prediction)
                .join(ModelRun, ModelRun.id == Prediction.model_run_id)
                .where(
                    ModelRun.status == "success",
                    ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
                )
            )

        stmt = select(Prediction, Symbol).join(Symbol, Symbol.id == Prediction.symbol_id).where(
            Symbol.ticker == ticker.upper()
        )
        if latest_model_run_id is not None:
            stmt = stmt.where(Prediction.model_run_id == latest_model_run_id)

        stmt = stmt.order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc()).limit(limit)
        rows = self.db.execute(stmt).all()
        hot_results = [
            {
                "model_run_id": prediction.model_run_id,
                "trade_date": prediction.trade_date,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
            }
            for prediction, symbol in rows
        ]
        if not latest_run_only or latest_model_run_id is None or not self.cold_reads_enabled:
            return hot_results

        symbol = self.db.scalar(select(Symbol).where(Symbol.ticker == ticker.upper()).limit(1))
        artifact = self.db.scalar(
            select(PredictionArtifact).where(
                PredictionArtifact.model_run_id == int(latest_model_run_id),
                PredictionArtifact.status == "verified",
            )
        )
        if symbol is None or artifact is None:
            return hot_results
        try:
            cold_rows = read_prediction_artifact_rows(
                artifact.artifact_path,
                symbol_ids=[int(symbol.id)],
                include_details=False,
                limit=limit,
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return hot_results
        merged = {
            (int(item["model_run_id"]), str(item["trade_date"])): item
            for item in (
                [
                    {
                        "model_run_id": int(row["model_run_id"]),
                        "trade_date": str(row["trade_date"]),
                        "ticker": symbol.ticker,
                        "name": symbol.name,
                        "score": row.get("score"),
                        "rank_value": row.get("rank_value"),
                    }
                    for row in cold_rows
                ]
                + hot_results
            )
        }
        return sorted(
            merged.values(),
            key=lambda item: (str(item["trade_date"]), int(item["model_run_id"])),
            reverse=True,
        )[: max(1, int(limit))]

    def get_latest_model_output_for_ticker(
        self, ticker: str, *, production_only: bool = True
    ) -> dict | None:
        """Latest successful model output for one ticker.

        ``production_only`` keeps the champion-only semantics used by operational
        candidate views. Per-ticker display surfaces (insight detail pages) pass
        ``production_only=False`` so imported/native model artifacts stay
        readable without being promoted to the production champion.

        Reads follow the same market-scoped layering as the explanation and
        trade-plan repositories: the physical hot table for CN/HK/US first, then
        the legacy shared ``predictions`` mirror kept during the migration.
        """
        model_type_conditions = (
            [ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES)]
            if production_only
            else []
        )
        prediction_table = Prediction
        detail_table = PredictionDetail
        row = None
        symbol = self.db.scalar(
            select(Symbol)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Symbol.ticker.asc())
            .limit(1)
        )
        if symbol is not None and isinstance(self.db, Session):
            market = str(symbol.market or "").strip().upper()
            if market in physical_fact_write_markets():
                prediction_table, detail_table, _ = physical_hot_prediction_models(market)
                row = self.db.execute(
                    select(prediction_table, Symbol, ModelRun, detail_table)
                    .join(Symbol, Symbol.id == prediction_table.symbol_id)
                    .join(ModelRun, ModelRun.id == prediction_table.model_run_id)
                    .outerjoin(
                        detail_table,
                        detail_table.prediction_id == prediction_table.id,
                    )
                    .where(prediction_table.symbol_id == int(symbol.id))
                    .where(Symbol.market == market)
                    .where(ModelRun.status == "success", *model_type_conditions)
                    .order_by(
                        prediction_table.trade_date.desc(),
                        prediction_table.model_run_id.desc(),
                    )
                    .limit(1)
                ).first()
        if row is None:
            prediction_table = Prediction
            detail_table = PredictionDetail
            stmt = (
                select(Prediction, Symbol, ModelRun, PredictionDetail)
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .join(ModelRun, ModelRun.id == Prediction.model_run_id)
                .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
                .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
                .where(ModelRun.status == "success", *model_type_conditions)
                .order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc())
                .limit(1)
            )
            row = self.db.execute(stmt).first()
        if row is None:
            return None

        prediction, symbol, model_run, prediction_detail = row
        peer_count = self.db.scalar(
            select(func.count(prediction_table.id))
            .where(prediction_table.model_run_id == prediction.model_run_id)
            .where(prediction_table.trade_date == prediction.trade_date)
        ) or 0

        rank_value = prediction.rank_value
        percentile = None
        if rank_value is not None and peer_count:
            percentile = round(max(0.0, min(100.0, (1 - ((rank_value - 1) / max(peer_count, 1))) * 100.0)), 1)

        payload = {
            "prediction_id": prediction.id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "trade_date": str(prediction.trade_date)[:10],
            "score": prediction.score,
            "rank_value": prediction.rank_value,
            "universe_size": peer_count,
            "percentile": percentile,
            "model_run": {
                "id": model_run.id,
                "name": model_run.name,
                "model_type": model_run.model_type,
                "market": model_run.market,
                "universe": model_run.universe,
                "created_at": model_run.created_at,
                "status": model_run.status,
            },
        }
        if prediction_detail is not None:
            payload.update(
                {
                    "confidence": prediction_detail.confidence,
                    "bullish_prob": prediction_detail.bullish_prob,
                    "bearish_prob": prediction_detail.bearish_prob,
                    "expected_return_5d": prediction_detail.expected_return_5d,
                    "expected_return_20d": prediction_detail.expected_return_20d,
                    "expected_drawdown_20d": prediction_detail.expected_drawdown_20d,
                    "model_reward_risk_ratio": prediction_detail.model_reward_risk_ratio,
                    "risk_score": prediction_detail.risk_score,
                    "target_horizon_days": prediction_detail.target_horizon_days,
                    "universe_size": prediction_detail.universe_size or payload["universe_size"],
                    "percentile": prediction_detail.percentile if prediction_detail.percentile is not None else payload["percentile"],
                    "regime_label": prediction_detail.regime_label,
                    "conviction_bucket": prediction_detail.conviction_bucket,
                    "position_size_hint": prediction_detail.position_size_hint,
                    "entry_style": prediction_detail.entry_style,
                    "signal_label": prediction_detail.signal_label,
                    "signal_strength": prediction_detail.signal_strength,
                    "summary_text": prediction_detail.summary_text,
                }
            )
        return payload

    def _latest_model_outputs_for_tickers(
        self, normalized: list[str], *, production_only: bool
    ) -> dict[str, dict]:
        model_type_conditions = (
            [ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES)]
            if production_only
            else []
        )
        ranked_predictions = (
            select(
                Prediction.id.label("prediction_id"),
                func.row_number().over(
                    partition_by=Prediction.symbol_id,
                    order_by=(Prediction.trade_date.desc(), Prediction.model_run_id.desc(), Prediction.id.desc()),
                ).label("rn"),
            )
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .where(Symbol.ticker.in_(normalized))
            .where(ModelRun.status == "success", *model_type_conditions)
            .subquery()
        )

        stmt = (
            select(Prediction, Symbol, ModelRun, PredictionDetail)
            .join(ranked_predictions, ranked_predictions.c.prediction_id == Prediction.id)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(ranked_predictions.c.rn == 1)
            .order_by(Symbol.ticker.asc())
        )
        rows = self.db.execute(stmt).all()

        pairs: set[tuple[int, str]] = set()
        for prediction, symbol, model_run, prediction_detail in rows:
            pairs.add((prediction.model_run_id, prediction.trade_date))

        if not rows:
            return {}

        pair_counts: dict[tuple[int, str], int] = {}
        for model_run_id, trade_date in pairs:
            pair_counts[(model_run_id, trade_date)] = self.db.scalar(
                select(func.count(Prediction.id))
                .where(Prediction.model_run_id == model_run_id)
                .where(Prediction.trade_date == trade_date)
            ) or 0

        payloads: dict[str, dict] = {}
        for prediction, symbol, model_run, prediction_detail in rows:
            peer_count = pair_counts.get((prediction.model_run_id, prediction.trade_date), 0)

            rank_value = prediction.rank_value
            percentile = None
            if rank_value is not None and peer_count:
                percentile = round(max(0.0, min(100.0, (1 - ((rank_value - 1) / max(peer_count, 1))) * 100.0)), 1)

            payload = {
                "prediction_id": prediction.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "trade_date": prediction.trade_date,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
                "universe_size": peer_count,
                "percentile": percentile,
                "model_run": {
                    "id": model_run.id,
                    "name": model_run.name,
                    "model_type": model_run.model_type,
                    "market": model_run.market,
                    "universe": model_run.universe,
                    "created_at": model_run.created_at,
                    "status": model_run.status,
                },
            }
            if prediction_detail is not None:
                payload.update(
                    {
                        "confidence": prediction_detail.confidence,
                        "bullish_prob": prediction_detail.bullish_prob,
                        "bearish_prob": prediction_detail.bearish_prob,
                        "expected_return_5d": prediction_detail.expected_return_5d,
                        "expected_return_20d": prediction_detail.expected_return_20d,
                        "expected_drawdown_20d": prediction_detail.expected_drawdown_20d,
                        "model_reward_risk_ratio": prediction_detail.model_reward_risk_ratio,
                        "risk_score": prediction_detail.risk_score,
                        "target_horizon_days": prediction_detail.target_horizon_days,
                        "universe_size": prediction_detail.universe_size or payload["universe_size"],
                        "percentile": prediction_detail.percentile if prediction_detail.percentile is not None else payload["percentile"],
                        "regime_label": prediction_detail.regime_label,
                        "conviction_bucket": prediction_detail.conviction_bucket,
                        "position_size_hint": prediction_detail.position_size_hint,
                        "entry_style": prediction_detail.entry_style,
                        "signal_label": prediction_detail.signal_label,
                        "signal_strength": prediction_detail.signal_strength,
                        "summary_text": prediction_detail.summary_text,
                    }
                )
            payloads[symbol.ticker] = payload
        return payloads

    def get_latest_model_outputs_for_tickers(self, tickers: list[str]) -> dict[str, dict]:
        """Latest successful model output per ticker, champion first.

        Tickers without a production champion fall back to the latest imported
        or research artifact so ingested model runs stay visible on display
        surfaces (insight, screener, dashboard, watchlist) without ever
        shadowing the production champion.
        """
        normalized = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()))
        if not normalized:
            return {}
        payloads = self._latest_model_outputs_for_tickers(normalized, production_only=True)
        missing = [ticker for ticker in normalized if ticker not in payloads]
        if missing:
            for ticker, payload in self._latest_model_outputs_for_tickers(
                missing, production_only=False
            ).items():
                payloads.setdefault(ticker, payload)
        return payloads

    def list_recent_prediction_snapshots(self, *, top_n: int = 10, limit_runs: int = 4) -> list[dict]:
        def _load_pairs(*, production_only: bool) -> list[tuple[int, str]]:
            stmt = (
                select(Prediction.model_run_id, Prediction.trade_date)
                .join(ModelRun, ModelRun.id == Prediction.model_run_id)
                .where(ModelRun.status == "success")
                .order_by(desc(Prediction.model_run_id), desc(Prediction.trade_date))
            )
            if production_only:
                stmt = stmt.where(ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES))
            seen: set[tuple[int, str]] = set()
            pairs: list[tuple[int, str]] = []
            for model_run_id, trade_date in self.db.execute(stmt):
                key = (int(model_run_id), str(trade_date))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
                if len(pairs) >= limit_runs:
                    break
            return pairs

        # Prefer production champion runs; when the workspace has none yet (a
        # fresh external/native import, for example) fall back to the latest
        # successful run of any type so leaderboard surfaces stay populated.
        pairs = _load_pairs(production_only=True) or _load_pairs(production_only=False)

        snapshots: list[dict] = []
        for model_run_id, trade_date in pairs:
            stmt = (
                select(Prediction, Symbol)
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .where(Prediction.model_run_id == model_run_id)
                .where(Prediction.trade_date == trade_date)
                .order_by(desc(Prediction.score))
                .limit(top_n)
            )
            rows = self.db.execute(stmt).all()
            snapshots.append(
                {
                    "model_run_id": model_run_id,
                    "trade_date": trade_date,
                    "items": [
                        {
                            "ticker": symbol.ticker,
                            "name": symbol.name,
                            "score": prediction.score,
                            "rank_value": prediction.rank_value,
                        }
                        for prediction, symbol in rows
                    ],
                }
            )
        return snapshots

    def count_recent_signal_hits(
        self,
        *,
        tickers: list[str],
        signal_label: str = "BUY",
        limit_runs: int = 5,
    ) -> dict[str, int]:
        normalized_tickers = sorted({str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()})
        counts = {ticker: 0 for ticker in normalized_tickers}
        if not normalized_tickers or limit_runs <= 0:
            return counts

        pair_stmt = (
            select(Prediction.model_run_id, Prediction.trade_date)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
            )
            .order_by(desc(Prediction.model_run_id), desc(Prediction.trade_date))
        )
        seen: set[tuple[int, str]] = set()
        pairs: list[tuple[int, str]] = []
        for model_run_id, trade_date in self.db.execute(pair_stmt):
            key = (int(model_run_id), str(trade_date))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
            if len(pairs) >= limit_runs:
                break

        normalized_label = str(signal_label or "").strip().upper()
        if not pairs:
            return counts

        for model_run_id, trade_date in pairs:
            stmt = (
                select(Symbol.ticker)
                .select_from(Prediction)
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
                .where(Prediction.model_run_id == model_run_id)
                .where(Prediction.trade_date == trade_date)
                .where(Symbol.ticker.in_(normalized_tickers))
            )
            if normalized_label and normalized_label != "ALL":
                stmt = stmt.where(func.upper(func.coalesce(PredictionDetail.signal_label, "")) == normalized_label)
            for ticker in self.db.execute(stmt).scalars().all():
                normalized_ticker = str(ticker).strip().upper()
                counts[normalized_ticker] = counts.get(normalized_ticker, 0) + 1
        return counts

class PredictionExplanationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db
        self._batch_size = 1000

    def replace_for_model_run(
        self,
        model_run_id: int,
        rows: list[dict],
        *,
        commit: bool = True,
    ) -> int:
        _assert_legacy_prediction_write_allowed(
            self.db,
            model_run_id=model_run_id,
        )
        prediction_identity_stmt = select(
            Prediction.id,
            Prediction.symbol_id,
            Prediction.trade_date,
        ).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.execute(prediction_identity_stmt).all())
        self.db.execute(
            delete(PredictionExplanation).where(
                PredictionExplanation.prediction_id.in_(
                    select(Prediction.id).where(
                        Prediction.model_run_id == model_run_id
                    )
                )
            )
        )

        if not rows:
            if commit:
                self.db.commit()
            return 0

        prediction_map = {
            (prediction.symbol_id, prediction.trade_date): prediction.id
            for prediction in predictions
        }
        now = utc_now_iso()
        payload_rows: list[dict] = []
        for row in rows:
            prediction_id = prediction_map.get((row["symbol_id"], row["trade_date"]))
            if prediction_id is None:
                continue
            payload_rows.append(
                {
                    "prediction_id": prediction_id,
                    "feature_name": row["feature_name"],
                    "feature_value": row.get("feature_value"),
                    "contribution": row.get("contribution"),
                    "direction": row.get("direction"),
                    "display_order": row.get("display_order"),
                    "created_at": now,
                }
            )

        inserted = len(payload_rows)
        for row_chunk in chunked_rows(payload_rows, self._batch_size):
            stmt = pg_insert(PredictionExplanation).values(row_chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[PredictionExplanation.prediction_id, PredictionExplanation.feature_name],
                set_={
                    "feature_value": stmt.excluded.feature_value,
                    "contribution": stmt.excluded.contribution,
                    "direction": stmt.excluded.direction,
                    "display_order": stmt.excluded.display_order,
                    "created_at": stmt.excluded.created_at,
                },
            )
            self.db.execute(stmt)
        if commit:
            self.db.commit()

        return inserted

    def get_latest_for_ticker(self, ticker: str) -> list[dict]:
        return list(self.get_latest_state_for_ticker(ticker)["rows"])

    @staticmethod
    def _serialize_rows(rows) -> list[dict]:
        return [
            {
                "feature_name": row.feature_name,
                "feature_value": row.feature_value,
                "contribution": row.contribution,
                "direction": row.direction,
                "display_order": row.display_order,
            }
            for row in rows
        ]

    def get_latest_state_for_ticker(self, ticker: str) -> dict:
        symbol = self.db.execute(
            select(Symbol.id, Symbol.market)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .limit(1)
        ).first()
        if symbol is None:
            return {
                "status": "no_prediction",
                "source_layer": None,
                "model_run_id": None,
                "trade_date": None,
                "rows": [],
                "reason": "symbol_not_found",
            }
        symbol_id = int(symbol.id)
        market = str(symbol.market or "").strip().upper()
        physical_markets = physical_fact_write_markets()
        if market in physical_markets:
            prediction_table, _detail_table, explanation_table = (
                physical_hot_prediction_models(market)
            )
        else:
            prediction_table, explanation_table = Prediction, PredictionExplanation
        latest = self.db.execute(
            select(
                prediction_table.id,
                prediction_table.model_run_id,
                prediction_table.trade_date,
            )
            .where(prediction_table.symbol_id == symbol_id)
            .order_by(
                prediction_table.trade_date.desc(),
                prediction_table.model_run_id.desc(),
            )
            .limit(1)
        ).first()
        if latest is None:
            return {
                "status": "no_prediction",
                "source_layer": prediction_table.__tablename__,
                "model_run_id": None,
                "trade_date": None,
                "rows": [],
                "reason": "prediction_not_found",
            }
        hot_rows = list(
            self.db.scalars(
                select(explanation_table)
                .where(explanation_table.prediction_id == int(latest.id))
                .order_by(
                    explanation_table.display_order.asc(),
                    desc(func.abs(explanation_table.contribution)),
                )
            ).all()
        )
        if hot_rows:
            return {
                "status": "materialized_hot",
                "source_layer": explanation_table.__tablename__,
                "model_run_id": int(latest.model_run_id),
                "trade_date": str(latest.trade_date),
                "rows": self._serialize_rows(hot_rows),
                "reason": None,
            }
        artifact = self.db.scalar(
            select(PredictionArtifact).where(
                PredictionArtifact.model_run_id == int(latest.model_run_id),
                PredictionArtifact.status == "verified",
            )
        )
        if artifact is None or int(artifact.explanation_count or 0) <= 0:
            return {
                "status": "not_materialized",
                "source_layer": explanation_table.__tablename__,
                "model_run_id": int(latest.model_run_id),
                "trade_date": str(latest.trade_date),
                "rows": [],
                "reason": (
                    "no_verified_artifact"
                    if artifact is None
                    else "no_explanations_in_verified_artifact"
                ),
            }
        if not get_settings().prediction_cold_reads_enabled:
            return {
                "status": "cold_unavailable",
                "source_layer": "cold_parquet_disabled",
                "model_run_id": int(latest.model_run_id),
                "trade_date": str(latest.trade_date),
                "rows": [],
                "reason": "cold_reads_disabled",
            }
        try:
            cold_rows = read_prediction_explanation_artifact_rows(
                artifact.artifact_path,
                symbol_ids=[symbol_id],
                trade_dates=[str(latest.trade_date)],
            )
        except (FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
            return {
                "status": "cold_unavailable",
                "source_layer": "cold_parquet",
                "model_run_id": int(latest.model_run_id),
                "trade_date": str(latest.trade_date),
                "rows": [],
                "reason": str(exc),
            }
        if not cold_rows:
            return {
                "status": "not_materialized",
                "source_layer": "cold_parquet",
                "model_run_id": int(latest.model_run_id),
                "trade_date": str(latest.trade_date),
                "rows": [],
                "reason": "ticker_not_selected_for_explanation",
            }
        return {
            "status": "loaded_cold",
            "source_layer": "cold_parquet",
            "model_run_id": int(latest.model_run_id),
            "trade_date": str(latest.trade_date),
            "rows": [
                {
                    "feature_name": row.get("feature_name"),
                    "feature_value": row.get("feature_value"),
                    "contribution": row.get("contribution"),
                    "direction": row.get("direction"),
                    "display_order": row.get("display_order"),
                }
                for row in cold_rows
            ],
            "reason": None,
        }

    def get_latest_for_tickers(self, tickers: list[str]) -> dict[str, list[dict]]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
        if not normalized:
            return {}
        symbol_rows = self.db.execute(
            select(Symbol.id, Symbol.ticker, Symbol.market).where(
                Symbol.ticker.in_(normalized)
            )
        ).all()
        symbols_by_market: dict[str, list[tuple[int, str]]] = {}
        for symbol_id, ticker, market in symbol_rows:
            market_code = str(market or "").strip().upper()
            symbols_by_market.setdefault(market_code, []).append(
                (int(symbol_id), str(ticker or "").strip().upper())
            )
        physical_markets = physical_fact_write_markets()
        payloads: dict[str, list[dict]] = {}
        for market_code, market_symbols in symbols_by_market.items():
            if market_code in physical_markets:
                prediction_table, _detail_table, explanation_table = (
                    physical_hot_prediction_models(market_code)
                )
            else:
                prediction_table, explanation_table = Prediction, PredictionExplanation
            symbol_ids = [item[0] for item in market_symbols]
            ticker_by_symbol_id = {item[0]: item[1] for item in market_symbols}
            prediction_rows = self.db.execute(
                select(prediction_table.id, prediction_table.symbol_id)
                .where(prediction_table.symbol_id.in_(symbol_ids))
                .order_by(
                    prediction_table.symbol_id.asc(),
                    prediction_table.trade_date.desc(),
                    prediction_table.model_run_id.desc(),
                )
            ).all()
            latest_prediction_id_by_symbol: dict[int, int] = {}
            for prediction_id, symbol_id in prediction_rows:
                latest_prediction_id_by_symbol.setdefault(
                    int(symbol_id), int(prediction_id)
                )
            prediction_id_to_ticker = {
                prediction_id: ticker_by_symbol_id[symbol_id]
                for symbol_id, prediction_id in latest_prediction_id_by_symbol.items()
            }
            for ticker in prediction_id_to_ticker.values():
                payloads.setdefault(ticker, [])
            if not prediction_id_to_ticker:
                continue
            explanations = self.db.scalars(
                select(explanation_table)
                .where(
                    explanation_table.prediction_id.in_(
                        list(prediction_id_to_ticker)
                    )
                )
                .order_by(
                    explanation_table.prediction_id.asc(),
                    explanation_table.display_order.asc(),
                    desc(func.abs(explanation_table.contribution)),
                )
            ).all()
            for row in explanations:
                ticker = prediction_id_to_ticker.get(int(row.prediction_id))
                if ticker:
                    payloads[ticker].append(
                        {
                            "feature_name": row.feature_name,
                            "feature_value": row.feature_value,
                            "contribution": row.contribution,
                            "direction": row.direction,
                            "display_order": row.display_order,
                            "source_layer": explanation_table.__tablename__,
                        }
                    )
        return payloads

class PredictionDetailRepository:
    def __init__(self, db: Session) -> None:
        self.db = db
        # A row has 21 bound columns.  2,500 rows stay below PostgreSQL's
        # 65,535-parameter ceiling while avoiding needless round trips.
        self._batch_size = 2500

    def replace_for_model_run(
        self,
        model_run_id: int,
        rows: list[dict],
        *,
        commit: bool = True,
    ) -> int:
        _assert_legacy_prediction_write_allowed(
            self.db,
            model_run_id=model_run_id,
        )
        prediction_identity_stmt = select(
            Prediction.id,
            Prediction.symbol_id,
            Prediction.trade_date,
        ).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.execute(prediction_identity_stmt).all())
        self.db.execute(
            delete(PredictionDetail).where(
                PredictionDetail.prediction_id.in_(
                    select(Prediction.id).where(
                        Prediction.model_run_id == model_run_id
                    )
                )
            )
        )

        if not rows:
            if commit:
                self.db.commit()
            return 0

        prediction_map = {
            (prediction.symbol_id, prediction.trade_date): prediction.id
            for prediction in predictions
        }
        now = utc_now_iso()
        payload_by_prediction_id: dict[int, dict] = {}
        for row in rows:
            prediction_id = prediction_map.get((row["symbol_id"], row["trade_date"]))
            if prediction_id is None:
                continue
            payload = {
                "prediction_id": prediction_id,
                "confidence": row.get("confidence"),
                "bullish_prob": row.get("bullish_prob"),
                "bearish_prob": row.get("bearish_prob"),
                "expected_return_5d": row.get("expected_return_5d"),
                "expected_return_20d": row.get("expected_return_20d"),
                "expected_drawdown_20d": row.get("expected_drawdown_20d"),
                "model_reward_risk_ratio": row.get("model_reward_risk_ratio"),
                "risk_score": row.get("risk_score"),
                "target_horizon_days": row.get("target_horizon_days"),
                "universe_size": row.get("universe_size"),
                "percentile": row.get("percentile"),
                "regime_label": row.get("regime_label"),
                "conviction_bucket": row.get("conviction_bucket"),
                "position_size_hint": row.get("position_size_hint"),
                "entry_style": row.get("entry_style"),
                "signal_label": row.get("signal_label"),
                "signal_strength": row.get("signal_strength"),
                "summary_text": row.get("summary_text"),
                "created_at": now,
            }
            existing = payload_by_prediction_id.get(prediction_id)
            if existing is None:
                payload_by_prediction_id[prediction_id] = payload
                continue
            existing_strength = float(existing.get("signal_strength") or 0.0)
            incoming_strength = float(payload.get("signal_strength") or 0.0)
            existing_confidence = float(existing.get("confidence") or 0.0)
            incoming_confidence = float(payload.get("confidence") or 0.0)
            if (incoming_strength, incoming_confidence) > (existing_strength, existing_confidence):
                payload_by_prediction_id[prediction_id] = payload

        payload_rows = list(payload_by_prediction_id.values())
        inserted = len(payload_rows)
        for row_chunk in chunked_rows(payload_rows, self._batch_size):
            # replace_for_model_run deletes every existing child first and the
            # payload is already deduplicated by prediction_id.  A conflict
            # update therefore adds index work without providing semantics.
            self.db.execute(insert(PredictionDetail).values(row_chunk))
        if commit:
            self.db.commit()

        return inserted

class PredictionTradePlanRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _values(row: dict) -> dict:
        return {
            "entry_low": row.get("entry_low"),
            "entry_high": row.get("entry_high"),
            "breakout_level": row.get("breakout_level"),
            "take_profit_low": row.get("take_profit_low"),
            "take_profit_high": row.get("take_profit_high"),
            "risk_level": row.get("risk_level"),
            "support_level": row.get("support_level"),
            "resistance_level": row.get("resistance_level"),
            "stop_type": row.get("stop_type"),
            "trailing_stop_pct": row.get("trailing_stop_pct"),
            "invalidation_reason": row.get("invalidation_reason"),
            "execution_tags_json": json.dumps(row.get("execution_tags") or []),
            "note": row.get("note"),
        }

    def _replace_legacy(self, model_run_id: int, rows: list[dict]) -> int:
        prediction_stmt = select(Prediction).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.scalars(prediction_stmt).all())
        prediction_ids = [prediction.id for prediction in predictions]
        if prediction_ids:
            for prediction_id_chunk in chunked_ids(prediction_ids):
                self.db.execute(
                    delete(PredictionTradePlan).where(
                        PredictionTradePlan.prediction_id.in_(prediction_id_chunk)
                    )
                )
            self.db.flush()
        prediction_map = {(prediction.symbol_id, prediction.trade_date): prediction.id for prediction in predictions}
        now = utc_now_iso()
        inserted = 0
        for row in rows:
            prediction_id = prediction_map.get(
                (int(row["symbol_id"]), str(row["trade_date"])[:10])
            )
            if prediction_id is None:
                continue
            self.db.add(
                PredictionTradePlan(
                    prediction_id=prediction_id,
                    **self._values(row),
                    created_at=now,
                )
            )
            inserted += 1
        self.db.flush()
        return inserted

    def replace_for_model_run(
        self,
        model_run_id: int,
        rows: list[dict],
        *,
        commit: bool = True,
    ) -> int:
        market = str(
            self.db.scalar(
                select(ModelRun.market).where(ModelRun.id == int(model_run_id))
            )
            or ""
        ).strip().upper()
        if market not in physical_fact_write_markets():
            inserted = self._replace_legacy(model_run_id, rows)
            if commit:
                self.db.commit()
            return inserted

        prediction_table, _, _ = physical_hot_prediction_models(market)
        plan_table = physical_prediction_trade_plan_model(market)
        predictions = list(
            self.db.scalars(
                select(prediction_table).where(
                    prediction_table.model_run_id == int(model_run_id)
                )
            )
        )
        prediction_ids = [int(prediction.id) for prediction in predictions]
        prediction_map = {
            (int(prediction.symbol_id), str(prediction.trade_date)): int(prediction.id)
            for prediction in predictions
        }
        requested_keys = {
            (int(row["symbol_id"]), str(row["trade_date"])[:10]) for row in rows
        }
        missing_keys = sorted(requested_keys - set(prediction_map))
        if missing_keys:
            raise RuntimeError(
                f"Refusing {market} trade-plan write because "
                f"{len(missing_keys)} rows have no physical hot prediction parent."
            )
        write_legacy = legacy_mirror_write_enabled(
            self.db,
            market=market,
            configured=bool(
                getattr(get_settings(), "market_physical_hot_dual_write_legacy", True)
            ),
        )
        inserted = 0
        try:
            if prediction_ids:
                self.db.execute(
                    delete(plan_table).where(
                        plan_table.prediction_id.in_(prediction_ids)
                    )
                )
            now = app_now()
            for row in rows:
                prediction_id = prediction_map.get(
                    (int(row["symbol_id"]), str(row["trade_date"])[:10])
                )
                if prediction_id is None:
                    continue
                self.db.add(
                    plan_table(
                        prediction_id=prediction_id,
                        **self._values(row),
                        created_at=now,
                    )
                )
                inserted += 1
            self.db.flush()
            if write_legacy:
                self._replace_legacy(model_run_id, rows)
            if commit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return inserted

    def get_latest_for_ticker(self, ticker: str) -> dict | None:
        symbol = self.db.scalar(
            select(Symbol)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Symbol.ticker.asc())
            .limit(1)
        )
        market = str(symbol.market or "").strip().upper() if symbol is not None else ""
        if symbol is not None and market in physical_fact_write_markets():
            prediction_table, _, _ = physical_hot_prediction_models(market)
            plan_table = physical_prediction_trade_plan_model(market)
            physical_row = self.db.scalar(
                select(plan_table)
                .join(
                    prediction_table,
                    prediction_table.id == plan_table.prediction_id,
                )
                .where(prediction_table.symbol_id == int(symbol.id))
                .order_by(
                    prediction_table.trade_date.desc(),
                    prediction_table.model_run_id.desc(),
                )
                .limit(1)
            )
            if physical_row is not None:
                return self._payload(physical_row)
        stmt = (
            select(PredictionTradePlan)
            .join(Prediction, Prediction.id == PredictionTradePlan.prediction_id)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc())
            .limit(1)
        )
        row = self.db.scalar(stmt)
        if row is None:
            return None
        return self._payload(row)

    @staticmethod
    def _payload(row) -> dict:
        return {
            "entry_low": row.entry_low,
            "entry_high": row.entry_high,
            "breakout_level": row.breakout_level,
            "take_profit_low": row.take_profit_low,
            "take_profit_high": row.take_profit_high,
            "risk_level": row.risk_level,
            "support_level": row.support_level,
            "resistance_level": row.resistance_level,
            "stop_type": row.stop_type,
            "trailing_stop_pct": row.trailing_stop_pct,
            "invalidation_reason": row.invalidation_reason,
            "execution_tags": json.loads(row.execution_tags_json) if row.execution_tags_json else [],
            "note": row.note,
        }

    def get_latest_for_tickers(self, tickers: list[str]) -> dict[str, dict]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
        if not normalized:
            return {}
        payloads: dict[str, dict] = {}
        for ticker in normalized:
            payload = self.get_latest_for_ticker(ticker)
            if payload is not None:
                payloads[ticker] = payload
        return payloads

class PredictionArtifactRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_manifest(self, manifest: dict, *, status: str = "verified") -> PredictionArtifact:
        model_run_id = int(manifest["model_run_id"])
        artifact_path = str(manifest.get("artifact_path") or "").strip()
        manifest_sha256 = str(manifest.get("manifest_sha256") or "").strip()
        if not artifact_path or not manifest_sha256:
            raise ValueError("Prediction artifact path and manifest SHA-256 are required.")
        values = {
            "model_run_id": model_run_id,
            "status": str(status or "verified"),
            "schema_version": str(manifest.get("schema_version") or "prediction-artifact-v1"),
            "market": str(manifest.get("market") or "").upper() or None,
            "artifact_path": artifact_path,
            "manifest_sha256": manifest_sha256,
            "prediction_count": int(manifest.get("row_count") or 0),
            "detail_count": int(manifest.get("detail_row_count") or 0),
            "explanation_count": int(manifest.get("explanation_row_count") or 0),
            "min_trade_date": manifest.get("min_trade_date"),
            "max_trade_date": manifest.get("max_trade_date"),
            "created_at": str(manifest.get("created_at") or utc_now_iso()),
            "verified_at": utc_now_iso() if status == "verified" else None,
        }
        stmt = pg_insert(PredictionArtifact).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[PredictionArtifact.model_run_id],
            set_={
                "status": stmt.excluded.status,
                "schema_version": stmt.excluded.schema_version,
                "market": stmt.excluded.market,
                "artifact_path": stmt.excluded.artifact_path,
                "manifest_sha256": stmt.excluded.manifest_sha256,
                "prediction_count": stmt.excluded.prediction_count,
                "detail_count": stmt.excluded.detail_count,
                "explanation_count": stmt.excluded.explanation_count,
                "min_trade_date": stmt.excluded.min_trade_date,
                "max_trade_date": stmt.excluded.max_trade_date,
                "verified_at": stmt.excluded.verified_at,
            },
        ).returning(PredictionArtifact)
        artifact = self.db.execute(stmt).scalar_one()
        self.db.commit()
        return artifact

    def set_status(self, model_run_id: int, status: str) -> PredictionArtifact | None:
        artifact = self.db.scalar(
            select(PredictionArtifact).where(PredictionArtifact.model_run_id == int(model_run_id))
        )
        if artifact is None:
            return None
        artifact.status = str(status)
        artifact.verified_at = utc_now_iso() if status == "verified" else artifact.verified_at
        self.db.commit()
        self.db.refresh(artifact)
        return artifact

class LivePredictionRepository:
    """Publish the latest cross-section used by latency-sensitive online reads."""

    DETAIL_FIELDS = (
        "confidence",
        "signal_label",
        "signal_strength",
        "expected_return_20d",
        "expected_drawdown_20d",
        "model_reward_risk_ratio",
        "conviction_bucket",
        "position_size_hint",
        "entry_style",
        "percentile",
        "summary_text",
    )

    def __init__(self, db: Session) -> None:
        self.db = db
        self.last_publish_action = "pending"
        self.last_publish_actions: dict[str, str] = {}

    @staticmethod
    def latest_rows(rows: list[dict]) -> list[dict]:
        dated_rows = [row for row in rows if row.get("trade_date")]
        if not dated_rows:
            return []
        latest_trade_date = max(str(row["trade_date"]) for row in dated_rows)
        return [row for row in dated_rows if str(row["trade_date"]) == latest_trade_date]

    def _publish_to_table(self, table, *, model_run_id: int, payload_rows: list[dict]) -> str:
        existing_rows = list(
            self.db.scalars(
                select(table).where(table.model_run_id == int(model_run_id))
            ).all()
        )
        comparable_fields = (
            "market",
            "trade_date",
            "score",
            "rank_value",
            *self.DETAIL_FIELDS,
        )
        existing_payload = {
            (int(row.symbol_id), str(row.trade_date)): tuple(
                getattr(row, field) for field in comparable_fields
            )
            for row in existing_rows
        }
        incoming_payload = {
            (int(row["symbol_id"]), str(row["trade_date"])): tuple(
                row.get(field) for field in comparable_fields
            )
            for row in payload_rows
        }
        if existing_payload == incoming_payload:
            return "unchanged"

        self.db.execute(delete(table).where(table.model_run_id == int(model_run_id)))
        for row_chunk in chunked_rows(payload_rows, 1000):
            stmt = pg_insert(table).values(row_chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    table.model_run_id,
                    table.symbol_id,
                    table.trade_date,
                ],
                set_={
                    "market": stmt.excluded.market,
                    "score": stmt.excluded.score,
                    "rank_value": stmt.excluded.rank_value,
                    "confidence": stmt.excluded.confidence,
                    "signal_label": stmt.excluded.signal_label,
                    "signal_strength": stmt.excluded.signal_strength,
                    "expected_return_20d": stmt.excluded.expected_return_20d,
                    "expected_drawdown_20d": stmt.excluded.expected_drawdown_20d,
                    "model_reward_risk_ratio": stmt.excluded.model_reward_risk_ratio,
                    "conviction_bucket": stmt.excluded.conviction_bucket,
                    "position_size_hint": stmt.excluded.position_size_hint,
                    "entry_style": stmt.excluded.entry_style,
                    "percentile": stmt.excluded.percentile,
                    "summary_text": stmt.excluded.summary_text,
                    "published_at": stmt.excluded.published_at,
                },
            )
            self.db.execute(stmt)
        return "replaced"

    def publish_for_model_run(
        self,
        *,
        model_run_id: int,
        market: str,
        prediction_rows: list[dict],
        detail_rows: list[dict],
        commit: bool = True,
    ) -> int:
        normalized_market = str(market or "").strip().upper()
        if normalized_market not in physical_fact_write_markets():
            raise ValueError(f"Unsupported live-prediction market: {market!r}")

        latest_prediction_rows = self.latest_rows(prediction_rows)
        detail_by_key = {
            (int(row["symbol_id"]), str(row["trade_date"])): row
            for row in self.latest_rows(detail_rows)
        }
        deduped_predictions: dict[tuple[int, str], dict] = {}
        for row in latest_prediction_rows:
            key = (int(row["symbol_id"]), str(row["trade_date"]))
            existing = deduped_predictions.get(key)
            if existing is None or float(row.get("score") or 0.0) > float(
                existing.get("score") or 0.0
            ):
                deduped_predictions[key] = row

        published_at = app_now()
        payload_rows: list[dict] = []
        for (symbol_id, trade_date_value), row in deduped_predictions.items():
            detail = detail_by_key.get((symbol_id, trade_date_value), {})
            payload = {
                "model_run_id": int(model_run_id),
                "symbol_id": symbol_id,
                "market": normalized_market,
                "trade_date": date.fromisoformat(trade_date_value),
                "score": row.get("score"),
                "rank_value": row.get("rank_value"),
                "published_at": published_at,
            }
            payload.update({field: detail.get(field) for field in self.DETAIL_FIELDS})
            payload_rows.append(payload)

        symbol_ids = {int(row["symbol_id"]) for row in payload_rows}
        matched_symbol_count = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.id.in_(symbol_ids),
                    Symbol.market == normalized_market,
                )
            )
            or 0
        )
        if matched_symbol_count != len(symbol_ids):
            raise RuntimeError(
                f"Refusing {normalized_market} fact write: "
                f"{len(symbol_ids) - matched_symbol_count} symbols are missing or cross-market."
            )

        run_market = str(
            self.db.scalar(
                select(ModelRun.market).where(ModelRun.id == int(model_run_id))
            )
            or ""
        ).strip().upper()
        if run_market != normalized_market:
            raise RuntimeError(
                f"Model run {model_run_id} market {run_market!r} cannot write "
                f"{normalized_market} live facts."
            )

        settings = get_settings()
        # The market-specific table is always the primary write target.  The
        # shared table is only a migration mirror and can never be the fallback
        # sole destination for CN/US facts.
        targets = [physical_live_prediction_model(normalized_market)]
        if legacy_mirror_write_enabled(
            self.db,
            market=normalized_market,
            configured=settings.market_physical_live_dual_write_legacy,
        ):
            targets.append(LivePrediction)

        self.last_publish_actions = {
            str(table.__tablename__): self._publish_to_table(
                table,
                model_run_id=int(model_run_id),
                payload_rows=payload_rows,
            )
            for table in targets
        }
        if any(action == "replaced" for action in self.last_publish_actions.values()):
            if commit:
                self.db.commit()
            self.last_publish_action = "replaced"
        else:
            self.last_publish_action = "unchanged"
        return len(payload_rows)

    def publish_from_physical_hot(
        self,
        *,
        model_run_id: int,
        market: str,
        commit: bool = True,
    ) -> int:
        """Publish the latest live cross-section inside PostgreSQL.

        The market-specific hot tables are the canonical source at this point in
        the publication transaction.  Copying the latest slice with
        ``INSERT .. SELECT`` avoids serializing and binding the same large Python
        payload for a third time during the observed dual-write period.
        """
        normalized_market = str(market or "").strip().upper()
        if normalized_market not in physical_fact_write_markets():
            raise ValueError(f"Unsupported live-prediction market: {market!r}")

        run_id = int(model_run_id)
        run_market = str(
            self.db.scalar(select(ModelRun.market).where(ModelRun.id == run_id))
            or ""
        ).strip().upper()
        if run_market != normalized_market:
            raise RuntimeError(
                f"Model run {run_id} market {run_market!r} cannot write "
                f"{normalized_market} live facts."
            )

        prediction_table, detail_table, _ = physical_hot_prediction_models(
            normalized_market
        )
        latest_trade_date = self.db.scalar(
            select(func.max(prediction_table.trade_date)).where(
                prediction_table.model_run_id == run_id
            )
        )
        if latest_trade_date is None:
            raise RuntimeError(
                f"Physical hot predictions for model run {run_id} are empty; "
                "refusing live publish."
            )

        settings = get_settings()
        targets = [physical_live_prediction_model(normalized_market)]
        if legacy_mirror_write_enabled(
            self.db,
            market=normalized_market,
            configured=settings.market_physical_live_dual_write_legacy,
        ):
            targets.append(LivePrediction)

        published_at = app_now()
        selected_columns = [
            "model_run_id",
            "symbol_id",
            "market",
            "trade_date",
            "score",
            "rank_value",
            *self.DETAIL_FIELDS,
            "published_at",
        ]
        source_select = select(
            prediction_table.model_run_id,
            prediction_table.symbol_id,
            prediction_table.market,
            prediction_table.trade_date,
            prediction_table.score,
            prediction_table.rank_value,
            *(getattr(detail_table, field) for field in self.DETAIL_FIELDS),
            literal(published_at),
        ).select_from(prediction_table).outerjoin(
            detail_table,
            detail_table.prediction_id == prediction_table.id,
        ).where(
            prediction_table.model_run_id == run_id,
            prediction_table.trade_date == latest_trade_date,
        )

        try:
            self.last_publish_actions = {}
            for target in targets:
                self.db.execute(delete(target).where(target.model_run_id == run_id))
                self.db.execute(
                    insert(target).from_select(selected_columns, source_select)
                )
                self.last_publish_actions[str(target.__tablename__)] = (
                    "copied_from_physical_hot"
                )
            if commit:
                self.db.commit()
            self.last_publish_action = "copied_from_physical_hot"
            return int(
                self.db.scalar(
                    select(func.count(prediction_table.id)).where(
                        prediction_table.model_run_id == run_id,
                        prediction_table.trade_date == latest_trade_date,
                    )
                )
                or 0
            )
        except Exception:
            self.db.rollback()
            raise

    def prune_market_snapshots(self, *, market: str, keep_runs: int = 2) -> int:
        normalized_market = str(market or "").strip().upper()
        tables = (physical_live_prediction_model(normalized_market),)
        if not physical_only_cutover_active(self.db, normalized_market):
            tables = (LivePrediction, *tables)
        deleted = 0
        for table in tables:
            protected_run_ids = list(
                self.db.scalars(
                    select(ModelRun.id)
                    .join(table, table.model_run_id == ModelRun.id)
                    .where(
                        ModelRun.status == "success",
                        table.market == normalized_market,
                    )
                    .distinct()
                    .order_by(ModelRun.id.desc())
                    .limit(max(1, int(keep_runs)))
                ).all()
            )
            if not protected_run_ids:
                continue
            result = self.db.execute(
                delete(table).where(
                    table.market == normalized_market,
                    table.model_run_id.not_in(protected_run_ids),
                )
            )
            deleted += int(result.rowcount or 0)
        self.db.commit()
        return deleted

    def remove_for_model_run(self, model_run_id: int) -> int:
        deleted = 0
        for table in (
            LivePrediction,
            physical_live_prediction_model("CN"),
            physical_live_prediction_model("HK"),
            physical_live_prediction_model("US"),
        ):
            result = self.db.execute(
                delete(table).where(table.model_run_id == int(model_run_id))
            )
            deleted += int(result.rowcount or 0)
        self.db.commit()
        return deleted

class PredictionWriteRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_model_run(
        self,
        model_run_id: int,
        rows: list[dict],
        *,
        commit: bool = True,
    ) -> int:
        _assert_legacy_prediction_write_allowed(
            self.db,
            model_run_id=model_run_id,
        )
        prediction_ids = list(
            self.db.scalars(select(Prediction.id).where(Prediction.model_run_id == model_run_id)).all()
        )
        if prediction_ids:
            for prediction_id_chunk in chunked_ids(prediction_ids):
                self.db.execute(
                    delete(PredictionDetail).where(PredictionDetail.prediction_id.in_(prediction_id_chunk))
                )
                self.db.execute(
                    delete(PredictionExplanation).where(PredictionExplanation.prediction_id.in_(prediction_id_chunk))
                )
                self.db.execute(
                    delete(PredictionTradePlan).where(PredictionTradePlan.prediction_id.in_(prediction_id_chunk))
                )
            self.db.execute(delete(Prediction).where(Prediction.model_run_id == model_run_id))
            self.db.flush()

        deduped_rows: dict[tuple[int, str], dict] = {}
        for row in rows:
            key = (int(row["symbol_id"]), str(row["trade_date"]))
            existing = deduped_rows.get(key)
            if existing is None:
                deduped_rows[key] = dict(row)
                continue
            existing_score = float(existing.get("score") or 0.0)
            incoming_score = float(row.get("score") or 0.0)
            existing_rank = float(existing.get("rank_value") or 0.0)
            incoming_rank = float(row.get("rank_value") or 0.0)
            if (
                incoming_score > existing_score
                or (incoming_score == existing_score and (incoming_rank <= existing_rank or existing_rank <= 0.0))
            ):
                deduped_rows[key] = dict(row)

        now = utc_now_iso()
        payload_rows = [
            {
                "model_run_id": model_run_id,
                "symbol_id": int(row["symbol_id"]),
                "trade_date": str(row["trade_date"]),
                "score": row.get("score"),
                "rank_value": row.get("rank_value"),
                "created_at": now,
            }
            for row in deduped_rows.values()
        ]
        # Six bound columns per row allow a 5,000-row batch under the 60,000
        # parameter safety budget.  Existing rows were removed above and the
        # payload is deduplicated, so plain INSERT preserves fail-closed unique
        # constraints while materially shortening the observed dual-write path.
        for row_chunk in chunked_rows(payload_rows, 5000):
            self.db.execute(insert(Prediction).values(row_chunk))

        if commit:
            self.db.commit()
        return len(deduped_rows)

    def list_for_model_run(self, model_run_id: int) -> list:
        market = str(
            self.db.scalar(
                select(ModelRun.market).where(ModelRun.id == int(model_run_id))
            )
            or ""
        ).strip().upper()
        if market in physical_fact_write_markets():
            prediction_table, _, _ = physical_hot_prediction_models(market)
            physical_stmt = (
                select(prediction_table)
                .where(prediction_table.model_run_id == int(model_run_id))
                .order_by(
                    prediction_table.trade_date.asc(),
                    prediction_table.rank_value.asc(),
                )
            )
            physical_rows = list(self.db.scalars(physical_stmt).all())
            if physical_rows:
                return physical_rows
        stmt = (
            select(Prediction)
            .where(Prediction.model_run_id == int(model_run_id))
            .order_by(Prediction.trade_date.asc(), Prediction.rank_value.asc())
        )
        return list(self.db.scalars(stmt).all())
