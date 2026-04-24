import csv
from pathlib import Path

from app.core.config import get_settings
from app.services.model_output_importer import ExternalModelOutputImporter
from app.services.repository import utc_now_iso


class QlibPredictionAdapter:
    """Bridge Qlib-style prediction rows into the app's unified prediction_details layer."""

    FIELDNAMES = [
        "ticker",
        "name",
        "trade_date",
        "score",
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
    ]

    def __init__(self) -> None:
        self.settings = get_settings()

    def is_available(self) -> bool:
        try:
            import qlib  # noqa: F401
        except ImportError:
            return False
        return True

    def build_import_csv(self, rows: list[dict], *, run_name: str) -> Path:
        if not rows:
            raise RuntimeError("No Qlib prediction rows were provided.")

        out_dir = self.settings.artifacts_dir / "qlib_imports"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in run_name).strip("_") or "qlib_run"
        output_path = out_dir / f"{safe_name}_{utc_now_iso().replace(':', '-')}.csv"

        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field) for field in self.FIELDNAMES})
        return output_path

    def import_prediction_rows(
        self,
        rows: list[dict],
        *,
        run_name: str,
        model_type: str = "qlib_external",
        market: str | None = None,
        universe: str | None = None,
    ) -> dict:
        csv_path = self.build_import_csv(rows, run_name=run_name)
        importer = ExternalModelOutputImporter()
        return importer.import_csv(
            csv_path,
            run_name=run_name,
            model_type=model_type,
            market=market,
            universe=universe,
            artifact_path=str(csv_path),
        )
