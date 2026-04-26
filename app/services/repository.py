import json
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, delete, desc, func, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.schema import SymbolCreate
from app.models.tables import (
    AppSetting,
    ConceptSnapshot,
    DataJob,
    FundamentalSnapshot,
    ModelRun,
    ModelChartSignal,
    Prediction,
    PredictionDetail,
    PredictionExplanation,
    PredictionTradePlan,
    PriceSyncState,
    StrategyDailyMetric,
    StrategyRun,
    Symbol,
    TechnicalSnapshot,
    Watchlist,
    WatchlistItem,
    WorkspaceSnapshot,
)
from app.services.tradability_filter import evaluate_candidate_tradability
from app.services.time_utils import app_now, app_now_iso


def utc_now_iso() -> str:
    return app_now_iso()


def _loads_json_object(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _safe_parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _job_duration_seconds(started_at: str | None, finished_at: str | None) -> int | None:
    started = _safe_parse_iso(started_at)
    finished = _safe_parse_iso(finished_at)
    if started is None or finished is None:
        return None
    if started.tzinfo is None and finished.tzinfo is not None:
        started = started.replace(tzinfo=finished.tzinfo)
    elif started.tzinfo is not None and finished.tzinfo is None:
        finished = finished.replace(tzinfo=started.tzinfo)
    elif started.tzinfo is not None and finished.tzinfo is not None:
        started = started.astimezone(UTC)
        finished = finished.astimezone(UTC)
    return max(0, int((finished - started).total_seconds()))


def ticker_query_candidates(ticker: str) -> list[str]:
    normalized = ticker.strip().upper()
    candidates = [normalized]
    if normalized.endswith(".HK"):
        core = normalized[:-3]
        if core.isdigit():
            raw = core.lstrip("0") or "0"
            for width in (4, 5):
                candidate = f"{raw.zfill(width)}.HK"
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def chunked_ids(values: list[int], size: int = 500) -> list[list[int]]:
    if size < 1:
        size = 1
    return [values[index : index + size] for index in range(0, len(values), size)]


def chunked_rows(values: list[dict], size: int = 100) -> list[list[dict]]:
    if size < 1:
        size = 1
    return [values[index : index + size] for index in range(0, len(values), size)]


def market_sort_case(column):
    return case(
        (column == "CN", 0),
        (column == "HK", 1),
        (column == "US", 2),
        else_=9,
    )


def _is_sqlite_locked_error(exc: Exception) -> bool:
    return "database is locked" in str(exc).lower()


def _sleep_for_lock_retry(attempt: int) -> None:
    time.sleep(min(0.2 * attempt, 1.0))


class SymbolRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_symbols(self) -> list[Symbol]:
        stmt = select(Symbol).order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        return list(self.db.scalars(stmt).all())

    def get_by_ticker(self, ticker: str) -> Symbol | None:
        candidates = ticker_query_candidates(ticker)
        stmt = select(Symbol).where(Symbol.ticker.in_(candidates)).order_by(Symbol.ticker.asc())
        return self.db.scalar(stmt)

    def create_symbol(self, payload: SymbolCreate) -> Symbol:
        now = utc_now_iso()
        symbol = Symbol(
            ticker=payload.ticker.upper(),
            name=payload.name,
            market=payload.market,
            exchange=payload.exchange,
            sector=payload.sector,
            industry=payload.industry,
            is_active=1,
            created_at=now,
            updated_at=now,
        )
        self.db.add(symbol)
        self.db.commit()
        self.db.refresh(symbol)
        return symbol

    def get_or_create_symbol(self, payload: SymbolCreate) -> Symbol:
        existing = self.get_by_ticker(payload.ticker)
        if existing is not None:
            changed = False
            if payload.name and (not existing.name or existing.name == existing.ticker):
                existing.name = payload.name
                changed = True
            if payload.market and not existing.market:
                existing.market = payload.market
                changed = True
            if payload.exchange and not existing.exchange:
                existing.exchange = payload.exchange
                changed = True
            if payload.sector and not existing.sector:
                existing.sector = payload.sector
                changed = True
            if payload.industry and not existing.industry:
                existing.industry = payload.industry
                changed = True
            if changed:
                existing.updated_at = utc_now_iso()
                self.db.commit()
                self.db.refresh(existing)
            return existing
        return self.create_symbol(payload)

    def get_overview(self, ticker: str) -> dict | None:
        symbol = self.get_by_ticker(ticker)
        if symbol is None:
            return None
        return {
            "id": symbol.id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "market": symbol.market,
            "exchange": symbol.exchange,
            "sector": symbol.sector,
            "industry": symbol.industry,
            "is_active": symbol.is_active,
            "created_at": symbol.created_at,
            "updated_at": symbol.updated_at,
        }

    def list_overviews_for_tickers(self, tickers: list[str]) -> dict[str, dict]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
        if not normalized:
            return {}
        stmt = select(Symbol).where(Symbol.ticker.in_(normalized)).order_by(Symbol.ticker.asc())
        rows = self.db.scalars(stmt).all()
        return {
            symbol.ticker: {
                "id": symbol.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "exchange": symbol.exchange,
                "sector": symbol.sector,
                "industry": symbol.industry,
                "is_active": symbol.is_active,
                "created_at": symbol.created_at,
                "updated_at": symbol.updated_at,
            }
            for symbol in rows
        }

    def update_symbol_metadata(
        self,
        symbol_id: int,
        *,
        name: str | None = None,
        market: str | None = None,
        exchange: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        overwrite_name: bool = False,
        overwrite_exchange: bool = False,
        overwrite_sector: bool = False,
        overwrite_industry: bool = False,
    ) -> Symbol | None:
        symbol = self.db.scalar(select(Symbol).where(Symbol.id == symbol_id))
        if symbol is None:
            return None

        changed = False
        if name and (overwrite_name or not symbol.name or symbol.name == symbol.ticker):
            symbol.name = name
            changed = True
        if market and not symbol.market:
            symbol.market = market
            changed = True
        if exchange and (overwrite_exchange or not symbol.exchange):
            symbol.exchange = exchange
            changed = True
        if sector and (overwrite_sector or not symbol.sector):
            symbol.sector = sector
            changed = True
        if industry and (overwrite_industry or not symbol.industry):
            symbol.industry = industry
            changed = True

        if changed:
            symbol.updated_at = utc_now_iso()
            self.db.commit()
            self.db.refresh(symbol)
        return symbol

    def list_symbols_for_metadata_refresh(
        self,
        *,
        market: str,
        limit: int = 200,
        only_missing: bool = True,
    ) -> list[Symbol]:
        stmt = select(Symbol).where(Symbol.market == market.upper())
        weak_name = or_(Symbol.name.is_(None), Symbol.name == "", func.upper(Symbol.name) == func.upper(Symbol.ticker))
        missing_exchange = or_(Symbol.exchange.is_(None), Symbol.exchange == "")
        missing_sector = or_(Symbol.sector.is_(None), Symbol.sector == "")
        missing_industry = or_(Symbol.industry.is_(None), Symbol.industry == "")
        if only_missing:
            stmt = stmt.where(or_(weak_name, missing_exchange, missing_sector, missing_industry))
        stmt = stmt.order_by(
            case((missing_sector, 0), else_=1),
            case((missing_industry, 0), else_=1),
            case((weak_name, 0), else_=1),
            case((missing_exchange, 0), else_=1),
            Symbol.updated_at.asc(),
            Symbol.ticker.asc(),
        ).limit(max(1, int(limit)))
        return list(self.db.scalars(stmt).all())


class PredictionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

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
        decision = evaluate_candidate_tradability(candidate)
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

    def list_latest_predictions(self, limit: int = 20) -> list[dict]:
        return self.list_latest_predictions_for_market(market=None, limit=limit)

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
        latest_run_stmt = select(func.max(Prediction.model_run_id)).select_from(Prediction)
        if normalized_market and normalized_market != "ALL":
            latest_run_stmt = (
                latest_run_stmt
                .join(Symbol, Symbol.id == Prediction.symbol_id)
                .where(Symbol.market == normalized_market)
            )
        latest_model_run_id = self.db.scalar(latest_run_stmt)
        if latest_model_run_id is None:
            return []

        latest_date_stmt = (
            select(func.max(Prediction.trade_date))
            .select_from(Prediction)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Prediction.model_run_id == latest_model_run_id)
        )
        if normalized_market and normalized_market != "ALL":
            latest_date_stmt = latest_date_stmt.where(Symbol.market == normalized_market)
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
            }
            for prediction, symbol, detail in rows
        ]

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
        if effective_trade_date is None:
            return []

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
            }
            for prediction, symbol, detail in rows
        ]

    def list_symbol_predictions(self, ticker: str, limit: int = 120, latest_run_only: bool = False) -> list[dict]:
        latest_model_run_id = None
        if latest_run_only:
            latest_model_run_id = self.db.scalar(select(func.max(Prediction.model_run_id)))

        stmt = select(Prediction, Symbol).join(Symbol, Symbol.id == Prediction.symbol_id).where(
            Symbol.ticker == ticker.upper()
        )
        if latest_model_run_id is not None:
            stmt = stmt.where(Prediction.model_run_id == latest_model_run_id)

        stmt = stmt.order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc()).limit(limit)
        rows = self.db.execute(stmt).all()
        return [
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

    def get_latest_model_output_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(Prediction, Symbol, ModelRun, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None

        prediction, symbol, model_run, prediction_detail = row
        peer_count = self.db.scalar(
            select(func.count(Prediction.id))
            .where(Prediction.model_run_id == prediction.model_run_id)
            .where(Prediction.trade_date == prediction.trade_date)
        ) or 0

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
        return payload

    def get_latest_model_outputs_for_tickers(self, tickers: list[str]) -> dict[str, dict]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
        if not normalized:
            return {}

        stmt = (
            select(Prediction, Symbol, ModelRun, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Symbol.ticker.in_(normalized))
            .order_by(Symbol.ticker.asc(), Prediction.trade_date.desc(), Prediction.model_run_id.desc())
        )
        rows = self.db.execute(stmt).all()

        latest_rows: dict[str, tuple[Prediction, Symbol, ModelRun, PredictionDetail | None]] = {}
        pairs: set[tuple[int, str]] = set()
        for prediction, symbol, model_run, prediction_detail in rows:
            ticker = symbol.ticker
            if ticker in latest_rows:
                continue
            latest_rows[ticker] = (prediction, symbol, model_run, prediction_detail)
            pairs.add((prediction.model_run_id, prediction.trade_date))

        if not latest_rows:
            return {}

        pair_counts: dict[tuple[int, str], int] = {}
        for model_run_id, trade_date in pairs:
            pair_counts[(model_run_id, trade_date)] = self.db.scalar(
                select(func.count(Prediction.id))
                .where(Prediction.model_run_id == model_run_id)
                .where(Prediction.trade_date == trade_date)
            ) or 0

        payloads: dict[str, dict] = {}
        for ticker, row in latest_rows.items():
            prediction, symbol, model_run, prediction_detail = row
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
            payloads[ticker] = payload
        return payloads

    def list_recent_prediction_snapshots(self, *, top_n: int = 10, limit_runs: int = 4) -> list[dict]:
        pair_stmt = (
            select(Prediction.model_run_id, Prediction.trade_date)
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
        self._batch_size = 50

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
        prediction_stmt = select(Prediction).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.scalars(prediction_stmt).all())
        prediction_ids = [prediction.id for prediction in predictions]
        if prediction_ids:
            for prediction_id_chunk in chunked_ids(prediction_ids):
                self.db.execute(
                    delete(PredictionExplanation).where(
                        PredictionExplanation.prediction_id.in_(prediction_id_chunk)
                    )
                )
                self.db.commit()

        if not rows:
            return 0

        prediction_map = {(prediction.symbol_id, prediction.trade_date): prediction.id for prediction in predictions}
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
            self.db.commit()

        return inserted

    def get_for_prediction(self, prediction_id: int) -> list[dict]:
        stmt = (
            select(PredictionExplanation)
            .where(PredictionExplanation.prediction_id == prediction_id)
            .order_by(PredictionExplanation.display_order.asc(), desc(func.abs(PredictionExplanation.contribution)))
        )
        rows = self.db.scalars(stmt).all()
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

    def get_latest_for_ticker(self, ticker: str) -> list[dict]:
        stmt = (
            select(Prediction.id)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc())
            .limit(1)
        )
        prediction_id = self.db.scalar(stmt)
        if prediction_id is None:
            return []
        return self.get_for_prediction(prediction_id)


class PredictionDetailRepository:
    def __init__(self, db: Session) -> None:
        self.db = db
        self._batch_size = 50

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
        prediction_stmt = select(Prediction).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.scalars(prediction_stmt).all())
        prediction_ids = [prediction.id for prediction in predictions]
        if prediction_ids:
            for prediction_id_chunk in chunked_ids(prediction_ids):
                self.db.execute(
                    delete(PredictionDetail).where(PredictionDetail.prediction_id.in_(prediction_id_chunk))
                )
                self.db.commit()

        if not rows:
            return 0

        prediction_map = {(prediction.symbol_id, prediction.trade_date): prediction.id for prediction in predictions}
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
            stmt = pg_insert(PredictionDetail).values(row_chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[PredictionDetail.prediction_id],
                set_={
                    "confidence": stmt.excluded.confidence,
                    "bullish_prob": stmt.excluded.bullish_prob,
                    "bearish_prob": stmt.excluded.bearish_prob,
                    "expected_return_5d": stmt.excluded.expected_return_5d,
                    "expected_return_20d": stmt.excluded.expected_return_20d,
                    "expected_drawdown_20d": stmt.excluded.expected_drawdown_20d,
                    "model_reward_risk_ratio": stmt.excluded.model_reward_risk_ratio,
                    "risk_score": stmt.excluded.risk_score,
                    "target_horizon_days": stmt.excluded.target_horizon_days,
                    "universe_size": stmt.excluded.universe_size,
                    "percentile": stmt.excluded.percentile,
                    "regime_label": stmt.excluded.regime_label,
                    "conviction_bucket": stmt.excluded.conviction_bucket,
                    "position_size_hint": stmt.excluded.position_size_hint,
                    "entry_style": stmt.excluded.entry_style,
                    "signal_label": stmt.excluded.signal_label,
                    "signal_strength": stmt.excluded.signal_strength,
                    "summary_text": stmt.excluded.summary_text,
                    "created_at": stmt.excluded.created_at,
                },
            )
            self.db.execute(stmt)
            self.db.commit()

        return inserted


class ModelChartSignalRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
        stmt = select(ModelChartSignal).where(ModelChartSignal.model_run_id == model_run_id)
        for signal in self.db.scalars(stmt).all():
            self.db.delete(signal)
        self.db.flush()

        if not rows:
            self.db.commit()
            return 0

        now = utc_now_iso()
        inserted = 0
        for row in rows:
            signal = ModelChartSignal(
                model_run_id=model_run_id,
                symbol_id=row["symbol_id"],
                trade_date=row["trade_date"],
                score=row.get("score"),
                rank_value=row.get("rank_value"),
                signal_label=row.get("signal_label"),
                signal_strength=row.get("signal_strength"),
                note=row.get("note"),
                created_at=now,
            )
            self.db.add(signal)
            inserted += 1

        self.db.commit()
        return inserted

    def get_latest_for_ticker(self, ticker: str, *, limit: int = 180) -> list[dict]:
        latest_model_run_id = self.db.scalar(select(func.max(ModelChartSignal.model_run_id)))
        if latest_model_run_id is None:
            return []
        stmt = (
            select(ModelChartSignal, Symbol)
            .join(Symbol, Symbol.id == ModelChartSignal.symbol_id)
            .where(ModelChartSignal.model_run_id == latest_model_run_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(ModelChartSignal.trade_date.desc())
            .limit(limit)
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "trade_date": signal.trade_date,
                "score": signal.score,
                "rank_value": signal.rank_value,
                "signal_label": signal.signal_label,
                "signal_strength": signal.signal_strength,
                "note": signal.note,
                "ticker": symbol.ticker,
            }
            for signal, symbol in rows
        ]


class PredictionTradePlanRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
        prediction_stmt = select(Prediction).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.scalars(prediction_stmt).all())
        prediction_ids = [prediction.id for prediction in predictions]
        if prediction_ids:
            for prediction_id_chunk in chunked_ids(prediction_ids):
                trade_plan_stmt = select(PredictionTradePlan).where(
                    PredictionTradePlan.prediction_id.in_(prediction_id_chunk)
                )
                for trade_plan in self.db.scalars(trade_plan_stmt).all():
                    self.db.delete(trade_plan)
            self.db.flush()

        if not rows:
            self.db.commit()
            return 0

        prediction_map = {(prediction.symbol_id, prediction.trade_date): prediction.id for prediction in predictions}
        now = utc_now_iso()
        inserted = 0
        for row in rows:
            prediction_id = prediction_map.get((row["symbol_id"], row["trade_date"]))
            if prediction_id is None:
                continue
            trade_plan = PredictionTradePlan(
                prediction_id=prediction_id,
                entry_low=row.get("entry_low"),
                entry_high=row.get("entry_high"),
                breakout_level=row.get("breakout_level"),
                take_profit_low=row.get("take_profit_low"),
                take_profit_high=row.get("take_profit_high"),
                risk_level=row.get("risk_level"),
                support_level=row.get("support_level"),
                resistance_level=row.get("resistance_level"),
                stop_type=row.get("stop_type"),
                trailing_stop_pct=row.get("trailing_stop_pct"),
                invalidation_reason=row.get("invalidation_reason"),
                execution_tags_json=json.dumps(row.get("execution_tags") or []),
                note=row.get("note"),
                created_at=now,
            )
            self.db.add(trade_plan)
            inserted += 1

        self.db.commit()
        return inserted

    def get_latest_for_ticker(self, ticker: str) -> dict | None:
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

        stmt = (
            select(PredictionTradePlan, Prediction, Symbol)
            .join(Prediction, Prediction.id == PredictionTradePlan.prediction_id)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Symbol.ticker.in_(normalized))
            .order_by(Symbol.ticker.asc(), Prediction.trade_date.desc(), Prediction.model_run_id.desc())
        )
        rows = self.db.execute(stmt).all()
        payloads: dict[str, dict] = {}
        for row, prediction, symbol in rows:
            if symbol.ticker in payloads:
                continue
            payloads[symbol.ticker] = {
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
        return payloads


class BacktestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _build_backtest_payload(self, row: StrategyRun) -> dict:
        summary = _loads_json_object(row.summary_json)
        validation = None
        if summary is not None:
            validation = {
                "annualized_return": summary.get("annualized_return"),
                "annualized_volatility": summary.get("annualized_volatility"),
                "sharpe_like": summary.get("sharpe_like"),
                "information_ratio": summary.get("information_ratio"),
                "calmar_like": summary.get("calmar_like"),
                "max_drawdown": summary.get("max_drawdown"),
                "hit_ratio": summary.get("hit_ratio"),
                "excess_hit_ratio": summary.get("excess_hit_ratio"),
                "avg_turnover": summary.get("avg_turnover"),
                "candidate_pass_rate": summary.get("candidate_pass_rate"),
                "selection_rate": summary.get("selection_rate"),
                "avg_selected_names": summary.get("avg_selected_names"),
                "cost_assumption_bps": summary.get("cost_assumption_bps"),
                "gate_stats": summary.get("gate_stats") or {},
                "capacity_flags": summary.get("capacity_flags") or {},
            }
        return {
            "id": row.id,
            "name": row.name,
            "strategy_type": row.strategy_type,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "status": row.status,
            "summary_json": row.summary_json,
            "summary": summary,
            "validation_summary": validation,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }

    def list_backtests(self) -> list[dict]:
        stmt = select(StrategyRun).order_by(StrategyRun.created_at.desc())
        rows = self.db.scalars(stmt).all()
        return [self._build_backtest_payload(row) for row in rows]

    def get_latest_backtest(self) -> StrategyRun | None:
        stmt = select(StrategyRun).order_by(StrategyRun.id.desc()).limit(1)
        return self.db.scalar(stmt)

    def get_latest_backtest_summary(self) -> dict | None:
        backtest = self.get_latest_backtest()
        if backtest is None:
            return None
        return self._build_backtest_payload(backtest)

    def get_daily_metrics(self, strategy_run_id: int) -> list[dict]:
        stmt = (
            select(StrategyDailyMetric)
            .where(StrategyDailyMetric.strategy_run_id == strategy_run_id)
            .order_by(StrategyDailyMetric.trade_date.asc())
        )
        rows = self.db.scalars(stmt).all()
        return [
            {
                "trade_date": row.trade_date,
                "nav": row.nav,
                "daily_return": row.daily_return,
                "benchmark_return": row.benchmark_return,
                "drawdown": row.drawdown,
                "turnover": row.turnover,
            }
            for row in rows
        ]

    def get_latest_backtest_curve(self) -> list[dict]:
        latest = self.get_latest_backtest()
        if latest is None:
            return []
        return self.get_daily_metrics(latest.id)


class PriceSyncStateRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_states(self) -> list[PriceSyncState]:
        stmt = (
            select(PriceSyncState)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        return list(self.db.scalars(stmt).all())

    def list_states_with_symbols(self) -> list[dict]:
        stmt = (
            select(PriceSyncState, Symbol)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "symbol_id": state.symbol_id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "provider": state.provider,
                "last_synced_date": state.last_synced_date,
                "status": state.status,
                "message": state.message,
                "updated_at": state.updated_at,
            }
            for state, symbol in rows
        ]

    def list_recent_states_with_symbols(self, limit: int = 5) -> list[dict]:
        stmt = (
            select(PriceSyncState, Symbol)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .order_by(desc(PriceSyncState.updated_at), market_sort_case(Symbol.market), Symbol.ticker.asc())
            .limit(max(1, limit))
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "symbol_id": state.symbol_id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "provider": state.provider,
                "last_synced_date": state.last_synced_date,
                "status": state.status,
                "message": state.message,
                "updated_at": state.updated_at,
            }
            for state, symbol in rows
        ]

    def get_status_overview(self) -> dict:
        rows = self.db.execute(
            select(
                PriceSyncState.status,
                PriceSyncState.provider,
                func.count().label("count"),
                func.max(PriceSyncState.updated_at).label("latest_updated_at"),
            ).group_by(PriceSyncState.status, PriceSyncState.provider)
        ).all()
        status_counts: dict[str, int] = {}
        provider_counts: dict[str, int] = {}
        latest_updated_at = None
        total = 0
        for row in rows:
            count = int(row.count or 0)
            total += count
            status = str(row.status or "unknown")
            provider = str(row.provider or "unknown")
            status_counts[status] = status_counts.get(status, 0) + count
            provider_counts[provider] = provider_counts.get(provider, 0) + count
            updated_at = row.latest_updated_at
            if updated_at and (latest_updated_at is None or str(updated_at) > str(latest_updated_at)):
                latest_updated_at = updated_at
        return {
            "total": total,
            "success": status_counts.get("success", 0),
            "status_counts": status_counts,
            "provider_counts": provider_counts,
            "latest_updated_at": latest_updated_at,
        }

    def get_state_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(PriceSyncState, Symbol)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .where(Symbol.ticker == ticker.upper())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        state, symbol = row
        return {
            "symbol_id": state.symbol_id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "provider": state.provider,
            "last_synced_date": state.last_synced_date,
            "status": state.status,
            "message": state.message,
            "updated_at": state.updated_at,
        }

    def upsert_state(
        self,
        *,
        symbol_id: int,
        provider: str,
        last_synced_date: str | None,
        status: str,
        message: str | None = None,
    ) -> PriceSyncState:
        attempts = 4
        for attempt in range(1, attempts + 1):
            stmt = select(PriceSyncState).where(PriceSyncState.symbol_id == symbol_id)
            existing = self.db.scalar(stmt)
            now = utc_now_iso()

            if existing is None:
                existing = PriceSyncState(
                    symbol_id=symbol_id,
                    provider=provider,
                    last_synced_date=last_synced_date,
                    status=status,
                    message=message,
                    updated_at=now,
                )
                self.db.add(existing)
            else:
                existing.provider = provider
                existing.last_synced_date = last_synced_date
                existing.status = status
                existing.message = message
                existing.updated_at = now
            try:
                self.db.commit()
                self.db.refresh(existing)
                return existing
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_sqlite_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Price sync state upsert exhausted retries.")


class DataJobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _serialize_job(self, row: DataJob) -> dict:
        params = _loads_json_object(row.params_json)
        result = (params or {}).get("result") if isinstance(params, dict) else None
        runtime = (params or {}).get("job_runtime") if isinstance(params, dict) else None
        return {
            "id": row.id,
            "job_type": row.job_type,
            "status": row.status,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "message": row.message,
            "params_json": row.params_json,
            "params": params,
            "result": result,
            "pipeline_step": (params or {}).get("pipeline_step"),
            "depends_on": (params or {}).get("depends_on") or [],
            "input_summary": (params or {}).get("input_summary"),
            "output_summary": (result or {}).get("output_summary") if isinstance(result, dict) else None,
            "quality_summary": (result or {}).get("quality_summary") if isinstance(result, dict) else None,
            "retry_count": (result or {}).get("retry_count", 0) if isinstance(result, dict) else 0,
            "duration_seconds": (
                (runtime or {}).get("duration_seconds")
                if isinstance(runtime, dict)
                else _job_duration_seconds(row.started_at, row.finished_at)
            ),
        }

    def create_job(self, *, job_type: str, status: str, params: dict | None = None, message: str | None = None) -> DataJob:
        attempts = 4
        for attempt in range(1, attempts + 1):
            job = DataJob(
                job_type=job_type,
                status=status,
                started_at=utc_now_iso(),
                finished_at=None,
                message=message,
                params_json=json.dumps(params, ensure_ascii=False) if params is not None else None,
            )
            self.db.add(job)
            try:
                self.db.commit()
                self.db.refresh(job)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_sqlite_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job creation exhausted retries.")

    def complete_job(
        self,
        job_id: int,
        *,
        status: str,
        message: str | None = None,
        result: dict | None = None,
    ) -> DataJob | None:
        attempts = 4
        for attempt in range(1, attempts + 1):
            stmt = select(DataJob).where(DataJob.id == job_id)
            job = self.db.scalar(stmt)
            if job is None:
                return None
            job.status = status
            job.finished_at = utc_now_iso()
            job.message = message
            if result is not None:
                params = _loads_json_object(job.params_json) or {}
                params["result"] = result
                params["job_runtime"] = {
                    "duration_seconds": _job_duration_seconds(job.started_at, job.finished_at),
                    "completed_at": job.finished_at,
                }
                job.params_json = json.dumps(params, ensure_ascii=False)
            try:
                self.db.commit()
                self.db.refresh(job)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_sqlite_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job completion exhausted retries.")

    def list_recent_jobs(self, limit: int = 20) -> list[dict]:
        stmt = select(DataJob).order_by(DataJob.id.desc()).limit(limit)
        rows = self.db.scalars(stmt).all()
        return [self._serialize_job(row) for row in rows]

    def get_latest_job(self, job_type: str | list[str] | tuple[str, ...] | set[str]) -> dict | None:
        if isinstance(job_type, (list, tuple, set)):
            job_types = [str(item or "").strip() for item in job_type if str(item or "").strip()]
        else:
            job_types = [str(job_type or "").strip()] if str(job_type or "").strip() else []
        if not job_types:
            return None
        stmt = (
            select(DataJob)
            .where(DataJob.job_type.in_(job_types))
            .order_by(DataJob.id.desc())
            .limit(1)
        )
        row = self.db.scalar(stmt)
        return self._serialize_job(row) if row is not None else None

    def has_running_job(self, job_type: str) -> bool:
        stmt = (
            select(DataJob.id)
            .where(DataJob.job_type == job_type)
            .where(DataJob.status == "running")
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def complete_stale_running_jobs(
        self,
        *,
        job_types: list[str] | None = None,
        stale_after_hours: int = 6,
        message_prefix: str = "Marked stale running job as failed.",
    ) -> int:
        cutoff = app_now() - timedelta(hours=max(1, stale_after_hours))
        stmt = select(DataJob).where(DataJob.status == "running")
        if job_types:
            stmt = stmt.where(DataJob.job_type.in_(job_types))
        rows = self.db.scalars(stmt).all()
        updated = 0
        now_iso = utc_now_iso()
        for row in rows:
            try:
                started_at = datetime.fromisoformat(row.started_at)
            except (TypeError, ValueError):
                started_at = None
            if started_at is None or started_at > cutoff:
                continue
            row.status = "failed"
            row.finished_at = now_iso
            original_message = (row.message or "").strip()
            row.message = (
                f"{message_prefix} Original state started at {row.started_at}."
                if not original_message
                else f"{message_prefix} {original_message}"
            )
            updated += 1
        if updated:
            self.db.commit()
        return updated


class WorkspaceSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_snapshot(
        self,
        *,
        snapshot_type: str,
        snapshot_date: str,
        payload: dict,
        source_job_id: int | None = None,
    ) -> WorkspaceSnapshot:
        attempts = 4
        for attempt in range(1, attempts + 1):
            snapshot = WorkspaceSnapshot(
                snapshot_type=snapshot_type,
                snapshot_date=snapshot_date,
                payload_json=json.dumps(payload, ensure_ascii=False),
                source_job_id=source_job_id,
                created_at=utc_now_iso(),
            )
            self.db.add(snapshot)
            try:
                self.db.commit()
                self.db.refresh(snapshot)
                return snapshot
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_sqlite_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Workspace snapshot creation exhausted retries.")

    def get_latest_snapshot(self, snapshot_type: str) -> dict | None:
        stmt = (
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.snapshot_type == snapshot_type)
            .order_by(WorkspaceSnapshot.id.desc())
            .limit(1)
        )
        row = self.db.scalar(stmt)
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            payload = None
        return {
            "id": row.id,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "payload": payload,
            "source_job_id": row.source_job_id,
            "created_at": row.created_at,
        }

    def list_snapshots(self, snapshot_type: str, *, limit: int = 20) -> list[dict]:
        stmt = (
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.snapshot_type == snapshot_type)
            .order_by(WorkspaceSnapshot.id.desc())
            .limit(limit)
        )
        rows = self.db.scalars(stmt).all()
        results: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                payload = None
            results.append(
                {
                    "id": row.id,
                    "snapshot_type": row.snapshot_type,
                    "snapshot_date": row.snapshot_date,
                    "payload": payload,
                    "source_job_id": row.source_job_id,
                    "created_at": row.created_at,
                }
            )
        return results

    def get_snapshot(self, snapshot_id: int, *, snapshot_type: str | None = None) -> dict | None:
        stmt = select(WorkspaceSnapshot).where(WorkspaceSnapshot.id == snapshot_id)
        if snapshot_type:
            stmt = stmt.where(WorkspaceSnapshot.snapshot_type == snapshot_type)
        row = self.db.scalar(stmt.limit(1))
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            payload = None
        return {
            "id": row.id,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "payload": payload,
            "source_job_id": row.source_job_id,
            "created_at": row.created_at,
        }


class DashboardReadRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def load_summary_snapshot(self) -> dict:
        model_repo = ModelRunRepository(self.db)
        signal_repo = PredictionRepository(self.db)
        backtest_repo = BacktestRepository(self.db)
        sync_repo = PriceSyncStateRepository(self.db)
        job_repo = DataJobRepository(self.db)
        concept_repo = ConceptSnapshotRepository(self.db)
        job_repo.complete_stale_running_jobs(
            job_types=["social_us_price_sync"],
            stale_after_hours=1,
            message_prefix="Dashboard cleanup closed a stale social U.S. price sync job.",
        )
        job_repo.complete_stale_running_jobs(
            stale_after_hours=6,
            message_prefix="Dashboard cleanup closed a stale running job.",
        )
        latest_signals = signal_repo.list_latest_signal_decisions(limit=10)
        return {
            "latest_signals": latest_signals,
            "sync_states": sync_repo.list_states_with_symbols(),
            "concept_summary": concept_repo.get_latest_summary(),
            "latest_model": model_repo.get_latest_run_summary(),
            "recent_model_runs": model_repo.list_recent_runs(limit=8),
            "latest_backtest": backtest_repo.get_latest_backtest_summary(),
            "latest_backtest_curve": backtest_repo.get_latest_backtest_curve(),
            "recent_jobs": job_repo.list_recent_jobs(limit=20),
        }


class AppSettingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, key: str) -> str | None:
        setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
        return setting.value if setting is not None else None

    def set(self, key: str, value: str) -> AppSetting:
        attempts = 4
        for attempt in range(1, attempts + 1):
            setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
            now = utc_now_iso()
            if setting is None:
                setting = AppSetting(key=key, value=value, updated_at=now)
                self.db.add(setting)
            else:
                setting.value = value
                setting.updated_at = now
            try:
                self.db.commit()
                self.db.refresh(setting)
                return setting
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_sqlite_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("App setting update exhausted retries.")


class FundamentalSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_snapshot(
        self,
        *,
        symbol_id: int,
        report_date: str,
        source: str,
        listing_date: str | None = None,
        pe_ttm: float | None = None,
        dividend_yield: float | None = None,
        market_cap: float | None = None,
        roe_avg_3y: float | None = None,
        net_profit_yoy: float | None = None,
        revenue_yoy: float | None = None,
        debt_to_assets: float | None = None,
        data: dict | None = None,
    ) -> FundamentalSnapshot:
        stmt = select(FundamentalSnapshot).where(
            FundamentalSnapshot.symbol_id == symbol_id,
            FundamentalSnapshot.report_date == report_date,
            FundamentalSnapshot.source == source,
        )
        existing = self.db.scalar(stmt)
        now = utc_now_iso()
        payload = {
            "listing_date": listing_date,
            "pe_ttm": pe_ttm,
            "dividend_yield": dividend_yield,
            "market_cap": market_cap,
            "roe_avg_3y": roe_avg_3y,
            "net_profit_yoy": net_profit_yoy,
            "revenue_yoy": revenue_yoy,
            "debt_to_assets": debt_to_assets,
            "data_json": json.dumps(data) if data is not None else None,
            "updated_at": now,
        }
        if existing is None:
            existing = FundamentalSnapshot(
                symbol_id=symbol_id,
                report_date=report_date,
                source=source,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def get_latest_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(FundamentalSnapshot, Symbol)
            .join(Symbol, Symbol.id == FundamentalSnapshot.symbol_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(FundamentalSnapshot.report_date.desc(), FundamentalSnapshot.id.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        snapshot, symbol = row
        return self._to_dict(snapshot, symbol)

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        symbol_stmt = select(Symbol.id, Symbol.ticker, Symbol.name, Symbol.market)
        if market and market != "ALL":
            symbol_stmt = symbol_stmt.where(Symbol.market == market)
        if tickers:
            symbol_stmt = symbol_stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        symbol_rows = self.db.execute(symbol_stmt).all()
        if not symbol_rows:
            return []

        symbol_map = {
            row.id: {
                "ticker": row.ticker,
                "name": row.name,
                "market": row.market,
            }
            for row in symbol_rows
        }

        subquery = (
            select(
                FundamentalSnapshot.symbol_id,
                func.max(FundamentalSnapshot.report_date).label("max_report_date"),
            )
            .where(FundamentalSnapshot.symbol_id.in_(list(symbol_map)))
            .group_by(FundamentalSnapshot.symbol_id)
            .subquery()
        )
        stmt = (
            select(FundamentalSnapshot)
            .join(
                subquery,
                (FundamentalSnapshot.symbol_id == subquery.c.symbol_id)
                & (FundamentalSnapshot.report_date == subquery.c.max_report_date),
            )
            .order_by(FundamentalSnapshot.symbol_id.asc(), FundamentalSnapshot.id.desc())
        )
        rows = self.db.scalars(stmt).all()
        deduped: dict[int, dict] = {}
        for snapshot in rows:
            if snapshot.symbol_id in deduped:
                continue
            symbol = symbol_map.get(snapshot.symbol_id)
            if symbol is None:
                continue
            deduped[snapshot.symbol_id] = self._to_dict(snapshot, symbol)
        return list(deduped.values())

    def _to_dict(self, snapshot: FundamentalSnapshot, symbol: Symbol | dict) -> dict:
        ticker = symbol.ticker if hasattr(symbol, "ticker") else symbol["ticker"]
        name = symbol.name if hasattr(symbol, "name") else symbol.get("name")
        market = symbol.market if hasattr(symbol, "market") else symbol.get("market")
        return {
            "symbol_id": snapshot.symbol_id,
            "ticker": ticker,
            "name": name,
            "market": market,
            "report_date": snapshot.report_date,
            "source": snapshot.source,
            "listing_date": snapshot.listing_date,
            "pe_ttm": snapshot.pe_ttm,
            "dividend_yield": snapshot.dividend_yield,
            "market_cap": snapshot.market_cap,
            "roe_avg_3y": snapshot.roe_avg_3y,
            "net_profit_yoy": snapshot.net_profit_yoy,
            "revenue_yoy": snapshot.revenue_yoy,
            "debt_to_assets": snapshot.debt_to_assets,
            "data_json": snapshot.data_json,
        }


class ConceptSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_snapshot(
        self,
        *,
        symbol_id: int,
        concept_name: str,
        as_of_date: str,
        source: str,
        concept_code: str | None = None,
        strength: float | None = None,
        data: dict | None = None,
    ) -> ConceptSnapshot:
        stmt = select(ConceptSnapshot).where(
            ConceptSnapshot.symbol_id == symbol_id,
            ConceptSnapshot.concept_name == concept_name,
            ConceptSnapshot.as_of_date == as_of_date,
            ConceptSnapshot.source == source,
        )
        existing = self.db.scalar(stmt)
        now = utc_now_iso()
        payload = {
            "concept_code": concept_code,
            "strength": strength,
            "data_json": json.dumps(data, ensure_ascii=False) if data is not None else None,
            "updated_at": now,
        }
        if existing is None:
            existing = ConceptSnapshot(
                symbol_id=symbol_id,
                concept_name=concept_name,
                as_of_date=as_of_date,
                source=source,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def list_latest_for_tickers(self, tickers: list[str]) -> list[dict]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
        if not normalized:
            return []
        stmt = (
            select(ConceptSnapshot, Symbol)
            .join(Symbol, Symbol.id == ConceptSnapshot.symbol_id)
            .where(Symbol.ticker.in_(normalized))
            .order_by(Symbol.ticker.asc(), ConceptSnapshot.as_of_date.desc(), ConceptSnapshot.concept_name.asc())
        )
        rows = self.db.execute(stmt).all()
        seen: set[tuple[str, str]] = set()
        payload: list[dict] = []
        for snapshot, symbol in rows:
            key = (symbol.ticker, snapshot.concept_name)
            if key in seen:
                continue
            seen.add(key)
            payload.append(
                {
                    "ticker": symbol.ticker,
                    "name": symbol.name,
                    "market": symbol.market,
                    "concept_name": snapshot.concept_name,
                    "concept_code": snapshot.concept_code,
                    "as_of_date": snapshot.as_of_date,
                    "source": snapshot.source,
                    "strength": snapshot.strength,
                }
            )
        return payload

    def get_latest_summary(self) -> dict:
        latest_date = self.db.scalar(select(func.max(ConceptSnapshot.as_of_date)))
        concept_count = self.db.scalar(select(func.count(func.distinct(ConceptSnapshot.concept_name)))) or 0
        symbol_count = self.db.scalar(select(func.count(func.distinct(ConceptSnapshot.symbol_id)))) or 0
        freshness = "missing"
        if latest_date:
            try:
                days_old = (date.today() - date.fromisoformat(str(latest_date))).days
                if days_old <= 1:
                    freshness = "fresh"
                elif days_old <= 5:
                    freshness = "stale"
                else:
                    freshness = "old"
            except ValueError:
                freshness = "unknown"
        return {
            "latest_as_of_date": latest_date,
            "concept_count": int(concept_count),
            "symbol_count": int(symbol_count),
            "freshness": freshness,
        }


class TechnicalSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_snapshot(
        self,
        *,
        symbol_id: int,
        as_of_date: str | None,
        source: str,
        limit_up_yesterday: bool,
        volume_breakout: bool,
        ma_cluster: bool,
        bullish_ma_stack: bool,
        macd_underwater_cross: bool,
        matched_patterns: list[str] | None = None,
    ) -> TechnicalSnapshot:
        existing = self.db.scalar(select(TechnicalSnapshot).where(TechnicalSnapshot.symbol_id == symbol_id))
        now = utc_now_iso()
        payload = {
            "as_of_date": as_of_date,
            "source": source,
            "limit_up_yesterday": 1 if limit_up_yesterday else 0,
            "volume_breakout": 1 if volume_breakout else 0,
            "ma_cluster": 1 if ma_cluster else 0,
            "bullish_ma_stack": 1 if bullish_ma_stack else 0,
            "macd_underwater_cross": 1 if macd_underwater_cross else 0,
            "matched_patterns_json": json.dumps(matched_patterns or [], ensure_ascii=False),
            "updated_at": now,
        }
        if existing is None:
            existing = TechnicalSnapshot(
                symbol_id=symbol_id,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        stmt = (
            select(TechnicalSnapshot, Symbol)
            .join(Symbol, Symbol.id == TechnicalSnapshot.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        if market and market != "ALL":
            stmt = stmt.where(Symbol.market == market)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        rows = self.db.execute(stmt).all()
        return [self._to_dict(snapshot, symbol) for snapshot, symbol in rows]

    def _to_dict(self, snapshot: TechnicalSnapshot, symbol: Symbol) -> dict:
        matched_patterns = []
        if snapshot.matched_patterns_json:
            try:
                matched_patterns = json.loads(snapshot.matched_patterns_json)
            except json.JSONDecodeError:
                matched_patterns = []
        return {
            "symbol_id": snapshot.symbol_id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "market": symbol.market,
            "exchange": symbol.exchange,
            "as_of_date": snapshot.as_of_date,
            "source": snapshot.source,
            "limit_up_yesterday": bool(snapshot.limit_up_yesterday),
            "volume_breakout": bool(snapshot.volume_breakout),
            "ma_cluster": bool(snapshot.ma_cluster),
            "bullish_ma_stack": bool(snapshot.bullish_ma_stack),
            "macd_underwater_cross": bool(snapshot.macd_underwater_cross),
            "matched_patterns": matched_patterns,
        }


class WatchlistRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_or_create_default(self, name: str = "My Watchlist") -> Watchlist:
        stmt = select(Watchlist).where(Watchlist.name == name)
        watchlist = self.db.scalar(stmt)
        if watchlist is not None:
            return watchlist
        now = utc_now_iso()
        watchlist = Watchlist(name=name, created_at=now, updated_at=now)
        self.db.add(watchlist)
        self.db.commit()
        self.db.refresh(watchlist)
        return watchlist

    def add_symbol(self, watchlist_id: int, symbol_id: int) -> WatchlistItem:
        stmt = select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == symbol_id,
        )
        existing = self.db.scalar(stmt)
        if existing is not None:
            return existing
        item = WatchlistItem(
            watchlist_id=watchlist_id,
            symbol_id=symbol_id,
            sync_enabled=0,
            created_at=utc_now_iso(),
        )
        self.db.add(item)
        watchlist = self.db.scalar(select(Watchlist).where(Watchlist.id == watchlist_id))
        if watchlist is not None:
            watchlist.updated_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(item)
        return item

    def remove_item(self, item_id: int) -> bool:
        item = self.db.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id))
        if item is None:
            return False
        watchlist = self.db.scalar(select(Watchlist).where(Watchlist.id == item.watchlist_id))
        self.db.delete(item)
        if watchlist is not None:
            watchlist.updated_at = utc_now_iso()
        self.db.commit()
        return True

    def set_sync_enabled(self, item_id: int, enabled: bool) -> WatchlistItem | None:
        item = self.db.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id))
        if item is None:
            return None
        item.sync_enabled = 1 if enabled else 0
        watchlist = self.db.scalar(select(Watchlist).where(Watchlist.id == item.watchlist_id))
        if watchlist is not None:
            watchlist.updated_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(item)
        return item

    def list_enabled_tickers(self, watchlist_id: int) -> list[str]:
        stmt = (
            select(Symbol.ticker)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .where(WatchlistItem.sync_enabled == 1)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_item(self, item_id: int) -> dict | None:
        stmt = (
            select(WatchlistItem, Symbol, PriceSyncState)
            .join(Symbol, Symbol.id == WatchlistItem.symbol_id)
            .join(PriceSyncState, PriceSyncState.symbol_id == Symbol.id, isouter=True)
            .where(WatchlistItem.id == item_id)
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        item, symbol, state = row
        return {
            "item_id": item.id,
            "symbol_id": symbol.id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "market": symbol.market,
            "exchange": symbol.exchange,
            "sync_enabled": item.sync_enabled,
            "last_synced_date": state.last_synced_date if state is not None else None,
            "sync_status": state.status if state is not None else None,
        }

    def list_items(self, watchlist_id: int) -> list[dict]:
        stmt = (
            select(WatchlistItem, Symbol, PriceSyncState)
            .join(Symbol, Symbol.id == WatchlistItem.symbol_id)
            .join(PriceSyncState, PriceSyncState.symbol_id == Symbol.id, isouter=True)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "item_id": item.id,
                "symbol_id": symbol.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "exchange": symbol.exchange,
                "sync_enabled": item.sync_enabled,
                "last_synced_date": state.last_synced_date if state is not None else None,
                "sync_status": state.status if state is not None else None,
                "created_at": item.created_at,
            }
            for item, symbol, state in rows
        ]

    def list_symbols_for_watchlist(self, watchlist_id: int) -> list[Symbol]:
        stmt = (
            select(Symbol)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        return list(self.db.scalars(stmt).all())

    def list_ticker_map(self, watchlist_id: int) -> dict[str, dict]:
        stmt = (
            select(WatchlistItem, Symbol, PriceSyncState)
            .join(Symbol, Symbol.id == WatchlistItem.symbol_id)
            .join(PriceSyncState, PriceSyncState.symbol_id == Symbol.id, isouter=True)
            .where(WatchlistItem.watchlist_id == watchlist_id)
        )
        rows = self.db.execute(stmt).all()
        return {
            symbol.ticker: {
                "item_id": item.id,
                "symbol_id": symbol.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "exchange": symbol.exchange,
                "sync_enabled": item.sync_enabled,
                "last_synced_date": state.last_synced_date if state is not None else None,
                "sync_status": state.status if state is not None else None,
            }
            for item, symbol, state in rows
        }


class ModelRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(
        self,
        *,
        name: str,
        model_type: str,
        market: str | None,
        universe: str | None,
        train_start: str | None,
        train_end: str | None,
        test_start: str | None,
        test_end: str | None,
        config: dict | None,
        artifact_path: str | None,
        status: str,
    ) -> ModelRun:
        run = ModelRun(
            name=name,
            model_type=model_type,
            market=market,
            universe=universe,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            config_json=json.dumps(config) if config is not None else None,
            artifact_path=artifact_path,
            status=status,
            created_at=utc_now_iso(),
            finished_at=None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def complete_run(self, run_id: int, status: str, artifact_path: str | None = None) -> ModelRun | None:
        stmt = select(ModelRun).where(ModelRun.id == run_id)
        run = self.db.scalar(stmt)
        if run is None:
            return None
        run.status = status
        run.finished_at = utc_now_iso()
        if artifact_path is not None:
            run.artifact_path = artifact_path
        self.db.commit()
        self.db.refresh(run)
        return run

    def complete_stale_running_runs(
        self,
        *,
        stale_after_hours: int = 6,
        message_prefix: str = "Marked stale running model run as failed.",
    ) -> int:
        cutoff = app_now() - timedelta(hours=max(1, stale_after_hours))
        rows = self.db.scalars(select(ModelRun).where(ModelRun.status == "running")).all()
        updated = 0
        for row in rows:
            try:
                started_at = datetime.fromisoformat(row.created_at)
            except (TypeError, ValueError):
                started_at = None
            if started_at is None or started_at > cutoff:
                continue
            row.status = "failed"
            row.finished_at = utc_now_iso()
            config = {}
            if row.config_json:
                try:
                    config = json.loads(row.config_json)
                except json.JSONDecodeError:
                    config = {}
            config["stale_cleanup_note"] = f"{message_prefix} Original run started at {row.created_at}."
            row.config_json = json.dumps(config, ensure_ascii=False)
            updated += 1
        if updated:
            self.db.commit()
        return updated

    def get_latest_run(self) -> ModelRun | None:
        stmt = select(ModelRun).order_by(ModelRun.id.desc()).limit(1)
        return self.db.scalar(stmt)

    def get_run_by_id(self, run_id: int) -> ModelRun | None:
        stmt = select(ModelRun).where(ModelRun.id == run_id)
        return self.db.scalar(stmt)

    def get_latest_run_summary(self) -> dict | None:
        run = self.get_latest_run()
        if run is None:
            return None
        return {
            "id": run.id,
            "name": run.name,
            "model_type": run.model_type,
            "market": run.market,
            "universe": run.universe,
            "train_start": run.train_start,
            "train_end": run.train_end,
            "test_start": run.test_start,
            "test_end": run.test_end,
            "status": run.status,
            "artifact_path": run.artifact_path,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }

    def get_latest_successful_run(
        self,
        *,
        market: str | None = None,
        model_types: list[str] | None = None,
        universe_like: list[str] | None = None,
    ) -> ModelRun | None:
        stmt = select(ModelRun).where(ModelRun.status == "success")
        if market and str(market).upper() != "ALL":
            normalized_market = str(market).upper()
            stmt = stmt.where(or_(ModelRun.market == normalized_market, ModelRun.market == "MIXED"))
        if model_types:
            normalized_types = [str(item).strip() for item in model_types if str(item).strip()]
            if normalized_types:
                stmt = stmt.where(ModelRun.model_type.in_(normalized_types))
        if universe_like:
            universe_clauses = []
            for candidate in universe_like:
                normalized_candidate = str(candidate or "").strip()
                if not normalized_candidate:
                    continue
                universe_clauses.append(ModelRun.universe == normalized_candidate)
                universe_clauses.append(ModelRun.universe.like(f"{normalized_candidate}%"))
            if universe_clauses:
                stmt = stmt.where(or_(*universe_clauses))
        stmt = stmt.order_by(ModelRun.id.desc()).limit(1)
        return self.db.scalar(stmt)

    def list_recent_runs(self, limit: int = 10) -> list[dict]:
        stmt = select(ModelRun).order_by(ModelRun.id.desc()).limit(limit)
        rows = self.db.scalars(stmt).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "model_type": row.model_type,
                "market": row.market,
                "universe": row.universe,
                "config_json": row.config_json,
                "status": row.status,
                "artifact_path": row.artifact_path,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ]


class PredictionWriteRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
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
        for row_chunk in chunked_rows(payload_rows, 1000):
            stmt = pg_insert(Prediction).values(row_chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Prediction.model_run_id, Prediction.symbol_id, Prediction.trade_date],
                set_={
                    "score": stmt.excluded.score,
                    "rank_value": stmt.excluded.rank_value,
                    "created_at": stmt.excluded.created_at,
                },
            )
            self.db.execute(stmt)

        self.db.commit()
        return len(deduped_rows)

    def list_for_model_run(self, model_run_id: int) -> list[Prediction]:
        stmt = (
            select(Prediction)
            .where(Prediction.model_run_id == model_run_id)
            .order_by(Prediction.trade_date.asc(), Prediction.rank_value.asc())
        )
        return list(self.db.scalars(stmt).all())


class StrategyRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(
        self,
        *,
        model_run_id: int | None,
        name: str,
        strategy_type: str,
        start_date: str | None,
        end_date: str | None,
        config: dict | None,
        status: str,
    ) -> StrategyRun:
        run = StrategyRun(
            model_run_id=model_run_id,
            name=name,
            strategy_type=strategy_type,
            start_date=start_date,
            end_date=end_date,
            config_json=json.dumps(config) if config is not None else None,
            summary_json=None,
            status=status,
            created_at=utc_now_iso(),
            finished_at=None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def replace_daily_metrics(self, strategy_run_id: int, rows: list[dict]) -> int:
        existing_stmt = select(StrategyDailyMetric).where(StrategyDailyMetric.strategy_run_id == strategy_run_id)
        for metric in self.db.scalars(existing_stmt).all():
            self.db.delete(metric)
        self.db.flush()

        now = utc_now_iso()
        for row in rows:
            metric = StrategyDailyMetric(
                strategy_run_id=strategy_run_id,
                trade_date=row["trade_date"],
                nav=row.get("nav"),
                daily_return=row.get("daily_return"),
                benchmark_return=row.get("benchmark_return"),
                drawdown=row.get("drawdown"),
                turnover=row.get("turnover"),
                created_at=now,
            )
            self.db.add(metric)

        self.db.commit()
        return len(rows)

    def complete_run(self, strategy_run_id: int, status: str, summary: dict | None) -> StrategyRun | None:
        stmt = select(StrategyRun).where(StrategyRun.id == strategy_run_id)
        run = self.db.scalar(stmt)
        if run is None:
            return None
        run.status = status
        run.summary_json = json.dumps(summary) if summary is not None else None
        run.finished_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(run)
        return run
