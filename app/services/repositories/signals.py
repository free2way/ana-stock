"""Model chart-signal and model-run domain repositories."""

import json
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import ModelChartSignal, ModelRun, Symbol
from app.services.market_storage_routing import (
    legacy_mirror_write_enabled,
    physical_fact_write_markets,
    physical_model_chart_signal_model,
)
from app.services.time_utils import app_now

from app.services.repositories.shared import (
    _loads_json_object,
    ticker_query_candidates,
    utc_now_iso,
)


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

    @staticmethod
    def _successful_run_filters(
        *,
        market: str | None,
        model_types: list[str] | None,
        universe_like: list[str] | None,
    ) -> list:
        clauses = [ModelRun.status == "success"]
        if market and str(market).upper() != "ALL":
            normalized_market = str(market).upper()
            clauses.append(
                or_(ModelRun.market == normalized_market, ModelRun.market == "MIXED")
            )
        if model_types:
            normalized_types = [str(item).strip() for item in model_types if str(item).strip()]
            if normalized_types:
                clauses.append(ModelRun.model_type.in_(normalized_types))
        if universe_like:
            universe_clauses = []
            for candidate in universe_like:
                normalized_candidate = str(candidate or "").strip()
                if not normalized_candidate:
                    continue
                universe_clauses.append(ModelRun.universe == normalized_candidate)
                universe_clauses.append(ModelRun.universe.like(f"{normalized_candidate}%"))
            if universe_clauses:
                clauses.append(or_(*universe_clauses))
        return clauses

    def get_latest_successful_run(
        self,
        *,
        market: str | None = None,
        model_types: list[str] | None = None,
        universe_like: list[str] | None = None,
    ) -> ModelRun | None:
        stmt = (
            select(ModelRun)
            .where(*self._successful_run_filters(
                market=market, model_types=model_types, universe_like=universe_like
            ))
            .order_by(ModelRun.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def list_successful_runs(
        self,
        *,
        market: str | None = None,
        model_types: list[str] | None = None,
        universe_like: list[str] | None = None,
        limit: int = 5,
    ) -> list[ModelRun]:
        """Newest-first successful runs eligible for champion selection.

        The caller (e.g. the screener model ranking) walks this window and skips
        runs the unified promotion gate withholds, so a non-servable latest run
        does not shadow an older servable one.
        """

        stmt = (
            select(ModelRun)
            .where(*self._successful_run_filters(
                market=market, model_types=model_types, universe_like=universe_like
            ))
            .order_by(ModelRun.id.desc())
            .limit(max(1, int(limit)))
        )
        return list(self.db.scalars(stmt).all())

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
