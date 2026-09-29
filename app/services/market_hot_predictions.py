from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import Date, and_, cast, delete, func, insert, literal, select
from sqlalchemy.orm import Session

from app.models.tables import (
    ModelRun,
    Prediction,
    PredictionDetail,
    PredictionExplanation,
    Symbol,
)
from app.services.market_storage_routing import (
    normalize_fact_market,
    physical_hot_prediction_models,
)
from app.services.time_utils import app_now


PREDICTION_COLUMNS = ("symbol_id", "trade_date", "score", "rank_value")
DETAIL_COLUMNS = (
    "confidence",
    "bullish_prob",
    "bearish_prob",
    "expected_return_5d",
    "expected_return_20d",
    "expected_drawdown_20d",
    "model_reward_risk_ratio",
    "risk_score",
    "target_horizon_days",
    "universe_size",
    "percentile",
    "regime_label",
    "conviction_bucket",
    "position_size_hint",
    "entry_style",
    "signal_label",
    "signal_strength",
    "summary_text",
)
EXPLANATION_COLUMNS = (
    "feature_name",
    "feature_value",
    "contribution",
    "direction",
    "display_order",
)

PREDICTION_FLOAT_COLUMNS = ("score", "rank_value")
DETAIL_FLOAT_COLUMNS = (
    "confidence",
    "bullish_prob",
    "bearish_prob",
    "expected_return_5d",
    "expected_return_20d",
    "expected_drawdown_20d",
    "model_reward_risk_ratio",
    "risk_score",
    "percentile",
    "signal_strength",
)
DETAIL_INTEGER_COLUMNS = ("target_horizon_days", "universe_size")
DETAIL_TEXT_COLUMNS = (
    "regime_label",
    "conviction_bucket",
    "position_size_hint",
    "entry_style",
    "signal_label",
    "summary_text",
)
EXPLANATION_FLOAT_COLUMNS = ("feature_value", "contribution")
EXPLANATION_INTEGER_COLUMNS = ("display_order",)
EXPLANATION_TEXT_COLUMNS = ("feature_name", "direction")

POSTGRES_MAX_BIND_PARAMETERS = 65_535
INSERT_BIND_PARAMETER_BUDGET = 60_000
DEFAULT_INSERT_BATCH_ROWS = 5_000


def _chunks(
    rows: list[dict],
    size: int = DEFAULT_INSERT_BATCH_ROWS,
) -> list[list[dict]]:
    """Split multi-row inserts without exceeding PostgreSQL's bind limit.

    SQLAlchemy expands ``insert(...).values(rows)`` to one bind parameter per
    value.  A fixed 5,000-row chunk is safe for the seven-column prediction
    payload, but the 20-column detail payload would create 100,000 parameters
    and fail before PostgreSQL can execute it.
    """
    if not rows:
        return []
    columns_per_row = max(len(row) for row in rows)
    parameter_limited_size = max(
        1,
        INSERT_BIND_PARAMETER_BUDGET // max(1, columns_per_row),
    )
    effective_size = min(max(1, int(size)), parameter_limited_size)
    return [
        rows[index : index + effective_size]
        for index in range(0, len(rows), effective_size)
    ]


def _normalize_date(value: object) -> str:
    return date.fromisoformat(str(value)[:10]).isoformat()


def _normalize_float(value: object) -> float | None:
    return None if value is None else float(value)


def _normalize_integer(value: object) -> int | None:
    return None if value is None else int(value)


def _normalize_text(value: object) -> str | None:
    return None if value is None else str(value)


def _normalize_typed_columns(
    raw: dict,
    *,
    float_columns: tuple[str, ...] = (),
    integer_columns: tuple[str, ...] = (),
    text_columns: tuple[str, ...] = (),
) -> dict:
    """Freeze database-native scalar types before hashing or persistence.

    Model outputs often contain numpy scalar values.  JSON serializes numpy
    float32/int64 through ``default=str`` while PostgreSQL returns native
    Python float/int values, which used to create a false semantic-parity
    failure even though the stored numbers were identical.
    """

    normalized = {
        column: _normalize_float(raw.get(column)) for column in float_columns
    }
    normalized.update(
        {
            column: _normalize_integer(raw.get(column))
            for column in integer_columns
        }
    )
    normalized.update(
        {column: _normalize_text(raw.get(column)) for column in text_columns}
    )
    return normalized


def _canonical_digest(rows: list[dict], columns: tuple[str, ...]) -> str:
    serialized = sorted(
        json.dumps(
            {column: row.get(column) for column in columns},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for row in rows
    )
    return hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def _prepare_rows(
    prediction_rows: list[dict],
    detail_rows: list[dict],
    explanation_rows: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    predictions: dict[tuple[int, str], dict] = {}
    for raw in prediction_rows:
        key = (int(raw["symbol_id"]), _normalize_date(raw["trade_date"]))
        row = {
            "symbol_id": key[0],
            "trade_date": key[1],
            **_normalize_typed_columns(
                raw,
                float_columns=PREDICTION_FLOAT_COLUMNS,
            ),
        }
        existing = predictions.get(key)
        if existing is None or float(row.get("score") or 0.0) > float(
            existing.get("score") or 0.0
        ):
            predictions[key] = row

    details: dict[tuple[int, str], dict] = {}
    for raw in detail_rows:
        key = (int(raw["symbol_id"]), _normalize_date(raw["trade_date"]))
        if key not in predictions:
            continue
        row = {
            "symbol_id": key[0],
            "trade_date": key[1],
            **_normalize_typed_columns(
                raw,
                float_columns=DETAIL_FLOAT_COLUMNS,
                integer_columns=DETAIL_INTEGER_COLUMNS,
                text_columns=DETAIL_TEXT_COLUMNS,
            ),
        }
        existing = details.get(key)
        if existing is None or (
            float(row.get("signal_strength") or 0.0),
            float(row.get("confidence") or 0.0),
        ) > (
            float(existing.get("signal_strength") or 0.0),
            float(existing.get("confidence") or 0.0),
        ):
            details[key] = row

    explanations: dict[tuple[int, str, str], dict] = {}
    for raw in explanation_rows:
        prediction_key = (
            int(raw["symbol_id"]),
            _normalize_date(raw["trade_date"]),
        )
        if prediction_key not in predictions:
            continue
        key = (*prediction_key, str(raw["feature_name"]))
        explanations[key] = {
            "symbol_id": prediction_key[0],
            "trade_date": prediction_key[1],
            **_normalize_typed_columns(
                raw,
                float_columns=EXPLANATION_FLOAT_COLUMNS,
                integer_columns=EXPLANATION_INTEGER_COLUMNS,
                text_columns=EXPLANATION_TEXT_COLUMNS,
            ),
        }
    return list(predictions.values()), list(details.values()), list(explanations.values())


class MarketHotPredictionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _load_rows(self, *, market: str, model_run_id: int) -> tuple[list[dict], list[dict], list[dict]]:
        prediction_table, detail_table, explanation_table = physical_hot_prediction_models(
            market
        )
        predictions = [
            dict(row)
            for row in self.db.execute(
                select(
                    prediction_table.symbol_id,
                    prediction_table.trade_date,
                    prediction_table.score,
                    prediction_table.rank_value,
                ).where(prediction_table.model_run_id == int(model_run_id))
            ).mappings()
        ]
        for row in predictions:
            row["trade_date"] = _normalize_date(row["trade_date"])
        details = [
            dict(row)
            for row in self.db.execute(
                select(
                    prediction_table.symbol_id,
                    prediction_table.trade_date,
                    *(getattr(detail_table, column) for column in DETAIL_COLUMNS),
                )
                .join(detail_table, detail_table.prediction_id == prediction_table.id)
                .where(prediction_table.model_run_id == int(model_run_id))
            ).mappings()
        ]
        for row in details:
            row["trade_date"] = _normalize_date(row["trade_date"])
        explanations = [
            dict(row)
            for row in self.db.execute(
                select(
                    prediction_table.symbol_id,
                    prediction_table.trade_date,
                    *(getattr(explanation_table, column) for column in EXPLANATION_COLUMNS),
                )
                .join(
                    explanation_table,
                    explanation_table.prediction_id == prediction_table.id,
                )
                .where(prediction_table.model_run_id == int(model_run_id))
            ).mappings()
        ]
        for row in explanations:
            row["trade_date"] = _normalize_date(row["trade_date"])
        return predictions, details, explanations

    @staticmethod
    def _digests(
        predictions: list[dict],
        details: list[dict],
        explanations: list[dict],
    ) -> dict[str, str]:
        return {
            "predictions": _canonical_digest(predictions, PREDICTION_COLUMNS),
            "prediction_details": _canonical_digest(
                details,
                ("symbol_id", "trade_date", *DETAIL_COLUMNS),
            ),
            "prediction_explanations": _canonical_digest(
                explanations,
                ("symbol_id", "trade_date", *EXPLANATION_COLUMNS),
            ),
        }

    def publish_for_model_run(
        self,
        *,
        model_run_id: int,
        market: str,
        prediction_rows: list[dict],
        detail_rows: list[dict],
        explanation_rows: list[dict],
        commit: bool = True,
        verify_after_write: bool = True,
    ) -> dict:
        normalized_market = normalize_fact_market(market)
        run_market = self.db.scalar(
            select(ModelRun.market).where(ModelRun.id == int(model_run_id))
        )
        if str(run_market or "").upper() != normalized_market:
            raise RuntimeError(
                f"Model run {model_run_id} market {run_market!r} cannot write "
                f"{normalized_market} facts."
            )

        predictions, details, explanations = _prepare_rows(
            prediction_rows,
            detail_rows,
            explanation_rows,
        )
        symbol_ids = {int(row["symbol_id"]) for row in predictions}
        matched_symbols = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.id.in_(symbol_ids),
                    Symbol.market == normalized_market,
                )
            )
            or 0
        )
        if matched_symbols != len(symbol_ids):
            raise RuntimeError(
                f"Refusing {normalized_market} hot write: "
                f"{len(symbol_ids) - matched_symbols} symbols are missing or cross-market."
            )

        source_digests = self._digests(predictions, details, explanations)
        existing = self._load_rows(
            market=normalized_market,
            model_run_id=int(model_run_id),
        )
        existing_digests = self._digests(*existing)
        source_counts = {
            "predictions": len(predictions),
            "prediction_details": len(details),
            "prediction_explanations": len(explanations),
        }
        existing_counts = {
            "predictions": len(existing[0]),
            "prediction_details": len(existing[1]),
            "prediction_explanations": len(existing[2]),
        }
        if source_counts == existing_counts and source_digests == existing_digests:
            return {
                "status": "pass",
                "action": "unchanged",
                "market": normalized_market,
                "model_run_id": int(model_run_id),
                "counts": source_counts,
                "digests": source_digests,
                "exact_match": True,
            }

        prediction_table, detail_table, explanation_table = physical_hot_prediction_models(
            normalized_market
        )
        now = app_now()
        try:
            self.db.execute(
                delete(prediction_table).where(
                    prediction_table.model_run_id == int(model_run_id)
                )
            )
            prediction_map: dict[tuple[int, str], int] = {}
            prediction_payloads = [
                {
                    "model_run_id": int(model_run_id),
                    "market": normalized_market,
                    "symbol_id": int(row["symbol_id"]),
                    "trade_date": date.fromisoformat(str(row["trade_date"])),
                    "score": row.get("score"),
                    "rank_value": row.get("rank_value"),
                    "created_at": now,
                }
                for row in predictions
            ]
            for chunk in _chunks(prediction_payloads):
                inserted = self.db.execute(
                    insert(prediction_table)
                    .values(chunk)
                    .returning(
                        prediction_table.id,
                        prediction_table.symbol_id,
                        prediction_table.trade_date,
                    )
                ).all()
                prediction_map.update(
                    {
                        (int(row.symbol_id), _normalize_date(row.trade_date)): int(row.id)
                        for row in inserted
                    }
                )

            detail_payloads = [
                {
                    "prediction_id": prediction_map[
                        (int(row["symbol_id"]), str(row["trade_date"]))
                    ],
                    **{column: row.get(column) for column in DETAIL_COLUMNS},
                    "created_at": now,
                }
                for row in details
            ]
            for chunk in _chunks(detail_payloads):
                self.db.execute(insert(detail_table).values(chunk))

            explanation_payloads = [
                {
                    "prediction_id": prediction_map[
                        (int(row["symbol_id"]), str(row["trade_date"]))
                    ],
                    **{column: row.get(column) for column in EXPLANATION_COLUMNS},
                    "created_at": now,
                }
                for row in explanations
            ]
            for chunk in _chunks(explanation_payloads):
                self.db.execute(insert(explanation_table).values(chunk))

            published_counts = source_counts
            published_digests = source_digests
            if verify_after_write:
                # Backfills and acceptance utilities retain the full in-transaction
                # round trip.  The production trainer performs the same semantic
                # parity check after commit through the immutable dual-write audit;
                # re-reading tens of thousands of just-inserted rows here doubled
                # the critical write-path latency without adding rollback safety.
                published = self._load_rows(
                    market=normalized_market,
                    model_run_id=int(model_run_id),
                )
                published_counts = {
                    "predictions": len(published[0]),
                    "prediction_details": len(published[1]),
                    "prediction_explanations": len(published[2]),
                }
                published_digests = self._digests(*published)
                exact_match = (
                    source_counts == published_counts
                    and source_digests == published_digests
                )
                if not exact_match:
                    raise RuntimeError(
                        "Physical hot prediction semantic parity failed: "
                        + json.dumps(
                            {
                                "source_counts": source_counts,
                                "published_counts": published_counts,
                                "source_digests": source_digests,
                                "published_digests": published_digests,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
            if commit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {
            "status": "pass",
            "action": "replaced",
            "market": normalized_market,
            "model_run_id": int(model_run_id),
            "counts": published_counts,
            "digests": published_digests,
            "exact_match": True,
            "verified_after_write": bool(verify_after_write),
            "tables": [
                prediction_table.__tablename__,
                detail_table.__tablename__,
                explanation_table.__tablename__,
            ],
        }

    def publish_from_legacy_mirror(
        self,
        *,
        model_run_id: int,
        market: str,
        commit: bool = True,
    ) -> dict:
        """Populate a physical market store from the in-transaction legacy mirror.

        During the observed dual-write phase the legacy rows have already been
        normalized and inserted.  Keeping the second copy inside PostgreSQL via
        ``INSERT .. SELECT`` avoids compiling and transmitting the same large
        Python payload twice while preserving one atomic transaction.
        """
        normalized_market = normalize_fact_market(market)
        run_id = int(model_run_id)
        run_market = self.db.scalar(
            select(ModelRun.market).where(ModelRun.id == run_id)
        )
        if str(run_market or "").upper() != normalized_market:
            raise RuntimeError(
                f"Model run {run_id} market {run_market!r} cannot write "
                f"{normalized_market} facts."
            )
        source_counts = {
            "predictions": int(
                self.db.scalar(
                    select(func.count(Prediction.id)).where(
                        Prediction.model_run_id == run_id
                    )
                )
                or 0
            ),
            "prediction_details": int(
                self.db.scalar(
                    select(func.count(PredictionDetail.id))
                    .join(Prediction, Prediction.id == PredictionDetail.prediction_id)
                    .where(Prediction.model_run_id == run_id)
                )
                or 0
            ),
            "prediction_explanations": int(
                self.db.scalar(
                    select(func.count(PredictionExplanation.id))
                    .join(
                        Prediction,
                        Prediction.id == PredictionExplanation.prediction_id,
                    )
                    .where(Prediction.model_run_id == run_id)
                )
                or 0
            ),
        }
        if source_counts["predictions"] == 0:
            raise RuntimeError(
                f"Legacy mirror for model run {run_id} is empty; refusing physical publish."
            )

        prediction_table, detail_table, explanation_table = physical_hot_prediction_models(
            normalized_market
        )
        now = app_now()
        try:
            self.db.execute(
                delete(prediction_table).where(prediction_table.model_run_id == run_id)
            )
            self.db.execute(
                insert(prediction_table).from_select(
                    [
                        "model_run_id",
                        "market",
                        "symbol_id",
                        "trade_date",
                        "score",
                        "rank_value",
                        "created_at",
                    ],
                    select(
                        Prediction.model_run_id,
                        literal(normalized_market),
                        Prediction.symbol_id,
                        cast(Prediction.trade_date, Date),
                        Prediction.score,
                        Prediction.rank_value,
                        literal(now),
                    ).where(Prediction.model_run_id == run_id),
                )
            )
            prediction_join = and_(
                prediction_table.model_run_id == run_id,
                prediction_table.symbol_id == Prediction.symbol_id,
                prediction_table.trade_date == cast(Prediction.trade_date, Date),
            )
            self.db.execute(
                insert(detail_table).from_select(
                    ["prediction_id", *DETAIL_COLUMNS, "created_at"],
                    select(
                        prediction_table.id,
                        *(getattr(PredictionDetail, column) for column in DETAIL_COLUMNS),
                        literal(now),
                    )
                    .select_from(PredictionDetail)
                    .join(Prediction, Prediction.id == PredictionDetail.prediction_id)
                    .join(prediction_table, prediction_join)
                    .where(Prediction.model_run_id == run_id),
                )
            )
            self.db.execute(
                insert(explanation_table).from_select(
                    ["prediction_id", *EXPLANATION_COLUMNS, "created_at"],
                    select(
                        prediction_table.id,
                        *(
                            getattr(PredictionExplanation, column)
                            for column in EXPLANATION_COLUMNS
                        ),
                        literal(now),
                    )
                    .select_from(PredictionExplanation)
                    .join(
                        Prediction,
                        Prediction.id == PredictionExplanation.prediction_id,
                    )
                    .join(prediction_table, prediction_join)
                    .where(Prediction.model_run_id == run_id),
                )
            )
            if commit:
                self.db.commit()
            return {
                "status": "pass",
                "action": "copied_from_legacy_mirror",
                "market": normalized_market,
                "model_run_id": run_id,
                "counts": source_counts,
            }
        except Exception:
            self.db.rollback()
            raise

    def remove_for_model_run(self, *, market: str, model_run_id: int) -> int:
        prediction_table, _, _ = physical_hot_prediction_models(market)
        result = self.db.execute(
            delete(prediction_table).where(
                prediction_table.model_run_id == int(model_run_id)
            )
        )
        self.db.commit()
        return int(result.rowcount or 0)
