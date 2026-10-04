import csv
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.tables import ModelRun, Symbol
from app.models.schema import SymbolCreate
from app.services.market_hot_predictions import MarketHotPredictionRepository
from app.services.market_storage_routing import legacy_mirror_write_enabled
from app.services.model_signal_summary import enrich_model_output, summarize_model_output
from app.services.price_basis_contract import probe_adjusted_view
from app.services.prediction_artifacts import select_hot_prediction_rows
from app.services.repository import (
    ModelChartSignalRepository,
    ModelRunRepository,
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionTradePlanRepository,
    PredictionWriteRepository,
    SymbolRepository,
)


def _infer_market_exchange(ticker: str) -> tuple[str, str]:
    normalized = ticker.strip().upper()
    if normalized.endswith(".HK"):
        return "HK", "HKEX"
    if normalized.endswith(".SS"):
        return "CN", "SSE"
    if normalized.endswith(".SZ"):
        return "CN", "SZSE"
    return "US", "NASDAQ"


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def _optional_int(value: str | None) -> int | None:
    number = _optional_float(value)
    if number is None:
        return None
    return int(number)


def _read_artifact_price_basis_binding(artifact_path: str | None) -> str | None:
    """View hash the exported artifact was produced against, if declared.

    External/native artifacts may carry ``adjusted_view_sha256`` (top level) or
    a ``price_basis_contract`` / ``price_basis`` block with ``view_sha256`` in
    their ``manifest.json``. Missing / malformed manifests simply mean "no
    declared basis" and never raise.
    """

    if not artifact_path:
        return None
    artifact = Path(artifact_path)
    if not artifact.is_dir():
        return None
    manifest_path = artifact / "manifest.json"
    if not manifest_path.exists() or not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    nested = payload.get("price_basis_contract") or payload.get("price_basis") or {}
    if not isinstance(nested, dict):
        nested = {}
    declared = payload.get("adjusted_view_sha256") or nested.get("view_sha256")
    return str(declared) if declared else None


def _external_price_basis_contract(market: str, *, artifact_path: str | None) -> dict:
    """Audit the local adjusted-view state for an imported prediction artifact.

    Imported predictions are produced outside this app, so the adjusted view is
    not *consumed* here and the contract is recorded as ``not applicable`` (a
    raw basis) — it must never be mislabelled as an adjusted run. Two guardrails
    still fail closed: a *corrupt* local view for the target market, and an
    artifact that declares a view hash differing from the current view.
    """

    probe = probe_adjusted_view(market, read_bars=False)
    declared_sha256 = _read_artifact_price_basis_binding(artifact_path)
    if probe.unreadable:
        raise RuntimeError(
            "[inference] price-basis contract refused the import: the adjusted view "
            f"for market={market} exists but is unreadable "
            f"(reason={probe.error}). A corrupt view is never treated as 'no view'; "
            "rebuild it (scripts/rebuild_adjusted_view.py) before importing predictions."
        )
    if declared_sha256 is not None and probe.view_sha256 != declared_sha256:
        raise RuntimeError(
            "[inference] price-basis contract refused the import: the artifact "
            f"declares adjusted_view_sha256={declared_sha256} but the current view "
            f"for market={market} is {probe.view_sha256}. The artifact was produced "
            "on a different price basis than the one on disk; rebuild or re-export "
            "it before importing."
        )
    return {
        "entry_point": "inference",
        "decision": "allow_raw_with_authorization",
        "reasons": [],
        "applicable": False,
        "source": "external_model_artifact",
        "view_state": probe.state,
        "view_sha256": probe.view_sha256,
        "actions_sha256": probe.actions_sha256,
        "coverage_share": round(float(probe.coverage_share), 8),
        "missing_rows": probe.missing_rows,
        "missing_symbols": list(probe.missing_symbols),
        "fallback_policy": "external_artifact",
        "authorized_by": "external_model_artifact",
        "declared_view_sha256": declared_sha256,
        "version_match": (
            None if declared_sha256 is None else probe.view_sha256 == declared_sha256
        ),
        "label_price_basis": "raw",
    }


class ExternalModelOutputImporter:
    """Import model outputs from an external CSV into predictions and prediction_details."""

    def import_csv(
        self,
        csv_path: Path,
        *,
        run_name: str,
        model_type: str = "qlib_external",
        market: str | None = None,
        universe: str | None = None,
        artifact_path: str | None = None,
    ) -> dict:
        if not csv_path.exists():
            raise RuntimeError(f"CSV not found: {csv_path}")

        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)

        if not rows:
            raise RuntimeError("External model CSV is empty.")

        required = {"ticker", "trade_date", "score"}
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"External model CSV is missing required columns: {', '.join(missing)}")

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[str(row["trade_date"])].append(row)

        inferred_markets = {
            _infer_market_exchange(str(row.get("ticker") or ""))[0]
            for row in rows
            if str(row.get("ticker") or "").strip()
        }
        requested_market = str(market or "").strip().upper()
        if requested_market in {"ALL", "MIXED"}:
            raise RuntimeError(
                "External model imports must target exactly one market."
            )
        if requested_market and inferred_markets != {requested_market}:
            raise RuntimeError(
                "External model import ticker markets do not match the requested "
                f"market {requested_market}: {sorted(inferred_markets)}"
            )
        if not requested_market:
            if len(inferred_markets) != 1:
                raise RuntimeError(
                    "External model imports must contain exactly one inferred market."
                )
            requested_market = next(iter(inferred_markets))

        prepared_predictions: list[dict] = []
        prepared_details: list[dict] = []
        # Probe/record the shared price-basis contract before any run row exists:
        # a corrupt local view or a basis-mismatched artifact must not create a
        # model run that downstream consumers could trust.
        price_basis_contract = (
            _external_price_basis_contract(requested_market, artifact_path=artifact_path)
            if requested_market in {"CN", "US"}
            else None
        )

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            detail_repo = PredictionDetailRepository(db)
            explanation_repo = PredictionExplanationRepository(db)

            all_dates = sorted(grouped.keys())
            run = model_repo.create_run(
                name=run_name,
                model_type=model_type,
                market=requested_market,
                universe=universe,
                train_start=all_dates[0] if all_dates else None,
                train_end=all_dates[-1] if all_dates else None,
                test_start=all_dates[0] if all_dates else None,
                test_end=all_dates[-1] if all_dates else None,
                config={
                    "source": "external_csv",
                    "csv_path": str(csv_path),
                    "price_basis_contract": price_basis_contract,
                },
                artifact_path=artifact_path,
                status="running",
            )

            for trade_date, date_rows in grouped.items():
                ranked = sorted(date_rows, key=lambda item: float(item["score"]), reverse=True)
                for index, row in enumerate(ranked, start=1):
                    ticker = str(row["ticker"]).strip().upper()
                    symbol = symbol_repo.get_by_ticker(ticker)
                    if symbol is None:
                        inferred_market, inferred_exchange = _infer_market_exchange(ticker)
                        symbol = symbol_repo.get_or_create_symbol(
                            SymbolCreate(
                                ticker=ticker,
                                name=(row.get("name") or "").strip() or ticker,
                                market=inferred_market,
                                exchange=inferred_exchange,
                            )
                        )

                    score = float(row["score"])
                    rank_value = _optional_float(row.get("rank_value")) or float(index)
                    enriched = enrich_model_output(
                        {
                            "score": score,
                            "rank_value": rank_value,
                            "universe_size": _optional_int(row.get("universe_size")) or len(ranked),
                            "percentile": _optional_float(row.get("percentile")),
                            "target_horizon_days": _optional_int(row.get("target_horizon_days")),
                            "confidence": _optional_float(row.get("confidence")),
                            "bullish_prob": _optional_float(row.get("bullish_prob")),
                            "bearish_prob": _optional_float(row.get("bearish_prob")),
                            "expected_return_5d": _optional_float(row.get("expected_return_5d")),
                            "expected_return_20d": _optional_float(row.get("expected_return_20d")),
                            "expected_drawdown_20d": _optional_float(row.get("expected_drawdown_20d")),
                            "model_reward_risk_ratio": _optional_float(row.get("model_reward_risk_ratio")),
                            "risk_score": _optional_float(row.get("risk_score")),
                            "regime_label": (row.get("regime_label") or "").strip() or None,
                            "conviction_bucket": (row.get("conviction_bucket") or "").strip() or None,
                            "position_size_hint": (row.get("position_size_hint") or "").strip() or None,
                            "entry_style": (row.get("entry_style") or "").strip() or None,
                            "signal_label": (row.get("signal_label") or "").strip() or None,
                            "signal_strength": _optional_float(row.get("signal_strength")),
                            "summary_text": (row.get("summary_text") or "").strip() or None,
                            "model_run": {"name": run_name},
                        },
                        lang="en",
                    ) or {}

                    prepared_predictions.append(
                        {
                            "symbol_id": symbol.id,
                            "trade_date": trade_date,
                            "score": score,
                            "rank_value": rank_value,
                        }
                    )
                    prepared_details.append(
                        {
                            "symbol_id": symbol.id,
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
                    )

            if not prepared_predictions:
                model_repo.complete_run(run.id, status="failed", artifact_path=artifact_path)
                raise RuntimeError("External model CSV produced no importable predictions.")

            physical_market = requested_market in {"CN", "US"}
            symbol_markets = {
                str(value or "").strip().upper()
                for value in db.scalars(
                    select(Symbol.market).where(
                        Symbol.id.in_(
                            {
                                int(row["symbol_id"])
                                for row in prepared_predictions
                            }
                        )
                    )
                )
            }
            if symbol_markets != {requested_market}:
                model_repo.complete_run(
                    run.id,
                    status="failed",
                    artifact_path=artifact_path,
                )
                raise RuntimeError("External model run market identity changed during import.")

            settings = get_settings()
            hot_predictions = prepared_predictions
            if physical_market:
                hot_predictions = select_hot_prediction_rows(
                    prepared_predictions,
                    full_trade_days=int(settings.prediction_hot_full_trade_days),
                    top_k=int(settings.prediction_hot_top_k),
                )
            hot_keys = {
                (int(row["symbol_id"]), str(row["trade_date"]))
                for row in hot_predictions
            }
            hot_details = [
                row
                for row in prepared_details
                if (int(row["symbol_id"]), str(row["trade_date"])) in hot_keys
            ]
            write_legacy = not physical_market or legacy_mirror_write_enabled(
                db,
                market=requested_market,
                configured=bool(settings.market_physical_hot_dual_write_legacy),
            )
            try:
                if write_legacy:
                    prediction_repo.replace_for_model_run(
                        run.id,
                        hot_predictions,
                        commit=False,
                    )
                    detail_repo.replace_for_model_run(
                        run.id,
                        hot_details,
                        commit=False,
                    )
                    explanation_repo.replace_for_model_run(
                        run.id,
                        [],
                        commit=False,
                    )
                if physical_market:
                    MarketHotPredictionRepository(db).publish_for_model_run(
                        model_run_id=run.id,
                        market=requested_market,
                        prediction_rows=hot_predictions,
                        detail_rows=hot_details,
                        explanation_rows=[],
                        commit=False,
                    )
                model_repo.merge_config(
                    run.id,
                    {
                        "prediction_storage_contract": {
                            "contract_version": "prediction-storage-v1",
                            "hot_write_mode": "compact",
                            "hot_full_trade_days": int(
                                settings.prediction_hot_full_trade_days
                            ),
                            "hot_top_k": int(settings.prediction_hot_top_k),
                            "legacy_hot_dual_write": bool(write_legacy),
                            "primary_market_table": bool(physical_market),
                            "live_publication": False,
                            "source_payload": "external_model_artifact",
                        }
                    },
                    commit=False,
                )
                model_repo.complete_run(
                    run.id,
                    status="success",
                    artifact_path=artifact_path or str(csv_path.resolve()),
                    commit=False,
                )
                db.commit()
            except Exception:
                db.rollback()
                model_repo.complete_run(
                    run.id,
                    status="failed",
                    artifact_path=artifact_path or str(csv_path.resolve()),
                )
                raise

            return {
                "model_run_id": run.id,
                "run_name": run.name,
                "market": requested_market,
                "predictions_written": len(hot_predictions),
                "details_written": len(hot_details),
                "trade_dates": all_dates,
                "physical_market_table": physical_market,
                "legacy_hot_dual_write": bool(write_legacy),
            }

    def import_explanations_csv(self, csv_path: Path, *, model_run_id: int) -> int:
        if not csv_path.exists():
            raise RuntimeError(f"Explanation CSV not found: {csv_path}")

        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)

        if not rows:
            return 0

        required = {"ticker", "trade_date", "feature_name"}
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"Explanation CSV is missing required columns: {', '.join(missing)}")

        return self.import_explanations_rows(rows, model_run_id=model_run_id)

    def import_explanations_rows(self, rows: list[dict], *, model_run_id: int) -> int:
        prepared_rows: list[dict] = []
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_run = db.scalar(
                select(ModelRun).where(ModelRun.id == int(model_run_id))
            )
            if model_run is None:
                raise RuntimeError(f"Model run {int(model_run_id)} was not found.")
            market = str(model_run.market or "").strip().upper()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                trade_date = str(row.get("trade_date") or "").strip()
                feature_name = str(row.get("feature_name") or "").strip()
                if not ticker or not trade_date or not feature_name:
                    continue
                symbol = symbol_repo.get_by_ticker(ticker)
                if symbol is None:
                    continue
                prepared_rows.append(
                    {
                        "symbol_id": symbol.id,
                        "trade_date": trade_date,
                        "feature_name": feature_name,
                        "feature_value": _optional_float(row.get("feature_value")),
                        "contribution": _optional_float(row.get("contribution")),
                        "direction": (row.get("direction") or "").strip() or None,
                        "display_order": _optional_int(row.get("display_order")),
                    }
                )
            physical_market = market in {"CN", "US"}
            settings = get_settings()
            write_legacy = not physical_market or legacy_mirror_write_enabled(
                db,
                market=market,
                configured=bool(settings.market_physical_hot_dual_write_legacy),
            )
            try:
                if write_legacy:
                    explanation_repo.replace_for_model_run(
                        model_run_id,
                        prepared_rows,
                        commit=False,
                    )
                if physical_market:
                    physical_repo = MarketHotPredictionRepository(db)
                    prediction_rows, detail_rows, _ = physical_repo._load_rows(
                        market=market,
                        model_run_id=int(model_run_id),
                    )
                    hot_keys = {
                        (int(row["symbol_id"]), str(row["trade_date"]))
                        for row in prediction_rows
                    }
                    physical_explanations = [
                        row
                        for row in prepared_rows
                        if (int(row["symbol_id"]), str(row["trade_date"]))
                        in hot_keys
                    ]
                    physical_repo.publish_for_model_run(
                        model_run_id=int(model_run_id),
                        market=market,
                        prediction_rows=prediction_rows,
                        detail_rows=detail_rows,
                        explanation_rows=physical_explanations,
                        commit=False,
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            return len(prepared_rows)

    def import_chart_signals_csv(self, csv_path: Path, *, model_run_id: int) -> int:
        if not csv_path.exists():
            raise RuntimeError(f"Chart signals CSV not found: {csv_path}")

        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)

        if not rows:
            return 0

        required = {"ticker", "trade_date"}
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"Chart signals CSV is missing required columns: {', '.join(missing)}")

        return self.import_chart_signals_rows(rows, model_run_id=model_run_id)

    def import_chart_signals_rows(self, rows: list[dict], *, model_run_id: int) -> int:
        prepared_rows: list[dict] = []
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            chart_signal_repo = ModelChartSignalRepository(db)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                trade_date = str(row.get("trade_date") or "").strip()
                if not ticker or not trade_date:
                    continue
                symbol = symbol_repo.get_by_ticker(ticker)
                if symbol is None:
                    continue
                prepared_rows.append(
                    {
                        "symbol_id": symbol.id,
                        "trade_date": trade_date,
                        "score": _optional_float(row.get("score")),
                        "rank_value": _optional_float(row.get("rank_value")),
                        "signal_label": (row.get("signal_label") or "").strip() or None,
                        "signal_strength": _optional_float(row.get("signal_strength")),
                        "note": (row.get("note") or "").strip() or None,
                    }
                )
            return chart_signal_repo.replace_for_model_run(model_run_id, prepared_rows)

    def import_trade_plan_json(self, json_path: Path, *, model_run_id: int) -> int:
        if not json_path.exists():
            raise RuntimeError(f"Trade plan JSON not found: {json_path}")

        import json

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            payload = [payload]

        return self.import_trade_plan_payload(payload, model_run_id=model_run_id)

    def import_trade_plan_payload(self, payload: list[dict] | dict, *, model_run_id: int) -> int:
        items = payload if isinstance(payload, list) else [payload]
        prepared_rows: list[dict] = []
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            trade_plan_repo = PredictionTradePlanRepository(db)
            for item in items:
                if not isinstance(item, dict):
                    continue
                ticker = str(item.get("ticker") or "").strip().upper()
                trade_date = str(item.get("trade_date") or "").strip()
                if not ticker or not trade_date:
                    continue
                symbol = symbol_repo.get_by_ticker(ticker)
                if symbol is None:
                    continue
                prepared_rows.append(
                    {
                        "symbol_id": symbol.id,
                        "trade_date": trade_date,
                        "entry_low": _optional_float(item.get("entry_low")),
                        "entry_high": _optional_float(item.get("entry_high")),
                        "breakout_level": _optional_float(item.get("breakout_level")),
                        "take_profit_low": _optional_float(item.get("take_profit_low")),
                        "take_profit_high": _optional_float(item.get("take_profit_high")),
                        "risk_level": _optional_float(item.get("risk_level")),
                        "support_level": _optional_float(item.get("support_level")),
                        "resistance_level": _optional_float(item.get("resistance_level")),
                        "stop_type": (item.get("stop_type") or "").strip() or None,
                        "trailing_stop_pct": _optional_float(item.get("trailing_stop_pct")),
                        "invalidation_reason": (item.get("invalidation_reason") or "").strip() or None,
                        "execution_tags": item.get("execution_tags") if isinstance(item.get("execution_tags"), list) else [],
                        "note": (item.get("note") or "").strip() or None,
                    }
                )
            return trade_plan_repo.replace_for_model_run(model_run_id, prepared_rows)
