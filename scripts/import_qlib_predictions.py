import argparse
import csv
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.qlib_prediction_adapter import QlibPredictionAdapter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Qlib-style prediction rows into predictions and prediction_details."
    )
    parser.add_argument("--csv", required=True, help="Path to a CSV with Qlib-style prediction rows.")
    parser.add_argument("--run-name", required=True, help="Name for the imported model run.")
    parser.add_argument("--model-type", default="qlib_external", help="Model type label for the imported run.")
    parser.add_argument("--market", default=None, help="Optional market label.")
    parser.add_argument("--universe", default=None, help="Optional universe label.")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return

    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    adapter = QlibPredictionAdapter()
    try:
        result = adapter.import_prediction_rows(
            rows,
            run_name=args.run_name,
            model_type=args.model_type,
            market=args.market,
            universe=args.universe,
        )
        print(
            f"Imported {result['predictions_written']} predictions and {result['details_written']} details "
            f"into model run {result['model_run_id']} ({result['run_name']})."
        )
    except Exception as exc:
        print(exc)


if __name__ == "__main__":
    main()
