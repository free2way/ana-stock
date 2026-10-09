from __future__ import annotations

import json
import math
import resource
import statistics
import sys
import time
import tracemalloc
import warnings
from bisect import bisect_right
from dataclasses import asdict
from collections import Counter, defaultdict
from datetime import date, datetime

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.corporate_action_coverage import (
    assess_corporate_action_coverage,
    coverage_evidence_fields,
)
from app.services.market_lake import get_latest_lake_trade_date, load_lake_rows
from app.services.market_hot_predictions import MarketHotPredictionRepository
from app.services.market_storage_routing import legacy_mirror_write_enabled
from app.services.model_signal_summary import enrich_model_output, summarize_model_output
from app.services.model_score_contract import SCORE_CONTRACT_VERSION
from app.services.execution_costs import FillCostModel
from app.services.execution_reconciliation import (
    VERSION as RECONCILED_VERSION, execution_contract, replay_candidate,
)
from app.services.prediction_artifacts import (
    PredictionArtifactWriter,
    PredictionPublicationLimitError,
    select_hot_explanation_rows,
    select_hot_prediction_rows,
)
from app.services.portfolio_book import load_portfolio_positions
from app.services.repository import (
    ConceptSnapshotRepository,
    FundamentalSnapshotRepository,
    LivePredictionRepository,
    ModelRunRepository,
    PointInTimeFeatureSnapshotRepository,
    PredictionArtifactRepository,
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionWriteRepository,
    SymbolRepository,
    WorkspaceSnapshotRepository,
)
from app.services.stock_selection.walk_forward import PointInTimeTrainingPool
from app.services.stock_selection.universe import default_universe_rules
from app.services.stock_selection.training_weights import (
    TRAINING_WEIGHT_POLICY, date_balanced_training_weights,
)
from app.services.stock_selection.executable_outcomes import (
    confirmed_outcome,
    limit_up_at_open,
    ExecutionEligibility,
)
from app.services.adjustment_snapshot import adjustment_version_binding
from app.services.price_basis_contract import (
    DECISION_REJECT,
    ENTRY_INFERENCE,
    ENTRY_TRAIN,
    RAW_FALLBACK_AUTHORIZATION_SOURCE,
    REASON_ADJUSTED_VIEW_ABSENT,
    REASON_UNREADABLE_VIEW,
    AdjustedViewProbe,
    PriceBasisRequirements,
    decide_price_basis,
    read_adjusted_view_with_probe,
)
from app.services.optin_audit import build_optin_audit
from app.services.ticker_format import infer_market_from_ticker
from app.services.stock_selection.labels import PriceBar
from app.services.stock_selection.training_window import TrainingWindowPolicy, TrainingWindowBlocked, select_training_window

EXECUTABLE_LABEL_PROFILE = "executable_net_return_v1"
LEGACY_LABEL_PROFILE = "legacy_short_horizon_composite_v1"

# Label families whose prices are read through `_label_price` and therefore
# must be tracked for adjusted-view coverage. The reconciled profile replays
# raw-basis execution provenance by contract and is deliberately excluded.
LABEL_PRICE_TRACKED_PROFILES = (EXECUTABLE_LABEL_PROFILE, LEGACY_LABEL_PROFILE)

# Walk-forward OOS evidence written into the run config for the unified
# promotion gate (``promotion_gate_v2`` reads ``oos_evaluation`` with an
# ``evaluated_date_count`` and a ``mean_risk_adjusted_return``). The trainer
# evaluates its own matured out-of-sample predictions, top-N by score per
# prediction date; only return-based label profiles can supply it.
OOS_EVALUATION_TOP_N = 5
OOS_RETURN_LABEL_PROFILES = (EXECUTABLE_LABEL_PROFILE, RECONCILED_VERSION)

# The trainer has no independent universe/metadata/industry readiness audit;
# that evidence is produced by the research pipeline
# (``audit_market_research_readiness``). Recorded next to the evidence it could
# not supply so the omission is auditable rather than silently missing.
DATA_READINESS_EVIDENCE_MISSING_REASON = (
    "the trainer does not run a universe/metadata/industry readiness audit; "
    "readiness evidence is produced by the research pipeline "
    "(audit_market_research_readiness) and must be attached by promotion tooling"
)


def _empty_label_price_stats() -> dict[str, int]:
    return {
        "adjusted_count": 0,
        "raw_fallback_count": 0,
        "dropped_missing_adjusted_count": 0,
    }

# Fundamental features the trainer consumes. History now comes from the
# point-in-time feature store so that the availability timestamp (publication
# / ingestion), not the fiscal period end, decides when a row may be used.
TRAINER_FUNDAMENTAL_FEATURES = (
    "pe_ttm",
    "dividend_yield",
    "market_cap",
    "roe_avg_3y",
    "net_profit_yoy",
    "revenue_yoy",
    "debt_to_assets",
)


def group_point_in_time_fundamental_history(records: list[dict]) -> dict[str, list[dict]]:
    """Pivot long-format PIT feature rows into per-report training history.

    Each output item carries the *maximum* ``available_time`` of its feature
    group so a partially published report is never treated as fully known
    before its last feature became available. Items are ordered by
    availability, which lets the cursor stop safely at the first row that is
    not yet observable.
    """

    grouped: dict[tuple[str, str], dict] = {}
    for record in records:
        ticker = str(record.get("ticker") or "").strip().upper()
        feature_name = str(record.get("feature_name") or "").strip()
        if not ticker or feature_name not in TRAINER_FUNDAMENTAL_FEATURES:
            continue
        payload: dict = {}
        raw_payload = record.get("payload_json")
        if raw_payload:
            try:
                parsed = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload)
                payload = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                payload = {}
        report_date = str(payload.get("report_date") or str(record.get("event_time") or "")[:10]).strip()
        available_time = str(record.get("available_time") or "").strip()
        ingested_time = str(record.get("ingested_time") or "").strip()
        key = (ticker, report_date)
        item = grouped.setdefault(
            key,
            {
                "ticker": ticker,
                "report_date": report_date,
                "available_time": available_time,
                "ingested_time": ingested_time,
                "source": str(record.get("source") or "").strip(),
            },
        )
        if available_time > str(item.get("available_time") or ""):
            item["available_time"] = available_time
        if ingested_time > str(item.get("ingested_time") or ""):
            item["ingested_time"] = ingested_time
        value = record.get("feature_value")
        if value is not None:
            try:
                item[feature_name] = float(value)
            except (TypeError, ValueError):
                continue
    history: dict[str, list[dict]] = defaultdict(list)
    for item in grouped.values():
        cleaned = {key: value for key, value in item.items() if value not in (None, "")}
        if not cleaned.get("available_time"):
            # Fail closed: a report without a publication timestamp is not
            # allowed to enter training at an assumed date.
            continue
        history[str(item["ticker"])].append(cleaned)
    for ticker in history:
        history[ticker].sort(
            key=lambda item: (str(item.get("available_time") or ""), str(item.get("report_date") or ""))
        )
    return dict(history)



def resolve_label_profile_contract(label_profile_setting: str | None) -> tuple[str, str]:
    """Map the configured CN label profile to (target_profile, score_semantics).

    The run contract recorded at create time must describe the label family the
    samples actually carry. Shipping the executable switch while the run
    metadata still claims the legacy composite breaks every downstream audit
    that separates the two label regimes.
    """
    normalized = str(label_profile_setting or "").strip().lower()
    if normalized == RECONCILED_VERSION:
        return RECONCILED_VERSION, "executable_next_open_net_return"
    if normalized == EXECUTABLE_LABEL_PROFILE:
        return "confirmed_next_open_fixed_exit_fill_cost_v2", "executable_next_open_net_return"
    if normalized == LEGACY_LABEL_PROFILE:
        return "short_horizon_composite_v1", "legacy_composite_margin"
    raise RuntimeError(f"Unsupported trainer label profile `{label_profile_setting}`.")


try:
    import lightgbm as lgb  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    lgb = None

try:
    import xgboost as xgb  # type: ignore
except ImportError:  # pragma: no cover - optional challenger
    xgb = None

try:
    import catboost as cat  # type: ignore
except ImportError:  # pragma: no cover - optional challenger
    cat = None


class SignalTrainer:
    """Train production signals and expose versioned challenger label adapters."""

    # Label price basis: "raw" unless the adjusted view was attached (A1).
    _label_basis: str = "raw"

    @staticmethod
    def executable_training_target(
        bars: list[PriceBar], *, signal_date: date, trading_dates: list[date],
        horizon_days: int, market: str, cost_bps: float | None = None,
        eligibility: ExecutionEligibility, target_mode: str = "net_return",
        industry_return: float | None = None,
        cost_model: FillCostModel | None = None,
    ) -> dict:
        """Challenger entry point; never mixes legacy composite and net labels."""
        if target_mode not in {"net_return", "industry_excess_return"}:
            raise ValueError("unsupported executable target mode")
        if target_mode == "industry_excess_return" and industry_return is None:
            raise ValueError("industry return required for industry-neutral target")
        label = confirmed_outcome(
            bars, signal_date=signal_date, trading_dates=trading_dates,
            horizon_days=horizon_days, market=market, cost_bps=cost_bps,
            eligibility=eligibility, industry_return=industry_return if industry_return is not None else 0.0,
            cost_model=cost_model,
        )
        return {
            "target": getattr(label, target_mode) if label else None,
            "target_mode": target_mode,
            "label_version": label.label_version if label else None,
            "label_available_date": label.label_available_date.isoformat() if label else None,
            "exclusion_reason": label.exclusion_reason if label else "label_not_mature",
            "cost_model_version": label.cost_model_version if label else None,
            "cost_model_hash": label.cost_model_hash if label else None,
        }

    @staticmethod
    def _complete_date_training_window(samples: list[dict], *, max_rows: int) -> list[dict]:
        """Choose whole newest market-date groups, independent of maturity arrival order."""
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        by_date: dict[str, list[dict]] = defaultdict(list)
        for sample in samples:
            by_date[str(sample["trade_date"])].append(sample)
        selected: list[dict] = []
        for trade_date in sorted(by_date, reverse=True):
            group = by_date[trade_date]
            if selected and len(selected) + len(group) > max_rows:
                break
            selected.extend(group)
        return sorted(selected, key=lambda row: (str(row["trade_date"]), str(row.get("ticker") or row.get("symbol") or "")))

    MODEL_CALIBRATION_SNAPSHOT_TYPE = "model_calibration_snapshot"

    @staticmethod
    def _process_peak_rss_bytes() -> int:
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
        # macOS reports bytes; Linux and most other Unix platforms report KiB.
        return peak if sys.platform == "darwin" else peak * 1024

    def __init__(self) -> None:
        self.settings = get_settings()
        # Label-side price-basis bookkeeping (A1). `_adjusted_basis_expected`
        # records whether an adjusted view was even applicable (CN/US); the
        # counters describe how the samples that were actually built used it.
        self._adjusted_basis_expected = False
        # Three-state adjusted-view status (A1 fail-closed follow-up): one of
        # ``absent`` / ``present`` / ``unreadable``. ``unreadable`` (file exists
        # but cannot be read/parsed) is never downgraded to "no view".
        self._adjusted_view_state = "absent"
        self._adjusted_view_error: str | None = None
        self._adjusted_view_market: str | None = None
        # Shared price-basis contract state (train decision + the probe it came
        # from) so the prediction product can persist the same audit fields.
        self._adjusted_view_probe: AdjustedViewProbe | None = None
        self._price_basis_decision = None
        self._price_basis_requirements: PriceBasisRequirements | None = None
        self._label_price_stats: dict[str, int] = _empty_label_price_stats()
        # Point-in-time universe filter outcome for the most recent `_load_rows`
        # call. Persisted into the run config so a filtered run is auditable;
        # `None` when rows were supplied without going through `_load_rows`.
        self._universe_filter_stats: dict | None = None
        # Signal days suppressed by the point-in-time universe gate while the
        # full timeline is retained (counted by `_build_lightgbm_samples`).
        self._universe_gated_signal_days: int = 0

    def _training_window_policy(self, market: str | None) -> TrainingWindowPolicy:
        prefix = "trainer_cn_window" if str(market or "").upper() == "CN" else "trainer_us_window"
        return TrainingWindowPolicy(mode=getattr(self.settings, f"{prefix}_mode"),
            date_count=getattr(self.settings, f"{prefix}_dates"),
            max_rows=getattr(self.settings, f"{prefix}_max_rows"),
            max_estimated_fit_bytes=getattr(self.settings, f"{prefix}_max_estimated_fit_bytes"))

    @staticmethod
    def _holding_symbol_ids(db, *, market: str | None) -> set[int]:
        market_code = str(market or "").strip().upper()
        try:
            positions = load_portfolio_positions()
        except Exception:
            return set()
        tickers = {
            str(item.get("ticker") or "").strip().upper()
            for item in positions
            if str(item.get("ticker") or "").strip()
            and (
                not market_code
                or str(item.get("market") or "").strip().upper() == market_code
            )
        }
        symbol_repo = SymbolRepository(db)
        symbol_ids: set[int] = set()
        for ticker in sorted(tickers):
            symbol = symbol_repo.get_by_ticker(ticker)
            if symbol is not None and (
                not market_code
                or str(symbol.market or "").strip().upper() == market_code
            ):
                symbol_ids.add(int(symbol.id))
        return symbol_ids

    def _persist_model_outputs(
        self,
        *,
        db,
        model_repo: ModelRunRepository,
        prediction_repo: PredictionWriteRepository,
        detail_repo: PredictionDetailRepository,
        explanation_repo: PredictionExplanationRepository,
        run_id: int,
        market: str | None,
        signal_rows: list[dict],
        detail_rows: list[dict],
        explanation_rows: list[dict],
        model_metadata: dict,
    ) -> int:
        artifact_manifest: dict | None = None
        artifact_path: str | None = None
        publication_started = time.perf_counter()
        publication_timings_ms: dict[str, float] = {}
        owns_memory_trace = not tracemalloc.is_tracing()
        if owns_memory_trace:
            tracemalloc.start()
        trace_start_current_bytes, trace_start_peak_bytes = (
            int(value) for value in tracemalloc.get_traced_memory()
        )
        try:
            hot_mode = str(self.settings.prediction_hot_write_mode or "compact").strip().lower()
            if hot_mode not in {"compact", "legacy_full"}:
                raise RuntimeError(f"Unsupported prediction hot write mode: {hot_mode}")
            artifact_writer = PredictionArtifactWriter()
            try:
                publication_plan = artifact_writer.plan(
                    prediction_rows=signal_rows,
                    detail_rows=detail_rows,
                    explanation_rows=explanation_rows,
                )
            except PredictionPublicationLimitError as exc:
                model_repo.merge_config(
                    run_id,
                    {"prediction_publication_plan": exc.plan},
                )
                raise
            model_repo.merge_config(
                run_id,
                {"prediction_publication_plan": publication_plan},
            )
            holding_symbol_ids = self._holding_symbol_ids(db, market=market)
            normalized_market = str(market or "").strip().upper()
            legacy_hot_dual_write = legacy_mirror_write_enabled(
                db,
                market=normalized_market,
                configured=bool(
                    getattr(
                        self.settings,
                        "market_physical_hot_dual_write_legacy",
                        True,
                    )
                ),
            )
            published_metadata = {
                **model_metadata,
                "prediction_publication_plan": publication_plan,
                "prediction_storage_contract": {
                    "contract_version": "prediction-storage-v1",
                    "hot_write_mode": hot_mode,
                    "hot_full_trade_days": int(self.settings.prediction_hot_full_trade_days),
                    "hot_top_k": int(self.settings.prediction_hot_top_k),
                    "hot_explanation_limit": int(
                        getattr(self.settings, "prediction_hot_explanation_limit", 50)
                    ),
                    "hot_explanation_boundary_radius": int(
                        getattr(
                            self.settings,
                            "prediction_hot_explanation_boundary_radius",
                            5,
                        )
                    ),
                    "hot_explanation_holding_count": len(holding_symbol_ids),
                    "hot_explanation_holding_symbol_ids": sorted(holding_symbol_ids),
                    "legacy_hot_dual_write": legacy_hot_dual_write,
                    "cold_explanation_scope": "latest_full_cross_section",
                    "cold_payload": "full",
                },
            }
            if self.settings.prediction_artifacts_enabled:
                phase_started = time.perf_counter()
                artifact_manifest = artifact_writer.write(
                    model_run_id=run_id,
                    market=market,
                    prediction_rows=signal_rows,
                    detail_rows=detail_rows,
                    explanation_rows=explanation_rows,
                    model_metadata=published_metadata,
                    publication_plan=publication_plan,
                )
                publication_timings_ms["artifact_publish_ms"] = round(
                    (time.perf_counter() - phase_started) * 1000.0,
                    3,
                )
                artifact_path = str(artifact_manifest["artifact_path"])
                PredictionArtifactRepository(db).upsert_manifest(artifact_manifest, status="verified")
            else:
                artifact_file = self.settings.artifacts_dir / f"model_run_{run_id}.json"
                artifact_file.write_text(
                    json.dumps(published_metadata, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                artifact_path = str(artifact_file.resolve())

            hot_signal_rows = signal_rows
            if hot_mode == "compact":
                hot_signal_rows = select_hot_prediction_rows(
                    signal_rows,
                    full_trade_days=self.settings.prediction_hot_full_trade_days,
                    top_k=self.settings.prediction_hot_top_k,
                )
            hot_keys = {
                (int(row["symbol_id"]), str(row["trade_date"]))
                for row in hot_signal_rows
            }
            hot_detail_rows = [
                row for row in detail_rows
                if (int(row["symbol_id"]), str(row["trade_date"])) in hot_keys
            ]
            selected_explanation_rows = select_hot_explanation_rows(
                explanation_rows,
                prediction_rows=signal_rows,
                top_k=int(
                    getattr(self.settings, "prediction_hot_explanation_limit", 50)
                ),
                holding_symbol_ids=holding_symbol_ids,
                boundary_radius=int(
                    getattr(
                        self.settings,
                        "prediction_hot_explanation_boundary_radius",
                        5,
                    )
                ),
            )
            hot_explanation_rows = [
                row for row in selected_explanation_rows
                if (int(row["symbol_id"]), str(row["trade_date"])) in hot_keys
            ]
            postgresql_started = time.perf_counter()
            if legacy_hot_dual_write:
                phase_started = time.perf_counter()
                count = prediction_repo.replace_for_model_run(
                    run_id,
                    hot_signal_rows,
                    commit=False,
                )
                publication_timings_ms["legacy_predictions_ms"] = round(
                    (time.perf_counter() - phase_started) * 1000.0,
                    3,
                )
                phase_started = time.perf_counter()
                detail_repo.replace_for_model_run(
                    run_id,
                    hot_detail_rows,
                    commit=False,
                )
                publication_timings_ms["legacy_details_ms"] = round(
                    (time.perf_counter() - phase_started) * 1000.0,
                    3,
                )
                phase_started = time.perf_counter()
                explanation_repo.replace_for_model_run(
                    run_id,
                    hot_explanation_rows,
                    commit=False,
                )
                publication_timings_ms["legacy_explanations_ms"] = round(
                    (time.perf_counter() - phase_started) * 1000.0,
                    3,
                )
            else:
                count = len(hot_signal_rows)
                publication_timings_ms.update(
                    {
                        "legacy_predictions_ms": 0.0,
                        "legacy_details_ms": 0.0,
                        "legacy_explanations_ms": 0.0,
                    }
                )
            phase_started = time.perf_counter()
            physical_hot_repo = MarketHotPredictionRepository(db)
            if legacy_hot_dual_write:
                physical_hot_repo.publish_from_legacy_mirror(
                    model_run_id=run_id,
                    market=str(market or ""),
                    commit=False,
                )
            else:
                physical_hot_repo.publish_for_model_run(
                    model_run_id=run_id,
                    market=str(market or ""),
                    prediction_rows=hot_signal_rows,
                    detail_rows=hot_detail_rows,
                    explanation_rows=hot_explanation_rows,
                    commit=False,
                    verify_after_write=False,
                )
            publication_timings_ms["physical_hot_ms"] = round(
                (time.perf_counter() - phase_started) * 1000.0,
                3,
            )
            phase_started = time.perf_counter()
            LivePredictionRepository(db).publish_from_physical_hot(
                model_run_id=run_id,
                market=str(market or ""),
                commit=False,
            )
            publication_timings_ms["live_predictions_ms"] = round(
                (time.perf_counter() - phase_started) * 1000.0,
                3,
            )
            model_repo.complete_run(
                run_id,
                status="success",
                artifact_path=artifact_path,
                commit=False,
            )
            commit_started = time.perf_counter()
            db.commit()
            publication_timings_ms["postgresql_commit_ms"] = round(
                (time.perf_counter() - commit_started) * 1000.0,
                3,
            )
            publication_timings_ms["postgresql_write_phase_ms"] = round(
                (time.perf_counter() - postgresql_started) * 1000.0,
                3,
            )
            publication_timings_ms["publication_total_ms"] = round(
                (time.perf_counter() - publication_started) * 1000.0,
                3,
            )
            traced_current_bytes, traced_peak_bytes = tracemalloc.get_traced_memory()
            model_repo.merge_config(
                run_id,
                {
                    "prediction_publication_timing": {
                        "timing_version": "prediction-publication-timing-v2",
                        **publication_timings_ms,
                        "process_peak_rss_bytes": self._process_peak_rss_bytes(),
                        "python_tracemalloc_current_bytes": int(traced_current_bytes),
                        "python_tracemalloc_peak_bytes": int(traced_peak_bytes),
                        "python_publication_incremental_peak_bytes": max(
                            0,
                            int(traced_peak_bytes)
                            - max(
                                trace_start_current_bytes,
                                trace_start_peak_bytes,
                            ),
                        ),
                        "cold_rows": len(signal_rows),
                        "hot_prediction_rows": len(hot_signal_rows),
                        "hot_detail_rows": len(hot_detail_rows),
                        "hot_explanation_rows": len(hot_explanation_rows),
                        "legacy_hot_dual_write": legacy_hot_dual_write,
                        "postgresql_atomic_publish": True,
                        "postgresql_transaction_version": "prediction-publication-transaction-v1",
                    }
                },
            )
            try:
                LivePredictionRepository(db).prune_market_snapshots(
                    market=str(market or ""),
                    keep_runs=2,
                )
            except Exception:
                db.rollback()
            return count
        except Exception:
            db.rollback()
            try:
                prediction_repo.replace_for_model_run(run_id, [])
            except Exception:
                db.rollback()
            try:
                LivePredictionRepository(db).remove_for_model_run(run_id)
            except Exception:
                db.rollback()
            try:
                MarketHotPredictionRepository(db).remove_for_model_run(
                    market=str(market or ""),
                    model_run_id=run_id,
                )
            except Exception:
                db.rollback()
            if artifact_manifest is not None:
                try:
                    PredictionArtifactRepository(db).set_status(run_id, "failed_run")
                except Exception:
                    db.rollback()
            model_repo.complete_run(run_id, status="failed", artifact_path=artifact_path)
            raise
        finally:
            if owns_memory_trace and tracemalloc.is_tracing():
                tracemalloc.stop()

    def _normalize_market_code(self, market: str | None) -> str | None:
        normalized = str(market or "").strip().upper()
        return normalized or None

    def _sample_label_market(self, *, market: str | None, ticker: str) -> str:
        """Resolve the market used to build executable labels for one ticker.

        An explicit CN/US/HK market always wins; empty, ``ALL``, ``MIXED`` or
        any other non-market code falls back to the repository's canonical
        ticker-suffix rule (:func:`infer_market_from_ticker`) so a label is
        never built against an ambiguous market.
        """

        normalized = self._normalize_market_code(market)
        if normalized in {"CN", "US", "HK"}:
            return normalized
        return infer_market_from_ticker(ticker)

    def _resolve_run_market(self, *, market: str | None, rows: list[dict]) -> str:
        """Resolve the single concrete market a persisted run belongs to.

        Physical/hot prediction storage rejects ambiguous markets
        (``""``/``MIXED``), so when the caller does not pin CN/US/HK we fall
        back to the tickers under training. Labels are still resolved
        per symbol; this only supplies the run-level storage market.
        """

        normalized = self._normalize_market_code(market)
        if normalized in {"CN", "US", "HK"}:
            return normalized
        tickers = {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
        if tickers:
            counts = Counter(infer_market_from_ticker(ticker) for ticker in tickers)
            return counts.most_common(1)[0][0]
        return "US"

    def _filter_rows_by_market(self, rows: list[dict], *, market: str | None) -> list[dict]:
        normalized_market = self._normalize_market_code(market)
        if normalized_market in {None, "", "ALL"}:
            return rows
        tickers = {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
        if not tickers:
            return rows
        with SessionLocal() as db:
            symbol_overviews = SymbolRepository(db).list_overviews_for_tickers(sorted(tickers))
        market_by_ticker = {
            str(ticker or "").strip().upper(): self._normalize_market_code((overview or {}).get("market"))
            for ticker, overview in symbol_overviews.items()
        }
        filtered = [
            row
            for row in rows
            if market_by_ticker.get(str(row.get("symbol") or "").strip().upper()) == normalized_market
        ]
        return filtered or rows

    def _load_rows(self, *, tickers: set[str] | None = None, market: str | None = None) -> list[dict]:
        rows = load_lake_rows(tickers=tickers)
        rows = self._filter_rows_by_market(rows, market=market)
        # The universe rules only mark signal days; the returned row set is the
        # complete (market-filtered) timeline used for features/entry/labels.
        rows, self._universe_filter_stats = self._apply_pit_universe_filter(rows, market=market)
        rows.sort(key=lambda row: (row.get("symbol") or "", row.get("date") or ""))
        self._attach_adjusted_basis(rows, market=market)
        return rows

    @staticmethod
    def _linear_quantile(sorted_values: list[float], quantile: float) -> float:
        """Deterministic linear-interpolation quantile (numpy ``linear``)."""

        if not sorted_values:
            raise ValueError("quantile of an empty sample")
        if len(sorted_values) == 1:
            return float(sorted_values[0])
        position = quantile * (len(sorted_values) - 1)
        lower_index = int(math.floor(position))
        upper_index = min(lower_index + 1, len(sorted_values) - 1)
        fraction = position - lower_index
        return float(
            sorted_values[lower_index]
            + (sorted_values[upper_index] - sorted_values[lower_index]) * fraction
        )

    def _apply_pit_universe_filter(
        self, rows: list[dict], *, market: str | None
    ) -> tuple[list[dict], dict]:
        """Gate signal days through the point-in-time tradable-universe rules.

        Thresholds are reused from
        ``stock_selection.universe.default_universe_rules``. Every decision for
        a symbol on date D reads only that symbol's rows up to and including D,
        so the filter borrows no future liquidity or history. The signal-day
        limit-up rule reuses the same band-relative threshold as the label-side
        entry check; it removes the *signal-day close lock* (a name that cannot
        be entered the next session), while the existing executable-label path
        keeps its own next-open unbuyable check, so the two never double-count
        the same exclusion.

        The rule only decides whether a symbol's signal day D may **produce a
        sample**. It never deletes a market row: entry, hold period and label
        construction must read the real consecutive-session timeline, so a
        rejected day D is marked (``pit_universe_allowed=False``) and the
        sample builder skips it while still walking the full date axis. Row
        deletion here used to shift "next-session entry" onto the next surviving
        row and stretch the fixed hold horizon into a variable one.

        The history rule is overridden by
        ``trainer_universe_min_history_sessions`` (0 = trainer ignores it),
        because the lake slice fed to training is much shorter than the shared
        universe warm-up; the effective value is recorded in ``stats``.
        """

        enabled = bool(getattr(self.settings, "trainer_universe_filter_enabled", True))
        stats: dict = {
            "enabled": enabled,
            "applied": False,
            "input_rows": len(rows),
            "retained_rows": len(rows),
            "output_rows": len(rows),
            "excluded_rows": 0,
            "row_deletion": False,
            "gate_semantics": "sample_gate_full_timeline_retained",
            "exclusion_counts": {},
        }
        if not enabled:
            stats["skipped_reason"] = "trainer_universe_filter_enabled=false"
            for row in rows:
                row.pop("pit_universe_allowed", None)
                row.pop("pit_universe_reasons", None)
            return list(rows), stats
        if not rows:
            stats["skipped_reason"] = "no_rows"
            return rows, stats
        market_code = self._normalize_market_code(market)
        if market_code not in {"CN", "US"}:
            inferred = Counter(
                infer_market_from_ticker(str(row.get("symbol") or "").strip().upper())
                for row in rows
                if str(row.get("symbol") or "").strip()
            )
            market_code = inferred.most_common(1)[0][0] if inferred else None
        if market_code not in {"CN", "US"}:
            stats["skipped_reason"] = f"unsupported_market:{market_code or 'unknown'}"
            for row in rows:
                row.pop("pit_universe_allowed", None)
                row.pop("pit_universe_reasons", None)
            return list(rows), stats
        rules = default_universe_rules(market_code)
        # 1a (2026-10-08): the training-side history rule is switchable. The
        # lake slice fed to the trainer is short (CN full-market coverage starts
        # 2025-02-14), so the shared 120-session warm-up prunes the head of the
        # window and starves the mature-feature-date gate. The liquidity and
        # signal-day limit-up rules stay in force either way.
        effective_min_history = max(
            0, int(getattr(self.settings, "trainer_universe_min_history_sessions", 0) or 0)
        )
        stats["rule_market"] = market_code
        stats["rules"] = {
            "min_price": rules.min_price,
            "min_adv20": rules.min_adv20,
            "min_history_sessions": effective_min_history,
            "adv_lookback_sessions": rules.adv_lookback_sessions,
            "exclude_st": rules.exclude_st,
            "exclude_suspended": rules.exclude_suspended,
            "exclude_signal_day_limit_up": rules.exclude_signal_day_limit_up,
        }
        stats["history_rule"] = {
            "enabled": effective_min_history > 0,
            "effective_min_history_sessions": effective_min_history,
            "universe_default_min_history_sessions": rules.min_history_sessions,
        }
        # The lake row schema carries no ST / suspension state; record the two
        # rules that therefore could not be evaluated instead of implying they
        # ran.
        stats["unapplied_rules"] = ["st_security", "suspended"]

        grouped: dict[str, list[dict]] = defaultdict(list)
        missing_symbol = 0
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                missing_symbol += 1
                continue
            grouped[symbol].append(row)

        exclusion_counter: Counter[str] = Counter()
        if missing_symbol:
            exclusion_counter["missing_symbol"] = missing_symbol
        admitted = 0
        for row in rows:
            if not str(row.get("symbol") or "").strip():
                row["pit_universe_allowed"] = False
                row["pit_universe_reasons"] = ("missing_symbol",)
        for symbol in sorted(grouped):
            symbol_rows = sorted(grouped[symbol], key=lambda item: str(item.get("date") or ""))
            closes = [self._safe_float(item.get("close")) for item in symbol_rows]
            volumes = [self._safe_float(item.get("volume")) for item in symbol_rows]
            # Valid history mirrors universe.py: only positive closes with a
            # non-negative volume contribute to session count and ADV.
            valid_dollar: list[float] = []
            valid_positions: list[int] = []
            for index, item in enumerate(symbol_rows):
                raw_volume = item.get("volume")
                if (
                    closes[index] is not None
                    and closes[index] > 0
                    and raw_volume not in (None, "")
                    and volumes[index] >= 0
                ):
                    valid_dollar.append(closes[index] * volumes[index])
                    valid_positions.append(index)
            limit_band = (
                self._resolve_cn_limit_band_pct(ticker=symbol, exchange=None, name=None)
                if market_code == "CN" and rules.exclude_signal_day_limit_up
                else None
            )
            for index, item in enumerate(symbol_rows):
                reasons: list[str] = []
                close = closes[index]
                if close is None or close <= 0:
                    reasons.append("invalid_price")
                elif close < rules.min_price:
                    reasons.append("low_price")
                raw_volume = item.get("volume")
                if raw_volume in (None, ""):
                    reasons.append("invalid_volume")
                history_sessions = bisect_right(valid_positions, index)
                if effective_min_history > 0 and history_sessions < effective_min_history:
                    reasons.append("insufficient_history")
                lookback = valid_dollar[
                    max(0, history_sessions - rules.adv_lookback_sessions):history_sessions
                ]
                adv20 = (sum(lookback) / len(lookback)) if lookback else 0.0
                if adv20 < rules.min_adv20:
                    reasons.append("low_adv20")
                if (
                    limit_band is not None
                    and index > 0
                    and close is not None
                    and close > 0
                    and closes[index - 1] is not None
                    and closes[index - 1] > 0
                    and (close / closes[index - 1]) - 1.0 >= (limit_band / 100.0) - 0.002
                ):
                    reasons.append("signal_day_limit_up_locked")
                # The row stays in the timeline either way; only its eligibility
                # as a signal day is recorded.
                if reasons:
                    for reason in reasons:
                        exclusion_counter[reason] += 1
                    item["pit_universe_allowed"] = False
                    item["pit_universe_reasons"] = tuple(reasons)
                    continue
                item["pit_universe_allowed"] = True
                item.pop("pit_universe_reasons", None)
                admitted += 1
        stats.update(
            {
                "applied": True,
                "retained_rows": len(rows),
                "output_rows": admitted,
                "excluded_rows": len(rows) - admitted,
                "exclusion_counts": dict(sorted(exclusion_counter.items())),
            }
        )
        return list(rows), stats

    def _attach_adjusted_basis(self, rows: list[dict], *, market: str | None) -> int:
        """Attach the versioned adjusted view onto rows for label construction (A1).

        Fields are namespaced (``adjusted_open``…): the legacy lake ``adj_close``
        column is NOT trustworthy (F1), so labels must read the rebuilt view.
        Raw ``open/high/low/close`` stay untouched for features and exchange
        limit checks.

        Every row is stamped with an explicit ``adjusted_view_attached`` flag so
        label construction can verify *per window* that all of its price points
        share one basis. The run-level ``_label_basis`` is only
        ``"adjusted_view"`` when the attach is complete for this row set;
        partial attaches are reported as ``"mixed"`` instead of silently
        claiming an adjusted run.
        """

        normalized = str(market or "").strip().upper()
        self._adjusted_basis_expected = False
        self._adjusted_view_state = "absent"
        self._adjusted_view_error = None
        self._adjusted_view_market = None
        self._adjusted_view_probe = None
        if normalized not in {"CN", "US"} or not rows:
            self._label_basis = "raw"
            return 0
        self._adjusted_basis_expected = True
        self._adjusted_view_market = normalized
        symbols = {str(row.get("symbol") or "").strip().upper() for row in rows}

        # Three-state view detection (fail closed) via the shared price-basis
        # contract. The attach itself never raises so prediction-only paths keep
        # degrading gracefully, but an ``unreadable`` view is recorded with its
        # path/reason and the run contract refuses to persist it rather than
        # treating it as "no view".
        adjusted, probe = read_adjusted_view_with_probe(
            normalized, symbols=symbols, rows=rows
        )
        self._adjusted_view_probe = probe
        self._adjusted_view_state = probe.state
        self._adjusted_view_error = probe.error
        attached = 0
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            trade_date = str(row.get("date") or "")[:10]
            bars = adjusted.get(symbol)
            payload = bars.get(trade_date) if bars else None
            usable = bool(payload) and all(
                isinstance(payload.get(field), (int, float))
                and math.isfinite(float(payload[field]))
                and float(payload[field]) > 0
                for field in ("open", "high", "low", "close")
            )
            if not usable:
                # Never leave a stale adjusted price from a previous attach: a
                # missing or unusable row must be unresolvable as adjusted
                # downstream, otherwise `_label_price` would silently mix a raw
                # fallback into a window we already accepted as adjusted.
                for field in ("open", "high", "low", "close"):
                    row.pop(f"adjusted_{field}", None)
                row["adjusted_view_attached"] = False
                continue
            row["adjusted_open"] = float(payload["open"])
            row["adjusted_high"] = float(payload["high"])
            row["adjusted_low"] = float(payload["low"])
            row["adjusted_close"] = float(payload["close"])
            row["adjusted_view_attached"] = True
            attached += 1
        if attached and attached == len(rows):
            self._label_basis = "adjusted_view"
        elif attached == 0:
            self._label_basis = "raw"
        else:
            self._label_basis = "mixed"
        return attached

    def _resolve_sample_price_basis(
        self,
        *,
        symbol_rows: list[dict],
        index: int,
        label_profile: str,
        horizon_days: int,
    ) -> str | None:
        """Classify one sample's label-window price basis.

        Returns ``None`` when basis tracking does not apply (non CN/US row sets,
        or the raw-basis reconciled profile), otherwise one of:

        * ``"adjusted"`` — every price point of the window is adjusted.
        * ``"raw_fallback"`` — no price point is adjusted; the whole window is
          consistently raw, so the sample can still be labeled (and counted) on
          raw prices while the run is reported as mixed.
        * ``"drop_missing_adjusted"`` — the window mixes adjusted and raw points.
          Mixed windows are never labeled: the sample is counted and its label
          dropped (fail closed).
        """

        if not getattr(self, "_adjusted_basis_expected", False):
            return None
        if label_profile not in LABEL_PRICE_TRACKED_PROFILES:
            return None
        span = horizon_days if label_profile == EXECUTABLE_LABEL_PROFILE else 5
        window = symbol_rows[index : index + span + 1]
        flags = [bool(row.get("adjusted_view_attached")) for row in window]
        if not flags or all(flags):
            return "adjusted"
        if not any(flags):
            return "raw_fallback"
        return "drop_missing_adjusted"

    def _label_price_basis_contract(
        self,
        *,
        label_profile: str,
        require_full: bool | None = None,
    ) -> dict[str, object]:
        """Resolve the truthful run-level label price basis and enforce the gate.

        ``adjusted_coverage_share`` is measured over the samples whose labels
        were actually built (adjusted + raw fallback + dropped), never over the
        raw lake width, so the gate follows label quality rather than an
        unrelated row count.

        The gate fires in three fail-closed situations for an applicable
        (CN/US, price-tracked-profile) run:

        * the view file is ``unreadable`` — always raise, never treat a corrupt
          view as "no view";
        * the view is ``absent`` and raw fallback was not explicitly opted into
          via ``PQW_TRAINER_ALLOW_RAW_FALLBACK=true`` — raise, so a missing view
          cannot silently produce raw labels;
        * the view is ``present`` but does not fully cover the label windows —
          raise (unless full coverage was explicitly waived).

        Only an explicitly opted-in absent view is persisted: it is reported as
        ``mixed:0.00000000`` (never ``adjusted_view``) with
        ``raw_fallback_allowed=true``. The opt-in is additionally recorded as a
        structured ``raw_fallback_optin_audit`` object (operator / decided_at /
        scope / reason); an opt-in that carries no reason is refused fail-closed
        so a waiver can never be persisted without an audit trail.
        """

        stats = getattr(self, "_label_price_stats", None) or _empty_label_price_stats()
        adjusted_count = int(stats.get("adjusted_count") or 0)
        raw_fallback_count = int(stats.get("raw_fallback_count") or 0)
        dropped_missing_adjusted_count = int(stats.get("dropped_missing_adjusted_count") or 0)
        candidate_count = adjusted_count + raw_fallback_count + dropped_missing_adjusted_count
        adjusted_coverage_share = (
            adjusted_count / candidate_count if candidate_count else 1.0
        )
        applicable = bool(getattr(self, "_adjusted_basis_expected", False)) and str(
            label_profile or ""
        ).strip().lower() in LABEL_PRICE_TRACKED_PROFILES
        view_state = str(getattr(self, "_adjusted_view_state", "absent") or "absent")
        view_present = view_state == "present"
        view_market = getattr(self, "_adjusted_view_market", None)
        allow_raw_fallback = bool(
            getattr(self.settings, "trainer_allow_raw_fallback", False)
        )
        if require_full is None:
            require_full = bool(
                getattr(self.settings, "trainer_require_full_adjusted_coverage", True)
            )
        # Build the probe from live detector state. Production rows come from
        # ``_attach_adjusted_basis`` (which stores the shared probe), while unit
        # callers may set ``_adjusted_view_state`` directly; both feed the same
        # decision table so train / inference / backtest cannot drift apart.
        probe = getattr(self, "_adjusted_view_probe", None)
        expected_market = str(view_market).strip().upper() if view_market else None
        if probe is None or probe.state != view_state or probe.market != expected_market:
            probe = AdjustedViewProbe(
                market=expected_market,
                state=view_state,
                error=getattr(self, "_adjusted_view_error", None),
                coverage_share=adjusted_coverage_share,
            )
        requirements = PriceBasisRequirements(
            entry_point=ENTRY_TRAIN,
            requires_adjusted_prices=applicable,
            require_full_coverage=bool(require_full),
            allow_raw_fallback=allow_raw_fallback,
            adjusted_count=adjusted_count,
            raw_fallback_count=raw_fallback_count,
            dropped_missing_adjusted_count=dropped_missing_adjusted_count,
            gates_coverage=True,
        )
        decision = decide_price_basis(ENTRY_TRAIN, probe, requirements)
        self._adjusted_view_probe = probe
        self._price_basis_decision = decision
        self._price_basis_requirements = requirements
        contract: dict[str, object] = {
            "label_price_basis": decision.label_price_basis,
            "label_price_basis_applicable": decision.applicable,
            "adjusted_view_present": view_present,
            "adjusted_view_state": decision.view_state,
            "raw_fallback_allowed": allow_raw_fallback,
            "adjusted_count": adjusted_count,
            "raw_fallback_count": raw_fallback_count,
            "dropped_missing_adjusted_count": dropped_missing_adjusted_count,
            "adjusted_coverage_share": round(decision.coverage_share, 8),
            "require_full_adjusted_coverage": bool(require_full),
        }
        if decision.decision != DECISION_REJECT:
            # Structured, accountable opt-in record. Enabling the raw-label
            # opt-in always requires a reason (same fail-closed rule as the
            # runner's unmodeled corporate-action opt-in): an enabled opt-in
            # without one raises MissingOptinReasonError here *before* the run is
            # persisted. Whether the waiver is actually exercised on this run is
            # deliberately irrelevant — a standing opt-in is still an operator
            # decision that must be attributable.
            contract["raw_fallback_optin_audit"] = build_optin_audit(
                enabled=allow_raw_fallback,
                source=RAW_FALLBACK_AUTHORIZATION_SOURCE,
                scope={
                    "entry_point": ENTRY_TRAIN,
                    "market": expected_market,
                    "label_profile": str(label_profile or "").strip().lower() or None,
                    "adjusted_view_state": decision.view_state,
                    "coverage_share": round(decision.coverage_share, 8),
                    "require_full_adjusted_coverage": bool(require_full),
                },
                operator=getattr(self.settings, "optin_operator", None),
                reason=getattr(self.settings, "optin_reason", None),
            )
            return contract
        if REASON_UNREADABLE_VIEW in decision.reasons:
            raise RuntimeError(
                "Trainer refused an adjusted-view label run: the adjusted view "
                f"exists but is unreadable for market={view_market} "
                f"(reason={getattr(self, '_adjusted_view_error', None)}). A corrupt "
                "or unreadable view is never treated as 'no view'. Rebuild it "
                "(scripts/rebuild_adjusted_view.py) before training."
            )
        if REASON_ADJUSTED_VIEW_ABSENT in decision.reasons:
            raise RuntimeError(
                "Trainer refused a CN/US label run without an adjusted view: no "
                f"adjusted view file exists for market={view_market}. Rebuild it "
                "(scripts/rebuild_adjusted_view.py), or explicitly opt in to raw "
                "labels with PQW_TRAINER_ALLOW_RAW_FALLBACK=true plus an auditable "
                "reason (PQW_OPTIN_REASON); the run is then recorded as "
                "mixed:0.00000000 with raw_fallback_allowed=true and a structured "
                "raw_fallback_optin_audit record."
            )
        # Incomplete coverage (or any future reject reason) fails closed with the
        # enumerable reason list so callers can explain the refusal.
        raise RuntimeError(
            "Trainer refused an adjusted-view label run with incomplete coverage: "
            f"adjusted_count={adjusted_count}, raw_fallback_count={raw_fallback_count}, "
            f"dropped_missing_adjusted_count={dropped_missing_adjusted_count}, "
            f"adjusted_coverage_share={adjusted_coverage_share:.8f}, "
            f"reasons={list(decision.reasons)}. "
            "Complete the adjusted view or set "
            "PQW_TRAINER_REQUIRE_FULL_ADJUSTED_COVERAGE=false to persist a "
            "truthfully-labelled mixed-basis run."
        )

    def _prediction_price_basis_contract(self) -> dict[str, object] | None:
        """Audit fields for the *inference* (prediction) product.

        The trainer both fits labels and scores recent dates from one view; the
        prediction product inherits the label basis. Reconciling the decision
        through the same table keeps published predictions from ever claiming an
        adjusted basis the run did not actually use.
        """

        decision = getattr(self, "_price_basis_decision", None)
        requirements = getattr(self, "_price_basis_requirements", None)
        probe = getattr(self, "_adjusted_view_probe", None)
        if decision is None or probe is None:
            return None
        if requirements is None:
            inference_requirements = PriceBasisRequirements(entry_point=ENTRY_INFERENCE)
        else:
            inference_requirements = PriceBasisRequirements(
                entry_point=ENTRY_INFERENCE,
                requires_adjusted_prices=requirements.requires_adjusted_prices,
                require_full_coverage=requirements.require_full_coverage,
                allow_raw_fallback=requirements.allow_raw_fallback,
                adjusted_count=requirements.adjusted_count,
                raw_fallback_count=requirements.raw_fallback_count,
                dropped_missing_adjusted_count=requirements.dropped_missing_adjusted_count,
                gates_coverage=True,
            )
        inference = decide_price_basis(ENTRY_INFERENCE, probe, inference_requirements)
        return inference.audit_fields()

    def _label_price(self, row: dict, field: str) -> float | None:
        """Label-side price: adjusted view when attached, else raw (A1)."""

        adjusted = self._safe_float(row.get(f"adjusted_{field}"))
        if adjusted is not None and adjusted > 0:
            return adjusted
        return self._safe_float(row.get(field))

    def _moving_average(self, values: list[float], window: int) -> float | None:
        if not values:
            return None
        sample = values[-window:] if len(values) >= window else values
        return sum(sample) / len(sample)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _safe_float(self, value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _stddev(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        try:
            return float(statistics.pstdev(values))
        except statistics.StatisticsError:
            return 0.0

    def _future_return(self, future_price: float, anchor_price: float) -> float | None:
        if future_price <= 0 or anchor_price <= 0:
            return None
        return (future_price / anchor_price) - 1.0

    def _future_path_metrics_20d(
        self, *, future_rows: list[dict], anchor_close: float
    ) -> dict[str, float | None]:
        """Additive twenty-session horizon labels, in percent.

        Anchored on the signal-day close (``anchor_close``) so the keys line up
        with the percent-scaled calibration metrics consumed by
        ``_build_detail_row``. The 20-day horizon is never derived from (nor fed
        into) the 1/3/5-day keys, and a path shorter than twenty sessions stays
        ``None`` so a short window cannot masquerade as a 20-day estimate.
        """
        window = future_rows[:20]
        if len(window) < 20 or anchor_close is None or anchor_close <= 0:
            return {"next_20d_close_return": None, "next_20d_max_drawdown": None}
        close_20d = self._label_price(window[-1], "close")
        lows = [value for value in (self._label_price(row, "low") for row in window) if value is not None]
        close_return = self._future_return(close_20d, anchor_close) if close_20d is not None else None
        drawdown = self._future_return(min(lows), anchor_close) if lows else None
        return {
            "next_20d_close_return": round(close_return * 100.0, 2) if close_return is not None else None,
            "next_20d_max_drawdown": round(drawdown * 100.0, 2) if drawdown is not None else None,
        }

    def _parse_iso_date(self, value: object) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _listing_days(self, *, trade_date: str, listing_date: str | None) -> int | None:
        trade_day = self._parse_iso_date(trade_date)
        listing_day = self._parse_iso_date(listing_date)
        if trade_day is None or listing_day is None:
            return None
        return max(0, (trade_day - listing_day).days)

    def _board_tier(self, *, ticker: str, exchange: str | None) -> float:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        if normalized_ticker.startswith(("300", "301")):
            return 1.0
        if normalized_ticker.startswith(("688", "689")) or normalized_exchange in {"STAR", "SSE STAR"}:
            return 1.0
        if normalized_ticker.endswith(".BJ") or normalized_exchange in {"BSE", "BJ"}:
            return 1.25
        return 0.0

    def _resolve_cn_limit_band_pct(self, *, ticker: str, exchange: str | None, name: str | None) -> float | None:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        normalized_name = str(name or "").strip().upper().replace(" ", "")
        if not normalized_ticker:
            return None
        code = normalized_ticker.split(".", 1)[0]
        if normalized_name.startswith(("ST", "*ST", "S*ST", "PT")):
            return 5.0
        if normalized_ticker.endswith(".BJ") or normalized_exchange in {"BSE", "BJ"}:
            return 30.0
        if code.startswith(("688", "689")) or normalized_exchange in {"STAR", "SSE STAR"}:
            return 20.0
        if code.startswith(("300", "301")):
            return 20.0
        if normalized_ticker.endswith((".SS", ".SZ", ".SH")):
            return 10.0
        return None

    def _load_symbol_feature_context(
        self,
        *,
        rows: list[dict],
        market: str | None,
        normalized_tickers: set[str] | None,
    ) -> dict[str, dict]:
        tickers = normalized_tickers or {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
        if not tickers:
            return {}
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            symbol_overviews = symbol_repo.list_overviews_for_tickers(sorted(tickers))
            latest_fundamentals = {
                str(item.get("ticker") or "").strip().upper(): item
                for item in FundamentalSnapshotRepository(db).list_latest_for_market(market, tickers=sorted(tickers))
            }
            # Training features come from the append-only point-in-time store:
            # fiscal period end is not a knowledge date. The wide snapshot
            # repository is only used above for non-training metadata
            # (name / listing date).
            pit_history_rows = PointInTimeFeatureSnapshotRepository(db).list_history_for_market(
                market,
                tickers=sorted(tickers),
                feature_names=list(TRAINER_FUNDAMENTAL_FEATURES),
            )
            concept_history_rows = ConceptSnapshotRepository(db).list_history_for_market(market, tickers=sorted(tickers))
        fundamentals_by_ticker = group_point_in_time_fundamental_history(pit_history_rows)
        concepts_by_ticker_date: dict[str, dict[str, dict]] = defaultdict(dict)
        for item in concept_history_rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            as_of_date = str(item.get("as_of_date") or "").strip()
            if not ticker or not as_of_date:
                continue
            bucket = concepts_by_ticker_date[ticker].setdefault(
                as_of_date,
                {"concept_count": 0.0, "max_strength": 0.0},
            )
            bucket["concept_count"] = float(bucket.get("concept_count") or 0.0) + 1.0
            strength = self._safe_float(item.get("strength"))
            if strength > float(bucket.get("max_strength") or 0.0):
                bucket["max_strength"] = strength
        context: dict[str, dict] = {}
        for ticker in sorted(tickers):
            symbol_meta = symbol_overviews.get(ticker) or {}
            fundamentals = latest_fundamentals.get(ticker) or {}
            concept_timeline = [
                {"as_of_date": as_of_date, **payload}
                for as_of_date, payload in sorted((concepts_by_ticker_date.get(ticker) or {}).items())
            ]
            context[ticker] = {
                "name": symbol_meta.get("name"),
                "exchange": symbol_meta.get("exchange"),
                "sector": symbol_meta.get("sector"),
                "industry": symbol_meta.get("industry"),
                "listing_date": fundamentals.get("listing_date"),
                "fundamental_history": fundamentals_by_ticker.get(ticker) or [],
                "concept_history": concept_timeline,
                "board_tier": self._board_tier(
                    ticker=ticker,
                    exchange=symbol_meta.get("exchange"),
                ),
                "limit_band_pct": self._resolve_cn_limit_band_pct(
                    ticker=ticker,
                    exchange=symbol_meta.get("exchange"),
                    name=symbol_meta.get("name"),
                ),
            }
        return context

    def _advance_fundamental_cursor(
        self,
        *,
        history: list[dict],
        cursor: int,
        trade_date: str,
    ) -> tuple[int, dict | None]:
        """Advance by publication availability, never by fiscal period end.

        History is ordered by ``available_time``. A report whose last feature
        had not been published on ``trade_date`` must not be visible to the
        sample. Rows without an availability timestamp are skipped rather than
        assumed known at period end (fail closed).
        """

        active: dict | None = None
        index = cursor
        while index < len(history):
            item = history[index]
            available_time = str(item.get("available_time") or "").strip()
            if not available_time:
                index += 1
                continue
            if available_time[:10] <= trade_date:
                active = item
                index += 1
                continue
            break
        if active is None and cursor > 0:
            previous = history[cursor - 1]
            previous_available = str(previous.get("available_time") or "").strip()
            if previous_available and previous_available[:10] <= trade_date:
                # Between two publications the last published report stays in
                # force. Timestamp-less rows are never used as fallback.
                active = previous
        return index, active

    def _advance_concept_cursor(
        self,
        *,
        history: list[dict],
        cursor: int,
        trade_date: str,
    ) -> tuple[int, dict | None]:
        active: dict | None = None
        index = cursor
        while index < len(history):
            as_of_date = str(history[index].get("as_of_date") or "").strip()
            if as_of_date and as_of_date <= trade_date:
                active = history[index]
                index += 1
                continue
            break
        if active is None and index > 0:
            active = history[index - 1]
        return index, active

    def _build_short_horizon_target_profile(
        self,
        *,
        symbol_rows: list[dict],
        index: int,
        anchor_close: float,
        limit_band_pct: float | None = None,
    ) -> tuple[float | None, dict[str, float]]:
        if anchor_close <= 0:
            return None, {}

        future_rows = symbol_rows[index + 1 :]
        if len(future_rows) < 3:
            return None, {}

        next_row = future_rows[0]
        next_open = self._label_price(next_row, "open")
        next_high = self._label_price(next_row, "high")
        next_low = self._label_price(next_row, "low")
        next_close = self._label_price(next_row, "close")

        future_3d = future_rows[:3]
        future_5d = future_rows[:5]
        future_3d_highs = [self._label_price(row, "high") for row in future_3d]
        future_3d_lows = [self._label_price(row, "low") for row in future_3d]
        future_5d_highs = [self._label_price(row, "high") for row in future_5d]
        future_5d_lows = [self._label_price(row, "low") for row in future_5d]

        next_1d_close_return = self._future_return(next_close, anchor_close)
        next_1d_open_gap = self._future_return(next_open, anchor_close)
        next_1d_open_to_high = self._future_return(next_high, next_open) if next_open > 0 else None
        next_1d_open_to_close = self._future_return(next_close, next_open) if next_open > 0 else None
        next_1d_low_drawdown = self._future_return(next_low, anchor_close)
        next_3d_max_return = self._future_return(max(future_3d_highs), anchor_close)
        next_3d_max_drawdown = self._future_return(min(future_3d_lows), anchor_close)
        next_5d_max_return = self._future_return(max(future_5d_highs), anchor_close) if len(future_5d) >= 5 else None
        next_5d_max_drawdown = self._future_return(min(future_5d_lows), anchor_close) if len(future_5d) >= 5 else None
        next_5d_close_return = (
            self._future_return(self._label_price(future_5d[-1], "close"), anchor_close) if len(future_5d) >= 5 else None
        )
        twenty_day_metrics = self._future_path_metrics_20d(
            future_rows=future_rows, anchor_close=anchor_close
        )
        next_20d_close_return = twenty_day_metrics["next_20d_close_return"]
        next_20d_max_drawdown = twenty_day_metrics["next_20d_max_drawdown"]

        failed_after_gap_up = 0.0
        if (
            next_1d_open_gap is not None
            and next_1d_open_gap >= 0.025
            and next_1d_open_to_close is not None
            and next_1d_open_to_close <= -0.02
        ):
            failed_after_gap_up = 1.0

        tradable_next_day = 1.0
        cn_limit_threshold = ((limit_band_pct - 0.2) / 100.0) if limit_band_pct and limit_band_pct > 0 else None
        if next_1d_open_gap is not None and cn_limit_threshold is not None and next_1d_open_gap >= cn_limit_threshold:
            tradable_next_day = 0.0
        elif next_1d_open_gap is not None and next_1d_open_gap >= 0.095:
            tradable_next_day = 0.0

        upside = (
            max(next_1d_close_return or 0.0, -0.12) * 0.20
            + max(next_1d_open_to_high or 0.0, 0.0) * 0.30
            + max(next_3d_max_return or 0.0, 0.0) * 0.35
            + max(next_5d_max_return or 0.0, 0.0) * 0.15
        )
        downside = (
            abs(min(next_1d_low_drawdown or 0.0, 0.0)) * 0.18
            + abs(min(next_3d_max_drawdown or 0.0, 0.0)) * 0.32
            + abs(min(next_5d_max_drawdown or 0.0, 0.0)) * 0.18
        )
        penalty = failed_after_gap_up * 0.05 + (0.04 if tradable_next_day < 0.5 else 0.0)
        composite_target = self._clamp(upside - downside - penalty, -0.35, 0.45)

        target_profile = {
            "next_1d_close_return": round((next_1d_close_return or 0.0) * 100.0, 2),
            "next_1d_open_gap": round((next_1d_open_gap or 0.0) * 100.0, 2),
            "next_1d_open_to_high": round((next_1d_open_to_high or 0.0) * 100.0, 2),
            "next_1d_open_to_close": round((next_1d_open_to_close or 0.0) * 100.0, 2),
            "next_3d_max_return": round((next_3d_max_return or 0.0) * 100.0, 2),
            "next_3d_max_drawdown": round((next_3d_max_drawdown or 0.0) * 100.0, 2),
            "next_5d_max_return": round((next_5d_max_return or 0.0) * 100.0, 2),
            "next_5d_max_drawdown": round((next_5d_max_drawdown or 0.0) * 100.0, 2),
            "next_5d_close_return": round((next_5d_close_return or 0.0) * 100.0, 2),
            "next_20d_close_return": next_20d_close_return,
            "next_20d_max_drawdown": next_20d_max_drawdown,
            "failed_after_gap_up": failed_after_gap_up,
            "tradable_next_day": tradable_next_day,
            "next_day_limit_band_pct": round(limit_band_pct or 0.0, 2),
            "composite_target": round(composite_target * 100.0, 2),
        }
        return composite_target, target_profile

    def _bar_date_value(self, row: dict) -> date | None:
        raw = str(row.get("date") or row.get("trade_date") or "").strip()
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def _build_executable_net_return_target(
        self,
        *,
        symbol_rows: list[dict],
        index: int,
        horizon_days: int,
        market: str,
        limit_band_pct: float | None,
    ) -> tuple[float | None, dict, str]:
        """P0 production label: confirmed next-open entry, fixed-horizon exit.

        Replaces the momentum-chasing legacy composite with the net return a
        trader could actually capture: T+1 entry eligibility (signal-day
        limit-up opens are excluded as unbuyable), an executable exit bar,
        and explicit per-fill commission plus slippage costs.
        """
        version = "confirmed_next_open_fixed_exit_fill_cost_v2"
        policy = str(
            getattr(self.settings, "trainer_cn_entry_not_executable_policy", "") or "exclude"
        ).strip().lower()
        if policy not in {"exclude", "keep"}:
            raise RuntimeError("trainer_cn_entry_not_executable_policy must be 'exclude' or 'keep'")
        commission_bps = float(getattr(self.settings, "trainer_cn_execution_commission_bps", 2.5) or 0.0)
        slippage_bps = float(getattr(self.settings, "trainer_cn_execution_slippage_bps", 15.0) or 0.0)
        cost_model = FillCostModel(commission_bps, slippage_bps)
        drawdown_penalty = float(getattr(self.settings, "trainer_drawdown_penalty", 0.25) or 0.0)

        window_rows = symbol_rows[index : index + horizon_days + 1]
        bars: list[PriceBar] = []
        for row in window_rows:
            bar_date = self._bar_date_value(row)
            if bar_date is None:
                return None, {"label_profile": version, "exclusion_reason": "unparseable_bar_date"}, version
            try:
                bars.append(
                    PriceBar(
                        bar_date,
                        (self._label_price(row, "open") or 0.0),
                        (self._label_price(row, "high") or 0.0),
                        (self._label_price(row, "low") or 0.0),
                        (self._label_price(row, "close") or 0.0),
                        max(self._safe_float(row.get("volume")) or 0.0, 0.0),
                    )
                )
            except ValueError:
                # A provider glitch (zero/negative/non-finite quote) must not
                # kill the whole training run: the sample simply stays
                # unlabeled and the dirty bar is surfaced through the same
                # auditable exclusion channel as unparseable dates.
                return None, {"label_profile": version, "exclusion_reason": "invalid_bar_prices"}, version
        profile = {
            "label_profile": version,
            "commission_bps_one_way": commission_bps,
            "slippage_bps_one_way": slippage_bps,
            "cost_model_hash": cost_model.model_hash,
            "drawdown_penalty": drawdown_penalty,
            # Additive 20-session horizon (percent) for the published
            # expected_return_20d / expected_drawdown_20d estimates. This is
            # independent of the label's own fixed-horizon net return.
            **self._future_path_metrics_20d(
                future_rows=symbol_rows[index + 1 : index + 21],
                anchor_close=self._label_price(symbol_rows[index], "close") or 0.0,
            ),
        }
        previous_close = bars[0].close
        next_open = bars[1].open
        if previous_close <= 0 or next_open <= 0:
            profile["exclusion_reason"] = "invalid_entry_prices"
            return None, profile, version
        limit_ratio = (limit_band_pct / 100.0) if limit_band_pct and limit_band_pct > 0 else None
        # Mirror the legacy 0.2pp band tolerance below the nominal limit.
        entry_threshold = (limit_ratio - 0.002) if limit_ratio is not None else None
        entry_allowed = not limit_up_at_open(next_open, previous_close, entry_threshold)
        eligibility = ExecutionEligibility(
            entry_allowed,
            True,
            reason=None if entry_allowed else "entry_not_executable_signal_day_limit_up",
        )
        try:
            outcome = confirmed_outcome(
                bars,
                signal_date=bars[0].trade_date,
                trading_dates=[bar.trade_date for bar in bars],
                horizon_days=horizon_days,
                market=market,
                eligibility=eligibility,
                cost_model=cost_model,
                drawdown_penalty=drawdown_penalty,
            )
        except (ValueError, TypeError):
            profile["exclusion_reason"] = "executable_label_input_error"
            return None, profile, version
        if outcome is None:
            profile["exclusion_reason"] = "label_window_immature"
            return None, profile, version
        reason = getattr(outcome, "exclusion_reason", None)
        gross = getattr(outcome, "gross_return", None)
        net = getattr(outcome, "net_return", None)
        if reason:
            profile["exclusion_reason"] = str(reason)
        if gross is not None:
            profile["gross_return"] = float(gross)
        if net is not None:
            profile["net_return"] = float(net)
        # Persist the label's own reference-adjusted returns so run-level OOS
        # evidence can cite them directly instead of re-deriving a metric.
        for return_key in (
            "market_excess_return",
            "industry_excess_return",
            "risk_adjusted_return",
            "path_drawdown",
        ):
            value = getattr(outcome, return_key, None)
            if value is not None:
                profile[return_key] = float(value)
        if not getattr(outcome, "tradable", True) and str(reason or "").startswith("entry"):
            profile["entry_not_executable"] = True
            if policy == "exclude":
                return None, profile, version
        if net is None:
            return None, profile, version
        return float(net), profile, version

    def reconciled_training_target(self, *, symbol_rows, index, horizon_days, market, ticker):
        cost = FillCostModel(
            float(getattr(self.settings, 'trainer_cn_execution_commission_bps', 2.5)),
            float(getattr(self.settings, 'trainer_cn_execution_slippage_bps', 15.0)))
        result = replay_candidate(ticker=ticker, market=market,
            signal_date=str(symbol_rows[index]['date']), horizon_days=horizon_days,
            rows=symbol_rows[index:], contract=execution_contract(market, cost))
        result['exclusion_reason'] = result['reason']
        result['label_profile'] = RECONCILED_VERSION
        return result['net_return'], result, RECONCILED_VERSION

    def _feature_names(self, *, lookback_days: int) -> list[str]:
        return [
            "recent_daily_return",
            "open_gap_pct",
            f"lookback_momentum_{lookback_days}d",
            "price_vs_ma20",
            "price_vs_ma10",
            "ma_alignment",
            "ma_stack",
            "ma20_slope_5d",
            "volume_ratio_20d",
            "volume_accel_3d",
            "dollar_volume_log",
            "dollar_volume_ratio_20d",
            "volatility_10d",
            "intraday_range_pct",
            "close_location_in_day_range",
            "close_location_in_20d_range",
            "swing_drawdown_5d",
            "breakout_gap_20d",
            "drawdown_from_20d_high",
            "market_cap_log",
            "roe_avg_3y_scaled",
            "net_profit_yoy_scaled",
            "revenue_yoy_scaled",
            "debt_to_assets_scaled",
            "concept_count_norm",
            "concept_strength_norm",
            "listing_days_log",
            "board_tier",
            "limit_up_count_20d_norm",
            "days_since_limit_up_norm",
            "limit_up_next_day_open_to_close_avg",
        ]

    def _feature_direction(self, feature_name: str) -> float:
        if feature_name in {
            "volatility_10d",
            "drawdown_from_20d_high",
            "swing_drawdown_5d",
            "debt_to_assets_scaled",
        }:
            return -1.0
        return 1.0

    def _build_lightgbm_samples(
        self,
        *,
        rows: list[dict],
        lookback_days: int,
        horizon_days: int,
        symbol_feature_context: dict[str, dict] | None = None,
        market: str = "CN",
    ) -> list[dict]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            trade_date = str(row.get("date") or "").strip()
            close = row.get("close")
            if not symbol or not trade_date or close in {None, ""}:
                continue
            grouped[symbol].append(row)

        samples: list[dict] = []
        self._label_price_stats = _empty_label_price_stats()
        self._universe_gated_signal_days = 0
        for symbol, symbol_rows in grouped.items():
            symbol_market = self._sample_label_market(market=market, ticker=symbol)
            context = (symbol_feature_context or {}).get(symbol) or {}
            fundamental_history = list(context.get("fundamental_history") or [])
            concept_history = list(context.get("concept_history") or [])
            fundamental_cursor = 0
            concept_cursor = 0
            symbol_rows.sort(key=lambda row: str(row.get("date") or ""))
            closes = [self._safe_float(row.get("close")) for row in symbol_rows]
            opens = [self._safe_float(row.get("open")) for row in symbol_rows]
            highs = [self._safe_float(row.get("high")) for row in symbol_rows]
            lows = [self._safe_float(row.get("low")) for row in symbol_rows]
            volumes = [self._safe_float(row.get("volume")) for row in symbol_rows]
            for index, row in enumerate(symbol_rows):
                if index < 1:
                    continue
                trade_date = str(row.get("date") or "").strip()
                fundamental_cursor, active_fundamental = self._advance_fundamental_cursor(
                    history=fundamental_history,
                    cursor=fundamental_cursor,
                    trade_date=trade_date,
                )
                concept_cursor, active_concept = self._advance_concept_cursor(
                    history=concept_history,
                    cursor=concept_cursor,
                    trade_date=trade_date,
                )
                # Point-in-time universe gate. The rule rejects a *signal day*,
                # never a market row: the full date axis above already supplied
                # the real previous/next sessions, so the entry (`index + 1`)
                # and the fixed hold horizon below stay on the true timeline.
                if row.get("pit_universe_allowed") is False:
                    self._universe_gated_signal_days += 1
                    continue
                close = closes[index]
                day_open = opens[index]
                day_high = highs[index]
                day_low = lows[index]
                previous_close = closes[index - 1]
                if close <= 0 or previous_close <= 0:
                    continue
                history_closes = closes[: index + 1]
                history_highs = highs[: index + 1]
                history_lows = lows[: index + 1]
                history_volumes = volumes[: index + 1]
                history_dollar_volumes = [
                    max(0.0, history_closes[pos] * history_volumes[pos]) for pos in range(len(history_closes))
                ]
                ma5 = self._moving_average(history_closes, 5)
                ma10 = self._moving_average(history_closes, 10)
                ma20 = self._moving_average(history_closes, 20)
                ma60 = self._moving_average(history_closes, 60)
                avg_volume_20 = self._moving_average(history_volumes, 20)
                avg_volume_3 = self._moving_average(history_volumes[:-1], 3) if len(history_volumes) > 1 else None
                avg_dollar_volume_20 = self._moving_average(history_dollar_volumes, 20)
                ma20_history = [
                    self._moving_average(history_closes[: pos + 1], 20)
                    for pos in range(len(history_closes))
                ]
                recent_returns = [
                    (history_closes[pos] / history_closes[pos - 1]) - 1.0
                    for pos in range(max(1, index - 9), index + 1)
                    if history_closes[pos - 1] > 0
                ]
                prior_window = history_closes[max(0, index - 20) : index]
                prior_high_20 = max(prior_window) if prior_window else previous_close
                recent_high_20 = max(history_highs[max(0, index - 19) : index + 1]) if history_highs else day_high
                recent_low_20 = min(history_lows[max(0, index - 19) : index + 1]) if history_lows else day_low
                recent_high_5 = max(history_highs[max(0, index - 4) : index + 1]) if history_highs else day_high
                lookback_anchor = history_closes[max(0, index - lookback_days)]
                lookback_momentum = ((close / lookback_anchor) - 1.0) if lookback_anchor > 0 else 0.0
                breakout_gap_20d = ((close / prior_high_20) - 1.0) if prior_high_20 > 0 else 0.0
                drawdown_from_20d_high = ((prior_high_20 / close) - 1.0) if prior_high_20 > 0 and close > 0 else 0.0
                volume_ratio_20d = (history_volumes[-1] / avg_volume_20) if avg_volume_20 and history_volumes[-1] > 0 else 1.0
                volume_accel_3d = (history_volumes[-1] / avg_volume_3) - 1.0 if avg_volume_3 and history_volumes[-1] > 0 else 0.0
                today_dollar_volume = max(0.0, close * history_volumes[-1])
                dollar_volume_ratio_20d = (
                    (today_dollar_volume / avg_dollar_volume_20) - 1.0
                    if avg_dollar_volume_20 and today_dollar_volume > 0
                    else 0.0
                )
                intraday_range_pct = ((day_high - day_low) / previous_close) if day_high > 0 and previous_close > 0 else 0.0
                close_location_in_day_range = (
                    ((close - day_low) / max(day_high - day_low, 1e-9)) - 0.5
                    if day_high > day_low
                    else 0.0
                )
                close_location_in_20d_range = (
                    ((close - recent_low_20) / max(recent_high_20 - recent_low_20, 1e-9)) - 0.5
                    if recent_high_20 > recent_low_20
                    else 0.0
                )
                ma20_reference = ma20_history[max(0, len(ma20_history) - 6)]
                ma20_slope_5d = ((ma20 / ma20_reference) - 1.0) if ma20 and ma20_reference else 0.0
                swing_drawdown_5d = ((close / recent_high_5) - 1.0) if recent_high_5 > 0 else 0.0
                listing_days = self._listing_days(
                    trade_date=trade_date,
                    listing_date=context.get("listing_date"),
                )
                listing_days_log = (
                    math.log1p(min(listing_days, 6000)) / 8.0
                    if listing_days is not None and listing_days > 0
                    else 0.0
                )
                market_cap = self._safe_float((active_fundamental or {}).get("market_cap"))
                roe_avg_3y = self._safe_float((active_fundamental or {}).get("roe_avg_3y"))
                net_profit_yoy = self._safe_float((active_fundamental or {}).get("net_profit_yoy"))
                revenue_yoy = self._safe_float((active_fundamental or {}).get("revenue_yoy"))
                debt_to_assets = self._safe_float((active_fundamental or {}).get("debt_to_assets"))
                concept_count = self._safe_float((active_concept or {}).get("concept_count"))
                concept_strength = self._safe_float((active_concept or {}).get("max_strength"))
                # P1 limit-up dynamics. Counts and recency use the trailing 20
                # sessions including the signal day itself (known at the close).
                # The next-day follow-through average, however, may only use
                # probes whose following session has already closed: a limit-up
                # on the signal day itself has no observable next session yet,
                # so including it would leak the label window into the feature.
                # Without a CN limit band (e.g. US runs) the factors stay
                # neutral 0.0.
                limit_band_for_factors = self._safe_float(context.get("limit_band_pct"), default=0.0) or 0.0
                limit_up_count_20d = 0
                days_since_limit_up = 20
                limit_up_next_day_moves: list[float] = []
                if limit_band_for_factors > 0:
                    limit_threshold = (limit_band_for_factors / 100.0) - 0.002
                    for probe in range(max(0, index - 19), index + 1):
                        probe_close = self._safe_float(symbol_rows[probe].get("close"))
                        prev_close = (
                            self._safe_float(symbol_rows[probe - 1].get("close")) if probe > 0 else None
                        )
                        if probe_close is None or probe_close <= 0 or prev_close is None or prev_close <= 0:
                            continue
                        if (probe_close / prev_close) - 1.0 >= limit_threshold:
                            limit_up_count_20d += 1
                            days_since_limit_up = index - probe
                            if probe >= index:
                                # Signal-day limit-up: its next session is part
                                # of the label window and must not enter features.
                                continue
                            next_row = symbol_rows[probe + 1] if probe + 1 < len(symbol_rows) else None
                            next_open = self._safe_float(next_row.get("open")) if next_row is not None else None
                            next_close = self._safe_float(next_row.get("close")) if next_row is not None else None
                            if next_open is not None and next_close is not None and next_open > 0:
                                limit_up_next_day_moves.append((next_close / next_open) - 1.0)
                limit_up_next_day_avg = (
                    sum(limit_up_next_day_moves) / len(limit_up_next_day_moves)
                    if limit_up_next_day_moves
                    else 0.0
                )
                sample = {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "features": {
                        "recent_daily_return": (close / previous_close) - 1.0,
                        "open_gap_pct": ((day_open / previous_close) - 1.0) if day_open > 0 else 0.0,
                        f"lookback_momentum_{lookback_days}d": lookback_momentum,
                        "price_vs_ma20": ((close / ma20) - 1.0) if ma20 else 0.0,
                        "price_vs_ma10": ((close / ma10) - 1.0) if ma10 else 0.0,
                        "ma_alignment": ((ma5 / ma20) - 1.0) if ma5 and ma20 else 0.0,
                        "ma_stack": ((ma20 / ma60) - 1.0) if ma20 and ma60 else 0.0,
                        "ma20_slope_5d": ma20_slope_5d,
                        "volume_ratio_20d": volume_ratio_20d - 1.0,
                        "volume_accel_3d": volume_accel_3d,
                        "dollar_volume_log": math.log10(today_dollar_volume + 1.0) / 10.0,
                        "dollar_volume_ratio_20d": dollar_volume_ratio_20d,
                        "volatility_10d": self._stddev(recent_returns),
                        "intraday_range_pct": intraday_range_pct,
                        "close_location_in_day_range": close_location_in_day_range,
                        "close_location_in_20d_range": close_location_in_20d_range,
                        "swing_drawdown_5d": swing_drawdown_5d,
                        "breakout_gap_20d": breakout_gap_20d,
                        "drawdown_from_20d_high": drawdown_from_20d_high,
                        "market_cap_log": math.log10(market_cap + 1.0) / 12.0 if market_cap > 0 else 0.0,
                        "roe_avg_3y_scaled": self._clamp(roe_avg_3y / 30.0, -1.0, 1.5),
                        "net_profit_yoy_scaled": self._clamp(net_profit_yoy / 80.0, -1.5, 2.0),
                        "revenue_yoy_scaled": self._clamp(revenue_yoy / 60.0, -1.5, 2.0),
                        "debt_to_assets_scaled": self._clamp(debt_to_assets / 100.0, 0.0, 1.5),
                        "concept_count_norm": self._clamp(concept_count / 8.0, 0.0, 1.5),
                        "concept_strength_norm": self._clamp(concept_strength / 100.0, 0.0, 1.0),
                        "listing_days_log": listing_days_log,
                        "board_tier": self._safe_float(context.get("board_tier")),
                        "limit_up_count_20d_norm": min(limit_up_count_20d, 5) / 5.0,
                        "days_since_limit_up_norm": days_since_limit_up / 20.0,
                        "limit_up_next_day_open_to_close_avg": self._clamp(limit_up_next_day_avg * 5.0, -0.5, 0.5),
                    },
                    "target": None,
                    "target_profile": {},
                    "label_start_date": None,
                    "label_end_date": None,
                    "label_available_date": None,
                    # "adjusted" / "raw_fallback" / "dropped_missing_adjusted"
                    # once a label window is resolved; None while unlabeled.
                    "label_price_basis": None,
                }
                # P0 label switch. The scheduled CN profile builds execution-
                # aware net-return labels (next-open entry, fixed-horizon
                # exit, per-fill commission and slippage); the legacy momentum
                # composite remains only as an explicit research fallback. A
                # labeled sample still requires the full declared horizon to
                # exist, and its availability date stays strictly label-mature.
                # Blank settings must resolve to the executable profile, never
                # silently regress to the legacy momentum composite.
                label_profile = str(
                    getattr(self.settings, "trainer_cn_label_profile", "") or EXECUTABLE_LABEL_PROFILE
                ).strip().lower()
                target_mode: str
                target_value: float | None = None
                target_profile: dict = {}
                # Window-level price basis. A label window may never mix the
                # adjusted view with raw fallbacks; the classification decides
                # whether this sample is labeled adjusted, labeled raw (whole
                # window consistently raw) or dropped.
                sample_basis = self._resolve_sample_price_basis(
                    symbol_rows=symbol_rows,
                    index=index,
                    label_profile=label_profile,
                    horizon_days=horizon_days,
                )
                if label_profile == RECONCILED_VERSION or index + horizon_days < len(symbol_rows):
                    if label_profile == RECONCILED_VERSION:
                        # Fail closed: the reconciled label path replays raw-basis
                        # execution and needs per-row provenance columns the lake
                        # loader does not select. Without this guard every sample
                        # would silently degrade to UNVERIFIED/None labels.
                        _required_reconciled_columns = (
                            "price_basis",
                            "execution_source_reference",
                            "corporate_action_status",
                        )
                        _missing_reconciled_columns = [
                            key for key in _required_reconciled_columns if key not in symbol_rows[index]
                        ]
                        if _missing_reconciled_columns:
                            raise RuntimeError(
                                "trainer_cn_label_profile=reconciled_v1 requires raw-basis provenance "
                                f"columns {_missing_reconciled_columns} on every row; the lake loader "
                                "does not provide them (use executable_net_return_v1 or pass enriched rows)"
                            )
                        target_value, target_profile, target_mode = self.reconciled_training_target(
                            symbol_rows=symbol_rows, index=index, horizon_days=horizon_days,
                            market=symbol_market, ticker=symbol)
                    elif label_profile == "executable_net_return_v1":
                        target_value, target_profile, target_mode = self._build_executable_net_return_target(
                            symbol_rows=symbol_rows,
                            index=index,
                            horizon_days=horizon_days,
                            market=symbol_market,
                            limit_band_pct=self._safe_float(context.get("limit_band_pct"), default=0.0) or None,
                        )
                    elif label_profile == "legacy_short_horizon_composite_v1":
                        target_value, target_profile = self._build_short_horizon_target_profile(
                            symbol_rows=symbol_rows,
                            index=index,
                            anchor_close=(self._label_price(row, "close") or close),
                            limit_band_pct=self._safe_float(context.get("limit_band_pct"), default=0.0) or None,
                        )
                        target_mode = "short_horizon_composite_v1"
                    else:
                        raise RuntimeError(
                            "Unsupported trainer label profile "
                            f"`{label_profile}`; expected executable_net_return_v1 "
                            "or legacy_short_horizon_composite_v1."
                        )
                    sample["target_profile"] = target_profile
                    if target_value is not None:
                        if sample_basis == "drop_missing_adjusted":
                            # The window mixes adjusted and raw points. Never
                            # persist such a label; count it and leave the row
                            # as an unlabeled (prediction-only) feature sample.
                            self._label_price_stats["dropped_missing_adjusted_count"] += 1
                            sample["label_price_basis"] = "dropped_missing_adjusted"
                            sample["target_profile"] = {
                                **target_profile,
                                "exclusion_reason": "label_window_mixed_price_basis",
                                "label_price_basis": "dropped_missing_adjusted",
                            }
                        else:
                            if sample_basis is not None:
                                self._label_price_stats[
                                    "adjusted_count" if sample_basis == "adjusted" else "raw_fallback_count"
                                ] += 1
                                sample["label_price_basis"] = sample_basis
                                target_profile["label_price_basis"] = sample_basis
                            sample["target"] = target_value
                            sample["target_profile"] = target_profile
                            sample["label_start_date"] = str(symbol_rows[index + 1].get("date") or "").strip()
                            sample["label_end_date"] = target_profile.get("label_available_date") or str(symbol_rows[index + horizon_days].get("date") or "").strip()
                            sample["label_available_date"] = sample["label_end_date"]
                    sample["label_mode"] = target_mode
                samples.append(sample)
        samples.sort(key=lambda item: (item["trade_date"], item["symbol"]))
        return samples

    def _training_stats(self, samples: list[dict], feature_names: list[str]) -> dict[str, tuple[float, float]]:
        stats: dict[str, tuple[float, float]] = {}
        for feature_name in feature_names:
            values = [self._safe_float(sample["features"].get(feature_name)) for sample in samples]
            if not values:
                stats[feature_name] = (0.0, 1.0)
                continue
            mean = sum(values) / len(values)
            std = self._stddev(values) or 1.0
            stats[feature_name] = (mean, std)
        return stats

    def _label_winsorize_config(self, market: str | None) -> dict:
        """Resolve the per-market target winsorization contract."""

        prefix = "trainer_cn" if str(market or "").upper() == "CN" else "trainer_us"
        return {
            "enabled": bool(getattr(self.settings, "trainer_label_winsorize_enabled", True)),
            "lower_quantile": float(
                getattr(self.settings, f"{prefix}_label_winsorize_lower", 0.025)
            ),
            "upper_quantile": float(
                getattr(self.settings, f"{prefix}_label_winsorize_upper", 0.975)
            ),
        }

    def _winsorize_targets(
        self, values: list, config: dict
    ) -> tuple[list, dict]:
        """Clip training targets to the configured quantile band.

        Bounds are computed from the *training window only*, so the clip uses
        no label that a strict point-in-time protocol would hide from this fit.
        """

        audit = {
            "enabled": bool(config.get("enabled")),
            "lower_quantile": config.get("lower_quantile"),
            "upper_quantile": config.get("upper_quantile"),
            "winsorized_count": 0,
        }
        if not config.get("enabled"):
            return list(values), audit
        finite = sorted(
            float(value)
            for value in values
            if value is not None and math.isfinite(float(value))
        )
        if not finite:
            return list(values), audit
        lower_bound = self._linear_quantile(finite, float(config.get("lower_quantile", 0.025)))
        upper_bound = self._linear_quantile(finite, float(config.get("upper_quantile", 0.975)))
        clipped: list = []
        winsorized = 0
        for value in values:
            if value is None:
                clipped.append(value)
                continue
            numeric = float(value)
            if numeric < lower_bound:
                clipped.append(lower_bound)
                winsorized += 1
            elif numeric > upper_bound:
                clipped.append(upper_bound)
                winsorized += 1
            else:
                clipped.append(numeric)
        audit.update(
            {
                "lower_bound": round(lower_bound, 10),
                "upper_bound": round(upper_bound, 10),
                "winsorized_count": winsorized,
            }
        )
        return clipped, audit

    def _feature_transform_config(self) -> dict:
        return {
            "enabled": bool(
                getattr(self.settings, "trainer_feature_transform_enabled", True)
            ),
            "method": "cross_sectional_winsor_mad_zscore",
            "scope": "per_trade_date",
            "winsor_lower": float(
                getattr(self.settings, "trainer_feature_transform_winsor_lower", 0.025)
            ),
            "winsor_upper": float(
                getattr(self.settings, "trainer_feature_transform_winsor_upper", 0.975)
            ),
            "zscore_clip": float(
                getattr(self.settings, "trainer_feature_transform_zscore_clip", 3.0)
            ),
        }

    def _cross_sectional_block(
        self, block: "object", config: dict
    ) -> "object":
        """Winsorize + MAD robust z-score each column of one date's block.

        The block is a 2-D float array of one trade date's cross-section
        (rows = samples, columns = features). Columns with fewer than two finite
        values or zero robust scale are left untouched rather than collapsed to
        a constant zero, so a degenerate cross-section never destroys the level.
        """

        import numpy as np

        lower_quantile = float(config.get("winsor_lower", 0.025))
        upper_quantile = float(config.get("winsor_upper", 0.975))
        clip = float(config.get("zscore_clip", 3.0))

        def _quantile(ordered: "object", quantile: float) -> float:
            if ordered.size == 1:
                return float(ordered[0])
            position = quantile * (ordered.size - 1)
            lower_index = int(np.floor(position))
            upper_index = min(lower_index + 1, ordered.size - 1)
            fraction = position - lower_index
            return float(
                ordered[lower_index]
                + (ordered[upper_index] - ordered[lower_index]) * fraction
            )

        for column in range(block.shape[1]):
            values = np.nan_to_num(block[:, column], nan=0.0, posinf=0.0, neginf=0.0)
            finite = values[np.isfinite(values)]
            if finite.size < 2:
                block[:, column] = values
                continue
            ordered = np.sort(finite)
            lower = _quantile(ordered, lower_quantile)
            upper = _quantile(ordered, upper_quantile)
            clipped = np.clip(values, lower, upper)
            median = float(np.median(clipped))
            mad = float(np.median(np.abs(clipped - median)))
            scale = mad * 1.4826
            if scale <= 1e-12:
                scale = float(np.std(clipped))
            if scale <= 1e-12:
                block[:, column] = values
                continue
            block[:, column] = np.clip((clipped - median) / scale, -clip, clip)
        return block

    def _cross_sectional_feature_matrix(
        self, samples: list[dict], feature_names: list[str]
    ) -> "object":
        """Build the feature matrix with a strict point-in-time cross-section.

        Values for trade date D are normalized with D's own cross-section only,
        so no other (especially future) session can influence them. Fitting and
        scoring both call this with the same contract.
        """

        import numpy as np

        config = self._feature_transform_config()
        matrix = np.empty((len(samples), len(feature_names)), dtype=np.float64)
        for row, sample in enumerate(samples):
            features = sample.get("features", {})
            for column, name in enumerate(feature_names):
                matrix[row, column] = self._safe_float(features.get(name))
        if not config.get("enabled") or not samples:
            return matrix
        grouped: dict[str, list[int]] = defaultdict(list)
        for position, sample in enumerate(samples):
            grouped[str(sample.get("trade_date") or "")].append(position)
        for positions in grouped.values():
            indices = np.asarray(positions, dtype=np.int64)
            transformed = self._cross_sectional_block(matrix[indices, :], config)
            matrix[indices, :] = transformed
        return matrix

    def _resolve_embargo_sessions(self, horizon_days: int) -> int:
        """Embargo gap in sessions; unset resolves to one label horizon."""

        setting = getattr(self.settings, "trainer_embargo_sessions", None)
        if setting is None:
            return int(horizon_days)
        value = int(setting)
        if value < 0:
            raise RuntimeError("trainer_embargo_sessions must not be negative")
        return value

    def _resolve_objective(self) -> dict:
        raw = str(getattr(self.settings, "trainer_objective", "huber") or "huber").strip().lower()
        if raw in {"l2", "mse", "regression", "reg:squarederror", "rmse"}:
            objective = "l2"
        else:
            objective = raw or "huber"
        return {
            "objective": objective,
            "alpha": float(getattr(self.settings, "trainer_huber_alpha", 0.9)),
        }

    def _fit_target_config(self) -> str:
        """Name of the field the GBDT actually fits.

        ``net_return`` is the historical (zero-regression) target; opt in to
        ``risk_adjusted_return`` so the drawdown penalty changes the fit instead
        of only the OOS metric. Both fields live on the same executable label,
        so this switch never changes label construction, only the ``y`` selected
        when the training matrix is assembled.
        """

        enabled = bool(getattr(self.settings, "trainer_fit_on_risk_adjusted", False))
        return "risk_adjusted_return" if enabled else "net_return"

    def _fit_target_value(self, sample: dict) -> float:
        """Resolve one training sample's ``y`` under the configured fit target.

        Mirrors ``_safe_float(sample["target"])`` exactly when the switch is off
        (default), so the fitted matrix is byte-identical to the prior
        behaviour. When on, the label's ``risk_adjusted_return`` is used and a
        profile that does not carry it (e.g. the reconciled replay) falls back
        to the net target, matching the OOS metric's own fallback.
        """

        if not bool(getattr(self.settings, "trainer_fit_on_risk_adjusted", False)):
            return self._safe_float(sample.get("target"))
        profile = sample.get("target_profile") or {}
        value = profile.get("risk_adjusted_return")
        if value is None:
            value = sample.get("target")
        return self._safe_float(value)

    def _resolve_random_seed(self) -> int:
        """GBDT random_state; 42 is the historical single-seed default."""

        try:
            return int(getattr(self.settings, "trainer_random_seed", 42))
        except (TypeError, ValueError):
            return 42

    def _build_regressor(self, model_family: str, objective_config: dict) -> object:
        """Construct the estimator honouring the configured robust objective."""

        is_huber = str(objective_config.get("objective")) == "huber"
        alpha = float(objective_config.get("alpha", 0.9))
        seed = self._resolve_random_seed()
        if model_family == "lightgbm":
            return lgb.LGBMRegressor(
                objective="huber" if is_huber else "regression",
                alpha=alpha,
                n_estimators=260, learning_rate=0.05,
                num_leaves=63, min_child_samples=40, subsample=0.8,
                colsample_bytree=0.8, reg_alpha=0.05, reg_lambda=0.1,
                random_state=seed, n_jobs=-1, verbosity=-1,
            )
        if model_family == "xgboost":
            return xgb.XGBRegressor(
                objective="reg:pseudohubererror" if is_huber else "reg:squarederror",
                huber_slope=alpha,
                n_estimators=260, learning_rate=0.05,
                max_depth=6, min_child_weight=40, subsample=0.8,
                colsample_bytree=0.8, reg_alpha=0.05, reg_lambda=0.1,
                random_state=seed, n_jobs=-1,
            )
        return cat.CatBoostRegressor(
            loss_function=(f"Huber:delta={alpha}" if is_huber else "RMSE"),
            iterations=260, learning_rate=0.05,
            depth=6, l2_leaf_reg=3.0, random_seed=seed,
            verbose=False, thread_count=-1,
        )

    def _feature_matrix(
        self, samples: list[dict], feature_names: list[str]
    ) -> "object":
        return self._cross_sectional_feature_matrix(samples, feature_names)

    def _predict_scores(self, model: object, rows: list[list[float]]) -> list[float]:
        if len(rows) == 0:
            return []
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="X does not have valid feature names, but LGBMRegressor was fitted with feature names",
                category=UserWarning,
            )
            return [float(value) for value in list(model.predict(rows))]

    def _build_lightgbm_explanations(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        feature_values: dict[str, float],
        feature_names: list[str],
        feature_importance: dict[str, float],
        feature_stats: dict[str, tuple[float, float]],
    ) -> list[dict]:
        rows: list[dict] = []
        for feature_name in feature_names:
            value = self._safe_float(feature_values.get(feature_name))
            mean, std = feature_stats.get(feature_name, (0.0, 1.0))
            z_score = ((value - mean) / std) if std else 0.0
            contribution = z_score * feature_importance.get(feature_name, 0.0) * self._feature_direction(feature_name)
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "trade_date": trade_date,
                    "feature_name": feature_name,
                    "feature_value": round(value * 100, 4),
                    "contribution": round(contribution * 100, 4),
                    "direction": "positive" if contribution >= 0 else "negative",
                    "display_order": 0,
                }
            )
        ranked = sorted(rows, key=lambda item: abs(float(item.get("contribution") or 0.0)), reverse=True)[:5]
        for index, row in enumerate(ranked, start=1):
            row["display_order"] = index
        return ranked

    def _summarize_target_profile(self, samples: list[dict]) -> dict[str, float | int]:
        metric_keys = [
            "next_1d_close_return",
            "next_1d_open_gap",
            "next_1d_open_to_high",
            "next_1d_open_to_close",
            "next_3d_max_return",
            "next_3d_max_drawdown",
            "next_5d_max_return",
            "next_5d_max_drawdown",
            "next_5d_close_return",
            "next_20d_close_return",
            "next_20d_max_drawdown",
            "failed_after_gap_up",
            "tradable_next_day",
            "composite_target",
            "gross_return",
            "net_return",
        ]
        summary: dict[str, float | int] = {"sample_count": len(samples)}
        for metric_key in metric_keys:
            values = [
                self._safe_float((sample.get("target_profile") or {}).get(metric_key))
                for sample in samples
                if (sample.get("target_profile") or {}).get(metric_key) is not None
            ]
            if not values:
                continue
            summary[f"{metric_key}_avg"] = round(sum(values) / len(values), 4)
        return summary

    @staticmethod
    def _oos_metric_value(sample: dict) -> float | None:
        """Return one matured prediction's realized gate metric.

        The unified gate recognises a ``risk_adjusted_return``-style mean. The
        trainer stores that exact field on each executable label (now
        ``net - drawdown_penalty * |path_drawdown|``); when a label profile does
        not carry it (e.g. the reconciled replay) the realized net target is
        used, which is the same cost-adjusted return minus the tail penalty.
        """

        profile = sample.get("target_profile") or {}
        value = profile.get("risk_adjusted_return")
        if value is None:
            value = sample.get("target")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _freeze_oos_candidates(
        ranked_pairs: list[tuple[dict, float]],
        *,
        top_n: int = OOS_EVALUATION_TOP_N,
    ) -> dict:
        """Freeze the top-N scored candidates *before* reading any outcome.

        P1-3: the evaluation list may never depend on post-hoc result
        availability. The candidate list is the score-ranked head; a candidate
        whose label is missing, rejected (e.g. a corporate-action discontinuity
        leaves no usable exit price) or not yet matured is counted in place
        instead of being backfilled by a lower-ranked name.
        """

        candidates = [
            sample for sample, _score in ranked_pairs[: max(0, int(top_n))]
        ]
        labeled_samples: list[dict] = []
        missing_reasons: Counter[str] = Counter()
        immature = 0
        for sample in candidates:
            if sample.get("target") is not None:
                labeled_samples.append(sample)
                continue
            reason = str(
                (sample.get("target_profile") or {}).get("exclusion_reason") or ""
            ).strip()
            if reason and reason != "label_window_immature":
                # The window existed but produced no usable outcome (missing exit
                # price, unbuyable / locked entry, corporate-action discontinuity).
                missing_reasons[reason] += 1
                continue
            # The label window is not observable yet: the sample builder skips the
            # label branch entirely once the window would run past the data, and
            # records `label_window_immature` when it runs but is not complete.
            missing_reasons[reason or "label_window_not_available"] += 1
            immature += 1
        missing_label = sum(missing_reasons.values())
        return {
            "candidates": candidates,
            "labeled_samples": labeled_samples,
            "candidate_count": len(candidates),
            "labeled_count": len(labeled_samples),
            "missing_label_count": missing_label,
            "immature_label_count": immature,
            "missing_outcome_count": missing_label - immature,
            "missing_label_reasons": dict(sorted(missing_reasons.items())),
        }

    @staticmethod
    def _summarize_oos_evaluation(
        per_date: list[dict],
        *,
        horizon_days: int,
        label_profile: str,
        top_n: int,
        window_capable_dates: int | None = None,
    ) -> dict[str, object] | None:
        """Aggregate matured walk-forward predictions into gate-readable evidence.

        ``per_date`` holds one record per prediction date that had a frozen
        top-N candidate list (the point-in-time pool guarantees such labels were
        never trained on). Returns ``None`` when no prediction date matured, so
        the run stays honestly unevaluated instead of reporting an empty metric.
        Dates whose candidates were all missing / rejected / immature still
        contribute their counts below, so the omission is auditable.

        ``window_capable_dates`` declares the maximum number of matured
        evaluation dates this run's prediction window can physically produce
        (prediction dates minus the label horizon). The promotion gate uses it
        only to lower an otherwise impossible date threshold, and records the
        decision in its audit fields; it is never used to fake coverage.
        """

        evaluated = [row for row in per_date if row.get("metric_value") is not None]
        if not evaluated:
            return None
        metric_values = [float(row["metric_value"]) for row in evaluated]
        net_values = [
            float(row["net_return"])
            for row in evaluated
            if row.get("net_return") is not None
        ]
        missing_reasons: Counter[str] = Counter()
        for row in per_date:
            missing_reasons.update(dict(row.get("missing_label_reasons") or {}))
        summary: dict[str, object] = {
            "schema_version": "walk_forward_oos_evaluation_v1",
            "source": "trainer_walk_forward_predictions",
            "evaluated_date_count": len(evaluated),
            "evaluated_sample_count": sum(
                int(row.get("sample_count") or 0) for row in evaluated
            ),
            # The frozen (pre-outcome) candidate list and everything the outcome
            # availability removed from the metric, counted in place so a
            # missing / rejected / immature name is never silently backfilled.
            "frozen_candidate_count": sum(
                int(row.get("candidate_count") or 0) for row in per_date
            ),
            "candidate_date_count": len(per_date),
            "missing_label_count": sum(
                int(row.get("missing_label_count") or 0) for row in per_date
            ),
            "immature_label_count": sum(
                int(row.get("immature_label_count") or 0) for row in per_date
            ),
            "missing_outcome_count": sum(
                int(row.get("missing_outcome_count") or 0) for row in per_date
            ),
            "missing_label_reasons": dict(sorted(missing_reasons.items())),
            "mean_risk_adjusted_return": round(
                sum(metric_values) / len(metric_values), 8
            ),
            "positive_date_rate": round(
                sum(1 for value in metric_values if value > 0) / len(metric_values), 6
            ),
            "top_n": int(top_n),
            "horizon_days": int(horizon_days),
            "label_profile": label_profile,
            "date_min": str(evaluated[0].get("trade_date") or ""),
            "date_max": str(evaluated[-1].get("trade_date") or ""),
            "metric_definition": (
                "mean realized label risk_adjusted_return of the frozen top-N "
                "scored walk-forward predictions per matured prediction date; the "
                "candidate list is chosen before labels are read, and candidates "
                "with a missing / rejected / immature label are counted under "
                "missing_label_count instead of being backfilled; the label "
                "path uses market/industry return 0.0, so this equals the "
                "cost-adjusted net return minus the configured drawdown penalty"
            ),
        }
        if window_capable_dates is not None:
            summary["window_capable_dates"] = max(0, int(window_capable_dates))
        if net_values:
            summary["mean_net_return"] = round(sum(net_values) / len(net_values), 8)
        return summary

    def _summarize_symbol_feature_context(self, symbol_feature_context: dict[str, dict]) -> dict[str, float | int]:
        total = len(symbol_feature_context)
        listing_count = sum(1 for item in symbol_feature_context.values() if item.get("listing_date"))
        board_tier_count = sum(1 for item in symbol_feature_context.values() if self._safe_float(item.get("board_tier")) > 0)
        fundamental_history_count = sum(1 for item in symbol_feature_context.values() if item.get("fundamental_history"))
        concept_history_count = sum(1 for item in symbol_feature_context.values() if item.get("concept_history"))
        return {
            "symbol_count": total,
            "listing_date_count": listing_count,
            "listing_date_coverage_pct": round((listing_count / max(total, 1)) * 100.0, 1),
            "fundamental_history_count": fundamental_history_count,
            "fundamental_history_coverage_pct": round((fundamental_history_count / max(total, 1)) * 100.0, 1),
            "concept_history_count": concept_history_count,
            "concept_history_coverage_pct": round((concept_history_count / max(total, 1)) * 100.0, 1),
            "non_main_board_count": board_tier_count,
            "non_main_board_pct": round((board_tier_count / max(total, 1)) * 100.0, 1),
        }

    def _feature_enhancement_meta(self, symbol_feature_context: dict[str, dict]) -> dict[str, object]:
        summary = self._summarize_symbol_feature_context(symbol_feature_context)
        listing_cov = float(summary.get("listing_date_coverage_pct") or 0.0)
        fundamental_cov = float(summary.get("fundamental_history_coverage_pct") or 0.0)
        concept_cov = float(summary.get("concept_history_coverage_pct") or 0.0)
        if max(listing_cov, fundamental_cov, concept_cov) >= 40.0:
            mode = "enhanced"
            note = "Historical fundamental/concept inputs are materially present in this run."
        elif max(listing_cov, fundamental_cov, concept_cov) > 0.0:
            mode = "partial"
            note = "Enhanced inputs are partially available; treat this as a mixed price-plus-context run."
        else:
            mode = "price_action_only"
            note = "Historical fundamental/concept coverage is absent, so this run is effectively price/volume only."
        return {
            "mode": mode,
            "coverage": summary,
            "note": note,
        }

    def _build_score_calibration(
        self,
        *,
        model: object,
        train_window: list[dict],
        feature_names: list[str],
        bucket_count: int = 12,
    ) -> list[dict]:
        if not train_window:
            return []
        # One matrix for the whole window, through the exact same transform the
        # fit and the walk-forward scoring use (`_feature_matrix` normalizes per
        # trade date). Batching is applied to the *prediction* only: building
        # per-chunk matrices would both skip the transform and cut a same-day
        # cross-section in half.
        matrix = self._feature_matrix(train_window, feature_names)
        predicted_scores: list[float] = []
        for start in range(0, len(train_window), 4096):
            chunk = matrix[start:start + 4096]
            scores = self._predict_scores(model, chunk)
            if len(scores) != len(chunk):
                raise RuntimeError("Calibration prediction count does not match training window")
            predicted_scores.extend(scores)
        ranked_pairs = sorted(
            zip(predicted_scores, train_window, strict=False),
            key=lambda pair: float(pair[0]),
        )
        if not ranked_pairs:
            return []
        bucket_size = max(25, math.ceil(len(ranked_pairs) / max(bucket_count, 1)))
        buckets: list[dict] = []
        for start in range(0, len(ranked_pairs), bucket_size):
            chunk = ranked_pairs[start : start + bucket_size]
            if not chunk:
                continue
            chunk_scores = [float(pair[0]) for pair in chunk]
            chunk_samples = [pair[1] for pair in chunk]
            profile_summary = self._summarize_target_profile(chunk_samples)
            buckets.append(
                {
                    "score_low": round(min(chunk_scores), 6),
                    "score_high": round(max(chunk_scores), 6),
                    "score_mid": round(sum(chunk_scores) / len(chunk_scores), 6),
                    "sample_count": len(chunk_samples),
                    "metrics": profile_summary,
                }
            )
        return buckets

    def _lookup_calibrated_metrics(
        self,
        *,
        score: float,
        calibration_buckets: list[dict],
    ) -> dict[str, float | int] | None:
        if not calibration_buckets:
            return None
        for bucket in calibration_buckets:
            if float(bucket.get("score_low") or 0.0) <= score <= float(bucket.get("score_high") or 0.0):
                return dict(bucket.get("metrics") or {})
        nearest = min(
            calibration_buckets,
            key=lambda bucket: abs(score - float(bucket.get("score_mid") or 0.0)),
        )
        return dict(nearest.get("metrics") or {})

    def _load_oos_score_calibration(self, *, market: str | None) -> tuple[list[dict], dict]:
        market_code = self._normalize_market_code(market) or "ALL"
        try:
            with SessionLocal() as db:
                snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(self.MODEL_CALIBRATION_SNAPSHOT_TYPE)
        except Exception:
            return [], {"source": "unavailable", "market": market_code}
        payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
        if not isinstance(payload, dict):
            return [], {"source": "missing", "market": market_code}
        payload_markets = {str(item or "").upper() for item in (payload.get("markets") or []) if str(item or "").strip()}
        if market_code not in {"", "ALL"} and payload_markets and market_code not in payload_markets:
            return [], {"source": "market_mismatch", "market": market_code, "payload_markets": sorted(payload_markets)}
        buckets = payload.get("score_calibration_buckets")
        if not isinstance(buckets, list) or not buckets:
            return [], {"source": "empty", "market": market_code, "snapshot_id": (snapshot or {}).get("id")}
        usable_buckets = [
            bucket
            for bucket in buckets
            if isinstance(bucket, dict) and isinstance(bucket.get("metrics"), dict) and int(bucket.get("sample_count") or 0) >= 5
        ]
        if not usable_buckets:
            return [], {"source": "thin", "market": market_code, "snapshot_id": (snapshot or {}).get("id")}
        return usable_buckets, {
            "source": "model_calibration_snapshot",
            "market": market_code,
            "snapshot_id": (snapshot or {}).get("id"),
            "snapshot_date": (snapshot or {}).get("snapshot_date"),
            "created_at": (snapshot or {}).get("created_at"),
            "sample_count": payload.get("sample_count"),
            "latest_trade_date": payload.get("latest_trade_date"),
        }

    def _build_detail_row(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        score: float,
        rank_value: float,
        universe_size: int,
        horizon_days: int,
        run_name: str,
        calibrated_metrics: dict[str, float | int] | None = None,
    ) -> dict:
        metrics = calibrated_metrics or {}
        expected_return_5d = metrics.get("next_5d_close_return_avg")
        # Five-day path extrema must not masquerade as twenty-day estimates.
        expected_return_20d = metrics.get("next_20d_close_return_avg")
        expected_drawdown_20d = metrics.get("next_20d_max_drawdown_avg")
        if expected_drawdown_20d is not None:
            expected_drawdown_20d = abs(float(expected_drawdown_20d))
        reward_risk_ratio = None
        if expected_return_20d not in (None, 0) and expected_drawdown_20d not in (None, 0):
            reward_risk_ratio = round(abs(float(expected_return_20d)) / float(expected_drawdown_20d), 2)
        risk_score = None
        if expected_drawdown_20d is not None:
            risk_score = round(self._clamp(expected_drawdown_20d * 4.3, 8.0, 92.0), 1)
        target_horizon = 5
        if metrics.get("next_3d_max_return_avg") is not None and metrics.get("next_5d_close_return_avg") is not None:
            next_3d = float(metrics.get("next_3d_max_return_avg") or 0.0)
            next_5d = float(metrics.get("next_5d_close_return_avg") or 0.0)
            if next_3d >= next_5d + 1.5:
                target_horizon = 3
        enriched = enrich_model_output(
            {
                "score": score,
                "rank_value": rank_value,
                "universe_size": universe_size,
                "percentile": round(
                    max(0.0, min(100.0, (1 - ((float(rank_value) - 1) / max(universe_size, 1))) * 100.0)),
                    1,
                ),
                "target_horizon_days": target_horizon or max(5, min(20, horizon_days)),
                "expected_return_5d": expected_return_5d,
                "expected_return_20d": expected_return_20d,
                "expected_drawdown_20d": expected_drawdown_20d,
                "model_reward_risk_ratio": reward_risk_ratio,
                "risk_score": risk_score,
                "model_run": {"name": run_name},
            },
            lang="en",
        ) or {}
        return {
            "symbol_id": symbol_id,
            "trade_date": trade_date,
            "confidence": enriched.get("confidence"),
            "bullish_prob": enriched.get("bullish_prob"),
            "bearish_prob": enriched.get("bearish_prob"),
            "expected_return_5d": enriched.get("expected_return_5d"),
            "expected_return_20d": enriched.get("expected_return_20d"),
            "expected_drawdown_20d": enriched.get("expected_drawdown_20d"),
            "model_reward_risk_ratio": enriched.get("model_reward_risk_ratio"),
            "risk_score": enriched.get("risk_score"),
            "target_horizon_days": enriched.get("target_horizon_days"),
            "universe_size": enriched.get("universe_size"),
            "percentile": enriched.get("percentile"),
            "regime_label": enriched.get("regime_label"),
            "conviction_bucket": enriched.get("conviction_bucket"),
            "position_size_hint": enriched.get("position_size_hint"),
            "entry_style": enriched.get("entry_style"),
            "signal_label": enriched.get("signal_label"),
            "signal_strength": enriched.get("signal_strength"),
            "summary_text": enriched.get("summary_text") or summarize_model_output(enriched, lang="en"),
        }

    def _train_lightgbm(
        self,
        *,
        run_name: str,
        signal_type: str,
        lookback_days: int,
        normalized_tickers: set[str] | None,
        market: str | None,
        universe: str | None,
        rows: list[dict],
        model_family: str = "lightgbm",
    ) -> int:
        if model_family == "lightgbm" and lgb is None:
            raise RuntimeError("LightGBM is not installed. Run `.venv/bin/pip install -r requirements.txt` first.")
        if model_family == "xgboost" and xgb is None:
            raise RuntimeError("XGBoost is not installed. Install the challenger dependencies before starting the race.")
        if model_family == "catboost" and cat is None:
            raise RuntimeError("CatBoost is not installed. Install the challenger dependencies before starting the race.")
        if signal_type != "momentum":
            raise RuntimeError("The LightGBM trainer currently supports `momentum` signal_type only.")

        horizon_days = max(5, min(10, lookback_days * 2))
        # Predictions must be persisted under one concrete market; infer it
        # from the tickers when the caller did not pin CN/US/HK.
        run_market = self._resolve_run_market(market=market, rows=rows)
        # Blank settings must resolve to the executable profile, never
        # silently regress to the legacy momentum composite.
        label_profile_setting = str(
            getattr(self.settings, "trainer_cn_label_profile", "") or EXECUTABLE_LABEL_PROFILE
        ).strip().lower()
        entry_not_executable_policy_setting = str(
            getattr(self.settings, "trainer_cn_entry_not_executable_policy", "") or "exclude"
        ).strip().lower()
        target_profile_version, score_semantics_version = resolve_label_profile_contract(label_profile_setting)
        execution_cost_setting = {
            "commission_bps_one_way": float(
                getattr(self.settings, "trainer_cn_execution_commission_bps", 2.5) or 0.0
            ),
            "slippage_bps_one_way": float(
                getattr(self.settings, "trainer_cn_execution_slippage_bps", 15.0) or 0.0
            ),
        }
        feature_names = self._feature_names(lookback_days=lookback_days)
        window_policy = self._training_window_policy(run_market)
        # Training-side robustness contract resolved once per run so the loop,
        # the fit and the persisted run config all describe the same choices.
        label_winsorize_config = self._label_winsorize_config(run_market)
        objective_config = self._resolve_objective()
        feature_transform_config = self._feature_transform_config()
        drawdown_penalty_setting = float(
            getattr(self.settings, "trainer_drawdown_penalty", 0.25) or 0.0
        )
        fit_target_setting = self._fit_target_config()
        random_seed_setting = self._resolve_random_seed()
        embargo_sessions = self._resolve_embargo_sessions(horizon_days)
        universe_filter_stats = getattr(self, "_universe_filter_stats", None) or {
            "enabled": bool(getattr(self.settings, "trainer_universe_filter_enabled", True)),
            "applied": False,
            "skipped_reason": "rows_supplied_without_load_rows",
            "input_rows": len(rows),
            "retained_rows": len(rows),
            "output_rows": len(rows),
            "excluded_rows": 0,
            "row_deletion": False,
            "gate_semantics": "sample_gate_full_timeline_retained",
            "exclusion_counts": {},
        }
        symbol_feature_context = self._load_symbol_feature_context(
            rows=rows,
            market=market,
            normalized_tickers=normalized_tickers,
        )
        samples = self._build_lightgbm_samples(
            rows=rows,
            lookback_days=lookback_days,
            horizon_days=horizon_days,
            symbol_feature_context=symbol_feature_context,
            market=market,
        )
        # Audit the gate's own output: how many signal days the tradable-universe
        # rules suppressed while the full (undeleted) timeline fed the features,
        # entry and label windows.
        universe_filter_stats = {
            **universe_filter_stats,
            "gated_signal_days": int(self._universe_gated_signal_days),
        }
        if not samples:
            raise RuntimeError("LightGBM trainer found no usable feature rows. The market lake may still be too short.")
        # Fail closed before any run row is created: a partially covered
        # adjusted view must not be persisted as an "adjusted" label run.
        label_price_contract = self._label_price_basis_contract(
            label_profile=label_profile_setting
        )
        # The prediction product inherits the (already gated) label basis; record
        # the same contract fields under the inference entry point so consumers
        # can never mislabel a run's predictions as adjusted.
        prediction_price_basis_contract = self._prediction_price_basis_contract()

        samples_by_date: dict[str, list[dict]] = defaultdict(list)
        labeled_samples: list[dict] = []
        all_dates: list[str] = []
        seen_dates: set[str] = set()
        for sample in samples:
            trade_date = sample["trade_date"]
            samples_by_date[trade_date].append(sample)
            if sample.get("target") is not None:
                labeled_samples.append(sample)
            if trade_date not in seen_dates:
                seen_dates.add(trade_date)
                all_dates.append(trade_date)
        all_dates.sort()
        warmup_dates = max(20, lookback_days * 8)
        if len(all_dates) <= warmup_dates + 5:
            raise RuntimeError("LightGBM trainer needs a longer price history before it can score recent trade dates.")
        prediction_start_index = max(warmup_dates, len(all_dates) - 60)
        prediction_dates = all_dates[prediction_start_index:]
        if not prediction_dates:
            raise RuntimeError("LightGBM trainer found no prediction dates.")

        first_prediction_date = prediction_dates[0]
        trading_dates = [value for value in (self._parse_iso_date(item) for item in all_dates) if value is not None]

        def _required_sample_date(sample: dict, key: str) -> date:
            parsed = self._parse_iso_date(sample.get(key))
            if parsed is None:
                raise RuntimeError(f"Training sample is missing a valid {key}.")
            return parsed

        point_in_time_pool = PointInTimeTrainingPool(
            labeled_samples,
            trading_dates=trading_dates,
            feature_date=lambda sample: _required_sample_date(sample, "trade_date"),
            label_end_date=lambda sample: _required_sample_date(sample, "label_end_date"),
            label_available_date=lambda sample: _required_sample_date(sample, "label_available_date"),
            sample_id=lambda sample: f"{sample['symbol']}:{sample['trade_date']}",
            purge_sessions=horizon_days,
            embargo_sessions=embargo_sessions,
        )
        first_prediction_day = self._parse_iso_date(first_prediction_date)
        if first_prediction_day is None:
            raise RuntimeError("LightGBM trainer found an invalid first prediction date.")
        train_pool = list(point_in_time_pool.advance(first_prediction_day))
        if len(train_pool) < 1000:
            raise RuntimeError("LightGBM trainer needs more labeled history before the first prediction date.")

        # A prediction's label is only visible after `horizon_days`.  Persist
        # the split protocol so downstream evaluation can distinguish genuine
        # walk-forward results from older runs that merely overlap a test date.
        initial_train_end = max((str(sample["trade_date"]) for sample in train_pool), default=None)
        if window_policy.mode == "complete_dates_v1":
            initial_dates = sorted({sample["trade_date"] for sample in train_pool})[-window_policy.date_count:]
            initial_train_start = initial_dates[0] if initial_dates else None
        else:
            initial_window = self._complete_date_training_window(train_pool, max_rows=window_policy.max_rows)
            initial_train_start = initial_window[0]["trade_date"] if initial_window else None
            del initial_window
        universe_version = f"{str(universe or ('local_watchlist' if normalized_tickers else 'full_dataset')).lower()}:{len(normalized_tickers or []) or 'all'}"

        enhancement_meta = self._feature_enhancement_meta(symbol_feature_context)
        input_market_date = get_latest_lake_trade_date(market=run_market) if run_market in {"CN", "US"} else None
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            detail_repo = PredictionDetailRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_repo.complete_stale_running_runs(
                stale_after_hours=6,
                message_prefix="Trainer cleanup closed a stale running model run.",
            )
            symbol_map = {symbol.ticker.upper(): symbol.id for symbol in symbol_repo.list_symbols()}
            binding_market = run_market
            training_binding = (
                adjustment_version_binding(binding_market)
                if binding_market in {"CN", "US"}
                else {"adjusted_view_sha256": None, "actions_snapshot_sha256": None}
            )
            run = model_repo.create_run(
                name=run_name,
                model_type=f"{model_family}_multifactor",
                market=run_market,
                universe=universe or ("local_watchlist" if normalized_tickers else "full_dataset"),
                train_start=initial_train_start,
                train_end=initial_train_end,
                test_start=prediction_dates[0] if prediction_dates else None,
                test_end=prediction_dates[-1] if prediction_dates else None,
                config={
                    "model_type": model_family,
                    "signal_type": signal_type,
                    "lookback_days": lookback_days,
                    "prediction_horizon_days": horizon_days,
                    "target_profile": target_profile_version,
                    # Same recorded cost tuple as the artifact manifest
                    # (``model_metadata['execution_cost_bps']``).  Persisting it
                    # on the run config lets ``resolve_execution_contract``
                    # re-derive the identical contract for the default
                    # executable profile, which the trainer otherwise leaves as
                    # a null ``execution_contract``.
                    "execution_cost_bps": execution_cost_setting,
                    "execution_contract": (execution_contract(run_market, FillCostModel(**execution_cost_setting))
                        if target_profile_version == RECONCILED_VERSION else None),
                    "training_weight_policy": TRAINING_WEIGHT_POLICY,
                    "training_window_policy": asdict(window_policy),
                    "score_semantics": score_semantics_version,
                    "score_contract_version": SCORE_CONTRACT_VERSION,
                    "target_metric_keys": [
                        "next_1d_close_return",
                        "next_1d_open_gap",
                        "next_1d_open_to_high",
                        "next_3d_max_return",
                        "next_3d_max_drawdown",
                        "next_5d_max_return",
                        "next_5d_close_return",
                        "failed_after_gap_up",
                        "tradable_next_day",
                    ],
                    "feature_families": [
                        "price_trend",
                        "price_extension",
                        "volume_intensity",
                        "intraday_structure",
                        "liquidity_proxy",
                        "listing_maturity",
                        "board_tier",
                        "cn_limit_up_dynamics",
                    ],
                    "feature_enhancement_mode": enhancement_meta.get("mode"),
                    "feature_enhancement_note": enhancement_meta.get("note"),
                    "symbol_context_summary": enhancement_meta.get("coverage"),
                    "ticker_count": len(normalized_tickers or []),
                    "prediction_dates": len(prediction_dates),
                    "input_market_date": input_market_date,
                    "schema_version": 3,
                    "evaluation_protocol": "walk_forward_purged_v2",
                    "training_protocol": "point_in_time_purged_v2",
                    "oos_start_date": first_prediction_date,
                    "purge_gap_days": horizon_days,
                    "embargo_sessions": embargo_sessions,
                    "label_availability_rule": "strictly_before_prediction_date",
                    "universe_version": universe_version,
                    "universe_filter_stats": universe_filter_stats,
                    "label_winsorize": label_winsorize_config,
                    "objective": objective_config,
                    "drawdown_penalty": drawdown_penalty_setting,
                    "fit_target": fit_target_setting,
                    "random_seed": random_seed_setting,
                    "feature_transform": feature_transform_config,
                    "label_price_basis": label_price_contract["label_price_basis"],
                    "label_price_basis_applicable": label_price_contract["label_price_basis_applicable"],
                    "adjusted_view_present": label_price_contract["adjusted_view_present"],
                    "adjusted_view_state": label_price_contract["adjusted_view_state"],
                    "raw_fallback_allowed": label_price_contract["raw_fallback_allowed"],
                    "raw_fallback_optin_audit": label_price_contract.get(
                        "raw_fallback_optin_audit"
                    ),
                    "adjusted_count": label_price_contract["adjusted_count"],
                    "raw_fallback_count": label_price_contract["raw_fallback_count"],
                    "dropped_missing_adjusted_count": label_price_contract[
                        "dropped_missing_adjusted_count"
                    ],
                    "adjusted_coverage_share": label_price_contract["adjusted_coverage_share"],
                    "require_full_adjusted_coverage": label_price_contract[
                        "require_full_adjusted_coverage"
                    ],
                    "adjustment_version": training_binding.get("adjustment_version"),
                    "adjusted_view_sha256": training_binding["adjusted_view_sha256"],
                    "actions_snapshot_sha256": training_binding["actions_snapshot_sha256"],
                    "prediction_price_basis_contract": prediction_price_basis_contract,
                },
                artifact_path=None,
                status="running",
            )
            run_id = int(run.id)
            # Avoid holding an idle PostgreSQL transaction during LightGBM fitting.
            db.commit()

            signal_rows: list[dict] = []
            detail_rows: list[dict] = []
            explanation_rows: list[dict] = []
            retrain_interval = 5
            normalized_market = run_market
            model = None
            feature_importance: dict[str, float] = {}
            feature_stats: dict[str, tuple[float, float]] = {}
            calibration_buckets: list[dict] = []
            latest_prediction_date = prediction_dates[-1]
            training_window_audits: list[dict] = []
            # One matured top-N record per prediction date; aggregated after the
            # loop into the gate-readable `oos_evaluation` evidence.
            oos_evaluation_records: list[dict] = []
            oos_calibration_buckets, oos_calibration_meta = self._load_oos_score_calibration(market=normalized_market)

            for index, trade_date in enumerate(prediction_dates):
                prediction_day = self._parse_iso_date(trade_date)
                if prediction_day is None:
                    raise RuntimeError(f"LightGBM trainer found invalid prediction date `{trade_date}`.")
                train_pool = list(point_in_time_pool.advance(prediction_day))
                if not train_pool:
                    continue
                if model is None or index % retrain_interval == 0:
                    try:
                        train_window, selection_audit = select_training_window(train_pool,
                            policy=window_policy, feature_count=len(feature_names))
                    except TrainingWindowBlocked as exc:
                        model_repo.merge_config(run_id, {"training_window_blocker": {**exc.audit, "prediction_date": trade_date},
                                                        "training_window_audits": training_window_audits})
                        model_repo.complete_run(run_id, status="failed", artifact_path=None)
                        db.commit()
                        raise
                    # One compact matrix, built with a strict point-in-time
                    # cross-sectional transform (per trade date, same-day
                    # cross-section only).
                    x_train = self._feature_matrix(train_window, feature_names)
                    raw_targets = [self._fit_target_value(sample) for sample in train_window]
                    y_train, winsorize_audit = self._winsorize_targets(
                        raw_targets, label_winsorize_config
                    )
                    sample_weights, weight_audit = date_balanced_training_weights(train_window)
                    training_window_audits.append({
                        **weight_audit, "prediction_date": trade_date,
                        "max_rows": window_policy.max_rows,
                        "window_selection": selection_audit,
                        "available_date_count": len({row["trade_date"] for row in train_pool}),
                        "meets_252_session_research_window": weight_audit["date_count"] >= 252,
                        "label_winsorize": winsorize_audit,
                    })
                    model = self._build_regressor(model_family, objective_config)
                    model.fit(x_train, y_train, sample_weight=sample_weights)
                    del x_train, y_train, sample_weights, raw_targets
                    raw_importance_obj = getattr(model, "feature_importances_", None)
                    if raw_importance_obj is None:
                        raw_importance = [0.0] * len(feature_names)
                    else:
                        raw_importance = [float(value) for value in list(raw_importance_obj)]
                    if len(raw_importance) < len(feature_names):
                        raw_importance.extend([0.0] * (len(feature_names) - len(raw_importance)))
                    raw_importance = raw_importance[: len(feature_names)]
                    importance_total = sum(raw_importance) or 1.0
                    feature_importance = {
                        feature_name: raw_importance[pos] / importance_total
                        for pos, feature_name in enumerate(feature_names)
                    }
                    feature_stats = self._training_stats(train_window, feature_names)
                    calibration_buckets = self._build_score_calibration(
                        model=model,
                        train_window=train_window,
                        feature_names=feature_names,
                    )

                date_samples = samples_by_date.get(trade_date) or []
                if not date_samples:
                    continue
                x_date = self._feature_matrix(date_samples, feature_names)
                predicted_scores = self._predict_scores(model, x_date) if model is not None else []
                ranked_pairs = sorted(
                    zip(date_samples, predicted_scores, strict=False),
                    key=lambda pair: float(pair[1]),
                    reverse=True,
                )
                # Record genuine out-of-sample evidence: a prediction date whose
                # top-N scored samples already carry a matured label. The
                # point-in-time pool only ever trains on labels available
                # strictly before this date, so these are never in-sample.
                if label_profile_setting in OOS_RETURN_LABEL_PROFILES:
                    # Freeze the candidate list from the scores first: outcome
                    # availability may never re-order or backfill the top-N.
                    frozen = self._freeze_oos_candidates(ranked_pairs)
                    if frozen["candidate_count"]:
                        selected_pairs = frozen["labeled_samples"]
                        metric_values = [
                            value
                            for value in (
                                self._oos_metric_value(sample)
                                for sample in selected_pairs
                            )
                            if value is not None
                        ]
                        net_values = [
                            self._safe_float(sample.get("target"))
                            for sample in selected_pairs
                        ]
                        oos_evaluation_records.append(
                            {
                                "trade_date": trade_date,
                                "metric_value": (
                                    sum(metric_values) / len(metric_values)
                                    if metric_values
                                    else None
                                ),
                                "net_return": (
                                    sum(net_values) / len(net_values)
                                    if net_values
                                    else None
                                ),
                                "sample_count": len(selected_pairs),
                                "candidate_count": frozen["candidate_count"],
                                "missing_label_count": frozen["missing_label_count"],
                                "immature_label_count": frozen["immature_label_count"],
                                "missing_outcome_count": frozen["missing_outcome_count"],
                                "missing_label_reasons": frozen["missing_label_reasons"],
                            }
                        )
                for rank_index, (sample, raw_score) in enumerate(ranked_pairs, start=1):
                    symbol = sample["symbol"]
                    symbol_id = symbol_map.get(symbol)
                    if symbol_id is None:
                        continue
                    score = self._clamp(float(raw_score), -0.35, 0.35)
                    signal_rows.append(
                        {
                            "symbol_id": symbol_id,
                            "trade_date": trade_date,
                            "score": score,
                            "rank_value": float(rank_index),
                        }
                    )
                    if trade_date == latest_prediction_date:
                        # Expected-return/drawdown fields are published from the
                        # out-of-sample calibration snapshot when one exists.
                        # This repo has no producer for
                        # ``model_calibration_snapshot`` yet, so fall back to the
                        # run's own matured train-window calibration buckets
                        # (20-day keys included) instead of publishing nulls.
                        detail_calibration_buckets = (
                            oos_calibration_buckets or calibration_buckets
                        )
                        calibrated_metrics = (
                            self._lookup_calibrated_metrics(
                                score=score,
                                calibration_buckets=detail_calibration_buckets,
                            )
                            if detail_calibration_buckets
                            else None
                        )
                        detail_rows.append(
                            self._build_detail_row(
                                symbol_id=symbol_id,
                                trade_date=trade_date,
                                score=score,
                                rank_value=float(rank_index),
                                universe_size=len(ranked_pairs),
                                horizon_days=horizon_days,
                                run_name=run_name,
                                calibrated_metrics=calibrated_metrics,
                            )
                        )
                        explanation_rows.extend(
                            self._build_lightgbm_explanations(
                                symbol_id=symbol_id,
                                trade_date=trade_date,
                                feature_values=sample["features"],
                                feature_names=feature_names,
                                feature_importance=feature_importance,
                                feature_stats=feature_stats,
                            )
                        )
            if not signal_rows:
                model_repo.complete_run(run_id, status="failed", artifact_path=None)
                raise RuntimeError("LightGBM trainer produced no predictions.")

            # Promotion-gate evidence. Keys match what `promotion_gate_v2`
            # reads from a run's config: `training_sample_count` (top level),
            # `oos_evaluation` (evaluated_date_count + mean metric), while
            # `purge_gap_days` / `embargo_sessions` / `prediction_price_basis_contract`
            # were already persisted on `create_run`. Evidence the trainer
            # genuinely cannot produce is recorded as an explicit omission
            # reason rather than a fabricated pass.
            latest_training_sample_count: int | None = None
            for audit in reversed(training_window_audits):
                candidate_sample_count = audit.get("sample_count")
                if isinstance(candidate_sample_count, (int, float)) and not isinstance(
                    candidate_sample_count, bool
                ):
                    latest_training_sample_count = int(candidate_sample_count)
                    break
            # The prediction window is a fixed trailing slice of the available
            # sessions; a prediction date can only contribute matured OOS
            # evidence once `horizon_days` further sessions exist. Declare that
            # physical ceiling so the promotion gate does not demand more
            # matured dates than this window can ever produce, and records the
            # reason when it lowers the threshold.
            window_capable_dates = max(0, len(prediction_dates) - horizon_days)
            oos_evaluation = (
                self._summarize_oos_evaluation(
                    oos_evaluation_records,
                    horizon_days=horizon_days,
                    label_profile=label_profile_setting,
                    top_n=OOS_EVALUATION_TOP_N,
                    window_capable_dates=window_capable_dates,
                )
                if label_profile_setting in OOS_RETURN_LABEL_PROFILES
                else None
            )
            promotion_evidence: dict[str, object] = {
                "training_window_audits": training_window_audits,
                "training_weight_policy": TRAINING_WEIGHT_POLICY,
            }
            if latest_training_sample_count is not None:
                promotion_evidence["training_sample_count"] = (
                    latest_training_sample_count
                )
            if oos_evaluation is not None:
                promotion_evidence["oos_evaluation"] = oos_evaluation
            else:
                promotion_evidence["oos_evaluation_missing_reason"] = (
                    "walk-forward OOS evidence requires a return-based label "
                    f"profile and at least one matured prediction date "
                    f"(label_profile={label_profile_setting!r})."
                )
            # Per-date audit of the frozen candidate list: missing / rejected /
            # immature outcomes are visible in the artifact instead of being
            # replaced by lower-ranked names.
            if oos_evaluation_records:
                promotion_evidence["oos_candidate_audit"] = [
                    {
                        "trade_date": row.get("trade_date"),
                        "candidate_count": int(row.get("candidate_count") or 0),
                        "labeled_count": int(row.get("sample_count") or 0),
                        "missing_label_count": int(row.get("missing_label_count") or 0),
                        "immature_label_count": int(
                            row.get("immature_label_count") or 0
                        ),
                        "missing_outcome_count": int(
                            row.get("missing_outcome_count") or 0
                        ),
                        "missing_label_reasons": row.get("missing_label_reasons") or {},
                    }
                    for row in oos_evaluation_records
                ]
            # Corporate-action coverage audit for this run's traded window. The
            # rule is shared with the backtest runner
            # (`app.services.corporate_action_coverage`); before this the trainer
            # path had no producer at all, so the gate could only report
            # `NOT_ENOUGH_EVIDENCE` for a model run. A store that is absent or
            # unreadable is recorded as an explicit missing reason -- never as a
            # fabricated "0 unmodeled events".
            coverage = assess_corporate_action_coverage(
                market=run_market,
                symbols=normalized_tickers,
                start_date=prediction_dates[0],
                end_date=prediction_dates[-1],
                holding_days=horizon_days,
            )
            promotion_evidence.update(coverage_evidence_fields(coverage))
            promotion_evidence["corporate_action_coverage_audit"] = coverage
            promotion_evidence["data_readiness_evidence_missing_reason"] = (
                DATA_READINESS_EVIDENCE_MISSING_REASON
            )
            model_repo.merge_config(run_id, promotion_evidence)
            return self._persist_model_outputs(
                db=db,
                model_repo=model_repo,
                prediction_repo=prediction_repo,
                detail_repo=detail_repo,
                explanation_repo=explanation_repo,
                run_id=run_id,
                market=run_market,
                signal_rows=signal_rows,
                detail_rows=detail_rows,
                explanation_rows=explanation_rows,
                model_metadata={
                        "model": run_name,
                        "model_type": model_family,
                        "signal_type": signal_type,
                        "lookback_days": lookback_days,
                        "prediction_horizon_days": horizon_days,
                        "target_profile": label_profile_setting,
                        "training_weight_policy": TRAINING_WEIGHT_POLICY,
                        "training_window_audits": training_window_audits,
                        "training_sample_count": latest_training_sample_count,
                        "oos_evaluation": oos_evaluation,
                        "corporate_action_coverage_audit": coverage,
                        "training_window_policy": asdict(window_policy),
                        "score_semantics": score_semantics_version,
                        "score_contract_version": SCORE_CONTRACT_VERSION,
                        "train_window_target_profile": self._summarize_target_profile(
                            train_window
                        ),
                        "calibration_buckets": oos_calibration_buckets,
                        "train_window_calibration_buckets": calibration_buckets,
                        "calibration_source": "model_calibration_snapshot" if oos_calibration_buckets else "disabled_without_oos",
                        "detail_estimate_calibration_source": (
                            "model_calibration_snapshot"
                            if oos_calibration_buckets
                            else "train_window_calibration"
                            if calibration_buckets
                            else "none"
                        ),
                        "oos_calibration_meta": oos_calibration_meta,
                        "oos_calibration_bucket_count": len(oos_calibration_buckets),
                        "feature_names": feature_names,
                        "feature_families": [
                            "price_trend",
                            "price_extension",
                            "volume_intensity",
                            "intraday_structure",
                            "liquidity_proxy",
                            "listing_maturity",
                            "board_tier",
                            "cn_limit_up_dynamics",
                        ],
                        "feature_enhancement_mode": enhancement_meta.get("mode"),
                        "feature_enhancement_note": enhancement_meta.get("note"),
                        "symbol_context_summary": enhancement_meta.get("coverage"),
                        "prediction_dates": prediction_dates,
                        "label_profile": label_profile_setting,
                        "label_price_basis": label_price_contract["label_price_basis"],
                        "label_price_basis_applicable": label_price_contract[
                            "label_price_basis_applicable"
                        ],
                        "adjusted_view_present": label_price_contract["adjusted_view_present"],
                        "adjusted_view_state": label_price_contract["adjusted_view_state"],
                        "raw_fallback_allowed": label_price_contract["raw_fallback_allowed"],
                        "raw_fallback_optin_audit": label_price_contract.get(
                            "raw_fallback_optin_audit"
                        ),
                        "adjusted_count": label_price_contract["adjusted_count"],
                        "raw_fallback_count": label_price_contract["raw_fallback_count"],
                        "dropped_missing_adjusted_count": label_price_contract[
                            "dropped_missing_adjusted_count"
                        ],
                        "adjusted_coverage_share": label_price_contract[
                            "adjusted_coverage_share"
                        ],
                        "require_full_adjusted_coverage": label_price_contract[
                            "require_full_adjusted_coverage"
                        ],
                        "prediction_price_basis_contract": prediction_price_basis_contract,
                        "entry_not_executable_policy": entry_not_executable_policy_setting,
                        "execution_cost_bps": execution_cost_setting,
                        "evaluation_protocol": "walk_forward_purged_v2",
                        "training_protocol": "point_in_time_purged_v2",
                        "oos_start_date": first_prediction_date,
                        "purge_gap_days": horizon_days,
                        "embargo_sessions": embargo_sessions,
                        "label_availability_rule": "strictly_before_prediction_date",
                        "universe_version": universe_version,
                        "universe_filter_stats": universe_filter_stats,
                        "label_winsorize": label_winsorize_config,
                        "objective": objective_config,
                        "drawdown_penalty": drawdown_penalty_setting,
                        "fit_target": fit_target_setting,
                        "random_seed": random_seed_setting,
                        "feature_transform": feature_transform_config,
                },
            )

    def train(
        self,
        run_name: str = "lightgbm_momentum",
        signal_type: str = "momentum",
        lookback_days: int = 3,
        tickers: list[str] | None = None,
        market: str | None = None,
        universe: str | None = None,
        model_type: str = "lightgbm",
    ) -> int:
        normalized_tickers = {
            str(ticker).strip().upper() for ticker in (tickers or []) if str(ticker).strip()
        } or None
        rows = self._load_rows(tickers=normalized_tickers, market=market)
        if not rows:
            raise RuntimeError("No local market data found. Refresh the Parquet market lake first.")
        if lookback_days < 1:
            raise RuntimeError("lookback_days must be at least 1.")
        normalized_model_type = str(model_type or "lightgbm").strip().lower()
        if normalized_model_type in {"baseline", "local_baseline"}:
            raise RuntimeError(
                "The legacy baseline trainer has been retired. Use model_type=`lightgbm` for all new signal runs."
            )
        model_family_map = {
            "lightgbm": "lightgbm", "lightgbm_multifactor": "lightgbm", "lgbm": "lightgbm",
            "xgboost": "xgboost", "xgboost_multifactor": "xgboost", "xgb": "xgboost",
            "catboost": "catboost", "catboost_multifactor": "catboost", "cat": "catboost",
        }
        model_family = model_family_map.get(normalized_model_type)
        if model_family:
            return self._train_lightgbm(
                run_name=run_name,
                signal_type=signal_type,
                lookback_days=lookback_days,
                normalized_tickers=normalized_tickers,
                market=market,
                universe=universe,
                rows=rows,
                model_family=model_family,
            )
        raise RuntimeError(f"Unsupported model_type `{model_type}`.")
