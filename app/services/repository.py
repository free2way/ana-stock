import json
import math
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, delete, desc, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.schema import SymbolCreate
from app.models.tables import (
    AppSetting,
    ConceptSnapshot,
    DataJob,
    FundamentalSnapshot,
    JobDefinition,
    JobRunAttempt,
    JobRunDependency,
    LivePrediction,
    MarketRefreshBatch,
    ModelEvaluation,
    ModelRun,
    ModelChartSignal,
    Prediction,
    PredictionArtifact,
    PredictionDetail,
    PredictionExplanation,
    PredictionTradePlan,
    PointInTimeFeatureSnapshot,
    PriceSyncState,
    StrategyDailyMetric,
    StrategyFill,
    StrategyOrder,
    StrategyPortfolioState,
    StrategyReject,
    StrategyRun,
    Symbol,
    TechnicalSnapshot,
    Watchlist,
    WatchlistItem,
    WorkspaceSnapshot,
)
from app.services.market_context import load_market_context_snapshot
from app.services.market_freshness import (
    classify_market_symbol_anomalies,
    summarize_market_freshness,
)
from app.services.market_storage_routing import (
    enabled_physical_markets,
    legacy_mirror_write_enabled,
    physical_fact_write_markets,
    physical_hot_prediction_models,
    physical_live_prediction_model,
    physical_model_chart_signal_model,
    physical_only_cutover_active,
    physical_prediction_trade_plan_model,
    physical_snapshot_models,
)
from app.services.json_payload_artifacts import (
    JsonPayloadArtifactStore,
    build_payload_envelope,
    canonical_json_bytes,
    resolve_payload_envelope,
    summarize_job_result,
)
from app.services.app_setting_storage import (
    decode_app_setting_value,
    encode_app_setting_value,
)
from app.services.prediction_artifacts import (
    read_prediction_artifact_rows,
    read_prediction_explanation_artifact_rows,
)
from app.services.market_lake import (
    count_lake_symbols_for_trade_date,
    get_latest_lake_trade_date,
    list_lake_symbols_for_trade_date,
)
from app.services.tradability_filter import evaluate_candidate_tradability
from app.services.time_utils import app_now, app_now_iso


DECOMMISSIONED_CN_REVIEW_JOB_TYPE = "cn" + "_close" + "_review"
# Operational candidate views must not infer the production champion from the
# largest model-run id. Challenger and historical-research runs share the same
# audit tables but are never serving models until an explicit champion switch.
PRODUCTION_SIGNAL_MODEL_TYPES = ("lightgbm_multifactor",)
JOB_MESSAGE_MAX_CHARS = 16_000


def _bounded_job_message(message: str | None) -> str | None:
    if message is None:
        return None
    normalized = str(message)
    if len(normalized) <= JOB_MESSAGE_MAX_CHARS:
        return normalized
    tail_size = 2_000
    omitted = len(normalized) - JOB_MESSAGE_MAX_CHARS
    marker = (
        f"\n...[{omitted} characters omitted; full error is stored in the result artifact]...\n"
    )
    head_size = JOB_MESSAGE_MAX_CHARS - tail_size - len(marker)
    return (
        f"{normalized[:head_size]}"
        f"{marker}"
        f"{normalized[-tail_size:]}"
    )


def utc_now_iso() -> str:
    return app_now_iso()


def _physical_snapshot_tables_for_market(market: str | None):
    market_code = str(market or "").strip().upper()
    if market_code not in physical_fact_write_markets():
        return None
    return physical_snapshot_models(market_code)


def _legacy_snapshot_writes_enabled(
    db: Session,
    physical_tables,
    *,
    market: str | None = "CN",
) -> bool:
    if physical_tables is None:
        return True
    return legacy_mirror_write_enabled(
        db,
        market=market,
        configured=bool(
            getattr(
                get_settings(),
                "market_physical_snapshot_dual_write_legacy",
                True,
            )
        ),
    )


def _symbol_market(db: Session, symbol_id: int) -> str:
    market = str(
        db.scalar(select(Symbol.market).where(Symbol.id == int(symbol_id))) or ""
    ).strip().upper()
    if market not in {"CN", "US"}:
        return market
    return market


def _assert_legacy_prediction_write_allowed(
    db: Session,
    *,
    model_run_id: int,
) -> None:
    """Fail closed once a market has switched to physical-only publication.

    Legacy repositories remain available during the observed dual-write phase
    and for rollback.  After the database cutover marker is active, however,
    an old importer must not silently repopulate the shared fact tables.
    """

    market = str(
        db.scalar(
            select(ModelRun.market).where(ModelRun.id == int(model_run_id))
        )
        or ""
    ).strip().upper()
    if market in physical_fact_write_markets() and physical_only_cutover_active(
        db,
        market,
    ):
        raise RuntimeError(
            f"Legacy shared prediction writes are disabled for {market} model "
            f"run {int(model_run_id)} after physical-only cutover."
        )


def _physical_date(value: str | date | None, *, nullable: bool = False) -> date | None:
    if value is None or not str(value).strip():
        if nullable:
            return None
        raise ValueError("Physical market facts require a valid date.")
    try:
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"Invalid physical market-fact date: {value!r}") from exc


def _physical_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _safe_parse_iso(str(value))
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Invalid timezone-aware market-fact timestamp: {value!r}")
    return parsed


def _loads_json_object(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _loads_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


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


def _is_database_locked_error(exc: Exception) -> bool:
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
    def __init__(self, db: Session, *, cold_reads_enabled: bool | None = None) -> None:
        self.db = db
        self.cold_reads_enabled = (
            bool(get_settings().prediction_cold_reads_enabled)
            if cold_reads_enabled is None
            else bool(cold_reads_enabled)
        )
        self._market_context_cache: dict[str, dict] = {}

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
        latest_run_stmt = (
            select(ModelRun.id)
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
                select(Prediction.id)
                .where(Prediction.model_run_id == ModelRun.id)
                .limit(1)
                .exists(),
            )
            .order_by(ModelRun.id.desc())
            .limit(1)
        )
        if normalized_market and normalized_market != "ALL":
            latest_run_stmt = latest_run_stmt.where(ModelRun.market == normalized_market)
        latest_model_run_id = self.db.scalar(latest_run_stmt)
        if latest_model_run_id is None:
            return []

        latest_date_stmt = select(func.max(Prediction.trade_date)).where(
            Prediction.model_run_id == latest_model_run_id
        )
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
                "model_activation_status": activation_status,
            }
            for prediction, symbol, detail in rows
        ]

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
        latest_run_id = self.db.scalar(
            select(ModelRun.id)
            .join(table, table.model_run_id == ModelRun.id)
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
                table.market == market,
            )
            .order_by(ModelRun.id.desc())
            .limit(1)
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
        return [
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
            return physical_results
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
            return hot_results
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
        return cold_results[: int(limit)] if limit and limit > 0 else cold_results

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

    def get_latest_model_output_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(Prediction, Symbol, ModelRun, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
            )
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
        normalized = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()))
        if not normalized:
            return {}

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
            .where(
                ModelRun.status == "success",
                ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
            )
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

    def list_recent_prediction_snapshots(self, *, top_n: int = 10, limit_runs: int = 4) -> list[dict]:
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


class ModelChartSignalRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _replace_legacy(self, model_run_id: int, rows: list[dict]) -> int:
        self.db.execute(
            delete(ModelChartSignal).where(
                ModelChartSignal.model_run_id == int(model_run_id)
            )
        )
        now = utc_now_iso()
        for row in rows:
            self.db.add(
                ModelChartSignal(
                    model_run_id=int(model_run_id),
                    symbol_id=int(row["symbol_id"]),
                    trade_date=str(row["trade_date"])[:10],
                    score=row.get("score"),
                    rank_value=row.get("rank_value"),
                    signal_label=row.get("signal_label"),
                    signal_strength=row.get("signal_strength"),
                    note=row.get("note"),
                    created_at=now,
                )
            )
        self.db.flush()
        return len(rows)

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

        table = physical_model_chart_signal_model(market)
        symbol_ids = {int(row["symbol_id"]) for row in rows}
        matched_symbols = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.id.in_(symbol_ids),
                    Symbol.market == market,
                )
            )
            or 0
        )
        if matched_symbols != len(symbol_ids):
            raise RuntimeError(
                f"Refusing {market} chart-signal write with missing or cross-market symbols."
            )
        write_legacy = legacy_mirror_write_enabled(
            self.db,
            market=market,
            configured=bool(
                getattr(get_settings(), "market_physical_hot_dual_write_legacy", True)
            ),
        )
        try:
            self.db.execute(
                delete(table).where(table.model_run_id == int(model_run_id))
            )
            now = app_now()
            for row in rows:
                self.db.add(
                    table(
                        model_run_id=int(model_run_id),
                        symbol_id=int(row["symbol_id"]),
                        market=market,
                        trade_date=date.fromisoformat(str(row["trade_date"])[:10]),
                        score=row.get("score"),
                        rank_value=row.get("rank_value"),
                        signal_label=row.get("signal_label"),
                        signal_strength=row.get("signal_strength"),
                        note=row.get("note"),
                        created_at=now,
                    )
                )
            self.db.flush()
            if write_legacy:
                self._replace_legacy(model_run_id, rows)
            if commit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return len(rows)

    def get_latest_for_ticker(self, ticker: str, *, limit: int = 180) -> list[dict]:
        symbol = self.db.scalar(
            select(Symbol)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Symbol.ticker.asc())
            .limit(1)
        )
        market = str(symbol.market or "").strip().upper() if symbol is not None else ""
        if symbol is not None and market in physical_fact_write_markets():
            table = physical_model_chart_signal_model(market)
            latest_model_run_id = self.db.scalar(
                select(func.max(table.model_run_id)).where(
                    table.symbol_id == int(symbol.id)
                )
            )
            if latest_model_run_id is not None:
                rows = list(
                    self.db.scalars(
                        select(table)
                        .where(
                            table.model_run_id == int(latest_model_run_id),
                            table.symbol_id == int(symbol.id),
                        )
                        .order_by(table.trade_date.desc())
                        .limit(limit)
                    )
                )
                if rows:
                    return [
                        {
                            "trade_date": str(row.trade_date),
                            "score": row.score,
                            "rank_value": row.rank_value,
                            "signal_label": row.signal_label,
                            "signal_strength": row.signal_strength,
                            "note": row.note,
                            "ticker": symbol.ticker,
                        }
                        for row in rows
                    ]
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

    def get_backtest(self, strategy_run_id: int) -> dict | None:
        row = self.db.scalar(select(StrategyRun).where(StrategyRun.id == strategy_run_id))
        if row is None:
            return None
        payload = self._build_backtest_payload(row)
        payload["config"] = _loads_json_object(row.config_json)
        payload["audit_counts"] = {
            "orders": self.db.scalar(
                select(func.count()).select_from(StrategyOrder).where(
                    StrategyOrder.strategy_run_id == strategy_run_id
                )
            )
            or 0,
            "fills": self.db.scalar(
                select(func.count()).select_from(StrategyFill).where(
                    StrategyFill.strategy_run_id == strategy_run_id
                )
            )
            or 0,
            "rejects": self.db.scalar(
                select(func.count()).select_from(StrategyReject).where(
                    StrategyReject.strategy_run_id == strategy_run_id
                )
            )
            or 0,
            "portfolio_states": self.db.scalar(
                select(func.count()).select_from(StrategyPortfolioState).where(
                    StrategyPortfolioState.strategy_run_id == strategy_run_id
                )
            )
            or 0,
        }
        return payload

    def list_execution_audit(
        self,
        strategy_run_id: int,
        *,
        side: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        stmt = select(StrategyOrder).where(StrategyOrder.strategy_run_id == strategy_run_id)
        normalized_side = str(side or "").strip().lower()
        normalized_status = str(status or "").strip().lower()
        if normalized_side in {"buy", "sell"}:
            stmt = stmt.where(StrategyOrder.side == normalized_side)
        if normalized_status in {"filled", "rejected", "submitted"}:
            stmt = stmt.where(StrategyOrder.status == normalized_status)
        stmt = stmt.order_by(StrategyOrder.effective_date.asc(), StrategyOrder.id.asc()).offset(
            max(0, int(offset))
        ).limit(min(1000, max(1, int(limit))))
        orders = list(self.db.scalars(stmt).all())
        order_ids = [row.order_id for row in orders]
        if not order_ids:
            return []
        fills = {
            row.order_id: row
            for row in self.db.scalars(
                select(StrategyFill).where(
                    StrategyFill.strategy_run_id == strategy_run_id,
                    StrategyFill.order_id.in_(order_ids),
                )
            ).all()
        }
        rejects = {
            row.order_id: row
            for row in self.db.scalars(
                select(StrategyReject).where(
                    StrategyReject.strategy_run_id == strategy_run_id,
                    StrategyReject.order_id.in_(order_ids),
                )
            ).all()
        }
        payload: list[dict] = []
        for row in orders:
            fill = fills.get(row.order_id)
            reject = rejects.get(row.order_id)
            payload.append(
                {
                    "order_id": row.order_id,
                    "insight_id": row.insight_id,
                    "ticker": row.ticker,
                    "side": row.side,
                    "signal_date": row.signal_date,
                    "effective_date": row.effective_date,
                    "order_type": row.order_type,
                    "exit_reason": row.exit_reason,
                    "status": row.status,
                    "fill_date": fill.fill_date if fill else None,
                    "quantity": fill.quantity if fill else None,
                    "reference_price": fill.reference_price if fill else None,
                    "fill_price": fill.fill_price if fill else None,
                    "fee": fill.fee if fill else None,
                    "slippage": fill.slippage if fill else None,
                    "notional": fill.notional if fill else None,
                    "lot_id": fill.lot_id if fill else None,
                    "entry_date": fill.entry_date if fill else None,
                    "reject_reason": reject.reject_reason if reject else None,
                }
            )
        return payload

    def get_portfolio_states(self, strategy_run_id: int) -> list[dict]:
        rows = self.db.scalars(
            select(StrategyPortfolioState)
            .where(StrategyPortfolioState.strategy_run_id == strategy_run_id)
            .order_by(StrategyPortfolioState.trade_date.asc())
        ).all()
        return [
            {
                "trade_date": row.trade_date,
                "cash": row.cash,
                "position_market_value": row.position_market_value,
                "nav": row.nav,
                "gross_exposure": row.gross_exposure,
                "net_exposure": row.net_exposure,
                "cumulative_fees": row.cumulative_fees,
                "cumulative_slippage": row.cumulative_slippage,
                "open_lots": row.open_lots,
            }
            for row in rows
        ]

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


class MarketRefreshBatchRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_batch(
        self,
        *,
        source_job_id: int | None,
        market: str,
        provider: str,
        requested_as_of_date: str,
        universe_count: int,
        started_at: str | None = None,
    ) -> MarketRefreshBatch:
        row = MarketRefreshBatch(
            source_job_id=source_job_id,
            market=str(market or "").strip().upper(),
            provider=str(provider or "").strip() or "unknown",
            requested_as_of_date=str(requested_as_of_date or "")[:10],
            universe_count=max(0, int(universe_count)),
            status="running",
            started_at=started_at or app_now_iso(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def complete_batch(self, batch_id: int, *, result: dict, finished_at: str | None = None) -> MarketRefreshBatch | None:
        row = self.db.get(MarketRefreshBatch, int(batch_id))
        if row is None:
            return None
        row.actual_as_of_date = str(result.get("actual_as_of_date") or result.get("trade_date") or "")[:10] or None
        row.universe_count = int(
            result.get("total_symbols")
            or result.get("universe_count")
            or result.get("rows_returned")
            or result.get("rows_written")
            or row.universe_count
            or 0
        )
        row.success_count = int(result.get("success_count") or 0)
        row.no_trade_count = int(result.get("no_trade_count") or 0)
        row.inactive_count = int(result.get("inactive_count") or 0)
        row.partial_count = int(result.get("stale_count") or result.get("partial_count") or 0)
        row.missing_count = int(result.get("missing_count") or 0)
        row.failed_count = int(result.get("failure_count") or result.get("failed_count") or 0)
        row.status = str(result.get("status") or "partial")
        row.summary_json = json.dumps(result, ensure_ascii=False)
        row.finished_at = finished_at or app_now_iso()
        self.db.commit()
        self.db.refresh(row)
        return row

    @staticmethod
    def _serialize(row: MarketRefreshBatch) -> dict:
        return {
            "id": row.id,
            "source_job_id": row.source_job_id,
            "market": row.market,
            "provider": row.provider,
            "requested_as_of_date": row.requested_as_of_date,
            "actual_as_of_date": row.actual_as_of_date,
            "universe_count": row.universe_count,
            "success_count": row.success_count,
            "no_trade_count": row.no_trade_count,
            "inactive_count": row.inactive_count,
            "partial_count": row.partial_count,
            "missing_count": row.missing_count,
            "failed_count": row.failed_count,
            "status": row.status,
            "summary": _loads_json_object(row.summary_json),
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }

    def record_result(
        self,
        *,
        source_job_id: int,
        market: str,
        provider: str,
        requested_as_of_date: str,
        result: dict,
    ) -> dict:
        """Create or update the one audit batch owned by a Job and market."""

        market_code = str(market or "").strip().upper()
        row = self.db.scalar(
            select(MarketRefreshBatch)
            .where(MarketRefreshBatch.source_job_id == int(source_job_id))
            .where(MarketRefreshBatch.market == market_code)
            .order_by(MarketRefreshBatch.id.desc())
            .limit(1)
        )
        if row is None:
            row = self.create_batch(
                source_job_id=source_job_id,
                market=market_code,
                provider=provider,
                requested_as_of_date=requested_as_of_date,
                universe_count=int(
                    result.get("total_symbols")
                    or result.get("universe_count")
                    or result.get("rows_returned")
                    or result.get("rows_written")
                    or 0
                ),
            )
        else:
            row.provider = str(provider or "").strip() or row.provider
            row.requested_as_of_date = str(requested_as_of_date or row.requested_as_of_date)[:10]
            self.db.commit()
        completed = self.complete_batch(row.id, result=result)
        return self._serialize(completed or row)

class PriceSyncStateRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

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
                "market": symbol.market,
                "provider": state.provider,
                "last_synced_date": state.last_synced_date,
                "status": state.status,
                "message": state.message,
                "updated_at": state.updated_at,
            }
            for state, symbol in rows
        ]

    def get_market_freshness_overview(
        self,
        markets: tuple[str, ...] = ("CN", "US"),
        *,
        tickers_by_market: dict[str, set[str]] | None = None,
    ) -> dict[str, dict]:
        normalized_markets = tuple(str(market or "").strip().upper() for market in markets)
        rows = self.db.execute(
            select(Symbol.ticker, Symbol.market, Symbol.is_active, PriceSyncState.last_synced_date, PriceSyncState.status)
            .outerjoin(PriceSyncState, PriceSyncState.symbol_id == Symbol.id)
            .where(Symbol.market.in_(normalized_markets))
        ).all()
        states = [
            {
                "ticker": ticker,
                "market": market,
                "last_synced_date": last_synced_date,
                "status": status or ("inactive" if not is_active else None),
                "is_active": bool(is_active),
            }
            for ticker, market, is_active, last_synced_date, status in rows
            if not tickers_by_market
            or str(market or "").strip().upper() not in tickers_by_market
            or str(ticker or "").strip().upper() in tickers_by_market.get(str(market or "").strip().upper(), set())
        ]
        overview: dict[str, dict] = {}
        for market in normalized_markets:
            summary = summarize_market_freshness(states, market=market)
            try:
                lake_latest = get_latest_lake_trade_date(market=market)
            except Exception:
                lake_latest = None
            lake_expected = summary.get("expected_as_of_date")
            lake_status = (
                "fresh" if lake_latest and lake_expected and lake_latest >= lake_expected
                else "stale" if lake_latest
                else "missing"
            )
            lake_symbol_count = 0
            lake_symbols: set[str] = set()
            if lake_latest:
                try:
                    lake_symbols = list_lake_symbols_for_trade_date(
                        market=market,
                        trade_date=lake_latest,
                    )
                    lake_symbol_count = len(lake_symbols)
                except Exception:
                    try:
                        lake_symbol_count = count_lake_symbols_for_trade_date(
                            market=market,
                            trade_date=lake_latest,
                        )
                    except Exception:
                        lake_symbol_count = 0
            classification = classify_market_symbol_anomalies(
                states,
                market=market,
                expected_as_of_date=str(summary.get("expected_as_of_date") or ""),
                lake_symbols=lake_symbols,
            )
            # Keep per-symbol state diagnostics intact, but expose the lake's
            # authoritative as-of date separately. A bulk lake refresh may be
            # current even when an old per-symbol sync row has not been touched.
            overview[market] = {
                **summary,
                "symbol_state_status": summary.get("status"),
                "lake_status": lake_status,
                "lake_latest_as_of_date": lake_latest,
                "lake_symbol_count": lake_symbol_count,
                "authoritative_as_of_date": lake_latest or summary.get("latest_as_of_date"),
                "anomaly_classification": classification,
                "blocking_anomaly_count": classification["blocking_anomaly_count"],
                "accounted_symbol_count": classification["accounted_count"],
            }
        return overview

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
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Price sync state upsert exhausted retries.")


def _job_markets_from_params(job_type: str, params: dict | None) -> list[str]:
    payload = params or {}
    markets: list[str] = []
    direct = payload.get("market")
    if direct:
        markets.append(str(direct).strip().upper())
    configured = payload.get("markets")
    if isinstance(configured, str):
        markets.extend(item.strip().upper() for item in configured.split(","))
    elif isinstance(configured, (list, tuple, set)):
        markets.extend(str(item).strip().upper() for item in configured)
    normalized_type = str(job_type or "").lower()
    if not markets:
        if "cn" in normalized_type or "a_share" in normalized_type:
            markets.append("CN")
        elif "us" in normalized_type or "polygon" in normalized_type:
            markets.append("US")
    return sorted({market for market in markets if market in {"CN", "US", "HK"}})


def _job_category(job_type: str) -> str:
    normalized = str(job_type or "").lower()
    if any(token in normalized for token in ("refresh", "train", "screener", "report", "risk", "backtest")):
        return "daily_pipeline"
    if any(token in normalized for token in ("cleanup", "sync", "retention", "metadata", "universe")):
        return "maintenance"
    return "ad_hoc"


def _job_provider(params: dict | None) -> str | None:
    payload = params or {}
    for key in ("provider", "provider_used", "source"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def _declared_upstream_job_ids(params: dict | None) -> list[int]:
    payload = params or {}
    raw: list[object] = [payload.get("source_job_id")]
    raw.extend(payload.get("source_job_ids") or [])
    raw.extend(payload.get("depends_on") or [])
    values: list[int] = []
    for item in raw:
        value = item.get("job_id", item.get("id")) if isinstance(item, dict) else item
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in values:
            values.append(parsed)
    return values


def _dependency_status(upstream: DataJob | None) -> tuple[str, str | None]:
    if upstream is None:
        return "unknown", "Referenced upstream Job was not found."
    normalized = str(upstream.status or "").lower()
    if normalized == "success":
        return "satisfied", None
    if normalized in {"partial", "not_configured", "empty"}:
        return "degraded", upstream.message or "Upstream Job completed with degraded output."
    if normalized == "running":
        return "waiting", upstream.message or "Upstream Job is still running."
    return "blocked", upstream.message or f"Upstream Job status is {normalized or 'unknown'}."


class DataJobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _ensure_run_metadata(self, job: DataJob, params: dict | None) -> None:
        """Create the definition, initial attempt, and declared lineage once.

        This intentionally treats the existing ``DataJob`` table as the
        job-run table, so rollout does not invalidate historical jobs.
        """

        now = utc_now_iso()
        definition = self.db.scalar(select(JobDefinition).where(JobDefinition.job_type == job.job_type))
        if definition is None:
            self.db.add(
                JobDefinition(
                    job_type=job.job_type,
                    display_name=job.job_type.replace("_", " "),
                    category=_job_category(job.job_type),
                    markets_json=json.dumps(_job_markets_from_params(job.job_type, params), ensure_ascii=False),
                    max_retries=0,
                    is_enabled=1,
                    config_json=json.dumps({"observed_from_run": True}, ensure_ascii=False),
                    created_at=now,
                    updated_at=now,
                )
            )
        attempt = self.db.scalar(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == job.id)
            .order_by(JobRunAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is None:
            self.db.add(
                JobRunAttempt(
                    job_id=job.id,
                    attempt_no=1,
                    status=job.status,
                    provider=_job_provider(params),
                    started_at=job.started_at,
                )
            )
        for upstream_id in _declared_upstream_job_ids(params):
            if upstream_id == job.id:
                continue
            exists = self.db.scalar(
                select(JobRunDependency.id)
                .where(JobRunDependency.job_id == job.id)
                .where(JobRunDependency.upstream_job_id == upstream_id)
                .where(JobRunDependency.dependency_type == "source_job")
                .limit(1)
            )
            if exists is not None:
                continue
            status, reason = _dependency_status(self.db.get(DataJob, upstream_id))
            self.db.add(
                JobRunDependency(
                    job_id=job.id,
                    upstream_job_id=upstream_id,
                    dependency_type="source_job",
                    status=status,
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
        self.db.commit()

    def _complete_latest_attempt(self, job: DataJob, *, status: str, result: dict | None) -> None:
        attempt = self.db.scalar(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == job.id)
            .order_by(JobRunAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is None:
            attempt = JobRunAttempt(
                job_id=job.id,
                attempt_no=1,
                status=status,
                provider=_job_provider(_loads_json_object(job.params_json)),
                started_at=job.started_at,
            )
            self.db.add(attempt)
        attempt.status = status
        attempt.finished_at = job.finished_at
        attempt.duration_seconds = _job_duration_seconds(job.started_at, job.finished_at)
        if status in {"failed", "failed_timeout"}:
            attempt.error_message = job.message
        if result is not None:
            attempt.summary_json = json.dumps(
                summarize_job_result(result),
                ensure_ascii=False,
            )
        self.db.commit()

    def _serialize_job(self, row: DataJob, *, hydrate_result_artifact: bool = False) -> dict:
        params = _loads_json_object(row.params_json)
        result = (params or {}).get("result") if isinstance(params, dict) else None
        result_source = "postgresql_inline" if isinstance(result, dict) else "none"
        result_artifact = (params or {}).get("result_artifact") if isinstance(params, dict) else None
        if not isinstance(result, dict) and isinstance(result_artifact, dict):
            if hydrate_result_artifact:
                result = JsonPayloadArtifactStore().read(result_artifact)
                result_source = "compressed_artifact"
            else:
                result = (params or {}).get("result_summary") or {}
                result_source = "postgresql_summary"
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
            "result_source": result_source,
            "result_artifact": result_artifact,
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
                message=_bounded_job_message(message),
                params_json=json.dumps(params, ensure_ascii=False) if params is not None else None,
            )
            self.db.add(job)
            try:
                self.db.commit()
                self.db.refresh(job)
                for metadata_attempt in range(1, attempts + 1):
                    try:
                        self._ensure_run_metadata(job, params)
                        break
                    except OperationalError as exc:
                        self.db.rollback()
                        if metadata_attempt >= attempts or not _is_database_locked_error(exc):
                            break
                        _sleep_for_lock_retry(metadata_attempt)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
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
            job.message = _bounded_job_message(message)
            if result is not None:
                params = _loads_json_object(job.params_json) or {}
                result_bytes = canonical_json_bytes(result)
                threshold = max(1024, int(get_settings().job_inline_result_max_bytes))
                if len(result_bytes) > threshold:
                    params.pop("result", None)
                    params["result_artifact"] = JsonPayloadArtifactStore().write(
                        result,
                        namespace="job_results",
                    )
                    params["result_summary"] = summarize_job_result(result)
                else:
                    params["result"] = result
                    params.pop("result_artifact", None)
                    params.pop("result_summary", None)
                params["job_runtime"] = {
                    "duration_seconds": _job_duration_seconds(job.started_at, job.finished_at),
                    "completed_at": job.finished_at,
                }
                job.params_json = json.dumps(params, ensure_ascii=False)
            try:
                self.db.commit()
                self.db.refresh(job)
                self._complete_latest_attempt(job, status=status, result=result)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job completion exhausted retries.")

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        message: str | None = None,
        progress: dict | None = None,
    ) -> DataJob | None:
        attempts = 4
        for attempt in range(1, attempts + 1):
            stmt = select(DataJob).where(DataJob.id == job_id)
            job = self.db.scalar(stmt)
            if job is None:
                return None
            if status is not None:
                job.status = status
            if message is not None:
                job.message = _bounded_job_message(message)
            if progress is not None:
                params = _loads_json_object(job.params_json) or {}
                params["progress"] = {
                    **(params.get("progress") or {}),
                    **progress,
                    "updated_at": utc_now_iso(),
                }
                job.params_json = json.dumps(params, ensure_ascii=False)
            try:
                self.db.commit()
                self.db.refresh(job)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job progress update exhausted retries.")

    def list_recent_jobs(self, limit: int = 20) -> list[dict]:
        stmt = (
            select(DataJob)
            .where(DataJob.job_type != DECOMMISSIONED_CN_REVIEW_JOB_TYPE)
            .order_by(DataJob.id.desc())
            .limit(max(limit * 2, limit))
        )
        rows = self.db.scalars(stmt).all()
        return [self._serialize_job(row) for row in rows][:limit]

    def get_job_detail(self, job_id: int) -> dict | None:
        row = self.db.get(DataJob, int(job_id))
        if row is None:
            return None
        payload = self._serialize_job(row, hydrate_result_artifact=True)
        definition = self.db.scalar(select(JobDefinition).where(JobDefinition.job_type == row.job_type))
        dependencies = self.db.scalars(
            select(JobRunDependency)
            .where(JobRunDependency.job_id == row.id)
            .order_by(JobRunDependency.id.asc())
        ).all()
        attempts = self.db.scalars(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == row.id)
            .order_by(JobRunAttempt.attempt_no.asc())
        ).all()
        batches = self.db.scalars(
            select(MarketRefreshBatch)
            .where(MarketRefreshBatch.source_job_id == row.id)
            .order_by(MarketRefreshBatch.id.asc())
        ).all()
        payload["definition"] = (
            {
                "id": definition.id,
                "job_type": definition.job_type,
                "display_name": definition.display_name,
                "category": definition.category,
                "markets": _loads_json_list(definition.markets_json),
                "schedule_rule": definition.schedule_rule,
                "timeout_minutes": definition.timeout_minutes,
                "max_retries": definition.max_retries,
                "is_enabled": bool(definition.is_enabled),
            }
            if definition is not None
            else None
        )
        upstream_by_id = {
            dependency.upstream_job_id: self.db.get(DataJob, dependency.upstream_job_id)
            for dependency in dependencies
        }
        payload["dependencies"] = []
        for dependency in dependencies:
            upstream = upstream_by_id.get(dependency.upstream_job_id)
            payload["dependencies"].append(
                {
                    "id": dependency.id,
                    "upstream_job_id": dependency.upstream_job_id,
                    "upstream_job_type": upstream.job_type if upstream is not None else None,
                    "upstream_status": upstream.status if upstream is not None else "unknown",
                    "dependency_type": dependency.dependency_type,
                    "status": dependency.status,
                    "reason": dependency.reason,
                    "required_as_of_date": dependency.required_as_of_date,
                    "actual_as_of_date": dependency.actual_as_of_date,
                }
            )
        payload["attempts"] = [
            {
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "provider": attempt.provider,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "duration_seconds": attempt.duration_seconds,
                "error_message": attempt.error_message,
                "summary": _loads_json_object(attempt.summary_json),
            }
            for attempt in attempts
        ]
        payload["market_refresh_batches"] = [MarketRefreshBatchRepository._serialize(batch) for batch in batches]
        return payload

    def get_latest_job(self, job_type: str | list[str] | tuple[str, ...] | set[str]) -> dict | None:
        if isinstance(job_type, (list, tuple, set)):
            job_types = [str(item or "").strip() for item in job_type if str(item or "").strip()]
        else:
            job_types = [str(job_type or "").strip()] if str(job_type or "").strip() else []
        job_types = [job_type for job_type in job_types if job_type != DECOMMISSIONED_CN_REVIEW_JOB_TYPE]
        if not job_types:
            return None
        stmt = (
            select(DataJob)
            .where(DataJob.job_type.in_(job_types))
            .order_by(DataJob.id.desc())
            .limit(1)
        )
        row = self.db.scalar(stmt)
        return (
            self._serialize_job(row, hydrate_result_artifact=True)
            if row is not None
            else None
        )

    def has_running_job(self, job_type: str) -> bool:
        stmt = (
            select(DataJob.id)
            .where(DataJob.job_type == job_type)
            .where(DataJob.status == "running")
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def get_running_job(self, job_types: str | list[str] | tuple[str, ...] | set[str]) -> dict | None:
        """Return the oldest active job for a type set so UI retries are idempotent."""
        if isinstance(job_types, str):
            normalized = [job_types.strip()]
        else:
            normalized = [str(item or "").strip() for item in job_types]
        normalized = [item for item in normalized if item]
        if not normalized:
            return None
        row = self.db.scalar(
            select(DataJob)
            .where(DataJob.job_type.in_(normalized))
            .where(DataJob.status == "running")
            .order_by(DataJob.id.asc())
            .limit(1)
        )
        return self._serialize_job(row) if row is not None else None

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
        completed_rows: list[DataJob] = []
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
            completed_rows.append(row)
        if updated:
            self.db.commit()
            for row in completed_rows:
                self._complete_latest_attempt(row, status="failed_timeout", result={"timeout": True})
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
        commit: bool = True,
    ) -> WorkspaceSnapshot:
        stored_payload = payload
        threshold = max(
            1024,
            int(get_settings().workspace_snapshot_inline_payload_max_bytes),
        )
        if len(canonical_json_bytes(payload)) > threshold:
            reference = JsonPayloadArtifactStore().write(
                payload,
                namespace="workspace_snapshots",
            )
            stored_payload = build_payload_envelope(payload, reference)
        attempts = 4
        for attempt in range(1, attempts + 1):
            snapshot = WorkspaceSnapshot(
                snapshot_type=snapshot_type,
                snapshot_date=snapshot_date,
                payload_json=json.dumps(stored_payload, ensure_ascii=False),
                source_job_id=source_job_id,
                created_at=utc_now_iso(),
            )
            self.db.add(snapshot)
            if not commit:
                self.db.flush()
                return snapshot
            try:
                self.db.commit()
                self.db.refresh(snapshot)
                return snapshot
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
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
            stored_payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            stored_payload = None
        payload, payload_source = resolve_payload_envelope(stored_payload)
        return {
            "id": row.id,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "payload": payload,
            "payload_source": payload_source,
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
                stored_payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                stored_payload = None
            payload, payload_source = resolve_payload_envelope(stored_payload)
            results.append(
                {
                    "id": row.id,
                    "snapshot_type": row.snapshot_type,
                    "snapshot_date": row.snapshot_date,
                    "payload": payload,
                    "payload_source": payload_source,
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
            stored_payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            stored_payload = None
        payload, payload_source = resolve_payload_envelope(stored_payload)
        return {
            "id": row.id,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "payload": payload,
            "payload_source": payload_source,
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
        if setting is None:
            return None
        resolved, _source = decode_app_setting_value(setting.value)
        return resolved

    def set(self, key: str, value: str, *, commit: bool = True) -> AppSetting:
        stored_value, _source = encode_app_setting_value(
            value,
            max_inline_bytes=get_settings().app_setting_inline_value_max_bytes,
        )
        attempts = 4
        for attempt in range(1, attempts + 1):
            setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
            now = utc_now_iso()
            if setting is None:
                setting = AppSetting(key=key, value=stored_value, updated_at=now)
                self.db.add(setting)
            else:
                setting.value = stored_value
                setting.updated_at = now
            if not commit:
                self.db.flush()
                return setting
            try:
                self.db.commit()
                self.db.refresh(setting)
                return setting
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
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
        symbol_market = _symbol_market(self.db, symbol_id)
        physical_tables = _physical_snapshot_tables_for_market(symbol_market)
        physical_table = physical_tables[0] if physical_tables is not None else None
        write_legacy = _legacy_snapshot_writes_enabled(
            self.db,
            physical_tables,
            market=symbol_market,
        )
        stmt = select(FundamentalSnapshot).where(
            FundamentalSnapshot.symbol_id == symbol_id,
            FundamentalSnapshot.report_date == report_date,
            FundamentalSnapshot.source == source,
        )
        existing = self.db.scalar(stmt) if write_legacy else None
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
        if write_legacy and existing is None:
            existing = FundamentalSnapshot(
                symbol_id=symbol_id,
                report_date=report_date,
                source=source,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        elif write_legacy:
            for key, value in payload.items():
                setattr(existing, key, value)
        physical_existing = None
        if physical_table is not None:
            physical_existing = self.db.scalar(
                select(physical_table).where(
                    physical_table.symbol_id == symbol_id,
                    physical_table.report_date == _physical_date(report_date),
                    physical_table.source == source,
                )
            )
            if physical_existing is None:
                physical_payload = {
                    **payload,
                    "listing_date": _physical_date(listing_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                physical_existing = physical_table(
                    symbol_id=symbol_id,
                    market=symbol_market,
                    report_date=_physical_date(report_date),
                    source=source,
                    created_at=_physical_datetime(now),
                    **physical_payload,
                )
                self.db.add(physical_existing)
            else:
                physical_payload = {
                    **payload,
                    "listing_date": _physical_date(listing_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                for key, value in physical_payload.items():
                    setattr(physical_existing, key, value)
        self.db.commit()
        result = existing if write_legacy else physical_existing
        if result is None:
            raise RuntimeError("Snapshot write did not produce a legacy or physical row.")
        self.db.refresh(result)
        return result

    def get_latest_for_ticker(self, ticker: str) -> dict | None:
        symbol = self.db.scalar(
            select(Symbol).where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
        )
        if symbol is None:
            return None
        physical_tables = _physical_snapshot_tables_for_market(symbol.market)
        snapshot_table = (
            physical_tables[0] if physical_tables is not None else FundamentalSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .where(Symbol.id == symbol.id)
            .order_by(snapshot_table.report_date.desc(), snapshot_table.id.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        snapshot, symbol = row
        return self._to_dict(snapshot, symbol)

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[0] if physical_tables is not None else FundamentalSnapshot
        )
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
                snapshot_table.symbol_id,
                func.max(snapshot_table.report_date).label("max_report_date"),
            )
            .where(snapshot_table.symbol_id.in_(list(symbol_map)))
            .group_by(snapshot_table.symbol_id)
            .subquery()
        )
        stmt = (
            select(snapshot_table)
            .join(
                subquery,
                (snapshot_table.symbol_id == subquery.c.symbol_id)
                & (snapshot_table.report_date == subquery.c.max_report_date),
            )
            .order_by(snapshot_table.symbol_id.asc(), snapshot_table.id.desc())
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

    def list_history_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[0] if physical_tables is not None else FundamentalSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc(), snapshot_table.report_date.asc(), snapshot_table.id.asc())
        )
        if market and market != "ALL":
            stmt = stmt.where(Symbol.market == market)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        rows = self.db.execute(stmt).all()
        return [self._to_dict(snapshot, symbol) for snapshot, symbol in rows]

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
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "source_layer": snapshot.__class__.__tablename__,
        }


class PointInTimeFeatureSnapshotRepository:
    """Append-only repository; an existing revision is never updated in place."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def append_snapshot(
        self,
        *,
        symbol_id: int,
        feature_name: str,
        feature_value: float,
        event_time: str,
        available_time: str,
        ingested_time: str,
        source: str,
        source_record_id: str,
        revision_id: str,
        payload: dict | None = None,
        commit: bool = True,
    ) -> tuple[PointInTimeFeatureSnapshot, bool]:
        symbol_market = _symbol_market(self.db, symbol_id)
        physical_tables = _physical_snapshot_tables_for_market(symbol_market)
        physical_table = physical_tables[1] if physical_tables is not None else None
        write_legacy = _legacy_snapshot_writes_enabled(
            self.db,
            physical_tables,
            market=symbol_market,
        )
        for name, value in (
            ("feature_name", feature_name),
            ("source", source),
            ("source_record_id", source_record_id),
            ("revision_id", revision_id),
        ):
            if not str(value or "").strip():
                raise ValueError(f"{name} must not be empty")
        numeric_value = float(feature_value)
        if not math.isfinite(numeric_value):
            raise ValueError("feature_value must be finite")
        parsed_event = _safe_parse_iso(event_time)
        parsed_available = _safe_parse_iso(available_time)
        parsed_ingested = _safe_parse_iso(ingested_time)
        if any(item is None or item.tzinfo is None for item in (parsed_event, parsed_available, parsed_ingested)):
            raise ValueError("feature timestamps must be valid and timezone-aware")
        if parsed_available < parsed_event:
            raise ValueError("available_time must not precede event_time")
        normalized_event_time = parsed_event.astimezone(UTC).isoformat()
        normalized_available_time = parsed_available.astimezone(UTC).isoformat()
        normalized_ingested_time = parsed_ingested.astimezone(UTC).isoformat()
        stmt = select(PointInTimeFeatureSnapshot).where(
            PointInTimeFeatureSnapshot.symbol_id == symbol_id,
            PointInTimeFeatureSnapshot.feature_name == feature_name,
            PointInTimeFeatureSnapshot.source == source,
            PointInTimeFeatureSnapshot.source_record_id == source_record_id,
            PointInTimeFeatureSnapshot.revision_id == revision_id,
        )
        existing = self.db.scalar(stmt) if write_legacy else None
        if existing is not None:
            if (
                float(existing.feature_value) != numeric_value
                or existing.event_time != normalized_event_time
            ):
                raise RuntimeError("revision identity collision in append-only feature store")
            if physical_table is None:
                return existing, False
        created_at = utc_now_iso()
        snapshot = existing
        inserted = False
        if write_legacy and existing is None:
            snapshot = PointInTimeFeatureSnapshot(
                symbol_id=symbol_id,
                feature_name=feature_name,
                feature_value=numeric_value,
                event_time=normalized_event_time,
                available_time=normalized_available_time,
                ingested_time=normalized_ingested_time,
                source=source,
                source_record_id=source_record_id,
                revision_id=revision_id,
                payload_json=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if payload is not None
                else None,
                created_at=created_at,
            )
            inserted = True
            self.db.add(snapshot)
        physical_existing = None
        physical_inserted = False
        if physical_table is not None:
            physical_existing = self.db.scalar(
                select(physical_table).where(
                    physical_table.symbol_id == symbol_id,
                    physical_table.feature_name == feature_name,
                    physical_table.source == source,
                    physical_table.source_record_id == source_record_id,
                    physical_table.revision_id == revision_id,
                )
            )
            if physical_existing is not None:
                if (
                    float(physical_existing.feature_value) != numeric_value
                    or _physical_datetime(physical_existing.event_time).astimezone(UTC)
                    != _physical_datetime(normalized_event_time).astimezone(UTC)
                ):
                    raise RuntimeError(
                        "revision identity collision in physical append-only feature store"
                    )
            else:
                physical_existing = physical_table(
                    symbol_id=symbol_id,
                    market=symbol_market,
                    feature_name=feature_name,
                    feature_value=numeric_value,
                    event_time=_physical_datetime(normalized_event_time),
                    available_time=_physical_datetime(normalized_available_time),
                    ingested_time=_physical_datetime(normalized_ingested_time),
                    source=source,
                    source_record_id=source_record_id,
                    revision_id=revision_id,
                    payload_json=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if payload is not None
                    else None,
                    created_at=_physical_datetime(created_at),
                )
                physical_inserted = True
                self.db.add(physical_existing)
        result = snapshot if write_legacy else physical_existing
        if result is None:
            raise RuntimeError("Feature write did not produce a legacy or physical row.")
        result_inserted = inserted if write_legacy else physical_inserted
        if commit:
            self.db.commit()
            self.db.refresh(result)
        else:
            self.db.flush()
        return result, result_inserted

    def list_history_for_market(
        self,
        market: str,
        *,
        tickers: list[str] | None = None,
        feature_names: list[str] | None = None,
    ) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[1]
            if physical_tables is not None
            else PointInTimeFeatureSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .where(Symbol.market == market)
            .order_by(
                Symbol.ticker.asc(),
                snapshot_table.feature_name.asc(),
                snapshot_table.available_time.asc(),
                snapshot_table.id.asc(),
            )
        )
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([item.upper() for item in tickers]))
        if feature_names:
            stmt = stmt.where(snapshot_table.feature_name.in_(feature_names))
        return [self._to_dict(snapshot, symbol) for snapshot, symbol in self.db.execute(stmt)]

    def summarize_market_coverage(
        self,
        market: str,
        *,
        required_features: tuple[str, ...] | list[str],
        minimum_cross_section_coverage: float = 0.60,
    ) -> dict:
        """Return a cheap operational snapshot; formal date coverage stays in the audit job."""

        market_code = str(market or "").strip().upper()
        physical_tables = _physical_snapshot_tables_for_market(market_code)
        snapshot_table = (
            physical_tables[1]
            if physical_tables is not None
            else PointInTimeFeatureSnapshot
        )
        feature_names = tuple(dict.fromkeys(str(item).strip() for item in required_features if str(item).strip()))
        total_symbols = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                )
            )
            or 0
        )
        rows = self.db.execute(
            select(
                snapshot_table.feature_name,
                func.count(func.distinct(snapshot_table.symbol_id)),
                func.count(snapshot_table.id),
                func.max(snapshot_table.available_time),
            )
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .where(
                Symbol.market == market_code,
                Symbol.is_active == 1,
                snapshot_table.feature_name.in_(feature_names),
            )
            .group_by(snapshot_table.feature_name)
        ).all()
        by_name = {
            str(feature_name): {
                "symbol_count": int(symbol_count or 0),
                "record_count": int(record_count or 0),
                "latest_available_time": latest_available_time,
            }
            for feature_name, symbol_count, record_count, latest_available_time in rows
        }
        feature_coverage = []
        for feature_name in feature_names:
            values = by_name.get(feature_name, {})
            symbol_count = int(values.get("symbol_count") or 0)
            coverage = (symbol_count / total_symbols) if total_symbols else 0.0
            feature_coverage.append(
                {
                    "feature_name": feature_name,
                    "symbol_count": symbol_count,
                    "record_count": int(values.get("record_count") or 0),
                    "coverage": coverage,
                    "coverage_pct": round(coverage * 100.0, 2),
                    "latest_available_time": values.get("latest_available_time"),
                    "cross_section_gate": "PASS"
                    if coverage >= minimum_cross_section_coverage
                    else "COLLECTING",
                }
            )

        ready_symbol_count = 0
        if feature_names:
            ready_subquery = (
                select(snapshot_table.symbol_id)
                .join(Symbol, Symbol.id == snapshot_table.symbol_id)
                .where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                    snapshot_table.feature_name.in_(feature_names),
                )
                .group_by(snapshot_table.symbol_id)
                .having(
                    func.count(func.distinct(snapshot_table.feature_name))
                    == len(feature_names)
                )
                .subquery()
            )
            ready_symbol_count = int(
                self.db.scalar(select(func.count()).select_from(ready_subquery)) or 0
            )
        minimum_coverage = min(
            (float(item["coverage"]) for item in feature_coverage),
            default=0.0,
        )
        return {
            "market": market_code,
            "required_features": list(feature_names),
            "total_symbols": total_symbols,
            "ready_symbol_count": ready_symbol_count,
            "ready_symbol_pct": round(
                (ready_symbol_count / total_symbols) * 100.0 if total_symbols else 0.0,
                2,
            ),
            "minimum_feature_coverage": minimum_coverage,
            "minimum_feature_coverage_pct": round(minimum_coverage * 100.0, 2),
            "minimum_cross_section_coverage": float(minimum_cross_section_coverage),
            "cross_section_gate": "PASS"
            if feature_coverage and minimum_coverage >= minimum_cross_section_coverage
            else "COLLECTING",
            "formal_date_gate": "PENDING_AUDIT",
            "feature_coverage": feature_coverage,
        }

    def summarize_market_coverage_as_of(
        self,
        market: str,
        *,
        cutoff: datetime,
        required_features: tuple[str, ...] | list[str],
        max_age_days: dict[str, int],
        minimum_cross_section_coverage: float = 0.60,
    ) -> dict:
        """Return freshness-aware coverage at one historical post-close cutoff."""

        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        if not 0.0 < minimum_cross_section_coverage <= 1.0:
            raise ValueError("minimum_cross_section_coverage must be in (0, 1]")
        market_code = str(market or "").strip().upper()
        physical_tables = _physical_snapshot_tables_for_market(market_code)
        snapshot_table = (
            physical_tables[1]
            if physical_tables is not None
            else PointInTimeFeatureSnapshot
        )
        feature_names = tuple(
            dict.fromkeys(str(item).strip() for item in required_features if str(item).strip())
        )
        missing_ages = set(feature_names) - set(max_age_days)
        if missing_ages:
            raise ValueError("max_age_days is missing features: " + ", ".join(sorted(missing_ages)))
        if any(int(max_age_days[name]) <= 0 for name in feature_names):
            raise ValueError("max_age_days values must be positive")

        cutoff_utc = cutoff.astimezone(UTC)
        cutoff_text = cutoff_utc.isoformat()
        cutoff_value = cutoff_utc if physical_tables is not None else cutoff_text
        total_symbols = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                )
            )
            or 0
        )
        feature_coverage: list[dict] = []
        freshness_filters = []
        for feature_name in feature_names:
            minimum_datetime = cutoff_utc - timedelta(
                days=int(max_age_days[feature_name])
            )
            minimum_time = (
                minimum_datetime
                if physical_tables is not None
                else minimum_datetime.isoformat()
            )
            freshness = or_(
                snapshot_table.available_time >= minimum_time,
                snapshot_table.ingested_time >= minimum_time,
            )
            feature_filter = (
                (snapshot_table.feature_name == feature_name)
                & freshness
            )
            freshness_filters.append(feature_filter)
            symbol_count = int(
                self.db.scalar(
                    select(func.count(func.distinct(snapshot_table.symbol_id)))
                    .join(Symbol, Symbol.id == snapshot_table.symbol_id)
                    .where(
                        Symbol.market == market_code,
                        Symbol.is_active == 1,
                        snapshot_table.feature_name == feature_name,
                        snapshot_table.event_time <= cutoff_value,
                        snapshot_table.available_time <= cutoff_value,
                        snapshot_table.ingested_time <= cutoff_value,
                        freshness,
                    )
                )
                or 0
            )
            coverage = symbol_count / total_symbols if total_symbols else 0.0
            feature_coverage.append(
                {
                    "feature_name": feature_name,
                    "symbol_count": symbol_count,
                    "coverage": coverage,
                    "coverage_pct": round(coverage * 100.0, 2),
                    "max_age_days": int(max_age_days[feature_name]),
                    "cross_section_gate": (
                        "PASS"
                        if coverage >= minimum_cross_section_coverage
                        else "COLLECTING"
                    ),
                }
            )

        ready_symbol_count = 0
        if feature_names:
            ready_subquery = (
                select(snapshot_table.symbol_id)
                .join(Symbol, Symbol.id == snapshot_table.symbol_id)
                .where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                    snapshot_table.event_time <= cutoff_value,
                    snapshot_table.available_time <= cutoff_value,
                    snapshot_table.ingested_time <= cutoff_value,
                    or_(*freshness_filters),
                )
                .group_by(snapshot_table.symbol_id)
                .having(
                    func.count(func.distinct(snapshot_table.feature_name))
                    == len(feature_names)
                )
                .subquery()
            )
            ready_symbol_count = int(
                self.db.scalar(select(func.count()).select_from(ready_subquery)) or 0
            )
        minimum_coverage = min(
            (float(item["coverage"]) for item in feature_coverage),
            default=0.0,
        )
        return {
            "market": market_code,
            "cutoff": cutoff_text,
            "required_features": list(feature_names),
            "total_symbols": total_symbols,
            "ready_symbol_count": ready_symbol_count,
            "ready_symbol_pct": round(
                (ready_symbol_count / total_symbols) * 100.0 if total_symbols else 0.0,
                2,
            ),
            "minimum_feature_coverage": minimum_coverage,
            "minimum_feature_coverage_pct": round(minimum_coverage * 100.0, 2),
            "minimum_cross_section_coverage": float(minimum_cross_section_coverage),
            "as_of_gate": (
                "PASS"
                if feature_coverage and minimum_coverage >= minimum_cross_section_coverage
                else "COLLECTING"
            ),
            "feature_coverage": feature_coverage,
        }

    @staticmethod
    def _to_dict(snapshot: PointInTimeFeatureSnapshot, symbol: Symbol) -> dict:
        return {
            "id": snapshot.id,
            "symbol_id": snapshot.symbol_id,
            "ticker": symbol.ticker,
            "market": symbol.market,
            "feature_name": snapshot.feature_name,
            "feature_value": snapshot.feature_value,
            "event_time": snapshot.event_time,
            "available_time": snapshot.available_time,
            "ingested_time": snapshot.ingested_time,
            "source": snapshot.source,
            "source_record_id": snapshot.source_record_id,
            "revision_id": snapshot.revision_id,
            "payload_json": snapshot.payload_json,
            "created_at": snapshot.created_at,
            "source_layer": snapshot.__class__.__tablename__,
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

    def list_history_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        stmt = (
            select(ConceptSnapshot, Symbol)
            .join(Symbol, Symbol.id == ConceptSnapshot.symbol_id)
            .order_by(
                market_sort_case(Symbol.market),
                Symbol.ticker.asc(),
                ConceptSnapshot.as_of_date.asc(),
                ConceptSnapshot.concept_name.asc(),
                ConceptSnapshot.id.asc(),
            )
        )
        if market and market != "ALL":
            stmt = stmt.where(Symbol.market == market)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        rows = self.db.execute(stmt).all()
        payload: list[dict] = []
        for snapshot, symbol in rows:
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
        symbol_market = _symbol_market(self.db, symbol_id)
        physical_tables = _physical_snapshot_tables_for_market(symbol_market)
        physical_table = physical_tables[2] if physical_tables is not None else None
        write_legacy = _legacy_snapshot_writes_enabled(
            self.db,
            physical_tables,
            market=symbol_market,
        )
        existing = (
            self.db.scalar(
                select(TechnicalSnapshot).where(
                    TechnicalSnapshot.symbol_id == symbol_id
                )
            )
            if write_legacy
            else None
        )
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
        if write_legacy and existing is None:
            existing = TechnicalSnapshot(
                symbol_id=symbol_id,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        elif write_legacy:
            for key, value in payload.items():
                setattr(existing, key, value)
        physical_existing = None
        if physical_table is not None:
            physical_existing = self.db.scalar(
                select(physical_table).where(physical_table.symbol_id == symbol_id)
            )
            if physical_existing is None:
                physical_payload = {
                    **payload,
                    "as_of_date": _physical_date(as_of_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                physical_existing = physical_table(
                    symbol_id=symbol_id,
                    market=symbol_market,
                    created_at=_physical_datetime(now),
                    **physical_payload,
                )
                self.db.add(physical_existing)
            else:
                physical_payload = {
                    **payload,
                    "as_of_date": _physical_date(as_of_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                for key, value in physical_payload.items():
                    setattr(physical_existing, key, value)
        self.db.commit()
        result = existing if write_legacy else physical_existing
        if result is None:
            raise RuntimeError("Technical write did not produce a legacy or physical row.")
        self.db.refresh(result)
        return result

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[2] if physical_tables is not None else TechnicalSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
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
            "source_layer": snapshot.__class__.__tablename__,
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

    def complete_run(
        self,
        run_id: int,
        status: str,
        artifact_path: str | None = None,
        *,
        commit: bool = True,
    ) -> ModelRun | None:
        stmt = select(ModelRun).where(ModelRun.id == run_id)
        run = self.db.scalar(stmt)
        if run is None:
            return None
        run.status = status
        run.finished_at = utc_now_iso()
        if artifact_path is not None:
            run.artifact_path = artifact_path
        if commit:
            self.db.commit()
            self.db.refresh(run)
        else:
            self.db.flush()
        return run

    def merge_config(
        self,
        run_id: int,
        updates: dict,
        *,
        commit: bool = True,
    ) -> ModelRun | None:
        """Persist deterministic execution metadata while a model run is active."""

        run = self.db.scalar(select(ModelRun).where(ModelRun.id == int(run_id)))
        if run is None:
            return None
        config = _loads_json_object(run.config_json)
        config.update(dict(updates))
        run.config_json = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
        if commit:
            self.db.commit()
            self.db.refresh(run)
        else:
            self.db.flush()
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

    def replace_execution_audit(
        self,
        strategy_run_id: int,
        *,
        orders: list[dict] | tuple[dict, ...],
        fills: list[dict] | tuple[dict, ...],
        rejects: list[dict] | tuple[dict, ...],
        portfolio_states: list[dict] | tuple[dict, ...],
    ) -> dict[str, int]:
        for model in (StrategyFill, StrategyReject, StrategyOrder, StrategyPortfolioState):
            self.db.execute(delete(model).where(model.strategy_run_id == strategy_run_id))
        now = utc_now_iso()
        fill_order_ids = {str(row.get("order_id") or "") for row in fills}
        reject_order_ids = {str(row.get("order_id") or "") for row in rejects}
        order_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "order_id": str(row["order_id"]),
                "insight_id": row.get("insight_id"),
                "ticker": str(row["ticker"]),
                "side": str(row["side"]),
                "signal_date": row.get("signal_date"),
                "effective_date": str(row["effective_date"]),
                "order_type": str(row["order_type"]),
                "exit_reason": row.get("exit_reason"),
                "status": (
                    "filled"
                    if str(row["order_id"]) in fill_order_ids
                    else "rejected"
                    if str(row["order_id"]) in reject_order_ids
                    else "submitted"
                ),
                "created_at": now,
            }
            for row in orders
        ]
        fill_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "order_id": str(row["order_id"]),
                "insight_id": row.get("insight_id"),
                "ticker": str(row["ticker"]),
                "side": str(row["side"]),
                "fill_date": str(row["fill_date"]),
                "quantity": float(row.get("quantity") or 0.0),
                "reference_price": float(row.get("reference_price") or 0.0),
                "fill_price": float(row.get("fill_price") or 0.0),
                "fee": float(row.get("fee") or 0.0),
                "slippage": float(row.get("slippage") or 0.0),
                "notional": float(row.get("notional") or 0.0),
                "lot_id": row.get("lot_id"),
                "entry_date": row.get("entry_date"),
                "created_at": now,
            }
            for row in fills
        ]
        reject_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "order_id": str(row["order_id"]),
                "ticker": str(row["ticker"]),
                "side": str(row["side"]),
                "effective_date": str(row["effective_date"]),
                "reject_reason": str(row["reject_reason"]),
                "created_at": now,
            }
            for row in rejects
        ]
        state_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "trade_date": str(row["trade_date"]),
                "cash": float(row.get("cash") or 0.0),
                "position_market_value": float(row.get("position_market_value") or 0.0),
                "nav": float(row.get("nav") or 0.0),
                "gross_exposure": float(row.get("gross_exposure") or 0.0),
                "net_exposure": float(row.get("net_exposure") or 0.0),
                "cumulative_fees": float(row.get("cumulative_fees") or 0.0),
                "cumulative_slippage": float(row.get("cumulative_slippage") or 0.0),
                "open_lots": int(row.get("open_lots") or 0),
                "created_at": now,
            }
            for row in portfolio_states
        ]
        for model, rows in (
            (StrategyOrder, order_rows),
            (StrategyFill, fill_rows),
            (StrategyReject, reject_rows),
            (StrategyPortfolioState, state_rows),
        ):
            if rows:
                self.db.execute(insert(model), rows)
        self.db.commit()
        return {
            "orders": len(order_rows),
            "fills": len(fill_rows),
            "rejects": len(reject_rows),
            "portfolio_states": len(state_rows),
        }

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
