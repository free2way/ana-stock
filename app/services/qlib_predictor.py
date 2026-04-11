from pathlib import Path
import json
from typing import Any

from app.core.config import get_settings
from app.services.model_output_importer import ExternalModelOutputImporter
from app.services.qlib_prediction_adapter import QlibPredictionAdapter


class QlibPredictor:
    """Thin predictor/import bridge for future native Qlib inference."""

    ARTIFACT_PREDICTIONS_FILENAME = "predictions.csv"
    ARTIFACT_PREDICTIONS_JSON_FILENAME = "predictions.json"
    ARTIFACT_PREDICTIONS_JSONL_FILENAME = "predictions.jsonl"
    ARTIFACT_NATIVE_RESULT_FILENAME = "native_result.json"
    ARTIFACT_MANIFEST_FILENAME = "manifest.json"
    ARTIFACT_EXPLANATIONS_FILENAME = "explanations.csv"
    ARTIFACT_FEATURES_FILENAME = "features.csv"
    ARTIFACT_CHART_SIGNALS_FILENAME = "chart_signals.csv"
    ARTIFACT_TRADE_PLAN_FILENAME = "trade_plan.json"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.adapter = QlibPredictionAdapter()

    def is_available(self) -> bool:
        return self.adapter.is_available()

    def validate_environment(self, *, artifact_path: str | None = None) -> dict:
        qlib_dir = self.settings.qlib_data_dir
        issues: list[str] = []
        artifact_predictions_csv = self._resolve_artifact_predictions_csv(artifact_path)

        if not self.is_available() and artifact_predictions_csv is None:
            issues.append("Qlib is not installed. Install it with `.venv/bin/pip install -r requirements-qlib.txt`.")
        if artifact_predictions_csv is None and not qlib_dir.exists():
            issues.append(
                f"Qlib dataset directory does not exist: {qlib_dir}. Build it with `scripts/build_dataset.py`."
            )
        elif artifact_predictions_csv is None and not any(qlib_dir.iterdir()):
            issues.append(
                f"Qlib dataset directory is empty: {qlib_dir}. Build it with `scripts/build_dataset.py`."
            )
        if artifact_path:
            artifact = Path(artifact_path)
            if not artifact.exists():
                issues.append(f"Model artifact was not found: {artifact}")

        return {
            "available": self.is_available(),
            "qlib_dir": str(qlib_dir),
            "artifact_path": artifact_path,
            "artifact_predictions_csv": str(artifact_predictions_csv) if artifact_predictions_csv else None,
            "ready": not issues,
            "issues": issues,
        }

    def expected_input_columns(self) -> list[str]:
        return list(QlibPredictionAdapter.FIELDNAMES)

    def build_prediction_schema(self) -> dict[str, Any]:
        return {
            "required": ["ticker", "trade_date", "score"],
            "optional": [
                "name",
                "rank_value",
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
            ],
            "fieldnames": self.expected_input_columns(),
        }

    def build_native_inference_manifest(
        self,
        *,
        run_name: str,
        artifact_path: str | None = None,
        market: str | None = None,
        universe: str | None = None,
    ) -> dict[str, Any]:
        status = self.validate_environment(artifact_path=artifact_path)
        return {
            "mode": "native_inference",
            "run_name": run_name,
            "model_type": "qlib_native",
            "market": market,
            "universe": universe,
            "environment": status,
            "expected_schema": self.build_prediction_schema(),
            "accepted_artifact_layouts": [
                {
                    "type": "directory",
                    "description": "Artifact directory containing a native predictions export and optional metadata manifest.",
                    "required_files": [self.ARTIFACT_PREDICTIONS_FILENAME],
                    "accepted_prediction_files": [
                        self.ARTIFACT_PREDICTIONS_FILENAME,
                        self.ARTIFACT_PREDICTIONS_JSON_FILENAME,
                        self.ARTIFACT_PREDICTIONS_JSONL_FILENAME,
                        self.ARTIFACT_NATIVE_RESULT_FILENAME,
                    ],
                    "optional_files": [
                        self.ARTIFACT_MANIFEST_FILENAME,
                        self.ARTIFACT_EXPLANATIONS_FILENAME,
                        self.ARTIFACT_FEATURES_FILENAME,
                        self.ARTIFACT_CHART_SIGNALS_FILENAME,
                        self.ARTIFACT_TRADE_PLAN_FILENAME,
                    ],
                },
                {
                    "type": "file",
                    "description": "A direct path to a Qlib-style predictions CSV or JSON payload.",
                    "accepted_filenames": [
                        self.ARTIFACT_PREDICTIONS_FILENAME,
                        self.ARTIFACT_PREDICTIONS_JSON_FILENAME,
                        self.ARTIFACT_PREDICTIONS_JSONL_FILENAME,
                        self.ARTIFACT_NATIVE_RESULT_FILENAME,
                        "*.csv",
                        "*.json",
                        "*.jsonl",
                    ],
                },
            ],
            "accepted_manifest_fields": [
                "run_name",
                "market",
                "universe",
                "model_type",
                "artifact_path",
                "target_horizon_days",
                "trade_date",
            ],
            "accepted_explanation_schema": {
                "required": ["ticker", "trade_date", "feature_name"],
                "optional": ["feature_value", "contribution", "direction", "display_order"],
                "accepted_filenames": [self.ARTIFACT_EXPLANATIONS_FILENAME, self.ARTIFACT_FEATURES_FILENAME],
            },
            "accepted_chart_signal_schema": {
                "required": ["ticker", "trade_date"],
                "optional": ["score", "rank_value", "signal_label", "signal_strength", "note"],
                "accepted_filenames": [self.ARTIFACT_CHART_SIGNALS_FILENAME],
            },
            "accepted_trade_plan_schema": {
                "required": ["ticker", "trade_date"],
                "optional": [
                    "entry_low",
                    "entry_high",
                    "breakout_level",
                    "take_profit_low",
                    "take_profit_high",
                    "risk_level",
                    "support_level",
                    "resistance_level",
                    "stop_type",
                    "trailing_stop_pct",
                    "invalidation_reason",
                    "execution_tags",
                    "note",
                ],
                "accepted_filenames": [self.ARTIFACT_TRADE_PLAN_FILENAME],
            },
            "accepted_native_result_schema": {
                "required": ["predictions"],
                "optional": [
                    "manifest",
                    "explanations",
                    "features",
                    "chart_signals",
                    "trade_plan",
                ],
                "accepted_filenames": [self.ARTIFACT_NATIVE_RESULT_FILENAME],
            },
            "next_step": (
                "You can either place a Qlib-style predictions CSV at "
                f"`<artifact_path>/{self.ARTIFACT_PREDICTIONS_FILENAME}`, `{self.ARTIFACT_PREDICTIONS_JSON_FILENAME}`, "
                f"`{self.ARTIFACT_PREDICTIONS_JSONL_FILENAME}`, or pass one of those files directly as `artifact_path`. "
                f"If `{self.ARTIFACT_MANIFEST_FILENAME}` is present, its metadata will be merged into the import context. "
                f"If `{self.ARTIFACT_EXPLANATIONS_FILENAME}` or `{self.ARTIFACT_FEATURES_FILENAME}` is present, feature "
                "contributions will also be imported into `prediction_explanations`. "
                f"If `{self.ARTIFACT_CHART_SIGNALS_FILENAME}` is present, the insight chart will use those historical "
                "signals before falling back to score-derived labels. "
                f"If `{self.ARTIFACT_TRADE_PLAN_FILENAME}` is present, insight execution levels will use that trade "
                "plan before falling back to the local price-structure engine. "
                f"If `{self.ARTIFACT_NATIVE_RESULT_FILENAME}` is present, predictions and optional companion payloads "
                "can be imported from one structured artifact. "
                "For a fully native predictor later, implement `_run_native_inference()` so it returns rows that match "
                "`expected_schema`."
            ),
        }

    def import_prediction_csv(
        self,
        csv_path: Path,
        *,
        run_name: str,
        model_type: str = "qlib_native",
        market: str | None = None,
        universe: str | None = None,
        artifact_path: str | None = None,
    ) -> dict:
        return self.adapter.import_prediction_rows(
            self._read_prediction_rows(csv_path),
            run_name=run_name,
            model_type=model_type,
            market=market,
            universe=universe,
        )

    def predict(
        self,
        *,
        run_name: str,
        artifact_path: str | None = None,
        market: str | None = None,
        universe: str | None = None,
        predictions_csv: Path | None = None,
    ) -> dict:
        """Current bridge behavior:
        - validates Qlib environment
        - if a predictions CSV is provided, imports it into the unified app layer
        - otherwise returns guidance for the future native inference hook
        """
        status = self.validate_environment(artifact_path=artifact_path)
        if predictions_csv is not None:
            if not predictions_csv.exists():
                raise RuntimeError(f"Predictions CSV not found: {predictions_csv}")
            imported = self.import_prediction_csv(
                predictions_csv,
                run_name=run_name,
                model_type="qlib_native",
                market=market,
                universe=universe,
                artifact_path=artifact_path,
            )
            native_companions = self._import_native_result_companions(
                model_run_id=imported["model_run_id"],
                artifact_path=artifact_path,
            )
            imported["explanations_written"] = native_companions["explanations_written"] or self._import_artifact_explanations(
                model_run_id=imported["model_run_id"],
                artifact_path=artifact_path,
            )
            imported["chart_signals_written"] = native_companions["chart_signals_written"] or self._import_artifact_chart_signals(
                model_run_id=imported["model_run_id"],
                artifact_path=artifact_path,
            )
            imported["trade_plans_written"] = native_companions["trade_plans_written"] or self._import_artifact_trade_plan(
                model_run_id=imported["model_run_id"],
                artifact_path=artifact_path,
            )
            imported["mode"] = "import_csv"
            imported["environment"] = status
            return imported

        if not status["ready"]:
            raise RuntimeError(" ".join(status["issues"]))

        artifact_metadata = self._read_artifact_manifest(artifact_path)
        rows = self._run_native_inference(
            run_name=run_name,
            artifact_path=artifact_path,
            market=market,
            universe=universe,
        )
        imported = self.adapter.import_prediction_rows(
            rows,
            run_name=str(artifact_metadata.get("run_name") or run_name),
            model_type=str(artifact_metadata.get("model_type") or "qlib_native"),
            market=str(artifact_metadata.get("market") or market) if (artifact_metadata.get("market") or market) else None,
            universe=str(artifact_metadata.get("universe") or universe) if (artifact_metadata.get("universe") or universe) else None,
        )
        native_companions = self._import_native_result_companions(
            model_run_id=imported["model_run_id"],
            artifact_path=artifact_path,
        )
        imported["explanations_written"] = native_companions["explanations_written"] or self._import_artifact_explanations(
            model_run_id=imported["model_run_id"],
            artifact_path=artifact_path,
        )
        imported["chart_signals_written"] = native_companions["chart_signals_written"] or self._import_artifact_chart_signals(
            model_run_id=imported["model_run_id"],
            artifact_path=artifact_path,
        )
        imported["trade_plans_written"] = native_companions["trade_plans_written"] or self._import_artifact_trade_plan(
            model_run_id=imported["model_run_id"],
            artifact_path=artifact_path,
        )
        imported["mode"] = "native_inference"
        imported["environment"] = status
        if artifact_metadata:
            imported["artifact_manifest"] = artifact_metadata
        return imported

    def _read_prediction_rows(self, csv_path: Path) -> list[dict]:
        import csv

        suffix = csv_path.suffix.lower()
        if csv_path.name == self.ARTIFACT_NATIVE_RESULT_FILENAME:
            with csv_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise RuntimeError(f"Native result payload must be a JSON object: {csv_path}")
            rows = payload.get("predictions")
            if not isinstance(rows, list):
                raise RuntimeError(f"`predictions` must be a list inside native result payload: {csv_path}")
            return rows
        if suffix == ".json":
            with csv_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, list):
                raise RuntimeError(f"Prediction JSON payload must be a list of objects: {csv_path}")
            rows: list[dict] = []
            for row in payload:
                if not isinstance(row, dict):
                    raise RuntimeError(f"Prediction JSON rows must be objects: {csv_path}")
                rows.append(row)
            return rows
        if suffix == ".jsonl":
            rows: list[dict] = []
            with csv_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise RuntimeError(f"Prediction JSONL rows must be objects: {csv_path}")
                    rows.append(row)
            return rows

        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _resolve_artifact_predictions_csv(self, artifact_path: str | None) -> Path | None:
        if not artifact_path:
            return None
        artifact = Path(artifact_path)
        if artifact.is_file():
            if artifact.suffix.lower() in {".csv", ".json", ".jsonl"}:
                return artifact
            return None
        for filename in (
            self.ARTIFACT_NATIVE_RESULT_FILENAME,
            self.ARTIFACT_PREDICTIONS_FILENAME,
            self.ARTIFACT_PREDICTIONS_JSON_FILENAME,
            self.ARTIFACT_PREDICTIONS_JSONL_FILENAME,
        ):
            candidate = artifact / filename
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _read_artifact_manifest(self, artifact_path: str | None) -> dict[str, Any]:
        if not artifact_path:
            return {}
        artifact = Path(artifact_path)
        manifest_path = artifact / self.ARTIFACT_MANIFEST_FILENAME if artifact.is_dir() else None
        if manifest_path is not None and manifest_path.exists() and manifest_path.is_file():
            with manifest_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise RuntimeError(f"Artifact manifest must be a JSON object: {manifest_path}")
            return payload
        native_payload = self._read_native_result_payload(artifact_path)
        manifest = native_payload.get("manifest")
        if manifest is None:
            return {}
        if not isinstance(manifest, dict):
            raise RuntimeError("`manifest` inside native_result.json must be a JSON object.")
        return manifest

    def _resolve_artifact_explanations_csv(self, artifact_path: str | None) -> Path | None:
        if not artifact_path:
            return None
        artifact = Path(artifact_path)
        if artifact.is_file():
            return None
        for filename in (self.ARTIFACT_EXPLANATIONS_FILENAME, self.ARTIFACT_FEATURES_FILENAME):
            candidate = artifact / filename
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _import_artifact_explanations(self, *, model_run_id: int, artifact_path: str | None) -> int:
        explanations_csv = self._resolve_artifact_explanations_csv(artifact_path)
        if explanations_csv is None:
            return 0
        importer = ExternalModelOutputImporter()
        return importer.import_explanations_csv(explanations_csv, model_run_id=model_run_id)

    def _resolve_artifact_chart_signals_csv(self, artifact_path: str | None) -> Path | None:
        if not artifact_path:
            return None
        artifact = Path(artifact_path)
        if artifact.is_file():
            return None
        candidate = artifact / self.ARTIFACT_CHART_SIGNALS_FILENAME
        if candidate.exists() and candidate.is_file():
            return candidate
        return None

    def _import_artifact_chart_signals(self, *, model_run_id: int, artifact_path: str | None) -> int:
        chart_signals_csv = self._resolve_artifact_chart_signals_csv(artifact_path)
        if chart_signals_csv is None:
            return 0
        importer = ExternalModelOutputImporter()
        return importer.import_chart_signals_csv(chart_signals_csv, model_run_id=model_run_id)

    def _resolve_artifact_trade_plan_json(self, artifact_path: str | None) -> Path | None:
        if not artifact_path:
            return None
        artifact = Path(artifact_path)
        if artifact.is_file():
            return None
        candidate = artifact / self.ARTIFACT_TRADE_PLAN_FILENAME
        if candidate.exists() and candidate.is_file():
            return candidate
        return None

    def _import_artifact_trade_plan(self, *, model_run_id: int, artifact_path: str | None) -> int:
        trade_plan_json = self._resolve_artifact_trade_plan_json(artifact_path)
        if trade_plan_json is None:
            return 0
        importer = ExternalModelOutputImporter()
        return importer.import_trade_plan_json(trade_plan_json, model_run_id=model_run_id)

    def _read_native_result_payload(self, artifact_path: str | None) -> dict[str, Any]:
        if not artifact_path:
            return {}
        artifact = Path(artifact_path)
        payload_path = artifact if artifact.is_file() and artifact.name == self.ARTIFACT_NATIVE_RESULT_FILENAME else artifact / self.ARTIFACT_NATIVE_RESULT_FILENAME
        if not payload_path.exists() or not payload_path.is_file():
            return {}
        with payload_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Native result payload must be a JSON object: {payload_path}")
        return payload

    def _import_native_result_companions(self, *, model_run_id: int, artifact_path: str | None) -> dict[str, int]:
        payload = self._read_native_result_payload(artifact_path)
        if not payload:
            return {"explanations_written": 0, "chart_signals_written": 0, "trade_plans_written": 0}
        importer = ExternalModelOutputImporter()
        explanations = payload.get("explanations")
        if not isinstance(explanations, list):
            explanations = payload.get("features")
        chart_signals = payload.get("chart_signals")
        trade_plan = payload.get("trade_plan")
        return {
            "explanations_written": importer.import_explanations_rows(explanations, model_run_id=model_run_id) if isinstance(explanations, list) else 0,
            "chart_signals_written": importer.import_chart_signals_rows(chart_signals, model_run_id=model_run_id) if isinstance(chart_signals, list) else 0,
            "trade_plans_written": importer.import_trade_plan_payload(trade_plan, model_run_id=model_run_id) if trade_plan is not None else 0,
        }

    def _run_native_inference(
        self,
        *,
        run_name: str,
        artifact_path: str | None = None,
        market: str | None = None,
        universe: str | None = None,
    ) -> list[dict]:
        csv_path = self._resolve_artifact_predictions_csv(artifact_path)
        if csv_path is not None:
            return self._read_prediction_rows(csv_path)
        raise NotImplementedError(
            "Native Qlib inference is not wired in yet. "
            f"Provide `{self.ARTIFACT_PREDICTIONS_FILENAME}`, `{self.ARTIFACT_PREDICTIONS_JSON_FILENAME}`, "
            f"`{self.ARTIFACT_PREDICTIONS_JSONL_FILENAME}`, or `{self.ARTIFACT_NATIVE_RESULT_FILENAME}` inside the artifact directory, "
            "pass one of those files directly as `artifact_path`, use `build_native_inference_manifest()` for the expected schema, "
            "or import a Qlib-style CSV with `scripts/run_qlib_predictor.py --predictions-csv ...`."
        )
